#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgenticDataIngestion Dashboard API Lambda Function - Refactored

This is the main entry point for the AgenticDataIngestion Dashboard API Lambda function.
The function has been refactored from a monolithic 2482-line file into a modular
structure with separate handlers for each endpoint group.

Architecture:
    - lambda_function.py: Main entry point (this file)
    - router.py: Request routing logic
    - handlers/: Modular endpoint handlers organized by domain
        - common.py: Shared utilities and AWS clients
        - pipelines.py: Pipeline management endpoints
        - files.py: File management endpoints
        - stats.py: Statistics endpoints
        - approvals.py: Approval management endpoints
        - health.py: Health check endpoints

Environment Variables:
    AWS_REGION: AWS region for service clients (default: us-west-2)
    STATE_TABLE: State table name (default: agentic_data_ingestion_state)
    MAPPINGS_TABLE: Schema mappings table (default: agentic_data_ingestion_schema_mappings)
    PIPELINES_TABLE: Pipelines table (default: agentic_data_ingestion_pipelines)
    GENERATED_CODE_TABLE: Generated code table (default: agentic_data_ingestion_generated_code)
    TEST_RESULTS_TABLE: Test results table (default: agentic_data_ingestion_test_results)
    APPROVALS_TABLE: Approvals table (default: agentic_data_ingestion_approvals)

Example Event:
    {
        "httpMethod": "GET",
        "path": "/api/pipelines",
        "queryStringParameters": {"status": "active"},
        "headers": {...},
        "body": null
    }

Returns:
    API Gateway response with statusCode, headers, and JSON body
"""

from typing import Any

from router import route_request


def lambda_handler(event: dict[str, Any], context) -> dict[str, Any]:
    """Main Lambda handler for dashboard API endpoints.

    This handler dispatches based on the inbound event shape:

    1. **EventBridge scheduled events** (``event["source"] == "aws.events"``):
       The ``agentic-data-ingestion-discovery-cleanup-{stage}`` schedule rule (defined in
       the Orchestration stack, see Task 15.1) fires daily at 03:00 UTC and
       targets this Lambda. We dispatch directly to
       :func:`handlers.jdbc_resources.cleanup_discovery_resources` and return
       its summary dict — EventBridge does not require an HTTP envelope.

    2. **API Gateway proxy events** (everything else): routed through
       :func:`router.route_request`, which dispatches to domain-specific
       handlers under ``handlers/``.

    The EventBridge branch is checked first and is fully additive — the
    existing API Gateway dispatch path is unchanged.

    Args:
        event: Either an EventBridge scheduled event or an API Gateway proxy
            event containing HTTP method, path, headers, body, etc.
        context: Lambda context object (unused but required by Lambda)

    Returns:
        For EventBridge invocations: a summary dict with keys
        ``crawlers_deleted``, ``connections_deleted``, ``tables_deleted``,
        and ``errors``.
        For API Gateway invocations: a dict with ``statusCode``, ``headers``,
        and ``body`` (JSON-encoded).

    Example:
        >>> event = {"httpMethod": "GET", "path": "/api/health"}
        >>> response = lambda_handler(event, None)
        >>> response["statusCode"]
        200
    """
    # EventBridge schedule rule dispatch — must be checked before any
    # router/API-Gateway code path runs because EventBridge events have no
    # ``httpMethod`` or ``path`` and would otherwise fail the router's
    # proxy-event assumptions.
    if event.get("source") == "aws.events":
        # Lazy imports to keep cold-start light for the common API Gateway path.
        from handlers.common import logger
        from handlers.jdbc_resources import cleanup_discovery_resources

        logger.info(
            "Dispatching EventBridge scheduled event to discovery cleanup janitor",
            extra={
                "detail_type": event.get("detail-type"),
                "rule_arns": event.get("resources"),
            },
        )
        try:
            summary = cleanup_discovery_resources()
        except Exception:
            # The janitor catches per-resource errors internally and records
            # them in ``summary["errors"]``, so a top-level exception here
            # indicates an unrecoverable failure (e.g., the initial
            # ``glue:GetCrawlers`` paginator itself raised something
            # unexpected). Re-raise so EventBridge marks the invocation
            # failed and applies its retry policy.
            logger.exception("Discovery cleanup janitor raised an unexpected exception")
            raise

        logger.info("Discovery cleanup janitor finished", extra={"summary": summary})
        return summary

    return route_request(event)
