# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for agent trace query handlers.

Covers:
- GET /api/runs/{runId}/agent-trace — all step traces for a run
- GET /api/runs/{runId}/agent-trace/{stepName} — single step trace
- GET /api/metrics/token-usage?period=7d — aggregate token usage
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
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

# Environment variables required by handlers/common.py at import time
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AUDIT_TABLE", "test-audit-table")
os.environ.setdefault("AGENT_TRACES_TABLE", "test-agent-traces-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RUN_ID = "run-2025-001"
STEP_NAME = "code_generation"
PIPELINE_ID = "pipe-123"


def _parse(response: dict) -> tuple:
    """Return (status_code, parsed_body) from a create_response dict."""
    return response["statusCode"], json.loads(response["body"])


# NOTE: The model IDs in these fixtures are illustrative sample trace data, not a
# primary-model selection. The canonical primary model is
# `us.anthropic.claude-sonnet-4-6` (P21 Req 1); these sample records intentionally
# retain a historical Claude Sonnet 4.5 ID to exercise trace parsing/serialization.
SAMPLE_TRACE_ITEM = {
    "run_id": RUN_ID,
    "step_name": STEP_NAME,
    "pipeline_id": PIPELINE_ID,
    "timestamp": "2025-06-15T12:00:00+00:00",
    "trace_data": {
        "model_calls": [
            {
                "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "input_tokens": 1250,
                "output_tokens": 340,
                "cache_read_tokens": 800,
                "duration_ms": 2340,
                "prompt_preview": "Generate PySpark transform...",
                "response_preview": "df_transformed = df.select(...",
            }
        ],
        "tool_calls": [
            {
                "tool_name": "create_glue_job",
                "duration_ms": 1200,
                "status": "success",
                "tool_params": '{"job_name": "agentic-data-ingestion-pipe-123"}',
                "result_preview": '{"job_name": "agentic-data-ingestion-pipe-123-etl"}',
            }
        ],
        "total_input_tokens": 1250,
        "total_output_tokens": 340,
        "total_cache_read_tokens": 800,
        "estimated_cost_usd": Decimal("0.0089"),
        "start_time": Decimal("1710500000.0"),
        "end_time": Decimal("1710500005.0"),
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "error": None,
    },
}

SAMPLE_TRACE_ITEM_2 = {
    "run_id": RUN_ID,
    "step_name": "schema_mapping",
    "pipeline_id": PIPELINE_ID,
    "timestamp": "2025-06-15T11:55:00+00:00",
    "trace_data": {
        "model_calls": [],
        "tool_calls": [],
        "total_input_tokens": 500,
        "total_output_tokens": 100,
        "total_cache_read_tokens": 0,
        "estimated_cost_usd": Decimal("0.003"),
        "start_time": Decimal("1710499900.0"),
        "end_time": Decimal("1710499902.0"),
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "error": None,
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_traces_table(monkeypatch):
    """Patch the agent_traces_table used by the handler."""
    from handlers import agent_traces as _mod

    mock_table = MagicMock()
    monkeypatch.setattr(_mod, "agent_traces_table", mock_table)
    _patch_traces_table.table = mock_table
    yield


# ---------------------------------------------------------------------------
# Import handlers under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.agent_traces import get_run_traces, get_step_trace, get_token_usage_metrics

# ===========================================================================
# Tests — get_run_traces
# ===========================================================================


class TestGetRunTraces:
    """GET /api/runs/{runId}/agent-trace"""

    def test_returns_all_traces_for_run(self):
        _patch_traces_table.table.query.return_value = {
            "Items": [SAMPLE_TRACE_ITEM, SAMPLE_TRACE_ITEM_2],
        }

        status, body = _parse(get_run_traces(RUN_ID))

        assert status == 200
        assert body["run_id"] == RUN_ID
        assert body["count"] == 2
        assert len(body["traces"]) == 2
        assert body["traces"][0]["step_name"] == STEP_NAME
        assert body["traces"][1]["step_name"] == "schema_mapping"

    def test_returns_empty_when_no_traces(self):
        _patch_traces_table.table.query.return_value = {"Items": []}

        status, body = _parse(get_run_traces(RUN_ID))

        assert status == 200
        assert body["traces"] == []
        assert body["count"] == 0

    def test_returns_400_for_empty_run_id(self):
        status, body = _parse(get_run_traces(""))

        assert status == 400
        assert "error" in body

    def test_trace_response_shape(self):
        _patch_traces_table.table.query.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        status, body = _parse(get_run_traces(RUN_ID))

        trace = body["traces"][0]
        assert "run_id" in trace
        assert "step_name" in trace
        assert "pipeline_id" in trace
        assert "timestamp" in trace
        assert "trace" in trace
        inner = trace["trace"]
        assert "model_calls" in inner
        assert "tool_calls" in inner
        assert "total_input_tokens" in inner
        assert "total_output_tokens" in inner
        assert "estimated_cost_usd" in inner

    def test_returns_500_on_dynamo_error(self):
        _patch_traces_table.table.query.side_effect = Exception("DynamoDB error")

        status, body = _parse(get_run_traces(RUN_ID))

        assert status == 500
        assert "error" in body


# ===========================================================================
# Tests — get_step_trace
# ===========================================================================


class TestGetStepTrace:
    """GET /api/runs/{runId}/agent-trace/{stepName}"""

    def test_returns_single_trace(self):
        _patch_traces_table.table.get_item.return_value = {
            "Item": SAMPLE_TRACE_ITEM,
        }

        status, body = _parse(get_step_trace(RUN_ID, STEP_NAME))

        assert status == 200
        assert body["run_id"] == RUN_ID
        assert body["step_name"] == STEP_NAME
        assert body["trace"]["total_input_tokens"] == 1250
        assert body["trace"]["total_output_tokens"] == 340

    def test_returns_404_when_not_found(self):
        _patch_traces_table.table.get_item.return_value = {}

        status, body = _parse(get_step_trace(RUN_ID, "nonexistent_step"))

        assert status == 404
        assert "error" in body

    def test_returns_400_for_missing_params(self):
        status, body = _parse(get_step_trace("", STEP_NAME))
        assert status == 400

        status, body = _parse(get_step_trace(RUN_ID, ""))
        assert status == 400

    def test_returns_500_on_dynamo_error(self):
        _patch_traces_table.table.get_item.side_effect = Exception("DynamoDB error")

        status, body = _parse(get_step_trace(RUN_ID, STEP_NAME))

        assert status == 500
        assert "error" in body


# ===========================================================================
# Tests — get_token_usage_metrics
# ===========================================================================


class TestGetTokenUsageMetrics:
    """GET /api/metrics/token-usage?period=7d"""

    def _make_trace_item(self, day_offset: int, input_tokens: int, output_tokens: int):
        """Create a trace item with a timestamp offset by day_offset days from now."""
        ts = datetime.now(UTC) - timedelta(days=day_offset)
        return {
            "run_id": f"run-{day_offset}",
            "step_name": "code_generation",
            "pipeline_id": "pipe-1",
            "timestamp": ts.isoformat(),
            "trace_data": {
                "total_input_tokens": input_tokens,
                "total_output_tokens": output_tokens,
                "total_cache_read_tokens": 0,
                "estimated_cost_usd": Decimal("0.01"),
            },
        }

    def test_aggregates_token_usage(self):
        items = [
            self._make_trace_item(1, 1000, 200),
            self._make_trace_item(1, 500, 100),
            self._make_trace_item(2, 800, 300),
        ]
        _patch_traces_table.table.scan.return_value = {"Items": items}

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 200
        assert body["period"] == "7d"
        assert body["total_input_tokens"] == 2300
        assert body["total_output_tokens"] == 600
        assert body["total_traces"] == 3
        assert len(body["daily"]) == 2  # 2 distinct days

    def test_defaults_to_7d_for_invalid_period(self):
        _patch_traces_table.table.scan.return_value = {"Items": []}

        status, body = _parse(get_token_usage_metrics({"period": "invalid"}))

        assert status == 200
        assert body["period"] == "7d"

    def test_defaults_to_7d_when_no_period(self):
        _patch_traces_table.table.scan.return_value = {"Items": []}

        status, body = _parse(get_token_usage_metrics({}))

        assert status == 200
        assert body["period"] == "7d"

    def test_returns_empty_daily_when_no_traces(self):
        _patch_traces_table.table.scan.return_value = {"Items": []}

        status, body = _parse(get_token_usage_metrics({"period": "1d"}))

        assert status == 200
        assert body["daily"] == []
        assert body["total_traces"] == 0
        assert body["total_input_tokens"] == 0

    def test_handles_pagination(self):
        item1 = self._make_trace_item(1, 100, 50)
        item2 = self._make_trace_item(2, 200, 100)
        _patch_traces_table.table.scan.side_effect = [
            {"Items": [item1], "LastEvaluatedKey": {"run_id": "run-1"}},
            {"Items": [item2]},
        ]

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 200
        assert body["total_traces"] == 2
        assert body["total_input_tokens"] == 300

    def test_daily_sorted_by_date(self):
        items = [
            self._make_trace_item(3, 100, 50),
            self._make_trace_item(1, 200, 100),
        ]
        _patch_traces_table.table.scan.return_value = {"Items": items}

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 200
        dates = [d["date"] for d in body["daily"]]
        assert dates == sorted(dates)

    def test_returns_500_on_dynamo_error(self):
        _patch_traces_table.table.scan.side_effect = Exception("DynamoDB error")

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 500
        assert "error" in body

    def test_supports_all_valid_periods(self):
        for period in ["1d", "7d", "30d", "90d"]:
            _patch_traces_table.table.scan.return_value = {"Items": []}
            status, body = _parse(get_token_usage_metrics({"period": period}))
            assert status == 200
            assert body["period"] == period
