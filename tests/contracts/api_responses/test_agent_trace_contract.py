# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: Agent trace API response shape validation.

Validates that the agent trace endpoints return responses matching
the expected JSON Schema shapes defined in the design document.

Task 6.7 — Phase 13a: Agent Observability
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import jsonschema
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
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------

MODEL_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "model_id": {"type": "string"},
        "input_tokens": {"type": ["number", "integer"]},
        "output_tokens": {"type": ["number", "integer"]},
        "cache_read_tokens": {"type": ["number", "integer"]},
        "duration_ms": {"type": "number"},
        "prompt_preview": {"type": "string"},
        "response_preview": {"type": "string"},
    },
}

TOOL_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "tool_name": {"type": "string"},
        "duration_ms": {"type": "number"},
        "status": {"type": "string"},
        "tool_params": {"type": "string"},
        "result_preview": {"type": "string"},
    },
}

TRACE_DATA_SCHEMA = {
    "type": "object",
    "required": [
        "model_calls",
        "tool_calls",
        "total_input_tokens",
        "total_output_tokens",
        "estimated_cost_usd",
    ],
    "properties": {
        "model_calls": {"type": "array", "items": MODEL_CALL_SCHEMA},
        "tool_calls": {"type": "array", "items": TOOL_CALL_SCHEMA},
        "total_input_tokens": {"type": ["number", "integer"]},
        "total_output_tokens": {"type": ["number", "integer"]},
        "total_cache_read_tokens": {"type": ["number", "integer"]},
        "estimated_cost_usd": {"type": "number"},
        "start_time": {"type": ["number", "null"]},
        "end_time": {"type": ["number", "null"]},
        "model_id": {"type": "string"},
        "error": {"type": ["string", "null"]},
    },
}

SINGLE_TRACE_SCHEMA = {
    "type": "object",
    "required": ["run_id", "step_name", "pipeline_id", "trace"],
    "properties": {
        "run_id": {"type": "string"},
        "step_name": {"type": "string"},
        "pipeline_id": {"type": "string"},
        "timestamp": {"type": "string"},
        "trace": TRACE_DATA_SCHEMA,
    },
}

RUN_TRACES_SCHEMA = {
    "type": "object",
    "required": ["run_id", "traces", "count"],
    "properties": {
        "run_id": {"type": "string"},
        "traces": {"type": "array", "items": SINGLE_TRACE_SCHEMA},
        "count": {"type": ["number", "integer"]},
    },
}

DAILY_TOKEN_USAGE_SCHEMA = {
    "type": "object",
    "required": ["date", "input_tokens", "output_tokens", "invocation_count"],
    "properties": {
        "date": {"type": "string"},
        "input_tokens": {"type": ["number", "integer"]},
        "output_tokens": {"type": ["number", "integer"]},
        "cache_read_tokens": {"type": ["number", "integer"]},
        "estimated_cost_usd": {"type": "number"},
        "invocation_count": {"type": ["number", "integer"]},
    },
}

TOKEN_USAGE_SCHEMA = {
    "type": "object",
    "required": [
        "period",
        "total_input_tokens",
        "total_output_tokens",
        "total_traces",
        "daily",
    ],
    "properties": {
        "period": {"type": "string", "enum": ["1d", "7d", "30d", "90d"]},
        "total_input_tokens": {"type": ["number", "integer"]},
        "total_output_tokens": {"type": ["number", "integer"]},
        "total_cache_read_tokens": {"type": ["number", "integer"]},
        "total_estimated_cost_usd": {"type": "number"},
        "total_traces": {"type": ["number", "integer"]},
        "daily": {"type": "array", "items": DAILY_TOKEN_USAGE_SCHEMA},
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


# NOTE: The model IDs below are illustrative sample trace data, not a primary-model
# selection. The canonical primary model is `us.anthropic.claude-sonnet-4-6` (P21 Req 1);
# this sample record intentionally retains a historical Claude Sonnet 4.5 ID to validate
# the agent-trace response contract shape.
SAMPLE_TRACE_ITEM = {
    "run_id": "run-2025-001",
    "step_name": "code_generation",
    "pipeline_id": "pipe-123",
    "timestamp": "2025-06-15T12:00:00+00:00",
    "trace_data": {
        "model_calls": [
            {
                "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "input_tokens": 1250,
                "output_tokens": 340,
                "cache_read_tokens": 800,
                "duration_ms": 2340.0,
                "prompt_preview": "Generate PySpark...",
                "response_preview": "df = ...",
            }
        ],
        "tool_calls": [
            {
                "tool_name": "create_glue_job",
                "duration_ms": 1200.0,
                "status": "success",
                "tool_params": '{"job_name": "test"}',
                "result_preview": '{"job_name": "test-etl"}',
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_traces_table(monkeypatch):
    from handlers import agent_traces as _mod

    mock_table = MagicMock()
    monkeypatch.setattr(_mod, "agent_traces_table", mock_table)
    _patch_traces_table.table = mock_table
    yield


from handlers.agent_traces import get_run_traces, get_step_trace, get_token_usage_metrics

# ===========================================================================
# Contract Tests
# ===========================================================================


class TestRunTracesContract:
    """Validate GET /runs/{runId}/agent-trace response shape."""

    def test_response_matches_schema(self):
        _patch_traces_table.table.query.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        status, body = _parse(get_run_traces("run-2025-001"))

        assert status == 200
        jsonschema.validate(body, RUN_TRACES_SCHEMA)

    def test_empty_response_matches_schema(self):
        _patch_traces_table.table.query.return_value = {"Items": []}

        status, body = _parse(get_run_traces("run-2025-001"))

        assert status == 200
        jsonschema.validate(body, RUN_TRACES_SCHEMA)

    def test_trace_has_required_fields(self):
        _patch_traces_table.table.query.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        _, body = _parse(get_run_traces("run-2025-001"))

        trace = body["traces"][0]
        required = {"run_id", "step_name", "pipeline_id", "trace"}
        missing = required - set(trace.keys())
        assert not missing, f"Trace missing fields: {missing}"

    def test_trace_data_has_required_fields(self):
        _patch_traces_table.table.query.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        _, body = _parse(get_run_traces("run-2025-001"))

        trace_data = body["traces"][0]["trace"]
        required = {
            "model_calls",
            "tool_calls",
            "total_input_tokens",
            "total_output_tokens",
            "estimated_cost_usd",
        }
        missing = required - set(trace_data.keys())
        assert not missing, f"Trace data missing fields: {missing}"


class TestStepTraceContract:
    """Validate GET /runs/{runId}/agent-trace/{stepName} response shape."""

    def test_response_matches_schema(self):
        _patch_traces_table.table.get_item.return_value = {
            "Item": SAMPLE_TRACE_ITEM,
        }

        status, body = _parse(get_step_trace("run-2025-001", "code_generation"))

        assert status == 200
        jsonschema.validate(body, SINGLE_TRACE_SCHEMA)

    def test_token_counts_are_integers(self):
        _patch_traces_table.table.get_item.return_value = {
            "Item": SAMPLE_TRACE_ITEM,
        }

        _, body = _parse(get_step_trace("run-2025-001", "code_generation"))

        assert isinstance(body["trace"]["total_input_tokens"], int)
        assert isinstance(body["trace"]["total_output_tokens"], int)


class TestTokenUsageContract:
    """Validate GET /metrics/token-usage response shape."""

    def test_response_matches_schema(self):
        _patch_traces_table.table.scan.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 200
        jsonschema.validate(body, TOKEN_USAGE_SCHEMA)

    def test_empty_response_matches_schema(self):
        _patch_traces_table.table.scan.return_value = {"Items": []}

        status, body = _parse(get_token_usage_metrics({"period": "7d"}))

        assert status == 200
        jsonschema.validate(body, TOKEN_USAGE_SCHEMA)

    def test_daily_entries_have_required_fields(self):
        _patch_traces_table.table.scan.return_value = {
            "Items": [SAMPLE_TRACE_ITEM],
        }

        _, body = _parse(get_token_usage_metrics({"period": "7d"}))

        for entry in body["daily"]:
            required = {"date", "input_tokens", "output_tokens", "invocation_count"}
            missing = required - set(entry.keys())
            assert not missing, f"Daily entry missing fields: {missing}"
