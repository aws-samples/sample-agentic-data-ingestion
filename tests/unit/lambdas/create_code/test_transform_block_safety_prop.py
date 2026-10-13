# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for the T-01 transform-block guard.

Two properties, both stated from the prompt's contract rather than from the
guard's output:

1. Any chain of statements in the shape ``transform_generation.j2`` asks for
   (one ``.withColumn``/``.withColumnRenamed``/``.drop``/``.filter`` per line,
   the first reading ``df``, each next reading ``df_transformed``, built from
   the templates' function vocabulary) is accepted.
2. Inserting one statement that reaches a module-scope name of the rendered
   Glue job, at any position in such a chain, makes the block rejected.
3. A forbidden Spark SQL word split into fragments that no single literal
   contains, and reassembled by an operation the guard permits elsewhere,
   is rejected at every SQL-text sink. This is the shape a token test misses.
"""

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

_CREATE_CODE_DIR = Path(__file__).resolve().parents[4] / "lambda" / "agentic_data_ingestion_create_code"
if str(_CREATE_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_CODE_DIR))

from transform_safety import UnsafeTransformBlockError, validate_transform_block_safety

#: Words the guard refuses inside Spark SQL text; generated column names avoid
#: them so property 1 tests the statement shape, not the word scan.
_SQL_WORDS = {"select", "reflect", "try_reflect", "java_method"}

_column = st.from_regex(r"[a-z][a-z0-9_]{0,11}", fullmatch=True).filter(lambda name: name not in _SQL_WORDS)

#: Single-column expressions built only from the templates' vocabulary.
_UNARY_WRAPPERS = ("lower", "upper", "trim", "initcap")
_CASTS = ("StringType()", "IntegerType()", "LongType()", "DoubleType()", "BooleanType()")


@st.composite
def _statement(draw: st.DrawFn, frame: str) -> str:
    target = draw(_column)
    source = draw(_column)
    kind = draw(st.sampled_from(("wrap", "cast", "date", "rename", "drop", "filter", "coalesce")))
    if kind == "wrap":
        return f'df_transformed = {frame}.withColumn("{target}", {draw(st.sampled_from(_UNARY_WRAPPERS))}(col("{source}")))'
    if kind == "cast":
        return f'df_transformed = {frame}.withColumn("{target}", col("{source}").cast({draw(st.sampled_from(_CASTS))}))'
    if kind == "date":
        return f'df_transformed = {frame}.withColumn("{target}", to_date(col("{source}"), "dd-MM-yyyy"))'
    if kind == "rename":
        return f'df_transformed = {frame}.withColumnRenamed("{source}", "{target}")'
    if kind == "drop":
        return f'df_transformed = {frame}.drop("{source}")'
    if kind == "filter":
        return f'df_transformed = {frame}.filter(col("{source}").isNotNull())'
    return f'df_transformed = {frame}.withColumn("{target}", coalesce(col("{source}"), lit("")))'


@st.composite
def _prompt_shaped_block(draw: st.DrawFn) -> list[str]:
    count = draw(st.integers(min_value=1, max_value=6))
    return [draw(_statement("df" if index == 0 else "df_transformed")) for index in range(count)]


#: Module-scope names of the rendered job (and builtins with reach) that a
#: transform block must not touch.
_JOB_SCOPE_REACH = st.sampled_from(
    (
        'x = spark.sql("SHOW TABLES")',
        'x = glueContext.create_dynamic_frame.from_catalog(database="d", table_name="t")',
        'x = boto3.client("s3")',
        'x = args["secret_arn"]',
        "x = sc._jvm",
        "x = job.commit()",
        'x = logger.info("x")',
        "x = json.dumps({})",
        "x = sys.argv",
        'x = __import__("os")',
        'x = open("/tmp/x")',
        "import os",
        'df.write.parquet("s3://elsewhere/")',
    )
)


@settings(max_examples=10)
@given(block=_prompt_shaped_block())
def test_a_prompt_shaped_chain_is_accepted(block: list[str]):
    assert validate_transform_block_safety("\n".join(block)) is None


@settings(max_examples=10)
@given(block=_prompt_shaped_block(), reach=_JOB_SCOPE_REACH, data=st.data())
def test_one_statement_reaching_job_scope_rejects_the_whole_block(block: list[str], reach: str, data: st.DataObject):
    position = data.draw(st.integers(min_value=0, max_value=len(block)))
    hostile = [*block[:position], reach, *block[position:]]

    with pytest.raises(UnsafeTransformBlockError):
        validate_transform_block_safety("\n".join(hostile))


#: SQL text that reaches outside the frame, each a call Spark would run.
_FORBIDDEN_SQL = (
    "java_method('java.lang.System', 'getProperty', 'java.version')",
    "reflect('java.util.UUID', 'randomUUID')",
    "try_reflect('java.lang.Runtime', 'getRuntime')",
    "id in (select id from parquet.`s3://other-bucket/`)",
)


def _python_literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@st.composite
def _assembled_sql_text(draw: st.DrawFn) -> str:
    """Python source for an expression whose VALUE is forbidden SQL, while no
    literal in it contains a forbidden word."""
    text = draw(st.sampled_from(_FORBIDDEN_SQL))
    # The shortest word present, so a cut inside it also breaks any longer word
    # containing it (`try_reflect` holds `reflect`).
    word = next(w for w in sorted(_SQL_WORDS, key=len) if w in text.lower())
    start = text.lower().index(word)
    cut = start + draw(st.integers(min_value=1, max_value=len(word) - 1))
    head, tail = text[:cut], text[cut:]
    how = draw(st.sampled_from(("replace", "concat", "percent", "slice")))
    if how == "replace":
        # Swap one character of the word for a marker, then put it back.
        masked = text[: cut - 1] + "*" + text[cut:]
        return f'{_python_literal(masked)}.replace("*", {_python_literal(text[cut - 1])})'
    if how == "concat":
        return f"{_python_literal(head)} + {_python_literal(tail)}"
    if how == "percent":
        return f"{_python_literal(head + '%s')} % {_python_literal(tail)}"
    padding = "x" * draw(st.integers(min_value=1, max_value=3))
    return f"{_python_literal(padding + head)}[{len(padding)}:] + {_python_literal(tail)}"


_SQL_TEXT_SINKS = st.sampled_from(
    (
        'df_transformed = df.withColumn("v", expr({}))',
        "df_transformed = df.filter({})",
        "df_transformed = df.where({})",
    )
)


@settings(max_examples=10)
@given(expression=_assembled_sql_text(), sink=_SQL_TEXT_SINKS)
def test_sql_text_assembled_from_permitted_operations_is_rejected(expression: str, sink: str):
    block = sink.format(expression)
    # The premise: no literal holds the word, so the literal scan alone passes it.
    assert all(word not in literal.lower() for word in _SQL_WORDS for literal in _string_literals(expression))

    with pytest.raises(UnsafeTransformBlockError):
        validate_transform_block_safety(block)


def _string_literals(source: str) -> list[str]:
    import ast

    return [
        node.value
        for node in ast.walk(ast.parse(source, mode="eval"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
