# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p44-verify-against-real-source-data, Property 10: No sampled cell
# value leaves the Verification_Session
"""Property 10 — no sampled cell value leaves the Verification_Session.

Spec ``p44-verify-against-real-source-data`` task 8.3, design §6.2.

    *For any* frame width, column-name set and distinctive taint value present
    only in the notional sampled rows, the taint appears in no emitted marker
    code, no rendered report, no captured S3 object body, no captured DynamoDB
    item, no log record at any level, and no frame provenance.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.7**

WHAT THE TAINT IS, AND WHAT IT IS NOT
-------------------------------------
The taint is a **cell value** — one datum inside one row of a notional sample.
It is never a column name. That distinction is resolved decision 3 and it is
load-bearing in both directions:

* **Real column NAMES are permitted and are NOT asserted absent.** Column names
  are schema, not data; the declared schema already appears throughout the
  dashboard; and a real header that does not match the declared schema is among
  the likeliest real causes of a transform producing zero rows, which is part of
  why the sample is worth reading at all. Asserting them absent would contradict
  R6.1, which explicitly permits ``columns``. This file therefore asserts the
  drawn column names are **present** in the rendered report — see
  :class:`TestColumnNamesAreSchemaAndAreAllowedThrough`. That assertion doubles
  as the plumbing check: a run whose column names never reached the report would
  be a run whose sinks this file was not really inspecting.
* Every drawn taint is wrapped in a ``TAINT…TAINT`` sentinel over an alphabet
  disjoint from the identifiers the column-name strategy produces, so a taint can
  never collide with a column name, a URI, a marker key or a Spark dtype. That is
  what makes "the taint appears nowhere" a statement about leakage rather than
  about coincidence.

**NO REAL SAMPLED DATA EXISTS ANYWHERE IN THIS FILE** (do-NOT 2). The rows are
notional and the taint is a string injected into the places a real cell value
*could* travel. No S3 object is read and no data source is contacted.

WHERE THE LINE IS — THE R6.9 EXCEPTION, STATED RATHER THAN DISCOVERED
---------------------------------------------------------------------
Property 10 is about a sampled row value reaching a sink **through the
marker / result / report / provenance path**. It is deliberately NOT a claim that
no fragment of a customer value can appear in ``error_details``, because R6.9
requires the opposite.

Task 8.1 landed ``_ERROR_LITERAL_MAX_CHARS = 64`` and
``_truncate_quoted_literals``, applied at ``exception_summary`` and at
``synthesised_traceback``'s frame rendering. That helper **bounds** a quoted
literal; it does not remove it (read end to end at
``glue_session_runner.py:3293-3383``). So a Spark diagnostic such as
``For input string: "1,234.56"`` keeps a 64-character prefix of the offending
value, and a taint shorter than 64 characters inside a quoted literal in
``Output.ErrorValue`` **does** reach ``error_details`` — by design. R6.6 bounds
that exposure and R6.9 requires the exception type and the failing column
identity to survive it, because "a truncation that loses them defeats its own
purpose" (do-NOT 16).

Asserting absence there would therefore be asserting against a ratified
requirement. :class:`TestTheBoundaryWithR69` pins that boundary from the other
side — the bounded literal survives, and it is bounded — so the scope of
Property 10 is a stated line rather than an accident of which channel the
generator happened to reach.

THE HONEYPOT — WHY THE FRAME OFFERS THE TAINT INSTEAD OF WITHHOLDING IT
-----------------------------------------------------------------------
Scanning the emitted code for the taint is nearly vacuous on its own: the marker
builders emit a *template*, and no row value is interpolated into it, so the
taint cannot be there today whatever the builders do with rows tomorrow. A change
that emitted ``print(df.take(5))`` would carry a hundred customer cells out of the
session and would pass a pure string scan of the code.

So the emitted marker code is **executed** against :class:`_HoneypotFrame`, a stub
Spark frame that answers every row-materialising accessor a future marker might
reach for — ``collect``, ``take``, ``head``, ``first``, ``toJSON``, ``toPandas``,
``show`` — and answers each one with the taint. The frame is a honeypot: it
*offers* the value, and the property asserts nothing carried it out. Under the
``take(5)`` change above the taint appears in captured stdout and the property
fails, which is what makes it a tripwire rather than a description.

:class:`TestTheDetectorDetects` proves the honeypot works by leaking on purpose
through the same assertion helper every other test in this file uses.

The stub is written here rather than imported from
``test_validation_statement.py``, which has an equivalent one. Two reasons: a
test module importing another test module by bare name depends on pytest's
``prepend`` import mode and on that file's current internals, and four sibling
tasks are editing this directory concurrently — a shared stub would couple this
file's result to an unrelated in-flight edit. The honeypot also needs the row
accessors that file's stub deliberately does not have.

WHAT IS CAPTURED VERSUS WHAT IS REASONED ABOUT
----------------------------------------------
Captured, as real objects produced by the shipped code:

* the three emitted statements — ``frame_build_statement`` (which contains
  ``build_sample_frame_code``'s reader and ``build_generated_marker_code``'s
  print), ``build_generated_marker_code`` alone, and ``build_validation_code``;
* the **stdout** those markers actually print when executed against the honeypot;
* the nine-key Test_Result_Contract dict ``success_results`` derives from that
  stdout through the real ``_parse_results``;
* ``FrameSource.provenance()``;
* and, by driving :func:`lambda_function.lambda_handler` on the ``glue`` arm with
  ``run_test`` returning that contract dict, every sink R6.11 enumerates —
  ``test_report.md``'s S3 body, ``test_results.json``'s S3 body (which carries the
  report twice, as ``review_result`` and as ``structured_test_report``), the
  ``test_results`` DynamoDB item, the ``pipeline_runs`` state-row values that
  carry ``checks[].detail`` and the ``review_summary`` excerpt, and every log
  record at every level.

Reasoned about rather than captured, and stated as such:

* Glue's service-owned ``/aws-glue/*`` log groups are outside reach (R6.12). The
  truncation and every assertion here are host-side, applied to text read back
  through ``GetStatement``; Spark logs inside the session, before any read.
* ``Output.Data.TextPlain`` stdout is bounded only by ``_bound_stderr`` (do-NOT
  24). This file executes the markers that write that stdout and asserts on what
  they print, which is the reachable half.
* R6.5 — no copy of the sampled rows persists beyond the session — is structural
  and is design §6.3's SMOKE row, not this property's.

do-NOT 1 AND do-NOT 2, MEASURED RATHER THAN ASSUMED
---------------------------------------------------
No Glue interactive session is created and no S3 object is read. Two independent
measures, because task 5.1's execution walked into the first one: an in-process
``run_guarded_session`` with ``frame_mode="sample"`` reaches the real
``CreateSession`` even with ``source_schema=None``, since ``is_sample`` retires
the absent-schema refusal arm and nothing is left to refuse.

1. ``glue_session_runner.boto3.client`` is patched wherever a runner is built, and
   the handler drive never constructs a real one — ``GlueSessionTestRunner`` is a
   ``MagicMock``.
2. :func:`no_aws_api_calls` guards ``botocore``'s own ``_make_api_call`` seam for
   every Glue session operation **and** for ``GetObject`` / ``ListObjectsV2`` /
   ``ListBuckets``, raising a ``BaseException`` subclass. Task 6.2's guard, widened
   by the three S3 operations because do-NOT 2 forbids reading a real object and
   this file is the one that talks about reading objects. ``BaseException`` for
   task 6.2's reason: the handler's verification arm is wrapped in
   ``except Exception``, which would swallow an ``Exception`` here, route it to a
   failure result, and let the test pass while a real call had been attempted.

LEDGER — AND A GAP THIS FILE HAS TO REPORT
------------------------------------------
``decisions(concept="security")`` returns **nothing**, both with and without
``include_backfilled=True``. This repository holds **no ratified decision about
data protection for customer rows** (requirements C10). Requirement 6 is
therefore first-of-kind: this file is testing a **proposal for ratification**, not
restating a settled rule, and the constant it rests on says so at its own
definition (``glue_session_runner.py:3255-3263``).

Records that do govern something asserted here:

* ``LGR-28b4d75-5`` (**template / ratified**) — expectations derive from the
  requirements, never from the producer's own output. Every key set, bound and
  sentinel below is transcribed from ``requirements.md`` R6 and design §6.2.
* ``LGR-819980f-1`` (**standing invariant**) — a ``jdbc://`` source is
  structurally unable to reach a read. Nothing here constructs a JDBC sample.
* ``LGR-ad78946-5`` (**template / ratified**) — the ERROR-level fall-back log.
  R6.4 is "no sampled row value at **any** log level", so the capture below sets
  no level floor rather than filtering to WARNING and above.
* ``LGR-3e45e65-1`` (**template / ratified**) — the region is pinned in
  ``tests/unit/conftest.py``; the ``setdefault`` calls below are consistent
  no-ops.

BLAST RADIUS AND COVERAGE, from CodeGraph ``explore``
-----------------------------------------------------
Counts are inflated by test-site attribution to the module under test, so the
production figure follows each one, read out of the module:

* ``build_generated_marker_code`` (``glue_session_runner.py:2622``) — reported 13
  callers, **1** in production (``frame_build_statement:2701``); covered by
  ``test_marker_round_trip_prop.py``, ``test_validation_statement.py``.
* ``build_validation_code`` (``:2706``) — reported 31, **1** in production
  (``validation_statement``); covered by ``test_validation_payload_keys_prop.py``,
  ``test_marker_round_trip_prop.py``, ``test_validation_statement.py``.
* ``frame_build_statement`` (``:2649``) — reported 19, **1** in production
  (``run_guarded_session``); covered by ``test_sample_frame_dispatch.py``,
  ``test_frame_refusal_reuse.py``, ``test_frame_refusal_precedes_session_prop.py``,
  ``test_run_test_composition.py`` +3.
* ``build_sample_frame_code`` (``:2201``) — reported 14, **1** in production
  (``build_frame_code:2292``); covered by ``test_sample_frame_dispatch.py``,
  ``test_s3_scheme_gate_prop.py``, ``test_validation_statement.py``. Its emitted
  PySpark has still never run against real Spark.
* ``FrameSource.provenance`` (``:415``) — covered by ``test_frame_provenance.py``
  (task 5.2), which pins the **six-key set** as an equality and
  ``dataclasses.fields(FrameSource)`` as exactly five names. This file
  deliberately does **not** restate either: R6.7 holds by construction because no
  field is a row, and 5.2 is what makes that construction falsifiable. What is
  added here is orthogonal — a statement about the **values**, over a taint that
  exists only in the rows.
* ``generate_comprehensive_test_report`` (``lambda_function.py:2155``) — covered
  by ``test_comprehensive_report.py``, ``test_report_names_the_running_executor.py``.

``shared/`` in the blast radius: ``shared/decimal_utils.convert_floats_to_decimal``
sits on the DynamoDB path and ``shared/artifact_schema.create_output_data`` is the
five-key gate. Both are exercised through the handler drive rather than described,
so a value that survived either one would surface in a captured sink here. This
file modifies neither, and modifies no production module.
"""

import json
import logging
import math
import os
import sys
import types
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from unittest.mock import MagicMock, patch

import botocore.client
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region/account before any boto3 import. `LGR-3e45e65-1` pins the region in
# tests/unit/conftest.py, so these are consistent no-ops.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The REAL runner module, imported before the stub set below: `glue_session_runner`
# imports from `code_interpreter_runner` at its own `:81`, so stubbing that module
# for this import would give the marker builders a MagicMock `_parse_results` and
# every stdout assertion would then be about a mock. Same ordering as
# `test_execution_verification_statement.py`.
from glue_session_runner import (
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    STAGE_VALIDATION,
    FrameSource,
    StatementOutcome,
    build_generated_marker_code,
    build_validation_code,
    exception_summary,
    frame_build_statement,
    statement_failure_results,
    success_results,
)

# Stub the heavy / AWS-touching imports so the handler module loads in isolation.
# The same set every sibling in this directory uses.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

#: A source object URI in the shape `file_validation` writes
#: (`file_validation/lambda_function.py:632`) — `sourceBucket`, not
#: `processedBucket`. Never read: `no_aws_api_calls` forbids `GetObject`.
S3_URI = "s3://adi-source-files-123456789012-us-west-2/pipeline-a/orders.csv"


# ---------------------------------------------------------------------------
# do-NOT 1 and do-NOT 2, measured rather than assumed
# ---------------------------------------------------------------------------


class AwsApiGuardViolation(BaseException):
    """A test in this file reached a real AWS API call. Stub the client."""


#: The session operations that bill, plus the polls a run would make against a
#: live session, plus the three S3 reads do-NOT 2 forbids. Enumerated rather than
#: "every operation" so the guard's failure message can name what was attempted.
GUARDED_OPERATIONS = frozenset(
    {
        "CreateSession",
        "GetSession",
        "RunStatement",
        "GetStatement",
        "StopSession",
        "DeleteSession",
        "GetObject",
        "ListObjectsV2",
        "ListBuckets",
    }
)

_REAL_MAKE_API_CALL = botocore.client.BaseClient._make_api_call


@pytest.fixture(autouse=True)
def no_aws_api_calls(monkeypatch):
    """Make a real Glue session or S3 read unreachable from this file.

    Task 6.2's guard (``test_sample_read_failure_prop.py``), widened by the three
    S3 operations: do-NOT 2 forbids reading a real object, and this is the file
    that talks about reading objects.

    ``BaseException`` on purpose, for task 6.2's reason. The handler's
    verification arm is ``except Exception`` and would swallow an ``Exception``
    raised here, route it to a failure result, and let the test pass while a real
    API call had been attempted — the exact failure mode the guard exists to stop.
    Same mechanism and same reasoning as ``tests/unit/conftest.py``'s
    ``no_cloudwatch_writes``.
    """

    def _guarded_make_api_call(self, operation_name, api_params):
        if operation_name in GUARDED_OPERATIONS:
            raise AwsApiGuardViolation(
                f"a unit test reached a real {operation_name} call — p44 do-NOT 1 forbids creating an "
                f"interactive session (it bills) and do-NOT 2 forbids reading a real source object. "
                f"Patch the client rather than weakening this guard."
            )
        return _REAL_MAKE_API_CALL(self, operation_name, api_params)

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _guarded_make_api_call)


@pytest.mark.parametrize("operation", ["CreateSession", "GetObject"])
def test_a_real_aws_call_would_be_caught_by_the_guard(operation):
    """The guard itself, so do-NOT 1 and do-NOT 2 are measured rather than asserted.

    Without this, :func:`no_aws_api_calls` could be silently inert — a renamed
    ``botocore`` seam, a monkeypatch that did not take — and every claim that no
    session was created and no object was read would rest on the client patches
    alone. Here the guard is provoked on purpose, through the real
    ``_make_api_call`` seam, once per do-NOT.
    """
    real_client = botocore.client.BaseClient

    with pytest.raises(AwsApiGuardViolation):
        real_client._make_api_call(MagicMock(), operation, {})


# ---------------------------------------------------------------------------
# The honeypot frame — it OFFERS the taint; the property says nothing took it
# ---------------------------------------------------------------------------


def _normalise(value: object) -> tuple:
    """A hashable identity in which two NaNs are the same value.

    Spark normalises ``NaN`` in grouping keys, so a row carrying ``NaN`` is a
    duplicate of itself, while ``float('nan') != float('nan')`` in plain Python.
    Without this the stub would report two identical rows as distinct and
    disagree with the emitted statement's own ``count_distinct`` over a struct.
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
    """``Column.isNull()`` — True for a null, **False for a float NaN**."""

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


def _evaluate(expression: object, row: dict) -> object:
    """Evaluate one non-aggregate expression against *row*."""
    if isinstance(expression, _Lit):
        return expression.value
    if isinstance(expression, _Col):
        return row[_unquote(expression.ref)]
    if isinstance(expression, _IsNull):
        return _evaluate(expression.column, row) is None
    if isinstance(expression, _When):
        return expression.value.value if _evaluate(expression.condition, row) else None
    if isinstance(expression, _Struct):
        # Never null, whatever its fields hold — which is why the emitted code
        # takes its distinct count over a struct rather than over bare columns.
        return tuple(_normalise(_evaluate(column, row)) for column in expression.columns)
    raise AssertionError(f"the honeypot does not implement {type(expression).__name__}")


def _aggregate(expression: object, rows: list[dict]) -> int:
    """Evaluate one aggregate expression over every row."""
    if isinstance(expression, _Count):
        return sum(1 for row in rows if _evaluate(expression.expression, row) is not None)
    if isinstance(expression, _CountDistinct):
        values = [_evaluate(expression.expression, row) for row in rows]
        return len({_normalise(value) for value in values if value is not None})
    raise AssertionError(f"{type(expression).__name__} is not an aggregate the honeypot knows")


class _HoneypotFunctions:
    """``pyspark.sql.functions``, as much of it as the emitted statements use."""

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
    frame: "_HoneypotFrame"

    def collect(self) -> list[list]:
        self.frame.aggregate_collect_calls += 1
        return [self.values]


@dataclass
class _HoneypotFrame:
    """A stub Spark frame that hands out the taint to anything that asks.

    The aggregate half — ``columns``, ``dtypes``, ``select().collect()``,
    ``count()`` — is what the two shipped markers actually use, and it answers
    exactly as Spark would for the payload they build.

    The **row half is the honeypot**, and it is the reason this class exists
    rather than a frame that withholds rows. ``collect``, ``take``, ``head``,
    ``first``, ``toJSON``, ``toLocalIterator``, ``toPandas`` and ``show`` all
    return — or, for ``show``, print — the taint-carrying rows. A marker changed
    to preview data would therefore succeed here and its taint would land in
    captured stdout, failing the property. A frame that raised on those accessors
    would fail too, but with a message about a missing stub method rather than
    about a leak, and it would not catch a change that reached only
    ``__getitem__``-free aggregates over the values.

    Every access is counted so that "the marker ran against this frame" is a
    measurement rather than an assumption.
    """

    column_names: list[str]
    rows: list[dict]
    spark_dtypes: list[tuple[str, str]]
    select_calls: list[tuple] = field(default_factory=list)
    aggregate_collect_calls: int = 0
    count_calls: int = 0
    columns_reads: int = 0
    row_accesses: list[str] = field(default_factory=list)

    # -- the aggregate half: what the shipped markers use --------------------

    @property
    def columns(self) -> list[str]:
        self.columns_reads += 1
        return list(self.column_names)

    @property
    def dtypes(self) -> list[tuple[str, str]]:
        return list(self.spark_dtypes)

    def select(self, *expressions) -> _Projection:
        self.select_calls.append(expressions)
        return _Projection([_aggregate(expression, self.rows) for expression in expressions], self)

    def count(self) -> int:
        """``df.count()`` — what the ``GENERATED:`` marker reports."""
        self.count_calls += 1
        return len(self.rows)

    # -- the honeypot half: every way a marker could reach a cell ------------

    def _offer(self, accessor: str) -> list[dict]:
        self.row_accesses.append(accessor)
        return [dict(row) for row in self.rows]

    def collect(self) -> list[dict]:
        return self._offer("collect")

    def take(self, num: int) -> list[dict]:
        return self._offer("take")[:num]

    def head(self, num: int | None = None) -> object:
        offered = self._offer("head")
        return offered[: num or 1] if num is not None else (offered[0] if offered else None)

    def first(self) -> object:
        offered = self._offer("first")
        return offered[0] if offered else None

    def limit(self, num: int) -> "_HoneypotFrame":
        """``.limit`` returns a frame, so a chained preview still finds the rows."""
        self.row_accesses.append("limit")
        return self

    def toJSON(self):  # the PySpark spelling
        return [json.dumps(row, default=str) for row in self._offer("toJSON")]

    def toLocalIterator(self):  # the PySpark spelling
        return iter(self._offer("toLocalIterator"))

    def toPandas(self):  # the PySpark spelling
        return self._offer("toPandas")

    def show(self, *args, **kwargs) -> None:
        for row in self._offer("show"):
            print(row)


#: A plausible Spark dtype per Python cell type. Only the KEY SET of ``dtypes`` is
#: asserted anywhere; these values exist so the honeypot can answer at all.
_SPARK_DTYPE_BY_PYTHON_TYPE = {int: "bigint", float: "double", str: "string", bool: "boolean"}


@contextmanager
def _pyspark_functions_installed():
    """Install a ``pyspark.sql.functions`` the emitted statement can import.

    A context manager rather than a fixture: hypothesis rejects a function-scoped
    fixture requested by a ``@given`` test, and a module-scoped install would
    leave a stub in ``sys.modules`` that changes what every later test in the
    session imports. The originals are restored in ``finally``.
    """
    functions = types.ModuleType("pyspark.sql.functions")
    for name in ("lit", "col", "when", "struct", "count", "count_distinct"):
        setattr(functions, name, getattr(_HoneypotFunctions, name))

    sql = types.ModuleType("pyspark.sql")
    sql.functions = functions
    installed = {"pyspark": types.ModuleType("pyspark"), "pyspark.sql": sql, "pyspark.sql.functions": functions}
    saved = {name: sys.modules.get(name, _ABSENT) for name in installed}
    sys.modules.update(installed)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is _ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


class _Absent:
    """Sentinel for "this module was not in ``sys.modules`` before"."""


_ABSENT = _Absent()


def _execute(code: str, namespace: dict) -> str:
    """Run emitted code against the honeypot; return everything it printed."""
    buffer = StringIO()
    with _pyspark_functions_installed(), redirect_stdout(buffer):
        # `exec` of the emitted marker IS the subject: a string scan of the
        # template cannot see a builder that starts materialising rows.
        exec(compile(code, "<emitted>", "exec"), namespace)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The generators
# ---------------------------------------------------------------------------

#: Column names are SCHEMA (resolved decision 3) and are drawn from an ordinary
#: identifier alphabet — lower case, digits, underscore. Disjoint from the taint
#: alphabet below, so a taint can never be mistaken for a column name.
_COLUMN_NAMES = st.lists(
    st.from_regex(r"\A[a-z][a-z0-9_]{0,11}\Z", fullmatch=True),
    min_size=1,
    max_size=8,
    unique=True,
)

#: The taint sentinel. Upper case and digits only, wrapped in ``TAINT…TAINT``, so
#: it cannot collide with a column name, a bucket or key segment, a marker key, a
#: Spark dtype or any word in the rendered report. That disjointness is what makes
#: "the taint appears nowhere" a leakage claim rather than a coincidence claim.
_TAINT_BODY = st.text(alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789", min_size=8, max_size=24)


@st.composite
def _tainted_frames(draw) -> "_Sample":
    """A notional sample: a column set, a row count, and a taint in the cells.

    The taint is placed in **at least one** cell and the placement varies — one
    column, several columns, one row, every row — so the property is quantified
    over where in the frame a real customer value might sit. Nulls and numeric
    cells are drawn alongside it, because the validation payload's null counts and
    duplicate count are what the markers report *instead of* the values.
    """
    columns = draw(_COLUMN_NAMES)
    row_count = draw(st.integers(min_value=1, max_value=6))
    taint = f"TAINT{draw(_TAINT_BODY)}TAINT"

    tainted_columns = draw(st.lists(st.sampled_from(columns), min_size=1, max_size=len(columns), unique=True))
    tainted_rows = draw(
        st.lists(st.integers(min_value=0, max_value=row_count - 1), min_size=1, max_size=row_count, unique=True)
    )
    filler = draw(st.lists(st.sampled_from([None, 0, 1, 2.5, float("nan"), "ordinary"]), min_size=1, max_size=4))

    rows: list[dict] = []
    for row_index in range(row_count):
        row: dict = {}
        for column_index, name in enumerate(columns):
            if name in tainted_columns and row_index in tainted_rows:
                row[name] = taint
            else:
                row[name] = filler[(row_index + column_index) % len(filler)]
        rows.append(row)

    #: The declared TARGET columns. Drawn from the same identifier alphabet and
    #: partly overlapping the frame's own, so `schema_compliance` and
    #: `missing_columns` are both exercised — `missing_columns` is a list of
    #: column NAMES and is permitted through (R6.1).
    declared = draw(st.lists(st.sampled_from(columns), min_size=0, max_size=len(columns), unique=True))
    return _Sample(columns=columns, rows=rows, taint=taint, declared_targets=declared)


@dataclass(frozen=True)
class _Sample:
    """One drawn notional sample. No real data: the taint is an injected string."""

    columns: list[str]
    rows: list[dict]
    taint: str
    declared_targets: list[str]

    def honeypot(self) -> _HoneypotFrame:
        dtypes = []
        for name in self.columns:
            present = next((row[name] for row in self.rows if row[name] is not None), None)
            dtypes.append((name, _SPARK_DTYPE_BY_PYTHON_TYPE.get(type(present), "string")))
        return _HoneypotFrame(
            column_names=list(self.columns),
            rows=[dict(row) for row in self.rows],
            spark_dtypes=dtypes,
        )

    def target_schema(self) -> dict:
        """The ``properties`` shape ``declared_target_columns`` reads."""
        return {"properties": {name: {"type": "string"} for name in self.declared_targets}}

    def source_schema(self) -> dict:
        """The declared SOURCE schema. Names only — a schema carries no rows."""
        return {"properties": {name: {"type": "string"} for name in self.columns}}

    def frame_source(self) -> FrameSource:
        """A Sample_Frame provenance for this run — the R6.7 subject."""
        return FrameSource(
            mode=FRAME_MODE_SAMPLE,
            requested_mode=FRAME_MODE_SAMPLE,
            source_uri=S3_URI,
            read_format="csv",
        )


# ---------------------------------------------------------------------------
# Running the two markers against the honeypot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SessionEmission:
    """Everything a Verification_Session emitted for one drawn sample.

    ``marker_code`` is what would be submitted; ``stdout`` is what the markers
    actually printed when executed against the honeypot; ``contract`` is the
    nine-key Test_Result_Contract dict the shipped ``success_results`` derived
    from that stdout through the real ``_parse_results``.
    """

    marker_code: dict[str, str]
    stdout: dict[str, str]
    contract: dict
    frame: _HoneypotFrame
    transform_output: _HoneypotFrame


def _emit(sample: _Sample) -> _SessionEmission:
    """Submit the real statements against the honeypot and collect what escaped."""
    frame = sample.honeypot()
    # The transform's output frame. A separate honeypot so that `df` and
    # `df_transformed` are distinct objects, as they are in a real run, and so the
    # validation marker's access counts are attributable to the right one.
    transformed = sample.honeypot()

    frame_statement = frame_build_statement(sample.frame_source(), sample.source_schema())
    marker_only = build_generated_marker_code()
    validation_code = build_validation_code(sample.target_schema())

    # The frame-build statement's leading text is `build_sample_frame_code`'s
    # `spark.read...` — it cannot be executed here (there is no Spark, and
    # do-NOT 2 forbids the read), so the frame is bound directly and the marker
    # half is executed on its own. That is the half that emits; the read half is
    # asserted as text.
    generated_stdout = _execute(marker_only, {"df": frame})
    validation_stdout = _execute(validation_code, {"df_transformed": transformed})

    outcomes = [
        StatementOutcome(
            stage=STAGE_FRAME_BUILD,
            state="AVAILABLE",
            output_status="ok",
            stdout=generated_stdout,
            statement={"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": generated_stdout}}},
        ),
        StatementOutcome(
            stage=STAGE_TRANSFORM,
            state="AVAILABLE",
            output_status="ok",
            stdout="",
            statement={"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": ""}}},
        ),
        StatementOutcome(
            stage=STAGE_VALIDATION,
            state="AVAILABLE",
            output_status="ok",
            stdout=validation_stdout,
            statement={"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": validation_stdout}}},
        ),
    ]

    return _SessionEmission(
        marker_code={
            "frame_build_statement": frame_statement,
            "build_generated_marker_code": marker_only,
            "build_validation_code": validation_code,
        },
        stdout={"GENERATED": generated_stdout, "VALIDATION": validation_stdout},
        contract=success_results(outcomes),
        frame=frame,
        transform_output=transformed,
    )


# ---------------------------------------------------------------------------
# Driving the handler, so the sinks captured are the real ones
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
    "source_data_uri": S3_URI,
}

_PASS_VERDICT = {"verdict": "pass", "issues": [], "categories": {"Correctness": "pass", "Security": "pass"}}


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipeline record exists and names no schema."""
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


@contextmanager
def _capture_log_records():
    """Capture every log record at every level (R6.4).

    A root-logger handler installed by hand rather than ``caplog``, because
    hypothesis rejects a function-scoped fixture requested by a ``@given`` test.
    ``setLevel(0)`` and ``root.setLevel(logging.NOTSET)`` deliberately set **no
    floor**: R6.4 is "no sampled row value at *any* log level", and the fall-back
    path logs at ERROR (``LGR-ad78946-5``) while the unrecognised-mode path logs at
    WARNING, so filtering to any level would narrow the claim.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector(level=0)
    root = logging.getLogger()
    previous_level = root.level
    previous_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    root.addHandler(handler)
    root.setLevel(logging.NOTSET)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
        logging.disable(previous_disable)


@dataclass(frozen=True)
class _CapturedSinks:
    """Every sink R6.11 enumerates, as the handler actually wrote it."""

    report_markdown: str
    test_results_json: dict
    dynamodb_item: dict
    state_row_values: list[dict]
    approval_payload: dict
    checks: list[dict]
    log_text: str
    response: dict

    def everything(self) -> str:
        """One searchable rendering of every captured sink.

        Serialised with ``default=str`` so a ``Decimal`` written by
        ``convert_floats_to_decimal`` on the DynamoDB path is searched as text
        rather than raising — the converter is on the path R6.3 is about, so its
        output has to be inspectable rather than skipped.
        """
        return "\n".join(
            [
                self.report_markdown,
                json.dumps(self.test_results_json, default=str),
                json.dumps(self.dynamodb_item, default=str),
                json.dumps(self.state_row_values, default=str),
                json.dumps(self.approval_payload, default=str),
                json.dumps(self.checks, default=str),
                self.log_text,
                json.dumps(self.response, default=str),
            ]
        )


def _glue_module_stub(runner: MagicMock) -> types.ModuleType:
    """A ``glue_session_runner`` stand-in for the handler's function-local import.

    The handler's ``glue`` branch does ``from glue_session_runner import
    GlueSessionTestRunner`` inside the function, so the constructor is replaced by
    replacing the module entry. No real runner is built, which is do-NOT 1's first
    measure; ``no_aws_api_calls`` is the second.

    ``FRAME_MODE_SYNTHETIC`` is re-exported because the handler imports it at
    ``:1005`` on the shadow path.
    """
    module = types.ModuleType("glue_session_runner")
    module.GlueSessionTestRunner = MagicMock(return_value=runner)
    module.FRAME_MODE_SYNTHETIC = FRAME_MODE_SYNTHETIC
    return module


def _drive_handler(execution_results: dict) -> _CapturedSinks:
    """Drive ``lambda_handler`` on the ``glue`` arm and capture every sink.

    Modelled on ``test_execution_verification_statement.py::_run_handler`` — the
    module-attribute patch harness is this directory's pattern, because moto
    cannot express what this needs (a verification result to inject and a review
    agent to script).

    The ``glue`` arm specifically: a Sample_Frame is reachable only under the
    non-shadow ``glue`` executor (R1.6), so that is the arm whose sinks R6.2 and
    R6.3 are about.
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    results_table = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=json.dumps(_PASS_VERDICT))
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", results_table),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("sys.modules", {"glue_session_runner": _glue_module_stub(runner)}),
        patch.dict(
            "os.environ",
            {
                "USE_AGENTCORE_RUNTIME": "false",
                _mod.VERIFICATION_EXECUTOR_ENV: _mod.VERIFICATION_EXECUTOR_GLUE,
                _mod.VERIFICATION_SESSION_ROLE_ARN_ENV: ROLE_ARN,
                _mod.VERIFICATION_FRAME_MODE_ENV: FRAME_MODE_SAMPLE,
            },
        ),
        _capture_log_records() as records,
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1", "source_data_uri": S3_URI}, None)

    bodies = {call.kwargs["Key"].rsplit("/", 1)[-1]: call.kwargs["Body"] for call in s3.put_object.call_args_list}
    assert "test_report.md" in bodies, sorted(bodies)
    assert "test_results.json" in bodies, sorted(bodies)

    report_markdown = bodies["test_report.md"].decode("utf-8")
    # The drive really took the `glue` arm, asserted on every call rather than in
    # one test. It matters: a Sample_Frame is reachable only under the non-shadow
    # `glue` executor (R1.6), and `resolve_effective_executor` falls back to
    # `pandas` on a missing role ARN (`LGR-ad78946-5`) — a fallback that would
    # leave every sink here captured for the wrong executor while every absence
    # assertion still passed. The same `runner` double answers on both arms, so
    # the report's own heading is the only discriminator available.
    assert "Real Spark Execution Results" in report_markdown, (
        "the handler drive fell back to the pandas arm — the sinks captured are not the Glue "
        "path's, which is the only path a Sample_Frame is reachable on (R1.6)"
    )
    assert runner.run_test.call_count == 1, runner.run_test.call_count
    test_results_json = json.loads(bodies["test_results.json"].decode("utf-8"))

    items = [call.kwargs["Item"] for call in results_table.put_item.call_args_list]
    assert len(items) == 1, f"expected exactly one test-results item, got {len(items)}"

    state_row_values = [
        {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        for call in runs_table.update_item.call_args_list
        if "ExpressionAttributeValues" in call.kwargs
    ]
    approval_payloads = [
        values[key]
        for values in state_row_values
        for key in values
        if key.endswith("test_approval_payload") or key == "output_data"
    ]

    # Formatted, not raw: a `%s` argument carrying a value would not appear in
    # `record.msg` alone, and R6.4 is about what a log READER sees.
    log_text = "\n".join(f"{record.levelname} {record.getMessage()}" for record in records)

    return _CapturedSinks(
        report_markdown=report_markdown,
        test_results_json=test_results_json,
        dynamodb_item=items[0],
        state_row_values=state_row_values,
        approval_payload={"payloads": approval_payloads},
        checks=test_results_json["test_result"]["checks"],
        log_text=log_text,
        response=response,
    )


def _assert_taint_absent(where: str, haystack: str, taint: str) -> None:
    """The one assertion helper every arm uses, so a leak reads identically.

    Shared deliberately: :class:`TestTheDetectorDetects` provokes a leak through
    this same helper, which is what makes "the detector detects" a statement about
    the instrument the property actually uses.
    """
    assert taint not in haystack, (
        f"a sampled CELL VALUE reached {where} — p44 R6 forbids it. The taint {taint!r} exists only in "
        f"the notional sampled rows, so its presence here means a row value left the "
        f"Verification_Session. Column NAMES are permitted (resolved decision 3); values are not."
    )


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


class TestProperty10NoSampledCellValueLeavesTheSession:
    """The taint reaches no marker, no report, no object, no item, no log, no provenance."""

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_no_sampled_cell_value_reaches_any_sink(self, sample: _Sample) -> None:
        """Property 10, over every sink R6.11 enumerates.

        One drawn sample drives the whole chain: the shipped marker builders emit,
        the emitted markers execute against the honeypot, the shipped
        ``success_results`` parses their stdout, and the shipped handler writes
        every sink from that contract dict. So each assertion below is over an
        object the production code produced, not over a transcription of one.
        """
        emission = _emit(sample)

        # R6.1 — the emitted statements. A template today; a tripwire for a
        # builder that starts interpolating rows.
        for name, code in emission.marker_code.items():
            _assert_taint_absent(f"the emitted {name}", code, sample.taint)

        # R6.1 — what the markers actually PRINTED, which is the channel.
        for marker, stdout in emission.stdout.items():
            _assert_taint_absent(f"the {marker}: marker's stdout", stdout, sample.taint)

        # The parsed contract, before any sink sees it.
        _assert_taint_absent("the Test_Result_Contract dict", json.dumps(emission.contract, default=str), sample.taint)

        # R6.7 — the frame provenance.
        _assert_taint_absent(
            "the frame provenance", json.dumps(sample.frame_source().provenance(), default=str), sample.taint
        )

        # R6.2, R6.3, R6.4 — the report, both S3 bodies, the DynamoDB item, the
        # state row, `checks[].detail`, the `review_summary` excerpt, and every
        # log record at every level.
        sinks = _drive_handler(emission.contract)
        _assert_taint_absent("the rendered Comprehensive_Report", sinks.report_markdown, sample.taint)
        _assert_taint_absent(
            "the test_results.json S3 object", json.dumps(sinks.test_results_json, default=str), sample.taint
        )
        _assert_taint_absent(
            "the test_results DynamoDB item", json.dumps(sinks.dynamodb_item, default=str), sample.taint
        )
        _assert_taint_absent(
            "a pipeline_runs state-row value", json.dumps(sinks.state_row_values, default=str), sample.taint
        )
        _assert_taint_absent("checks[].detail", json.dumps(sinks.checks, default=str), sample.taint)
        _assert_taint_absent("a log record", sinks.log_text, sample.taint)
        # The belt-and-braces sweep, so a sink added to `_CapturedSinks` later is
        # covered without a new assertion having to be remembered.
        _assert_taint_absent("some captured sink", sinks.everything(), sample.taint)

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_the_markers_report_only_counts_names_types_and_percentages(self, sample: _Sample) -> None:
        """R6.1's positive half: what the markers DO emit is aggregate only.

        The absence assertion above is one direction; this is the other. Every
        value in the two marker payloads is a number, a column NAME, a Spark dtype
        string, or a container of those — so there is no key under which a cell
        value could be hiding under a different spelling.
        """
        emission = _emit(sample)
        generated = _payload(emission.stdout["GENERATED"], "GENERATED")
        validation = _payload(emission.stdout["VALIDATION"], "VALIDATION")

        assert generated["record_count"] == len(sample.rows)
        assert generated["columns"] == list(sample.columns)

        names = set(sample.columns)
        numeric = ("record_count", "duplicates", "completeness", "uniqueness", "schema_compliance", "overall_quality")
        for key in numeric:
            assert isinstance(validation[key], int | float), (key, validation[key])
        assert set(validation["null_counts"]) == names
        assert all(isinstance(count, int) for count in validation["null_counts"].values())
        assert set(validation["dtypes"]) == names
        assert all(isinstance(dtype, str) for dtype in validation["dtypes"].values())
        assert set(validation["missing_columns"]) <= set(sample.declared_targets)


def _payload(stdout: str, marker: str) -> dict:
    """The JSON object printed after *marker*."""
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
    raise AssertionError(f"no {marker}: payload in {stdout!r}")


# ---------------------------------------------------------------------------
# Anti-vacuity — the honeypot really offers the taint, and the detector detects
# ---------------------------------------------------------------------------


class TestTheDetectorDetects:
    """Without these, "the taint appears nowhere" could be true of nothing at all."""

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_the_honeypot_hands_out_the_taint_to_anything_that_asks(self, sample: _Sample) -> None:
        """The taint IS in the frame — so its absence downstream is a real fact.

        Every row accessor a marker could reach for is exercised, and each one
        surfaces the taint. A honeypot that quietly stopped offering rows would
        make Property 10 pass for the wrong reason, and this is what fails first
        if that happens.
        """
        frame = sample.honeypot()

        assert sample.taint in json.dumps(frame.collect(), default=str)
        assert sample.taint in json.dumps(frame.take(len(sample.rows)), default=str)
        assert sample.taint in json.dumps(frame.head(len(sample.rows)), default=str)
        assert sample.taint in json.dumps(frame.toJSON(), default=str)
        assert sample.taint in json.dumps(list(frame.toLocalIterator()), default=str)
        assert sample.taint in json.dumps(frame.toPandas(), default=str)
        assert sample.taint in json.dumps(frame.limit(1).collect(), default=str)
        assert frame.row_accesses, "the honeypot recorded no row access"

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_a_marker_that_previewed_rows_would_fail_this_property(self, sample: _Sample) -> None:
        """A leaking marker, executed through the same instrument, is caught.

        This is the discriminating test for the whole file. A hypothetical
        ``build_generated_marker_code`` that added a data preview — the plausible
        "make the report more useful" change — is emitted here by hand, executed
        against the same honeypot, and put through the same
        :func:`_assert_taint_absent`. It must fail. If it passed, every absence
        assertion above would be measuring nothing.
        """
        # `collect()` rather than `take(3)`: a bounded preview would miss a taint
        # that happens to sit in a later row, and a discriminating test must not
        # depend on where the generator put it.
        leaky = 'import json\nprint("GENERATED:" + json.dumps({"preview": df.collect()}, default=str))\n'
        stdout = _execute(leaky, {"df": sample.honeypot()})

        assert sample.taint in stdout, "the leaky marker did not leak; the honeypot is not wired"
        with pytest.raises(AssertionError, match="reached the leaky marker"):
            _assert_taint_absent("the leaky marker", stdout, sample.taint)

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_the_shipped_markers_really_ran_against_the_honeypot(self, sample: _Sample) -> None:
        """The emission is a measurement, not an empty exec.

        ``count()`` and ``columns`` for the ``GENERATED:`` marker; one ``select``
        and one ``collect`` for the ``VALIDATION:`` payload — R10.6's single Spark
        action, observed here rather than assumed. A stdout that was empty because
        nothing executed would satisfy every absence assertion vacuously.
        """
        emission = _emit(sample)

        assert emission.frame.count_calls == 1, emission.frame.count_calls
        assert emission.frame.columns_reads >= 1
        assert "GENERATED:" in emission.stdout["GENERATED"]

        assert len(emission.transform_output.select_calls) == 1, emission.transform_output.select_calls
        assert emission.transform_output.aggregate_collect_calls == 1
        assert "VALIDATION:" in emission.stdout["VALIDATION"]

        # Neither shipped marker reached a row accessor. Stated as its own
        # assertion because it is the mechanism behind the property, and a change
        # that started previewing rows should fail here with a message about the
        # accessor as well as above with one about the taint.
        assert emission.frame.row_accesses == [], emission.frame.row_accesses
        assert emission.transform_output.row_accesses == [], emission.transform_output.row_accesses

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_a_value_placed_in_the_contract_dict_does_reach_the_captured_sinks(self, sample: _Sample) -> None:
        """The sink-side twin of the leaky-marker test — the capture chain is live.

        Property 10's sink arms would hold vacuously if nothing the contract dict
        carried ever reached a captured sink. So a value is placed where a leak
        *would* put it — ``error_details``, the one field on the contract that
        renders customer-derived text — and it must arrive.

        This is also the R6.9 channel, which makes the test do double duty: it
        proves the capture is real, and it demonstrates that
        :class:`TestTheBoundaryWithR69`'s boundary is a live path rather than a
        theoretical one. Note the value here is **not** inside a quoted literal,
        so ``_truncate_quoted_literals`` leaves it alone — the truncation bounds
        quoted literals only, and everything outside one is byte-preserved (R6.9).
        """
        contract = dict(_emit(sample).contract)
        contract["execution_successful"] = False
        contract["error_details"] = f"NumberFormatException: {sample.taint}"

        sinks = _drive_handler(contract)

        assert sample.taint in sinks.report_markdown, "the report did not carry error_details"
        assert sample.taint in json.dumps(sinks.test_results_json, default=str)
        assert sample.taint in json.dumps(sinks.checks, default=str), "checks[].detail did not carry it"
        assert sample.taint in json.dumps(sinks.state_row_values, default=str), (
            "no pipeline_runs state-row value carried it — the state row is what carries "
            "`output_data.summary.test_report_markdown` and the `review_summary` excerpt (R6.11)"
        )
        assert sample.taint in sinks.log_text, (
            "no captured log record carried it — the handler logs `error_details` at WARNING "
            "(`lambda_function.py:3112`), so R6.4's arm would be vacuous if this did not arrive"
        )
        assert sample.taint in sinks.everything()
        # NOT the `test_results` DynamoDB item, and that is a fact about the item
        # rather than a gap in the capture: its `error_details` is
        # `f"Failed checks: {', '.join(failed_check_names)}"` (`:3377-3379`), i.e.
        # check NAMES, so the raw text never reaches it. The item stays in
        # Property 10's absence set because R6.11 enumerates it and R6.3 names
        # "any DynamoDB item"; what this comment records is that its channel is
        # narrow, so its absence arm is the weakest of the six.
        assert sample.taint not in json.dumps(sinks.dynamodb_item, default=str)

    def test_the_captured_sinks_are_not_empty(self) -> None:
        """The handler drive really produced the artifacts it claims to capture.

        Every absence assertion over a sink would hold trivially of an empty
        string, so the sinks' own content is pinned once: the report is markdown
        with the execution section in it, both S3 bodies exist, the DynamoDB item
        carries its keys, and at least one log record was captured at some level.
        """
        sample = _Sample(
            columns=["customer_id", "amount"],
            rows=[{"customer_id": "TAINTUNUSEDTAINT", "amount": 1}],
            taint="TAINTUNUSEDTAINT",
            declared_targets=["customer_id"],
        )
        sinks = _drive_handler(_emit(sample).contract)

        assert "Execution Status" in sinks.report_markdown
        assert sinks.test_results_json["test_result"]["checks"]
        assert sinks.dynamodb_item["PK"] == "TEST#cv-1"
        assert sinks.state_row_values, "no pipeline_runs state-row update was captured"
        assert sinks.log_text.strip(), "no log record was captured at any level"
        # The `review_summary` excerpt (R6.11) is inside the approval payload the
        # state row carries, and it is a slice of the report — so it is covered by
        # the state-row assertion above rather than by a separate capture. Pinned
        # here so that stays true rather than becoming an unchecked claim.
        assert any("review_summary" in json.dumps(values, default=str) for values in sinks.state_row_values)


# ---------------------------------------------------------------------------
# Real column NAMES are permitted, and are asserted PRESENT
# ---------------------------------------------------------------------------


class TestColumnNamesAreSchemaAndAreAllowedThrough:
    """Resolved decision 3, asserted in the direction the requirement points."""

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(sample=_tainted_frames())
    def test_the_real_header_reaches_the_markers_and_is_not_filtered(self, sample: _Sample) -> None:
        """A Sample_Frame's ``columns`` marker may carry the real object's header.

        R6.1 names ``columns`` among what a session may emit, and resolved
        decision 3 rejected a per-mode branch on that key: column names are
        schema, not data; the declared schema already appears throughout the
        dashboard; and a header that does not match the declared schema is among
        the likeliest real causes of a transform producing zero rows, which is
        part of why reading the sample is worth doing.

        So this asserts the header is **present**. Asserting it absent would
        contradict the requirement — and would also hide the case the sample
        exists to reveal.
        """
        emission = _emit(sample)
        generated = _payload(emission.stdout["GENERATED"], "GENERATED")
        validation = _payload(emission.stdout["VALIDATION"], "VALIDATION")

        assert generated["columns"] == list(sample.columns)
        for name in sample.columns:
            assert name in validation["null_counts"]
            assert name in validation["dtypes"]

    def test_the_declared_column_names_reach_the_rendered_report(self) -> None:
        """The plumbing check, and the second half of resolved decision 3.

        A run whose column names never reached the report would be a run whose
        sinks the absence assertions were not really inspecting. Fixed names
        rather than drawn ones, because what is asserted is that *these* strings
        travel — the property above is what quantifies over the header.
        """
        sample = _Sample(
            columns=["customer_id", "order_total"],
            rows=[{"customer_id": "TAINTUNUSEDTAINT", "order_total": 1}],
            taint="TAINTUNUSEDTAINT",
            declared_targets=["customer_id", "shipping_region"],
        )
        sinks = _drive_handler(_emit(sample).contract)
        rendered = sinks.everything()

        # `missing_columns` is a list of column NAMES and is reported, which is
        # what makes a header/schema mismatch legible to a reviewer.
        assert "shipping_region" in rendered, "a declared column name did not reach any sink"


# ---------------------------------------------------------------------------
# The boundary with R6.9 — where this property stops, and why
# ---------------------------------------------------------------------------


class TestTheBoundaryWithR69:
    """Property 10 is about the marker path, not about a Spark diagnostic.

    Task 8.1's ``_truncate_quoted_literals`` **bounds** a quoted literal to
    ``_ERROR_LITERAL_MAX_CHARS``; it does not remove it, because R6.9 requires the
    exception type and the failing column identity to survive and resolved
    decision 4 rejected redacting the literal entirely — "``For input string:
    "…"`` with the value removed does not tell a reviewer which of the six causes
    above it was".

    So a short value inside a quoted literal in ``Output.ErrorValue`` does reach
    ``error_details``, by design. These two tests pin that boundary from the other
    side, so the scope of Property 10 above is a stated line rather than an
    accident of which channel the generator happened to reach.
    """

    def test_a_short_quoted_value_survives_into_error_details_by_design(self) -> None:
        """R6.9, not a hole in R6.2.

        The value is a diagnostic here, not a payload: it is the cell that defeated
        a cast, and the whole point of reporting it is that a reviewer can tell a
        thousands separator from a timestamp from a sentinel.
        """
        value = "1,234.56"
        statement = {
            "State": "AVAILABLE",
            "Output": {
                "Status": "error",
                "ErrorName": "NumberFormatException",
                "ErrorValue": f'For input string: "{value}" in column `amount`',
                "Traceback": [],
            },
        }
        outcome = StatementOutcome(
            stage=STAGE_TRANSFORM, state="AVAILABLE", output_status="error", stdout="", statement=statement
        )

        result = statement_failure_results(outcome)

        assert value in result["error_details"], "R6.9 requires the bounded literal to survive"
        # And R6.9's other half: the exception type and the unquoted column
        # identifier are byte-preserved.
        assert "NumberFormatException" in result["error_details"]
        assert "`amount`" in result["error_details"]

    def test_a_long_quoted_value_is_bounded_and_says_how_much_it_dropped(self) -> None:
        """R6.6 and R6.8 — the delimiter-collapse case the bound exists for.

        A CSV read with the wrong delimiter collapses a whole record into one
        field, so the untruncated message would reproduce a customer row. The
        bound caps that at a recognisable fragment and states the elision, which
        is what keeps "the value is enormous" itself a diagnostic.
        """
        row = "TAINT" + "X" * 600 + "TAINT"
        statement = {
            "State": "AVAILABLE",
            "Output": {"Status": "error", "ErrorName": "NumberFormatException", "ErrorValue": f'bad: "{row}"'},
        }

        summary = exception_summary(statement)

        assert row not in summary, "a 610-character field reproduced a whole row into a sink"
        assert "chars elided" in summary
        assert "NumberFormatException" in summary


# ---------------------------------------------------------------------------
# The sinks this property cannot reach, recorded rather than implied
# ---------------------------------------------------------------------------


def test_the_out_of_reach_sinks_are_recorded_here() -> None:
    """R6.12 and do-NOT 24, as a docstring with a home rather than a comment.

    Two channels are outside anything this file can assert, and both are stated
    residuals rather than gaps:

    * **Glue's service-owned ``/aws-glue/*`` log groups.** The truncation and
      every assertion here are host-side, applied to text read back through
      ``GetStatement``. Spark logs the untruncated message *inside* the
      Verification_Session, before any read. That retention is not set by this
      repository and the group names carry a runtime session id, so they cannot be
      enumerated at deploy time. Avoiding this residual means not letting real
      values reach Spark, which means not doing p44 (R6.12).
    * **``Output.Data.TextPlain`` stdout.** It cannot be truncated without
      corrupting the ``GENERATED:`` / ``VALIDATION:`` marker parse (do-NOT 24), so
      it is bounded only by ``_bound_stderr``. What this file *can* do about it is
      what it does: execute the two markers that write that stdout and assert on
      what they print.

    The test body asserts the one thing about them that is checkable — that this
    file still names both, so a residual cannot be quietly dropped while the
    property above continues to read as though it covered everything. Whitespace
    is normalised because the names are wrapped across lines in the prose.
    """
    prose = " ".join(__doc__.split())

    assert "/aws-glue/*" in prose, "the Glue-owned log-group residual (R6.12) is no longer stated"
    assert "Output.Data.TextPlain" in prose, "the stdout-channel residual (do-NOT 24) is no longer stated"
