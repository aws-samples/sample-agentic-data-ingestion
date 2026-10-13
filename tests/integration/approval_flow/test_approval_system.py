# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for AgenticDataIngestion Approval System

Tests cover:
- Approval request creation and validation
- Approval decision processing and Step Functions integration
- Timeout handling and escalation logic
- Approval data storage and retrieval
"""

import copy
import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError
from tests.fixtures.conftest import (
    FIRST_RUN_DECIMAL_RUN_ID,
    FIRST_RUN_QUALITY_CHECKS_STATE_ROW,
)

# Add the Lambda function directories to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Mock the shared modules before importing
sys.modules["eventbridge_publisher"] = Mock()
sys.modules["notification_service"] = Mock()

# Mock the actual shared modules that the Lambda handlers import at module level
_mock_eb = Mock()
_mock_eb.get_eventbridge_publishers = Mock(return_value=(Mock(), Mock(), Mock()))
sys.modules.setdefault("eventbridge_utils", _mock_eb)
sys.modules.setdefault("env_utils", Mock(require_env=lambda name, default=None: default or name))

# Create a proper metrics_facade mock where time_function is a pass-through decorator
_mock_metrics = Mock()


def _passthrough_time_function(*args, **kwargs):
    """time_function decorator that just returns the original function"""

    def decorator(func):
        return func

    return decorator


_mock_metrics.time_function = _passthrough_time_function
sys.modules["metrics_facade"] = _mock_metrics
sys.modules.setdefault("artifact_schema", Mock())

# Clear any cached Lambda modules from other test files to avoid cross-contamination
for mod_name in list(sys.modules.keys()):
    if "agentic_data_ingestion_approval" in mod_name or mod_name == "lambda_function":
        del sys.modules[mod_name]

# Import the Lambda functions (fresh import with correct mocks)
import agentic_data_ingestion_approval_handler.lambda_function as approval_handler
import agentic_data_ingestion_approval_timeout_handler.lambda_function as timeout_handler


class TestApprovalRequestValidation(unittest.TestCase):
    """Test approval request validation and sanitization"""

    def test_validate_approval_request_valid(self):
        """Test validation with valid approval request"""
        event = {
            "approval_type": "schema_mapping",
            "pipeline_id": "test-pipeline-123",
            "run_id": "run-456",
            "task_token": "token-789",
        }

        result = approval_handler.validate_approval_request(event)

        self.assertEqual(result["approval_type"], "schema_mapping")
        self.assertEqual(result["pipeline_id"], "test-pipeline-123")
        self.assertEqual(result["run_id"], "run-456")

    def test_validate_approval_request_missing_field(self):
        """Test validation with missing required field"""
        event = {
            "approval_type": "schema_mapping",
            "pipeline_id": "test-pipeline-123",
            # Missing run_id
        }

        with self.assertRaises(ValueError) as context:
            approval_handler.validate_approval_request(event)

        self.assertIn("Missing required field: run_id", str(context.exception))

    def test_validate_approval_request_invalid_type(self):
        """Test validation with invalid approval type"""
        event = {"approval_type": "invalid_type", "pipeline_id": "test-pipeline-123", "run_id": "run-456"}

        with self.assertRaises(ValueError) as context:
            approval_handler.validate_approval_request(event)

        self.assertIn("Invalid approval type", str(context.exception))

    def test_validate_approval_request_sanitization(self):
        """Whitespace is trimmed and over-long free text is truncated to 500."""
        event = {
            "approval_type": "schema_mapping",
            "pipeline_id": "  test-pipeline-123  ",  # Extra whitespace
            "run_id": "run-456",
            "summary": "x" * 600,  # Free text, not an identifier
        }

        result = approval_handler.validate_approval_request(event)

        self.assertEqual(result["pipeline_id"], "test-pipeline-123")  # Trimmed
        self.assertEqual(len(result["summary"]), 500)  # Truncated

    def test_validate_approval_request_refuses_an_over_long_identifier(self):
        """An identifier is refused, not truncated: a clipped run_id is a
        different id that resolves to nothing (`_EXACT_IDENTITY_FIELDS`). This
        test used to expect the truncation the handler deliberately stopped
        doing."""
        event = {"approval_type": "schema_mapping", "pipeline_id": "test-pipeline-123", "run_id": "x" * 600}

        with self.assertRaises(ValueError) as context:
            approval_handler.validate_approval_request(event)

        self.assertIn("'run_id'", str(context.exception))
        self.assertIn("REFUSED rather than truncated", str(context.exception))


class TestApprovalRequestCreation(unittest.TestCase):
    """Test approval request creation and storage"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_approvals_table = Mock()
        self.mock_task_tokens_table = Mock()

        # Patch the module-level table objects and time
        self.approvals_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.approvals_table", self.mock_approvals_table
        )
        self.tokens_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.task_tokens_table", self.mock_task_tokens_table
        )
        self.time_patcher = patch("agentic_data_ingestion_approval_handler.lambda_function.time")

        self.approvals_patcher.start()
        self.tokens_patcher.start()
        self.mock_time = self.time_patcher.start()
        self.mock_time.time.return_value = 1609459200  # Fixed timestamp

        # Also patch state_table to avoid real DynamoDB calls in create_approval_request
        self.mock_state_table = Mock()
        self.state_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.state_table", self.mock_state_table
        )
        self.state_patcher.start()

        # Patch dynamodb resource used for state_table re-creation inside create_approval_request
        self.mock_dynamodb = Mock()
        self.mock_dynamodb.Table.return_value = self.mock_state_table
        self.dynamodb_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.dynamodb", self.mock_dynamodb
        )
        self.dynamodb_patcher.start()

    def tearDown(self):
        """Clean up patches"""
        self.approvals_patcher.stop()
        self.tokens_patcher.stop()
        self.time_patcher.stop()
        self.state_patcher.stop()
        self.dynamodb_patcher.stop()

    @patch("agentic_data_ingestion_approval_handler.lambda_function.get_pipeline_config")
    @patch("agentic_data_ingestion_approval_handler.lambda_function.get_approval_data")
    def test_create_approval_request_success(self, mock_get_approval_data, mock_get_pipeline_config):
        """Test successful approval request creation"""
        # Setup mocks
        mock_get_pipeline_config.return_value = {"approvals": {"timeout_hours": 24, "timeout_behavior": "manual"}}
        mock_get_approval_data.return_value = {"test_data": "value"}

        event = {
            "approval_type": "schema_mapping",
            "pipeline_id": "test-pipeline-123",
            "run_id": "run-456",
            "task_token": "token-789",
        }

        # Execute
        approval_id = approval_handler.create_approval_request(event)

        # Verify
        self.assertEqual(approval_id, "test-pipeline-123_schema_mapping_1609459200")
        self.mock_approvals_table.put_item.assert_called_once()

        # Check the item that was stored
        stored_item = self.mock_approvals_table.put_item.call_args[1]["Item"]
        self.assertEqual(stored_item["approval_id"], approval_id)
        self.assertEqual(stored_item["pipeline_id"], "test-pipeline-123")
        self.assertEqual(stored_item["approval_type"], "schema_mapping")
        self.assertEqual(stored_item["status"], "pending")

        # Verify task token was stored in dedicated table
        self.mock_task_tokens_table.put_item.assert_called_once()

    @patch("agentic_data_ingestion_approval_handler.lambda_function.get_pipeline_config")
    @patch("agentic_data_ingestion_approval_handler.lambda_function.get_approval_data")
    def test_create_approval_request_dynamodb_error(self, mock_get_approval_data, mock_get_pipeline_config):
        """Test approval request creation with DynamoDB error"""
        # Setup mocks
        mock_get_pipeline_config.return_value = {"approvals": {}}
        mock_get_approval_data.return_value = {}
        self.mock_approvals_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Test error"}}, "PutItem"
        )

        event = {"approval_type": "schema_mapping", "pipeline_id": "test-pipeline-123", "run_id": "run-456"}

        # Execute and verify exception
        with self.assertRaises(ClientError):
            approval_handler.create_approval_request(event)


class TestApprovalDecisionProcessing(unittest.TestCase):
    """Test approval decision processing and Step Functions integration

    Task-token callbacks follow the Send_Before_Status flow (P28b R3.1): the
    approval record carries only ``has_task_token`` (see
    ``create_approval_request``, approval_handler lambda_function.py:371) and
    the token itself lives in the dedicated task-tokens table written by
    ``store_task_token``. So these tests must wire ``task_tokens_table`` —
    an inline ``task_token`` on the approval record is a shape production
    never writes, so the ``has_task_token`` guard (lambda_function.py:536) is
    falsey and the handler correctly skips the whole callback block, writing
    the decision status without sending. That is what the pre-fix assertions
    tripped on: ``send_task_success`` called 0 times, not a token error.
    (TaskTokenNotFound is the different case where ``has_task_token`` is set
    but the token record is gone.) Ordering itself is pinned in
    tests/unit/lambdas/approval_handler/test_approval_send_ordering.py.
    """

    def setUp(self):
        """Set up test fixtures"""
        self.mock_approvals_table = Mock()
        self.mock_task_tokens_table = Mock()
        self.mock_stepfunctions = Mock()

        # Patch module-level objects
        self.approvals_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.approvals_table", self.mock_approvals_table
        )
        self.tokens_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.task_tokens_table", self.mock_task_tokens_table
        )
        self.stepfunctions_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.stepfunctions", self.mock_stepfunctions
        )

        # Patch state_table and dynamodb to avoid real calls in process_approval_decision
        self.mock_state_table = Mock()
        self.mock_dynamodb = Mock()
        self.mock_dynamodb.Table.return_value = self.mock_state_table
        self.state_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.state_table", self.mock_state_table
        )
        self.dynamodb_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.dynamodb", self.mock_dynamodb
        )

        self.approvals_patcher.start()
        self.tokens_patcher.start()
        self.stepfunctions_patcher.start()
        self.state_patcher.start()
        self.dynamodb_patcher.start()

    def tearDown(self):
        """Clean up patches"""
        self.approvals_patcher.stop()
        self.tokens_patcher.stop()
        self.stepfunctions_patcher.stop()
        self.state_patcher.stop()
        self.dynamodb_patcher.stop()

    def test_process_approval_decision_approve(self):
        """Test processing approval decision - approve"""
        # Setup mock approval record
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "schema_mapping",
            "status": "pending",
            "has_task_token": True,
            "run_id": "run-001",
        }

        self.mock_approvals_table.get_item.return_value = {"Item": approval_record}
        self.mock_task_tokens_table.get_item.return_value = {
            "Item": {"approval_id": "test-approval-123", "task_token": "test-token-456"}
        }

        # Execute
        result = approval_handler.process_approval_decision(
            approval_id="test-approval-123", decision="approved", comment="Looks good", user="test-user"
        )

        # Verify DynamoDB update
        self.mock_approvals_table.update_item.assert_called_once()
        update_call = self.mock_approvals_table.update_item.call_args
        self.assertEqual(update_call[1]["Key"]["approval_id"], "test-approval-123")
        self.assertIn(":status", update_call[1]["ExpressionAttributeValues"])
        self.assertEqual(update_call[1]["ExpressionAttributeValues"][":status"], "approved")

        # Verify Step Functions success — token read from the dedicated table
        self.mock_stepfunctions.send_task_success.assert_called_once()
        success_call = self.mock_stepfunctions.send_task_success.call_args
        self.assertEqual(success_call[1]["taskToken"], "test-token-456")
        self.mock_task_tokens_table.get_item.assert_called_once_with(Key={"approval_id": "test-approval-123"})
        # Token consumed by the successful send — its record is cleaned up
        self.mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": "test-approval-123"})

        # Verify result
        self.assertEqual(result["approval_id"], "test-approval-123")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["processed_by"], "test-user")

    def test_process_approval_decision_reject(self):
        """Test processing approval decision - reject"""
        # Setup mock approval record
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "code_generation",
            "status": "pending",
            "has_task_token": True,
            "run_id": "run-001",
        }

        self.mock_approvals_table.get_item.return_value = {"Item": approval_record}
        self.mock_task_tokens_table.get_item.return_value = {
            "Item": {"approval_id": "test-approval-123", "task_token": "test-token-456"}
        }

        # Execute
        result = approval_handler.process_approval_decision(
            approval_id="test-approval-123", decision="rejected", comment="Code quality issues", user="test-user"
        )

        # Verify Step Functions failure — token read from the dedicated table
        self.mock_stepfunctions.send_task_failure.assert_called_once()
        failure_call = self.mock_stepfunctions.send_task_failure.call_args
        self.assertEqual(failure_call[1]["taskToken"], "test-token-456")
        self.assertEqual(failure_call[1]["error"], "ApprovalRejected")
        self.mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": "test-approval-123"})

        # Verify result
        self.assertEqual(result["decision"], "rejected")

    def test_process_approval_decision_not_found(self):
        """Test processing decision for non-existent approval"""
        self.mock_approvals_table.get_item.return_value = {}  # No item found

        with self.assertRaises(ValueError) as context:
            approval_handler.process_approval_decision(approval_id="non-existent", decision="approved")

        self.assertIn("Approval not found", str(context.exception))

    def test_process_approval_decision_already_processed(self):
        """Test processing decision for already processed approval"""
        approval_record = {
            "approval_id": "test-approval-123",
            "status": "approved",  # Already processed
        }

        self.mock_approvals_table.get_item.return_value = {"Item": approval_record}

        with self.assertRaises(ValueError) as context:
            approval_handler.process_approval_decision(approval_id="test-approval-123", decision="approved")

        self.assertIn("Approval already processed", str(context.exception))

    def test_process_approval_decision_invalid_decision(self):
        """Test processing with invalid decision"""
        approval_record = {"approval_id": "test-approval-123", "status": "pending"}

        self.mock_approvals_table.get_item.return_value = {"Item": approval_record}

        with self.assertRaises(ValueError) as context:
            approval_handler.process_approval_decision(approval_id="test-approval-123", decision="invalid")

        self.assertIn("Invalid decision", str(context.exception))


class TestTimeoutHandling(unittest.TestCase):
    """Test timeout handling and escalation logic

    ``_send_timeout_callback`` follows the same Send_Before_Status flow as the
    approval handler (P28b R3.2): it short-circuits unless the record says
    ``has_task_token`` (timeout_handler lambda_function.py:193) and then reads
    the token from the task-tokens table, so both must be wired here.
    """

    def setUp(self):
        """Set up test fixtures"""
        self.mock_approvals_table = Mock()
        self.mock_task_tokens_table = Mock()
        self.mock_stepfunctions = Mock()
        self.mock_cloudwatch = Mock()

        # Patch module-level objects in the timeout handler
        self.approvals_patcher = patch(
            "agentic_data_ingestion_approval_timeout_handler.lambda_function.approvals_table", self.mock_approvals_table
        )
        self.tokens_patcher = patch(
            "agentic_data_ingestion_approval_timeout_handler.lambda_function.task_tokens_table",
            self.mock_task_tokens_table,
        )
        self.stepfunctions_patcher = patch(
            "agentic_data_ingestion_approval_timeout_handler.lambda_function.stepfunctions", self.mock_stepfunctions
        )
        self.cloudwatch_patcher = patch(
            "agentic_data_ingestion_approval_timeout_handler.lambda_function.cloudwatch", self.mock_cloudwatch
        )

        self.approvals_patcher.start()
        self.tokens_patcher.start()
        self.stepfunctions_patcher.start()
        self.cloudwatch_patcher.start()

    def tearDown(self):
        """Clean up patches"""
        self.approvals_patcher.stop()
        self.tokens_patcher.stop()
        self.stepfunctions_patcher.stop()
        self.cloudwatch_patcher.stop()

    def test_scan_expired_approvals(self):
        """Test scanning for expired approvals"""
        # Mock expired approvals
        expired_approvals = [
            {
                "approval_id": "expired-1",
                "pipeline_id": "pipeline-1",
                "status": "pending",
                "timeout_at": "2023-01-01T00:00:00",
            },
            {
                "approval_id": "expired-2",
                "pipeline_id": "pipeline-2",
                "status": "pending",
                "timeout_at": "2023-01-01T01:00:00",
            },
        ]

        self.mock_approvals_table.scan.return_value = {"Items": expired_approvals}

        # Execute
        result = timeout_handler.scan_expired_approvals()

        # Verify
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["approval_id"], "expired-1")
        self.assertEqual(result[1]["approval_id"], "expired-2")

        # Verify scan parameters
        scan_call = self.mock_approvals_table.scan.call_args
        self.assertIn("FilterExpression", scan_call[1])

    def test_auto_approve_timeout(self):
        """Test auto-approve timeout behavior"""
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "schema_mapping",
            "run_id": "run-456",
            "has_task_token": True,
        }
        self.mock_task_tokens_table.get_item.return_value = {
            "Item": {"approval_id": "test-approval-123", "task_token": "test-token"}
        }

        # Execute
        result = timeout_handler.auto_approve_timeout(approval_record)

        # Verify
        self.assertTrue(result)

        # Verify DynamoDB update
        self.mock_approvals_table.update_item.assert_called_once()
        update_call = self.mock_approvals_table.update_item.call_args
        self.assertEqual(update_call[1]["ExpressionAttributeValues"][":status"], "approved")
        self.assertEqual(update_call[1]["ExpressionAttributeValues"][":processed_by"], "system_timeout")

        # Verify Step Functions success — token read from the dedicated table
        self.mock_stepfunctions.send_task_success.assert_called_once()
        success_call = self.mock_stepfunctions.send_task_success.call_args
        self.assertEqual(success_call[1]["taskToken"], "test-token")
        output = json.loads(success_call[1]["output"])
        self.assertEqual(output["approval_decision"], "approved")
        self.assertTrue(output["timeout_auto_approved"])
        # Token consumed by the successful send — its record is cleaned up
        self.mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": "test-approval-123"})

    def test_auto_reject_timeout(self):
        """Test auto-reject timeout behavior"""
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "code_generation",
            "run_id": "run-456",
            "has_task_token": True,
        }
        self.mock_task_tokens_table.get_item.return_value = {
            "Item": {"approval_id": "test-approval-123", "task_token": "test-token"}
        }

        # Execute
        result = timeout_handler.auto_reject_timeout(approval_record)

        # Verify
        self.assertTrue(result)

        # Verify DynamoDB update
        self.mock_approvals_table.update_item.assert_called_once()
        update_call = self.mock_approvals_table.update_item.call_args
        self.assertEqual(update_call[1]["ExpressionAttributeValues"][":status"], "rejected")

        # Verify Step Functions failure — token read from the dedicated table
        self.mock_stepfunctions.send_task_failure.assert_called_once()
        failure_call = self.mock_stepfunctions.send_task_failure.call_args
        self.assertEqual(failure_call[1]["taskToken"], "test-token")
        self.assertEqual(failure_call[1]["error"], "ApprovalTimeout")
        # Token consumed by the successful send — its record is cleaned up
        self.mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": "test-approval-123"})

    def test_manual_timeout_handling(self):
        """Test manual timeout handling"""
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "first_run",
        }

        # Execute
        result = timeout_handler.manual_timeout_handling(approval_record)

        # Verify
        self.assertTrue(result)

        # Verify DynamoDB update (marks as timed out but keeps pending)
        self.mock_approvals_table.update_item.assert_called_once()
        update_call = self.mock_approvals_table.update_item.call_args
        self.assertEqual(update_call[1]["ExpressionAttributeValues"][":timeout_occurred"], True)

        # Verify no Step Functions calls (stays pending)
        self.mock_stepfunctions.send_task_success.assert_not_called()
        self.mock_stepfunctions.send_task_failure.assert_not_called()

    @patch("agentic_data_ingestion_approval_timeout_handler.lambda_function.create_timeout_alarm")
    def test_process_approval_timeout_manual(self, mock_create_alarm):
        """Test processing timeout with manual behavior"""
        approval_record = {
            "approval_id": "test-approval-123",
            "pipeline_id": "test-pipeline",
            "approval_type": "schema_mapping",
            "timeout_behavior": "manual",
        }

        # Execute
        result = timeout_handler.process_approval_timeout(approval_record)

        # Verify
        self.assertTrue(result)
        mock_create_alarm.assert_called_once_with(approval_record)

    def test_create_timeout_alarm(self):
        """Test CloudWatch alarm creation for manual timeouts"""
        approval_record = {"approval_id": "test-approval-123", "pipeline_id": "test-pipeline"}

        # Execute
        timeout_handler.create_timeout_alarm(approval_record)

        # Verify CloudWatch metric and alarm creation
        self.mock_cloudwatch.put_metric_data.assert_called_once()
        self.mock_cloudwatch.put_metric_alarm.assert_called_once()

        # Check alarm parameters
        alarm_call = self.mock_cloudwatch.put_metric_alarm.call_args
        self.assertEqual(alarm_call[1]["AlarmName"], "AgenticDataIngestion-Approval-Timeout-test-approval-123")
        self.assertEqual(alarm_call[1]["MetricName"], "ManualTimeoutEscalation")


class TestApprovalDataRetrieval(unittest.TestCase):
    """Test approval data storage and retrieval"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_state_table = Mock()

        # Patch the module-level state_table
        self.state_table_patcher = patch(
            "agentic_data_ingestion_approval_handler.lambda_function.state_table", self.mock_state_table
        )
        self.state_table_patcher.start()

    def tearDown(self):
        """Clean up patches"""
        self.state_table_patcher.stop()

    def test_get_approval_data_schema_mapping(self):
        """Test retrieving schema mapping approval data.

        p50 C1 — DELIBERATE FIXTURE REVERSAL, name kept verbatim, and the second
        instance of the same reversal in this file. This test used to seed the
        mapping keys at the state row's TOP level and assert the handler read
        them back. No producer writes them there: create_approval_payload
        (schema_mapping/lambda_function.py:1732-1780) builds them and
        store_mapping_result stores the payload whole at output_data.approval
        (:1927). Measured live on run run-2026-08-24-13-48-26-4fccdf03
        (the validation account), the approval record carried 237 characters with eight of
        nine keys null — the same outcome the code_generation twin below had, and
        the twin LGR-86698d4-1 (template / ratified) predicted by name.

        The fixture below is the producer's real shape. Amending the fixture
        rather than teaching the handler the fixture's shape follows
        LGR-3650694-1 (template / ratified), which set that direction for this
        file; here, as with code_generation, the handler moves too, because the
        handler was the party disagreeing with the producer.

        evidence and questions are NOT part of that payload and keep their
        top-level read — their writer is the complex-mapping agent's update_item
        at schema_mapping/lambda_function.py:2277-2291.
        """
        # Mock state table response — key is (run_id, step_name)
        mock_item = {
            "run_id": "run-456",
            "step_name": "schema_mapping",
            "output_data": {
                "artifact_type": "schema_mapping",
                "approval": {
                    "approval_type": "schema_mapping",
                    "source_schema": {"field1": "string"},
                    "target_schema": {"field2": "string"},
                    "proposed_mappings": [{"source": "field1", "target": "field2"}],
                    "confidence_scores": {"field1->field2": 0.95},
                    "unmapped_fields": [],
                },
            },
        }

        self.mock_state_table.get_item.return_value = {"Item": mock_item}

        # Execute
        result = approval_handler.get_approval_data("schema_mapping", "run-456")

        # Verify
        self.assertEqual(result["source_schema"], {"field1": "string"})
        self.assertEqual(result["target_schema"], {"field2": "string"})
        self.assertEqual(len(result["proposed_mappings"]), 1)
        self.assertEqual(result["confidence_scores"]["field1->field2"], 0.95)

    def test_get_approval_data_code_generation(self):
        """Test retrieving code generation approval data.

        p34 R3.6 — DELIBERATE FIXTURE REVERSAL, name kept verbatim. This test
        used to seed seven attributes at the two state rows' TOP level
        (generated_code_s3_uri / template_used / code_quality_metrics /
        strands_agent_reasoning on create_code; test_results /
        test_success_rate / performance_metrics on test_code) and assert the
        handler read them back. No producer writes any of them there, so it
        pinned a shape that only ever existed inside this test: in the live
        account the one code_generation approval ever created carried those
        seven keys with seven nulls.

        The fixtures below are the producers' real attribute sets —
        create_code stores its payload at output_data.approval
        (create_code/lambda_function.py:839) and test_code writes test_result,
        singular (test_code/lambda_function.py:1722). Amending the fixture to
        the production shape rather than teaching the handler the fixture's
        shape follows LGR-3650694-1 (template / ratified), which set that
        direction for this file; here the handler moves too, because the
        handler was the party disagreeing with the producers.
        """
        # create_code's state row — the payload lives inside output_data.
        code_item = {
            "run_id": "run-456",
            "step_name": "create_code",
            "output_data": {
                "code_version_id": "cv-789",
                "code_s3_uri": "s3://bucket/code.py",
                "approval": {
                    "approval_type": "code_generation",
                    "code_version_id": "cv-789",
                    "generated_code_s3_uri": "s3://bucket/code.py",
                    "generated_code_preview": "import sys\n",
                    "code_quality_metrics": {"lines_of_code": 120},
                    "dqdl_rules": 'Rules = [ IsComplete "id" ]',
                    "strands_agent_reasoning": "Selected template based on...",
                    "template_selection": {"template_used": "basic_etl"},
                },
            },
        }

        # test_code's state row — test_result, singular.
        test_item = {
            "run_id": "run-456",
            "step_name": "test_code",
            "test_result": {
                "status": "passed",
                "checks": [
                    {"name": "syntax_check", "passed": True, "detail": "compiles"},
                    {"name": "transform_execution", "passed": True, "detail": "9 rows"},
                ],
                "report_s3_uri": "s3://test-outputs/run-456/test_report.md",
            },
        }

        self.mock_state_table.get_item.side_effect = [
            {"Item": code_item},  # First call for code data
            {"Item": test_item},  # Second call for test data
        ]

        # Execute — no inline payload, so the test_code row is the fallback.
        result = approval_handler.get_approval_data("code_generation", "run-456")

        # Verify: the code fields a reviewer needs, from where they are written.
        # code_version_id is the one ApprovalArtifact renders the CodeViewer on.
        self.assertEqual(result["code_version_id"], "cv-789")
        self.assertEqual(result["generated_code_s3_uri"], "s3://bucket/code.py")
        self.assertEqual(result["template_used"], "basic_etl")
        self.assertEqual(result["code_quality_metrics"]["lines_of_code"], 120)
        self.assertEqual(result["strands_agent_reasoning"], "Selected template based on...")
        self.assertEqual(result["dqdl_rules"], 'Rules = [ IsComplete "id" ]')
        # R3.6: the structured test result, including the checks list.
        self.assertEqual(result["test_status"], "passed")
        self.assertEqual([c["name"] for c in result["test_result"]["checks"]], ["syntax_check", "transform_execution"])
        self.assertEqual(result["report_s3_uri"], "s3://test-outputs/run-456/test_report.md")
        # The seven-null shape cannot recur: no key in the payload is None.
        self.assertNotIn(None, result.values())

    def test_get_approval_data_code_generation_prefers_the_inline_test_result(self):
        """The inline SFN payload wins over the state row (R3.6).

        WaitForCodeApproval carries 'test_result.$': '$.test_code_result.test_result'
        (orchestration-stack.ts:639), the same key ForceTestReviewApproval passes
        at :606, so the state-row read is the fallback rather than the source.
        """
        code_item = {
            "run_id": "run-456",
            "step_name": "create_code",
            "output_data": {"approval": {"code_version_id": "cv-789"}},
        }
        inline_result = {
            "status": "passed",
            "checks": [{"name": "output_validation", "passed": True, "detail": "df_transformed present"}],
            "report_s3_uri": "s3://test-outputs/run-456/test_report.md",
        }

        self.mock_state_table.get_item.side_effect = [{"Item": code_item}]

        result = approval_handler.get_approval_data(
            "code_generation",
            "run-456",
            {"approval_type": "code_generation", "run_id": "run-456", "test_result": inline_result},
        )

        self.assertEqual(result["test_result"], inline_result)
        self.assertEqual(result["code_version_id"], "cv-789")
        # Only the create_code row was read — the inline payload short-circuits
        # the test_code lookup.
        self.assertEqual(self.mock_state_table.get_item.call_count, 1)

    def test_get_approval_data_first_run(self):
        """Test retrieving first run approval data.

        p50 C5 — DELIBERATE FIXTURE REVERSAL, name kept verbatim, and the THIRD
        instance of the same reversal in this file (see the schema_mapping and
        code_generation tests above). This is the test the handler's own note
        used to name as the thing pinning the fiction: the branch read
        ``quality_checks`` and ``data_quality_rules`` off the
        ``create_quality_checks`` row's TOP level, the note said "that writer
        never writes them there", and this test seeded them there so the read
        came back green anyway.

        WHAT THE TWO ASSERTIONS WERE PINNING, before changing them. Not "the
        keys are at the top level" — that was the fixture's doing. Their subject
        was that the two facts *reach the approver at all*: a quality-checks
        list, and a numeric quality threshold. Both subjects survive the
        amendment; only the addresses and the rule shape change.

        The quality row below is now ``FIRST_RUN_QUALITY_CHECKS_STATE_ROW`` from
        ``tests/fixtures/conftest.py`` rather than a hand-written dict, because
        a hand-written "producer shape" with no producer behind it is how this
        fiction got here in the first place. One fixture now answers "what does
        ``create_quality_checks`` write" for both this layer and the unit layer
        (``test_first_run_payload_read.py``), so the two cannot drift apart.
        Amending the fixture to the production shape rather than teaching the
        handler the fixture's shape follows ``LGR-3650694-1`` (template /
        ratified), which set that direction for this file; here, as in the two
        tests above, the handler moves too, because the handler was the party
        disagreeing with the producer.

        Deliberately NOT re-asserted here — the unit layer owns it: the
        top-level decoy case, the ``quality_configuration`` second source, the
        degraded-row cases, and the post-``json.dumps`` ``Decimal`` conversion
        are all in ``test_first_run_payload_read.py``. What this test keeps is
        the integration-layer subject: two ``get_item`` calls against two
        different rows merge into one payload.

        The run id is the live run the spec measured
        (``run-2026-08-24-13-48-26-4fccdf03``) rather than this file's usual
        ``run-456``, so the row, the infra row and the call all name one run.
        """
        # Mock infrastructure response — key is (run_id, step_name).
        #
        # KEPT AS A HAND-WRITTEN TOP-LEVEL FIXTURE, ON PURPOSE. These three
        # attributes are pinning fiction too — nothing writes any of them at
        # this row's top level either, and the handler's note beside the read
        # (approval_handler/lambda_function.py:453-485) works through all three.
        # They are OUT of R4.1's scope, which names exactly two keys, both on
        # the create_quality_checks row, so amending them here would enlarge a
        # reversal this spec sized and tested for two. Their nullness against a
        # faithful create_infrastructure row is pinned in the unit layer by
        # test_the_out_of_scope_infra_reads_are_still_null; this fixture stays
        # as it was so that the follow-up which fixes them has an assertion to
        # reverse, exactly as this test was for the two keys below.
        infra_item = {
            "run_id": FIRST_RUN_DECIMAL_RUN_ID,
            "step_name": "create_infrastructure",
            "infrastructure_status": "deployed",
            "cloudformation_stack_id": "stack-123",
            "glue_job_name": "test-job",
        }

        # Mock quality checks response — the producer's real row. Deep-copied
        # because the constant is shared module state.
        quality_item = copy.deepcopy(FIRST_RUN_QUALITY_CHECKS_STATE_ROW)
        producer_approval = quality_item["output_data"]["approval"]
        producer_quality_checks = producer_approval["pre_run_validation"]["quality_checks"]
        producer_quality_rules = producer_approval["quality_check_configuration"]["quality_rules"]

        self.mock_state_table.get_item.side_effect = [{"Item": infra_item}, {"Item": quality_item}]

        # Execute
        result = approval_handler.get_approval_data("first_run", FIRST_RUN_DECIMAL_RUN_ID)

        # Verify
        # The infra half — see the note on the fixture above. Green because the
        # fixture feeds the read its own invention, not because the read is right.
        self.assertEqual(result["infrastructure_status"], "deployed")
        self.assertEqual(result["glue_job_name"], "test-job")

        # AMENDED (p50 C5): was `len(result["quality_checks"]) == 2` against a
        # top-level ["null_check", "format_check"]. Same subject — the checks
        # list reaches the approver — read from where the writer puts it,
        # output_data.approval.pre_run_validation.quality_checks. Asserted
        # against the producer's own list rather than a count: only the first of
        # those lines is derived from anything and the other three are
        # unconditional literals in the writer, so a bare count would read as an
        # endorsement of boilerplate C5 deliberately does not fix.
        self.assertEqual(result["quality_checks"], producer_quality_checks)

        # AMENDED (p50 C5): was `result["data_quality_rules"]["null_threshold"]
        # == 0.05`. Here the SHAPE changed and not just the address — the
        # writer's rules are a LIST OF DICTS keyed by rule_name / severity, and
        # no writer ever produced a {"null_threshold": ...} mapping. The old
        # assertion's subject, a numeric threshold reaching the approver intact,
        # survives as the range rule's max_value, which is a Decimal at this
        # point because the whole item goes through convert_floats_to_decimal on
        # write (the conversion to float happens later, at the serialization
        # boundary — LGR-86698d4-1, template / ratified).
        self.assertEqual(result["data_quality_rules"], producer_quality_rules)
        range_rule = next(r for r in result["data_quality_rules"] if r["rule_name"] == "customer_id_range_check")
        self.assertEqual(range_rule["max_value"], Decimal("999999.5"))

        # Both rows were read and both contributed — the integration-layer
        # subject this test keeps sole ownership of.
        self.assertEqual(self.mock_state_table.get_item.call_count, 2)

    def test_get_approval_data_missing_data(self):
        """Test retrieving approval data when state data is missing"""
        self.mock_state_table.get_item.return_value = {}  # No item found

        # Execute
        result = approval_handler.get_approval_data("schema_mapping", "run-456")

        # Verify returns empty dict when no data found
        self.assertEqual(result, {})


class TestLambdaHandlerIntegration(unittest.TestCase):
    """Test Lambda handler integration and end-to-end flows"""

    @patch("agentic_data_ingestion_approval_handler.lambda_function.create_approval_request")
    @patch("agentic_data_ingestion_approval_handler.lambda_function.send_approval_notifications")
    @patch("agentic_data_ingestion_approval_handler.lambda_function.approvals_table")
    def test_lambda_handler_create_approval(self, mock_table, mock_send_notifications, mock_create_request):
        """Test Lambda handler for creating approval request"""
        # Setup mocks
        mock_create_request.return_value = "test-approval-123"
        mock_table.get_item.return_value = {
            "Item": {"approval_id": "test-approval-123", "pipeline_id": "test-pipeline"}
        }

        event = {"approval_type": "schema_mapping", "pipeline_id": "test-pipeline-123", "run_id": "run-456"}

        # Execute
        response = approval_handler.lambda_handler(event, {})

        # Verify
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["approval_id"], "test-approval-123")
        self.assertEqual(body["status"], "pending")

        mock_create_request.assert_called_once()
        mock_send_notifications.assert_called_once()

    @patch("agentic_data_ingestion_approval_handler.lambda_function.process_approval_decision")
    def test_lambda_handler_process_decision(self, mock_process_decision):
        """Test Lambda handler for processing approval decision"""
        # Setup mock
        mock_process_decision.return_value = {
            "approval_id": "test-approval-123",
            "decision": "approved",
            "processed_by": "test-user",
        }

        event = {
            "approval_id": "test-approval-123",
            "decision": "approved",
            "comment": "Looks good",
            "user": "test-user",
        }

        # Execute
        response = approval_handler.lambda_handler(event, {})

        # Verify
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertIn("Approval approved successfully", body["message"])

        mock_process_decision.assert_called_once_with(
            approval_id="test-approval-123", decision="approved", comment="Looks good", user="test-user"
        )

    def test_lambda_handler_invalid_event(self):
        """Test Lambda handler with invalid event format"""
        event = {"invalid_field": "value"}

        # Execute
        response = approval_handler.lambda_handler(event, {})

        # Verify error response
        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertEqual(body["error"], "ValidationError")

    @patch("agentic_data_ingestion_approval_timeout_handler.lambda_function.scan_expired_approvals")
    @patch("agentic_data_ingestion_approval_timeout_handler.lambda_function.process_approval_timeout")
    def test_timeout_handler_lambda(self, mock_process_timeout, mock_scan_expired):
        """Test timeout handler Lambda function"""
        # Setup mocks
        expired_approvals = [
            {
                "approval_id": "expired-1",
                "pipeline_id": "pipeline-1",
                "approval_type": "schema_mapping",
                "timeout_behavior": "auto_approve",
            }
        ]

        mock_scan_expired.return_value = expired_approvals
        mock_process_timeout.return_value = True

        # Execute
        response = timeout_handler.lambda_handler({}, {})

        # Verify
        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["message"], "Timeout processing completed")
        self.assertEqual(body["report"]["total_timeouts_processed"], 1)

        mock_scan_expired.assert_called_once()
        mock_process_timeout.assert_called_once_with(expired_approvals[0])


if __name__ == "__main__":
    # Set up environment variables for testing
    os.environ["APPROVALS_TABLE"] = "test-approvals-table"
    os.environ["STATE_TABLE"] = "test-state-table"
    os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:us-east-1:123456789012:test-topic"

    # Run the tests
    unittest.main(verbosity=2)
