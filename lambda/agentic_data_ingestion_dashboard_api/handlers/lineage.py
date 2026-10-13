#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lineage query handler for dashboard API.

Returns the lineage chain for a specific pipeline run from the Executions_Table.
"""

from .common import create_response, executions_table, logger


def get_run_lineage(pipeline_id: str, run_id: str) -> dict:
    """Return the lineage chain for a pipeline run.

    Reads the ``lineage`` list attribute from the Executions_Table row
    identified by *pipeline_id* (PK) and *run_id* (SK).

    Returns:
        200 with ``{"run_id", "pipeline_id", "lineage": [...]}``
        404 when the run does not exist.
    """
    try:
        resp = executions_table.get_item(Key={"pipeline_id": pipeline_id, "run_id": run_id})
    except Exception as exc:
        logger.error("Failed to read lineage for %s/%s: %s", pipeline_id, run_id, exc)
        return create_response(500, {"error": "Internal server error"})

    if "Item" not in resp:
        return create_response(404, {"error": f"Run {run_id} not found for pipeline {pipeline_id}"})

    lineage = resp["Item"].get("lineage", [])
    return create_response(200, {"run_id": run_id, "pipeline_id": pipeline_id, "lineage": lineage})
