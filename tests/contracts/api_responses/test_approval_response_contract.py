# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: Approval response shape.

Validates that approval records returned by get_approvals contain the
fields expected by the frontend Approval TypeScript interface, including
the critical has_task_token boolean.

Task 7.2 — Requirements 4.5, 4.8
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import approvals as _approvals
from handlers import common as _common
from handlers.approvals import get_approvals, get_pending_approvals


def _make_approval(approval_id="appr-001", status="pending", has_token=True):
    """Build a realistic approval DynamoDB item."""
    return {
        "approval_id": approval_id,
        "pipeline_id": "pipe-001",
        "pipeline_key": "pipe-001",
        "run_id": "run-001",
        "approval_type": "schema_mapping",
        "status": status,
        "created_at": "2025-01-15T12:00:00Z",
        "has_task_token": has_token,
        "gate_name": "schema_mapping_approval",
    }


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    mock_approvals = MagicMock()
    monkeypatch.setattr(_common, "approvals_table", mock_approvals)
    monkeypatch.setattr(_approvals, "approvals_table", mock_approvals)
    _patch_tables.approvals_table = mock_approvals


class TestApprovalResponseContract:
    """Validate approval records match the Approval TypeScript interface."""

    def test_approval_has_required_fields(self):
        _patch_tables.approvals_table.query.return_value = {"Items": [_make_approval()]}
        status, body = _parse(get_approvals({"status": "pending"}))
        assert status == 200
        assert isinstance(body, list)
        approval = body[0]
        required = {"approval_id", "approval_type", "status", "created_at"}
        missing = required - set(approval.keys())
        assert not missing, f"Approval missing required fields: {missing}"

    def test_has_task_token_is_boolean(self):
        """The has_task_token field must be a boolean — critical for approval flow."""
        _patch_tables.approvals_table.query.return_value = {"Items": [_make_approval(has_token=True)]}
        _, body = _parse(get_approvals({}))
        approval = body[0]
        assert "has_task_token" in approval, "Approval must include has_task_token"
        assert isinstance(approval["has_task_token"], bool)

    def test_approval_field_types(self):
        _patch_tables.approvals_table.query.return_value = {"Items": [_make_approval()]}
        _, body = _parse(get_approvals({}))
        a = body[0]
        assert isinstance(a["approval_id"], str)
        assert isinstance(a["approval_type"], str)
        assert isinstance(a["status"], str)
        assert isinstance(a["created_at"], str)

    def test_pending_approvals_returns_list(self):
        _patch_tables.approvals_table.query.return_value = {"Items": [_make_approval(), _make_approval("appr-002")]}
        status, body = _parse(get_pending_approvals())
        assert status == 200
        assert isinstance(body, list)
        assert len(body) == 2

    def test_empty_approvals(self):
        _patch_tables.approvals_table.query.return_value = {"Items": []}
        status, body = _parse(get_approvals({}))
        assert status == 200
        assert body == []
