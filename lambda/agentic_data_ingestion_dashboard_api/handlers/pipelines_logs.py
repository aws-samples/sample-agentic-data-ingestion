#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CloudWatch step-log retrieval and in-memory cache for the dashboard API.

Extracted verbatim from ``handlers/pipelines.py`` (P21 Task 7.2 — module
decomposition). Behaviour is unchanged; ``handlers/pipelines.py`` re-exports
the public names for router/test compatibility.
"""

import time
from datetime import UTC, datetime, timedelta

from step_names import StepName, from_any

from .common import logger, logs_client

# Canonical step name → CloudWatch log group mapping. Lookups normalize the
# incoming step name via step_names.from_any (P28 R8.2, task 14.2), so state
# rows in ANY naming generation — canonical snake_case, legacy PascalCase
# ("CodeGeneration"), or phantom approval variants — resolve to the same
# log group. "JobCompletion" is not a pipeline step (no enum member); it
# keeps a literal alias entry.
LAMBDA_LOG_GROUPS: dict[str, str] = {
    StepName.FILE_VALIDATION: "/aws/lambda/agentic_data_ingestion_file_validation",
    StepName.SCHEMA_EXTRACT: "/aws/lambda/agentic_data_ingestion_schema_extract",
    StepName.SCHEMA_COMPARE: "/aws/lambda/agentic_data_ingestion_schema_compare",
    StepName.SCHEMA_MAPPING: "/aws/lambda/agentic_data_ingestion_schema_mapping",
    StepName.CREATE_CODE: "/aws/lambda/agentic_data_ingestion_create_code",
    StepName.TEST_CODE: "/aws/lambda/agentic_data_ingestion_test_code",
    StepName.CREATE_GLUE_PIPELINE: "/aws/lambda/agentic_data_ingestion_create_glue_pipeline",
    StepName.CREATE_INFRASTRUCTURE: "/aws/lambda/agentic_data_ingestion_create_infrastructure",
    StepName.CREATE_QUALITY_CHECKS: "/aws/lambda/agentic_data_ingestion_create_quality_checks",
    StepName.RUN_PIPELINE: "/aws/lambda/agentic_data_ingestion_run_pipeline",
    "JobCompletion": "/aws/lambda/agentic_data_ingestion_job_completion",
}


def _canonical_or_raw(step_name: str) -> str:
    """Canonical spelling for any known step-name variant; raw value otherwise."""
    try:
        return str(from_any(step_name))
    except ValueError:
        return step_name


def get_step_logs(step_name: str, run_id: str, limit: int = 30) -> list:
    """Fetch logs for a pipeline step from CloudWatch, filtered by run_id.

    For agent steps (create_code, test_code, create_glue_pipeline,
    create_infrastructure — any naming variant), also queries AgentCore
    Runtime container logs and merges them with Lambda proxy logs.
    """
    log_group = LAMBDA_LOG_GROUPS.get(_canonical_or_raw(step_name))
    if not log_group:
        return []

    logs = []

    # Fetch Lambda logs using CloudWatch Logs Insights (handles JSON-escaped run_ids correctly)
    try:
        import time as _time

        now = datetime.now(UTC)
        start_ts = int((now - timedelta(days=30)).timestamp())
        end_ts = int(now.timestamp())

        query = f'fields @message | filter @message like "{run_id}" | sort @timestamp asc | limit {limit * 2}'
        query_id = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_ts,
            endTime=end_ts,
            queryString=query,
        )["queryId"]

        # Poll for results (typically completes in 1-2 seconds)
        for _ in range(8):
            _time.sleep(0.5)
            result = logs_client.get_query_results(queryId=query_id)
            if result["status"] == "Complete":
                break

        for row in result.get("results", []):
            msg = ""
            for field in row:
                if field["field"] == "@message":
                    msg = field["value"].rstrip("\n")
                    break
            if msg:
                cleaned = _clean_log_message(msg)
                if cleaned:
                    logs.append(cleaned)
    except Exception as e:
        logger.warning(f"Failed to fetch Lambda logs for step {step_name}: {e}")

    # For agent steps, also fetch AgentCore container logs
    agentcore_logs = _get_agentcore_step_logs(step_name, run_id, limit)
    if agentcore_logs:
        logs.extend(agentcore_logs)

    return logs[:limit]


# AgentCore container log group prefixes for agent steps
_AGENTCORE_LOG_PREFIX = "/aws/bedrock-agentcore/runtimes/agentic_data_ingestion_agent_"

# OTEL/collector noise patterns to filter out of step logs
_LOG_NOISE_PATTERNS = (
    "service.go:",
    "telemetryAPI",
    "extensions/",
    "EXTENSION",
    "collector",
    "Starting shutdown",
    "Starting aws-otel",
    "Starting extensions",
    "Starting GRPC server",
    "Starting HTTP server",
    "Everything is ready. Begin running",
    "Received SHUTDOWN",
    "Stopping extensions",
    "INIT_START",
    "INIT_REPORT",
    "Configuration of configurator not loaded",
    "Attempting to instrument while already instrumented",
    "TracerProvider was missing",
    "MeterProvider was missing",
    "Subscription success",
    "Using default config URI",
    "service::telemetry::metrics",
    "Serving metrics",
    "Development component",
    "otel-instrument",
    "OTEL_PYTHON_CONFIGURATOR",
)


def _clean_log_message(msg: str) -> str | None:
    """Clean a Lambda log message: filter noise, extract useful content.

    Returns the cleaned message or None if it should be filtered out.
    """
    # Skip Lambda platform messages
    if msg.startswith(("START ", "END ", "REPORT ", "XRAY ", "INIT_START", "INIT_REPORT", "TELEMETRY", "EXTENSION")):
        return None

    # Try to parse as JSON
    if msg.startswith("{"):
        try:
            import json as _json

            parsed = _json.loads(msg)

            # Powertools format: has "message" key (application log)
            if "message" in parsed:
                level = parsed.get("level", "INFO").upper()
                message = parsed["message"]
                step = parsed.get("step_name", "")
                # Skip noisy Powertools messages
                if any(
                    skip in message
                    for skip in ("Attempting to instrument", "TracerProvider was missing", "MeterProvider was missing")
                ):
                    return None
                prefix = f"[{level}]" if level else ""
                step_prefix = f"[{step}]" if step else ""
                return f"{prefix} {step_prefix} {message}".strip()

            # OTEL application log format: has "body" key
            if "body" in parsed:
                body = parsed["body"]
                sev = parsed.get("severityText", "INFO")
                if body and isinstance(body, str):
                    return f"[{sev}] {body}"

            # OTEL collector noise: has "caller" key (service.go, extensions, etc.)
            if "caller" in parsed:
                return None

            # Other JSON with "msg" key (collector logs)
            if "msg" in parsed and "caller" not in parsed:
                msg_text = parsed["msg"]
                # Filter known collector messages
                if any(
                    skip in msg_text
                    for skip in (
                        "Everything is ready",
                        "Starting",
                        "Stopping",
                        "Subscription",
                        "Listening for",
                        "Using default config",
                        "Setting up own telemetry",
                        "Serving metrics",
                        "Development component",
                        "deprecated",
                        "Launching OpenTelemetry",
                        "Received SHUTDOWN",
                    )
                ):
                    return None
                return f"[INFO] {msg_text}"

            return None  # Unknown JSON format — skip
        except (ValueError, TypeError):
            pass

    # Tab-delimited Lambda format: [LEVEL]\tTIMESTAMP\tREQUEST_ID\tMESSAGE
    if msg.startswith(("[INFO]", "[WARNING]", "[ERROR]", "[DEBUG]")):
        parts = msg.split("\t", 3)
        if len(parts) >= 4:
            level = parts[0]
            message = parts[3].strip()
            return f"{level} {message}"
        return msg.strip()

    # Configuration/instrument warnings
    if "Configuration of configurator" in msg or "Attempting to instrument" in msg:
        return None

    # Plain text — return if meaningful
    stripped = msg.strip()
    if stripped and len(stripped) > 10:
        return stripped

    return None


# Canonical step name → AgentCore runtime agent name (log-group suffix).
# Lookups normalize via from_any, mirroring LAMBDA_LOG_GROUPS.
_STEP_TO_AGENT: dict[str, str] = {
    StepName.CREATE_CODE: "create_code",
    StepName.TEST_CODE: "test_code",
    StepName.CREATE_GLUE_PIPELINE: "create_pipeline",
    StepName.CREATE_INFRASTRUCTURE: "create_infra",
}


def _get_agentcore_step_logs(step_name: str, run_id: str, limit: int = 20) -> list:
    """Fetch logs from AgentCore Runtime container for an agent step.

    Uses Logs Insights to query OTEL JSON logs, extracts the 'body' field.
    """
    agent_name = _STEP_TO_AGENT.get(_canonical_or_raw(step_name))
    if not agent_name:
        return []

    try:
        # Find the log group (runtime ID suffix varies)
        response = logs_client.describe_log_groups(
            logGroupNamePrefix=f"{_AGENTCORE_LOG_PREFIX}{agent_name}",
            limit=1,
        )
        log_groups = response.get("logGroups", [])
        if not log_groups:
            return []

        log_group_name = log_groups[0]["logGroupName"]

        import time as _time

        now = datetime.now(UTC)
        start_ts = int((now - timedelta(days=30)).timestamp())
        end_ts = int(now.timestamp())

        query = f'fields @message | filter @message like "{run_id}" | sort @timestamp asc | limit {limit}'
        query_id = logs_client.start_query(
            logGroupName=log_group_name,
            startTime=start_ts,
            endTime=end_ts,
            queryString=query,
        )["queryId"]

        for _ in range(8):
            _time.sleep(0.5)
            result = logs_client.get_query_results(queryId=query_id)
            if result["status"] == "Complete":
                break

        logs = []
        for row in result.get("results", []):
            msg = ""
            for field in row:
                if field["field"] == "@message":
                    msg = field["value"]
                    break
            if not msg:
                continue
            # Parse OTEL JSON to extract the body (actual log message)
            try:
                import json as _json

                parsed = _json.loads(msg)
                body = parsed.get("body", "")
                severity = parsed.get("severityText", "INFO")
                if body and not body.startswith("{"):
                    # Plain text log message
                    logs.append(f"[{severity}] [AgentCore] {body}")
                elif body:
                    # Structured body — try to extract message
                    try:
                        body_obj = _json.loads(body)
                        logs.append(f"[{severity}] [AgentCore] {body_obj.get('message', body[:200])}")
                    except (ValueError, TypeError):
                        logs.append(f"[{severity}] [AgentCore] {body[:200]}")
            except (ValueError, TypeError):
                # Not JSON — use raw message, skip OTEL collector noise
                if "service.go" not in msg and "telemetryAPI" not in msg and "extensions" not in msg:
                    logs.append(f"[AgentCore] {msg.rstrip()[:200]}")

        return logs
    except Exception as e:
        logger.debug(f"Failed to fetch AgentCore logs for {step_name}: {e}")
        return []


# Simple in-memory cache with TTL (5 minutes)
_cache = {}

_cache_ttl = 300  # 5 minutes


def _get_cached(key: str):
    """Get cached value if not expired"""
    if key in _cache:
        value, timestamp = _cache[key]
        if time.time() - timestamp < _cache_ttl:
            return value
    return None


def _set_cache(key: str, value):
    """Set cached value with current timestamp"""
    _cache[key] = (value, time.time())
