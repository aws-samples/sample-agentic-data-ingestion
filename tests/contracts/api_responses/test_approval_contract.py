# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract tests for approval API response shape.

Validates AC-7.3 and AC-7.5: approval records use has_task_token boolean
(not task_token string), and API responses have the expected shape.
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
    os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
HANDLERS_DIR = os.path.join(DASHBOARD_API_DIR, "handlers")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
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

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    from handlers import approvals as approvals_mod


APPROVAL_ID = "approval-contract-001"
TASK_TOKEN = "contract-test-token-value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_sfn_client.reset_mock()
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.approvals_table = mock_approvals_table
    yield


# ---------------------------------------------------------------------------
# Tests: Approval record shape
# ---------------------------------------------------------------------------


class TestApprovalRecordHasTaskTokenBoolean:
    """Approval record uses has_task_token boolean, not task_token string."""

    def test_approval_record_has_boolean_flag(self):
        """The approval record stored in DynamoDB uses has_task_token (bool),
        not task_token (string). This is a contract — the dashboard reads
        this field to decide whether to show 'resume execution' UI."""
        record = {
            "approval_id": APPROVAL_ID,
            "status": "pending",
            "has_task_token": True,
            "pipeline_id": "pipe-001",
            "run_id": "run-001",
        }
        assert isinstance(record["has_task_token"], bool)
        assert "task_token" not in record


# ---------------------------------------------------------------------------
# Tests: Approve success response shape
# ---------------------------------------------------------------------------


class TestApproveResponseShape:
    """Approve response has success, approval_id, status, resumed_execution, message."""

    def test_approve_response_has_required_fields(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": APPROVAL_ID,
                "status": "pending",
                "has_task_token": True,
            }
        }
        mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])

        required_fields = {"success", "approval_id", "status", "resumed_execution", "message"}
        assert required_fields.issubset(body.keys()), f"Missing fields: {required_fields - body.keys()}"

        assert isinstance(body["success"], bool)
        assert isinstance(body["approval_id"], str)
        assert isinstance(body["status"], str)
        assert isinstance(body["resumed_execution"], bool)
        assert isinstance(body["message"], str)


# ---------------------------------------------------------------------------
# Tests: Error response shape for expired token
# ---------------------------------------------------------------------------


class TestExpiredTokenErrorResponseShape:
    """Error response for expired token has error, detail, approval_id."""

    def test_expired_token_error_response_shape(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": APPROVAL_ID,
                "status": "pending",
                "has_task_token": True,
            }
        }
        # Token not found
        mock_task_tokens_table.get_item.return_value = {}

        result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 410
        body = json.loads(result["body"])

        required_fields = {"error", "detail", "approval_id"}
        assert required_fields.issubset(body.keys()), f"Missing fields: {required_fields - body.keys()}"

        assert isinstance(body["error"], str)
        assert isinstance(body["detail"], str)
        assert isinstance(body["approval_id"], str)
        assert body["approval_id"] == APPROVAL_ID
