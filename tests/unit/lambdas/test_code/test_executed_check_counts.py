# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: test_code metrics come from executed checks, not LLM text (P28 R10.5, R10.6).

Task 9.4 deleted the LLM-invented numerics ("Total Tests" / "Coverage"
regex-parsed from free text, the sentiment-keyword fabrication fallback, and
the parse-exception fallback that forced a passed 6/6 result). Task 11.2
(R13.1) replaced the free-text parser entirely: the review model returns a
structured JSON verdict, parsed by review_verdict.parse_review_verdict —
schema-validated, one reparse retry, then honest StepFailure.

Every reported metric (checks_run / checks_passed / checks_failed, and the
dashboard-stable tests_run / tests_passed / tests_failed aliases) is a count
of actually executed checks (review verdict, per-category verdicts, Code
Interpreter execution). coverage_percent is removed everywhere — it never
had a real measurement behind it.

Validates: P28 Requirements 10.5, 10.6, 13.1
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation.
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
    sys.modules["lambda_function"] = _mod
    yield


_LLM_INVENTED_KEYS = {"total_tests", "tests_passed", "tests_failed", "coverage_percent", "overall_status"}


# ---------------------------------------------------------------------------
# Structured verdict parsing (P28 R13.1, task 11.2) — the regex extraction of
# markdown headings is deleted; the single parser is
# review_verdict.parse_review_verdict. These tests keep the R10.5/R10.6
# invariant: no LLM-invented numeric metrics anywhere in parsed results.
# ---------------------------------------------------------------------------


def _verdict_json(verdict: str, categories: dict[str, str], issues: list | None = None) -> str:
    import json as _json

    return _json.dumps({"verdict": verdict, "issues": issues or [], "categories": categories})


def test_parsed_results_carry_categories_but_no_numeric_metrics():
    review = _verdict_json(
        "fail",
        {"Correctness": "pass", "Security": "fail"},
        issues=[{"severity": "major", "description": "Hardcoded credentials."}],
    )
    parsed = _mod.verdict_to_parsed_results(_mod.parse_review_verdict(review))

    assert parsed["test_categories"]["Correctness"]["passed"] is True
    assert parsed["test_categories"]["Security"]["passed"] is False
    assert parsed["recommendation"] == "REJECT"
    assert not (_LLM_INVENTED_KEYS & set(parsed)), (
        f"LLM-invented metric keys present: {_LLM_INVENTED_KEYS & set(parsed)}"
    )


def test_parsed_results_ignore_inflated_numeric_claims():
    """A verdict document claiming 'tests_passed: 99' contributes NO numbers
    anywhere — unknown keys are simply not part of the schema output."""
    import json as _json

    review = _json.dumps(
        {
            "verdict": "pass",
            "issues": [],
            "categories": {"Correctness": "pass"},
            "total_tests": 100,
            "tests_passed": 99,
            "coverage_percent": 100,
        }
    )
    parsed = _mod.verdict_to_parsed_results(_mod.parse_review_verdict(review))

    assert set(parsed["test_categories"]) == {"Correctness"}
    assert not (_LLM_INVENTED_KEYS & set(parsed))


def test_unparseable_review_raises_instead_of_fabricating_metrics():
    """Free-text prose (the old sentiment-fallback input) is NOT parseable —
    the parser raises instead of fabricating a passed result."""
    with pytest.raises(_mod.ReviewVerdictError):
        _mod.parse_review_verdict("Everything looks good, no issues, production ready, well-structured code.")


def test_non_string_input_raises_instead_of_forcing_passed():
    """R10.5 residual: the parse-exception fallback used to force a passed
    6/6 result. The schema parser raises honestly on non-text input."""
    with pytest.raises(_mod.ReviewVerdictError):
        _mod.parse_review_verdict(None)


# ---------------------------------------------------------------------------
# Approval gating and regeneration analysis — honest counts in, honest gate out
# ---------------------------------------------------------------------------

_NO_REGEN = {"regeneration_needed": False, "max_attempts_reached": False}


def test_approval_not_required_when_all_executed_checks_pass():
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=3,
        checks_passed=3,
        review_verdict={"verdict": "pass", "issues": [], "categories": []},
        regeneration_analysis=_NO_REGEN,
    )
    assert required is False


def test_approval_required_on_failed_status_even_if_review_claims_high_numbers():
    """The gate keys off the honest status/counts — a review body claiming
    'Tests Passed: 99' can no longer flip approval."""
    required = _mod.determine_code_approval_requirement(
        test_status="failed",
        checks_run=3,
        checks_passed=2,
        review_verdict={"verdict": "pass", "issues": [], "categories": []},
        regeneration_analysis=_NO_REGEN,
    )
    assert required is True


def test_regeneration_analysis_reports_executed_check_counts():
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="failed",
        checks_run=3,
        checks_passed=1,
        review_verdict={
            "verdict": "fail",
            "issues": [{"severity": "error", "description": "Two checks failed"}],
            "categories": [],
        },
        regeneration_attempt=0,
    )

    assert analysis["regeneration_needed"] is True
    assert "executed checks failed: 2 out of 3" in analysis["feedback_context"]["issues_found"]


def test_regeneration_not_needed_when_all_checks_pass():
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict={"verdict": "pass", "issues": [], "categories": []},
        regeneration_attempt=0,
    )
    assert analysis["regeneration_needed"] is False


# ---------------------------------------------------------------------------
# Property: no path to "passed" without ALL executed checks passing (R10.5)
# ---------------------------------------------------------------------------

_CATEGORY_NAMES = ["Correctness", "Schema Compliance", "Error Handling", "AWS Best Practices", "Security"]


@given(
    category_outcomes=st.dictionaries(st.sampled_from(_CATEGORY_NAMES), st.booleans(), max_size=5),
    execution_ok=st.booleans(),
)
def test_status_is_passed_iff_every_executed_check_passed(category_outcomes, execution_ok):
    """**Validates: Requirements 10.5**

    For ANY combination of static-category verdicts and execution outcome,
    the reported status is "passed" exactly when every executed check passed
    — there is no input that forces "passed" past a failing check.
    """
    parsed = {
        "test_categories": {
            name: {"status": "PASSED" if ok else "FAILED", "passed": ok} for name, ok in category_outcomes.items()
        },
        "recommendation": "APPROVE",
    }
    execution_results = (
        {"execution_successful": True, "input_records": 5, "output_records": 5}
        if execution_ok
        else {"execution_successful": False, "error_details": "boom"}
    )

    status, checks = _mod.aggregate_check_results(parsed, execution_results)

    assert len(checks) == len(category_outcomes) + 1  # + execution check
    all_passed = all(c["passed"] for c in checks)
    assert status == ("passed" if all_passed else "failed")
    assert all_passed == (all(category_outcomes.values()) and execution_ok)


# ---------------------------------------------------------------------------
# Full handler — executed-check counts in every persisted/returned payload
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {
        "code_version_id": "cv-1",
        "code_s3_uri": "s3://code-bucket/code.py",
    },
}


def _state_rows(runs_table) -> list[dict]:
    """Reconstruct the state rows the handler wrote to the runs table.

    The handler's row write is an ``update_item`` SET over exactly the
    attributes the step owns, not a full-row ``put_item``: a PutItem there
    deleted ``store_regeneration_feedback``'s write on the same item earlier in
    the same invocation (see ``build_state_row_update``). The row is therefore
    the ``Key`` plus the SET values, identified by ``step_sequence`` — an
    attribute only the row write sets.
    """
    rows = []
    for call in runs_table.update_item.call_args_list:
        values = {name.lstrip(":"): value for name, value in call.kwargs["ExpressionAttributeValues"].items()}
        if "step_sequence" not in values:
            continue
        rows.append({**call.kwargs["Key"], **values})
    return rows


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a keyless record.

    p33 task 4.3 plumbing, not an assertion change: the handler's target-schema
    read moved from the schema-mappings item to the pipeline record, so the
    pipelines table is now on the handler's path. A bare ``MagicMock()`` answers
    ``False`` to ``"Item" in response``, which now reads as a *missing* pipeline
    record and raises StepFailure (requirement 2.8). The record below exists and
    carries no target schema — requirement 2.6's legitimate state — which is
    what keeps every executed-check count in this file unchanged: with no target
    schema there is no computed schema-compliance entry to count.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _run_handler(review_text: str, execution_results: dict):
    """Run lambda_handler with all AWS/agent dependencies mocked.

    Returns (response, state_item, results_table_item).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    results_table = MagicMock()
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value=review_text)
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
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    testing_items = [i for i in _state_rows(runs_table) if i.get("step_name") == "test_code"]
    assert len(testing_items) == 1, "expected exactly one test_code state item"

    results_items = [call.kwargs["Item"] for call in results_table.put_item.call_args_list]
    assert len(results_items) == 1, "expected exactly one test-results table item"

    return response, testing_items[0], results_items[0]


_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}

# Structured verdict with TWO category verdicts plus junk numeric claims the
# schema does not know about. Actual executed checks: 1 review verdict + 2
# static categories + 1 execution = 4 — the junk numbers appear nowhere.
_REVIEW_INFLATED_CLAIMS = json.dumps(
    {
        "verdict": "pass",
        "issues": [],
        "categories": {"Correctness": "pass", "Security": "pass"},
        "total_tests": 100,
        "tests_passed": 99,
        "tests_failed": 1,
        "coverage_percent": 100,
    }
)

_REVIEW_ONE_FAIL = json.dumps(
    {
        "verdict": "fail",
        "issues": [{"severity": "major", "category": "Security", "description": "Plaintext credentials."}],
        "categories": {"Correctness": "pass", "Security": "fail"},
        "total_tests": 100,
        "tests_passed": 99,
        "tests_failed": 1,
        "coverage_percent": 100,
    }
)


def test_handler_counts_come_from_actual_checks_not_review_text():
    """Review claims 'tests_passed: 99' — 4 actual checks → 4-based counts."""
    response, state_item, results_item = _run_handler(_REVIEW_INFLATED_CLAIMS, _EXEC_OK)

    assert response["test_results"]["checks_run"] == 4
    assert response["test_results"]["checks_passed"] == 4
    assert response["test_results"]["checks_failed"] == 0
    # Dashboard-stable aliases carry the same executed-check counts.
    assert response["test_results"]["tests_run"] == 4
    assert response["test_results"]["tests_passed"] == 4
    assert response["test_results"]["tests_failed"] == 0
    assert response["test_summary"]["total_tests"] == 4
    assert response["test_summary"]["tests_passed"] == 4
    assert "99" not in response["message"]
    assert "4/4 checks passed" in response["message"]


def test_state_item_and_results_table_carry_executed_check_counts():
    response, state_item, results_item = _run_handler(_REVIEW_INFLATED_CLAIMS, _EXEC_OK)

    for metrics in (
        results_item["test_results"],
        state_item["output_data"]["approval"]["test_results"],
    ):
        assert metrics["checks_run"] == 4
        assert metrics["checks_passed"] == 4
        assert metrics["checks_failed"] == 0
        assert metrics["tests_run"] == 4
        assert metrics["tests_passed"] == 4
        assert metrics["tests_failed"] == 0

    summary = state_item["output_data"]["summary"]
    assert summary["tests_total"] == 4
    assert summary["tests_passed"] == 4
    assert summary["tests_failed"] == 0


def test_no_coverage_percent_in_any_persisted_or_returned_payload():
    """coverage_percent had no honest source — it is removed everywhere
    (p29 note: the dashboard coverage box conditionally renders on > 0 and
    simply disappears)."""
    response, state_item, results_item = _run_handler(_REVIEW_INFLATED_CLAIMS, _EXEC_OK)

    assert "coverage_percent" not in response["test_results"]
    assert "coverage_percent" not in response["test_summary"]
    assert "coverage_percent" not in results_item["test_results"]
    assert "coverage_percent" not in state_item["output_data"]["approval"]["test_results"]
    assert "coverage_percent" not in state_item["output_data"]["summary"]


def test_handler_failed_check_yields_honest_counts_and_forces_approval():
    """A fail verdict + failed Security category among inflated numeric
    claims → failed status, 2/4 counts, approval required."""
    response, state_item, results_item = _run_handler(_REVIEW_ONE_FAIL, _EXEC_OK)

    assert response["test_status"] == "failed"
    assert response["test_result"]["status"] == "failed"
    assert response["test_results"]["checks_run"] == 4
    assert response["test_results"]["checks_passed"] == 2  # Correctness + execution
    assert response["test_results"]["checks_failed"] == 2  # verdict + Security
    assert response["approval_required"] is True
    assert state_item["status"] == "failed"
    assert results_item["test_results"]["tests_failed"] == 2


def test_handler_unparseable_review_fails_honestly_after_one_reparse_retry():
    """P28 R13.1 (task 11.2): with an unparseable review the handler performs
    exactly ONE reparse retry, then raises StepFailure — it does NOT fabricate
    checks or a passed status from prose."""
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(return_value="Great code, no issues, production ready!")
    agent.add_hook = MagicMock()

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock()),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    assert agent.call_count == 2  # original + exactly one reparse retry
    assert "structured verdict" in exc_info.value.message


def test_sfn_choice_contract_intact():
    """The 5.1 contract survives the metric swap: test_result {status,
    checks[], report_s3_uri} at $.test_code_result.test_result.status.

    p40 task 9.1 (R6): AMENDED IN PLACE, not deleted (``LGR-60c4985-6`` /
    ``LGR-b2f79ef-6``, both template / ratified — a moved assertion is amended
    with its reason AT the assertion so the change stays legible as a
    deliberate widening rather than reading as a regression).

    ``execution_verified`` joins the key set. What this test is actually
    protecting is unchanged and is asserted below unchanged: the Choice reads
    ``$.test_code_result.test_result.status`` and nothing else
    (``orchestration-stack.ts:853-854``, read), so widening the object cannot
    move a routing decision — ``B3`` / ``LGR-69ca78b-1``. The exact-set form is
    KEPT rather than relaxed to ``>=``, because "no key appears here that the
    spec did not put here" is the property that makes the inertness claim
    checkable at all.

    ``verification_note`` is deliberately NOT in the set: this fixture is
    ``_EXEC_OK``, a run whose transform executed, and the note is emitted only
    when it did not. The non-verified shape is task 9.2's subject.
    """
    response, state_item, _ = _run_handler(_REVIEW_INFLATED_CLAIMS, _EXEC_OK)

    for container in (response["test_result"], state_item["test_result"]):
        assert set(container) == {"status", "checks", "report_s3_uri", "execution_verified"}
        assert container["status"] in {"passed", "failed"}
        assert isinstance(container["checks"], list)
        assert container["report_s3_uri"].startswith("s3://")
        # p40 R6: this fixture executed, so the label says so and carries no note.
        assert container["execution_verified"] is True
