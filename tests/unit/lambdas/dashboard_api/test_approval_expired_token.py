# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for error handling when task tokens are missing or expired.

Validates AC-7.4 error path: approve/reject with has_task_token=True but
token not found in task_tokens table returns 410 with clear error.
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

_table_map = {
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    from handlers import approvals as approvals_mod


APPROVAL_ID = "approval-expired-001"


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.approvals_table = mock_approvals_table
    yield


def _approval_record_with_token():
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": True,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApproveExpiredToken:
    """Approve with has_task_token=True but token not found → returns 410."""

    def test_approve_missing_token_returns_410(self):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record_with_token()}
        # Token not found — get_item returns no Item
        mock_task_tokens_table.get_item.return_value = {}

        result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 410
        body = json.loads(result["body"])
        assert "error" in body
        assert "expired" in body["error"].lower() or "not found" in body["error"].lower()
        assert body["approval_id"] == APPROVAL_ID


class TestRejectExpiredToken:
    """Reject with expired/missing token → returns 410."""

    def test_reject_missing_token_returns_410(self):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record_with_token()}
        # Token expired/deleted by TTL — get_item returns no Item
        mock_task_tokens_table.get_item.return_value = {}

        result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "test"}))

        assert result["statusCode"] == 410
        body = json.loads(result["body"])
        assert "error" in body
        assert body["approval_id"] == APPROVAL_ID
