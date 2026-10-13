# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for retry_run() with user-selected retry_from_step.

Validates: Requirements 3.1, 3.2, 3.3, 3.5
Tests the retry_run function's handling of the optional retry_from_step
field in the request body: using a user-selected step, falling back to
the auto-detected failed step, rejecting invalid step names, and
rejecting malformed JSON bodies.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out Lambda-layer dependencies before importing the handler
# ---------------------------------------------------------------------------
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:agentic-data-ingestion-pipeline",
    "STATE_TABLE": "agentic_data_ingestion_state",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add handler package and shared directory to path
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

try:
    from handlers import (
        pipelines,
        pipelines_runs,
    )
    from handlers.pipelines import retry_run
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-pipeline-abc12345"
RUN_ID = "run-abc123"

# ---------------------------------------------------------------------------
# Mock tables and clients
# ---------------------------------------------------------------------------
mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_locks_table = MagicMock()
mock_dynamodb = MagicMock()

_table_map = {
    "agentic_data_ingestion_pipeline_locks": mock_locks_table,
}
mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())


@pytest.fixture(autouse=True)
def _reset_mocks():
    """Reset all mocks before each test and wire them into the module."""
    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_executions_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_locks_table.reset_mock()
    mock_dynamodb.reset_mock()
    mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())

    # Default: no items
    mock_state_table.query.return_value = {"Items": []}
    mock_executions_table.query.return_value = {"Items": []}
    # Pipeline record backing the rerun contract's source resolution
    mock_pipelines_table.get_item.return_value = {
        "Item": {
            "pipeline_id": PIPELINE_ID,
            "source_type": "S3_UPLOAD",
            "source_bucket": "test-source-bucket",
        }
    }

    # Wire mocks into the module
    pipelines.pipelines_table = mock_pipelines_table
    pipelines.state_table = mock_state_table
    pipelines.executions_table = mock_executions_table
    pipelines.approvals_table = mock_approvals_table
    pipelines.dynamodb = mock_dynamodb
    # P21 Task 7.2: retry_run now lives in handlers.pipelines_runs; wire the
    # same mocks into the owning module (module references only).
    pipelines_runs.pipelines_table = mock_pipelines_table
    pipelines_runs.state_table = mock_state_table
    pipelines_runs.executions_table = mock_executions_table
    pipelines_runs.approvals_table = mock_approvals_table
    pipelines_runs.dynamodb = mock_dynamodb
    yield


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------


def _failed_run_entries(failed_step="CreateCode"):
    """State entries for a run that failed at the given step.

    Steps before the failed step are marked as success with output_data.
    """
    from retry_utils import STEP_ORDER

    items = []
    for step in STEP_ORDER:
        if step == failed_step:
            items.append(
                {
                    "run_id": RUN_ID,
                    "step_name": step,
                    "status": "error",
                    "pipeline_id": PIPELINE_ID,
                    "original_pipeline_id": PIPELINE_ID,
                    "error_message": f"{step} failed",
                    "output_data": {},
                    "timestamp": "2024-01-01T00:10:00Z",
                }
            )
            break
        items.append(
            {
                "run_id": RUN_ID,
                "step_name": step,
                "status": "success",
                "pipeline_id": PIPELINE_ID,
                "original_pipeline_id": PIPELINE_ID,
                "output_data": {f"{step}_data": True},
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )
    return items


def _failed_execution_record():
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "status": "FAILED",
        "execution_id": "arn:aws:states:us-east-1:123456789012:execution:test:run-1",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryRunWithUserSelectedStep:
    """Validates: Requirement 3.1 — retry_from_step in body uses that step."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_uses_user_selected_step(self, mock_boto_client):
        """When body contains retry_from_step, SFN input uses that step."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"retry_from_step": "SchemaExtract"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["success"] is True
        assert resp_body["retry_from_step"] == "SchemaExtract"

        # Verify SFN input
        call_kwargs = mock_sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["retry_from_step"] == "SchemaExtract"

    @patch("handlers.pipelines_runs.boto3.client")
    def test_sfn_input_includes_cached_results_for_prior_steps(self, mock_boto_client):
        """Validates: Req 3.5 — cached results for steps before retry point are in SFN input.

        P28 R9.2: the rerun contract emits cached results at the CANONICAL
        snake_case ``{step}_result`` keys — the exact resultPath names the
        state machine dereferences. (The pre-contract code emitted PascalCase
        ``FileValidation_result`` keys that never matched any resultPath.)
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"retry_from_step": "SchemaCompare"})
        result = retry_run(RUN_ID, body=body)

        assert result["statusCode"] == 200

        call_kwargs = mock_sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])

        # FileValidation and SchemaExtract precede SchemaCompare — their
        # results are cached at the canonical resultPath keys (the state rows
        # were written with the PascalCase step names; the contract
        # normalizes them)
        assert "file_validation_result" in sfn_input
        assert sfn_input["file_validation_result"]["FileValidation_data"] is True
        assert "schema_extract_result" in sfn_input
        assert sfn_input["schema_extract_result"]["SchemaExtract_data"] is True
        # The dead PascalCase keys are gone
        assert "FileValidation_result" not in sfn_input
        assert "SchemaExtract_result" not in sfn_input

    @patch("handlers.pipelines_runs.boto3.client")
    def test_response_includes_retry_from_step(self, mock_boto_client):
        """Validates: Req 3.5 — success response body contains retry_from_step."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"retry_from_step": "CreateCode"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["retry_from_step"] == "CreateCode"


class TestRetryRunFallbackToFailedStep:
    """Validates: Requirement 3.2 — without retry_from_step, falls back to auto-detected failed step."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_falls_back_to_failed_step_with_empty_body(self, mock_boto_client):
        """When body is empty JSON, retry uses the auto-detected failed step."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID, body="{}")
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["retry_from_step"] == "CreateCode"

    @patch("handlers.pipelines_runs.boto3.client")
    def test_falls_back_to_failed_step_with_no_body(self, mock_boto_client):
        """When body is the default (no retry_from_step key), retry uses the failed step."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["retry_from_step"] == "CreateCode"

    @patch("handlers.pipelines_runs.boto3.client")
    def test_falls_back_with_body_missing_retry_from_step_key(self, mock_boto_client):
        """When body has other keys but no retry_from_step, falls back to failed step."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("SchemaMapping")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"some_other_key": "value"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["retry_from_step"] == "SchemaMapping"


class TestRetryRunInvalidStepName:
    """Validates: Requirement 3.3 — invalid step name returns 400 with valid_steps."""

    def test_returns_400_for_invalid_step_name(self):
        """Invalid step name returns 400 with error and valid_steps list."""
        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}

        body = json.dumps({"retry_from_step": "NonExistentStep"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert resp_body["error"] == "Invalid step name"
        assert "valid_steps" in resp_body
        assert isinstance(resp_body["valid_steps"], list)
        assert len(resp_body["valid_steps"]) == 13
        assert "FileValidation" in resp_body["valid_steps"]
        assert "RunPipeline" in resp_body["valid_steps"]

    def test_returns_400_for_empty_string_step(self):
        """Empty string step name is rejected."""
        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}

        body = json.dumps({"retry_from_step": ""})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert resp_body["error"] == "Invalid step name"
        assert "valid_steps" in resp_body

    @patch("handlers.pipelines_runs.boto3.client")
    def test_accepts_case_variant_step_and_normalizes(self, mock_boto_client):
        """P28 R8.2 (task 14.2): casing variants of a known step are accepted
        and normalized via from_any — only genuinely unknown names get 400."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"retry_from_step": "createcode"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        # Echoed value is the contract-normalized SFN state id
        assert resp_body["retry_from_step"] == "CreateCode"

    @patch("handlers.pipelines_runs.boto3.client")
    def test_accepts_snake_case_failed_step_from_failure_record(self, mock_boto_client):
        """P28 R8.2 (task 14.2): the dashboard posts the snake_case failed_step
        from a Failure_Record; the endpoint must normalize it, not reject it
        against the PascalCase STEP_ORDER."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn
        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        body = json.dumps({"retry_from_step": "create_code"})
        result = retry_run(RUN_ID, body=body)
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert resp_body["retry_from_step"] == "CreateCode"
        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert sfn_input["retry_from_step"] == "CreateCode"


class TestRetryRunAdoptsRerunContract:
    """Validates: Requirements 9.1, 9.2 (P28) — retry input via the shared contract."""

    @patch("handlers.pipelines_runs.boto3.client")
    def test_retry_input_carries_full_trigger_contract(self, mock_boto_client):
        """The retry input carries source_bucket/source_prefix + triggered_by."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        mock_state_table.query.return_value = {"Items": _failed_run_entries("CreateCode")}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID, body=json.dumps({"retry_from_step": "CreateCode"}))
        assert result["statusCode"] == 200

        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert sfn_input["triggered_by"] == "retry"
        assert sfn_input["trigger"] == "retry"
        assert sfn_input["source_type"] == "S3_UPLOAD"
        # Sourced from the pipeline record wired in the fixture
        assert sfn_input["source_bucket"] == "test-source-bucket"
        assert sfn_input["source_prefix"] == PIPELINE_ID
        assert sfn_input["run_id"] == RUN_ID

    @patch("handlers.pipelines_runs.boto3.client")
    def test_legacy_cased_state_rows_emit_canonical_result_keys(self, mock_boto_client):
        """Legacy 'CodeGeneration' state rows cache as create_code_result.

        The pre-contract code emitted the state row's own casing
        ('CodeGeneration_result') — a dead key no resultPath ever read.
        """
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        items = [
            {
                "run_id": RUN_ID,
                "step_name": "CodeGeneration",  # legacy create_code state-row name
                "status": "success",
                "pipeline_id": PIPELINE_ID,
                "original_pipeline_id": PIPELINE_ID,
                "output_data": {"code_version_id": "cv-1"},
                "timestamp": "2024-01-01T00:00:00Z",
            },
            {
                "run_id": RUN_ID,
                "step_name": "CodeTesting",  # legacy test_code state-row name
                "status": "error",
                "pipeline_id": PIPELINE_ID,
                "original_pipeline_id": PIPELINE_ID,
                "error_message": "boom",
                "output_data": {},
                "timestamp": "2024-01-01T00:10:00Z",
            },
        ]
        mock_state_table.query.return_value = {"Items": items}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID, body=json.dumps({"retry_from_step": "TestCode"}))
        assert result["statusCode"] == 200

        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert sfn_input["create_code_result"] == {"code_version_id": "cv-1"}
        assert "CodeGeneration_result" not in sfn_input

    @patch("handlers.pipelines_runs.boto3.client")
    def test_agent_state_rows_are_not_cached_as_step_results(self, mock_boto_client):
        """Non-step rows (agent records) never become {step}_result keys."""
        mock_sfn = MagicMock()
        mock_boto_client.return_value = mock_sfn

        items = _failed_run_entries("CreateCode") + [
            {
                "run_id": RUN_ID,
                "step_name": "self_healing",  # agent record, not a pipeline step
                "status": "success",
                "pipeline_id": PIPELINE_ID,
                "output_data": {"remediation_applied": "upgrade"},
                "timestamp": "2024-01-01T00:20:00Z",
            }
        ]
        mock_state_table.query.return_value = {"Items": items}
        mock_executions_table.query.return_value = {"Items": [_failed_execution_record()]}
        mock_locks_table.get_item.return_value = {}

        result = retry_run(RUN_ID, body=json.dumps({"retry_from_step": "CreateCode"}))
        assert result["statusCode"] == 200

        sfn_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert "self_healing_result" not in sfn_input


class TestRetryRunMalformedBody:
    """Validates: Requirement 3.3 — malformed JSON body returns 400."""

    def test_returns_400_for_malformed_json(self):
        """Malformed JSON body returns 400 with descriptive error."""
        result = retry_run(RUN_ID, body="not valid json{{{")
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "Invalid JSON" in resp_body["error"]

    def test_returns_400_for_truncated_json(self):
        """Truncated JSON body returns 400."""
        result = retry_run(RUN_ID, body='{"retry_from_step": ')
        resp_body = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "Invalid JSON" in resp_body["error"]
