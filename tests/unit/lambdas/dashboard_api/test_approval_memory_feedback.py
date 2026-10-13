# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for Memory feedback integration in approval/rejection handlers.

Validates AC-12, AC-13: approved and rejected schema mapping decisions
are stored in AgentCore Memory via MappingMemoryClient.
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
os.environ.setdefault("MEMORY_ENABLED", "false")  # Disable real Memory calls

# ---------------------------------------------------------------------------
# Mock setup
# ---------------------------------------------------------------------------
mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()
mock_sfn_client = MagicMock()
mock_memory_client = MagicMock()

_table_map = {
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    mock_client.return_value = MagicMock()
    from handlers import approvals as approvals_mod


APPROVAL_ID = "approval-memory-001"
TASK_TOKEN = "memory-test-token-value"

PROPOSED_MAPPINGS = [
    {"source_field": "cust_id", "target_field": "customer_id", "confidence": 95},
    {"source_field": "fname", "target_field": "first_name", "confidence": 88},
]


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_sfn_client.reset_mock()
    mock_memory_client.reset_mock()
    mock_memory_client.store_approval_feedback.return_value = True
    mock_memory_client.store_mapping_decision.return_value = True
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.approvals_table = mock_approvals_table
    yield


def _schema_mapping_approval(has_task_token=False):
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "approval_type": "schema_mapping",
        "has_task_token": has_task_token,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "proposed_mappings": PROPOSED_MAPPINGS,
    }


def _code_generation_approval():
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "approval_type": "code_generation",
        "has_task_token": False,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
    }


# ---------------------------------------------------------------------------
# Tests: Approve stores Memory feedback
# ---------------------------------------------------------------------------


class TestApproveStoresMemoryFeedback:
    """AC-12: Approved schema mapping decisions are stored in Memory."""

    def test_approve_schema_mapping_stores_feedback(self):
        mock_approvals_table.get_item.return_value = {"Item": _schema_mapping_approval()}

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_memory_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "reviewer"}))

        assert result["statusCode"] == 200

        # store_approval_feedback called with approved=True
        mock_memory_client.store_approval_feedback.assert_called_once_with(APPROVAL_ID, approved=True)

        # store_mapping_decision called for each proposed mapping with approved=True
        approval_data = _schema_mapping_approval()
        proposed = approval_data.get("proposed_mappings", [])
        assert mock_memory_client.store_mapping_decision.call_count == len(proposed)
        for mapping in proposed:
            mock_memory_client.store_mapping_decision.assert_any_call(
                source_field=mapping["source_field"],
                target_field=mapping["target_field"],
                confidence=mapping.get("confidence", 0.5),
                pipeline_id="pipe-001",
                approved=True,
            )

    def test_approve_non_schema_mapping_skips_memory(self):
        """Memory feedback is only stored for schema_mapping approval_type."""
        mock_approvals_table.get_item.return_value = {"Item": _code_generation_approval()}

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_memory_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "reviewer"}))

        assert result["statusCode"] == 200
        mock_memory_client.store_approval_feedback.assert_not_called()
        mock_memory_client.store_mapping_decision.assert_not_called()

    def test_approve_memory_failure_does_not_break_approval(self):
        """Memory errors are logged but don't fail the approval."""
        mock_approvals_table.get_item.return_value = {"Item": _schema_mapping_approval()}
        failing_client = MagicMock()
        failing_client.store_approval_feedback.side_effect = Exception("Memory unavailable")

        with patch("handlers.approvals.MappingMemoryClient", return_value=failing_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "reviewer"}))

        # Approval still succeeds
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True


# ---------------------------------------------------------------------------
# Tests: Reject stores Memory feedback
# ---------------------------------------------------------------------------


class TestRejectStoresMemoryFeedback:
    """AC-13: Rejected schema mapping decisions are stored in Memory with reason."""

    def test_reject_schema_mapping_stores_feedback_with_reason(self):
        mock_approvals_table.get_item.return_value = {"Item": _schema_mapping_approval()}

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_memory_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.reject_request(
                    APPROVAL_ID,
                    json.dumps({"user": "reviewer", "reason": "wrong target field"}),
                )

        assert result["statusCode"] == 200

        # store_approval_feedback called with approved=False and reviewer_notes
        mock_memory_client.store_approval_feedback.assert_called_once_with(
            APPROVAL_ID, approved=False, reviewer_notes="wrong target field"
        )

        # store_mapping_decision called for each proposed mapping with approved=False
        assert mock_memory_client.store_mapping_decision.call_count == 2
        mock_memory_client.store_mapping_decision.assert_any_call(
            source_field="cust_id",
            target_field="customer_id",
            confidence=95,
            pipeline_id="pipe-001",
            approved=False,
        )

    def test_reject_non_schema_mapping_skips_memory(self):
        mock_approvals_table.get_item.return_value = {"Item": _code_generation_approval()}

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_memory_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.reject_request(
                    APPROVAL_ID,
                    json.dumps({"user": "reviewer", "reason": "bad code"}),
                )

        assert result["statusCode"] == 200
        mock_memory_client.store_approval_feedback.assert_not_called()

    def test_reject_memory_failure_does_not_break_rejection(self):
        mock_approvals_table.get_item.return_value = {"Item": _schema_mapping_approval()}
        failing_client = MagicMock()
        failing_client.store_approval_feedback.side_effect = Exception("Memory down")

        with patch("handlers.approvals.MappingMemoryClient", return_value=failing_client):
            with patch("boto3.client") as patched_client:
                patched_client.return_value = mock_sfn_client
                result = approvals_mod.reject_request(
                    APPROVAL_ID,
                    json.dumps({"user": "reviewer", "reason": "bad mapping"}),
                )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True
