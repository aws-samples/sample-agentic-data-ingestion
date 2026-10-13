# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the WebSocket disconnect Lambda.

Validates AC-2.3: ws-disconnect Lambda removes all subscription records
for the disconnected connection from the agentic_data_ingestion_ws_connections table.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_ws_disconnect"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")

# ---------------------------------------------------------------------------
# Mock setup — force fresh import of the disconnect Lambda module
# ---------------------------------------------------------------------------

mock_connections_table = MagicMock()

# Remove cached lambda_function to ensure we import the disconnect version
if "lambda_function" in sys.modules:
    del sys.modules["lambda_function"]

with patch("boto3.resource") as mock_resource:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_connections_table
    mock_resource.return_value = mock_dynamo
    import lambda_function as ws_disconnect


@pytest.fixture(autouse=True)
def _reset_mock():
    mock_connections_table.reset_mock()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
CONNECTION_ID = "test-conn-disconnect-xyz="


def _disconnect_event(connection_id=CONNECTION_ID):
    return {
        "requestContext": {
            "connectionId": connection_id,
            "routeKey": "$disconnect",
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDisconnect:
    """$disconnect removes all subscription records for the connection."""

    def test_disconnect_returns_200(self):
        mock_connections_table.query.return_value = {"Items": []}
        result = ws_disconnect.lambda_handler(_disconnect_event(), None)
        assert result["statusCode"] == 200

    def test_disconnect_queries_by_connection_id(self):
        mock_connections_table.query.return_value = {"Items": []}
        ws_disconnect.lambda_handler(_disconnect_event(), None)

        mock_connections_table.query.assert_called_once()
        kwargs = mock_connections_table.query.call_args[1]
        # Verify the key condition targets our connection_id
        assert "KeyConditionExpression" in kwargs

    def test_disconnect_deletes_all_subscriptions(self):
        mock_connections_table.query.return_value = {
            "Items": [
                {"connection_id": CONNECTION_ID, "pipeline_id": "pipe-1"},
                {"connection_id": CONNECTION_ID, "pipeline_id": "pipe-2"},
                {"connection_id": CONNECTION_ID, "pipeline_id": "__all__"},
            ]
        }

        ws_disconnect.lambda_handler(_disconnect_event(), None)

        assert mock_connections_table.delete_item.call_count == 3
        deleted_keys = [c[1]["Key"] for c in mock_connections_table.delete_item.call_args_list]
        assert {"connection_id": CONNECTION_ID, "pipeline_id": "pipe-1"} in deleted_keys
        assert {"connection_id": CONNECTION_ID, "pipeline_id": "pipe-2"} in deleted_keys
        assert {"connection_id": CONNECTION_ID, "pipeline_id": "__all__"} in deleted_keys

    def test_disconnect_no_subscriptions_no_deletes(self):
        mock_connections_table.query.return_value = {"Items": []}
        ws_disconnect.lambda_handler(_disconnect_event(), None)
        mock_connections_table.delete_item.assert_not_called()
