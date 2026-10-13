# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /metrics/comprehensive response shape.

Validates the ComprehensiveMetrics TypeScript interface including nested
performance, stages, and infrastructure sections.

Task 7.2 — Requirements 4.7, 4.8
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

from handlers import metrics as _metrics
from handlers.metrics import get_comprehensive_metrics


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


def _mock_cloudwatch_response():
    """Return a minimal CloudWatch response for mocking."""
    return {"MetricDataResults": []}


@pytest.fixture(autouse=True)
def _patch_cloudwatch(monkeypatch):
    mock_cw = MagicMock()
    mock_cw.get_metric_data.return_value = _mock_cloudwatch_response()

    # Patch the cloudwatch client on the metrics module
    monkeypatch.setattr(_metrics, "cloudwatch_client", mock_cw)

    # Patch the cache to always miss (return None)
    monkeypatch.setattr(_metrics, "_get_cached_metrics", lambda period: None)
    monkeypatch.setattr(_metrics, "_cache_metrics", lambda period, metrics: None)

    # Patch state_table for cache operations
    mock_state = MagicMock()
    mock_state.get_item.return_value = {}
    mock_state.query.return_value = {"Items": []}
    mock_state.scan.return_value = {"Items": []}
    monkeypatch.setattr(_metrics, "_cache_table", mock_state)

    # Patch the state table so these contract tests never touch real DynamoDB
    # (P28 R17.1: metrics read paths now Query GSIs instead of scanning).
    #
    # p39 task 6.2: the executions-table stub is gone with the subject it
    # stubbed. `handlers.metrics` no longer imports `executions_table` — the
    # flat-rate cost fallback was its only reader — and `monkeypatch.setattr`
    # raises `AttributeError` on an attribute that does not exist.
    mock_data_table = MagicMock()
    mock_data_table.query.return_value = {"Items": []}
    monkeypatch.setattr(_metrics, "state_table", mock_data_table)


class TestMetricsResponseContract:
    """Validate GET /metrics/comprehensive response matches ComprehensiveMetrics."""

    def test_response_has_top_level_sections(self):
        status, body = _parse(get_comprehensive_metrics({}))
        assert status == 200
        assert "performance" in body, "Missing 'performance' section"
        assert "stages" in body, "Missing 'stages' section"
        assert "infrastructure" in body, "Missing 'infrastructure' section"

    def test_performance_has_required_fields(self):
        _, body = _parse(get_comprehensive_metrics({}))
        perf = body["performance"]
        required = {
            "lines_ingested_24h",
            "processing_rate",
            "overall_success_rate",
            "failed_pipelines",
            "agent_latency_ms",
            "monthly_cost",
            "trends",
        }
        missing = required - set(perf.keys())
        assert not missing, f"Performance missing fields: {missing}"

    def test_trends_has_required_fields(self):
        _, body = _parse(get_comprehensive_metrics({}))
        trends = body["performance"]["trends"]
        required = {"ingestion", "success", "agent_latency", "cost"}
        missing = required - set(trends.keys())
        assert not missing, f"Trends missing fields: {missing}"

    def test_trends_values_are_valid(self):
        _, body = _parse(get_comprehensive_metrics({}))
        trends = body["performance"]["trends"]
        # p39 R4.4: `not_established` joins the vocabulary for a window with no
        # baseline on the previous side. Not folded into `stable` — see
        # LGR-4854b3d-1.
        valid_values = {"up", "down", "stable", "not_established"}
        for key in ["ingestion", "success", "agent_latency", "cost"]:
            assert trends[key] in valid_values, f"trends.{key} = '{trends[key]}' not in {valid_values}"

    def test_infrastructure_has_all_services(self):
        _, body = _parse(get_comprehensive_metrics({}))
        infra = body["infrastructure"]
        required_services = {"agentcore", "dynamodb", "s3", "lambda"}
        missing = required_services - set(infra.keys())
        assert not missing, f"Infrastructure missing services: {missing}"

    def test_infrastructure_service_has_status(self):
        _, body = _parse(get_comprehensive_metrics({}))
        infra = body["infrastructure"]
        valid_statuses = {"healthy", "warning", "critical", "unknown"}
        for service_name in ["agentcore", "dynamodb", "s3", "lambda"]:
            service = infra[service_name]
            assert "status" in service, f"{service_name} missing 'status'"
            assert service["status"] in valid_statuses, (
                f"{service_name}.status = '{service['status']}' not in {valid_statuses}"
            )

    def test_stages_values_are_objects_with_required_fields(self):
        _, body = _parse(get_comprehensive_metrics({}))
        stages = body["stages"]
        assert isinstance(stages, dict)
        for stage_name, stage_data in stages.items():
            required = {"success_count", "error_count", "avg_time_ms"}
            missing = required - set(stage_data.keys())
            assert not missing, f"Stage '{stage_name}' missing fields: {missing}"

    def test_performance_field_types(self):
        _, body = _parse(get_comprehensive_metrics({}))
        perf = body["performance"]
        # p39 R4.9: null when no `load_metrics` row in the window carries a
        # usable `rows_written` (the source since 2026-10-10; before that, the
        # `file_validation` `record_count_estimate`). The handler once
        # substituted the count of files, so this asserted a number of a
        # different unit than the tile's label. `processing_rate` inherits it —
        # no numerator, no rate. Amended with their reason rather than deleted.
        assert perf["lines_ingested_24h"] is None or isinstance(perf["lines_ingested_24h"], (int, float))
        assert perf["processing_rate"] is None or isinstance(perf["processing_rate"], (int, float))
        # p39 R4.6: null when the terminal-outcome denominator is zero, rather
        # than the 100.0 this used to assert. Amended with its reason.
        assert perf["overall_success_rate"] is None or isinstance(perf["overall_success_rate"], (int, float))
        assert isinstance(perf["failed_pipelines"], (int, float))
        # p39 R2.4: null when the window holds no `GlueJob.RunCostEstimate`
        # datapoint, rather than the flat-rate row count this used to assert.
        # Amended with its reason.
        assert perf["monthly_cost"] is None or isinstance(perf["monthly_cost"], (int, float))
