# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for RDS resource route integration in resources_router.

Validates: Requirement 12 — Router dispatches /api/resources/rds/* paths
to the correct handlers in rds_resources.py, and unknown RDS subpaths
return 404.
"""

import json
import os
import sys
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup — must come before importing router
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
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
os.environ.setdefault("ERRORS_TABLE", "agentic_data_ingestion_errors")
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from router import resources_router, route_request


def _make_event(path, method="GET", query_params=None):
    """Build a minimal API Gateway event."""
    return {
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "queryStringParameters": query_params or {},
        "body": None,
    }


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


MOCK_RESPONSE = {"statusCode": 200, "body": json.dumps({"ok": True})}


class TestRdsInstancesRoute:
    """GET /api/resources/rds/instances dispatches to list_rds_instances."""

    @patch("handlers.rds_resources.list_rds_instances", return_value=MOCK_RESPONSE)
    def test_dispatches_to_list_rds_instances(self, mock_handler):
        event = _make_event("/api/resources/rds/instances")
        result = route_request(event)
        mock_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 200

    @patch("handlers.rds_resources.list_rds_instances", return_value=MOCK_RESPONSE)
    def test_via_resources_router_directly(self, mock_handler):
        event = _make_event("/api/resources/rds/instances")
        result = resources_router(event, "rds/instances")
        mock_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 200


class TestRdsClustersRoute:
    """GET /api/resources/rds/clusters dispatches to list_rds_clusters."""

    @patch("handlers.rds_resources.list_rds_clusters", return_value=MOCK_RESPONSE)
    def test_dispatches_to_list_rds_clusters(self, mock_handler):
        event = _make_event("/api/resources/rds/clusters")
        result = route_request(event)
        mock_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 200


class TestGlueConnectionsRoute:
    """GET /api/resources/rds/glue-connections dispatches to list_glue_jdbc_connections."""

    @patch("handlers.rds_resources.list_glue_jdbc_connections", return_value=MOCK_RESPONSE)
    def test_dispatches_to_list_glue_jdbc_connections(self, mock_handler):
        event = _make_event("/api/resources/rds/glue-connections")
        result = route_request(event)
        mock_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 200


class TestRdsSecretsRoute:
    """GET /api/resources/rds/secrets dispatches to list_rds_secrets."""

    @patch("handlers.rds_resources.list_rds_secrets", return_value=MOCK_RESPONSE)
    def test_dispatches_to_list_rds_secrets(self, mock_handler):
        event = _make_event("/api/resources/rds/secrets")
        result = route_request(event)
        mock_handler.assert_called_once_with(event, None)
        assert result["statusCode"] == 200


class TestUnknownRdsSubpath:
    """Unknown /api/resources/rds/* paths return 404."""

    def test_unknown_rds_subpath_returns_404(self):
        event = _make_event("/api/resources/rds/nonexistent")
        result = route_request(event)
        status, body = _parse(result)
        assert status == 404
        assert "Unknown RDS resources endpoint" in body["error"]
        assert "nonexistent" in body["error"]

    def test_unknown_rds_subpath_via_resources_router(self):
        event = _make_event("/api/resources/rds/foobar")
        result = resources_router(event, "rds/foobar")
        status, body = _parse(result)
        assert status == 404
        assert "Unknown RDS resources endpoint" in body["error"]
