# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration tests: test-failure gating (P28 R4, task 5.2).

SFN contract under test (simulated at the Lambda level per design.md — moto
cannot execute real state machines with Lambda integrations; the state-machine
topology itself — TestResultChoice branching, ForceTestReviewApproval being a
waitForTaskToken task, approve-anyway → CheckCodeApproval — is pinned by the
CDK assertion tests in deploy-cdk/test/orchestration-stack.test.ts):

    TestCode returns test_result.status == "failed" (a SUCCESSFUL step)
      → TestResultChoice default branch                 [CDK-asserted]
      → ForceTestReviewApproval invokes the REAL approval handler with the
        exact payload the state passes (approval_type "test_failure",
        report inline, task token)                      [tested here]
      → approval record (type test_failure, pending, report attached) +
        task token persisted; the execution is parked on the token —
        CreateInfrastructure can only be reached through a human decision
      → reject: send_task_failure(ApprovalRejected) → Catch → RecordFailure
        → Failure_Record, run failed                    [tested here]

Regeneration is p16 retry-from-step (resume from create_code) — carried as
remediation_hint in the approval payload, not a state-machine loop.

The invoked Lambdas are the REAL handlers (approval_handler, record_failure)
against moto-mocked DynamoDB — no stubbed AWS clients.

Validates: Requirements 4.5
"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_SHARED_DIR = _PROJECT_ROOT / "shared"

RUN_ID = "run-2026-01-01-00-00-00-itest5x2"
PIPELINE_ID = "customer-pipeline"
EXECUTION_NAME = "exec-itest-test-failure-gating"
# Realistic Step Functions task-token length (~600-700 chars)
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "x" * 640

FAILED_TEST_RESULT = {
    "status": "failed",
    "checks": [
        {"name": "syntax_check", "passed": True, "detail": "code compiles"},
        {"name": "transform_execution", "passed": False, "detail": "to_date format transform raised"},
        {"name": "output_validation", "passed": False, "detail": "df_transformed missing"},
    ],
    "report_s3_uri": f"s3://agentic-data-ingestion-test-outputs/{PIPELINE_ID}/{RUN_ID}/test_report.md",
}

# The EXACT payload ForceTestReviewApproval passes to the approval handler
# (deploy-cdk/lib/orchestration-stack.ts) with every JSONPath resolved the way
# Step Functions would against a failed-test execution state.
FORCED_APPROVAL_PAYLOAD = {
    "approval_type": "test_failure",
    "pipeline_id": PIPELINE_ID,
    "run_id": RUN_ID,
    "execution_arn": EXECUTION_NAME,
    "task_token": TASK_TOKEN,
    "test_result": FAILED_TEST_RESULT,
    "remediation_hint": "retry-from-step:create_code",
}

DOWNSTREAM_STEPS = {
    "create_infrastructure",
    "create_glue_pipeline",
    "create_quality_checks",
    "run_pipeline",
}


def _advertised_approval_window_seconds() -> int:
    """The advertised approval window, from the same source the handler reads.

    ``create_approval_request`` computes ``timeout_at`` from
    ``config.default_timeout_hours()`` (env ``DEFAULT_TIMEOUT_HOURS``,
    defaulting to ``APPROVAL_WINDOW_HOURS``), so reading that reader here
    checks the handler used the shared window rather than a literal of its
    own — and does not add a second copy of the number to this file. The
    window itself is pinned against the CDK gate constant in
    ``tests/contracts/test_approval_window_cross_language.py`` (p34 R6.6).
    """
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    from config import default_timeout_hours

    return default_timeout_hours() * 3600


def _load_lambda_module(lambda_dir_name: str, alias: str):
    """Load a Lambda's lambda_function.py under a unique module alias."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))

    sys.modules.pop(alias, None)
    spec = importlib.util.spec_from_file_location(
        alias,
        str(_LAMBDA_ROOT / lambda_dir_name / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _create_forced_approval(mock_dynamodb_tables) -> tuple[str, dict]:
    """Invoke the REAL approval handler with the ForceTestReviewApproval payload.

    Returns (approval_id, approval_record as stored in the approvals table).
    """
    ah_module = _load_lambda_module("agentic_data_ingestion_approval_handler", "itest_approval_handler_lambda")

    response = ah_module.lambda_handler(dict(FORCED_APPROVAL_PAYLOAD), MagicMock())
    assert response["statusCode"] == 200, f"approval request failed: {response}"
    approval_id = json.loads(response["body"])["approval_id"]

    approvals_table = mock_dynamodb_tables["agentic_data_ingestion_approvals"]
    record = approvals_table.get_item(Key={"approval_id": approval_id}).get("Item")
    assert record is not None, "approval record must exist"
    return approval_id, record


def _sfn_rejection_error() -> dict:
    """The SFN Catch error object produced when the review is rejected.

    The dashboard reject flow calls send_task_failure(error="ApprovalRejected",
    cause=<json>) (handlers/approvals.py); Step Functions surfaces these as the
    Error/Cause of the Catch payload delivered at $.error.
    """
    return {
        "Error": "ApprovalRejected",
        "Cause": json.dumps(
            {
                "approval_id": f"{PIPELINE_ID}_test_failure_1234567890",
                "rejected_by": "reviewer@example.com",
                "reason": "transform is wrong — regenerate via retry-from-step",
            }
        ),
    }


def _execution_state_at_forced_review(error_object: dict | None = None) -> dict:
    """The execution state as it exists while ForceTestReviewApproval runs.

    Mirrors the orchestration stack: completed steps' results at their
    resultPath keys, the failed structured test result at
    $.test_code_result.test_result, and (for the rejection case) the Catch
    payload at $.error. $.test_review_approval is absent — the gate never
    produced output.
    """
    state = {
        "source_bucket": "agentic-data-ingestion-source",
        "source_key": f"{PIPELINE_ID}/data.csv",
        "pipeline_id": PIPELINE_ID,
        "trigger": "s3_upload",
        "file_validation_result": {
            "statusCode": 200,
            "run_id": RUN_ID,
            "original_pipeline_id": PIPELINE_ID,
            "source_format": "csv",
        },
        "schema_extract_result": {"statusCode": 200, "run_id": RUN_ID},
        "schema_compare_result": {"statusCode": 200, "action": "proceed"},
        "schema_mapping_result": {"statusCode": 200, "mapping_id": "map-001"},
        "create_code_result": {"statusCode": 200, "code_version_id": "code-001"},
        "test_code_result": {
            "statusCode": 200,
            "run_id": RUN_ID,
            "test_status": "failed",
            "test_result": FAILED_TEST_RESULT,
        },
    }
    if error_object is not None:
        state["error"] = error_object
    return state


class TestFailedTestRoutesToForcedApproval:
    """A failed test_result payload produces the forced test_failure approval.

    Validates: Requirements 4.5
    """

    def test_forced_approval_record_created_with_report(self, mock_dynamodb_tables, mock_lambda_env):
        """The approval record carries type test_failure + the full report."""
        _approval_id, record = _create_forced_approval(mock_dynamodb_tables)

        assert record["approval_type"] == "test_failure"
        assert record["status"] == "pending"
        assert record["pipeline_id"] == PIPELINE_ID
        assert record["run_id"] == RUN_ID
        assert record["execution_arn"] == EXECUTION_NAME
        assert record["has_task_token"] is True

        # The full structured report is attached for the dashboard (R4.2)
        approval_data = json.loads(record["approval_data"])
        assert approval_data["test_status"] == "failed"
        assert approval_data["test_result"]["status"] == "failed"
        assert approval_data["test_result"]["checks"] == FAILED_TEST_RESULT["checks"]
        assert approval_data["report_s3_uri"] == FAILED_TEST_RESULT["report_s3_uri"]
        assert approval_data["failed_checks"] == ["transform_execution", "output_validation"]
        # Regeneration is p16 retry-from-step — documented in the payload
        assert approval_data["remediation_hint"] == "retry-from-step:create_code"

    def test_task_token_persisted_for_the_gate(self, mock_dynamodb_tables, mock_lambda_env):
        """The waitForTaskToken token is stored (read → send → delete path, task 3.1)."""
        approval_id, _ = _create_forced_approval(mock_dynamodb_tables)

        token_table = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
        token_row = token_table.get_item(Key={"approval_id": approval_id}).get("Item")
        assert token_row is not None, "task token record must exist"
        assert token_row["task_token"] == TASK_TOKEN
        # 7-day TTL backstop — deliberately longer than the 72h advertised
        # window it has to outlive (p34 R6; see config.TASK_TOKEN_TTL_DAYS).
        assert "ttl" in token_row

    def test_shares_one_approval_window_with_the_other_gates(self, mock_dynamodb_tables, mock_lambda_env):
        """test_failure flows through the SAME approval window as the other gates.

        AMENDED DELIBERATELY — p34 R6, not a regression.
            This test was named ``test_seven_day_approval_window`` and
            asserted ``7 * 24 * 3600``. Renamed because the old name stated
            the number it asserted, the same situation p34 task 10.3 hit with
            ``test_schema_mapping_gate_timeout_is_604800``.

            The surviving subject — the one worth keeping — is that all three
            approval gates share ONE window, so the forced test-review gate
            has no window of its own to drift. That is still true. Only the
            number moved: p34 R6 shortened the enforcing side
            (``APPROVAL_TIMEOUT_SECONDS`` = 72 h on all three gates in
            ``deploy-cdk/lib/orchestration-stack.ts``) and the advertised side
            now mirrors it, so an approver is not told 7 days for a run the
            gate abandons after 3.

            The expected value is read from ``shared/config.py`` rather than
            restated, so this assertion cannot become the next drifting
            literal; the config constant is in turn asserted equal to the CDK
            constant by
            ``tests/contracts/test_approval_window_cross_language.py``.
            Amending rather than deleting follows ``LGR-5906ec6-2`` and
            ``LGR-60c4985-6`` (both template / ratified).
        """
        _, record = _create_forced_approval(mock_dynamodb_tables)

        created_at = datetime.fromisoformat(record["created_at"])
        timeout_at = datetime.fromisoformat(record["timeout_at"])
        assert (timeout_at - created_at).total_seconds() == pytest.approx(_advertised_approval_window_seconds(), abs=5)


class TestNeverReachesCreateInfrastructureWithoutDecision:
    """While the forced review is pending, no downstream step has run.

    Validates: Requirements 4.5
    """

    def test_execution_parked_no_downstream_state(self, mock_dynamodb_tables, mock_lambda_env):
        """The run is parked on the token: downstream steps never wrote state.

        ForceTestReviewApproval is a waitForTaskToken task (CDK-asserted): the
        only ways forward are send_task_success (approve-anyway →
        CheckCodeApproval) or send_task_failure (reject → failure chain).
        Until one of those happens the approval stays pending, the token stays
        stored, and CreateInfrastructure never executes.
        """
        from boto3.dynamodb.conditions import Key

        approval_id, record = _create_forced_approval(mock_dynamodb_tables)

        assert record["status"] == "pending"
        token_table = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
        assert "Item" in token_table.get_item(Key={"approval_id": approval_id}), (
            "token must remain until a decision consumes it"
        )

        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        rows = state_table.query(KeyConditionExpression=Key("run_id").eq(RUN_ID))["Items"]
        steps_with_rows = {row["step_name"] for row in rows}
        assert not DOWNSTREAM_STEPS & steps_with_rows, (
            f"CreateInfrastructure and later steps must never run before the decision — "
            f"found rows for: {DOWNSTREAM_STEPS & steps_with_rows}"
        )


class TestRejectionRoutesToFailureChain:
    """Rejecting the forced review fails the run through the standard chain.

    Rejection = send_task_failure(ApprovalRejected) → the gate's Catch
    (States.ALL) → RecordFailure → ReleaseLockFailure → PipelineFailed —
    identical semantics to WaitForCodeApproval rejection.

    Validates: Requirements 4.5
    """

    def test_rejection_writes_failure_record_and_no_downstream(self, mock_dynamodb_tables, mock_lambda_env):
        from boto3.dynamodb.conditions import Key

        _create_forced_approval(mock_dynamodb_tables)

        rf_module = _load_lambda_module("agentic_data_ingestion_record_failure", "itest_record_failure_lambda_5x2")
        result = rf_module.lambda_handler(
            {
                "state": _execution_state_at_forced_review(_sfn_rejection_error()),
                "execution_name": EXECUTION_NAME,
            },
            MagicMock(),
        )
        assert result["recorded"] is True

        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        record = state_table.get_item(Key={"run_id": RUN_ID, "step_name": "pipeline_failure"}).get("Item")
        assert record is not None, "rejection must produce a Failure_Record"

        assert record["status"] == "error"
        assert record["error_name"] == "ApprovalRejected"
        # Progress inference: test_code_result present, test_review_approval
        # absent → attribution points at the step under review. The gate has
        # no Canonical_Step_Names member (it is not a pipeline step), so
        # record_failure's PROGRESS_ORDER maps the forced-review gate to
        # "test_code" (P28 R8.2, task 14.2) — which is also what p16
        # retry-from-step needs for the create_code/test_code resume.
        assert record["failed_step"] == "test_code"
        assert record["failed_step_source"] == "inferred"
        assert record["original_pipeline_id"] == PIPELINE_ID
        assert record["execution_name"] == EXECUTION_NAME

        # The rejected run never reached infrastructure creation
        rows = state_table.query(KeyConditionExpression=Key("run_id").eq(RUN_ID))["Items"]
        steps_with_rows = {row["step_name"] for row in rows}
        assert not DOWNSTREAM_STEPS & steps_with_rows


# ---------------------------------------------------------------------------
# p32 Defect 5 (C4) — a refusal-only run does not force the gate
#
# The pandas converter is a *proxy* for Spark. When it refuses a construct the
# transform never runs, so nothing about it was proven either way — that is a
# non-verification, not a failure (p32 2.20/2.21). A refusal-only run must
# therefore take TestResultChoice's "passed" branch to CheckCodeApproval, not
# the default branch to ForceTestReviewApproval: there is no failure to review
# and nothing to regenerate, because the PySpark is correct.
#
# The refusal here is REAL — the real converter refuses, the real runner's
# structured result carries the refusal, and the real aggregation classifies
# it. The Choice's matched literal is read out of the CDK source rather than
# transcribed, so this test fails if either side drifts.
#
# Validates: Requirements 2.20, 2.21, 2.24; 3.21, 3.22
# ---------------------------------------------------------------------------

_TEST_CODE_DIR = _LAMBDA_ROOT / "agentic_data_ingestion_test_code"
_ORCHESTRATION_STACK = _PROJECT_ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts"

# p32 2.23 reason (b) — a construct the converter cannot YET express, so the
# refusal is a defect in the checker rather than a statement about the
# transform.
#
# This was p32 1.7 (two chained `.withColumn` calls on one statement) until
# task T10's AST converter made that shape CONVERT, which is what 2.7
# requires. Only the input moved — no assertion below changed. The replacement
# is the same shape T10 substituted into the three unit files it fixed
# (`test_code_interpreter_runner.py`, `test_honest_aggregation.py`,
# `test_not_verified_classification_prop.py`): a chain that mixes frame
# operations, the same defect class as `pyspark_templates/README.md`'s
# parenthesised fluent worked example (1.12), collapsed onto one line so the
# refusal still reports `withColumn` on line 1.
_REFUSED_TRANSFORM_BLOCK = (
    'df_transformed = df.withColumnRenamed("old", "new").withColumn("a", trim(col("x"))).drop("tmp")'
)

_SOURCE_SCHEMA = {"properties": {"x": {"type": "string"}, "y": {"type": "string"}}}
_TARGET_SCHEMA = {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}}

_ALL_STATIC_PASS = {
    "verdict": "pass",
    "issues": [],
    "test_categories": {
        "Correctness": {"status": "PASSED", "passed": True},
        "Security": {"status": "PASSED", "passed": True},
    },
    "recommendation": "APPROVE",
}
_REVIEW_PASS = {"verdict": "pass", "issues": [], "categories": {}}


def _load_test_code_aggregation():
    """Load test_code's lambda_function with only its heavy imports stubbed.

    The strands/AgentCore/metrics imports are not installed in the unit
    environment; the aggregation, the regeneration analysis and the approval
    gate under test are pure functions of their arguments.
    """
    from unittest.mock import patch as _patch

    if str(_TEST_CODE_DIR) not in sys.path:
        sys.path.insert(0, str(_TEST_CODE_DIR))

    stubs = {
        "strands": MagicMock(),
        "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(write_agent_audit_entry=None),
        "prompt_template_loader": MagicMock(load_prompt_template=None),
        "metrics_facade": MagicMock(PipelineMetrics=None),
        "eventbridge_utils": MagicMock(
            get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
        ),
        "code_interpreter_runner": MagicMock(),
        "env_utils": MagicMock(
            get_aws_account_id=MagicMock(return_value="123456789012"),
            require_env=MagicMock(side_effect=lambda k, d=None: d or k),
        ),
        "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    }
    with (
        _patch.dict("sys.modules", stubs),
        _patch("boto3.client", MagicMock()),
        _patch("boto3.resource", MagicMock()),
    ):
        spec = importlib.util.spec_from_file_location(
            "itest_test_code_lambda_p32",
            str(_TEST_CODE_DIR / "lambda_function.py"),
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = module
        spec.loader.exec_module(module)
    sys.modules.pop("lambda_function", None)
    return module


def _real_refusal_execution_results() -> dict:
    """The real runner's structured outcome for a real converter refusal."""
    from unittest.mock import patch as _patch

    if str(_TEST_CODE_DIR) not in sys.path:
        sys.path.insert(0, str(_TEST_CODE_DIR))

    with _patch("boto3.client", MagicMock()):
        from code_interpreter_runner import CodeInterpreterTestRunner

        runner = CodeInterpreterTestRunner("us-east-1")
    assert runner.client is not None, "guard: a None client takes the _empty_results path instead"
    # Conversion runs before any session is started, so no AWS call is reached.
    return runner.run_test(_REFUSED_TRANSFORM_BLOCK, _SOURCE_SCHEMA, _TARGET_SCHEMA)


def _choice_passed_literal() -> str:
    """The status value TestResultChoice matches, read from the CDK source."""
    import re

    source = _ORCHESTRATION_STACK.read_text(encoding="utf-8")
    match = re.search(
        r"stringEquals\(\s*'\$\.test_code_result\.test_result\.status'\s*,\s*'([a-z_]+)'\s*\)",
        source,
    )
    assert match is not None, "TestResultChoice's status condition not found in orchestration-stack.ts"
    return match.group(1)


class TestRefusalOnlyRunDoesNotForceTheGate:
    """p32 2.21 — a non-verification is not a failure, so no forced review."""

    def test_refusal_only_run_takes_the_choice_passed_branch(self):
        mod = _load_test_code_aggregation()
        execution_results = _real_refusal_execution_results()
        assert execution_results["not_verified_by_proxy"] is True

        test_status, checks = mod.aggregate_check_results(_ALL_STATIC_PASS, execution_results)
        test_result = {"status": test_status, "checks": checks}

        # isPresent + stringEquals(..., 'passed') at orchestration-stack.ts:681-682
        assert test_result["status"] == _choice_passed_literal()

    def test_refusal_only_run_requires_no_approval_and_no_regeneration(self):
        mod = _load_test_code_aggregation()
        execution_results = _real_refusal_execution_results()

        test_status, checks = mod.aggregate_check_results(_ALL_STATIC_PASS, execution_results)
        verified = [c for c in checks if not mod.is_not_verified_check(c)]
        checks_run = len(verified)
        checks_passed = sum(1 for c in verified if c["passed"])

        analysis = mod.analyze_test_results_for_regeneration(
            test_status, checks_run, checks_passed, _REVIEW_PASS, regeneration_attempt=0
        )
        assert analysis["regeneration_needed"] is False
        assert (
            mod.determine_code_approval_requirement(test_status, checks_run, checks_passed, _REVIEW_PASS, analysis)
            is False
        )

    def test_the_non_verification_still_reaches_the_payload(self):
        """2.20 — "not verified" is not implemented as "not reported"."""
        mod = _load_test_code_aggregation()
        execution_results = _real_refusal_execution_results()

        _status, checks = mod.aggregate_check_results(_ALL_STATIC_PASS, execution_results)

        entry = next(c for c in checks if mod.is_not_verified_check(c))
        assert "withColumn" in entry["detail"]
        assert "line 1" in entry["detail"]
        assert entry["not_verified_reason"] == "not_yet_expressible"

    def test_a_genuinely_failed_test_still_takes_the_default_branch(self):
        """3.21/3.22 — the gate this reclassification must not soften."""
        mod = _load_test_code_aggregation()
        ran_and_failed = {
            "execution_successful": False,
            "executed": True,
            "exit_code": 1,
            "error_details": "transform execution failed: exit code 1",
        }

        test_status, checks = mod.aggregate_check_results(_ALL_STATIC_PASS, ran_and_failed)

        assert test_status == "failed"
        assert test_status != _choice_passed_literal(), "a real failure must reach ForceTestReviewApproval"
        assert not [c for c in checks if mod.is_not_verified_check(c)]
