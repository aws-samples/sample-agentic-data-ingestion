#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Agent trace query handlers for dashboard API.

Provides endpoints for querying AI agent trace data from the
``agentic_data_ingestion_agent_traces`` DynamoDB table:

- ``GET /api/runs/{runId}/agent-trace`` — all step traces for a run
- ``GET /api/runs/{runId}/agent-trace/{stepName}`` — single step trace
- ``GET /api/metrics/token-usage?period=7d`` — aggregate token usage
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from env_utils import require_env

from .common import create_response, dynamodb, logger

# ---------------------------------------------------------------------------
# Table initialisation (lazy, matching the pattern in common.py)
# ---------------------------------------------------------------------------

AGENT_TRACES_TABLE_NAME = require_env("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
agent_traces_table = dynamodb.Table(AGENT_TRACES_TABLE_NAME)

# ---------------------------------------------------------------------------
# Period mapping for token-usage metrics
# ---------------------------------------------------------------------------

PERIOD_MAP: dict[str, timedelta] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}

# Bedrock cross-region inference-profile prefixes. The same model is recorded
# as both ``us.anthropic.claude-sonnet-4-6`` (Strands agents) and
# ``anthropic.claude-sonnet-4-6`` (schema_mapping), so the per-model comparison
# must collapse the region prefix or the same model shows as two rows.
_INFERENCE_PROFILE_PREFIXES = ("us.", "eu.", "apac.", "global.")


def _normalize_model_id(model_id: str) -> str:
    """Strip a leading Bedrock inference-profile region prefix so equivalent
    model ids aggregate into a single model-comparison row."""
    if not model_id:
        return ""
    for prefix in _INFERENCE_PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/runs/{runId}/agent-trace
# ═══════════════════════════════════════════════════════════════════════════


def get_run_traces(run_id: str) -> dict[str, Any]:
    """Return all step traces for a pipeline run.

    Queries the agent_traces table by ``run_id`` (partition key) and returns
    every step trace record.
    """
    if not run_id:
        return create_response(400, {"error": "run_id is required"})

    try:
        resp = agent_traces_table.query(
            KeyConditionExpression=Key("run_id").eq(run_id),
        )
        items = resp.get("Items", [])

        traces = [_format_trace_item(item) for item in items]

        return create_response(
            200,
            {
                "run_id": run_id,
                "traces": traces,
                "count": len(traces),
            },
        )
    except Exception as exc:
        logger.error("Failed to query agent traces for run %s: %s", run_id, exc)
        return create_response(500, {"error": "Internal server error"})


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/runs/{runId}/agent-trace/{stepName}
# ═══════════════════════════════════════════════════════════════════════════


def get_step_trace(run_id: str, step_name: str) -> dict[str, Any]:
    """Return a single step trace for a pipeline run.

    Uses DynamoDB ``GetItem`` with the composite key (run_id, step_name).
    """
    if not run_id or not step_name:
        return create_response(400, {"error": "run_id and step_name are required"})

    try:
        resp = agent_traces_table.get_item(
            Key={"run_id": run_id, "step_name": step_name},
        )
        item = resp.get("Item")

        if not item:
            return create_response(
                404,
                {
                    "error": "Trace not found",
                    "run_id": run_id,
                    "step_name": step_name,
                },
            )

        return create_response(200, _format_trace_item(item))
    except Exception as exc:
        logger.error(
            "Failed to get agent trace for run=%s step=%s: %s",
            run_id,
            step_name,
            exc,
        )
        return create_response(500, {"error": "Internal server error"})


# ═══════════════════════════════════════════════════════════════════════════
# GET /api/metrics/token-usage?period=7d
# ═══════════════════════════════════════════════════════════════════════════


def get_token_usage_metrics(query_params: dict[str, str]) -> dict[str, Any]:
    """Aggregate token usage from agent traces over a time period.

    Scans the agent_traces table, filters by timestamp within the requested
    period, and aggregates token counts per day.

    Supported ``period`` values: ``1d``, ``7d`` (default), ``30d``, ``90d``.
    """
    period = query_params.get("period", "7d")
    if period not in PERIOD_MAP:
        period = "7d"

    delta = PERIOD_MAP[period]
    cutoff = datetime.now(UTC) - delta
    cutoff_iso = cutoff.isoformat()

    try:
        # Scan with filter on timestamp
        items: list[dict[str, Any]] = []
        scan_kwargs: dict[str, Any] = {
            "FilterExpression": "attribute_exists(#ts) AND #ts >= :cutoff",
            "ExpressionAttributeNames": {"#ts": "timestamp"},
            "ExpressionAttributeValues": {":cutoff": cutoff_iso},
        }

        while True:
            resp = agent_traces_table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

        # Aggregate per day
        daily: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "estimated_cost_usd": Decimal("0"),
                "invocation_count": 0,
            }
        )

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cost = Decimal("0")

        # Per-model aggregation for model_comparison
        model_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total_input": 0,
                "total_output": 0,
                "total_cost": Decimal("0"),
                "total_latency_ms": 0.0,
                "count": 0,
            }
        )

        for item in items:
            trace_data = item.get("trace_data", {})
            ts = item.get("timestamp", "")
            day_key = ts[:10] if len(ts) >= 10 else "unknown"

            input_tokens = int(trace_data.get("total_input_tokens", 0))
            output_tokens = int(trace_data.get("total_output_tokens", 0))
            cache_read_tokens = int(trace_data.get("total_cache_read_tokens", 0))
            cost = Decimal(str(trace_data.get("estimated_cost_usd", 0)))

            daily[day_key]["input_tokens"] += input_tokens
            daily[day_key]["output_tokens"] += output_tokens
            daily[day_key]["cache_read_tokens"] += cache_read_tokens
            daily[day_key]["estimated_cost_usd"] += cost
            daily[day_key]["invocation_count"] += 1

            total_input += input_tokens
            total_output += output_tokens
            total_cache_read += cache_read_tokens
            total_cost += cost

            # Per-model stats — normalise the region prefix so the same model
            # recorded with/without a ``us.`` prefix aggregates into one row.
            model_id = _normalize_model_id(trace_data.get("model_id", "") or "")
            if model_id and model_id != "unknown":
                ms = model_stats[model_id]
                ms["total_input"] += input_tokens
                ms["total_output"] += output_tokens
                ms["total_cost"] += cost
                # Sum model call durations for latency
                for mc in trace_data.get("model_calls", []):
                    ms["total_latency_ms"] += float(mc.get("duration_ms", 0))
                ms["count"] += 1

        # Sort daily entries by date
        daily_sorted = [{"date": k, **v} for k, v in sorted(daily.items())]

        # Build model comparison
        model_comparison = []
        for mid, ms in sorted(model_stats.items(), key=lambda x: x[1]["count"], reverse=True):
            cnt = ms["count"]
            # Exclude models with no recorded tokens. These are either non-LLM
            # agent traces (orchestration, self-healing, etc. record no token
            # usage) or failed/empty invocations (e.g. a retired model id whose
            # Converse call recorded 0 tokens) — both would otherwise pollute
            # the comparison with meaningless zero-token rows.
            if cnt == 0 or (ms["total_input"] == 0 and ms["total_output"] == 0):
                continue
            model_comparison.append(
                {
                    "model_id": mid,
                    "avg_input_tokens": round(ms["total_input"] / cnt),
                    "avg_output_tokens": round(ms["total_output"] / cnt),
                    "avg_cost_usd": float(ms["total_cost"] / cnt),
                    "avg_latency_ms": round(ms["total_latency_ms"] / cnt),
                    "invocation_count": cnt,
                }
            )

        return create_response(
            200,
            {
                "period": period,
                "total_input_tokens": total_input,
                "total_output_tokens": total_output,
                "total_cache_read_tokens": total_cache_read,
                "total_estimated_cost_usd": total_cost,
                "total_traces": len(items),
                "daily": daily_sorted,
                "model_comparison": model_comparison,
            },
        )
    except Exception as exc:
        logger.error("Failed to aggregate token usage metrics: %s", exc)
        return create_response(500, {"error": "Internal server error"})


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _format_trace_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw DynamoDB item into the API response shape."""
    trace_data = item.get("trace_data", {})
    return {
        "run_id": item.get("run_id", ""),
        "step_name": item.get("step_name", ""),
        "pipeline_id": item.get("pipeline_id", ""),
        "timestamp": item.get("timestamp", ""),
        "trace": {
            "model_calls": trace_data.get("model_calls", []),
            "tool_calls": trace_data.get("tool_calls", []),
            "total_input_tokens": int(trace_data.get("total_input_tokens", 0)),
            "total_output_tokens": int(trace_data.get("total_output_tokens", 0)),
            "total_cache_read_tokens": int(trace_data.get("total_cache_read_tokens", 0)),
            "estimated_cost_usd": trace_data.get("estimated_cost_usd", 0),
            "start_time": trace_data.get("start_time"),
            "end_time": trace_data.get("end_time"),
            "model_id": trace_data.get("model_id", ""),
            "error": trace_data.get("error"),
        },
    }
