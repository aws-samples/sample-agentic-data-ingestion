# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /pipeline/{id}/queued-files response shape.

Validates the QueuedFilesResponse interface: queued_files array and count.
Also validates that the backward-compat endpoint delegates to executions.

Task 7.2 — Requirements 4.3, 4.8
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

from handlers import common as _common
from handlers import pipelines_runs as _pipelines  # P21 Task 7.2 — runs handlers live here
from handlers.pipelines import get_queued_files

PIPELINE_ID = "pipe-queued-001"


def _make_queued_item(file_name="data.csv"):
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": f"run-queued-{file_name}",
        "status": "QUEUED",
        "file_name": file_name,
        "source_key": f"s3://source/{PIPELINE_ID}/{file_name}",
        "started_at": "2025-01-15T12:00:00Z",
    }


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    mock_exec = MagicMock()
    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_dynamodb = MagicMock()

    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "approvals_table", mock_approvals)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)
    monkeypatch.setattr(_pipelines, "executions_table", mock_exec)
    monkeypatch.setattr(_pipelines, "state_table", mock_state)
    monkeypatch.setattr(_pipelines, "approvals_table", mock_approvals)
    if hasattr(_pipelines, "dynamodb"):
        monkeypatch.setattr(_pipelines, "dynamodb", mock_dynamodb)

    # P21 Task 7.2: get_pipeline_executions / get_queued_files now live in
    # handlers.pipelines_runs; mirror the table mocks onto the owning module
    # (module references only — behaviour-preserving).
    from handlers import pipelines_runs as _runs

    monkeypatch.setattr(_runs, "executions_table", mock_exec)
    monkeypatch.setattr(_runs, "state_table", mock_state)
    monkeypatch.setattr(_runs, "approvals_table", mock_approvals)
    monkeypatch.setattr(_runs, "dynamodb", mock_dynamodb)

    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    _patch_tables.exec_table = mock_exec
    _patch_tables.state_table = mock_state
    _patch_tables.approvals_table = mock_approvals
    _patch_tables.dynamodb = mock_dynamodb


class TestQueuedFilesResponseContract:
    """Validate GET /pipeline/{id}/queued-files backward-compat response shape."""

    def test_response_has_queued_files_and_count(self):
        _patch_tables.exec_table.query.return_value = {
            "Items": [_make_queued_item("a.csv"), _make_queued_item("b.csv")]
        }
        _patch_tables.approvals_table.scan.return_value = {"Items": []}
        status, body = _parse(get_queued_files(PIPELINE_ID))
        assert status == 200
        assert "queued_files" in body
        assert "count" in body
        assert isinstance(body["queued_files"], list)
        assert isinstance(body["count"], int)

    def test_each_queued_file_has_required_fields(self):
        _patch_tables.exec_table.query.return_value = {"Items": [_make_queued_item("data.csv")]}
        _patch_tables.approvals_table.scan.return_value = {"Items": []}
        _, body = _parse(get_queued_files(PIPELINE_ID))
        for qf in body["queued_files"]:
            required = {"file_name", "source_key", "queued_at", "status"}
            missing = required - set(qf.keys())
            assert not missing, f"Queued file missing fields: {missing}"

    def test_queued_file_field_types(self):
        _patch_tables.exec_table.query.return_value = {"Items": [_make_queued_item()]}
        _patch_tables.approvals_table.scan.return_value = {"Items": []}
        _, body = _parse(get_queued_files(PIPELINE_ID))
        qf = body["queued_files"][0]
        assert isinstance(qf["file_name"], str)
        assert isinstance(qf["source_key"], str)
        assert isinstance(qf["queued_at"], str)
        assert isinstance(qf["status"], str)

    def test_empty_queue(self):
        _patch_tables.exec_table.query.return_value = {"Items": []}
        status, body = _parse(get_queued_files(PIPELINE_ID))
        assert status == 200
        assert body["queued_files"] == []
        assert body["count"] == 0

    def test_count_matches_array_length(self):
        _patch_tables.exec_table.query.return_value = {
            "Items": [_make_queued_item("a.csv"), _make_queued_item("b.csv"), _make_queued_item("c.csv")]
        }
        _patch_tables.approvals_table.scan.return_value = {"Items": []}
        _, body = _parse(get_queued_files(PIPELINE_ID))
        assert body["count"] == len(body["queued_files"])
