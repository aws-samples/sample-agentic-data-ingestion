# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: AI review loop honesty (P28 R13, task 11.2).

The review quality gate and the regeneration trigger consume the structured
JSON verdict — the keyword/substring machinery is deleted:

  * ``critical_patterns`` (regen trigger): ``"syntax error" in review_lower``
    matched "no syntax errors found" and triggered regeneration on a clean
    review.
  * ``critical_phrases`` (approval gate): same substring dishonesty class.

Regeneration is bounded (config ``CODE_REGENERATION_MAX_ATTEMPTS``, default
2) with the bound recorded in state, and the regeneration feedback carries
the reviewer's issues so the regen prompt differs from the original.

Validates: P28 Requirements 13.1, 13.2, 13.3, 13.4
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_LAMBDA_SOURCE = os.path.join(_TEST_CODE_DIR, "lambda_function.py")
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


_PASS_VERDICT = {"verdict": "pass", "issues": []}
_FAIL_VERDICT = {
    "verdict": "fail",
    "issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date format string is wrong."},
        {"severity": "major", "category": "Security", "description": "Credentials logged in plaintext."},
    ],
}


# ---------------------------------------------------------------------------
# R13.2/R13.4: "no syntax errors found" does NOT trigger regeneration
# ---------------------------------------------------------------------------


def test_no_syntax_errors_found_review_does_not_trigger_regeneration():
    """The poster-child dishonesty: a PASS verdict whose issue text contains
    the words "no syntax errors found" must NOT trigger regeneration. Under
    the deleted substring trigger, `"syntax error" in review_lower` matched
    this phrase and regenerated clean code.

    **Validates: Requirements 13.2, 13.4**
    """
    verdict = {
        "verdict": "pass",
        "issues": [{"severity": "minor", "description": "No syntax errors found; style is acceptable."}],
    }
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=verdict,
        regeneration_attempt=0,
    )
    assert analysis["regeneration_needed"] is False
    assert analysis["max_attempts_reached"] is False


def test_substring_trigger_and_phrase_gate_are_deleted_from_source():
    """Grep assertion: the substring machinery is fully removed (R13.2).

    Checks the load-bearing constructs, not identifier names (which comments
    may legitimately mention as deleted history): the lowercased review text
    variable the substrings were matched against, and the trigger/phrase
    string literals themselves.
    """
    with open(_LAMBDA_SOURCE, encoding="utf-8") as f:
        source = f.read()
    assert "review_lower" not in source  # the substring-matching variable
    assert '"syntax error"' not in source  # critical_patterns trigger literal
    assert '"does not compile"' not in source  # critical_patterns trigger literal
    assert '"recommendation: reject"' not in source  # critical_phrases gate literal


# ---------------------------------------------------------------------------
# R13.2: the structured verdict drives regeneration
# ---------------------------------------------------------------------------


def test_fail_verdict_triggers_regeneration_with_reviewer_issues():
    """**Validates: Requirements 13.2, 13.3** — fail verdict → regen, and the
    feedback carries the reviewer's issues (structured + rendered strings)."""
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="failed",
        checks_run=4,
        checks_passed=2,
        review_verdict=_FAIL_VERDICT,
        regeneration_attempt=0,
    )
    assert analysis["regeneration_needed"] is True
    feedback = analysis["feedback_context"]
    assert feedback["reviewer_issues"] == _FAIL_VERDICT["issues"]
    rendered = "\n".join(feedback["issues_found"])
    assert "to_date format string is wrong." in rendered
    assert "Credentials logged in plaintext." in rendered
    assert "review verdict: fail" in rendered


def test_fail_verdict_alone_triggers_regeneration_even_when_checks_pass():
    """Regen is verdict-driven (R13.2), not dependent on check counts."""
    analysis = _mod.analyze_test_results_for_regeneration(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=_FAIL_VERDICT,
        regeneration_attempt=0,
    )
    assert analysis["regeneration_needed"] is True


# ---------------------------------------------------------------------------
# R13.3: bounded attempts (config, default 2), bound recorded in analysis
# ---------------------------------------------------------------------------


def test_attempts_bounded_at_default_two():
    below = _mod.analyze_test_results_for_regeneration("failed", 4, 2, _FAIL_VERDICT, regeneration_attempt=1)
    assert below["regeneration_needed"] is True
    assert below["max_attempts"] == 2
    assert below["regeneration_attempt"] == 1

    at_bound = _mod.analyze_test_results_for_regeneration("failed", 4, 2, _FAIL_VERDICT, regeneration_attempt=2)
    assert at_bound["regeneration_needed"] is False
    assert at_bound["max_attempts_reached"] is True
    assert at_bound["max_attempts"] == 2


def test_attempt_bound_configurable_via_env():
    with patch.dict("os.environ", {"CODE_REGENERATION_MAX_ATTEMPTS": "5"}):
        analysis = _mod.analyze_test_results_for_regeneration("failed", 4, 2, _FAIL_VERDICT, regeneration_attempt=4)
        assert analysis["regeneration_needed"] is True
        assert analysis["max_attempts"] == 5


def test_misconfigured_attempt_bound_raises_honestly():
    with patch.dict("os.environ", {"CODE_REGENERATION_MAX_ATTEMPTS": "banana"}), pytest.raises(ValueError):
        _mod.get_regeneration_max_attempts()


# ---------------------------------------------------------------------------
# R13.1: the approval gate consumes the structured verdict
# ---------------------------------------------------------------------------

_NO_REGEN = {"regeneration_needed": False, "max_attempts_reached": False}


def test_fail_verdict_requires_approval_even_when_all_checks_pass():
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=_FAIL_VERDICT,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is True


def test_pass_verdict_with_passing_checks_requires_no_approval():
    required = _mod.determine_code_approval_requirement(
        test_status="passed",
        checks_run=4,
        checks_passed=4,
        review_verdict=_PASS_VERDICT,
        regeneration_analysis=_NO_REGEN,
    )
    assert required is False


# ---------------------------------------------------------------------------
# R13.1: the verdict participates in the honest aggregation as a check
# ---------------------------------------------------------------------------


def test_fail_verdict_fails_aggregation_even_when_categories_pass():
    parsed = {
        "verdict": "fail",
        "issues": _FAIL_VERDICT["issues"],
        "test_categories": {"Correctness": {"status": "PASSED", "passed": True}},
        "recommendation": "REJECT",
    }
    status, checks = _mod.aggregate_check_results(
        parsed, {"execution_successful": True, "input_records": 5, "output_records": 5}
    )
    assert status == "failed"
    verdict_check = next(c for c in checks if c["name"] == "ai_review_verdict")
    assert verdict_check["passed"] is False
    assert "to_date format string is wrong." in verdict_check["detail"]


# ---------------------------------------------------------------------------
# Handler: reparse retry (success on 2nd response) + bound recorded in state
# ---------------------------------------------------------------------------

_CODE_GEN_ITEM = {
    "run_id": "run-1",
    "step_name": "create_code",
    "status": "success",
    "output_data": {"code_version_id": "cv-1", "code_s3_uri": "s3://code-bucket/code.py"},
}

_EXEC_OK = {"execution_successful": True, "input_records": 10, "output_records": 10}

_VALID_PASS_JSON = json.dumps({"verdict": "pass", "issues": [], "categories": {"Correctness": "pass"}})
_VALID_FAIL_JSON = json.dumps(
    {
        "verdict": "fail",
        "issues": [{"severity": "major", "category": "Security", "description": "Plaintext credentials."}],
        "categories": {"Correctness": "pass", "Security": "fail"},
    }
)


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
    record and raises StepFailure (p33 requirement 2.8). The record below exists
    and carries no target schema — requirement 2.6's legitimate state — so the
    review-gate behaviour this file asserts on is unchanged.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def _run_handler(agent_responses: list[str], execution_results: dict):
    """Run lambda_handler; the mocked agent returns agent_responses in order.

    Returns (response, state_item, agent_mock, runs_table_mock).
    """
    runs_table = MagicMock()
    runs_table.get_item.return_value = {"Item": _CODE_GEN_ITEM}
    code_table = MagicMock()
    code_table.get_item.return_value = {"Item": {"PK": "CODE#cv-1", "SK": "VERSION"}}
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"print('transform')"))}

    agent = MagicMock(side_effect=agent_responses)
    agent.add_hook = MagicMock()

    runner = MagicMock()
    runner.run_test.return_value = execution_results

    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "generated_code_table", code_table),
        patch.object(_mod, "test_results_table", MagicMock()),
        patch.object(_mod, "s3_client", s3),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.object(_mod, "Agent", MagicMock(return_value=agent)),
        patch.object(_mod, "AgentTracePlugin", MagicMock()),
        patch.object(_mod, "CodeInterpreterTestRunner", MagicMock(return_value=runner)),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
    ):
        response = _mod.lambda_handler({"run_id": "run-1", "pipeline_id": "pipe-1"}, None)

    testing_items = [i for i in _state_rows(runs_table) if i.get("step_name") == "test_code"]
    assert len(testing_items) == 1
    return response, testing_items[0], agent, runs_table


def test_handler_reparse_retry_recovers_from_one_malformed_response():
    """First response is prose, retry returns valid JSON → run completes and
    the agent was asked exactly twice (one reparse retry).

    **Validates: Requirements 13.1**
    """
    response, state_item, agent, _ = _run_handler(
        ["Looks good to me! No syntax errors found.", _VALID_PASS_JSON], _EXEC_OK
    )
    assert agent.call_count == 2
    assert response["test_status"] == "passed"
    # The reparse prompt carried the malformed response back to the model.
    retry_prompt = agent.call_args_list[1].args[0]
    assert "No syntax errors found" in retry_prompt
    assert '"verdict"' in retry_prompt


def test_handler_records_attempt_bound_in_state_row_and_response():
    """**Validates: Requirements 13.3** — bound + attempt count in state."""
    response, state_item, _, _ = _run_handler([_VALID_FAIL_JSON], _EXEC_OK)

    assert state_item["regeneration_attempt"] == 0
    assert state_item["regeneration_max_attempts"] == 2  # config default
    analysis = json.loads(state_item["regeneration_analysis"])
    assert analysis["max_attempts"] == 2
    assert analysis["regeneration_attempt"] == 0
    assert response["regeneration_max_attempts"] == 2


def test_handler_fail_verdict_stores_reviewer_issues_for_regeneration():
    """**Validates: Requirements 13.3** — the stored regeneration feedback
    carries the reviewer's issues (the create_code prompt embeds them)."""
    _, state_item, _, runs_table = _run_handler([_VALID_FAIL_JSON], _EXEC_OK)

    # store_regeneration_feedback writes via update_item on the runs table.
    # The step's own row write is also an update_item now, so select the
    # feedback call by the attribute it sets rather than by call order.
    update_calls = [
        c for c in runs_table.update_item.call_args_list if ":feedback" in c.kwargs["ExpressionAttributeValues"]
    ]
    assert update_calls, "expected regeneration feedback to be stored"
    feedback = update_calls[0].kwargs["ExpressionAttributeValues"][":feedback"]
    assert feedback["reviewer_issues"] == [
        {
            "severity": "major",
            "category": "Security",
            "description": "Plaintext credentials.",
            # P28b R1b additive fields: unattributed issue → conservative default.
            "attribution": "transform_block",
            "attribution_source": "default",
        }
    ]
    assert any("Plaintext credentials." in line for line in feedback["issues_found"])


def test_handler_pass_verdict_still_gates_on_failed_execution():
    """A pass verdict cannot mask a failed Code Interpreter execution — the
    honest aggregation (R4.3) still fails the step."""
    exec_failed = {"execution_successful": False, "error_details": "IndentationError"}
    response, state_item, _, _ = _run_handler([_VALID_PASS_JSON], exec_failed)
    assert response["test_status"] == "failed"
    assert state_item["status"] == "failed"
    assert response["approval_required"] is True
