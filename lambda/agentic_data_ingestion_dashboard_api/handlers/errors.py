# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Errors API handler — query the agentic_data_ingestion_errors table."""

# Initialize errors table from env var
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key

from env_utils import require_env
from handlers.common import create_response, handle_error, logger

_ERRORS_TABLE_NAME = require_env("ERRORS_TABLE", "agentic_data_ingestion_errors")
_dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
_errors_table = _dynamodb.Table(_ERRORS_TABLE_NAME)


def get_errors(event: dict[str, Any]) -> dict[str, Any]:
    """Query errors table with optional date range and filters.

    Query params:
        start_date  – ISO date string (default: 7 days ago)
        end_date    – ISO date string (default: today)
        pipeline_id – filter by pipeline
        error_type  – filter by error classification
    """
    try:
        params = event.get("queryStringParameters") or {}
        now = datetime.now(UTC)

        start_date = params.get("start_date", (now - timedelta(days=7)).strftime("%Y-%m-%d"))
        end_date = params.get("end_date", now.strftime("%Y-%m-%d"))
        pipeline_id = params.get("pipeline_id")
        error_type = params.get("error_type")

        logger.info(
            "Querying errors: start=%s end=%s pipeline=%s type=%s", start_date, end_date, pipeline_id, error_type
        )

        # Query each date partition in the range
        all_errors = []
        current = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            query_kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("error_date").eq(date_str),
            }

            # Build filter expression for optional filters
            filters = []
            if pipeline_id:
                filters.append(Attr("pipeline_id").eq(pipeline_id))
            if error_type:
                filters.append(Attr("error_type").eq(error_type))

            if filters:
                combined = filters[0]
                for f in filters[1:]:
                    combined = combined & f
                query_kwargs["FilterExpression"] = combined

            resp = _errors_table.query(**query_kwargs)
            all_errors.extend(resp.get("Items", []))
            current += timedelta(days=1)

        # Sort by timestamp descending (newest first)
        all_errors.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

        return create_response(
            200,
            {
                "errors": all_errors,
                "count": len(all_errors),
                "start_date": start_date,
                "end_date": end_date,
            },
        )

    except Exception as e:
        return handle_error(e, "Failed to query errors")
