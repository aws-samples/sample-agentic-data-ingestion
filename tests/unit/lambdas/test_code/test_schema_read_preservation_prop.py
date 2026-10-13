# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — preservation of non-bug-condition behaviour (p33).

For any input where the bug condition does NOT hold, the p33 fix SHALL produce
the same ``test_status``, the same pass/fail per pre-existing check, the same
``verified: False`` partitioning, and — for a source schema of 20 or fewer
fields — **byte-identical** test-frame code.

**GREEN ON UNFIXED CODE.** This file is the baseline the fix must preserve, so a
failure here means either the assertion is wrong or the harness is broken; it
never licenses a change to the source. Every expectation below was derived by
running the UNFIXED code first and recording what it actually produced — the
observed values are quoted in each test's docstring so a later reader can tell a
pin from a guess.

Methodology (design §Preservation Checking)::

    FOR ALL input WHERE NOT isBugCondition(input) DO
      ASSERT test_status_original(input)        = test_status_fixed(input)
      ASSERT verified_partition_original(input) = verified_partition_fixed(input)
    END FOR

Property-based rather than example-based because preservation is a universal
claim, and the interesting cases (refusal x static outcome x execution outcome x
target-schema state) form a product space hand-written cases under-sample.

What is real here and what is simulated
---------------------------------------
Real: ``aggregate_check_results``, ``is_not_verified_check``, the pandas
converter (``PySpark2PandasConverter``), ``classify_conversion_refusal``,
``_conversion_failure_results`` / ``_failed_execution_results`` /
``_empty_results``, ``_build_test_data_code``, ``_build_validation_code``, and
``lambda_handler`` for the D2 fence.

Simulated: the AgentCore Code Interpreter client and the review agent. ``pandas``
is not installed in the unit environment, so where the generated validation code
must actually run, the frame is a local shim (the same pattern
``test_code_interpreter_runner.py`` established). None of the properties below
depend on pandas semantics.

Two ledger notes, so a reader knows which claims are governed:

* ``LGR-334667e-1`` (backfilled) is the record behind requirement 3.3's
  "no execution-success override": it is the commit that removed the override so
  that execution success no longer masks a blocking static failure. Requirement
  3.3 preserves that, and :func:`test_a_failing_verified_check_always_derives_failed`
  is its regression fence.
* ``LGR-3e45e65-1`` (template/ratified) pins ``AWS_REGION`` /
  ``AWS_DEFAULT_REGION`` by assignment in ``tests/unit/conftest.py``, which makes
  the ``os.environ.setdefault`` calls below consistent no-ops rather than the
  thing that chooses the region.

Nothing in the ledger governs the 20-field frame cap — ``why`` on
``code_interpreter_runner.py`` returns only ``LGR-feb6a1d-1`` (backfilled, a
281-file snapshot) and ``LGR-d2e9878-1`` (backfilled), neither of which mentions
a field limit. That is a ledger gap, not an all-clear, and it is precisely why
:func:`test_narrow_schema_frame_code_is_byte_identical` exists.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.14
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, example, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The runner and the converter are the REAL modules — only the bedrock-agentcore
# client is stubbed, so every refusal shape, every generated code string and
# every score below is genuinely produced by the shipped implementation.
from code_interpreter_runner import (
    NOT_VERIFIED_REASON_NOT_YET,
    NOT_VERIFIED_REASON_PERMANENT,
    CodeInterpreterTestRunner,
    classify_conversion_refusal,
)
from pyspark_to_pandas import (
    # p40 task 12.3: the PRODUCER's spelling of the receiver-shape refusal
    # reason, imported rather than transcribed (`LGR-28b4d75-5`: a test written
    # from the producer's output cannot detect the producer drifting, because
    # the output is its own oracle). `_execution_results_for` builds the
    # `ConversionError` with this, and the property asserts against the
    # CONSUMER's spelling (`_mod.SCALAR_LITERAL_RECEIVER_REASON`) — which
    # lambda_function.py:237-253 keeps as a deliberate second spelling, because
    # importing it there yields a dead arm under the `MagicMock` stub of this
    # module. Using both ends here means a drift between them fails this test.
    REFUSAL_REASON_SCALAR_LITERAL,
    ConversionError,
)

# p33 task 4.3 moved the handler's target-schema read into the shared resolver,
# which builds its OWN lazily-cached S3 client, so `patch.object(_mod,
# "s3_client", ...)` no longer covers it and the handler would otherwise make a
# REAL `get_object` call from a unit test. Imported HERE, before the
# `patch.dict("sys.modules", ...)` block: that context manager restores the dict
# on exit and would drop a module first imported inside it, so importing after
# the block would bind a SECOND instance whose `_s3_client` is not the one
# `lambda_function` calls.
import target_schema_resolution as _tsr

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


_SETTINGS = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def _runner() -> CodeInterpreterTestRunner:
    """A runner whose bedrock-agentcore client is a stub but non-None.

    A ``None`` client short-circuits ``run_test`` to ``_empty_results`` before
    any conversion happens, which would silently skip what these tests measure.
    """
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        runner = CodeInterpreterTestRunner(region="us-east-1")
    assert runner.client is not None, "guard: a None client takes the _empty_results path instead"
    return runner


def _verified(checks: list[dict]) -> list[dict]:
    """The partition ``aggregate_check_results:346`` derives ``test_status`` from."""
    return [c for c in checks if not _mod.is_not_verified_check(c)]


def _parsed(verdict: str, categories: dict[str, bool], issues: list | None = None) -> dict:
    """A ``parse_test_report``-shaped dict: structural data only (P28 R10.6)."""
    return {
        "verdict": verdict,
        "issues": issues if issues is not None else ([] if verdict == "pass" else [{"severity": "major"}]),
        "test_categories": {
            name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in categories.items()
        },
    }


# ---------------------------------------------------------------------------
# 3.2 — a proxy refusal still emits `not_verified_by_proxy` in place of
#       `code_interpreter_execution`, and stays partitioned out of the
#       derivation at lambda_function.py:346 while staying in `checks`.
# ---------------------------------------------------------------------------

#: Constructs the shipped ``_PERMANENTLY_INEXPRESSIBLE_CONSTRUCTS`` frozenset
#: (``code_interpreter_runner.py:164-186``) carries, and constructs it does not.
#: OBSERVED on unfixed code: ``groupBy``/``join``/``Window``/``over``/
#: ``row_number`` classify ``permanently_inexpressible``; ``explode``,
#: ``posexplode`` and ``pivot`` classify ``not_yet_expressible``.
#:
#: p41 task 9.3 touched this block for two reasons and amended **no assertion**:
#: the citation above read ``:67-90``, stale twice over (task 9.2's comment added
#: 27 lines); and ``initcap`` joins ``_NOT_YET_CONSTRUCTS``. ``initcap`` was never
#: in ``_PERMANENT_CONSTRUCTS`` — this file's lists are hardcoded and it was
#: absent — so nothing needed removing. It is added on the other side because p41
#: 2.7 established that reason as its true one: ``initcap`` is ``.str.title()`` —
#: single-frame and elementwise — so "no pandas equivalent exists" was false; it
#: is unimplemented, not inexpressible. That name is stamped p41, not OBSERVED
#: pre-fix like the five beside it, and the test derives the expected reason from
#: the shipped ``classify_conversion_refusal`` rather than from either list, so
#: adding it pins the new truth without transcribing it (``LGR-28b4d75-5``).
_PERMANENT_CONSTRUCTS = ["groupBy", "agg", "join", "crossJoin", "union", "Window", "over", "row_number", "lag"]
_NOT_YET_CONSTRUCTS = ["explode", "posexplode", "pivot", "some_unknown_udf", "sampleBy", "initcap"]


@st.composite
def proxy_refusals(draw):
    """A refusal drawn from BOTH reason classes, with a construct and a line.

    Drawing from both lists is deliberate: requirement 3.2 requires the reason
    to survive, and ``classify_conversion_refusal``
    (``code_interpreter_runner.py:189-200``) is the only thing that distinguishes
    them. Generating one class would let a regression that collapsed the two
    pass. (p41 task 9.3: the citation read ``:93-104``, stale by the same drift
    as the one above the two lists.)
    """
    construct = draw(st.sampled_from(_PERMANENT_CONSTRUCTS + _NOT_YET_CONSTRUCTS))
    line_number = draw(st.integers(min_value=1, max_value=400))
    source_line = draw(st.sampled_from([f"df_transformed = df.{construct}(x)", f'df.withColumn("c", {construct}())']))
    return construct, source_line, line_number


@_SETTINGS
@given(proxy_refusals(), st.booleans(), st.booleans())
def test_proxy_refusal_still_emits_not_verified_by_proxy_and_stays_partitioned(refusal, verdict_ok, static_ok):
    """3.2 — the refusal shape and its partitioning are unchanged.

    OBSERVED on unfixed code, for construct ``groupBy`` on line 4 with a passing
    review::

        check names       ['ai_review_verdict', 'static_analysis:Correctness',
                           'not_verified_by_proxy']
        nv entry keys     ['detail', 'name', 'not_verified_reason', 'passed',
                           'verified']
        nv passed         False
        nv verified       False
        nv reason         permanently_inexpressible
        nv detail         "NOT VERIFIED by the pandas proxy: construct 'groupBy'
                           on line 4 was not converted, so the transform never
                           ran. Reason (permanently_inexpressible): no
                           single-frame pandas equivalent exists, ..."
        partitioned out   ['ai_review_verdict', 'static_analysis:Correctness']

    No ``code_interpreter_execution`` entry appears alongside it — the
    substitution, not an addition. That is what "in place of" means, and it is
    the p32 shape D2 reuses rather than replacing.

    Validates: Requirements 3.2
    """
    construct, source_line, line_number = refusal
    runner = _runner()
    execution_results = runner._conversion_failure_results(ConversionError(construct, source_line, line_number))

    parsed = _parsed("pass" if verdict_ok else "fail", {"Correctness": static_ok})
    status, checks = _mod.aggregate_check_results(parsed, execution_results)

    names = [c["name"] for c in checks]
    assert _mod.NOT_VERIFIED_CHECK_NAME in names, f"refusal for {construct!r} emitted no not-verified entry: {names}"
    # "in place of", not "in addition to" — 3.2's substitution clause.
    assert "code_interpreter_execution" not in names, (
        f"a proxy refusal emitted BOTH {_mod.NOT_VERIFIED_CHECK_NAME} and code_interpreter_execution: {names}. "
        "3.2 requires the refusal to REPLACE the execution check, not accompany it."
    )
    assert names.count(_mod.NOT_VERIFIED_CHECK_NAME) == 1, f"more than one not-verified entry: {names}"

    entry = next(c for c in checks if c["name"] == _mod.NOT_VERIFIED_CHECK_NAME)
    assert entry["verified"] is False, entry
    assert entry["passed"] is False, entry
    # The reason survives, and it is the one the shipped classifier assigns —
    # not a constant, so a change that collapsed the two reasons fails here.
    expected_reason = classify_conversion_refusal(construct)
    assert expected_reason in (NOT_VERIFIED_REASON_PERMANENT, NOT_VERIFIED_REASON_NOT_YET)
    assert entry["not_verified_reason"] == expected_reason, entry

    # 3.2's text clause: the construct, the line and the reason are all present
    # AS TEXT in the detail — LGR-99050e6-1's house pattern, colour alone is
    # invisible to a screen reader and fails WCAG 1.4.1.
    assert construct in entry["detail"], entry["detail"]
    assert str(line_number) in entry["detail"], entry["detail"]
    assert expected_reason in entry["detail"], entry["detail"]

    # Partitioned OUT of the derivation (`:346`) but still IN `checks` (`:348`).
    assert _mod.is_not_verified_check(entry) is True
    assert entry not in _verified(checks)
    assert entry in checks

    # A refusal alone is not evidence of failure: with every verified check
    # passing, the status is "passed" despite the refusal being present.
    if verdict_ok and static_ok:
        assert status == "passed", (status, checks)


# ---------------------------------------------------------------------------
# 3.3 — any verified check that fails derives `failed`. No majority heuristic,
#       no execution-success override (LGR-334667e-1's removal, preserved).
# ---------------------------------------------------------------------------

_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}


@st.composite
def check_matrices(draw):
    """A verdict, N static categories and an execution outcome, at least one failing.

    Constrained so the antecedent of 3.3 actually holds — a matrix in which
    everything passes would make the test vacuous. The category count reaches 8
    so the anti-majority case (1 failure out of many) is generated, which is the
    heuristic 3.3 forbids.
    """
    n = draw(st.integers(min_value=1, max_value=8))
    names = [f"Category{i}" for i in range(n)]
    outcomes = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    verdict_ok = draw(st.booleans())
    exec_ok = draw(st.booleans())
    # Force at least one verified failure.
    if verdict_ok and exec_ok and all(outcomes):
        if outcomes:
            outcomes[draw(st.integers(min_value=0, max_value=n - 1))] = False
        else:  # pragma: no cover - n >= 1 makes this unreachable
            exec_ok = False
    return dict(zip(names, outcomes, strict=True)), verdict_ok, exec_ok


@_SETTINGS
@given(check_matrices())
def test_a_failing_verified_check_always_derives_failed(matrix):
    """3.3 — one failing verified check is enough, whatever the rest do.

    OBSERVED on unfixed code::

        1 of 5 static fails, exec OK   -> failed  (7 checks, 7 verified)
        all static pass, exec fails    -> failed  (4 checks, 4 verified)
        verdict fail, everything else  -> failed  (4 checks, 4 verified)
        all pass                       -> passed  (4 checks, 4 verified)

    The first line is the anti-majority case and the second is the
    anti-override case: a successful execution does not rescue a static failure.
    That override was removed deliberately (``LGR-334667e-1``, backfilled:
    "Execution success no longer overrides blocking static failures") and 3.3
    preserves the removal.

    Validates: Requirements 3.3
    """
    categories, verdict_ok, exec_ok = matrix
    parsed = _parsed("pass" if verdict_ok else "fail", categories)
    execution_results = _EXEC_OK if exec_ok else {"execution_successful": False, "error_details": "IndentationError"}

    status, checks = _mod.aggregate_check_results(parsed, execution_results)
    verified = _verified(checks)

    failing = [c["name"] for c in verified if not c["passed"]]
    assert failing, "guard: the generator must produce at least one failing verified check"
    assert status == "failed", (
        f"status is {status!r} with {len(failing)} of {len(verified)} verified checks failing ({failing}). "
        "3.3 forbids a majority heuristic and forbids execution success overriding a static failure."
    )

    # No entry carries the not-verified discriminator on this path, so the
    # partition is the identity — pinned so a future change cannot start
    # quietly excluding a genuine failure from the derivation.
    assert len(verified) == len(checks), checks


@_SETTINGS
@given(st.integers(min_value=1, max_value=8))
def test_all_verified_checks_passing_still_derives_passed(n_categories):
    """3.3's converse — the derivation is not one-sided.

    Without this, a change that hardcoded ``failed`` would satisfy the test
    above. OBSERVED on unfixed code: all-pass yields ``passed``.

    Validates: Requirements 3.3
    """
    parsed = _parsed("pass", {f"Category{i}": True for i in range(n_categories)})
    status, checks = _mod.aggregate_check_results(parsed, _EXEC_OK)
    assert status == "passed", (status, checks)
    assert all(c["passed"] for c in _verified(checks))


# ---------------------------------------------------------------------------
# 3.4 — a run that verifies nothing still derives `failed`.
# ---------------------------------------------------------------------------


@_SETTINGS
@given(proxy_refusals())
def test_a_run_that_verifies_nothing_still_derives_failed(refusal):
    """3.4 — an all-unverified check list is ``failed``, not ``passed``.

    OBSERVED on unfixed code, with ``parsed_results={}`` and a refusal::

        status           failed
        checks           ['not_verified_by_proxy']
        verified_checks  []          (len 0)

    ``:347`` reads ``verified_checks and all(...)``, so the empty partition
    falsifies the conjunction. A run that proved nothing must not report
    "passed" — the same posture as a missing execution (p32 3.22).

    This is reachable only by a direct unit call with an empty
    ``parsed_results``; the handler path always emits ``ai_review_verdict``,
    which is what :func:`test_the_d2_fence_verified_checks_is_never_empty`
    pins separately.

    Validates: Requirements 3.4
    """
    construct, source_line, line_number = refusal
    runner = _runner()
    execution_results = runner._conversion_failure_results(ConversionError(construct, source_line, line_number))

    status, checks = _mod.aggregate_check_results({}, execution_results)

    assert _verified(checks) == [], f"expected an empty verified partition, got {_verified(checks)}"
    assert checks, "3.4 must not be implemented by emitting nothing — the entry stays in the list (C4)"
    assert status == "failed", f"a run that verified nothing reported {status!r}"


# ---------------------------------------------------------------------------
# 3.5 — the validation code binds `df_transformed` only, and raises with an
#       explicit reported error when it is absent
#       (code_interpreter_runner.py:361-365).
# ---------------------------------------------------------------------------


class _FakeSeries:
    def __init__(self, values):
        self._values = list(values)
        self.dtype = "object"

    def isna(self):
        return _FakeSeries([v is None for v in self._values])

    def sum(self):
        return sum(1 for v in self._values if v is True)


class _FakeDataFrame:
    """Just enough pandas surface for the generated validation code.

    Local rather than imported from ``test_code_interpreter_runner``: importing
    one test module from another couples their ``sys.path`` setup.
    """

    def __init__(self, data: dict):
        self._data = {k: list(v) for k, v in data.items()}
        self.columns = list(self._data.keys())

    def __len__(self):
        return len(next(iter(self._data.values()), []))

    def __getitem__(self, col):
        return _FakeSeries(self._data[col])

    def duplicated(self):
        rows = list(zip(*self._data.values(), strict=True))
        seen: set = set()
        flags = []
        for row in rows:
            flags.append(row in seen)
            seen.add(row)
        return _FakeSeries(flags)


_COLUMN_NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12)


@_SETTINGS
@given(
    st.lists(_COLUMN_NAMES, min_size=1, max_size=6, unique=True),
    st.lists(_COLUMN_NAMES, min_size=1, max_size=6, unique=True),
)
def test_validation_code_raises_when_df_transformed_is_absent(target_columns, raw_columns):
    """3.5 — no fallback to the raw ``df``, and the error is reported not swallowed.

    OBSERVED on unfixed code, for a one-field target schema:

    * ``_build_validation_code`` mentions ``df_transformed`` 3 times and never
      binds ``_df`` to a bare ``df``.
    * With no ``df_transformed`` in scope, executing the generated code prints
      ``VALIDATION:{"error": "transform did not produce df_transformed"}`` and
      raises ``RuntimeError: transform did not produce df_transformed``.
    * With the raw ``df`` in scope but ``df_transformed`` still absent, the
      outcome is **identical** — the raw frame is not silently validated.

    That second case is the one that matters. Validating the input instead of the
    output is validation theater, and the raw ``df`` is always in scope inside
    the interpreter (``_run_lifecycle:175`` creates it), so an accidental
    fallback would be invisible rather than loud.

    Validates: Requirements 3.5
    """
    runner = _runner()
    code = runner._build_validation_code({"properties": {c: {"type": "string"} for c in target_columns}})

    assert "_df = df_transformed" in code, code
    for banned in ("_df = df\n", "_df = df ", "or df\n", "else df\n"):
        assert banned not in code, f"found a raw-df fallback {banned!r} in the generated validation code"

    for label, namespace_extra in (
        ("nothing else in scope", {}),
        ("raw df in scope", {"df": _FakeDataFrame({c: ["x"] * 4 for c in raw_columns})}),
    ):
        printed: list[str] = []
        # `sink=printed` binds this iteration's list rather than closing over the
        # loop variable, so the two cases cannot cross-contaminate.
        namespace = {
            "print": lambda *a, sink=printed: sink.append(" ".join(str(x) for x in a)),
            **namespace_extra,
        }
        with pytest.raises(RuntimeError, match="transform did not produce df_transformed"):
            exec(code, namespace)
        reported = [line for line in printed if line.startswith("VALIDATION:")]
        assert reported, f"[{label}] the absent-frame path printed no VALIDATION line: {printed}"
        payload = json.loads(reported[0][len("VALIDATION:") :])
        assert payload == {"error": "transform did not produce df_transformed"}, (label, payload)


@_SETTINGS
@given(st.lists(_COLUMN_NAMES, min_size=1, max_size=6, unique=True))
def test_validation_code_binds_the_transformed_frame_when_present(columns):
    """3.5's positive half — with ``df_transformed`` bound, the transformed frame is measured.

    OBSERVED: the ``VALIDATION:`` payload's ``null_counts`` / ``dtypes`` keys are
    the TRANSFORMED frame's columns. Pinned so a change cannot start reporting
    the raw frame's shape while still raising correctly on the absent case.

    Requirement 3.1 rides along: when the transform produces every declared
    column, ``schema_compliance`` is ``100.0`` with an empty ``missing_columns``.

    Validates: Requirements 3.1, 3.5
    """
    runner = _runner()
    code = runner._build_validation_code({"properties": {c: {"type": "string"} for c in columns}})

    transformed = _FakeDataFrame({c: [f"v{i}" for i in range(4)] for c in columns})
    raw = _FakeDataFrame({"raw_only": ["x"] * 4})
    printed: list[str] = []
    namespace = {
        "print": lambda *a: printed.append(" ".join(str(x) for x in a)),
        "df": raw,
        "df_transformed": transformed,
    }
    exec(code, namespace)

    payload = json.loads(next(line for line in printed if line.startswith("VALIDATION:"))[len("VALIDATION:") :])
    assert sorted(payload["null_counts"]) == sorted(columns), payload
    assert sorted(payload["dtypes"]) == sorted(columns), payload
    assert "raw_only" not in payload["null_counts"], "the RAW frame was measured — validation theater"
    # 3.1: every declared column present -> a full score and no missing columns.
    assert payload["schema_compliance"] == 100.0, payload
    assert payload["missing_columns"] == [], payload


# ---------------------------------------------------------------------------
# The D2 fence — for any handler-reachable input, `verified_checks` is non-empty.
# ---------------------------------------------------------------------------

_PIPELINE_ID = "pipe-1"
_MAPPING_ID = "map-1"
_CODE_BUCKET = "code-bucket"
_CODE_KEY = "code.py"
_SCHEMA_BUCKET = "adi-target-schemas-123456789012-us-east-1"
_SCHEMA_KEY = f"{_PIPELINE_ID}/target-schema.json"

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": f"s3://{_CODE_BUCKET}/{_CODE_KEY}"},
}

#: The schema-mappings record as the live table really shapes it: hashes only,
#: no `target_schema` and no `source_schema`. The absence of those attributes is
#: the bug the fix addresses; here it is the input the fence must survive.
_MAPPING_ITEM = {
    "mapping_id": _MAPPING_ID,
    "pipeline_id": _PIPELINE_ID,
    "source_schema_hash": "3f1a" * 16,
    "target_schema_hash": "9c2b" * 16,
    "mapping_config": {"field_mappings": []},
    "status": "approved",
}

#: The three target-schema states the fence has to hold across, wired through the
#: pipelines table and S3 so the axis is LIVE once the fix reads from there
#: (D3/D5) rather than being decoration.
#:
#: MEASURED on unfixed code, per state, by recording which stubs the handler
#: actually touches: ``dynamodb.Table(...)`` is called for
#: ``agentic_data_ingestion_schema_mappings`` and nothing else,
#: ``pipelines_table.get_item`` is called ZERO times, the only S3 read is
#: ``code-bucket/code.py``, and the ``target_schema`` handed to ``run_test`` is
#: ``{}`` for all three states — including ``present``, where S3 holds a real
#: two-field schema. That is the defect stated from the preservation side: the
#: canonical location is never consulted, so the state cannot matter yet. After
#: task 4.3 it will, and the fence has to keep holding.
_TARGET_SCHEMA_STATES = {
    "present": {
        "pipeline_id": _PIPELINE_ID,
        "target_schema_bucket": _SCHEMA_BUCKET,
        "target_schema_s3_key": _SCHEMA_KEY,
    },
    "absent": {"pipeline_id": _PIPELINE_ID},
    "key_without_bucket": {"pipeline_id": _PIPELINE_ID, "target_schema_s3_key": _SCHEMA_KEY},
}

_TARGET_SCHEMA_OBJECT = {
    "type": "object",
    "properties": {"order_key": {"type": "string"}, "total_amount": {"type": "number"}},
}


def _run_handler(review_text: str, execution_results: dict | None, target_schema_state: str = "absent") -> dict:
    """Drive ``lambda_handler`` with every AWS/agent dependency stubbed.

    Modelled on ``test_honest_aggregation.py::_run_handler``. moto cannot express
    what this needs — a Code Interpreter client to inject and a review agent to
    script — so the module-attribute patch harness is the pattern.

    ``target_schema_state`` shapes the pipelines record and what S3 holds at the
    canonical location, so the third axis of the D2 fence is a real input rather
    than an unused parameter.
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}

    mappings_table = MagicMock()
    mappings_table.get_item.return_value = {"Item": dict(_MAPPING_ITEM)}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": dict(_TARGET_SCHEMA_STATES[target_schema_state])}

    def _table(name):
        if "mapping" in name:
            return mappings_table
        if "pipeline" in name:
            return pipelines_table
        return MagicMock()

    fake_dynamodb = MagicMock()
    fake_dynamodb.Table.side_effect = _table

    s3 = MagicMock()

    def _get_object(Bucket, Key, **_kw):
        if (Bucket, Key) == (_CODE_BUCKET, _CODE_KEY):
            return {"Body": MagicMock(read=MagicMock(return_value=b"df_transformed = df"))}
        if (Bucket, Key) == (_SCHEMA_BUCKET, _SCHEMA_KEY):
            body = json.dumps(_TARGET_SCHEMA_OBJECT).encode()
            return {"Body": MagicMock(read=MagicMock(return_value=body))}
        raise AssertionError(f"unexpected S3 read: s3://{Bucket}/{Key}")

    s3.get_object.side_effect = _get_object

    agent = MagicMock(return_value=review_text)
    agent.add_hook = MagicMock()
    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        # The resolver's own seam (p33 task 4.3) — the same mock, so the
        # `unexpected S3 read` assertion in `_get_object` still covers the
        # target-schema read after the delegation.
        patch.object(_tsr, "_s3_client", lambda: s3),
        patch.object(_mod, "dynamodb", fake_dynamodb),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        return _mod.lambda_handler({"run_id": "run-1", "pipeline_id": _PIPELINE_ID, "mapping_id": _MAPPING_ID}, None)


@st.composite
def handler_reachable_states(draw):
    """A ``(review_verdict, execution_results, target_schema_state)`` triple.

    The three axes of design D2's argument:

    * **review verdict** — pass/fail, with and without ``categories``. The
      no-categories shape is the weakest case: it emits exactly ONE check
      (``ai_review_verdict``), so if the fence holds there it holds everywhere.
    * **execution outcome** — the four branches of the total ``if/elif/elif/else``
      in ``aggregate_check_results`` (now ``lambda_function.py:485-611``):
      ``None``, a proxy refusal, success, and failure; plus the two other shapes
      ``run_test`` can return (``_empty_results``, ``_failed_execution_results``).
    * **target-schema state** — present / absent / half-written. Today this axis
      changes nothing (the read is vacuous, which IS the bug), so it is carried
      as an input the fix must not break rather than as an observable.

    WIDENED by p40 task 12.3 (R4/R9), amended here rather than forked into a
    second generator: a second one would be a second place for the fence's
    assertions to drift (``LGR-b2f79ef-6``). The execution axis gains
    ``scalar_literal_refusal`` — a proxy refusal carrying p40 R4's THIRD reason,
    ``scalar_literal_receiver``, from the converter's receiver-shape fence.

    Why this reason specifically, and why it is not decoration:
    ``LGR-b2f79ef-2`` (template / ratified) states the caveat this whole property
    exists for — *"``:347`` derives ``failed`` when nothing was verified, so a
    ``verified: False`` entry must never be the **only** check"* (the derivation
    it names now lives at ``lambda_function.py:677``). Under p40's ratified arm
    the proxy refuses MORE often than before, so ``scalar_literal_receiver`` is
    about to be the most common not-verified reason on this path — the one least
    likely to be rare, and therefore the one the fence most needs exercising on.
    Before this widening the fence was only ever driven over ``groupBy``, whose
    reason is ``permanently_inexpressible``.
    """
    verdict_ok = draw(st.booleans())
    with_categories = draw(st.booleans())
    categories = {"Correctness": "pass", "Security": "pass" if verdict_ok else "fail"} if with_categories else None
    payload: dict = {
        "verdict": "pass" if verdict_ok else "fail",
        "issues": [] if verdict_ok else [{"severity": "major", "category": "Security", "description": "x"}],
    }
    if categories is not None:
        payload["categories"] = categories

    exec_kind = draw(
        st.sampled_from(
            [
                "ok",
                "exec_failed",
                "none",
                "refusal",
                # p40 task 12.3 — the third proxy reason, added to the SAME
                # sampled_from rather than to a second generator.
                "scalar_literal_refusal",
                "empty_results",
                "failed_execution_results",
            ]
        )
    )
    target_schema_state = draw(st.sampled_from(["present", "absent", "key_without_bucket"]))
    return json.dumps(payload), exec_kind, target_schema_state


def _execution_results_for(kind: str) -> dict | None:
    runner = _runner()
    if kind == "ok":
        return dict(_EXEC_OK)
    if kind == "exec_failed":
        return {"execution_successful": False, "error_details": "KeyError: 'city'"}
    if kind == "none":
        return None
    if kind == "refusal":
        return runner._conversion_failure_results(ConversionError("groupBy", 'df.groupBy("a")', 4))
    if kind == "scalar_literal_refusal":
        # p40 task 12.3 / R4. Built through the REAL `_conversion_failure_results`,
        # like every other kind here, so the reason travels the shipped precedence
        # (`exc.reason or classify_conversion_refusal(exc.construct)`,
        # `code_interpreter_runner.py:650`) rather than being hand-assembled into
        # the results dict.
        #
        # `construct="cast"` is deliberate and is the discriminating choice:
        # `classify_conversion_refusal` keys on the construct NAME, and `cast` is
        # a name a plain not-yet-expressible refusal also uses, so if the
        # precedence ever regressed to construct-first this kind would silently
        # become `not_yet_expressible` — which the guard in the property below
        # catches rather than tolerates.
        return runner._conversion_failure_results(
            ConversionError(
                "cast",
                'df_transformed = df.withColumn("mobile_phone", lit(None).cast("string"))',
                12,
                reason=REFUSAL_REASON_SCALAR_LITERAL,
            )
        )
    if kind == "empty_results":
        return runner._empty_results("bedrock-agentcore client not available")
    if kind == "failed_execution_results":
        return runner._failed_execution_results("transform execution", {"stderr": "Traceback", "exit_code": 1})
    raise AssertionError(f"unknown execution kind {kind!r}")


#: The weakest handler-reachable input: a verdict with NO categories (one check)
#: and a proxy refusal (that one check is the only verified entry). Pinned as an
#: explicit example so the fence's floor is exercised on every seed, not only
#: when the generator happens to draw it.
_FENCE_FLOOR = (json.dumps({"verdict": "pass", "issues": []}), "refusal", "absent")

#: The SAME floor under p40 R4's third reason (task 12.3).
#:
#: MEASURED rather than asserted, by instrumenting this module's own generator
#: over `max_examples=100` (p40 task 8.3's discipline, which found 2.10% in
#: `test_not_verified_advisory_prop.py` and would not claim coverage on it):
#:
#:   * `scalar_literal_refusal` is **14.29%** of draws (12 of 84);
#:   * the FLOOR shape — no categories AND the new kind AND a target-schema
#:     state that reaches aggregation — is **4.76%** (4 of 84).
#:
#: The 84 is the finding worth writing down. This generator's space is
#: 2 (verdict) x 2 (categories) x 7 (exec kind) x 3 (schema state) = **84
#: distinct tuples**, so at `max_examples=100` Hypothesis EXHAUSTS it and stops:
#: every input, including this floor, is covered on every run, and the two
#: percentages above are exact rather than sampled. At `_SETTINGS`' 25 it drew 25
#: of the (then 72) points and the new kind could have been missed entirely —
#: which is a second, independent reason the 100 in `_FENCE_SETTINGS` is load
#: bearing and not just compliance with task 12's floor. (That last sentence is
#: arithmetic — 25 < 72 — not a second measurement.)
#:
#: So this `@example` is belt-and-braces TODAY, and it is kept deliberately:
#: exhaustion is a property of the space being 84 wide, and it stops holding the
#: moment anyone adds an axis or a seventh execution kind. `_FENCE_FLOOR` above
#: is pinned for exactly that reason and this follows it.
_SCALAR_LITERAL_FENCE_FLOOR = (json.dumps({"verdict": "pass", "issues": []}), "scalar_literal_refusal", "absent")

#: AMENDED IN PLACE by p40 task 12.3, not forked (`LGR-60c4985-6`,
#: `LGR-b2f79ef-6` — both template): the D2 fence gets its own settings so
#: `max_examples` can reach task 12's floor of 100 without moving the other
#: nine properties in this file off `_SETTINGS`' 25. 25 was below that floor and
#: is the reason this is a change rather than a no-op. Affordable: the fence ran
#: at 0.09 s for 25 examples before this task, so 100 costs well under a second
#: even though every example drives the whole `lambda_handler`.
_FENCE_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


@_FENCE_SETTINGS
@example(_FENCE_FLOOR)
@example(_SCALAR_LITERAL_FENCE_FLOOR)
@example(
    (
        # `review_verdict._validate_verdict_document:201` rejects an issue with
        # no non-empty `description`, so a hand-written example must carry one.
        json.dumps({"verdict": "fail", "issues": [{"severity": "major", "description": "x"}]}),
        "refusal",
        "present",
    )
)
@example((json.dumps({"verdict": "pass", "issues": []}), "none", "key_without_bucket"))
@given(handler_reachable_states())
def test_the_d2_fence_verified_checks_is_never_empty(state):
    """The D2 fence — no handler-reachable input yields an all-unverified list.

    This is the property that keeps a ``verified: False`` ``schema_compliance``
    entry from ever being the sole determinant of ``test_status``. ``:347``
    derives ``failed`` when nothing verified, so an empty verified partition
    would make "we could not check" read as "the run failed" — the opposite of
    requirement 2.6's intent.

    Design D2 argues the fence holds because ``ai_review_verdict`` is always
    emitted on the handler path (``verdict_to_parsed_results`` at ``:1140`` runs
    only after ``parse_review_verdict`` succeeded; a parse failure raises
    upstream at ``:1116-1125``) and because the execution branch at ``:275-339``
    is a total ``if/elif/elif/else`` that always appends exactly one entry. This
    test turns that argument into a check, so it stays true rather than merely
    being true today.

    OBSERVED on unfixed code across the full 3 x 5 product driven through
    ``lambda_handler``::

        verdict=pass          exec=ok       -> passed  3 checks, 3 verified
        verdict=pass          exec=refusal  -> passed  3 checks, 2 verified
        verdict=fail          exec=refusal  -> failed  4 checks, 3 verified
        verdict=no-categories exec=refusal  -> passed  2 checks, 1 verified   <- the floor
        verdict=no-categories exec=None     -> failed  2 checks, 2 verified

    The floor is 1, and the entry holding it up is ``ai_review_verdict``.

    Amended by p33 task 4.3: two of the three target-schema states still reach
    aggregation and the table above still describes them. The third,
    ``key_without_bucket``, now fails the step before aggregation (requirement
    2.7) and is asserted as such below.

    AMENDED by p40 task 12.3 (R4/R9) — the claim is unchanged; the input space
    widened. The generator now also draws ``scalar_literal_refusal``, R4's third
    proxy reason, and ``max_examples`` moved 25 -> 100 to reach p40 task 12's
    floor. ``LGR-b2f79ef-2`` is the record this property was built for, and its
    caveat is why the new reason has to arrive here rather than only in the
    classification tests: under p40's ratified arm the proxy refuses more often,
    so ``scalar_literal_receiver`` becomes the most common ``verified: False``
    entry on this path, and a ``verified: False`` entry must never be the only
    check. OBSERVED for the new kind, at the floor (no categories, so
    ``ai_review_verdict`` is the only verified entry)::

        verdict=no-categories exec=scalar_literal_refusal
            -> passed  2 checks, 1 verified
            checks     ['ai_review_verdict', 'not_verified_by_proxy']
            nv reason  scalar_literal_receiver

    The floor is still 1 and the entry holding it up is still
    ``ai_review_verdict`` — the new reason does not lower it, which is the fact
    this widening turns from an inference into a check.

    Validates: Requirements 3.2, 3.3, 3.4, p40 R4, p40 R9
    """
    review_text, exec_kind, target_schema_state = state
    execution_results = _execution_results_for(exec_kind)

    if target_schema_state == "key_without_bucket":
        # AMENDED by p33 task 4.3 (C11 — amended at the assertion, not deleted).
        # This state used to reach aggregation: the read was aimed at the
        # mappings record, so a half-written PIPELINE record changed nothing and
        # the fence held over it vacuously. Task 4.3 aims the read at the
        # pipeline record, and requirement 2.7 says a `target_schema_s3_key`
        # with no `target_schema_bucket` must FAIL THE STEP naming both
        # attributes rather than be treated as an absent schema. So this input
        # no longer produces a check list at all, and asserting the fence over
        # it would be asserting that a half-written record is tolerated —
        # the opposite of 2.7.
        #
        # The state stays in the generator rather than being dropped: it is
        # still handler-reachable, and what it reaches is now pinned here. The
        # fence itself (verified_checks never empty) is unaffected — it is a
        # property of aggregation, and this input never gets there.
        with pytest.raises(_mod.StepFailure) as excinfo:
            _run_handler(review_text, execution_results, target_schema_state)
        message = str(excinfo.value)
        assert "target_schema_s3_key" in message, message
        assert "target_schema_bucket" in message, message
        return

    response = _run_handler(review_text, execution_results, target_schema_state)
    checks = response["test_result"]["checks"]
    verified = _verified(checks)

    assert verified, (
        f"verified_checks is EMPTY for a handler-reachable input "
        f"(exec={exec_kind!r}, target_schema={target_schema_state!r}, review={review_text}). "
        f"The D2 fence is breached: a verified: False entry could now be the sole "
        f"determinant of test_status. checks={checks}"
    )
    # The entry the fence rests on, named so a regression says why it broke.
    assert any(c["name"] == "ai_review_verdict" for c in verified), (
        f"ai_review_verdict is absent from the verified partition — D2's argument rests on it "
        f"always being emitted on the handler path. checks={checks}"
    )
    # The derivation is consistent with the partition, not with the raw list.
    expected_status = "passed" if all(c["passed"] for c in verified) else "failed"
    assert response["test_status"] == expected_status, (response["test_status"], checks)

    if exec_kind == "scalar_literal_refusal":
        # p40 task 12.3's guard, and the reason the widening is load-bearing
        # rather than a longer list. Without it this kind could reach the fence
        # as an ordinary `not_yet_expressible` refusal and the property would
        # pass while exercising nothing new — the dead-arm failure mode
        # `lambda_function.py:245-252` measured for the imported-constant
        # spelling, and the shape `LGR-28b4d75-5` warns about.
        #
        # Asserted against the CONSUMER's constant while `_execution_results_for`
        # built the input from the PRODUCER's, so a drift between the two
        # deliberate spellings fails here too.
        nv = [c for c in checks if c["name"] == _mod.NOT_VERIFIED_CHECK_NAME]
        assert len(nv) == 1, f"expected exactly one proxy non-verification, got {nv}"
        assert nv[0]["not_verified_reason"] == _mod.SCALAR_LITERAL_RECEIVER_REASON, (
            f"the receiver-shape refusal reached aggregation as "
            f"{nv[0]['not_verified_reason']!r}. The construct is 'cast', which "
            f"`classify_conversion_refusal` also maps to a p32 reason, so this says the "
            f"`exc.reason or classify_conversion_refusal(...)` precedence regressed to "
            f"construct-first. entry={nv[0]}"
        )
        # The load the fence is under: this entry IS partitioned out, so it is
        # capable of being the only check — which is exactly `LGR-b2f79ef-2`'s
        # caveat and the reason the fence has to hold over this reason too.
        assert _mod.is_not_verified_check(nv[0]) is True, nv[0]
        assert nv[0] not in verified, (nv[0], verified)


# ---------------------------------------------------------------------------
# 3.14 — narrow-schema frame byte-identity.
#
# The frozen baseline below was CAPTURED from the unfixed
# `_build_test_data_code` (`code_interpreter_runner.py:315-344`) and is asserted
# character for character. It is deliberately brittle: 3.14 says the narrow path
# must stay BYTE-identical, so a formatting-only change to the builder is itself
# a 3.14 violation and must break this test rather than slip through. Compare
# the width case in `test_test_frame_width_prop.py`, which asserts only that
# every declared field appears — the >20 path is allowed to change shape.
# ---------------------------------------------------------------------------

#: The literal preamble/postamble of `_build_test_data_code`'s f-string
#: (`:332-344`), captured verbatim from unfixed code.
_FRAME_PREAMBLE = "import pandas as pd, numpy as np, json, random\nnum_rows = 100\ndata = {\n"
_FRAME_POSTAMBLE = (
    "\n}\n"
    "df = pd.DataFrame(data)\n"
    "# Inject ~5% nulls\n"
    "for c in df.columns:\n"
    "    mask = [random.random() < 0.05 for _ in range(num_rows)]\n"
    "    df.loc[mask, c] = None\n"
    'print("GENERATED:" + json.dumps({"record_count": len(df), "columns": list(df.columns)}))\n'
)

#: The three type branches at `:324-329`, captured verbatim. The keys are the
#: declared types each branch claims; the values are the expression template.
_INT_TYPES = ("integer", "int", "long", "bigint")
_FLOAT_TYPES = ("float", "double", "decimal", "number")
#: Anything else falls to the string branch at `:328-329` — including a
#: non-dict `meta`, which `:323` coerces to "string".
_OTHER_TYPES = ("string", "date", "timestamp", "boolean", "binary", "struct")


def _expected_column_expression(name: str, declared_type: str) -> str:
    """The exact line `:325` / `:327` / `:329` emits for one field."""
    if declared_type in _INT_TYPES:
        return f'    "{name}": [i for i in range(num_rows)]'
    if declared_type in _FLOAT_TYPES:
        return f'    "{name}": [round(i * 1.1, 2) for i in range(num_rows)]'
    return f'    "{name}": [f"val_{{i}}" for i in range(num_rows)]'


def _expected_frame_code(fields: list[tuple[str, str]]) -> str:
    """Reconstruct `_build_test_data_code`'s output from the frozen baseline."""
    return _FRAME_PREAMBLE + ",\n".join(_expected_column_expression(n, t) for n, t in fields) + _FRAME_POSTAMBLE


_FIELD_NAMES = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_0123456789", min_size=1, max_size=14).filter(
    lambda s: not s.startswith("_")
)
_DECLARED_TYPES = st.sampled_from(list(_INT_TYPES + _FLOAT_TYPES + _OTHER_TYPES))


@st.composite
def narrow_source_schemas(draw):
    """A source schema of 1..20 fields, in BOTH shapes the builder handles.

    ``:317`` reads ``properties`` first and falls back to ``fields``, so both
    shapes must be covered or half the read is unpinned. The type pool spans all
    three branches at ``:324-329`` — an int-only pool would leave the float and
    string expressions unverified.

    Capped at 20 because that is exactly requirement 3.14's domain: 21 or more
    fields is the >20 case, which D12 deliberately changes and which task 1's
    ``test_test_frame_width_prop.py`` owns.
    """
    n = draw(st.integers(min_value=1, max_value=20))
    names = draw(st.lists(_FIELD_NAMES, min_size=n, max_size=n, unique=True))
    types = draw(st.lists(_DECLARED_TYPES, min_size=n, max_size=n))
    shape = draw(st.sampled_from(["properties", "fields"]))
    fields = list(zip(names, types, strict=True))
    schema = {shape: {name: {"type": declared} for name, declared in fields}}
    if shape == "fields":
        # `:317`'s fallback only fires when `properties` is ABSENT — an empty
        # `properties` would win and short-circuit to the {id, value} fallback.
        assert "properties" not in schema
    return schema, fields


@_SETTINGS
@given(narrow_source_schemas())
def test_narrow_schema_frame_code_is_byte_identical(case):
    """3.14 — a <=20-field schema produces character-identical frame code.

    This is what makes D12's blast radius provably confined to the >20 case.
    Without it, "the narrow path is unchanged" is an unread all-clear.

    OBSERVED on unfixed code, for
    ``{"properties": {"i": {"type": "integer"}, "f": {"type": "double"},
    "s": {"type": "string"}}}``::

        import pandas as pd, numpy as np, json, random
        num_rows = 100
        data = {
            "i": [i for i in range(num_rows)],
            "f": [round(i * 1.1, 2) for i in range(num_rows)],
            "s": [f"val_{i}" for i in range(num_rows)]
        }
        df = pd.DataFrame(data)
        # Inject ~5% nulls
        for c in df.columns:
            mask = [random.random() < 0.05 for _ in range(num_rows)]
            df.loc[mask, c] = None
        print("GENERATED:" + json.dumps({"record_count": len(df), "columns": list(df.columns)}))

    The same input under ``{"fields": ...}`` with ``bigint``/``decimal``/``date``
    produced byte-identical output, which is why both shapes are generated
    against one reconstruction.

    The expectation is reconstructed from a FROZEN copy of the template
    (``_FRAME_PREAMBLE`` / ``_FRAME_POSTAMBLE`` / ``_expected_column_expression``),
    not re-derived from the function — a self-comparison would be a tautology.

    Validates: Requirements 3.14
    """
    schema, fields = case
    runner = _runner()
    actual = runner._build_test_data_code(schema)
    expected = _expected_frame_code(fields)

    assert actual == expected, (
        "narrow-schema frame code is not byte-identical to the frozen baseline.\n"
        f"--- expected ---\n{expected!r}\n--- actual ---\n{actual!r}"
    )
    # The four clauses requirement 3.14 spells out, asserted by name so a
    # failure says which one moved rather than just "strings differ".
    assert "num_rows = 100" in actual, "the 100-row count moved"
    assert actual.count('": [') == len(fields), f"expected {len(fields)} column declarations: {actual!r}"
    assert "mask = [random.random() < 0.05 for _ in range(num_rows)]" in actual, "the ~5% null injection moved"
    assert 'print("GENERATED:" + json.dumps(' in actual, "the GENERATED: marker moved"
    # Declaration order follows the schema's insertion order, not sorted order.
    declared_order = [name for name, _ in fields]
    emitted_order = [line.split('"')[1] for line in actual.splitlines() if line.startswith('    "')]
    assert emitted_order == declared_order, (emitted_order, declared_order)


def test_empty_schema_frame_code_is_the_frozen_id_value_fallback():
    """`_build_test_data_code({})`'s `{id, value}` fallback (`:318-319`), pinned.

    OBSERVED on unfixed code, sha256
    ``4795f7027f80641a426e8c38b916adbe07d3f376d27ed425a4a629901061d3f2``::

        data = {
            "id": [f"val_{i}" for i in range(num_rows)],
            "value": [f"val_{i}" for i in range(num_rows)]
        }

    **This function's behaviour on an empty schema is unchanged by D12.** What
    D12 changes is that ``run_test`` refuses BEFORE reaching it (requirement
    2.11), because a two-column ``{id, value}`` frame is not the pipeline's real
    input and a transform exercised against it proves nothing. Those are two
    different facts and both need pinning:

    * this test, plus the existing ``test_handles_empty_schema``
      (``test_code_interpreter_runner.py:568``), pin the BUILDER;
    * the refusal is asserted at the ``run_test`` level by task 4.10 — not by
      changing this test.

    So a fix that makes the builder raise or return something else on ``{}`` is
    a regression, even though the path becomes unreachable through ``run_test``.

    Validates: Requirements 3.14
    """
    runner = _runner()
    actual = runner._build_test_data_code({})
    expected = _expected_frame_code([("id", "string"), ("value", "string")])
    assert actual == expected, f"--- expected ---\n{expected!r}\n--- actual ---\n{actual!r}"


@pytest.mark.parametrize(
    ("schema", "why"),
    [
        ({}, "no key at all"),
        ({"properties": {}}, "empty properties"),
        ({"fields": {}}, "empty fields"),
        ({"properties": {}, "fields": {"a": {"type": "string"}}}, "empty properties wins over populated fields"),
    ],
)
def test_falsy_field_maps_all_reach_the_same_frozen_fallback(schema, why):
    """Every falsy-``fields`` route lands on the same ``{id, value}`` frame.

    OBSERVED on unfixed code: ``:317``'s ``get("properties", get("fields", {}))``
    returns ``properties`` when the key is PRESENT even if empty, so the fourth
    case does NOT fall through to ``fields`` — it takes the ``{id, value}``
    fallback at ``:318-319``. Pinned because it is the input class requirement
    2.11 will start refusing, and a fix that changed which frame this produces
    (rather than refusing before it) would be a silent behaviour change.

    Validates: Requirements 3.14
    """
    runner = _runner()
    expected = _expected_frame_code([("id", "string"), ("value", "string")])
    assert runner._build_test_data_code(schema) == expected, why


@_SETTINGS
@given(st.lists(_FIELD_NAMES, min_size=1, max_size=20, unique=True))
def test_non_dict_field_metadata_falls_to_the_string_branch(names):
    """`:323`'s ``isinstance(meta, dict)`` guard, pinned.

    OBSERVED on unfixed code: ``{"properties": {"a": "string"}}`` — metadata that
    is a bare string rather than a dict — emits the string expression, not a
    crash. This is the third type branch reached by a route the dict-metadata
    generator above cannot produce.

    Validates: Requirements 3.14
    """
    runner = _runner()
    schema = {"properties": {name: "whatever-not-a-dict" for name in names}}
    expected = _expected_frame_code([(name, "string") for name in names])
    assert runner._build_test_data_code(schema) == expected
