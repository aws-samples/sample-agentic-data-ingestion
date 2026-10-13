# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Integration tests for AgenticDataIngestion Dashboard API

Tests cover:
- API Gateway integration with Lambda functions
- Database queries and data aggregation
- Real-time update system and EventBridge integration
- Approval UI components and user interactions
"""

import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

from botocore.exceptions import ClientError

# Add the Lambda function directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda"))
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# Clear any cached Lambda modules from other test files to avoid cross-contamination
for _mod in list(sys.modules.keys()):
    if "agentic_data_ingestion_dashboard_api" in _mod or _mod in ("lambda_function", "router", "handlers"):
        del sys.modules[_mod]

# Import the Lambda function and handler modules
import agentic_data_ingestion_dashboard_api.lambda_function as dashboard_api
from agentic_data_ingestion_dashboard_api.handlers import approvals as approvals_handler
from agentic_data_ingestion_dashboard_api.handlers import files as files_handler
from agentic_data_ingestion_dashboard_api.handlers import pipelines as pipelines_handler
from agentic_data_ingestion_dashboard_api.handlers import stats as stats_handler
from agentic_data_ingestion_dashboard_api.handlers.common import create_response

# The handler module paths for patching — tables are imported into each handler.
# Due to sys.path including the dashboard_api directory, there are TWO module instances:
# - 'handlers.stats' (used by the router's relative imports)
# - 'agentic_data_ingestion_dashboard_api.handlers.stats' (used by the test's absolute imports)
# We must patch BOTH to cover lambda_handler calls AND direct handler calls.
_SHORT = "handlers"
_LONG = "agentic_data_ingestion_dashboard_api.handlers"


def _make_table_patchers(mock_state, mock_pipelines, mock_approvals, mock_mappings=None, mock_executions=None):
    """Create patchers for all handler modules using object-based patching."""
    # Import the actual module objects to avoid stale sys.modules references
    from handlers import approvals as _approvals_mod
    from handlers import files as _files

    # P21 Task 7.2: the pipeline CRUD handlers (get_pipelines, get_pipeline_details,
    # …) now live in handlers.pipelines_crud and the schema-mapping handlers in
    # handlers.pipelines_mappings. Patch the table handles on the short-path
    # modules (used when requests are routed via route_request) AND directly on
    # the globals dict each long-path handler actually executes in (resolved via
    # ``__globals__``). The latter is robust against the short/long
    # ``agentic_data_ingestion_dashboard_api.handlers`` vs ``handlers`` module-identity
    # split that pytest's import loader can produce. Module references only —
    # behaviour-preserving.
    from handlers import pipelines_crud as _pipelines_mod
    from handlers import pipelines_mappings as _pipelines_mappings_mod
    from handlers import stats as _stats

    patchers = [
        patch.object(_stats, "state_table", mock_state),
        patch.object(_stats, "pipelines_table", mock_pipelines),
        patch.object(_stats, "approvals_table", mock_approvals),
        patch.object(_files, "state_table", mock_state),
        patch.object(_approvals_mod, "approvals_table", mock_approvals),
        patch.object(_approvals_mod, "state_table", mock_state),
        patch.object(_pipelines_mod, "state_table", mock_state),
        patch.object(_pipelines_mod, "pipelines_table", mock_pipelines),
        patch.object(_pipelines_mod, "approvals_table", mock_approvals),
        # Also patch the long-path modules (used by lambda_handler calls)
        patch.object(stats_handler, "state_table", mock_state),
        patch.object(stats_handler, "pipelines_table", mock_pipelines),
        patch.object(stats_handler, "approvals_table", mock_approvals),
        patch.object(files_handler, "state_table", mock_state),
        patch.object(approvals_handler, "approvals_table", mock_approvals),
        patch.object(approvals_handler, "state_table", mock_state),
    ]
    if mock_mappings:
        patchers.append(patch.object(_pipelines_mod, "mappings_table", mock_mappings))
        patchers.append(patch.object(_pipelines_mappings_mod, "mappings_table", mock_mappings))
    if mock_executions:
        patchers.append(patch.object(_pipelines_mod, "executions_table", mock_executions))

    # Patch the globals dict the long-path handlers actually run in.
    _crud_globals = pipelines_handler.get_pipelines.__globals__
    _crud_overrides = {
        "state_table": mock_state,
        "pipelines_table": mock_pipelines,
        "approvals_table": mock_approvals,
    }
    if mock_mappings is not None:
        _crud_overrides["mappings_table"] = mock_mappings
    if mock_executions is not None:
        _crud_overrides["executions_table"] = mock_executions
    patchers.append(patch.dict(_crud_globals, _crud_overrides))
    if mock_mappings is not None:
        patchers.append(
            patch.dict(
                pipelines_handler.rerun_mapping.__globals__,
                {"mappings_table": mock_mappings},
            )
        )
    return patchers


class TestAPIGatewayIntegration(unittest.TestCase):
    """Test API Gateway integration with Lambda functions"""

    def setUp(self):
        """Set up test fixtures — mock tables in each handler module where they're used"""
        self.mock_state_table = Mock()
        self.mock_pipelines_table = Mock()
        self.mock_approvals_table = Mock()
        self.mock_mappings_table = Mock()
        self.mock_executions_table = Mock()

        self.patchers = _make_table_patchers(
            self.mock_state_table,
            self.mock_pipelines_table,
            self.mock_approvals_table,
            self.mock_mappings_table,
            self.mock_executions_table,
        )
        for p in self.patchers:
            p.start()

    def tearDown(self):
        """Clean up patches"""
        for p in self.patchers:
            p.stop()

    def test_cors_preflight_request(self):
        """Test CORS preflight OPTIONS request handling"""
        event = {
            "httpMethod": "OPTIONS",
            "path": "/api/pipelines",
            "headers": {
                "Origin": "https://dashboard.agentic_data_ingestion.com",
                "Access-Control-Request-Method": "GET",
            },
        }

        response = dashboard_api.lambda_handler(event, {})

        # Verify CORS response
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("Access-Control-Allow-Origin", response["headers"])
        self.assertEqual(response["headers"]["Access-Control-Allow-Origin"], "*")
        self.assertIn("Access-Control-Allow-Methods", response["headers"])

    def test_get_pipelines_endpoint(self):
        """Test GET /api/pipelines endpoint"""
        # Mock pipelines_table.scan → return 1 active pipeline
        self.mock_pipelines_table.scan.return_value = {
            "Items": [
                {
                    "pipeline_id": "test-pipeline-1",
                    "pipeline_name": "Test Pipeline",
                    "status": "active",
                    "created_at": "2025-01-01T10:00:00Z",
                    "updated_at": "2025-01-01T12:00:00Z",
                }
            ]
        }
        # state_table.query is called multiple times by enrichment (schema history, file count, recent files)
        self.mock_state_table.query.return_value = {"Items": [], "Count": 0, "ScannedCount": 0}
        # executions_table.query → no executions
        self.mock_executions_table.query.return_value = {"Items": []}
        # approvals_table.scan → no pending approvals
        self.mock_approvals_table.scan.return_value = {"Items": []}

        # Clear the in-memory cache so our mocks are hit
        from agentic_data_ingestion_dashboard_api.handlers import pipelines as _ph

        _ph._cache.clear()

        event = {
            "httpMethod": "GET",
            "path": "/api/pipelines",
            "queryStringParameters": None,
        }

        response = dashboard_api.lambda_handler(event, {})

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        # Paginated envelope
        self.assertIn("pipelines", body)
        self.assertIn("total_count", body)
        self.assertIn("page", body)
        self.assertIn("page_size", body)
        self.assertIn("total_pages", body)
        self.assertIn("has_more", body)
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(len(body["pipelines"]), 1)
        self.assertEqual(body["pipelines"][0]["pipeline_id"], "test-pipeline-1")

    def test_get_pipeline_details_endpoint(self):
        """Test GET /api/pipeline/{id} endpoint"""
        # state_table.query via PipelineIndex GSI → return a couple of step items
        # NOTE: The handler only sets current_schema_hash when the schema_extract
        # timestamp is strictly greater than the current updated_at. We order items
        # so that file_validation sets updated_at first, then schema_extract exceeds it.
        self.mock_state_table.query.return_value = {
            "Items": [
                {
                    "pipeline_id": "test-pipeline-1",
                    "step_name": "file_validation",
                    "status": "success",
                    "timestamp": "2025-01-01T10:00:00Z",
                    "original_s3_uri": "s3://bucket/data.csv",
                },
                {
                    "pipeline_id": "test-pipeline-1",
                    "step_name": "schema_extract",
                    "status": "success",
                    "timestamp": "2025-01-01T10:05:00Z",
                    "schema_hash": "abc123",
                    "extracted_schema": '{"col1": "string"}',
                    "json_s3_uri": "s3://bucket/data.json",
                },
            ]
        }
        # pipelines_table.get_item → pipeline metadata
        self.mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": "test-pipeline-1",
                "pipeline_name": "Test Pipeline",
                "target_schema_name": "target_v1",
                "ai_model": "anthropic.claude-3",
                "pipeline_description": "A test pipeline",
                "status": "active",
            }
        }
        # mappings_table.get_item → no mapping
        self.mock_mappings_table.get_item.return_value = {}

        event = {
            "httpMethod": "GET",
            "path": "/api/pipelines/test-pipeline-1",
            "queryStringParameters": None,
        }

        response = dashboard_api.lambda_handler(event, {})

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["pipeline_key"], "test-pipeline-1")
        self.assertEqual(body["pipeline_name"], "Test Pipeline")
        self.assertEqual(body["file_count"], 1)
        self.assertEqual(body["schema_version"], 1)
        self.assertGreaterEqual(len(body["schema_history"]), 1)
        self.assertEqual(body["schema_history"][0]["hash"], "abc123")

    def test_get_stats_endpoint(self):
        """Test GET /api/stats endpoint"""
        # Mock DynamoDB responses
        self.mock_pipelines_table.scan.return_value = {
            "Items": [
                {"pipeline_id": "p1", "status": "active"},
                {"pipeline_id": "p2", "status": "active"},
                {"pipeline_id": "p3", "status": "archived"},
            ]
        }

        # AMENDED BY p47 D4 — was ``state_table.scan.side_effect`` returning two
        # ``Items`` lists that the handler counted client-side. The two filtered
        # state-table scans are now four ``Select="COUNT"`` Queries on
        # ``step_name-timestamp-index`` (R7.4), which return a server-side ``Count``
        # and no items at all, so a scan stub can no longer drive this path.
        #
        # Four responses, in the order the handler issues them: file_validation
        # total, file_validation successes, schema_mapping total, schema_mapping
        # successes. The figures are the same population as before — 3 file rows of
        # which 2 succeeded, 2 mapping rows both of which did — so every assertion
        # below is unchanged. Absent ``LastEvaluatedKey`` means each walk completed,
        # which is what makes ``partial_counts`` empty.
        self.mock_state_table.query.side_effect = [
            {"Count": 3, "ScannedCount": 3},  # file_validation, all statuses
            {"Count": 2, "ScannedCount": 3},  # file_validation, status == success
            {"Count": 2, "ScannedCount": 2},  # schema_mapping, all statuses
            {"Count": 2, "ScannedCount": 2},  # schema_mapping, status == success
        ]

        self.mock_approvals_table.scan.return_value = {
            "Items": [{"approval_id": "a1", "status": "pending"}, {"approval_id": "a2", "status": "approved"}]
        }

        event = {"httpMethod": "GET", "path": "/api/stats", "queryStringParameters": None}

        response = dashboard_api.lambda_handler(event, {})

        # Verify response
        self.assertEqual(response["statusCode"], 200)

        body = json.loads(response["body"])
        self.assertEqual(body["total_pipelines"], 3)
        self.assertEqual(body["active_pipelines"], 2)
        self.assertEqual(body["total_files_processed"], 3)
        self.assertEqual(body["successful_files"], 2)
        self.assertAlmostEqual(body["file_success_rate"], 66.67, places=1)
        self.assertEqual(body["pending_approvals"], 1)
        # p47 D4: the response now carries R7.4's marker, and it travels through the
        # router intact. Empty here because every read above completed.
        self.assertEqual(body["partial_counts"], [])
        # Every state read went to the GSI, and the step name is in the key
        # condition rather than in a post-read filter.
        self.assertEqual(self.mock_state_table.scan.call_count, 0)
        self.assertEqual(self.mock_state_table.query.call_count, 4)
        for call in self.mock_state_table.query.call_args_list:
            self.assertEqual(call.kwargs["IndexName"], "step_name-timestamp-index")
            self.assertEqual(call.kwargs["Select"], "COUNT")

    def test_endpoint_not_found(self):
        """Test 404 response for unknown endpoints"""
        event = {"httpMethod": "GET", "path": "/api/unknown", "queryStringParameters": None}

        response = dashboard_api.lambda_handler(event, {})

        self.assertEqual(response["statusCode"], 404)
        body = json.loads(response["body"])
        self.assertIn("error", body)

    def test_decimal_encoder(self):
        """Test Decimal encoding in JSON responses"""
        # Create response with Decimal values using the common create_response
        test_data = {"confidence_score": Decimal("0.95"), "processing_time": Decimal("123.45")}

        response = create_response(200, test_data)

        # Verify Decimals are converted to floats
        body = json.loads(response["body"])
        self.assertEqual(body["confidence_score"], 0.95)
        self.assertEqual(body["processing_time"], 123.45)


class TestDatabaseQueriesAndAggregation(unittest.TestCase):
    """Test database queries and data aggregation logic"""

    def setUp(self):
        """Set up test fixtures — mock tables in handler modules"""
        self.mock_state_table = Mock()
        self.mock_pipelines_table = Mock()
        self.mock_mappings_table = Mock()
        self.mock_approvals_table = Mock()
        self.mock_executions_table = Mock()

        self.patchers = _make_table_patchers(
            self.mock_state_table,
            self.mock_pipelines_table,
            self.mock_approvals_table,
            self.mock_mappings_table,
            self.mock_executions_table,
        )
        for p in self.patchers:
            p.start()

    def tearDown(self):
        """Clean up patches"""
        for p in self.patchers:
            p.stop()

    def test_pipeline_aggregation_with_multiple_runs(self):
        """Test pipeline data aggregation across multiple runs"""
        # Two pipelines returned by scan
        self.mock_pipelines_table.scan.return_value = {
            "Items": [
                {
                    "pipeline_id": "pipeline-a",
                    "pipeline_name": "Pipeline A",
                    "status": "active",
                    "updated_at": "2025-01-02T00:00:00Z",
                },
                {
                    "pipeline_id": "pipeline-b",
                    "pipeline_name": "Pipeline B",
                    "status": "active",
                    "updated_at": "2025-01-01T00:00:00Z",
                },
            ]
        }
        # Enrichment queries return empty
        self.mock_state_table.query.return_value = {"Items": [], "Count": 0, "ScannedCount": 0}
        self.mock_executions_table.query.return_value = {"Items": []}
        self.mock_approvals_table.scan.return_value = {"Items": []}

        from agentic_data_ingestion_dashboard_api.handlers import pipelines as _ph

        _ph._cache.clear()

        response = pipelines_handler.get_pipelines()

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["total_count"], 2)
        self.assertEqual(len(body["pipelines"]), 2)
        # Sorted newest first
        ids = [p["pipeline_id"] for p in body["pipelines"]]
        self.assertEqual(ids, ["pipeline-a", "pipeline-b"])

    def test_schema_mapping_integration(self):
        """Test schema mapping data integration from separate table"""
        # state_table.query returns items including a schema_mapping step with mapping_id
        self.mock_state_table.query.return_value = {
            "Items": [
                {
                    "pipeline_id": "map-pipeline",
                    "step_name": "file_validation",
                    "status": "success",
                    "timestamp": "2025-01-01T09:00:00Z",
                    "original_s3_uri": "s3://bucket/file.csv",
                },
                {
                    "pipeline_id": "map-pipeline",
                    "step_name": "schema_extract",
                    "status": "success",
                    "timestamp": "2025-01-01T09:05:00Z",
                    "schema_hash": "hash-xyz",
                    "extracted_schema": '{"a": "int"}',
                    "json_s3_uri": "s3://bucket/file.json",
                },
                {
                    "pipeline_id": "map-pipeline",
                    "step_name": "schema_mapping",
                    "status": "success",
                    "timestamp": "2025-01-01T09:10:00Z",
                    "source_schema_hash": "hash-xyz",
                    "mapping_id": "mapping-001",
                    "mapping_count": 3,
                    "unmapped_source_count": 0,
                    "unmapped_target_count": 1,
                },
            ]
        }
        # pipelines_table.get_item → pipeline metadata
        self.mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": "map-pipeline",
                "pipeline_name": "Mapping Pipeline",
                "status": "active",
            }
        }
        # mappings_table.get_item → mapping details
        self.mock_mappings_table.get_item.return_value = {
            "Item": {
                "mapping_id": "mapping-001",
                "mapping_result": json.dumps({"a": "col_a"}),
                "confidence_score": Decimal("0.92"),
                "ai_model_used": "anthropic.claude-3",
            }
        }

        response = pipelines_handler.get_pipeline_details("map-pipeline")

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertTrue(body["has_mapping"])
        self.assertEqual(body["mapping_count"], 3)
        self.assertEqual(body["mapping_id"], "mapping-001")
        self.assertAlmostEqual(body["mapping_confidence"], 0.92, places=2)
        # Schema history entry should also be enriched with mapping info
        mapped_schema = next((s for s in body["schema_history"] if s["hash"] == "hash-xyz"), None)
        self.assertIsNotNone(mapped_schema)
        self.assertTrue(mapped_schema["has_mapping"])

    def test_error_handling_in_queries(self):
        """Test error handling when DynamoDB queries fail"""
        # Mock DynamoDB error — get_pipelines scans pipelines_table first
        self.mock_pipelines_table.scan.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Test error"}}, "Scan"
        )

        response = pipelines_handler.get_pipelines()

        # Verify error response
        self.assertEqual(response["statusCode"], 500)
        body = json.loads(response["body"])
        self.assertIn("error", body)

    def test_file_filtering_by_pipeline(self):
        """Test file filtering by pipeline_key parameter"""
        # Mock state table response — get_files scans with FilterExpression
        self.mock_state_table.scan.return_value = {
            "Items": [
                {
                    "original_pipeline_id": "target-pipeline",
                    "pipeline_id": "target-pipeline",
                    "step_name": "file_validation",
                    "status": "success",
                    "timestamp": "2025-01-01T10:00:00Z",
                    "original_s3_uri": "s3://bucket/target-file.csv",
                }
            ]
        }

        # Test with pipeline filter — get_files takes query_params dict
        response = files_handler.get_files({"pipeline_key": "target-pipeline"})

        # Verify filtering
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])

        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["pipeline_key"], "target-pipeline")
        self.assertEqual(body[0]["file_key"], "target-file.csv")


class TestApprovalAPIIntegration(unittest.TestCase):
    """Test approval API endpoints and integration"""

    def setUp(self):
        """Set up test fixtures — mock tables in handler modules"""
        self.mock_approvals_table = Mock()
        self.mock_state_table = Mock()

        self.patchers = []
        for prefix in ("handlers", "agentic_data_ingestion_dashboard_api.handlers"):
            self.patchers.extend(
                [
                    patch(f"{prefix}.approvals.approvals_table", self.mock_approvals_table),
                    patch(f"{prefix}.approvals.state_table", self.mock_state_table),
                ]
            )
        for p in self.patchers:
            p.start()

    def tearDown(self):
        """Clean up patches"""
        for p in self.patchers:
            p.stop()

    def test_get_pending_approvals(self):
        """Test GET /api/approvals/pending endpoint"""
        # Mock approvals table response — get_pending_approvals queries the
        # status GSI (P28 R17.1) so DynamoDB returns only pending items
        self.mock_approvals_table.query.return_value = {
            "Items": [
                {
                    "approval_id": "approval-1",
                    "pipeline_id": "test-pipeline-1",
                    "approval_type": "schema_mapping",
                    "status": "pending",
                    "created_at": "2025-01-01T10:00:00Z",
                    "timeout_at": "2025-01-08T10:00:00Z",
                    "approval_data": json.dumps({"test": "data"}),
                }
            ]
        }

        event = {"httpMethod": "GET", "path": "/api/approvals/pending", "queryStringParameters": None}

        response = dashboard_api.lambda_handler(event, {})

        # Verify response
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])

        # Should only return pending approvals
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["approval_id"], "approval-1")
        self.assertEqual(body[0]["status"], "pending")

    @patch("agentic_data_ingestion_dashboard_api.handlers.approvals.task_tokens_table")
    def test_approve_request(self, mock_tokens_table):
        """Test POST /api/approvals/{id}/approve endpoint"""
        # Mock the approval record lookup
        self.mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "approval-123",
                "pipeline_id": "test-pipeline",
                "approval_type": "schema_mapping",
                "status": "pending",
                "has_task_token": False,
                "run_id": "run-001",
            }
        }

        event = {
            "httpMethod": "POST",
            "path": "/api/approvals/approval-123/approve",
            "queryStringParameters": None,
            "body": json.dumps({"comment": "Looks good to me", "user": "test-user"}),
        }

        response = dashboard_api.lambda_handler(event, {})

        # Verify response
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertTrue(body["success"])
        self.assertEqual(body["status"], "approved")

        # Verify approval table was updated
        self.mock_approvals_table.update_item.assert_called_once()


class TestRealTimeUpdateSystem(unittest.TestCase):
    """Test real-time update system and EventBridge integration"""

    def setUp(self):
        """Set up test fixtures"""
        # Mock EventBridge client
        self.mock_events = Mock()

        # Patch EventBridge
        self.events_patcher = patch("boto3.client")
        self.mock_boto3_client = self.events_patcher.start()
        self.mock_boto3_client.return_value = self.mock_events

    def tearDown(self):
        """Clean up patches"""
        self.events_patcher.stop()

    def test_incremental_updates_endpoint(self):
        """Test incremental updates for real-time dashboard"""
        from handlers import common as _common_mod
        from handlers import files as _files_mod

        scan_result = {
            "Items": [
                {
                    "pipeline_id": "test-pipeline-1",
                    "step_name": "file_validation",
                    "status": "success",
                    "timestamp": "2025-01-01T11:55:00Z",
                    "original_s3_uri": "s3://bucket/file.csv",
                }
            ]
        }
        mock_state = MagicMock()
        mock_state.scan.return_value = scan_result

        with (
            patch.object(_files_mod, "state_table", mock_state),
            patch.object(_common_mod, "state_table", mock_state),
            patch.object(files_handler, "state_table", mock_state),
        ):
            response = files_handler.get_files({})
        self.assertEqual(response["statusCode"], 200)

    def test_eventbridge_event_structure(self):
        """Test EventBridge event structure for dashboard updates"""
        # This tests the expected event structure that would be published
        # by Lambda functions for real-time dashboard updates

        expected_event = {
            "source": "agentic_data_ingestion.pipeline",
            "detail-type": "Pipeline State Change",
            "detail": {
                "pipeline_id": "test-pipeline-1",
                "execution_id": "exec-123",
                "stage": "schema_mapping",
                "status": "completed",
                "timestamp": "2025-01-01T12:00:00Z",
                "metrics": {"duration_ms": 1500, "lines_processed": 10000},
            },
        }

        # Verify event structure
        self.assertIn("source", expected_event)
        self.assertIn("detail-type", expected_event)
        self.assertIn("detail", expected_event)
        self.assertIn("pipeline_id", expected_event["detail"])
        self.assertIn("status", expected_event["detail"])

    def test_dashboard_polling_configuration(self):
        """Test dashboard polling configuration for different update types"""
        # Test configuration for different polling intervals
        polling_config = {
            "critical_updates": 5000,  # 5 seconds for approvals, errors
            "normal_updates": 30000,  # 30 seconds for general status
            "metrics_updates": 300000,  # 5 minutes for aggregated metrics
        }

        # Verify reasonable polling intervals
        self.assertLessEqual(polling_config["critical_updates"], 10000)  # Max 10 seconds
        self.assertGreaterEqual(polling_config["normal_updates"], 30000)  # Min 30 seconds
        self.assertGreaterEqual(polling_config["metrics_updates"], 300000)  # Min 5 minutes


class TestUIComponentIntegration(unittest.TestCase):
    """Test approval UI components and user interactions"""

    def test_approval_item_rendering_data(self):
        """Test data structure for approval item rendering"""
        # Test the data structure that would be used by the UI
        approval_data = {
            "approval_id": "approval-123",
            "pipeline_id": "test-pipeline-1",
            "pipeline_name": "Test Pipeline",
            "approval_type": "schema_mapping",
            "status": "pending",
            "created_at": "2025-01-01T10:00:00Z",
            "timeout_at": "2025-01-08T10:00:00Z",
            "approval_data": {
                "source_schema": {"field1": "string"},
                "target_schema": {"target_field1": "string"},
                "proposed_mappings": [{"source": "field1", "target": "target_field1", "confidence": 0.95}],
                "confidence_scores": {"field1->target_field1": 0.95},
                "unmapped_fields": [],
            },
        }

        # Verify required fields for UI rendering
        required_fields = [
            "approval_id",
            "pipeline_id",
            "approval_type",
            "status",
            "created_at",
            "timeout_at",
            "approval_data",
        ]

        for field in required_fields:
            self.assertIn(field, approval_data)

        # Verify approval data structure for schema mapping
        if approval_data["approval_type"] == "schema_mapping":
            schema_fields = ["source_schema", "target_schema", "proposed_mappings"]
            for field in schema_fields:
                self.assertIn(field, approval_data["approval_data"])

    def test_pipeline_step_visualization_data(self):
        """Test data structure for pipeline step visualization"""
        # Test the data structure for step-by-step pipeline visualization
        pipeline_steps = [
            {
                "step_id": "1",
                "name": "File Validation",
                "lambda_function": "agentic_data_ingestion_file_validation",
                "status": "completed",
                "start_time": "2025-01-01T10:00:00Z",
                "end_time": "2025-01-01T10:01:00Z",
                "duration": 60000,  # milliseconds
                "icon": {"type": "validate", "color": "green"},
                "tooltip": "Validate uploaded file format and structure",
            },
            {
                "step_id": "2",
                "name": "Schema Extract",
                "lambda_function": "agentic_data_ingestion_schema_extract",
                "status": "running",
                "start_time": "2025-01-01T10:01:00Z",
                "icon": {"type": "extract", "color": "blue", "animation": "pulse"},
                "tooltip": "Extract schema from validated file",
            },
            {
                "step_id": "3",
                "name": "Schema Mapping",
                "lambda_function": "agentic_data_ingestion_schema_mapping",
                "status": "pending",
                "icon": {"type": "mapping", "color": "gray"},
                "tooltip": "Generate mappings between source and target schemas",
            },
        ]

        # Verify step data structure
        for step in pipeline_steps:
            required_fields = ["step_id", "name", "lambda_function", "status", "icon"]
            for field in required_fields:
                self.assertIn(field, step)

            # Verify icon structure
            self.assertIn("type", step["icon"])
            self.assertIn("color", step["icon"])

    def test_dashboard_metrics_data_structure(self):
        """Test dashboard metrics data structure for UI components"""
        # Test the metrics data structure for dashboard widgets
        dashboard_metrics = {
            "overview": {"total_pipelines": 15, "active_pipelines": 12, "pending_approvals": 3, "error_pipelines": 1},
            "processing": {
                "total_files_processed_24h": 1250,
                "average_processing_rate": 52.1,  # files per hour
                "success_rate": 94.5,  # percentage
                "error_rate": 5.5,  # percentage
            },
            "approvals": {
                "pending_schema_mappings": 2,
                "pending_code_generations": 1,
                "pending_first_runs": 0,
                "average_approval_time": 3.2,  # hours
            },
            "performance": {
                "mcp_operation_latency": 245,  # milliseconds
                "strands_agent_decision_time": 1.8,  # seconds
                "agentcore_health_status": "healthy",
            },
        }

        # Verify metrics structure
        required_sections = ["overview", "processing", "approvals", "performance"]
        for section in required_sections:
            self.assertIn(section, dashboard_metrics)

        # Verify numeric values are reasonable
        self.assertGreaterEqual(dashboard_metrics["overview"]["total_pipelines"], 0)
        self.assertGreaterEqual(dashboard_metrics["processing"]["success_rate"], 0)
        self.assertLessEqual(dashboard_metrics["processing"]["success_rate"], 100)


class TestEndToEndAPIWorkflow(unittest.TestCase):
    """Test end-to-end API workflows"""

    def setUp(self):
        """Set up test fixtures — mock tables in handler modules"""
        self.mock_state_table = Mock()
        self.mock_pipelines_table = Mock()
        self.mock_approvals_table = Mock()
        self.mock_mappings_table = Mock()

        self.patchers = _make_table_patchers(
            self.mock_state_table, self.mock_pipelines_table, self.mock_approvals_table, self.mock_mappings_table
        )
        for p in self.patchers:
            p.start()

    def tearDown(self):
        """Clean up patches"""
        for p in self.patchers:
            p.stop()

    def test_complete_pipeline_lifecycle_api_calls(self):
        """Test complete pipeline lifecycle through API calls — stats, approvals, files"""
        # 1. Get stats
        self.mock_pipelines_table.scan.return_value = {
            "Items": [{"pipeline_id": "new-pipeline", "pipeline_name": "New Pipeline", "status": "active"}]
        }
        # AMENDED BY p47 D4 — see ``test_get_stats_endpoint`` for the reason. Four
        # COUNT Queries replace the two filtered scans; same population as before
        # (one successful file_validation row, no mappings yet).
        self.mock_state_table.query.side_effect = [
            {"Count": 1, "ScannedCount": 1},  # file_validation, all statuses
            {"Count": 1, "ScannedCount": 1},  # file_validation, status == success
            {"Count": 0, "ScannedCount": 0},  # schema_mapping, all statuses
            {"Count": 0, "ScannedCount": 0},  # schema_mapping, status == success
        ]
        self.mock_approvals_table.scan.return_value = {
            "Items": [
                {
                    "approval_id": "approval-new-pipeline",
                    "pipeline_id": "new-pipeline",
                    "approval_type": "schema_mapping",
                    "status": "pending",
                }
            ]
        }

        stats_response = stats_handler.get_stats()
        self.assertEqual(stats_response["statusCode"], 200)
        stats_body = json.loads(stats_response["body"])
        self.assertEqual(stats_body["total_pipelines"], 1)
        self.assertEqual(stats_body["pending_approvals"], 1)

        # 2. Check for pending approvals (status GSI Query, P28 R17.1)
        self.mock_approvals_table.query.return_value = {
            "Items": [
                {
                    "approval_id": "approval-new-pipeline",
                    "pipeline_id": "new-pipeline",
                    "approval_type": "schema_mapping",
                    "status": "pending",
                }
            ]
        }

        approvals_response = approvals_handler.get_pending_approvals()
        self.assertEqual(approvals_response["statusCode"], 200)

        # 3. Get files — reset side_effect from stats call
        self.mock_state_table.scan.side_effect = None
        self.mock_state_table.scan.return_value = {
            "Items": [
                {
                    "pipeline_id": "new-pipeline",
                    "original_pipeline_id": "new-pipeline",
                    "step_name": "file_validation",
                    "status": "success",
                    "timestamp": "2025-01-01T10:00:00Z",
                    "original_s3_uri": "s3://bucket/data.csv",
                }
            ]
        }

        files_response = files_handler.get_files({})
        self.assertEqual(files_response["statusCode"], 200)


if __name__ == "__main__":
    # Set up environment variables for testing
    os.environ["AWS_REGION"] = "us-west-2"
    os.environ["STATE_TABLE_NAME"] = "test-state-table"
    os.environ["PIPELINES_TABLE_NAME"] = "test-pipelines-table"
    os.environ["APPROVALS_TABLE_NAME"] = "test-approvals-table"
    os.environ["MAPPINGS_TABLE_NAME"] = "test-mappings-table"

    # Run the tests
    unittest.main(verbosity=2)
