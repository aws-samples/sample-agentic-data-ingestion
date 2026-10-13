# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for router registration of rerun-mapping and mapping-hints routes.

Validates: Requirements 1.7 (rerun-mapping in RESOURCE_ROUTERS),
           mapping-hints route in pipelines_router.
"""

import json
import os
import sys
from unittest.mock import MagicMock

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

import router
from router import RESOURCE_ROUTERS, pipelines_router, route_request


class TestRerunMappingRouterRegistration:
    """Verify rerun-mapping is registered in RESOURCE_ROUTERS."""

    def test_rerun_mapping_key_exists(self):
        assert "rerun-mapping" in RESOURCE_ROUTERS

    def test_rerun_mapping_is_callable(self):
        assert callable(RESOURCE_ROUTERS["rerun-mapping"])

    def test_post_rerun_mapping_dispatches_via_route_request(self):
        """route_request finds the rerun-mapping router entry and calls it."""
        mock_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        original = RESOURCE_ROUTERS["rerun-mapping"]
        RESOURCE_ROUTERS["rerun-mapping"] = mock_handler
        try:
            event = {
                "httpMethod": "POST",
                "path": "/api/rerun-mapping",
                "body": json.dumps({"pipeline_key": "test-pipe"}),
            }
            result = route_request(event)
            mock_handler.assert_called_once_with(event, "")
            assert result["statusCode"] == 200
        finally:
            RESOURCE_ROUTERS["rerun-mapping"] = original


class TestMappingHintsRoute:
    """Verify POST /api/pipelines/{id}/mapping-hints routes correctly."""

    def test_post_mapping_hints_routes_to_handler(self):
        mock_handler = MagicMock(return_value={"statusCode": 200, "body": json.dumps({"success": True})})
        original = router.save_mapping_hints
        router.save_mapping_hints = mock_handler
        try:
            body = json.dumps({"hints": {"fname": "first_name"}})
            event = {
                "httpMethod": "POST",
                "path": "/api/pipelines/pipe-123/mapping-hints",
                "body": body,
                "queryStringParameters": {},
            }
            result = pipelines_router(event, "pipe-123/mapping-hints")
            mock_handler.assert_called_once_with("pipe-123", body)
            assert result["statusCode"] == 200
        finally:
            router.save_mapping_hints = original

    def test_mapping_hints_via_route_request(self):
        mock_handler = MagicMock(return_value={"statusCode": 200, "body": json.dumps({"success": True})})
        original = router.save_mapping_hints
        router.save_mapping_hints = mock_handler
        try:
            body = json.dumps({"hints": {"lname": "last_name"}})
            event = {
                "httpMethod": "POST",
                "path": "/api/pipelines/pipe-456/mapping-hints",
                "body": body,
                "queryStringParameters": {},
            }
            result = route_request(event)
            mock_handler.assert_called_once_with("pipe-456", body)
            assert result["statusCode"] == 200
        finally:
            router.save_mapping_hints = original


class TestBothKeysExist:
    """Verify both rerun-mapping and mapping-hints are accessible."""

    def test_rerun_mapping_in_resource_routers(self):
        assert "rerun-mapping" in RESOURCE_ROUTERS

    def test_mapping_hints_reachable_via_pipelines(self):
        """mapping-hints is a sub-route of pipelines, not a top-level resource."""
        mock_handler = MagicMock(return_value={"statusCode": 200, "body": "{}"})
        original = router.save_mapping_hints
        router.save_mapping_hints = mock_handler
        try:
            event = {
                "httpMethod": "POST",
                "path": "/api/pipelines/test/mapping-hints",
                "body": "{}",
                "queryStringParameters": {},
            }
            result = pipelines_router(event, "test/mapping-hints")
            mock_handler.assert_called_once()
        finally:
            router.save_mapping_hints = original
