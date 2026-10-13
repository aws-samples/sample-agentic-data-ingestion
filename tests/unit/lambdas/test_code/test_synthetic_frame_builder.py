# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Glue runner's synthetic frame builder.

Spec `real-spark-verification` task 5.1, design §4.4's ``synthetic`` mode.
Requirement 10.3.

**No Spark, and no pandas either.** ``pyspark`` is deliberately not a dependency
of this repository (design §3 do-NOT 7, ``LGR-4dd9b42-1``) and pandas is not
installed in the test environment, so what is checked here is what the builder
*emits* plus what that code *does* when executed against a stubbed
``pyspark.sql.types`` and a recording ``spark``. Executing it matters: a
comma-placement defect in the emitted null-index list produced code that
``ast.parse`` accepted — adjacent list literals with no separator are
*subscripts*, not a syntax error — and only running it caught that.
``test_generated_frame_code_executes`` is that test.

What is pinned here:

* **the parity half of R10.3** — column names, column order, row count and null
  positions. ``test_null_mask_matches_the_pandas_builders_loop`` transcribes
  ``_build_test_data_code``'s null loop and asserts the two agree; task 5.2
  generalises it over schemas and seeds
* **bucket membership across BOTH builders** —
  ``test_type_buckets_agree_with_the_pandas_builder`` reads the value expression
  the pandas builder emits for the same schema and requires it to be the one this
  builder chose. A bucket edited on one side and not the other fails here rather
  than as an unexplained shadow disagreement later
* **the seed is fixed and load-bearing** — the same schema emits byte-identical
  code twice, and a different seed moves the null positions
* **the explicit default refuses** — a declared type outside the vocabulary
  raises :class:`FrameBuildError` naming the column and the type, before any code
  is returned and therefore before any session could be created
* **the 6.1 seam** — the builder binds ``FRAME_VARIABLE`` and emits no
  ``GENERATED:`` marker, because the marker protocol is task 6.1's and design §3
  do-NOT 8 forbids touching it from anywhere else
* ``declared_source_fields`` reused **verbatim by import**, asserted by object
  identity — the same technique ``test_glue_result_mapping.py`` uses for
  ``_bound_stderr``, and for the same reason: "an equivalent reimplementation"
  cannot satisfy it, and the helper exists (its own docstring says so) "so the
  two cannot drift"
"""

import ast
import os
import random
import sys
import types
from pathlib import Path

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import code_interpreter_runner
import glue_session_runner
from code_interpreter_runner import _MAX_TEST_FRAME_COLUMNS, CodeInterpreterTestRunner, declared_source_fields
from glue_session_runner import (
    FRAME_VARIABLE,
    FrameBuildError,
    build_synthetic_frame_code,
    spark_type_for,
    synthetic_frame_fields,
    synthetic_null_rows,
)

_SEED = glue_session_runner._SYNTHETIC_FRAME_SEED
_ROWS = glue_session_runner._SYNTHETIC_FRAME_ROWS
_RATE = glue_session_runner._SYNTHETIC_NULL_RATE


# ── Helpers ───────────────────────────────────────────────────────────


def _schema(**fields: str) -> dict:
    """A source schema in the ``properties`` shape ``schema_extract`` writes."""
    return {"type": "object", "properties": {name: {"type": kind} for name, kind in fields.items()}}


class _RecordingSpark:
    """Stands in for the ``spark`` a Glue interactive session provides.

    Records what ``createDataFrame`` was given so the emitted code can be
    inspected by its effect rather than by its text.
    """

    def __init__(self) -> None:
        self.rows: list[tuple] | None = None
        self.schema: list[tuple] | None = None

    def createDataFrame(self, rows, schema):
        self.rows = rows
        self.schema = schema
        return object()


def _stub_pyspark_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a ``pyspark.sql.types`` whose constructors record their names.

    ``StructField`` becomes ``(name, type_name, nullable)`` and ``StructType`` a
    plain list, so an assertion reads the emitted schema directly. Installed via
    ``monkeypatch.setitem`` so it is removed again at teardown — a real
    ``pyspark`` left in ``sys.modules`` would change what every later test in the
    session imports.
    """
    types_module = types.ModuleType("pyspark.sql.types")
    for name in ("LongType", "DoubleType", "StringType"):
        # The constructor returns its own name, so the recorded StructField
        # carries the Spark type this builder chose.
        setattr(types_module, name, (lambda captured: lambda: captured)(name))
    types_module.StructField = lambda name, spark_type, nullable: (name, spark_type, nullable)
    types_module.StructType = list

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.ModuleType("pyspark.sql"))
    monkeypatch.setitem(sys.modules, "pyspark.sql.types", types_module)


def _execute_frame_code(code: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, _RecordingSpark]:
    """Run emitted frame-build code and return its namespace plus the spark stub."""
    _stub_pyspark_types(monkeypatch)
    spark = _RecordingSpark()
    namespace: dict = {"spark": spark}
    exec(compile(code, "<frame_build>", "exec"), namespace)
    return namespace, spark


def _pandas_null_reference(columns, *, num_rows: int = _ROWS, seed: int = _SEED) -> dict[str, list[int]]:
    """The pandas builder's null loop, transcribed and seeded.

    ``code_interpreter_runner.py:529-531`` emits::

        for c in df.columns:
            mask = [random.random() < 0.05 for _ in range(num_rows)]
            df.loc[mask, c] = None

    — ``num_rows`` draws per column, columns in frame order, from the module-level
    ``random`` stream. That loop is what :func:`synthetic_null_rows` has to
    reproduce, so it is written out here rather than paraphrased. Seeding it is
    the one liberty taken: the emitted loop is unseeded today, which is exactly
    why R10.3 exists and what makes a shadow ``null_counts`` difference
    uninterpretable until both sides are seeded.
    """
    random.seed(seed)
    reference: dict[str, list[int]] = {}
    for column in columns:
        mask = [random.random() < _RATE for _ in range(num_rows)]
        reference[column] = [row for row, is_null in enumerate(mask) if is_null]
    return reference


def _normalise(expression: str) -> str:
    """Round-trip an expression through the AST so quoting cannot matter.

    ``ast.unparse`` normalises ``f"val_{i}"`` and ``f'val_{i}'`` to one spelling,
    so a comparison across two builders is about the expression rather than about
    which quote character each happened to use.
    """
    return ast.unparse(ast.parse(expression, mode="eval").body)


def _pandas_value_comprehension(schema: dict, column: str) -> str:
    """The list expression the pandas builder emits for *column*, normalised.

    Read out of the code ``_build_test_data_code`` actually emits, so bucket
    membership is compared against the other builder's behaviour rather than
    against a second copy of the bucket lists — which is the comparison that
    catches a bucket edited on one side only.
    """
    # `_build_test_data_code` reads no attribute of `self`; the same idiom
    # `test_glue_result_mapping.py` uses to call `_parse_results` as a function.
    emitted = CodeInterpreterTestRunner._build_test_data_code(None, schema)
    line = next(candidate for candidate in emitted.splitlines() if candidate.strip().startswith(f'"{column}":'))
    # `col_defs` are joined with ",\n", so every line but the last carries one.
    return _normalise(line.split(":", 1)[1].strip().rstrip(","))


def _expected_comprehension(spark_type: str) -> str:
    """The pandas comprehension this Spark type's value expression implies."""
    return _normalise(f"[{glue_session_runner._VALUE_EXPRESSIONS[spark_type]} for i in range(num_rows)]")


# ── Shared helper reuse: the two builders cannot drift ────────────────


def test_declared_source_fields_is_reused_by_import_not_reimplemented():
    """The frame builder reads the schema through the SHARED helper.

    Object identity, not equivalence. ``declared_source_fields``' own docstring
    says it exists "so the two cannot drift" — a second implementation here that
    happened to agree today would be free to stop agreeing tomorrow, and every
    column-level shadow disagreement after that would be an artifact of the
    reading rather than of the executor.
    """
    assert glue_session_runner.declared_source_fields is declared_source_fields
    assert glue_session_runner.declared_source_fields is code_interpreter_runner.declared_source_fields


def test_properties_wins_over_fields_because_the_shared_helper_says_so():
    """The ``properties``-key rule reaches the frame, empty value included.

    p33 requirement 3.14: ``properties`` wins whenever the KEY is present, even
    when its value is empty. Pinned here as well as on the helper, because it is
    the frame's column list that the rule decides.
    """
    both = {"properties": {}, "fields": {"ignored": {"type": "integer"}}}
    # An empty `properties` means "declares nothing", so the fallback frame.
    assert [name for name, _ in synthetic_frame_fields(both)] == ["id", "value"]

    fields_only = {"fields": {"used": {"type": "integer"}}}
    assert synthetic_frame_fields(fields_only) == [("used", "LongType")]


# ── Column list, order and width (R10.3) ─────────────────────────────


def test_one_column_per_declared_field_in_declaration_order():
    schema = _schema(zebra="string", apple="integer", mango="number")
    assert synthetic_frame_fields(schema) == [
        ("zebra", "StringType"),
        ("apple", "LongType"),
        ("mango", "DoubleType"),
    ]


def test_empty_schema_falls_back_to_the_pandas_builders_two_columns():
    """The ``{id, value}`` fallback, mirrored rather than improved.

    A run measured against it proves nothing (``_frame_refusal``'s docstring),
    and task 5.3 is what stops it being reached. Until then the two executors
    build the same useless frame instead of two different useless frames.
    """
    for empty in ({}, {"properties": {}}, {"fields": {}}, None):
        assert synthetic_frame_fields(empty) == [("id", "StringType"), ("value", "StringType")]


def test_width_is_sliced_at_the_frame_ceiling():
    """``[:_MAX_TEST_FRAME_COLUMNS]``, the same belt-and-braces slice.

    R10.4: the ceiling is derived from the DynamoDB item-size limit on the state
    row (``code_interpreter_runner.py:105-127``), not from the executor, so real
    Spark does not change it. Task 5.3 adds the refusal that makes this slice
    unreachable on the live path.
    """
    wide = _schema(**{f"c{index}": "integer" for index in range(_MAX_TEST_FRAME_COLUMNS + 88)})
    assert len(synthetic_frame_fields(wide)) == _MAX_TEST_FRAME_COLUMNS


# ── The three type buckets, and the explicit default ─────────────────


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        # LongType — verbatim from the pandas builder's first branch.
        ("integer", "LongType"),
        ("int", "LongType"),
        ("long", "LongType"),
        ("bigint", "LongType"),
        # DoubleType — verbatim from its second branch.
        ("float", "DoubleType"),
        ("double", "DoubleType"),
        ("decimal", "DoubleType"),
        ("number", "DoubleType"),
        # StringType — its `else`, written out over the observed vocabulary.
        ("string", "StringType"),
        ("str", "StringType"),
        ("boolean", "StringType"),
        ("bool", "StringType"),
        ("date", "StringType"),
        ("timestamp", "StringType"),
        ("array", "StringType"),
        ("object", "StringType"),
        ("null", "StringType"),
        ("varchar", "StringType"),
        ("struct", "StringType"),
        # Numeric-looking names the pandas builder ALSO strings. Parity is the
        # requirement (R10.3); fixing the typing belongs to the pandas builder's
        # buckets, which R3.3 puts out of scope.
        ("smallint", "StringType"),
        ("tinyint", "StringType"),
        ("numeric", "StringType"),
        ("real", "StringType"),
    ],
)
def test_type_buckets_agree_with_the_pandas_builder(declared, expected):
    """Each vocabulary member lands in the bucket, and in the SAME bucket.

    Two assertions, and the second is the one that matters: the value expression
    the pandas builder emits for the same declared type is read out of its
    generated code and required to be this bucket's expression. Editing a bucket
    on one side only fails here.
    """
    assert spark_type_for(declared) == expected
    assert _pandas_value_comprehension(_schema(col=declared), "col") == _expected_comprehension(expected)


def test_the_three_buckets_are_disjoint():
    """No declared type may map to two Spark types."""
    buckets = (
        glue_session_runner._INT_DECLARED_TYPES,
        glue_session_runner._FLOAT_DECLARED_TYPES,
        glue_session_runner._STRING_DECLARED_TYPES,
    )
    assert set.intersection(*(set(bucket) for bucket in buckets)) == set()
    assert sum(len(bucket) for bucket in buckets) == len(set().union(*buckets))


def test_a_missing_type_defaults_to_string_like_the_pandas_builder():
    """An undeclared type is ``string`` on both sides (``:512``)."""
    assert synthetic_frame_fields({"properties": {"bare": {}}}) == [("bare", "StringType")]
    assert _pandas_value_comprehension({"properties": {"bare": {}}}, "bare") == _expected_comprehension("StringType")


def test_a_non_dict_field_meta_defaults_to_string_like_the_pandas_builder():
    """``meta`` that is not a dict is an undeclared type (``:512``)."""
    assert synthetic_frame_fields({"properties": {"odd": "string"}}) == [("odd", "StringType")]


def test_an_unknown_declared_type_refuses_and_names_it():
    """The explicit default is a refusal, not a fall-through to ``StringType``.

    Spark enforces the ``StructType``, so a mis-typed column changes what every
    operation on it does — and the symptom would be a transform error or a wrong
    data-quality number, attributed to the transform.
    """
    with pytest.raises(FrameBuildError) as excinfo:
        spark_type_for("geography")
    assert "geography" in str(excinfo.value)


def test_an_unknown_type_names_the_column_it_was_declared_on():
    """A 400-field schema has to say WHICH field."""
    schema = _schema(good="integer", suspect="geography")
    with pytest.raises(FrameBuildError) as excinfo:
        synthetic_frame_fields(schema)
    message = str(excinfo.value)
    assert "suspect" in message
    assert "geography" in message


def test_a_case_variant_is_an_unknown_type_rather_than_a_silent_retype():
    """Case is not normalised, because the pandas builder does not normalise it.

    ``dtype in ("integer", …)`` is tested against the raw declared value there,
    so ``"Integer"`` lands in its string bucket. Lower-casing here would type the
    column as a long on one executor and a string on the other — the exact
    divergence R10.3 forbids. Every producer in this repo lower-cases before
    writing a schema, so a case variant is an unknown input, and an unknown input
    refuses.
    """
    assert _pandas_value_comprehension(_schema(col="Integer"), "col") == _expected_comprehension("StringType")
    with pytest.raises(FrameBuildError):
        spark_type_for("Integer")


def test_a_union_type_refuses_rather_than_choosing_one_arm():
    """``{"type": ["string", "null"]}`` has no single Spark type."""
    with pytest.raises(FrameBuildError) as excinfo:
        synthetic_frame_fields({"properties": {"u": {"type": ["string", "null"]}}})
    assert "union" in str(excinfo.value)


def test_the_refusal_happens_before_any_code_is_returned():
    """Nothing is spent discovering an unknown type.

    :class:`FrameBuildError` is raised host-side out of
    :func:`build_synthetic_frame_code`, so it cannot be reached with a session
    already created — the code that would be submitted does not exist yet.
    """
    with pytest.raises(FrameBuildError):
        build_synthetic_frame_code(_schema(ok="string", bad="geography"))


def test_frame_build_error_is_not_a_session_error():
    """No session was involved, so a ``GlueSessionError`` handler must not claim it."""
    assert not issubclass(FrameBuildError, glue_session_runner.GlueSessionError)


# ── The seed (R10.3) ─────────────────────────────────────────────────


def test_the_null_mask_is_deterministic_across_calls():
    columns = ["a", "b", "c"]
    assert synthetic_null_rows(columns) == synthetic_null_rows(columns)


def test_the_emitted_code_is_byte_identical_across_calls():
    """Determinism at the level that actually reaches Spark."""
    schema = _schema(id="integer", amount="number", name="string")
    assert build_synthetic_frame_code(schema) == build_synthetic_frame_code(schema)


def test_the_seed_is_what_makes_it_deterministic():
    """A different seed moves the null positions.

    The complement of the test above: without this, "deterministic" could be
    satisfied by a mask that ignores the seed entirely — which would be
    reproducible and would still fail R10.3 the moment the pandas side was
    seeded to anything.
    """
    columns = [f"c{index}" for index in range(12)]
    assert synthetic_null_rows(columns, seed=_SEED) != synthetic_null_rows(columns, seed=_SEED + 1)


def test_the_null_rate_is_about_five_percent():
    """~5% of cells, matching ``random.random() < 0.05``.

    A band rather than an equality: the count per column is itself drawn, which
    is what makes a per-column ``null_counts`` comparison worth recording. Wide
    enough that it pins the RATE and not this seed's particular draw.
    """
    columns = [f"c{index}" for index in range(120)]
    mask = synthetic_null_rows(columns)
    nulls = sum(len(rows) for rows in mask.values())
    assert 0.02 < nulls / (len(columns) * _ROWS) < 0.09


def test_null_mask_matches_the_pandas_builders_loop():
    """The parity claim, as a worked example. Task 5.2 generalises it.

    A seeded transcription of ``_build_test_data_code``'s null loop produces the
    same positions as :func:`synthetic_null_rows`. That equality is what makes
    R10.3 reachable: the pandas builder is unseeded today, and seeding it is all
    that stands between here and identical frames.
    """
    columns = ["id", "amount", "name", "flag"]
    assert synthetic_null_rows(columns) == _pandas_null_reference(columns)


def test_null_mask_draw_order_is_per_column_in_frame_order():
    """One stream, ``num_rows`` draws per column, columns in order.

    Pinned because two other orderings are equally deterministic and both wrong:
    drawing row-major, or giving each column its own seeded stream. Either would
    pass every determinism test above and none of the parity ones.
    """
    columns = ["first", "second"]
    mask = synthetic_null_rows(columns)

    random.seed(_SEED)
    first_stream = [random.random() < _RATE for _ in range(_ROWS)]
    second_stream = [random.random() < _RATE for _ in range(_ROWS)]

    assert mask["first"] == [row for row, is_null in enumerate(first_stream) if is_null]
    assert mask["second"] == [row for row, is_null in enumerate(second_stream) if is_null]


# ── What the emitted code is, and what it does ───────────────────────


def test_emitted_code_parses():
    assert ast.parse(build_synthetic_frame_code(_schema(id="integer", name="string")))


def test_emitted_code_binds_the_frame_variable_and_nothing_else_matters():
    code = build_synthetic_frame_code(_schema(id="integer"))
    assert f"{FRAME_VARIABLE} = spark.createDataFrame(" in code


def test_emitted_code_carries_no_marker_because_that_is_task_6_1s():
    """The 6.1 seam. Design §3 do-NOT 8: the marker protocol is not touched here.

    Task 6.1 appends the ``GENERATED:`` line that reports on
    :data:`FRAME_VARIABLE`. Emitting one here would put the same protocol in two
    places, and reusing ``_parse_results`` verbatim is what makes this an
    executor swap rather than a contract change.
    """
    code = build_synthetic_frame_code(_schema(id="integer"))
    assert "GENERATED:" not in code
    assert "VALIDATION:" not in code


def test_emitted_code_imports_only_the_three_bucket_types():
    """No ``pyspark`` import in THIS module; the emitted code imports for itself."""
    code = build_synthetic_frame_code(_schema(a="integer", b="number", c="string"))
    imports = [line for line in code.splitlines() if line.startswith("from pyspark")]
    assert imports == ["from pyspark.sql.types import DoubleType, LongType, StringType, StructField, StructType"]


def test_this_module_never_imports_pyspark_itself():
    """Design §3 do-NOT 7 / ``LGR-4dd9b42-1``: ``pyspark`` is not a dependency.

    The builder emits a code string; Spark imports what it needs on the session
    side. An ``import pyspark`` here would work in a Glue container and fail in
    the Lambda package and in this test environment, which is the failure mode
    the do-NOT exists to prevent.
    """
    source = ast.parse(Path(glue_session_runner.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(source) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(source) if isinstance(node, ast.ImportFrom) and node.module}
    assert "pyspark" not in imported


def test_generated_frame_code_executes(monkeypatch):
    """Run the emitted code. Parsing it is not enough.

    A comma emitted after the null list's name comment produced code that
    ``ast.parse`` accepted and that failed at runtime — adjacent list literals
    with no separator are subscripts. This is the test that distinguishes the
    two.
    """
    schema = _schema(id="integer", amount="number", name="string")
    _, spark = _execute_frame_code(build_synthetic_frame_code(schema), monkeypatch)

    assert spark.schema == [
        ("id", "LongType", True),
        ("amount", "DoubleType", True),
        ("name", "StringType", True),
    ]
    assert len(spark.rows) == _ROWS


def test_generated_frame_has_the_null_positions_the_mask_declares(monkeypatch):
    """The seeded mask is what actually lands in the frame (R10.3).

    Without this, :func:`synthetic_null_rows` could be perfectly deterministic
    and the emitted code could null different cells.
    """
    schema = _schema(id="integer", amount="number", name="string")
    columns = [name for name, _ in synthetic_frame_fields(schema)]
    _, spark = _execute_frame_code(build_synthetic_frame_code(schema), monkeypatch)

    expected = synthetic_null_rows(columns)
    for index, column in enumerate(columns):
        observed = [row for row, values in enumerate(spark.rows) if values[index] is None]
        assert observed == expected[column], column


def test_generated_frame_values_are_the_pandas_expressions(monkeypatch):
    """Non-null values equal, not merely similar — ``i``, ``round(i*1.1, 2)``, ``val_{i}``."""
    schema = _schema(id="integer", amount="number", name="string")
    columns = [name for name, _ in synthetic_frame_fields(schema)]
    mask = synthetic_null_rows(columns)
    _, spark = _execute_frame_code(build_synthetic_frame_code(schema), monkeypatch)

    row = next(index for index in range(_ROWS) if not any(index in mask[column] for column in columns))
    assert spark.rows[row] == (row, round(row * 1.1, 2), f"val_{row}")


def test_a_single_column_frame_emits_a_tuple_not_a_bare_value(monkeypatch):
    """The one-column case, which a missing trailing comma silently breaks."""
    _, spark = _execute_frame_code(build_synthetic_frame_code(_schema(only="boolean")), monkeypatch)
    assert len(spark.schema) == 1
    assert all(isinstance(values, tuple) and len(values) == 1 for values in spark.rows)


def test_a_column_name_with_quotes_or_braces_survives(monkeypatch):
    """Names are emitted through ``json.dumps``, not interpolated bare.

    The pandas builder interpolates bare (``:513-518``); a name containing a
    quote or a backslash emits code there that does not parse, or parses as a
    different name. Escaping here changes no name that was already safe, so
    parity is unaffected.
    """
    schema = {"properties": {'a"b': {"type": "string"}, "c\\d": {"type": "integer"}, "e{f}": {"type": "number"}}}
    _, spark = _execute_frame_code(build_synthetic_frame_code(schema), monkeypatch)
    assert [name for name, _, _ in spark.schema] == ['a"b', "c\\d", "e{f}"]


def test_the_widest_allowed_frame_still_executes(monkeypatch):
    """512 columns — the ceiling R10.4 retains — build and run."""
    wide = _schema(**{f"c{index}": "integer" for index in range(_MAX_TEST_FRAME_COLUMNS)})
    _, spark = _execute_frame_code(build_synthetic_frame_code(wide), monkeypatch)
    assert len(spark.schema) == _MAX_TEST_FRAME_COLUMNS
    assert len(spark.rows) == _ROWS


def test_row_count_is_one_hundred_on_both_executors():
    """``num_rows = 100``, the pandas builder's fixed value (R10.3)."""
    assert _ROWS == 100
    code = build_synthetic_frame_code(_schema(id="integer"))
    assert f"num_rows = {_ROWS}" in code
    assert "num_rows = 100" in CodeInterpreterTestRunner._build_test_data_code(None, _schema(id="integer"))
