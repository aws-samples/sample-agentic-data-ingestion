# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for per-run actual_cost enrichment in get_pipeline_executions.

Validates: Requirements 4.1, 4.2, 4.3
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("AWS_REGION", "us-west-2")

PIPELINE_ID = "pipe-cost-test-001"
STATE_TABLE_NAME = "agentic_data_ingestion_state"


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _make_execution(run_id: str, status: str = "SUCCEEDED") -> dict:
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": run_id,
        "status": status,
        "current_step": "RunPipeline",
        "steps_status": {"file_validation": "completed"},
        "updated_at": "2025-01-15T12:05:30+00:00",
        "started_at": "2025-01-15T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    # Guard against test-ordering pollution: other test files may replace
    # handlers.* in sys.modules with mock-tainted copies.  Force a fresh
    # import to get modules bound to real boto3.
    import importlib

    # P21 Task 7.2: get_pipeline_executions now lives in handlers.pipelines_runs.
    # Reload common + the owning module to shed any mock-tainted bindings other
    # test files may have installed. Guard each reload so a missing namespace
    # parent (test-ordering artefact) doesn't error the fixture.
    #
    # The reload re-reads STATE_TABLE, and the responses below are keyed by
    # STATE_TABLE_NAME. The module-scope setdefault above is a no-op once an
    # earlier file has set STATE_TABLE to its own name, so pin it for the reload.
    # This used to pass only because a leaked env_utils MagicMock ignored the
    # env var and returned the default (LGR-c58f276-4).
    monkeypatch.setenv("STATE_TABLE", STATE_TABLE_NAME)
    for _name in ("handlers.common", "handlers.pipelines_runs", "handlers.pipelines"):
        _mod = sys.modules.get(_name)
        if _mod is not None and "handlers" in sys.modules:
            try:
                importlib.reload(_mod)
            except ImportError:
                pass

    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_exec = MagicMock()
    mock_state = MagicMock()
    mock_dynamodb = MagicMock()

    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "dynamodb", mock_dynamodb)
    # The owning module (handlers.pipelines_runs) is where get_pipeline_executions
    # reads its table handles, so patch the mocks there.
    monkeypatch.setattr(_runs, "executions_table", mock_exec)
    monkeypatch.setattr(_runs, "state_table", mock_state)
    monkeypatch.setattr(_runs, "dynamodb", mock_dynamodb)
    mock_state.query.return_value = {"Items": []}
    # Default: batch_get_item returns empty
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    _patch_tables.exec_table = mock_exec
    _patch_tables.state_table = mock_state
    _patch_tables.dynamodb = mock_dynamodb
    # Expose the freshly-loaded function for tests to call
    _patch_tables.get_pipeline_executions = _runs.get_pipeline_executions


# ===========================================================================
# Tests
# ===========================================================================


class TestActualCostPresent:
    """Req 4.1: Executions list includes actual_cost field on each execution."""

    def test_actual_cost_appears_on_execution_with_cost_data(self):
        """When RunPipeline record has actual_cost, it appears on the execution."""
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.return_value = {
            "Responses": {
                STATE_TABLE_NAME: [
                    {
                        "run_id": "run-001",
                        "output_data": {"actual_cost": Decimal("1.234")},
                    }
                ]
            },
            "UnprocessedKeys": {},
        }

        status, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 1
        assert body["executions"][0]["actual_cost"] == 1.234

    def test_actual_cost_on_multiple_executions(self):
        """Each execution gets its own actual_cost from its RunPipeline record."""
        _patch_tables.exec_table.query.return_value = {
            "Items": [
                _make_execution("run-001"),
                _make_execution("run-002"),
                _make_execution("run-003"),
            ]
        }
        _patch_tables.dynamodb.batch_get_item.return_value = {
            "Responses": {
                STATE_TABLE_NAME: [
                    {"run_id": "run-001", "output_data": {"actual_cost": Decimal("1.5")}},
                    {"run_id": "run-003", "output_data": {"actual_cost": Decimal("3.75")}},
                ]
            },
            "UnprocessedKeys": {},
        }

        _, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        costs = {e["run_id"]: e["actual_cost"] for e in body["executions"]}
        assert costs["run-001"] == 1.5
        assert costs["run-002"] is None  # no RunPipeline record
        assert costs["run-003"] == 3.75


class TestActualCostNull:
    """Req 4.3: actual_cost is null when no RunPipeline record or no actual_cost."""

    def test_actual_cost_null_when_no_run_pipeline_record(self):
        """When batch_get_item returns no record for a run, actual_cost is null."""
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.return_value = {
            "Responses": {},
            "UnprocessedKeys": {},
        }

        _, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert body["executions"][0]["actual_cost"] is None

    def test_actual_cost_null_when_output_data_has_no_cost(self):
        """When RunPipeline record exists but output_data lacks actual_cost."""
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.return_value = {
            "Responses": {
                STATE_TABLE_NAME: [
                    {"run_id": "run-001", "output_data": {"dpu_hours": Decimal("2.0")}},
                ]
            },
            "UnprocessedKeys": {},
        }

        _, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert body["executions"][0]["actual_cost"] is None

    def test_actual_cost_null_when_no_executions(self):
        """Empty executions list — no cost enrichment needed."""
        _patch_tables.exec_table.query.return_value = {"Items": []}

        _, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert body["executions"] == []


class TestCostEnrichmentResilience:
    """Cost enrichment failures don't break the response."""

    def test_batch_get_item_failure_still_returns_executions(self):
        """If batch_get_item raises, executions still returned with null costs."""
        _patch_tables.exec_table.query.return_value = {"Items": [_make_execution("run-001")]}
        _patch_tables.dynamodb.batch_get_item.side_effect = Exception("DynamoDB error")

        status, body = _parse(_patch_tables.get_pipeline_executions(PIPELINE_ID))

        assert status == 200
        assert len(body["executions"]) == 1
        assert body["executions"][0]["actual_cost"] is None
