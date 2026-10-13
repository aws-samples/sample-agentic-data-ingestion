# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
EventBridge Event Publishing Utility for AgenticDataIngestion Pipeline Operations

This module provides standardized event publishing for pipeline state changes
and approval status updates across all Lambda functions.
"""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

# P28 R19.2: single shared Decimal→float converter (replaces the two nested
# ``convert_decimals_for_json`` copies; layer 0 → layer 2 import is allowed).
from decimal_utils import convert_decimals_to_float

logger = logging.getLogger()

# Environment variables
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "default")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

# Construct custom event bus name if using default
if EVENT_BUS_NAME == "default":
    EVENT_BUS_NAME = f"agentic-data-ingestion-pipeline-events-{ENVIRONMENT}"

# Initialize EventBridge client
eventbridge_client = boto3.client("events")


class EventBridgePublisher:
    """Standardized EventBridge event publisher for AgenticDataIngestion pipeline operations"""

    def __init__(self, event_bus_name: str = None):
        self.event_bus_name = event_bus_name or EVENT_BUS_NAME
        self.source = "agentic_data_ingestion.pipeline"

    def publish_pipeline_state_change(
        self,
        pipeline_id: str,
        execution_id: str,
        stage: str,
        status: str,
        run_id: str = None,
        metrics: dict[str, Any] = None,
        error_details: str = None,
        mcp_operations: list[dict[str, Any]] = None,
    ) -> bool:
        """
        Publish pipeline state change event

        Args:
            pipeline_id: Pipeline identifier
            execution_id: Step Functions execution ID
            stage: Lambda function name (e.g., agentic_data_ingestion_schema_extract)
            status: completed|failed|running|pending
            run_id: Run identifier for grouping pipeline steps
            metrics: Performance and processing metrics
            error_details: Error information if status is failed
            mcp_operations: MCP operation details and performance

        Returns:
            bool: True if event published successfully, False otherwise
        """
        try:
            detail = {
                "pipeline_id": pipeline_id,
                "execution_id": execution_id,
                "stage": stage,
                "status": status,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Add optional fields
            if run_id:
                detail["run_id"] = run_id
            if metrics:
                detail["metrics"] = metrics
            if error_details:
                detail["error_details"] = error_details
            if mcp_operations:
                detail["mcp_operations"] = mcp_operations

            # Convert Decimals to floats for JSON serialization (shared decimal_utils)
            detail = convert_decimals_to_float(detail)

            event = {
                "Source": self.source,
                "DetailType": "Pipeline State Change",
                "Detail": json.dumps(detail),
                "EventBusName": self.event_bus_name,
            }

            response = eventbridge_client.put_events(Entries=[event])

            if response["FailedEntryCount"] > 0:
                logger.error(
                    f"Failed to publish pipeline state change event: {response['Entries'][0].get('ErrorMessage', 'Unknown error')}"
                )
                return False

            logger.info(f"Published pipeline state change event: {pipeline_id} - {stage} - {status}")
            return True

        except Exception as e:
            logger.error(f"Error publishing pipeline state change event: {e}")
            return False

    def publish_approval_status_change(
        self,
        approval_id: str,
        pipeline_id: str,
        approval_type: str,
        status: str,
        run_id: str = None,
        execution_id: str = None,
        approver: str = None,
        decision_details: dict[str, Any] = None,
    ) -> bool:
        """
        Publish approval status change event

        Args:
            approval_id: Unique approval identifier
            pipeline_id: Pipeline identifier
            approval_type: schema_mapping|code_generation|first_run
            status: pending|approved|rejected|timeout
            run_id: Run identifier for grouping
            execution_id: Step Functions execution ID
            approver: User who made the approval decision
            decision_details: Additional approval decision context

        Returns:
            bool: True if event published successfully, False otherwise
        """
        try:
            detail = {
                "approval_id": approval_id,
                "pipeline_id": pipeline_id,
                "approval_type": approval_type,
                "status": status,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Add optional fields
            if run_id:
                detail["run_id"] = run_id
            if execution_id:
                detail["execution_id"] = execution_id
            if approver:
                detail["approver"] = approver
            if decision_details:
                detail["decision_details"] = decision_details

            # Convert Decimals to floats for JSON serialization (shared decimal_utils)
            detail = convert_decimals_to_float(detail)

            event = {
                "Source": self.source,
                "DetailType": "Approval Status Change",
                "Detail": json.dumps(detail),
                "EventBusName": self.event_bus_name,
            }

            response = eventbridge_client.put_events(Entries=[event])

            if response["FailedEntryCount"] > 0:
                logger.error(
                    f"Failed to publish approval status change event: {response['Entries'][0].get('ErrorMessage', 'Unknown error')}"
                )
                return False

            logger.info(f"Published approval status change event: {approval_id} - {approval_type} - {status}")
            return True

        except Exception as e:
            logger.error(f"Error publishing approval status change event: {e}")
            return False

    def publish_mcp_operation_event(
        self,
        pipeline_id: str,
        operation_name: str,
        status: str,
        response_time_ms: int,
        cost_cents: float = None,
        fallback_reason: str = None,
        run_id: str = None,
        execution_id: str = None,
    ) -> bool:
        """
        Publish MCP operation performance event

        Args:
            pipeline_id: Pipeline identifier
            operation_name: MCP operation name
            status: success|failed|fallback
            response_time_ms: Operation response time in milliseconds
            cost_cents: Operation cost in cents
            fallback_reason: Reason for fallback if status is fallback
            run_id: Run identifier for grouping
            execution_id: Step Functions execution ID

        Returns:
            bool: True if event published successfully, False otherwise
        """
        try:
            detail = {
                "pipeline_id": pipeline_id,
                "operation_name": operation_name,
                "status": status,
                "response_time_ms": response_time_ms,
                "timestamp": datetime.now(UTC).isoformat(),
            }

            # Add optional fields
            if cost_cents is not None:
                detail["cost_cents"] = cost_cents
            if fallback_reason:
                detail["fallback_reason"] = fallback_reason
            if run_id:
                detail["run_id"] = run_id
            if execution_id:
                detail["execution_id"] = execution_id

            event = {
                "Source": self.source,
                "DetailType": "MCP Operation",
                "Detail": json.dumps(detail),
                "EventBusName": self.event_bus_name,
            }

            response = eventbridge_client.put_events(Entries=[event])

            if response["FailedEntryCount"] > 0:
                logger.error(
                    f"Failed to publish MCP operation event: {response['Entries'][0].get('ErrorMessage', 'Unknown error')}"
                )
                return False

            logger.info(f"Published MCP operation event: {pipeline_id} - {operation_name} - {status}")
            return True

        except Exception as e:
            logger.error(f"Error publishing MCP operation event: {e}")
            return False


# Convenience functions for easy import and use
def publish_pipeline_state_change(pipeline_id: str, execution_id: str, stage: str, status: str, **kwargs) -> bool:
    """Convenience function to publish pipeline state change"""
    publisher = EventBridgePublisher()
    return publisher.publish_pipeline_state_change(pipeline_id, execution_id, stage, status, **kwargs)


def publish_approval_status_change(
    approval_id: str, pipeline_id: str, approval_type: str, status: str, **kwargs
) -> bool:
    """Convenience function to publish approval status change"""
    publisher = EventBridgePublisher()
    return publisher.publish_approval_status_change(approval_id, pipeline_id, approval_type, status, **kwargs)


def publish_mcp_operation_event(
    pipeline_id: str, operation_name: str, status: str, response_time_ms: int, **kwargs
) -> bool:
    """Convenience function to publish MCP operation event"""
    publisher = EventBridgePublisher()
    return publisher.publish_mcp_operation_event(pipeline_id, operation_name, status, response_time_ms, **kwargs)
