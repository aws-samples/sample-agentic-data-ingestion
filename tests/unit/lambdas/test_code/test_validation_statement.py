# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Glue runner's validation statement and stdout markers.

Spec `real-spark-verification` task 6.1, design §4.9 and C7's table.
Requirements 1.3, 10.1, 10.2, 10.6.

**No Spark, and no pandas.** ``pyspark`` is deliberately not a dependency of this
repository (design §3 do-NOT 7, ``LGR-4dd9b42-1``), so what is checked here is
what :func:`build_validation_code` *emits* plus what that code *does* when
executed against a stubbed ``pyspark.sql.functions`` and a stub frame — the same
arrangement ``test_synthetic_frame_builder.py`` and ``test_frame_parity_prop.py``
use for the frame builders.

Executing it is not optional. The first version of the emitted payload carried
``"missing_columns": missing_columns`` where the local is ``missing`` — the
pandas path emits ``"missing_columns": missing``, so the key does not name its own
variable. That parses cleanly and fails at run time with a ``NameError`` naming
neither the module nor the payload. ``test_emitted_statement_executes`` and the
per-frame arithmetic tests are what caught it; ``_VALIDATION_PAYLOAD_EXPRESSIONS``
is what stops it recurring.

════════════════════════════════════════════════════════════════════════
WHAT THE STUB ENCODES, AND WHAT THAT DOES AND DOES NOT PROVE
════════════════════════════════════════════════════════════════════════

:class:`_StubFunctions` implements three pieces of *documented* Spark semantics,
each of which the emitted arithmetic depends on:

1. ``Column.isNull()`` is True for a null and **False for a float NaN**. This is
   design C7's one value divergence and the reason completeness moves.
2. ``count(DISTINCT expr)`` skips rows where *expr* is null, and a struct built by
   ``struct(...)`` is itself never null — which is why the emitted code takes the
   distinct count over a struct rather than over the bare column list.
3. NaN is normalised in a grouping/distinct key, so a row carrying NaN is a
   duplicate of an identical row.

So these tests check that **the emitted arithmetic computes the R10.2 definitions
given those semantics**. They do NOT measure real Spark: no Spark runs in this
repository, and design C7 is itself explicit that the divergence it predicts is
"derived from documented semantics" rather than measured. Where the two could
part company, the tests say so at the assertion.

════════════════════════════════════════════════════════════════════════
THE ORACLE IS TASK 1.1'S, IMPORTED — NOT RESTATED
════════════════════════════════════════════════════════════════════════

Every expected number comes from ``test_dq_definitions_characterization``'s
``*_from_definition`` oracles and its five hand-built frames, imported rather than
re-derived. Task 1.1's report asks for exactly that: restating the arithmetic here
would give the emitted code a second oracle written by the same hand at the same
time, which is the circularity `LGR-28b4d75-5` names — "a test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle."

Requirement 10.5 is the direction: expectations come from the definitions, never
from what the pandas path currently prints.

**THE RE-BASELINE, STATED RATHER THAN DISCOVERED.** On the two frames carrying a
float ``NaN`` the emitted statement reports different numbers from the pandas path,
and ``TestTheRecordedMovementAgainstPandas`` asserts that movement against task
1.1's ``observed_pandas`` records so it is legible as a re-baseline rather than as
a regression:

* ``NULLS_INCLUDING_NAN``            — ``amount`` nulls 2 → **1**, completeness
                                       75.0 → **83.3**, overall 91.7 → **94.4**
* ``DUPLICATE_ROWS_CONTAINING_NAN``  — ``v`` nulls 2 → **0**, completeness
                                       50.0 → **100.0**, overall 66.7 → **83.3**

``uniqueness``, ``duplicates``, ``schema_compliance`` and ``missing_columns`` move
on no frame — the first two because the duplicate expressions agree on exact
duplicates, the last two because schema compliance is set arithmetic over column
names on both sides (design §2).

``dtypes`` values are asserted **only** to be present and to be a string-to-string
mapping. Task 1.1 leaves them unpinned and records why: C7 predicts every entry
moves, and the pandas side is not one vocabulary either — a string column reports
``object`` on pandas 2.2.3 and ``str`` on 3.0.5, with nothing pinning which pandas
the managed interpreter runs.

Tasks 6.2, 6.3 and 6.4 generalise three of these into properties — the marker
round-trip through the production parser, the executor-independent key set, and
the metric definitions over generated frames. What is here is the example-level
baseline they build on.

Validates: Requirements 1.3, 10.1, 10.2, 10.6
"""

import ast
import json
import math
import os
import sys
import types
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import CodeInterpreterTestRunner
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    FRAME_VARIABLE,
    STAGE_VALIDATION,
    VALIDATION_PAYLOAD_KEYS,
    FrameBuildError,
    FrameSource,
    build_frame_code,
    build_generated_marker_code,
    build_sample_frame_code,
    build_synthetic_frame_code,
    build_transform_namespace_code,
    build_validation_code,
    declared_target_columns,
    frame_build_statement,
    validation_statement,
)
from tests.unit.lambdas.test_code.test_dq_definitions_characterization import (
    ALL_FRAMES,
    DECLARED_TARGET_COLUMN_ABSENT,
    DUPLICATE_ROWS_CONTAINING_NAN,
    NAN,
    NO_DECLARED_TARGETS,
    NULL,
    NULLS_INCLUDING_NAN,
    Frame,
    completeness_from_definition,
    duplicate_rows_from_definition,
    missing_target_columns_from_definition,
    null_counts_from_definition,
    overall_from_components,
    schema_compliance_from_definition,
    uniqueness_from_definition,
)

# ── The Spark stub ────────────────────────────────────────────────────


def _normalise(value):
    """A hashable identity for a cell inside a distinct/grouping key.

    Spark's ``NormalizeFloatingNumbers`` rule canonicalises NaN (and -0.0) in
    grouping and distinct keys, so a row carrying NaN is a duplicate of an
    identical row. Encoded here for the same reason task 1.1's ``_cell_key``
    encodes it: ``float('nan') != float('nan')`` in plain Python, so without this
    a stub would report two identical rows as distinct and disagree with both the
    definition and the recorded pandas observation.
    """
    if isinstance(value, float) and math.isnan(value):
        return ("__nan__",)
    return (type(value).__name__, value)


def _unquote(ref: str) -> str:
    """Undo the emitted backtick quoting: ```a``b``` → ``a`b``."""
    assert ref.startswith("`") and ref.endswith("`"), f"column reference {ref!r} is not backtick-quoted"
    return ref[1:-1].replace("``", "`")


@dataclass(frozen=True)
class _Lit:
    value: object


@dataclass(frozen=True)
class _Col:
    ref: str

    def isNull(self) -> "_IsNull":  # the PySpark spelling, matched deliberately
        return _IsNull(self)


@dataclass(frozen=True)
class _IsNull:
    """``Column.isNull()`` — True for a null, **False for a float NaN** (C7)."""

    column: _Col


@dataclass(frozen=True)
class _When:
    condition: object
    value: object


@dataclass(frozen=True)
class _Struct:
    columns: tuple


@dataclass(frozen=True)
class _Count:
    expression: object


@dataclass(frozen=True)
class _CountDistinct:
    expression: object


def _evaluate(expression, row: dict):
    """Evaluate one non-aggregate expression against *row*."""
    if isinstance(expression, _Lit):
        return expression.value
    if isinstance(expression, _Col):
        return row[_unquote(expression.ref)]
    if isinstance(expression, _IsNull):
        # The load-bearing line: a NaN is a present IEEE value, so it is not null.
        return _evaluate(expression.column, row) is None
    if isinstance(expression, _When):
        return expression.value.value if _evaluate(expression.condition, row) else None
    if isinstance(expression, _Struct):
        # Never null, whatever its fields hold — which is why the emitted code
        # takes its distinct count over a struct rather than over bare columns.
        return tuple(_normalise(_evaluate(column, row)) for column in expression.columns)
    raise AssertionError(f"the stub does not implement {type(expression).__name__}")


def _aggregate(expression, rows: list[dict]) -> int:
    """Evaluate one aggregate expression over every row."""
    if isinstance(expression, _Count):
        return sum(1 for row in rows if _evaluate(expression.expression, row) is not None)
    if isinstance(expression, _CountDistinct):
        # `count(DISTINCT expr)` skips rows where expr is null.
        values = [_evaluate(expression.expression, row) for row in rows]
        return len({_normalise(value) for value in values if value is not None})
    raise AssertionError(f"{type(expression).__name__} is not an aggregate the stub knows")


class _StubFunctions:
    """``pyspark.sql.functions``, as much of it as the statement uses."""

    @staticmethod
    def lit(value):
        return _Lit(value)

    @staticmethod
    def col(ref):
        return _Col(ref)

    @staticmethod
    def when(condition, value):
        return _When(condition, value)

    @staticmethod
    def struct(*columns):
        return _Struct(tuple(columns))

    @staticmethod
    def count(expression):
        return _Count(expression)

    @staticmethod
    def count_distinct(expression):
        return _CountDistinct(expression)


@dataclass
class _Projection:
    """What ``select`` returns: one row of aggregate values."""

    values: list
    frame: "_StubFrame"

    def collect(self) -> list[list]:
        self.frame.collect_calls += 1
        return [self.values]


@dataclass
class _StubFrame:
    """A frame the emitted statement can be run against.

    Records ``select`` and ``collect`` calls, which is how R10.6 — the payload
    costs ONE Spark action — becomes checkable rather than a claim in a comment.
    """

    column_names: list[str]
    rows: list[dict]
    spark_dtypes: list[tuple[str, str]]
    select_calls: list[tuple] = field(default_factory=list)
    collect_calls: int = 0

    @property
    def columns(self) -> list[str]:
        return list(self.column_names)

    @property
    def dtypes(self) -> list[tuple[str, str]]:
        return list(self.spark_dtypes)

    def select(self, *expressions) -> _Projection:
        self.select_calls.append(expressions)
        return _Projection([_aggregate(expression, self.rows) for expression in expressions], self)

    def count(self) -> int:
        """``df.count()`` — what the ``GENERATED:`` marker reports (C7's 4th row)."""
        self.collect_calls += 1
        return len(self.rows)


#: A plausible Spark dtype for each Python cell type. Only the KEY SET of
#: ``dtypes`` is asserted anywhere; these values exist so the stub can answer at
#: all. See the module docstring for why the values are unpinned.
_SPARK_DTYPE_BY_PYTHON_TYPE = {int: "bigint", float: "double", str: "string", bool: "boolean"}


def _stub_frame(frame: Frame) -> _StubFrame:
    """Turn one of task 1.1's hand-built frames into a stub Spark frame."""
    rows = [{name: row[index] for index, name in enumerate(frame.columns)} for row in frame.rows]
    dtypes = []
    for index, name in enumerate(frame.columns):
        present = next((row[index] for row in frame.rows if row[index] is not None), None)
        dtypes.append((name, _SPARK_DTYPE_BY_PYTHON_TYPE.get(type(present), "string")))
    return _StubFrame(column_names=list(frame.columns), rows=rows, spark_dtypes=dtypes)


def _install_pyspark_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a ``pyspark.sql.functions`` the emitted statement can import.

    Via ``monkeypatch.setitem`` so it is removed at teardown — a stub left in
    ``sys.modules`` would change what every later test in the session imports.
    """
    functions = types.ModuleType("pyspark.sql.functions")
    for name in ("lit", "col", "when", "struct", "count", "count_distinct"):
        setattr(functions, name, getattr(_StubFunctions, name))

    sql = types.ModuleType("pyspark.sql")
    sql.functions = functions
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", sql)
    monkeypatch.setitem(sys.modules, "pyspark.sql.functions", functions)


def _target_schema(frame: Frame) -> dict:
    """The ``properties`` shape both validation builders read."""
    return {"properties": {name: {"type": "string"} for name in frame.declared_targets}}


def _execute(code: str, namespace: dict, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run emitted code with the stub installed; return its stdout."""
    _install_pyspark_functions(monkeypatch)
    buffer = StringIO()
    with redirect_stdout(buffer):
        exec(compile(code, "<validation>", "exec"), namespace)
    return buffer.getvalue()


def _payload(stdout: str, marker: str = "VALIDATION") -> dict:
    """The raw JSON object printed after *marker*."""
    start = stdout.index("{", stdout.index(f"{marker}:"))
    depth, index = 0, start
    while index < len(stdout):
        if stdout[index] == "{":
            depth += 1
        elif stdout[index] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stdout[start : index + 1])
        index += 1
    raise AssertionError(f"no balanced {marker} payload in {stdout!r}")


#: "no target schema was passed", distinct from an explicit ``None`` — which is
#: itself one of the shapes under test.
_DERIVE_FROM_FRAME = object()


def _validate(frame: Frame, monkeypatch: pytest.MonkeyPatch, *, target_schema=_DERIVE_FROM_FRAME) -> dict:
    """Run the emitted validation statement against *frame*; return the payload."""
    schema = _target_schema(frame) if target_schema is _DERIVE_FROM_FRAME else target_schema
    stub = _stub_frame(frame)
    stdout = _execute(build_validation_code(schema), {"df_transformed": stub}, monkeypatch)
    payload = _payload(stdout)
    payload["_stub"] = stub
    return payload


@pytest.fixture
def pandas_runner():
    """A pandas-path runner whose boto client is a mock — no builder touches it."""
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        return CodeInterpreterTestRunner(region="us-west-2")


# ══════════════════════════════════════════════════════════════════════
# The statement is emittable, parseable and runnable
# ══════════════════════════════════════════════════════════════════════


class TestTheEmittedStatement:
    def test_emitted_statement_parses(self):
        assert ast.parse(build_validation_code(_target_schema(DECLARED_TARGET_COLUMN_ABSENT)))

    def test_emitted_statement_executes(self, monkeypatch):
        """Parsing is not enough — see the module docstring for the defect this caught."""
        payload = _validate(NULLS_INCLUDING_NAN, monkeypatch)
        assert payload["record_count"] == len(NULLS_INCLUDING_NAN.rows)

    def test_emitted_statement_contains_no_blank_line(self):
        """A Glue ``RunStatement`` executes as a cell.

        A blank line inside an indented block is the shape that terminates a block
        early in a REPL-style executor, and the pandas builder emits none either.
        """
        code = build_validation_code(_target_schema(NULLS_INCLUDING_NAN))
        assert [line for line in code.splitlines() if not line.strip()] == []

    def test_the_emitted_statement_imports_only_what_it_uses(self):
        code = build_validation_code(None)
        imports = sorted(line for line in code.splitlines() if line.startswith(("import ", "from ")))
        assert imports == ["from pyspark.sql import functions as _F", "import json"]

    def test_no_pyspark_import_in_the_producing_module(self):
        """``pyspark`` is not a dependency here (design §3 do-NOT 7)."""
        source = ast.parse(Path(glue_session_runner.__file__).read_text())
        from_imports = {
            node.module.split(".")[0] for node in ast.walk(source) if isinstance(node, ast.ImportFrom) and node.module
        }
        plain_imports = {
            alias.name.split(".")[0]
            for node in ast.walk(source)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "pyspark" not in from_imports | plain_imports

    def test_the_statement_is_deterministic(self):
        schema = _target_schema(DECLARED_TARGET_COLUMN_ABSENT)
        assert build_validation_code(schema) == build_validation_code(schema)


# ══════════════════════════════════════════════════════════════════════
# R10.6 — one select, therefore one action
# ══════════════════════════════════════════════════════════════════════


class TestThePayloadCostsOneAction:
    """R10.6, checked by counting rather than asserted in a comment."""

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_exactly_one_select_and_one_collect(self, frame, monkeypatch):
        payload = _validate(frame, monkeypatch)
        stub = payload["_stub"]
        assert len(stub.select_calls) == 1, "R10.6: the whole payload is computed in ONE select"
        assert stub.collect_calls == 1, "one select collected once is one Spark action"

    def test_the_one_select_carries_every_aggregate_the_payload_needs(self, monkeypatch):
        """Row count, distinct-row count, and one null count per column."""
        frame = NULLS_INCLUDING_NAN
        payload = _validate(frame, monkeypatch)
        (expressions,) = payload["_stub"].select_calls
        assert len(expressions) == 2 + len(frame.columns)
        assert isinstance(expressions[0], _Count)
        assert isinstance(expressions[1], _CountDistinct)
        assert all(isinstance(expression, _Count) for expression in expressions[2:])

    def test_the_distinct_count_is_taken_over_a_struct(self, monkeypatch):
        """Not over the bare column list — see the next test for what that costs."""
        payload = _validate(NULLS_INCLUDING_NAN, monkeypatch)
        (expressions,) = payload["_stub"].select_calls
        assert isinstance(expressions[1].expression, _Struct)

    def test_a_frame_whose_every_row_carries_a_null_is_still_counted(self, monkeypatch):
        """Why the struct is load-bearing rather than stylistic.

        SQL ``count(DISTINCT a, b)`` drops any row with a null argument, so over
        this frame it would see ZERO rows, report ``duplicates`` as
        ``record_count - 0 == 3`` and score ``uniqueness`` at 0.0 for three
        distinct rows. A struct built by ``struct(...)`` is never null, so every
        row is counted.
        """
        frame = Frame(
            name="every_row_has_a_null",
            columns=("a", "b"),
            rows=((1, NULL), (2, NULL), (NULL, 3)),
            declared_targets=("a", "b"),
        )
        payload = _validate(frame, monkeypatch)
        assert payload["duplicates"] == 0
        assert payload["uniqueness"] == 100.0
        assert duplicate_rows_from_definition(frame) == 0

    def test_dtypes_and_missing_columns_cost_no_action(self, monkeypatch):
        """Both are schema-derived, and the action count above already proves it.

        Stated as its own assertion because it is the reason four metrics fit in
        one select: only three of the nine keys need the data at all.
        """
        payload = _validate(DECLARED_TARGET_COLUMN_ABSENT, monkeypatch)
        assert payload["dtypes"] and payload["missing_columns"] == ["email"]
        assert payload["_stub"].collect_calls == 1


# ══════════════════════════════════════════════════════════════════════
# R10.1 — the nine keys, identical to the pandas path's
# ══════════════════════════════════════════════════════════════════════


class TestThePayloadKeySet:
    def test_the_declared_key_set_is_the_nine_r10_1_pins(self):
        assert VALIDATION_PAYLOAD_KEYS == (
            "record_count",
            "null_counts",
            "dtypes",
            "duplicates",
            "schema_compliance",
            "completeness",
            "uniqueness",
            "overall_quality",
            "missing_columns",
        )

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_the_emitted_payload_carries_exactly_those_keys(self, frame, monkeypatch):
        payload = _validate(frame, monkeypatch)
        payload.pop("_stub")
        assert tuple(payload) == VALIDATION_PAYLOAD_KEYS

    def test_the_key_set_is_the_one_the_pandas_path_emits(self, pandas_runner):
        """R10.1 is an equality between executors, so read the other side.

        Task 6.3 generalises this; here it is asserted against the pandas
        builder's actual emitted text rather than against a transcription.
        """
        pandas_code = pandas_runner._build_validation_code(_target_schema(NULLS_INCLUDING_NAN))
        for key in VALIDATION_PAYLOAD_KEYS:
            assert f'"{key}":' in pandas_code
        emitted_keys = {
            line.split('"')[1]
            for line in pandas_code.splitlines()
            if line.startswith('    "') and line.rstrip().endswith(",")
        }
        assert emitted_keys == set(VALIDATION_PAYLOAD_KEYS)

    def test_every_key_has_a_declared_source_expression(self):
        """The map is total over the key set, and one entry is not the identity."""
        expressions = glue_session_runner._VALIDATION_PAYLOAD_EXPRESSIONS
        assert set(expressions) == set(VALIDATION_PAYLOAD_KEYS)
        assert expressions["missing_columns"] == "missing", (
            'the pandas path emits `"missing_columns": missing`; a key that names its own '
            "variable is a coincidence holding for eight keys, not a rule"
        )


# ══════════════════════════════════════════════════════════════════════
# R1.3 — the markers round-trip through the PRODUCTION parser
# ══════════════════════════════════════════════════════════════════════


class TestTheMarkersRoundTripThroughTheProductionParser:
    """Design §3 do-NOT 8: the parser is reused verbatim and is not touched.

    Task 6.2 generalises this over generated payload values, brace-containing
    column names included. What is pinned here is that the reuse is real — the
    same function object — and that a run's markers reach it intact.
    """

    def test_the_parser_is_the_production_function_object(self):
        assert glue_session_runner._parse_results is CodeInterpreterTestRunner._parse_results

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_a_full_run_parses_into_the_contract(self, frame, monkeypatch):
        stub = _stub_frame(frame)
        stdout = _execute(
            build_generated_marker_code() + build_validation_code(_target_schema(frame)),
            {FRAME_VARIABLE: stub, "df_transformed": stub},
            monkeypatch,
        )
        parsed = glue_session_runner._parse_results(None, stdout)

        assert parsed["execution_successful"] is True
        assert parsed["error_details"] is None
        assert parsed["input_records"] == len(frame.rows)
        assert parsed["output_records"] == len(frame.rows)
        assert parsed["data_quality_results"]["null_counts"] == null_counts_from_definition(frame)

    def test_the_parser_maps_the_payload_onto_its_seven_dq_keys(self, monkeypatch):
        frame = DECLARED_TARGET_COLUMN_ABSENT
        payload = _validate(frame, monkeypatch)
        stdout = _execute(
            build_validation_code(_target_schema(frame)), {"df_transformed": _stub_frame(frame)}, monkeypatch
        )
        dq = glue_session_runner._parse_results(None, stdout)["data_quality_results"]

        assert dq["completeness_score"] == payload["completeness"]
        assert dq["uniqueness_score"] == payload["uniqueness"]
        assert dq["schema_compliance"] == payload["schema_compliance"]
        assert dq["overall_quality_score"] == payload["overall_quality"]
        assert dq["null_counts"] == payload["null_counts"]
        assert dq["duplicates"] == payload["duplicates"]
        assert dq["missing_columns"] == payload["missing_columns"]

    def test_a_balanced_brace_in_a_column_name_survives_the_brace_depth_scan(self, monkeypatch):
        """The parser scans for a BALANCED object, so a brace in a name is the hazard.

        A name whose braces balance — ``a{b}`` — is safe: the scanner's depth goes
        1 → 2 → 1 across it and the object still closes in the right place. Task
        6.2 generates these; one example here keeps the unit layer honest, and the
        next test records the case that is NOT safe.
        """
        frame = Frame(
            name="balanced_brace_in_a_column_name",
            columns=("a{b}", "e.f", "g`h"),
            rows=((1, 2, 3), (1, 2, 3)),
            declared_targets=("a{b}",),
        )
        stub = _stub_frame(frame)
        stdout = _execute(
            build_generated_marker_code() + build_validation_code(_target_schema(frame)),
            {FRAME_VARIABLE: stub, "df_transformed": stub},
            monkeypatch,
        )
        parsed = glue_session_runner._parse_results(None, stdout)

        assert parsed["execution_successful"] is True
        assert parsed["data_quality_results"]["null_counts"] == {"a{b}": 0, "e.f": 0, "g`h": 0}
        assert parsed["data_quality_results"]["duplicates"] == 1

    def test_an_unbalanced_closing_brace_in_a_column_name_defeats_the_parser(self):
        """════ A PRE-EXISTING PARSER LIMITATION, RECORDED RATHER THAN FIXED ════

        ``_extract_json`` counts braces without tracking string literals
        (``code_interpreter_runner.py:604-614``), so a column name containing a
        ``}`` that is not preceded by a matching ``{`` closes the object early and
        the slice fed to ``json.loads`` is truncated. It raises
        ``JSONDecodeError``, which propagates out of ``_parse_results``.

        **This is not introduced by the Glue runner and is not repairable here.**
        Design §3 do-NOT 8 forbids changing ``_parse_results`` or the marker
        protocol — reusing the parser verbatim is what makes this an executor swap
        — so the limitation is pinned as behaviour instead. The exposure is
        *executor-independent*, which is the reason it matters to R10.1: both
        builders print ``"columns": list(df.columns)`` and both key ``null_counts``
        by column name, so the pandas path fails identically on the same frame.
        Demonstrated below on a hand-built marker line, which is the pandas path's
        emitted shape and needs no pandas to construct.

        For task 6.2: generate braces, but expect this shape to raise rather than
        round-trip, and do not attribute it to the executor.
        """
        # A `}` reached before any matching `{` at that depth closes the object
        # early, so the slice handed to `json.loads` ends mid-string.
        pandas_shaped = 'VALIDATION:{"record_count": 2, "null_counts": {"c}d": 0}}'
        with pytest.raises(json.JSONDecodeError):
            glue_session_runner._parse_results(None, pandas_shaped)

        # A name whose braces balance is unaffected, which is why the previous
        # test passes and this one does not: the defect is asymmetric.
        balanced = 'VALIDATION:{"record_count": 2, "null_counts": {"a{b}": 0}}'
        assert glue_session_runner._parse_results(None, balanced)["output_records"] == 2

        # And the pandas builder emits exactly that shape, so the exposure is shared.
        with patch("code_interpreter_runner.boto3") as mock_boto:
            mock_boto.client.return_value = MagicMock()
            pandas_code = CodeInterpreterTestRunner(region="us-west-2")._build_validation_code({})
        assert "null_counts = {c: int(_df[c].isna().sum()) for c in _df.columns}" in pandas_code
        assert '"null_counts": null_counts,' in pandas_code

    def test_a_dotted_column_name_is_read_as_a_column_and_not_as_a_struct_field(self, monkeypatch):
        """Why every reference is backtick-quoted: ``F.col`` PARSES its argument."""
        frame = Frame(
            name="dotted",
            columns=("a.b",),
            rows=((1,), (NULL,)),
            declared_targets=("a.b",),
        )
        payload = _validate(frame, monkeypatch)
        assert payload["null_counts"] == {"a.b": 1}
        (expressions,) = payload["_stub"].select_calls
        assert expressions[2].expression.condition.column.ref == "`a.b`"

    def test_a_backtick_in_a_column_name_is_doubled_in_the_reference(self, monkeypatch):
        payload = _validate(
            Frame(name="ticked", columns=("g`h",), rows=((1,),), declared_targets=("g`h",)),
            monkeypatch,
        )
        (expressions,) = payload["_stub"].select_calls
        assert expressions[2].expression.condition.column.ref == "`g``h`"


# ══════════════════════════════════════════════════════════════════════
# R10.2 / R10.5 — the metrics equal their definitions
# ══════════════════════════════════════════════════════════════════════


class TestTheMetricsEqualTheirDefinitions:
    """Every expectation is task 1.1's oracle, imported. Task 6.4 generalises."""

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_record_count_is_the_row_count(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["record_count"] == len(frame.rows)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_null_counts_equal_the_definition(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["null_counts"] == null_counts_from_definition(frame)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_completeness_equals_the_definition(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["completeness"] == completeness_from_definition(frame)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_duplicates_equal_the_definition(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["duplicates"] == duplicate_rows_from_definition(frame)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_uniqueness_equals_the_definition(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["uniqueness"] == uniqueness_from_definition(frame)

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_missing_columns_equal_the_definition(self, frame, monkeypatch):
        assert _validate(frame, monkeypatch)["missing_columns"] == missing_target_columns_from_definition(frame)

    @pytest.mark.parametrize(
        "frame",
        [f for f in ALL_FRAMES if "schema_compliance" in f.expected],
        ids=lambda f: f.name,
    )
    def test_schema_compliance_equals_the_definition(self, frame, monkeypatch):
        """The one metric design §2 says does not move — set arithmetic both sides.

        The emitted expression is ``1 - missing/declared`` where the oracle is
        ``present/declared``; algebraically equal, and asserted rather than assumed
        because the two could differ in the last bit before rounding.
        """
        assert _validate(frame, monkeypatch)["schema_compliance"] == schema_compliance_from_definition(frame)

    @pytest.mark.parametrize(
        "frame",
        [f for f in ALL_FRAMES if "overall_quality" in f.expected],
        ids=lambda f: f.name,
    )
    def test_overall_quality_is_the_mean_of_the_rounded_components(self, frame, monkeypatch):
        payload = _validate(frame, monkeypatch)
        assert payload["overall_quality"] == overall_from_components(
            payload["completeness"], payload["uniqueness"], payload["schema_compliance"]
        )

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_every_hand_computed_expectation_is_reproduced(self, frame, monkeypatch):
        """Against the literals task 1.1 wrote out, not only against its helpers."""
        payload = _validate(frame, monkeypatch)
        for key, expected in frame.expected.items():
            assert payload[key] == expected, f"{frame.name}.{key}"


class TestTheNanReadingIsTheDefinitions:
    """C7's one value divergence, which is the whole re-baseline."""

    def test_a_nan_is_not_counted_as_a_null(self, monkeypatch):
        payload = _validate(NULLS_INCLUDING_NAN, monkeypatch)
        assert payload["null_counts"]["amount"] == 1, "one absent cell; the NaN is a present value"
        assert payload["completeness"] == 83.3

    def test_a_frame_whose_only_missing_marker_is_nan_is_fully_complete(self, monkeypatch):
        payload = _validate(DUPLICATE_ROWS_CONTAINING_NAN, monkeypatch)
        assert payload["null_counts"] == {"k": 0, "v": 0}
        assert payload["completeness"] == 100.0

    def test_a_row_carrying_nan_is_still_a_duplicate_of_itself(self, monkeypatch):
        """Spark normalises NaN in a distinct key — reasoned, not measured.

        The stub encodes ``NormalizeFloatingNumbers``; no Spark runs here. This is
        the one place the emitted arithmetic depends on a documented Spark rule
        rather than on plain IEEE comparison, and it is also where the definition
        and the recorded pandas observation happen to agree.
        """
        payload = _validate(DUPLICATE_ROWS_CONTAINING_NAN, monkeypatch)
        assert payload["duplicates"] == 1
        assert payload["uniqueness"] == 50.0

    def test_a_nan_bearing_row_is_not_a_duplicate_of_a_null_bearing_one(self, monkeypatch):
        """The two are different values, so the rows are distinct."""
        frame = Frame(
            name="nan_against_null",
            columns=("k", "v"),
            rows=(("a", NAN), ("a", NULL)),
            declared_targets=("k", "v"),
        )
        payload = _validate(frame, monkeypatch)
        assert payload["duplicates"] == 0
        assert payload["null_counts"] == {"k": 0, "v": 1}


class TestTheRecordedMovementAgainstPandas:
    """The re-baseline, stated. Every difference here is expected and predicted.

    Asserted against task 1.1's ``observed_pandas`` records so that a value which
    moves is legible as the measured semantic difference C7 predicts rather than
    as an unexplained regression — and so that a value which is NOT supposed to
    move fails here if it ever does.
    """

    #: The frames C7 predicts a movement on: exactly those carrying a float NaN.
    MOVED = {"nulls_including_nan", "duplicate_rows_containing_nan"}

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_exactly_the_nan_bearing_frames_move(self, frame, monkeypatch):
        payload = _validate(frame, monkeypatch)
        moved = {key for key, observed in frame.observed_pandas.items() if key in payload and payload[key] != observed}
        assert bool(moved) == (frame.name in self.MOVED), f"{frame.name} moved on {sorted(moved)}"

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_completeness_never_falls_below_the_pandas_value(self, frame, monkeypatch):
        """C7's direction: pandas counts MORE nulls, so it scores lower or equal."""
        assert _validate(frame, monkeypatch)["completeness"] >= frame.observed_pandas["completeness"]

    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_uniqueness_duplicates_and_schema_compliance_do_not_move(self, frame, monkeypatch):
        payload = _validate(frame, monkeypatch)
        for key in ("uniqueness", "duplicates", "schema_compliance", "missing_columns"):
            assert payload[key] == frame.observed_pandas[key], f"{frame.name}.{key} is not supposed to move"

    def test_the_two_movements_are_the_ones_recorded_in_the_module_docstring(self, monkeypatch):
        """The exact numbers, so the report of this change is checkable."""
        first = _validate(NULLS_INCLUDING_NAN, monkeypatch)
        assert (NULLS_INCLUDING_NAN.observed_pandas["null_counts"]["amount"], first["null_counts"]["amount"]) == (2, 1)
        assert (NULLS_INCLUDING_NAN.observed_pandas["completeness"], first["completeness"]) == (75.0, 83.3)
        assert (NULLS_INCLUDING_NAN.observed_pandas["overall_quality"], first["overall_quality"]) == (91.7, 94.4)

        second = _validate(DUPLICATE_ROWS_CONTAINING_NAN, monkeypatch)
        assert (DUPLICATE_ROWS_CONTAINING_NAN.observed_pandas["null_counts"]["v"], second["null_counts"]["v"]) == (2, 0)
        assert (DUPLICATE_ROWS_CONTAINING_NAN.observed_pandas["completeness"], second["completeness"]) == (50.0, 100.0)
        assert (DUPLICATE_ROWS_CONTAINING_NAN.observed_pandas["overall_quality"], second["overall_quality"]) == (
            66.7,
            83.3,
        )


class TestDtypesIsEmittedButNotPinned:
    @pytest.mark.parametrize("frame", ALL_FRAMES, ids=lambda f: f.name)
    def test_dtypes_covers_every_column_and_maps_names_to_strings(self, frame, monkeypatch):
        dtypes = _validate(frame, monkeypatch)["dtypes"]
        assert set(dtypes) == set(frame.columns)
        assert all(isinstance(value, str) for value in dtypes.values())

    def test_no_frame_records_an_expected_dtypes_value(self):
        """Task 1.1 leaves ``dtypes`` unpinned on purpose; nothing here re-pins it."""
        for frame in ALL_FRAMES:
            assert "dtypes" not in frame.expected
            assert "dtypes" not in frame.observed_pandas


# ══════════════════════════════════════════════════════════════════════
# The declared-target reading, and the empty-set case
# ══════════════════════════════════════════════════════════════════════


class TestDeclaredTargetColumns:
    """Mirrors ``_build_validation_code``'s one-line reading; pinned against it."""

    @pytest.mark.parametrize(
        "schema,expected",
        [
            ({"properties": {"a": {}, "b": {}}}, ["a", "b"]),
            ({"fields": {"a": {}, "b": {}}}, ["a", "b"]),
            # `properties` wins whenever the KEY is present — p33 3.14's shape.
            ({"properties": {}, "fields": {"a": {}}}, []),
            ({"properties": {"a": {}}, "fields": {"b": {}}}, ["a"]),
            ({}, []),
            (None, []),
        ],
    )
    def test_the_reading_agrees_with_the_pandas_builder(self, schema, expected, pandas_runner):
        assert declared_target_columns(schema) == expected
        if schema is not None:
            pandas_code = pandas_runner._build_validation_code(schema)
            assert f"expected = {json.dumps(expected)}" in pandas_code

    def test_declaration_order_is_preserved(self):
        assert declared_target_columns({"properties": {"z": {}, "a": {}, "m": {}}}) == ["z", "a", "m"]

    def test_the_declared_list_reaches_the_emitted_statement(self):
        code = build_validation_code(_target_schema(DECLARED_TARGET_COLUMN_ABSENT))
        assert f"expected = {json.dumps(list(DECLARED_TARGET_COLUMN_ABSENT.declared_targets))}" in code

    def test_a_list_shaped_fields_is_refused_rather_than_read_as_nothing_declared(self):
        """Reading it as "nothing declared" would report a vacuous 100.0."""
        with pytest.raises(FrameBuildError, match="vacuous"):
            declared_target_columns({"fields": [{"name": "a"}]})


class TestZeroDeclaredTargetsReproducesTheVacuousScore:
    """Knowingly, not by accident (R10.2's gap, task 1.1's ``NO_DECLARED_TARGETS``)."""

    def test_the_definition_does_not_decide_it(self):
        with pytest.raises(ValueError, match="does not decide"):
            schema_compliance_from_definition(NO_DECLARED_TARGETS)

    @pytest.mark.parametrize("schema", [None, {}, {"properties": {}}])
    def test_the_emitted_statement_reports_the_recorded_short_circuit(self, schema, monkeypatch):
        payload = _validate(NO_DECLARED_TARGETS, monkeypatch, target_schema=schema)
        assert payload["schema_compliance"] == NO_DECLARED_TARGETS.observed_pandas["schema_compliance"] == 100.0
        assert payload["missing_columns"] == []
        assert payload["overall_quality"] == NO_DECLARED_TARGETS.observed_pandas["overall_quality"]

    def test_the_short_circuit_is_the_pandas_path_s_own_expression(self, pandas_runner):
        expression = (
            "schema_compliance = round((1 - len(missing) / max(len(expected), 1)) * 100, 1) if expected else 100.0"
        )
        assert expression in build_validation_code(None)
        assert expression in pandas_runner._build_validation_code({})


# ══════════════════════════════════════════════════════════════════════
# The absent-transform-frame arm, reproduced verbatim
# ══════════════════════════════════════════════════════════════════════


class TestTheAbsentTransformFrameArm:
    """P28 R10.4 — never fall back to the raw ``df``; that is validation theater."""

    def test_the_statement_prints_the_error_payload_and_raises(self, monkeypatch):
        _install_pyspark_functions(monkeypatch)
        buffer = StringIO()
        with redirect_stdout(buffer), pytest.raises(RuntimeError, match="did not produce df_transformed"):
            exec(compile(build_validation_code(None), "<validation>", "exec"), {})
        assert _payload(buffer.getvalue()) == {"error": "transform did not produce df_transformed"}

    def test_the_parser_reports_it_as_a_non_success_with_the_error_detail(self, monkeypatch):
        _install_pyspark_functions(monkeypatch)
        buffer = StringIO()
        with redirect_stdout(buffer), pytest.raises(RuntimeError):
            exec(compile(build_validation_code(None), "<validation>", "exec"), {})
        parsed = glue_session_runner._parse_results(None, buffer.getvalue())

        assert parsed["execution_successful"] is False
        assert parsed["error_details"] == "transform did not produce df_transformed"
        assert parsed["data_quality_results"] == {}

    def test_the_message_is_the_pandas_path_s_own(self, pandas_runner):
        """A second spelling of an inline literal, so it is pinned against the first."""
        message = glue_session_runner._MISSING_TRANSFORM_FRAME_ERROR
        assert message == "transform did not produce df_transformed"
        assert message in pandas_runner._build_validation_code({})

    def test_the_raw_frame_is_never_read(self, monkeypatch):
        """``df`` is in scope on the session; binding it would validate the INPUT."""
        code = build_validation_code(None)
        assert "_df = df_transformed" in code
        assert f"_df = {FRAME_VARIABLE}\n" not in code

        # And behaviourally: a namespace carrying `df` but not `df_transformed`
        # still refuses rather than validating the frame it was given.
        _install_pyspark_functions(monkeypatch)
        with redirect_stdout(StringIO()), pytest.raises(RuntimeError):
            exec(compile(code, "<validation>", "exec"), {FRAME_VARIABLE: _stub_frame(NULLS_INCLUDING_NAN)})


# ══════════════════════════════════════════════════════════════════════
# The GENERATED marker and the frame-build statement
# ══════════════════════════════════════════════════════════════════════


class TestTheGeneratedMarker:
    def test_it_reports_on_the_frame_variable(self):
        code = build_generated_marker_code()
        assert f"{FRAME_VARIABLE}.count()" in code
        assert f"list({FRAME_VARIABLE}.columns)" in code

    def test_it_carries_the_two_keys_the_pandas_path_emits(self, pandas_runner):
        pandas_code = pandas_runner._build_test_data_code({"properties": {"a": {"type": "string"}}})
        assert '"record_count"' in pandas_code and '"columns"' in pandas_code
        code = build_generated_marker_code()
        assert '"record_count"' in code and '"columns"' in code

    def test_the_record_count_is_counted_and_not_assumed(self, monkeypatch):
        """C7's fourth row: ``len(df)`` → ``df.count()``, an action.

        Counted rather than assumed because a ``sample`` frame's row count is not
        knowable host-side — ``.limit(N)`` is a ceiling and the object may hold
        fewer rows.
        """
        stub = _StubFrame(column_names=["a"], rows=[{"a": 1}] * 7, spark_dtypes=[("a", "bigint")])
        stdout = _execute(build_generated_marker_code(), {FRAME_VARIABLE: stub}, monkeypatch)

        assert _payload(stdout, "GENERATED") == {"record_count": 7, "columns": ["a"]}
        assert stub.collect_calls == 1, "the marker is the frame-build statement's one action"

    def test_the_parser_reads_it_as_input_records(self, monkeypatch):
        stub = _StubFrame(column_names=["a"], rows=[{"a": 1}] * 3, spark_dtypes=[("a", "bigint")])
        stdout = _execute(build_generated_marker_code(), {FRAME_VARIABLE: stub}, monkeypatch)
        assert glue_session_runner._parse_results(None, stdout)["input_records"] == 3


class TestTheFrameBuildStatement:
    """The 6.1 seam the frame builders' docstrings promise."""

    SYNTHETIC = FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC)
    SAMPLE = FrameSource(
        mode=FRAME_MODE_SAMPLE,
        requested_mode=FRAME_MODE_SAMPLE,
        source_uri="s3://bucket/key.parquet",
        read_format="parquet",
    )
    SCHEMA = {"properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}

    def test_it_is_the_frame_code_plus_the_namespace_plus_the_marker(self):
        """Three components since defect 3, still asserted as exact equality.

        The middle one is ``build_transform_namespace_code`` — the import lines
        that reproduce the generated job's module-scope namespace, so the
        transform statement's bare ``col`` resolves (R1.4). It is folded into this
        statement rather than submitted as a fourth, because
        ``SHADOW_GLUE_WORST_CASE_SECONDS`` is derived from *three* statements and
        its drift fence hardcodes the same ``3 *``; see
        ``test_transform_namespace_preamble.py`` for that reasoning and for the
        namespace's own coverage.
        """
        assert frame_build_statement(self.SYNTHETIC, self.SCHEMA) == (
            build_frame_code(self.SYNTHETIC, self.SCHEMA)
            + build_transform_namespace_code()
            + build_generated_marker_code()
        )

    def test_it_composes_on_the_sample_arm_too(self):
        assert frame_build_statement(self.SAMPLE, None) == (
            build_sample_frame_code("s3://bucket/key.parquet", "parquet")
            + build_transform_namespace_code()
            + build_generated_marker_code()
        )

    @pytest.mark.parametrize("mode", ["synthetic", "sample"])
    def test_the_composed_statement_parses(self, mode):
        source = self.SYNTHETIC if mode == "synthetic" else self.SAMPLE
        assert ast.parse(frame_build_statement(source, self.SCHEMA))

    def test_the_individual_builders_still_emit_no_marker(self):
        """Their contract is unchanged: the marker lives in exactly one place.

        ``build_frame_code`` stays a pure mode dispatch as well, so what each
        builder produces remains checkable on its own.
        """
        for code in (
            build_synthetic_frame_code(self.SCHEMA),
            build_sample_frame_code("s3://bucket/key.parquet", "parquet"),
            build_frame_code(self.SYNTHETIC, self.SCHEMA),
            build_frame_code(self.SAMPLE, None),
        ):
            assert "GENERATED:" not in code
            assert "VALIDATION:" not in code

    def test_a_declared_type_outside_the_vocabulary_still_fails_before_any_session(self):
        """Composition adds no failure mode and hides none."""
        with pytest.raises(FrameBuildError):
            frame_build_statement(self.SYNTHETIC, {"properties": {"bad": {"type": "geography"}}})


class TestTheValidationStatementPairing:
    def test_it_pairs_the_stage_label_with_the_code(self):
        stage, code = validation_statement(_target_schema(NULLS_INCLUDING_NAN))
        assert stage == STAGE_VALIDATION
        assert code == build_validation_code(_target_schema(NULLS_INCLUDING_NAN))

    def test_it_is_the_shape_run_guarded_session_takes(self, monkeypatch):
        """``[(stage_label, code), …]`` — *statements_after_frame*'s type."""
        pair = validation_statement(None)
        assert isinstance(pair, tuple) and len(pair) == 2
        assert all(isinstance(part, str) for part in pair)


# ══════════════════════════════════════════════════════════════════════
# The frame-build statement is what a guarded run submits
# ══════════════════════════════════════════════════════════════════════


def test_a_guarded_run_submits_the_composed_frame_statement(monkeypatch):
    """The marker reaches Spark, not merely the module.

    ``run_guarded_session`` builds the frame statement itself, so this is the only
    place the composition can be observed at the submission boundary.
    """
    glue = MagicMock()
    glue.create_session.return_value = {"Session": {"Id": "s"}}
    glue.get_session.return_value = {"Session": {"Status": "READY"}}
    glue.run_statement.return_value = {"Id": 1}
    glue.get_statement.return_value = {
        "Statement": {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}}
    }

    schema = {"properties": {"id": {"type": "integer"}}}
    with patch("glue_session_runner.boto3") as boto:
        boto.client.return_value = glue
        runner = glue_session_runner.GlueSessionTestRunner("us-west-2", role_arn="arn:aws:iam::1:role/r")
        run = runner.run_guarded_session(schema, [validation_statement(None)])

    submitted = [call.kwargs["Code"] for call in glue.run_statement.call_args_list]
    assert submitted[0] == frame_build_statement(
        FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC), schema
    )
    assert submitted[0].startswith(build_synthetic_frame_code(schema)), "the seeded frame R10.3 pins is unchanged"
    assert "GENERATED:" in submitted[0]
    assert submitted[1] == build_validation_code(None)
    assert not run.refused
