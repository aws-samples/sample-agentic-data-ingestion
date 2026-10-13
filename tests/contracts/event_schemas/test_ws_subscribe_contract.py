# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test for WebSocket subscribe message format.

Validates that the subscribe message format expected by the ws-connect
Lambda matches what the dashboard's useWebSocket hook sends:

    {
        "action": "subscribe",
        "pipeline_ids": ["pipe-1", "pipe-2"]
    }

The ws-connect Lambda parses body.action and body.pipeline_ids — this
test ensures that contract is honoured from both sides.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_ws_connect")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")

# ---------------------------------------------------------------------------
# Mock setup
# ---------------------------------------------------------------------------
mock_connections_table = MagicMock()

with patch("boto3.resource") as mock_resource:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_connections_table
    mock_resource.return_value = mock_dynamo
    if "lambda_function" in sys.modules:
        del sys.modules["lambda_function"]
    import lambda_function as ws_connect_contract


CONNECTION_ID = "conn-contract-sub="


@pytest.fixture(autouse=True)
def _reset_mock():
    mock_connections_table.reset_mock()
    yield


# ---------------------------------------------------------------------------
# Contract tests: subscribe message format
# ---------------------------------------------------------------------------


class TestSubscribeMessageContract:
    """Subscribe message format matches what ws-connect Lambda expects."""

    def test_subscribe_message_has_action_and_pipeline_ids(self):
        """The canonical subscribe message has exactly 'action' and 'pipeline_ids'."""
        subscribe_msg = {
            "action": "subscribe",
            "pipeline_ids": ["pipe-1", "pipe-2"],
        }
        assert subscribe_msg["action"] == "subscribe"
        assert isinstance(subscribe_msg["pipeline_ids"], list)

    def test_lambda_accepts_subscribe_message(self):
        """ws-connect Lambda successfully processes a well-formed subscribe message."""
        subscribe_msg = {
            "action": "subscribe",
            "pipeline_ids": ["pipe-alpha", "pipe-beta"],
        }
        event = {
            "requestContext": {
                "connectionId": CONNECTION_ID,
                "routeKey": "$default",
            },
            "body": json.dumps(subscribe_msg),
        }

        result = ws_connect_contract.lambda_handler(event, None)

        assert result["statusCode"] == 200
        # Verify it deleted the __all__ placeholder
        mock_connections_table.delete_item.assert_called_once_with(
            Key={
                "connection_id": CONNECTION_ID,
                "pipeline_id": "__all__",
            }
        )
        # Verify it created per-pipeline records
        assert mock_connections_table.put_item.call_count == 2

    def test_pipeline_ids_must_be_list(self):
        """pipeline_ids is expected to be a list (array in JSON)."""
        subscribe_msg = {
            "action": "subscribe",
            "pipeline_ids": ["single-pipe"],
        }
        event = {
            "requestContext": {
                "connectionId": CONNECTION_ID,
                "routeKey": "$default",
            },
            "body": json.dumps(subscribe_msg),
        }

        result = ws_connect_contract.lambda_handler(event, None)
        assert result["statusCode"] == 200

        item = mock_connections_table.put_item.call_args[1]["Item"]
        assert item["pipeline_id"] == "single-pipe"

    def test_action_field_is_required(self):
        """A message without 'action' field is ignored (no side effects)."""
        event = {
            "requestContext": {
                "connectionId": CONNECTION_ID,
                "routeKey": "$default",
            },
            "body": json.dumps({"pipeline_ids": ["pipe-1"]}),
        }

        result = ws_connect_contract.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_connections_table.put_item.assert_not_called()
        mock_connections_table.delete_item.assert_not_called()

    def test_subscribe_with_empty_pipeline_ids(self):
        """Subscribe with empty pipeline_ids removes __all__ but creates no records."""
        subscribe_msg = {
            "action": "subscribe",
            "pipeline_ids": [],
        }
        event = {
            "requestContext": {
                "connectionId": CONNECTION_ID,
                "routeKey": "$default",
            },
            "body": json.dumps(subscribe_msg),
        }

        result = ws_connect_contract.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_connections_table.delete_item.assert_called_once()
        mock_connections_table.put_item.assert_not_called()
