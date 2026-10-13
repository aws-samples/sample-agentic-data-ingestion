# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""EventBridge publisher import utility with explicit feature flag control."""

import logging
import os

logger = logging.getLogger(__name__)


def _noop(*args, **kwargs):
    logger.warning("EventBridge publisher not available, skipping event publication")
    return False


def get_eventbridge_publishers():
    """
    Returns (publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event).

    When EVENTBRIDGE_ENABLED=false, returns no-op functions.
    When EVENTBRIDGE_ENABLED=true (default), imports the real publishers —
    if the module is missing the ImportError propagates immediately (fail fast).
    """
    if os.environ.get("EVENTBRIDGE_ENABLED", "true").lower() != "true":
        logger.info("EventBridge publishing disabled via EVENTBRIDGE_ENABLED=false")
        return _noop, _noop, _noop

    from eventbridge_publisher import (
        publish_approval_status_change,
        publish_mcp_operation_event,
        publish_pipeline_state_change,
    )

    return publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event
