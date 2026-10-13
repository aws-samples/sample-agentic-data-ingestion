# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test for WebSocket broadcast message format.

Validates that the broadcast message JSON matches the ExecutionUpdate
TypeScript interface used by the dashboard:

    interface ExecutionUpdate {
        type: 'execution_update';
        pipeline_id: string;
        run_id: string;
        status: string;
        current_step: string;
        updated_at: string;
    }
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_ws_broadcast")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")
os.environ.setdefault("WS_API_ENDPOINT", "wss://test123.execute-api.us-west-2.amazonaws.com/prod")

# ---------------------------------------------------------------------------
# Mock setup
# ---------------------------------------------------------------------------
mock_connections_table = MagicMock()
mock_apigw_client = MagicMock()

with patch("boto3.resource") as mock_resource:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_connections_table
    mock_resource.return_value = mock_dynamo
    if "lambda_function" in sys.modules:
        del sys.modules["lambda_function"]
    import lambda_function as ws_broadcast_contract


# The exact keys the ExecutionUpdate TypeScript interface expects
EXECUTION_UPDATE_KEYS = {"type", "pipeline_id", "run_id", "status", "current_step", "updated_at"}


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_connections_table.reset_mock()
    mock_apigw_client.reset_mock()
    yield


def _capture_broadcast_message(
    pipeline_id="pipe-contract",
    run_id="run-contract-001",
    status="running",
    current_step="schema_extract",
    updated_at="2025-01-15T12:00:00Z",
):
    """Run the broadcast Lambda and capture the message sent via post_to_connection."""
    mock_connections_table.query.return_value = {
        "Items": [{"connection_id": "conn-capture", "pipeline_id": pipeline_id}],
    }

    event = {
        "Records": [
            {
                "eventName": "MODIFY",
                "dynamodb": {
                    "NewImage": {
                        "pipeline_id": {"S": pipeline_id},
                        "run_id": {"S": run_id},
                        "status": {"S": status},
                        "current_step": {"S": current_step},
                        "updated_at": {"S": updated_at},
                    }
                },
            }
        ],
    }

    with patch("lambda_function.boto3.client") as mock_client_factory:
        mock_client_factory.return_value = mock_apigw_client
        ws_broadcast_contract.lambda_handler(event, None)

    data_bytes = mock_apigw_client.post_to_connection.call_args[1]["Data"]
    return json.loads(data_bytes.decode("utf-8"))


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


class TestExecutionUpdateContract:
    """Broadcast message matches the ExecutionUpdate TypeScript interface."""

    def test_message_has_exactly_expected_keys(self):
        message = _capture_broadcast_message()
        assert set(message.keys()) == EXECUTION_UPDATE_KEYS, (
            f"Expected keys {EXECUTION_UPDATE_KEYS}, got {set(message.keys())}"
        )

    def test_type_field_is_execution_update(self):
        message = _capture_broadcast_message()
        assert message["type"] == "execution_update"

    def test_all_values_are_strings(self):
        message = _capture_broadcast_message()
        for key, value in message.items():
            assert isinstance(value, str), f"Field '{key}' should be string, got {type(value).__name__}"

    def test_pipeline_id_matches_source(self):
        message = _capture_broadcast_message(pipeline_id="pipe-xyz-123")
        assert message["pipeline_id"] == "pipe-xyz-123"

    def test_run_id_matches_source(self):
        message = _capture_broadcast_message(run_id="run-2025-01-15-deadbeef")
        assert message["run_id"] == "run-2025-01-15-deadbeef"

    def test_status_matches_source(self):
        message = _capture_broadcast_message(status="completed")
        assert message["status"] == "completed"

    def test_current_step_matches_source(self):
        message = _capture_broadcast_message(current_step="create_glue_pipeline")
        assert message["current_step"] == "create_glue_pipeline"

    def test_updated_at_matches_source(self):
        message = _capture_broadcast_message(updated_at="2025-06-01T08:30:00Z")
        assert message["updated_at"] == "2025-06-01T08:30:00Z"

    def test_no_extra_keys_present(self):
        """Ensure no unexpected keys leak into the message."""
        message = _capture_broadcast_message()
        extra = set(message.keys()) - EXECUTION_UPDATE_KEYS
        assert not extra, f"Unexpected keys in message: {extra}"
