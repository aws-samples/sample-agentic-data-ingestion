#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cost aggregation handler for dashboard API.

Queries the State_Table PipelineIndex for RunPipeline steps, extracts
``actual_cost`` from ``output_data``, and computes a Cost_Summary.
"""

from boto3.dynamodb.conditions import Attr, Key

from dynamo_typing import item_dict

from .common import create_response, logger, state_table


def get_pipeline_costs(pipeline_id: str) -> dict:
    """Return aggregated cost data for all runs of a pipeline.

    Queries the State_Table PipelineIndex GSI for RunPipeline steps,
    extracts ``actual_cost`` from each record's ``output_data`` map,
    and computes total_cost, average_cost_per_run, cost_trend (last 7),
    and most_expensive_run.

    Returns:
        200 with Cost_Summary dict.
    """
    try:
        resp = state_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
            FilterExpression=Attr("step_name").is_in(["RunPipeline", "run_pipeline"]),
        )
        items = resp.get("Items", [])
    except Exception as exc:
        logger.error("Failed to query costs for %s: %s", pipeline_id, exc)
        return create_response(500, {"error": "Internal server error"})

    # Extract costs from output_data
    runs_with_cost: list[dict] = []
    for item in items:
        output_data = item_dict(item, "output_data")
        actual_cost = output_data.get("actual_cost")
        if actual_cost is not None:
            runs_with_cost.append(
                {
                    "run_id": item.get("run_id"),
                    "actual_cost": float(actual_cost),
                    "timestamp": item.get("timestamp", ""),
                }
            )

    # Sort newest first
    runs_with_cost.sort(key=lambda r: r["timestamp"], reverse=True)

    total = sum(r["actual_cost"] for r in runs_with_cost)
    avg = total / len(runs_with_cost) if runs_with_cost else 0
    trend = runs_with_cost[:7]
    most_expensive = max(runs_with_cost, key=lambda r: r["actual_cost"]) if runs_with_cost else None

    return create_response(
        200,
        {
            "pipeline_id": pipeline_id,
            "total_cost": round(total, 3),
            "average_cost_per_run": round(avg, 3),
            "cost_trend": trend,
            "most_expensive_run": most_expensive,
            "run_count": len(runs_with_cost),
        },
    )
