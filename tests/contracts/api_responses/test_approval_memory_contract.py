# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test: approval/rejection of schema_mapping type stores Memory feedback.

Validates that the approval handler calls MappingMemoryClient for schema_mapping
approvals and that non-schema_mapping types are unaffected.
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

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")
os.environ.setdefault("MEMORY_ENABLED", "false")

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

APPROVAL_ID = "contract-memory-001"


@pytest.fixture(autouse=True)
def _reset():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.approvals_table = mock_approvals_table
    yield


class TestApprovalMemoryContract:
    """Contract: schema_mapping approvals trigger Memory feedback; others do not."""

    def test_schema_mapping_approval_calls_memory(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": APPROVAL_ID,
                "status": "pending",
                "approval_type": "schema_mapping",
                "has_task_token": False,
                "pipeline_id": "pipe-c",
                "run_id": "run-c",
                "proposed_mappings": [
                    {"source_field": "id", "target_field": "record_id", "confidence": 90},
                ],
            }
        }
        mock_mem = MagicMock()
        mock_mem.store_approval_feedback.return_value = True
        mock_mem.store_mapping_decision.return_value = True

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_mem), patch("boto3.client"):
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 200
        mock_mem.store_approval_feedback.assert_called_once()
        # Mapping decisions stored with approved=True at approval time
        mock_mem.store_mapping_decision.assert_called_once_with(
            source_field="id",
            target_field="record_id",
            confidence=90,
            pipeline_id="pipe-c",
            approved=True,
        )

    def test_schema_mapping_rejection_calls_memory_with_notes(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": APPROVAL_ID,
                "status": "pending",
                "approval_type": "schema_mapping",
                "has_task_token": False,
                "pipeline_id": "pipe-c",
                "run_id": "run-c",
                "proposed_mappings": [
                    {"source_field": "id", "target_field": "record_id", "confidence": 90},
                ],
            }
        }
        mock_mem = MagicMock()
        mock_mem.store_approval_feedback.return_value = True
        mock_mem.store_mapping_decision.return_value = True

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_mem), patch("boto3.client"):
            result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "wrong field"}))

        assert result["statusCode"] == 200
        mock_mem.store_approval_feedback.assert_called_once_with(
            APPROVAL_ID, approved=False, reviewer_notes="wrong field"
        )

    def test_code_generation_approval_does_not_call_memory(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": APPROVAL_ID,
                "status": "pending",
                "approval_type": "code_generation",
                "has_task_token": False,
                "pipeline_id": "pipe-c",
                "run_id": "run-c",
            }
        }
        mock_mem = MagicMock()

        with patch("handlers.approvals.MappingMemoryClient", return_value=mock_mem), patch("boto3.client"):
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 200
        mock_mem.store_approval_feedback.assert_not_called()
        mock_mem.store_mapping_decision.assert_not_called()
