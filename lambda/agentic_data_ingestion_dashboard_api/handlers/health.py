#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Health check handlers for dashboard API"""

from typing import Any

from .common import create_response, handle_error, is_phantom_execution_row


def get_health() -> dict[str, Any]:
    """Health check endpoint"""
    return create_response(
        200, {"status": "healthy", "service": "agentic-data-ingestion-dashboard-api", "version": "1.0.0"}
    )


def get_notifications(query_params: dict[str, str]) -> dict[str, Any]:
    """Get dashboard notifications from DynamoDB."""
    try:
        from notification_service import DashboardChannel

        limit = min(int(query_params.get("limit", "50")), 200)
        unread_only = query_params.get("unread_only", "").lower() == "true"

        channel = DashboardChannel()
        notifications = channel.get_dashboard_notifications(limit=limit, unread_only=unread_only)

        return create_response(
            200,
            {
                "notifications": notifications,
                "count": len(notifications),
            },
        )
    except Exception as e:
        return handle_error(e, "Failed to fetch notifications")


def mark_notification_read(notification_id: str) -> dict[str, Any]:
    """Mark a notification as read."""
    try:
        from notification_service import DashboardChannel

        channel = DashboardChannel()
        success = channel.mark_notification_read(notification_id)

        if not success:
            return create_response(404, {"error": f"Notification not found: {notification_id}"})

        return create_response(200, {"success": True})
    except Exception as e:
        return handle_error(e, "Failed to mark notification as read")


def get_updates(query_params: dict[str, str]) -> dict[str, Any]:
    """Get recent pipeline execution updates."""
    try:
        from boto3.dynamodb.conditions import Key

        from .common import executions_table

        limit = min(int(query_params.get("limit", "20")), 100)

        # Query executions table StatusIndex for each active/recent status
        all_updates: list[dict[str, Any]] = []

        for status in ("RUNNING", "IN_PROGRESS", "WAITING_APPROVAL", "SUCCEEDED", "FAILED"):
            try:
                resp = executions_table.query(
                    IndexName="StatusIndex",
                    KeyConditionExpression=Key("status").eq(status),
                    ScanIndexForward=False,
                    Limit=limit,
                )
                all_updates.extend(resp.get("Items", []))
            except Exception:
                continue

        # p38 R3.3: drop Phantom_Execution_Rows — rows no state-recording writer
        # has ever touched — before the sort. **The filter is correct and stays;
        # what was wrong is the motivation originally written for it.** That
        # motivation said "the seven phantoms sit permanently in the ``RUNNING``
        # partition of ``StatusIndex``". Read-only live enumeration (task 10.1,
        # account ``777788889999`` / us-west-2) found **one** phantom for the rows
        # it reached, not seven, and that row reads ``CANCELLED`` — it carries a
        # ``completed_at`` and an ``updated_at`` a day after ``started_at``. The
        # loop above queries only ``RUNNING`` / ``IN_PROGRESS`` /
        # ``WAITING_APPROVAL`` / ``SUCCEEDED`` / ``FAILED``, so **this endpoint
        # never returned that row.** A rationale that was wrong about *where* the
        # rows are, not a filter that is wrong.
        #
        # So do not weaken this, do not make it conditional on anything, and do
        # not remove it: it is the right spelling for any future phantom that does
        # land in one of the five statuses queried above, and it costs nothing on
        # a partition that holds none. It is also the only placement that fixes
        # both answers at once — ``count`` is ``len(updates)`` and ``updates`` is a
        # slice of this list, so filtering one and not the other would move the
        # bug rather than fix it. ``LGR-21a1b5b-2`` (**template / ratified**) still
        # governs and is unaffected by the count: it names this table's
        # ``StatusIndex`` GSI as one of the three readers a non-run row reaches,
        # having rejected putting the file ledger on this table precisely because
        # "its StatusIndex GSI is partitioned on ``status``".
        #
        # What actually makes the enumerated row harmless is **task 6.1's** filter
        # in ``get_pipeline_executions`` (``pipelines_runs.py:286``), which queries
        # the base table on ``pipeline_id`` (``:228-232``) rather than by status and
        # therefore *does* return the row before dropping it.
        #
        # **Before the sort is safe HERE, and that differs from task 6.1's
        # placement deliberately.** There the same filter runs AFTER the QUEUED
        # split, as the defensive ordering: a filter applied after the split cannot
        # remove a row from ``queued_files``. There is no split here — the loop
        # above queries five statuses and ``QUEUED`` is not among them, so no
        # placeholder can reach ``all_updates`` at all. A reader comparing the two
        # filters should not have to derive that. (Nor could a placeholder match
        # this predicate anyway: its only writer is ``_track_queued_file``,
        # ``pipeline_trigger/lambda_function.py:676-694``, which sets
        # ``current_step="queued_trigger"`` at ``:689``. That closes design §5
        # delta D3, which the design text still lists as unresolved; neither
        # filter's placement ever depended on it.)
        #
        # Both Skipped_Run_Records survive on their own merits — each carries
        # ``current_step`` (``pipeline_trigger/lambda_function.py:243``, ``:499``)
        # — which ``LGR-21a1b5b-4`` (**template / ratified**) requires, and their
        # ``status="SKIPPED"`` is not one of the five queried here anyway.
        #
        # Deliberately NOT applied in ``handlers/metrics.py``: ``:112`` and
        # ``:724`` read this same GSI and p39-metrics-truth is rewriting those
        # computations, so a copy there would be a second, divergent spelling of
        # one predicate in a file another spec is changing.
        all_updates = [u for u in all_updates if not is_phantom_execution_row(u)]

        # Sort by started_at descending, take top N
        all_updates.sort(key=lambda x: x.get("started_at", ""), reverse=True)
        updates = all_updates[:limit]

        return create_response(
            200,
            {
                "updates": updates,
                "count": len(updates),
            },
        )
    except Exception as e:
        return handle_error(e, "Failed to fetch updates")
