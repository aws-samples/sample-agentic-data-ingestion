# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/eventbridge_publisher.py.

These tests exercise the REAL ``EventBridgePublisher`` class and its
module-level convenience functions (``publish_pipeline_state_change`` and
siblings) — not a monkeypatched fake. The boto3 EventBridge client is mocked
at the boundary (``eventbridge_publisher.eventbridge_client``) so we can assert
the real code builds correct ``put_events`` entries: Source, DetailType, the
JSON-encoded Detail shape, and EventBusName. Disabled / failure / exception
paths are covered too.

Mocking style mirrors the sibling ``tests/unit/shared`` suites: import the real
shared module and patch the boto3 boundary with ``unittest.mock``. No moto /
live AWS.
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest

# Make shared modules importable (matches test_notification_service.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-east-1")

import eventbridge_publisher
from eventbridge_publisher import EventBridgePublisher


def _ok_response():
    """A successful put_events response (no failed entries)."""
    return {"FailedEntryCount": 0, "Entries": [{"EventId": "evt-1"}]}


def _failed_response(message="boom"):
    """A put_events response indicating a failed entry."""
    return {"FailedEntryCount": 1, "Entries": [{"ErrorMessage": message}]}


@pytest.fixture
def mock_eb_client():
    """Patch the module-level EventBridge boto3 client at the boundary."""
    with patch.object(eventbridge_publisher, "eventbridge_client") as client:
        client.put_events.return_value = _ok_response()
        yield client


# ---------------------------------------------------------------------------
# publish_pipeline_state_change
# ---------------------------------------------------------------------------
class TestPublishPipelineStateChange:
    def test_builds_correct_event_entry(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="my-bus")
        ok = publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="agentic_data_ingestion_schema_extract",
            status="completed",
        )
        assert ok is True

        mock_eb_client.put_events.assert_called_once()
        entries = mock_eb_client.put_events.call_args.kwargs["Entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["Source"] == "agentic_data_ingestion.pipeline"
        assert entry["DetailType"] == "Pipeline State Change"
        assert entry["EventBusName"] == "my-bus"

        detail = json.loads(entry["Detail"])
        assert detail["pipeline_id"] == "pipe-1"
        assert detail["execution_id"] == "exec-1"
        assert detail["stage"] == "agentic_data_ingestion_schema_extract"
        assert detail["status"] == "completed"
        assert "timestamp" in detail

    def test_optional_fields_included_when_provided(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="stage-1",
            status="failed",
            run_id="run-9",
            metrics={"rows": 100},
            error_details="something broke",
            mcp_operations=[{"op": "x", "ms": 5}],
        )
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["run_id"] == "run-9"
        assert detail["metrics"] == {"rows": 100}
        assert detail["error_details"] == "something broke"
        assert detail["mcp_operations"] == [{"op": "x", "ms": 5}]

    def test_optional_fields_omitted_when_absent(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="stage-1",
            status="running",
        )
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        for key in ("run_id", "metrics", "error_details", "mcp_operations"):
            assert key not in detail

    def test_decimals_converted_to_float_in_detail(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="stage-1",
            status="completed",
            metrics={"cost": Decimal("12.50"), "nested": {"rows": Decimal("3")}},
        )
        assert ok is True
        # The Detail must be valid JSON (Decimals would break json.dumps if
        # not converted) and the values must be floats.
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["metrics"]["cost"] == 12.5
        assert detail["metrics"]["nested"]["rows"] == 3.0

    def test_returns_false_on_failed_entry(self, mock_eb_client):
        mock_eb_client.put_events.return_value = _failed_response("rejected")
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="stage-1",
            status="completed",
        )
        assert ok is False

    def test_returns_false_on_client_exception(self, mock_eb_client):
        mock_eb_client.put_events.side_effect = RuntimeError("network down")
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_pipeline_state_change(
            pipeline_id="pipe-1",
            execution_id="exec-1",
            stage="stage-1",
            status="completed",
        )
        assert ok is False


# ---------------------------------------------------------------------------
# publish_approval_status_change
# ---------------------------------------------------------------------------
class TestPublishApprovalStatusChange:
    def test_builds_correct_event_entry(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="approval-bus")
        ok = publisher.publish_approval_status_change(
            approval_id="apr-1",
            pipeline_id="pipe-1",
            approval_type="code_generation",
            status="approved",
        )
        assert ok is True
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["Source"] == "agentic_data_ingestion.pipeline"
        assert entry["DetailType"] == "Approval Status Change"
        assert entry["EventBusName"] == "approval-bus"

        detail = json.loads(entry["Detail"])
        assert detail["approval_id"] == "apr-1"
        assert detail["pipeline_id"] == "pipe-1"
        assert detail["approval_type"] == "code_generation"
        assert detail["status"] == "approved"
        assert "timestamp" in detail

    def test_optional_fields_included(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        publisher.publish_approval_status_change(
            approval_id="apr-1",
            pipeline_id="pipe-1",
            approval_type="schema_mapping",
            status="rejected",
            run_id="run-1",
            execution_id="exec-1",
            approver="alice",
            decision_details={"reason": "bad mapping"},
        )
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["run_id"] == "run-1"
        assert detail["execution_id"] == "exec-1"
        assert detail["approver"] == "alice"
        assert detail["decision_details"] == {"reason": "bad mapping"}

    def test_decimals_converted(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_approval_status_change(
            approval_id="apr-1",
            pipeline_id="pipe-1",
            approval_type="first_run",
            status="approved",
            decision_details={"score": Decimal("0.95")},
        )
        assert ok is True
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["decision_details"]["score"] == 0.95

    def test_returns_false_on_failed_entry(self, mock_eb_client):
        mock_eb_client.put_events.return_value = _failed_response()
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_approval_status_change(
            approval_id="apr-1",
            pipeline_id="pipe-1",
            approval_type="first_run",
            status="pending",
        )
        assert ok is False

    def test_returns_false_on_exception(self, mock_eb_client):
        mock_eb_client.put_events.side_effect = ValueError("kaboom")
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_approval_status_change(
            approval_id="apr-1",
            pipeline_id="pipe-1",
            approval_type="first_run",
            status="pending",
        )
        assert ok is False


# ---------------------------------------------------------------------------
# publish_mcp_operation_event
# ---------------------------------------------------------------------------
class TestPublishMcpOperationEvent:
    def test_builds_correct_event_entry(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="mcp-bus")
        ok = publisher.publish_mcp_operation_event(
            pipeline_id="pipe-1",
            operation_name="generate_pyspark_code",
            status="success",
            response_time_ms=123,
        )
        assert ok is True
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["Source"] == "agentic_data_ingestion.pipeline"
        assert entry["DetailType"] == "MCP Operation"
        assert entry["EventBusName"] == "mcp-bus"

        detail = json.loads(entry["Detail"])
        assert detail["pipeline_id"] == "pipe-1"
        assert detail["operation_name"] == "generate_pyspark_code"
        assert detail["status"] == "success"
        assert detail["response_time_ms"] == 123
        assert "timestamp" in detail

    def test_optional_fields_included(self, mock_eb_client):
        publisher = EventBridgePublisher(event_bus_name="bus")
        publisher.publish_mcp_operation_event(
            pipeline_id="pipe-1",
            operation_name="optimize_glue_job",
            status="fallback",
            response_time_ms=50,
            cost_cents=1.5,
            fallback_reason="circuit open",
            run_id="run-1",
            execution_id="exec-1",
        )
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["cost_cents"] == 1.5
        assert detail["fallback_reason"] == "circuit open"
        assert detail["run_id"] == "run-1"
        assert detail["execution_id"] == "exec-1"

    def test_cost_cents_zero_is_included(self, mock_eb_client):
        # cost_cents is gated on `is not None`, so 0.0 must still be included.
        publisher = EventBridgePublisher(event_bus_name="bus")
        publisher.publish_mcp_operation_event(
            pipeline_id="pipe-1",
            operation_name="op",
            status="success",
            response_time_ms=10,
            cost_cents=0.0,
        )
        detail = json.loads(mock_eb_client.put_events.call_args.kwargs["Entries"][0]["Detail"])
        assert detail["cost_cents"] == 0.0

    def test_returns_false_on_failed_entry(self, mock_eb_client):
        mock_eb_client.put_events.return_value = _failed_response()
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_mcp_operation_event(
            pipeline_id="pipe-1",
            operation_name="op",
            status="failed",
            response_time_ms=10,
        )
        assert ok is False

    def test_returns_false_on_exception(self, mock_eb_client):
        mock_eb_client.put_events.side_effect = RuntimeError("down")
        publisher = EventBridgePublisher(event_bus_name="bus")
        ok = publisher.publish_mcp_operation_event(
            pipeline_id="pipe-1",
            operation_name="op",
            status="failed",
            response_time_ms=10,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Default event bus name resolution
# ---------------------------------------------------------------------------
class TestEventBusNameDefault:
    def test_uses_module_default_when_not_provided(self, mock_eb_client):
        publisher = EventBridgePublisher()
        assert publisher.event_bus_name == eventbridge_publisher.EVENT_BUS_NAME
        publisher.publish_pipeline_state_change(
            pipeline_id="p",
            execution_id="e",
            stage="s",
            status="completed",
        )
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["EventBusName"] == eventbridge_publisher.EVENT_BUS_NAME

    def test_source_is_agentic_data_ingestion_pipeline(self):
        publisher = EventBridgePublisher()
        assert publisher.source == "agentic_data_ingestion.pipeline"


# ---------------------------------------------------------------------------
# Module-level convenience functions (the REAL ones)
# ---------------------------------------------------------------------------
class TestConvenienceFunctions:
    def test_publish_pipeline_state_change_delegates_to_real_publisher(self, mock_eb_client):
        ok = eventbridge_publisher.publish_pipeline_state_change(
            "pipe-1", "exec-1", "stage-1", "completed", run_id="run-1"
        )
        assert ok is True
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "Pipeline State Change"
        detail = json.loads(entry["Detail"])
        assert detail["pipeline_id"] == "pipe-1"
        assert detail["run_id"] == "run-1"

    def test_publish_approval_status_change_delegates(self, mock_eb_client):
        ok = eventbridge_publisher.publish_approval_status_change(
            "apr-1", "pipe-1", "code_generation", "approved", approver="bob"
        )
        assert ok is True
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "Approval Status Change"
        detail = json.loads(entry["Detail"])
        assert detail["approver"] == "bob"

    def test_publish_mcp_operation_event_delegates(self, mock_eb_client):
        ok = eventbridge_publisher.publish_mcp_operation_event("pipe-1", "op-x", "success", 77, cost_cents=2.0)
        assert ok is True
        entry = mock_eb_client.put_events.call_args.kwargs["Entries"][0]
        assert entry["DetailType"] == "MCP Operation"
        detail = json.loads(entry["Detail"])
        assert detail["response_time_ms"] == 77
        assert detail["cost_cents"] == 2.0


# ---------------------------------------------------------------------------
# "Disabled" behaviour
# ---------------------------------------------------------------------------
class TestDisabledPath:
    """The publisher module itself always attempts to publish; the
    EVENTBRIDGE_ENABLED=false gate lives in eventbridge_utils, which returns
    no-op functions instead of these real ones. Here we verify that when the
    underlying boto3 client raises (e.g. when EventBridge is not reachable in a
    disabled/local environment) the publisher degrades gracefully by returning
    False rather than propagating the error to callers."""

    def test_graceful_false_when_client_unavailable(self, mock_eb_client):
        mock_eb_client.put_events.side_effect = Exception("EventBridge disabled/unreachable")
        publisher = EventBridgePublisher(event_bus_name="bus")
        assert publisher.publish_pipeline_state_change("p", "e", "s", "completed") is False
        assert publisher.publish_approval_status_change("a", "p", "t", "pending") is False
        assert publisher.publish_mcp_operation_event("p", "op", "failed", 1) is False
