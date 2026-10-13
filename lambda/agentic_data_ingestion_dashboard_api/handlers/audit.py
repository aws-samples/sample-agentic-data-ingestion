#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Audit log query handler for dashboard API.

Returns audit entries for a specific pipeline from the Audit_Table,
with optional ``limit`` and ``action`` filter parameters.
"""

from boto3.dynamodb.conditions import Attr, Key

from .common import audit_table, create_response, logger


def get_pipeline_audit(pipeline_id: str, query_params: dict) -> dict:
    """Return audit entries for a pipeline.

    Queries the Audit_Table by ``resource_id`` (pipeline_id), sorted by
    timestamp descending.  Supports optional query parameters:

    - ``limit``: maximum number of entries to return (default 50)
    - ``action``: filter entries by action type

    Returns:
        200 with ``{"audit_entries": [...], "count": N}``
    """
    try:
        limit = int(query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50

    action_filter = query_params.get("action")

    try:
        query_kwargs: dict = {
            "KeyConditionExpression": Key("resource_id").eq(pipeline_id),
            "ScanIndexForward": False,
            "Limit": limit,
        }

        if action_filter:
            query_kwargs["FilterExpression"] = Attr("action").eq(action_filter)

        resp = audit_table.query(**query_kwargs)
        entries = resp.get("Items", [])
    except Exception as exc:
        logger.error("Failed to query audit for %s: %s", pipeline_id, exc)
        return create_response(500, {"error": "Internal server error"})

    return create_response(
        200,
        {
            "audit_entries": entries,
            "count": len(entries),
        },
    )
