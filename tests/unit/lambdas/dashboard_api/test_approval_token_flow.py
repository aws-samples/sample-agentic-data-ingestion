# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the approve/reject flow in the dashboard API.

Validates AC-7.4: approve/reject reads token from task_tokens table,
calls send_task_success/send_task_failure, then deletes the token record.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
HANDLERS_DIR = os.path.join(DASHBOARD_API_DIR, "handlers")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(DASHBOARD_API_DIR))
sys.path.insert(0, os.path.abspath(HANDLERS_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup
# ---------------------------------------------------------------------------
mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()
mock_sfn_client = MagicMock()

_table_map = {
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    mock_client.return_value = MagicMock()
    from handlers import approvals


APPROVAL_ID = "approval-test-001"
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAAX9p_long_token_value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_sfn_client.reset_mock()
    # Wire up the task_tokens_table on the module
    approvals.task_tokens_table = mock_task_tokens_table
    approvals.approvals_table = mock_approvals_table
    yield


def _approval_record(has_task_token=True):
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": has_task_token,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "created_at": "2025-01-15T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Tests: Approve with task token
# ---------------------------------------------------------------------------


class TestApproveWithToken:
    """Approve: reads approval with has_task_token=True, retrieves token, calls send_task_success."""

    def test_approve_reads_token_and_resumes_sfn(self):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token=True)}
        mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["success"] is True
        assert body["resumed_execution"] is True

        # Token was read and deleted
        mock_task_tokens_table.get_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})

        # SFN was called
        mock_sfn_client.send_task_success.assert_called_once()
        sfn_call_kwargs = mock_sfn_client.send_task_success.call_args[1]
        assert sfn_call_kwargs["taskToken"] == TASK_TOKEN


# ---------------------------------------------------------------------------
# Tests: Reject with task token
# ---------------------------------------------------------------------------


class TestRejectWithToken:
    """Reject: reads token, calls send_task_failure, deletes token record."""

    def test_reject_reads_token_and_fails_sfn(self):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token=True)}
        mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "bad data"}))

        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["success"] is True

        # Token was read and deleted
        mock_task_tokens_table.get_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})

        # SFN failure was called
        mock_sfn_client.send_task_failure.assert_called_once()
        sfn_call_kwargs = mock_sfn_client.send_task_failure.call_args[1]
        assert sfn_call_kwargs["taskToken"] == TASK_TOKEN
        assert sfn_call_kwargs["error"] == "ApprovalRejected"


# ---------------------------------------------------------------------------
# Tests: Approve without task token
# ---------------------------------------------------------------------------


class TestApproveWithoutToken:
    """Approve without token: has_task_token=False, skips token retrieval, returns 200."""

    def test_approve_without_token_skips_retrieval(self):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token=False)}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["success"] is True
        assert body["resumed_execution"] is False

        # Token table was never touched
        mock_task_tokens_table.get_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()

        # SFN was not called
        mock_sfn_client.send_task_success.assert_not_called()
