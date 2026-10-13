# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /pipelines response matches PaginatedPipelines interface.

Seeds DynamoDB data, calls the actual handler, and validates the response
against the JSON Schema derived from the frontend TypeScript interfaces.

Task 7.2 — Requirements 4.1, 4.8
"""

import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required at import time
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

from unittest.mock import MagicMock

from handlers import common as _common

# P21 Task 7.2: get_pipelines / edit_pipeline now live in handlers.pipelines_crud.
# Bind the patch target there so the table mocks reach the relocated functions
# (module references only — behaviour-preserving).
from handlers import pipelines_crud as _pipelines
from handlers.pipelines import get_pipelines

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_pipeline_item():
    """Return a realistic pipeline DynamoDB item."""
    return {
        "pipeline_id": "pipe-test-001",
        "pipeline_name": "Customer Data Pipeline",
        "status": "active",
        "target_schema_name": "customer_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "pipeline_description": "Test pipeline",
        "created_at": "2025-01-10T08:00:00Z",
        "updated_at": "2025-01-15T12:00:00Z",
        "glue_job_metadata": {},
    }


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    """Patch DynamoDB tables on the handler modules with mocks."""
    mock_pipelines = MagicMock()
    mock_state = MagicMock()
    mock_executions = MagicMock()
    mock_approvals = MagicMock()
    mock_mappings = MagicMock()

    # Pipelines table scan returns one pipeline
    mock_pipelines.scan.return_value = {"Items": [_seed_pipeline_item()]}

    # State table query returns empty (no schema history / files)
    mock_state.query.return_value = {"Items": [], "ScannedCount": 0}
    mock_state.get_item.return_value = {}

    # Executions table query returns empty
    mock_executions.query.return_value = {"Items": []}

    # Approvals table scan returns empty
    mock_approvals.scan.return_value = {"Items": []}

    # Patch on both common and pipelines modules
    monkeypatch.setattr(_common, "pipelines_table", mock_pipelines)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "executions_table", mock_executions)
    monkeypatch.setattr(_common, "approvals_table", mock_approvals)
    monkeypatch.setattr(_common, "mappings_table", mock_mappings)

    monkeypatch.setattr(_pipelines, "pipelines_table", mock_pipelines)
    monkeypatch.setattr(_pipelines, "state_table", mock_state)
    monkeypatch.setattr(_pipelines, "executions_table", mock_executions)
    monkeypatch.setattr(_pipelines, "approvals_table", mock_approvals)
    monkeypatch.setattr(_pipelines, "mappings_table", mock_mappings)

    # The audit writer is fire-and-forget but would otherwise attempt a real
    # boto3 PutItem from inside a contract test — stub it out entirely.
    monkeypatch.setattr(_pipelines, "write_audit_entry", MagicMock())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


class TestPipelinesResponseContract:
    """Validate GET /pipelines response matches PaginatedPipelines schema."""

    def test_response_validates_against_paginated_schema(self):
        status, body = _parse(get_pipelines())
        assert status == 200
        # Validate the wrapper shape
        assert "pipelines" in body
        assert "total_count" in body
        assert "page" in body
        assert "page_size" in body
        assert "total_pages" in body
        assert "has_more" in body

    def test_each_pipeline_has_required_fields(self):
        status, body = _parse(get_pipelines())
        assert status == 200
        for pipeline in body["pipelines"]:
            required = {"pipeline_key", "pipeline_name", "status", "created_at", "updated_at"}
            missing = required - set(pipeline.keys())
            assert not missing, f"Pipeline missing required fields: {missing}"

    def test_pipeline_field_types(self):
        status, body = _parse(get_pipelines())
        assert status == 200
        for pipeline in body["pipelines"]:
            assert isinstance(pipeline["pipeline_key"], str)
            assert isinstance(pipeline["pipeline_name"], str)
            assert isinstance(pipeline["status"], str)
            assert isinstance(pipeline["created_at"], str)
            assert isinstance(pipeline["updated_at"], str)

    def test_empty_response_validates(self):
        """Empty pipeline list still has correct wrapper shape."""
        # Override scan to return empty
        _pipelines.pipelines_table.scan.return_value = {"Items": []}
        status, body = _parse(get_pipelines())
        assert status == 200
        assert body["pipelines"] == []
        assert body["total_count"] == 0
        assert isinstance(body["has_more"], bool)


class TestEditPipelinePartialUpdate:
    """Validate edit_pipeline uses partial target_config update (AC-23, AC-24)."""

    def test_partial_target_config_does_not_wipe_fields(self):
        """Sending { write_mode: 'append' } should NOT replace the entire target_config map."""
        from handlers.pipelines import edit_pipeline

        # Track the update_item call to verify SET expressions
        calls = []
        original_update = _pipelines.pipelines_table.update_item

        def capture_update(**kwargs):
            calls.append(kwargs)
            return {}

        _pipelines.pipelines_table.update_item = capture_update

        try:
            body = json.dumps({"target_config": {"write_mode": "append"}})
            result = edit_pipeline("pipe-test-001", body)
            status, resp_body = _parse(result)

            assert status == 200
            # First call: if_not_exists initialization, second call: path-based SET
            assert len(calls) == 2

            # The main update (second call) should use path-based SET. The nested
            # key is aliased via ExpressionAttributeNames (reserved-word safety).
            update_expr = calls[1].get("UpdateExpression", "")
            expr_names = calls[1].get("ExpressionAttributeNames", {})
            assert "target_config.#tc_write_mode" in update_expr
            assert expr_names.get("#tc_write_mode") == "write_mode"
            assert "target_config = :tc" not in update_expr
        finally:
            _pipelines.pipelines_table.update_item = original_update

    def test_partial_approval_config_does_not_wipe_fields(self):
        """Sending partial approval_config should use path-based SET expressions."""
        from handlers.pipelines import edit_pipeline

        calls = []
        original_update = _pipelines.pipelines_table.update_item

        def capture_update(**kwargs):
            calls.append(kwargs)
            return {}

        _pipelines.pipelines_table.update_item = capture_update

        try:
            body = json.dumps({"approval_config": {"require_code_approval": True}})
            result = edit_pipeline("pipe-test-001", body)
            status, resp_body = _parse(result)

            assert status == 200
            # First call: if_not_exists initialization, second call: path-based SET
            assert len(calls) == 2

            # Nested key aliased via ExpressionAttributeNames (reserved-word safety).
            update_expr = calls[1].get("UpdateExpression", "")
            expr_names = calls[1].get("ExpressionAttributeNames", {})
            assert "approval_config.#ac_require_code_approval" in update_expr
            assert expr_names.get("#ac_require_code_approval") == "require_code_approval"
            assert "approval_config = :ac" not in update_expr
        finally:
            _pipelines.pipelines_table.update_item = original_update
