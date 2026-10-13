# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
WebSocket Connect / Default Route Lambda.

Handles $connect and $default routes for the AgenticDataIngestion WebSocket API.
- $connect: stores connection ID with a default '__all__' subscription.
- $default: handles 'subscribe' action to create per-pipeline subscription records,
  refusing (400, no writes) a pipeline_ids list that is malformed or too long.

Trigger: API Gateway WebSocket $connect and $default routes.
Output:  Writes to agentic_data_ingestion_ws_connections DynamoDB table.
"""

import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

import boto3

# Lambda layer / local dev import paths
sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

from env_utils import require_env

logger = Logger(service="agentic_data_ingestion")

dynamodb = boto3.resource("dynamodb")
connections_table = dynamodb.Table(require_env("WS_CONNECTIONS_TABLE", "agentic_data_ingestion_ws_connections"))

# ── Subscribe limits ───────────────────────────────────────────────────────
# Every entry in a subscribe message becomes one row in the connections table,
# and the message comes from any authenticated socket, so the list is bounded
# here rather than trusted.
#
# How many ids the dashboard sends: WebSocketProvider.sendSubscriptions sends
# every pipeline topic it has registered (dashboard-react/src/providers/
# WebSocketProvider.tsx, `pipeline_ids: ids`), deduplicated by topic key. The
# only registrations come from useWebSocket, called with a single id by
# usePipelineExecutions and useRunDetail, and every page that calls those sits
# under /pipelines/:id, all with that route's one id. So a legitimate message
# carries one id. 50 is headroom for a future multi-pipeline view, not a
# measured need.
MAX_SUBSCRIBE_PIPELINE_IDS = 50

# How long one id may be. create_pipeline makes the id from a slug of the name
# plus "-" and 8 hex characters, and the pipeline's Glue job is named
# "agentic-data-ingestion-job-<id>", which Glue caps at 255 characters, so no
# working pipeline has an id over 228. 256 characters is also at most 1,024
# UTF-8 bytes, DynamoDB's limit for the table's `pipeline_id` sort key.
MAX_PIPELINE_ID_LENGTH = 256


def lambda_handler(event, context):
    """Route $connect and $default WebSocket events."""
    connection_id = event["requestContext"]["connectionId"]
    route_key = event["requestContext"].get("routeKey", "$connect")
    logger.append_keys(step_name="WSConnect", connection_id=connection_id)

    if route_key == "$connect":
        return _handle_connect(connection_id)
    elif route_key == "$default":
        return _handle_default(connection_id, event)

    return {"statusCode": 200}


def _handle_connect(connection_id: str) -> dict:
    """Store connection with default __all__ subscription."""
    connections_table.put_item(
        Item={
            "connection_id": connection_id,
            "pipeline_id": "__all__",
            "connected_at": _iso_now(),
            "ttl": int(time.time()) + 86400,  # 24-hour TTL
        }
    )
    logger.info("Connected: %s", connection_id)
    return {"statusCode": 200}


def _handle_default(connection_id: str, event: dict) -> dict:
    """Handle subscribe/unsubscribe messages on $default route.

    A message that is not a JSON object, or a subscribe whose `pipeline_ids`
    fails `_subscribe_refusal`, is refused whole: 400, and nothing is deleted
    or written. The list is never truncated to fit. The $default route has no
    route response, so API Gateway does not relay the 400 to the client; the
    reason is in the log.

    Whether each id names a pipeline is not checked: this function has no read
    grant on the pipelines table. An id that names nothing costs one row that
    no broadcast ever matches, which expires with the TTL.
    """
    raw_body = event.get("body")
    try:
        body = json.loads(raw_body) if raw_body else {}
    except (TypeError, ValueError, RecursionError):  # JSONDecodeError is a ValueError
        body = None
    if not isinstance(body, dict):
        logger.warning("Refused $default message from %s: body is not a JSON object", connection_id)
        return {"statusCode": 400}

    action = body.get("action")

    if action == "subscribe":
        pipeline_ids = body.get("pipeline_ids", [])
        refusal = _subscribe_refusal(pipeline_ids)
        if refusal is not None:
            logger.warning("Refused subscribe from %s: %s", connection_id, refusal)
            return {"statusCode": 400}
        unique_ids = list(dict.fromkeys(pipeline_ids))  # first-seen order, each id once

        # Remove __all__ placeholder
        connections_table.delete_item(
            Key={
                "connection_id": connection_id,
                "pipeline_id": "__all__",
            }
        )
        # Create one record per pipeline subscription
        for pid in unique_ids:
            connections_table.put_item(
                Item={
                    "connection_id": connection_id,
                    "pipeline_id": pid,
                    "connected_at": _iso_now(),
                    "ttl": int(time.time()) + 86400,
                }
            )
        logger.info("Subscribed %s to %d pipeline(s)", connection_id, len(unique_ids))

    return {"statusCode": 200}


def _subscribe_refusal(pipeline_ids: Any) -> str | None:
    """Why a subscribe's `pipeline_ids` is refused, or None when it is acceptable.

    Acceptable: a list of at most MAX_SUBSCRIBE_PIPELINE_IDS entries, each a
    non-empty string of at most MAX_PIPELINE_ID_LENGTH characters with no "/".
    The count is of the list as sent, before duplicates are dropped. The reason
    names an index and a type, never the caller's value, so the log line stays
    bounded.
    """
    if not isinstance(pipeline_ids, list):
        return f"pipeline_ids must be a list, not {type(pipeline_ids).__name__}"
    if len(pipeline_ids) > MAX_SUBSCRIBE_PIPELINE_IDS:
        return f"{len(pipeline_ids)} pipeline_ids is more than the limit of {MAX_SUBSCRIBE_PIPELINE_IDS}"
    for index, pid in enumerate(pipeline_ids):
        if not isinstance(pid, str) or not pid:
            return f"pipeline_ids[{index}] is not a non-empty string"
        if len(pid) > MAX_PIPELINE_ID_LENGTH:
            return f"pipeline_ids[{index}] is longer than {MAX_PIPELINE_ID_LENGTH} characters"
        if "/" in pid:
            return f"pipeline_ids[{index}] contains '/'"
    return None


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()
