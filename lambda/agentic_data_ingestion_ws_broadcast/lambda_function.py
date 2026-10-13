# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
WebSocket Broadcast Lambda.

Triggered by DynamoDB Streams on the agentic_data_ingestion_executions table.
For each stream record, queries the ws_connections table for all
subscribers (specific pipeline + __all__) and pushes the execution
update via the API Gateway Management API.

Trigger: DynamoDB Stream (NEW_IMAGE) on agentic_data_ingestion_executions table.
Output:  WebSocket messages to connected clients.
"""

import json
import os
import sys

import boto3
from boto3.dynamodb.conditions import Key

# Lambda layer / local dev import paths
sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

from env_utils import require_env

logger = Logger(service="agentic_data_ingestion")

dynamodb = boto3.resource("dynamodb")
connections_table = dynamodb.Table(require_env("WS_CONNECTIONS_TABLE", "agentic_data_ingestion_ws_connections"))


def lambda_handler(event, context):
    """Process DynamoDB Stream records and broadcast to WebSocket subscribers."""
    logger.append_keys(step_name="WSBroadcast")
    ws_endpoint = require_env("WS_API_ENDPOINT")
    # Convert wss:// URL to https:// for the Management API
    management_url = ws_endpoint.replace("wss://", "https://")
    apigw = boto3.client("apigatewaymanagementapi", endpoint_url=management_url)

    for record in event.get("Records", []):
        if record["eventName"] not in ("INSERT", "MODIFY"):
            continue

        new_image = record["dynamodb"].get("NewImage", {})
        pipeline_id = new_image.get("pipeline_id", {}).get("S", "")
        if not pipeline_id:
            continue

        logger.append_keys(pipeline_id=pipeline_id)

        # Build update message
        message = json.dumps(
            {
                "type": "execution_update",
                "pipeline_id": pipeline_id,
                "run_id": new_image.get("run_id", {}).get("S", ""),
                "status": new_image.get("status", {}).get("S", ""),
                "current_step": new_image.get("current_step", {}).get("S", ""),
                "updated_at": new_image.get("updated_at", {}).get("S", ""),
            }
        )

        # Query all connections subscribed to this pipeline
        connections = _get_subscribers(pipeline_id)
        if not connections:
            logger.debug("No subscribers for pipeline %s", pipeline_id)
            continue

        # Broadcast to each subscriber
        stale_connections = []
        for conn in connections:
            try:
                apigw.post_to_connection(
                    ConnectionId=conn["connection_id"],
                    Data=message.encode("utf-8"),
                )
            except apigw.exceptions.GoneException:
                logger.info("Stale connection: %s", conn["connection_id"])
                stale_connections.append(conn)
            except Exception as e:
                logger.warning("Failed to post to %s: %s", conn["connection_id"], e)

        # Clean up stale connections
        for conn in stale_connections:
            try:
                connections_table.delete_item(
                    Key={
                        "connection_id": conn["connection_id"],
                        "pipeline_id": conn["pipeline_id"],
                    }
                )
            except Exception as e:
                logger.warning("Failed to delete stale connection %s: %s", conn["connection_id"], e)

    return {"statusCode": 200}


def _get_subscribers(pipeline_id: str) -> list:
    """Get all connections subscribed to a pipeline (or __all__), deduplicated."""
    results = []

    # Specific pipeline subscribers
    response = connections_table.query(
        IndexName="PipelineIndex",
        KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
    )
    results.extend(response.get("Items", []))

    # __all__ subscribers (watching all pipelines)
    response = connections_table.query(
        IndexName="PipelineIndex",
        KeyConditionExpression=Key("pipeline_id").eq("__all__"),
    )
    results.extend(response.get("Items", []))

    # Deduplicate by connection_id
    seen = set()
    unique = []
    for r in results:
        if r["connection_id"] not in seen:
            seen.add(r["connection_id"])
            unique.append(r)
    return unique
