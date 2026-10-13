# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
WebSocket Disconnect Lambda.

Handles $disconnect route — removes all subscription records for the
disconnected connection from the agentic_data_ingestion_ws_connections table.

Trigger: API Gateway WebSocket $disconnect route.
Output:  Deletes from agentic_data_ingestion_ws_connections DynamoDB table.
"""

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
    """Delete all subscription records for the disconnected connection."""
    connection_id = event["requestContext"]["connectionId"]
    logger.append_keys(step_name="WSDisconnect", connection_id=connection_id)

    # Query all subscription records for this connection
    response = connections_table.query(
        KeyConditionExpression=Key("connection_id").eq(connection_id),
    )

    items = response.get("Items", [])
    for item in items:
        connections_table.delete_item(
            Key={
                "connection_id": connection_id,
                "pipeline_id": item["pipeline_id"],
            }
        )

    logger.info("Disconnected %s — removed %d subscription(s)", connection_id, len(items))
    return {"statusCode": 200}
