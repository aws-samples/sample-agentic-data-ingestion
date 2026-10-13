# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: when ``aggregate_check_results`` emits ``schema_compliance``.

p33 task 4.4 / design D1, D2, C13, C14. The property tests in
``test_schema_compliance_gate_prop.py`` drive this through ``lambda_handler``,
which fixes one target-schema state per case; these tests call
``aggregate_check_results`` directly so the **emission rule itself** is pinned
over the product of *resolved target-schema state* × *execution outcome*.

Why that product needs its own tests rather than being left to the handler-level
properties: the rule has two independent halves, and the handler exercises only
the diagonal.

* The **gate** — an entry is emitted only when the execution produced validation
  output. The handler cases all execute successfully, so nothing there would
  notice a change that started emitting a check on a proxy refusal or on an
  execution failure. That is the C4-shaped mistake this file forbids: reporting
  a comparison for a transform that never ran.
* The **state** — a resolved schema scores the run; ``None`` (requirement 2.6)
  replaces the vacuous 100 with an explicit non-verification; and
  ``TARGET_SCHEMA_UNSPECIFIED`` (a caller that says nothing, which is every
  pre-p33 call site) must not have a not-verified entry synthesised from its
  silence.

The ``not_verified_by_harness`` shape is hand-built here, deliberately: task
4.10 adds ``_frame_truncation_results`` that produces it, and until then this
file **is** the contract that branch has to satisfy. The key names asserted
below (``not_verified_field_count`` / ``not_verified_frame_ceiling``) are the
ones ``lambda_function`` reads.

Validates: Requirements 2.2, 2.6, 2.11, 3.2, 3.3, 3.4, 3.13
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# The runner is the REAL module (it touches no AWS at import), so the refusal
# and failure shapes below are the ones production computes — a drift between
# the runner's result shapes and this emission rule fails here rather than live.
from code_interpreter_runner import CodeInterpreterTestRunner
from pyspark_to_pandas import ConversionError

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


#: A review in which everything passes, so the schema-compliance entry is the
#: only thing that can move `test_status`.
_ALL_STATIC_PASS = {
    "verdict": "pass",
    "issues": [],
    "test_categories": {"Correctness": {"status": "PASSED", "passed": True}},
}

_TARGET_SCHEMA = {"type": "object", "properties": {"order_key": {"type": "string"}}}


def _runner() -> CodeInterpreterTestRunner:
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        return CodeInterpreterTestRunner(region="us-east-1")


def _validated(schema_compliance: float, missing_columns: list[str]) -> dict:
    """The shape ``_parse_results`` builds when a ``VALIDATION:`` payload parsed.

    ``schema_compliance`` and ``missing_columns`` are written together there
    (``code_interpreter_runner.py:437-445``), which is why the presence of the
    former is what "the execution produced validation output" reads.
    """
    return {
        "execution_successful": True,
        "executed": True,
        "exit_code": 0,
        "stderr": "",
        "input_records": 100,
        "output_records": 100,
        "data_quality_results": {
            "completeness_score": 95.0,
            "uniqueness_score": 100.0,
            "schema_compliance": schema_compliance,
            "overall_quality_score": 98.0,
            "null_counts": {"order_key": 5},
            "duplicates": 0,
            "missing_columns": missing_columns,
        },
        "error_details": None,
        "conversion_report": None,
    }


def _named(checks: list[dict], name: str) -> list[dict]:
    return [c for c in checks if c["name"] == name]


def _names(checks: list[dict]) -> list[str]:
    return [c["name"] for c in checks]


# ---------------------------------------------------------------------------
# A resolved target schema + validation output → the computed check
# ---------------------------------------------------------------------------


def test_a_shortfall_is_an_emitted_failing_check_naming_the_columns():
    """Requirements 2.2, 3.13 — the score and the absent columns as TEXT (C5)."""
    status, checks = _mod.aggregate_check_results(
        _ALL_STATIC_PASS, _validated(50.0, ["order_key", "total_amount"]), _TARGET_SCHEMA
    )

    entry = _named(checks, "schema_compliance")
    assert len(entry) == 1, _names(checks)
    assert entry[0]["passed"] is False
    assert "verified" not in entry[0], "a computed shortfall is a VERIFIED failing check, not a non-verification"
    assert "50.0" in entry[0]["detail"]
    assert "order_key" in entry[0]["detail"] and "total_amount" in entry[0]["detail"]
    # It participates in the derivation — that is what makes the gate binding.
    assert status == "failed"


def test_full_compliance_is_an_emitted_passing_check():
    """Requirement 3.1's converse at the check level — the gate is two-sided."""
    status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _validated(100.0, []), _TARGET_SCHEMA)

    entry = _named(checks, "schema_compliance")
    assert len(entry) == 1, _names(checks)
    assert entry[0]["passed"] is True
    assert "100.0" in entry[0]["detail"]
    assert status == "passed"


def test_the_computed_check_is_distinct_from_the_reviewers_schema_compliance():
    """D1's naming note — one is computed, one is an LLM opinion. Both stay."""
    parsed = {
        "verdict": "pass",
        "issues": [],
        "test_categories": {"Schema Compliance": {"status": "PASSED", "passed": True}},
    }
    _status, checks = _mod.aggregate_check_results(parsed, _validated(40.0, ["order_key"]), _TARGET_SCHEMA)

    assert _named(checks, "schema_compliance"), _names(checks)
    assert _named(checks, "static_analysis:Schema Compliance"), _names(checks)
    assert _named(checks, "schema_compliance")[0]["passed"] is False
    assert _named(checks, "static_analysis:Schema Compliance")[0]["passed"] is True


# ---------------------------------------------------------------------------
# No validation output → no entry, whatever the target-schema state
# ---------------------------------------------------------------------------


def test_no_entry_when_the_execution_did_not_run_at_all():
    """``execution_results is None`` — nothing to compare (D1 item 3)."""
    _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, None, _TARGET_SCHEMA)
    assert "schema_compliance" not in _names(checks)
    assert _named(checks, "code_interpreter_execution")[0]["passed"] is False


def test_no_entry_on_a_proxy_refusal_even_with_a_target_schema_resolved():
    """3.2 / C4 — the transform never ran, so it is not measured.

    The refusal shape is the runner's own, so this also pins that a refusal
    carries an EMPTY ``data_quality_results`` — the fact the gate rests on.
    """
    refusal = _runner()._conversion_failure_results(ConversionError("groupBy", 'df.groupBy("a")', 4))
    assert refusal["data_quality_results"] == {}

    status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, refusal, _TARGET_SCHEMA)
    assert "schema_compliance" not in _names(checks), (
        f"a transform the proxy refused was scored for schema compliance: {_names(checks)}"
    )
    assert _mod.NOT_VERIFIED_CHECK_NAME in _names(checks)
    assert status == "passed", "a refusal is not evidence of failure (p32 2.21)"


def test_no_entry_when_the_transform_ran_and_failed():
    """A failed execution produced no validation output (D1 item 3)."""
    failed = _runner()._failed_execution_results("transform execution", {"stderr": "Traceback", "exit_code": 1})
    assert failed["data_quality_results"] == {}

    status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, failed, _TARGET_SCHEMA)
    assert "schema_compliance" not in _names(checks), _names(checks)
    assert status == "failed"


# ---------------------------------------------------------------------------
# Requirement 2.6 — no target schema at all
# ---------------------------------------------------------------------------


def test_a_schema_less_pipeline_replaces_the_vacuous_score_with_a_non_verification():
    """Requirement 2.6 / D2 — "could not check" must not read as "passed".

    The score reaching this call is ``100.0``: with no declared columns the
    expected list is empty and the arithmetic cannot report a shortfall. That
    100 is the defect (1.2/1.7), so the entry states in text that nothing was
    checked instead of reporting it.
    """
    status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _validated(100.0, []), None)

    entry = _named(checks, "schema_compliance")
    assert len(entry) == 1, _names(checks)
    assert entry[0]["verified"] is False
    assert entry[0]["not_verified_reason"] == "no_target_schema"
    assert entry[0]["passed"] is not True
    assert "no target schema" in entry[0]["detail"].lower()
    assert "100" not in entry[0]["detail"], "the vacuous score must not be reported as a number"

    # C3/C4: p32's discriminator, partitioned out of the derivation but kept in
    # the list — so an unverifiable check neither passes nor fails the run.
    assert _mod.is_not_verified_check(entry[0]) is True
    assert entry[0] in checks
    assert status == "passed"
    # The execution itself still reports honestly alongside it.
    assert _named(checks, "code_interpreter_execution")[0]["passed"] is True


def test_the_entry_is_json_serializable_for_the_sfn_payload():
    """The state row and the approval payload carry it (requirement 3.13)."""
    _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _validated(0.0, ["order_key"]), _TARGET_SCHEMA)
    assert json.loads(json.dumps(checks)) == checks


# ---------------------------------------------------------------------------
# A caller that says nothing gets no not-verified entry invented for it
# ---------------------------------------------------------------------------


def test_an_unspecified_target_schema_is_not_treated_as_an_absent_one():
    """``TARGET_SCHEMA_UNSPECIFIED`` — silence is not requirement 2.6's state.

    ``None`` is a meaningful value (the resolver's "this pipeline has no target
    schema"), which is why the default cannot be ``None``: every pre-p33 call
    site passes two arguments, and none of them is asserting that the pipeline
    is schema-less.
    """
    _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _validated(100.0, []))

    entry = _named(checks, "schema_compliance")
    assert len(entry) == 1, _names(checks)
    assert "verified" not in entry[0], f"a not-verified entry was synthesised from silence: {entry[0]}"
    assert entry[0]["passed"] is True

    # And with no validation output, silence emits nothing at all.
    _status, refusal_checks = _mod.aggregate_check_results(
        _ALL_STATIC_PASS, _runner()._empty_results("bedrock-agentcore client not available")
    )
    assert "schema_compliance" not in _names(refusal_checks), _names(refusal_checks)


# ---------------------------------------------------------------------------
# The harness non-verification branch (C13 / C14) — task 4.10's contract
# ---------------------------------------------------------------------------

#: What ``_frame_truncation_results`` (task 4.10) must return. Hand-built
#: because that function does not exist yet; the branch reading it does.
_HARNESS_REFUSAL = {
    "execution_successful": False,
    "executed": False,
    "exit_code": None,
    "stderr": "",
    "input_records": 0,
    "output_records": 0,
    "data_quality_results": {},
    "error_details": "test frame could not be built: 640 fields exceeds the 512-column ceiling",
    "conversion_report": None,
    "not_verified_by_harness": True,
    "not_verified_reason": "test_frame_truncated",
    "not_verified_field_count": 640,
    "not_verified_frame_ceiling": 512,
}


def test_a_harness_refusal_replaces_the_execution_check_and_names_the_numbers():
    """Requirement 2.11 / C14 — a width refusal is not a proxy refusal."""
    status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _HARNESS_REFUSAL, _TARGET_SCHEMA)

    entry = _named(checks, "not_verified_by_harness")
    assert len(entry) == 1, _names(checks)
    assert entry[0]["verified"] is False
    assert entry[0]["not_verified_reason"] == "test_frame_truncated"
    # The field count and the ceiling as TEXT (C5), not colour alone.
    assert "640" in entry[0]["detail"] and "512" in entry[0]["detail"]
    # C14: not the proxy's shape — no construct, no line number invented.
    assert "construct" not in entry[0]["detail"]
    assert _mod.NOT_VERIFIED_CHECK_NAME not in _names(checks)
    # In place of the execution check, not alongside it.
    assert "code_interpreter_execution" not in _names(checks), _names(checks)
    # C3: a second reason, not a third state — the partition is unchanged.
    assert _mod.is_not_verified_check(entry[0]) is True
    assert status == "passed", "a frame the harness could not build must not fail the transform"


def test_a_harness_refusal_emits_no_schema_compliance_entry():
    """C13 — a shortfall the harness caused can never present as a failure.

    Asserted with a target schema RESOLVED, which is the case that could
    plausibly go wrong: the schema is there to score against, and the only
    reason not to score is that the frame was never built.
    """
    _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, _HARNESS_REFUSAL, _TARGET_SCHEMA)
    assert "schema_compliance" not in _names(checks), (
        f"an incomplete test frame was scored for schema compliance (C13): {_names(checks)}"
    )


def test_the_no_source_schema_reason_round_trips_with_its_own_text():
    """Requirement 2.11's second reason — both are harness limitations."""
    results = dict(_HARNESS_REFUSAL, not_verified_reason="no_source_schema", not_verified_field_count=0)
    _status, checks = _mod.aggregate_check_results(_ALL_STATIC_PASS, results, None)

    entry = _named(checks, "not_verified_by_harness")
    assert len(entry) == 1, _names(checks)
    assert entry[0]["not_verified_reason"] == "no_source_schema"
    assert "no source schema" in entry[0]["detail"].lower()
    # Still no score, even though the pipeline is also schema-less.
    assert "schema_compliance" not in _names(checks), _names(checks)
