# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Integration test for WebSocket real-time updates (end-to-end).

Requires a deployed environment with:
  - WS_API_ENDPOINT: WebSocket API Gateway URL (wss://...)
  - EXECUTIONS_TABLE: DynamoDB executions table name
  - AWS_REGION: AWS region (default: us-west-2)
  - AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true: Must be explicitly set to enable real AWS calls

Run with:
    AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true WS_API_ENDPOINT=wss://... pytest tests/test_ws_integration.py -m integration -v

Skipped in normal test runs (requires real deployed infrastructure).
"""

import json
import os
import time
import uuid

import boto3
import pytest

# Optional: websocket-client (pip install websocket-client)
try:
    import websocket
except ImportError:
    websocket = None

pytestmark = pytest.mark.integration

WS_API_ENDPOINT = os.environ.get("WS_API_ENDPOINT", "")
EXECUTIONS_TABLE = os.environ.get("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

PIPELINE_ID = f"integ-test-{uuid.uuid4().hex[:8]}"
RUN_ID = f"run-{uuid.uuid4().hex[:8]}"

# Guard: this entire module requires real AWS + a deployed WebSocket endpoint
_USE_REAL_AWS = os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "false").lower() == "true"


def _skip_if_no_endpoint():
    endpoint = os.environ.get("WS_API_ENDPOINT", "")
    # Only use endpoints that look like real deployed WebSocket APIs
    if not endpoint or "test" in endpoint.lower() or "test123" in endpoint.lower():
        pytest.skip("WS_API_ENDPOINT not set or is a test stub — skipping integration test")


def _skip_if_no_websocket_lib():
    if websocket is None:
        pytest.skip("websocket-client not installed — pip install websocket-client")


def _skip_if_not_real_aws():
    if not _USE_REAL_AWS:
        pytest.skip("AGENTIC_DATA_INGESTION_TEST_REAL_AWS not set — skipping real-AWS WebSocket test")


@pytest.fixture(scope="module")
def dynamodb_resource():
    """Shared DynamoDB resource for the test module.

    Only creates a real boto3 resource when AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true.
    Otherwise returns None (tests will be skipped before using it).
    """
    if not _USE_REAL_AWS:
        return None
    return boto3.resource("dynamodb", region_name=AWS_REGION)


@pytest.fixture(scope="module")
def executions_table(dynamodb_resource):
    """Reference to the executions DynamoDB table.

    Only returns a real table when AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true.
    """
    if dynamodb_resource is None:
        return None
    return dynamodb_resource.Table(EXECUTIONS_TABLE)


def _put_execution(executions_table, pipeline_id, run_id, status, step):
    """Insert or update an execution record to trigger a DynamoDB Stream event."""
    executions_table.put_item(
        Item={
            "pipeline_id": pipeline_id,
            "run_id": run_id,
            "status": status,
            "current_step": step,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )


def _cleanup_execution(executions_table, pipeline_id, run_id):
    """Remove the test execution record."""
    try:
        executions_table.delete_item(Key={"pipeline_id": pipeline_id, "run_id": run_id})
    except Exception:
        pass  # best-effort cleanup


class TestWebSocketIntegration:
    """End-to-end: connect → subscribe → trigger update → receive message."""

    def test_connect_subscribe_receive_update(self, executions_table):
        """
        1. Open a WebSocket connection to the deployed API.
        2. Send a subscribe message for a test pipeline.
        3. Write an execution record to DynamoDB (triggers Stream → broadcast).
        4. Verify that an execution_update message arrives within 5 seconds.
        """
        _skip_if_not_real_aws()
        _skip_if_no_endpoint()
        _skip_if_no_websocket_lib()

        ws = None
        try:
            # 1. Connect
            ws = websocket.create_connection(WS_API_ENDPOINT, timeout=10)
            assert ws.connected, "WebSocket failed to connect"

            # 2. Subscribe to the test pipeline
            subscribe_msg = json.dumps(
                {
                    "action": "subscribe",
                    "pipeline_ids": [PIPELINE_ID],
                }
            )
            ws.send(subscribe_msg)

            # Small delay to let the subscription propagate
            time.sleep(0.5)

            # 3. Trigger a DynamoDB update (this fires the Stream → broadcast Lambda)
            _put_execution(
                executions_table,
                pipeline_id=PIPELINE_ID,
                run_id=RUN_ID,
                status="running",
                step="schema_extract",
            )

            # 4. Wait for the execution_update message (up to 5s)
            ws.settimeout(5)
            received = None
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                    msg = json.loads(raw)
                    if msg.get("type") == "execution_update" and msg.get("pipeline_id") == PIPELINE_ID:
                        received = msg
                        break
                except websocket.WebSocketTimeoutException:
                    break

            assert received is not None, f"Did not receive execution_update for {PIPELINE_ID} within 5s"
            assert received["pipeline_id"] == PIPELINE_ID
            assert received["run_id"] == RUN_ID
            assert received["status"] == "running"
            assert received["current_step"] == "schema_extract"
            assert "updated_at" in received

        finally:
            # Cleanup
            if ws and ws.connected:
                ws.close()
            _cleanup_execution(executions_table, PIPELINE_ID, RUN_ID)

    def test_connect_and_disconnect(self):
        """Verify basic connect/disconnect lifecycle works without errors."""
        _skip_if_not_real_aws()
        _skip_if_no_endpoint()
        _skip_if_no_websocket_lib()

        ws = websocket.create_connection(WS_API_ENDPOINT, timeout=10)
        assert ws.connected
        ws.close()

    def test_subscribe_multiple_pipelines(self, executions_table):
        """Subscribe to multiple pipelines and verify updates for each."""
        _skip_if_not_real_aws()
        _skip_if_no_endpoint()
        _skip_if_no_websocket_lib()

        pid_a = f"integ-multi-a-{uuid.uuid4().hex[:6]}"
        pid_b = f"integ-multi-b-{uuid.uuid4().hex[:6]}"
        rid_a = f"run-a-{uuid.uuid4().hex[:6]}"
        rid_b = f"run-b-{uuid.uuid4().hex[:6]}"

        ws = None
        try:
            ws = websocket.create_connection(WS_API_ENDPOINT, timeout=10)
            ws.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "pipeline_ids": [pid_a, pid_b],
                    }
                )
            )
            time.sleep(0.5)

            # Trigger updates for both pipelines
            _put_execution(executions_table, pid_a, rid_a, "running", "file_validation")
            _put_execution(executions_table, pid_b, rid_b, "completed", "create_glue_pipeline")

            # Collect messages for up to 5s
            ws.settimeout(5)
            received_pids = set()
            deadline = time.time() + 5
            while time.time() < deadline and len(received_pids) < 2:
                try:
                    raw = ws.recv()
                    msg = json.loads(raw)
                    if msg.get("type") == "execution_update":
                        received_pids.add(msg["pipeline_id"])
                except websocket.WebSocketTimeoutException:
                    break

            assert pid_a in received_pids, f"Missing update for {pid_a}"
            assert pid_b in received_pids, f"Missing update for {pid_b}"

        finally:
            if ws and ws.connected:
                ws.close()
            _cleanup_execution(executions_table, pid_a, rid_a)
            _cleanup_execution(executions_table, pid_b, rid_b)
