# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: `test_failure` approval type in the approval handler (P28 R4.2, task 5.2).

The ForceTestReviewApproval state invokes the approval handler with
``approval_type: "test_failure"`` and the structured test report inline
(``test_result: {status, checks, report_s3_uri}`` — the task 5.1 contract)
plus a ``remediation_hint`` pointing at p16 retry-from-step. These tests pin:

- validate_approval_request accepts the new type (and still rejects unknowns)
- get_approval_data builds the approval payload from the inline report
  (checks + failed-check names + report_s3_uri + remediation_hint)
- the state-table fallback (canonical step_name "test_code") when no inline report

Validates: Requirements 4.2
"""

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    import importlib.util

    sys.modules.pop("lambda_function", None)
    _spec = importlib.util.spec_from_file_location(
        "approval_handler_test_failure_lf",
        os.path.join(LAMBDA_DIR, "lambda_function.py"),
    )
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)


import pytest


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_state_table.reset_mock(return_value=True, side_effect=True)
    handler.state_table = mock_state_table
    yield


RUN_ID = "run-2026-01-01-00-00-00-ut000001"

FAILED_TEST_RESULT = {
    "status": "failed",
    "checks": [
        {"name": "syntax_check", "passed": True, "detail": "compiles"},
        {"name": "transform_execution", "passed": False, "detail": "to_date raised"},
        {"name": "output_validation", "passed": False, "detail": "df_transformed missing"},
    ],
    "report_s3_uri": "s3://test-outputs/run/test_report.md",
}


class TestValidateApprovalRequestAcceptsTestFailure:
    """validate_approval_request accepts test_failure and keeps the report intact."""

    def test_test_failure_is_a_valid_type(self):
        event = {
            "approval_type": "test_failure",
            "pipeline_id": "pipe-1",
            "run_id": RUN_ID,
            "task_token": "tok-123",
            "test_result": FAILED_TEST_RESULT,
            "remediation_hint": "retry-from-step:create_code",
        }

        result = handler.validate_approval_request(event)

        assert result["approval_type"] == "test_failure"
        # The dict payload must pass through sanitization untouched
        assert result["test_result"] == FAILED_TEST_RESULT
        assert result["remediation_hint"] == "retry-from-step:create_code"

    def test_unknown_type_still_rejected(self):
        with pytest.raises(ValueError, match="Invalid approval type"):
            handler.validate_approval_request(
                {"approval_type": "test_faliure", "pipeline_id": "pipe-1", "run_id": RUN_ID}
            )


class TestGetApprovalDataTestFailure:
    """get_approval_data('test_failure', ...) carries the full report for the dashboard."""

    def test_report_taken_from_inline_event_payload(self):
        event = {
            "approval_type": "test_failure",
            "run_id": RUN_ID,
            "test_result": FAILED_TEST_RESULT,
            "remediation_hint": "retry-from-step:create_code",
        }

        data = handler.get_approval_data("test_failure", RUN_ID, event)

        assert data["test_result"] == FAILED_TEST_RESULT
        assert data["test_status"] == "failed"
        assert data["failed_checks"] == ["transform_execution", "output_validation"]
        assert data["report_s3_uri"] == "s3://test-outputs/run/test_report.md"
        assert data["remediation_hint"] == "retry-from-step:create_code"
        # Inline payload used — no state-table read needed
        mock_state_table.get_item.assert_not_called()

    def test_fallback_reads_test_code_state_row(self):
        """P28 R8.2 (task 14.2): the fallback reads the canonical test_code row."""
        mock_state_table.get_item.return_value = {
            "Item": {"run_id": RUN_ID, "step_name": "test_code", "test_result": FAILED_TEST_RESULT}
        }

        data = handler.get_approval_data("test_failure", RUN_ID, {"approval_type": "test_failure"})

        mock_state_table.get_item.assert_called_once_with(Key={"run_id": RUN_ID, "step_name": "test_code"})
        assert data["test_result"] == FAILED_TEST_RESULT
        assert data["failed_checks"] == ["transform_execution", "output_validation"]
        # Default hint documents the p16 remediation path
        assert data["remediation_hint"] == "retry-from-step:create_code"

    def test_missing_report_everywhere_is_honest(self):
        mock_state_table.get_item.return_value = {}

        data = handler.get_approval_data("test_failure", RUN_ID, None)

        assert data["test_result"] is None
        assert data["test_status"] == "unknown"
        assert data["remediation_hint"] == "retry-from-step:create_code"
