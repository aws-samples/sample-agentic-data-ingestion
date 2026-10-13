# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the costs aggregation handler.

Covers:
- Aggregation with multiple runs (total, average, most_expensive, run_count)
- Empty pipeline (zero costs, null most_expensive_run)
- cost_trend limited to 7 entries
- most_expensive_run selection picks the highest cost

Validates: Requirements 3.1, 3.2, 3.3, 3.4
"""

import json
import os
import sys
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

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("STATE_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-cost-001"


def _parse(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


def _make_run_item(run_id: str, actual_cost: float, timestamp: str) -> dict:
    """Build a State_Table item representing a RunPipeline step with cost."""
    return {
        "run_id": run_id,
        "step_name": "RunPipeline",
        "pipeline_id": PIPELINE_ID,
        "timestamp": timestamp,
        "output_data": {"actual_cost": actual_cost},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_state_table(monkeypatch):
    """Patch the state_table used by the costs handler."""
    from handlers import common as _common

    mock_table = MagicMock()
    monkeypatch.setattr(_common, "state_table", mock_table)

    from handlers import costs as _costs

    monkeypatch.setattr(_costs, "state_table", mock_table)
    _patch_state_table.table = mock_table
    yield


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.costs import get_pipeline_costs

# ===========================================================================
# Tests
# ===========================================================================


class TestAggregationMultipleRuns:
    """Validates Req 3.1, 3.2, 3.4: cost aggregation across multiple runs."""

    def test_aggregates_costs_correctly(self):
        items = [
            _make_run_item("run-1", 1.50, "2025-01-10T10:00:00Z"),
            _make_run_item("run-2", 2.50, "2025-01-11T10:00:00Z"),
            _make_run_item("run-3", 3.00, "2025-01-12T10:00:00Z"),
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["total_cost"] == 7.0
        assert body["average_cost_per_run"] == round(7.0 / 3, 3)
        assert body["run_count"] == 3

    def test_skips_items_without_actual_cost(self):
        items = [
            _make_run_item("run-1", 5.0, "2025-01-10T10:00:00Z"),
            {
                "run_id": "run-2",
                "step_name": "RunPipeline",
                "pipeline_id": PIPELINE_ID,
                "timestamp": "2025-01-11T10:00:00Z",
                "output_data": {},  # no actual_cost
            },
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["total_cost"] == 5.0
        assert body["run_count"] == 1


class TestEmptyPipeline:
    """Validates Req 3.3: empty pipeline returns zero costs."""

    def test_returns_zero_costs_when_no_runs(self):
        _patch_state_table.table.query.return_value = {"Items": []}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["total_cost"] == 0
        assert body["average_cost_per_run"] == 0
        assert body["cost_trend"] == []
        assert body["most_expensive_run"] is None
        assert body["run_count"] == 0

    def test_returns_zero_when_no_items_have_cost(self):
        items = [
            {
                "run_id": "run-1",
                "step_name": "RunPipeline",
                "pipeline_id": PIPELINE_ID,
                "timestamp": "2025-01-10T10:00:00Z",
                "output_data": {},
            },
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["total_cost"] == 0
        assert body["run_count"] == 0
        assert body["most_expensive_run"] is None


class TestCostTrendLimitedToSeven:
    """Validates Req 3.2: cost_trend contains at most 7 entries."""

    def test_trend_limited_to_seven_most_recent(self):
        items = [
            _make_run_item(f"run-{i}", float(i), f"2025-01-{i + 1:02d}T10:00:00Z")
            for i in range(1, 11)  # 10 runs
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert len(body["cost_trend"]) == 7
        assert body["run_count"] == 10
        # Trend should be sorted newest first — run-10 first
        assert body["cost_trend"][0]["run_id"] == "run-10"
        assert body["cost_trend"][6]["run_id"] == "run-4"

    def test_trend_returns_all_when_fewer_than_seven(self):
        items = [
            _make_run_item("run-1", 1.0, "2025-01-01T10:00:00Z"),
            _make_run_item("run-2", 2.0, "2025-01-02T10:00:00Z"),
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert len(body["cost_trend"]) == 2


class TestMostExpensiveRunSelection:
    """Validates Req 3.2: most_expensive_run picks the highest cost."""

    def test_selects_most_expensive_run(self):
        items = [
            _make_run_item("run-cheap", 0.50, "2025-01-10T10:00:00Z"),
            _make_run_item("run-expensive", 99.99, "2025-01-11T10:00:00Z"),
            _make_run_item("run-mid", 10.0, "2025-01-12T10:00:00Z"),
        ]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["most_expensive_run"]["run_id"] == "run-expensive"
        assert body["most_expensive_run"]["actual_cost"] == 99.99

    def test_single_run_is_most_expensive(self):
        items = [_make_run_item("run-only", 42.0, "2025-01-10T10:00:00Z")]
        _patch_state_table.table.query.return_value = {"Items": items}

        status, body = _parse(get_pipeline_costs(PIPELINE_ID))

        assert status == 200
        assert body["most_expensive_run"]["run_id"] == "run-only"
        assert body["most_expensive_run"]["actual_cost"] == 42.0
