# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import boto3

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

# Initialize logger
from aws_lambda_powertools import Logger

from dynamo_typing import item_dict
from env_utils import require_env

logger = Logger(service="agentic_data_ingestion")

# Environment variables
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
pipelines_table = dynamodb.Table(PIPELINES_TABLE)


def get_pipeline_approval_config(pipeline_id: str) -> dict[str, Any]:
    """Get current approval configuration for a pipeline"""
    try:
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return {"error": f"Pipeline {pipeline_id} not found"}

        pipeline_item = response["Item"]
        approval_config = pipeline_item.get("approval_config", {})

        # Return default configuration if none exists
        if not approval_config:
            approval_config = {
                "schema_mapping_enabled": True,
                "code_generation_enabled": True,
                "first_run_enabled": True,
                "auto_approval_thresholds": {
                    "schema_mapping_confidence": 0.9,
                    "code_quality_score": 80,
                    "max_estimated_cost": 50,
                },
                "notification_settings": {"email_enabled": True, "slack_enabled": False, "timeout_hours": 24},
            }

        return {
            "pipeline_id": pipeline_id,
            "approval_config": approval_config,
            "last_updated": pipeline_item.get("approval_config_updated", "never"),
            "updated_by": pipeline_item.get("approval_config_updated_by", "system"),
        }

    except Exception as e:
        logger.error(f"Error getting pipeline approval config: {e}")
        return {"error": f"Failed to get approval config: {e!s}"}


def update_pipeline_approval_config(
    pipeline_id: str, approval_config: dict[str, Any], updated_by: str = "api"
) -> dict[str, Any]:
    """Update approval configuration for a pipeline"""
    try:
        timestamp = datetime.now(UTC).isoformat()

        # Validate approval configuration
        required_fields = ["schema_mapping_enabled", "code_generation_enabled", "first_run_enabled"]
        for field in required_fields:
            if field not in approval_config:
                return {"error": f"Missing required field: {field}"}

        # Validate boolean fields
        boolean_fields = ["schema_mapping_enabled", "code_generation_enabled", "first_run_enabled"]
        for field in boolean_fields:
            if not isinstance(approval_config[field], bool):
                return {"error": f"Field {field} must be boolean"}

        # Update pipeline with new approval configuration
        update_expression = "SET approval_config = :config, approval_config_updated = :timestamp, approval_config_updated_by = :updated_by"
        expression_values: dict[str, Any] = {
            ":config": approval_config,
            ":timestamp": timestamp,
            ":updated_by": updated_by,
        }

        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
        )

        logger.info(f"Updated approval config for pipeline {pipeline_id}")

        return {
            "pipeline_id": pipeline_id,
            "approval_config": approval_config,
            "last_updated": timestamp,
            "updated_by": updated_by,
            "status": "success",
        }

    except Exception as e:
        logger.error(f"Error updating pipeline approval config: {e}")
        return {"error": f"Failed to update approval config: {e!s}"}


def list_pipelines_with_approval_config() -> dict[str, Any]:
    """List all pipelines with their approval configurations"""
    try:
        response = pipelines_table.scan(
            ProjectionExpression="pipeline_id, pipeline_name, approval_config, approval_config_updated, #status",
            ExpressionAttributeNames={"#status": "status"},
        )

        pipelines = []
        for item in response.get("Items", []):
            pipeline_info = {
                "pipeline_id": item["pipeline_id"],
                "pipeline_name": item.get("pipeline_name", item["pipeline_id"]),
                "status": item.get("status", "unknown"),
                "approval_config": item.get("approval_config", {}),
                "approval_config_updated": item.get("approval_config_updated", "never"),
            }

            # Add approval summary
            approval_config = item_dict(item, "approval_config")
            if approval_config:
                pipeline_info["approval_summary"] = {
                    "schema_mapping": approval_config.get("schema_mapping_enabled", True),
                    "code_generation": approval_config.get("code_generation_enabled", True),
                    "first_run": approval_config.get("first_run_enabled", True),
                    "total_enabled": sum(
                        [
                            approval_config.get("schema_mapping_enabled", True),
                            approval_config.get("code_generation_enabled", True),
                            approval_config.get("first_run_enabled", True),
                        ]
                    ),
                }
            else:
                pipeline_info["approval_summary"] = {
                    "schema_mapping": True,
                    "code_generation": True,
                    "first_run": True,
                    "total_enabled": 3,
                }

            pipelines.append(pipeline_info)

        return {"pipelines": pipelines, "total_count": len(pipelines)}

    except Exception as e:
        logger.error(f"Error listing pipelines with approval config: {e}")
        return {"error": f"Failed to list pipelines: {e!s}"}


def create_default_approval_config() -> dict[str, Any]:
    """Create default approval configuration template"""
    return {
        "schema_mapping_enabled": True,
        "code_generation_enabled": True,
        "first_run_enabled": True,
        "auto_approval_thresholds": {
            "schema_mapping_confidence": 0.9,
            "code_quality_score": 80,
            "max_estimated_cost": 50,
        },
        "notification_settings": {
            "email_enabled": True,
            "slack_enabled": False,
            "timeout_hours": 24,
            "reminder_intervals": [4, 12, 24],  # Hours
        },
        "escalation_settings": {"enabled": False, "escalation_after_hours": 48, "escalation_contacts": []},
    }


def lambda_handler(event, context):
    """
    Lambda function to handle pipeline approval configuration API endpoints

    Supported operations:
    - GET /approval-config/{pipeline_id} - Get approval config for pipeline
    - PUT /approval-config/{pipeline_id} - Update approval config for pipeline
    - GET /approval-config - List all pipelines with approval configs
    - GET /approval-config/template - Get default approval config template
    """
    try:
        logger.info(f"Approval config API event: {json.dumps(event)}")

        logger.append_keys(step_name="ApprovalConfig")

        # Extract HTTP method and path
        http_method = event.get("httpMethod", "GET")
        path = event.get("path", "")
        path_parameters = event.get("pathParameters") or {}
        query_parameters = event.get("queryStringParameters") or {}

        # Parse request body for PUT/POST requests
        body = {}
        if event.get("body"):
            try:
                body = json.loads(event["body"])
            except json.JSONDecodeError:
                return {
                    "statusCode": 400,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": "Invalid JSON in request body"}),
                }

        # Route to appropriate handler
        if http_method == "GET":
            if path.endswith("/template"):
                # GET /approval-config/template
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"default_approval_config": create_default_approval_config()}),
                }
            elif "pipeline_id" in path_parameters:
                # GET /approval-config/{pipeline_id}
                pipeline_id = path_parameters["pipeline_id"]
                result = get_pipeline_approval_config(pipeline_id)
                status_code = 404 if "error" in result else 200
                return {
                    "statusCode": status_code,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(result),
                }
            else:
                # GET /approval-config
                result = list_pipelines_with_approval_config()
                status_code = 500 if "error" in result else 200
                return {
                    "statusCode": status_code,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(result),
                }

        elif http_method == "PUT":
            if "pipeline_id" in path_parameters:
                # PUT /approval-config/{pipeline_id}
                pipeline_id = path_parameters["pipeline_id"]
                approval_config = body.get("approval_config", {})
                updated_by = body.get("updated_by", "api")

                result = update_pipeline_approval_config(pipeline_id, approval_config, updated_by)
                status_code = 400 if "error" in result else 200
                return {
                    "statusCode": status_code,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(result),
                }
            else:
                return {
                    "statusCode": 400,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"error": "Pipeline ID required for PUT requests"}),
                }

        else:
            return {
                "statusCode": 405,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Method {http_method} not allowed"}),
            }

    except Exception as e:
        error_message = f"Error in approval config API: {e!s}"
        logger.error(error_message)

        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": error_message, "type": "InternalServerError"}),
        }
