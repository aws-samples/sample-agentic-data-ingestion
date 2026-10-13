# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit test for the WebSocket broadcast Lambda with no subscribers.

Validates that the broadcast Lambda handles an empty connections list
(no subscribers for a pipeline) without error.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_ws_broadcast"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
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
    # Re-import to get a fresh module reference with our mocks
    if "lambda_function" in sys.modules:
        del sys.modules["lambda_function"]
    import lambda_function as ws_broadcast_nosub


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_connections_table.reset_mock()
    mock_apigw_client.reset_mock()
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNoSubscribers:
    """Broadcast with zero subscribers completes without error."""

    @patch("lambda_function.boto3.client")
    def test_no_subscribers_returns_200(self, mock_client_factory):
        mock_client_factory.return_value = mock_apigw_client
        # Both pipeline-specific and __all__ queries return empty
        mock_connections_table.query.return_value = {"Items": []}

        event = {
            "Records": [
                {
                    "eventName": "MODIFY",
                    "dynamodb": {
                        "NewImage": {
                            "pipeline_id": {"S": "pipe-no-subs"},
                            "run_id": {"S": "run-001"},
                            "status": {"S": "running"},
                            "current_step": {"S": "file_validation"},
                            "updated_at": {"S": "2025-01-15T12:00:00Z"},
                        }
                    },
                }
            ],
        }

        result = ws_broadcast_nosub.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_apigw_client.post_to_connection.assert_not_called()
        mock_connections_table.delete_item.assert_not_called()

    @patch("lambda_function.boto3.client")
    def test_no_subscribers_processes_multiple_records(self, mock_client_factory):
        mock_client_factory.return_value = mock_apigw_client
        mock_connections_table.query.return_value = {"Items": []}

        event = {
            "Records": [
                {
                    "eventName": "MODIFY",
                    "dynamodb": {
                        "NewImage": {
                            "pipeline_id": {"S": "pipe-a"},
                            "run_id": {"S": "run-1"},
                            "status": {"S": "running"},
                            "current_step": {"S": "step-1"},
                            "updated_at": {"S": "2025-01-15T12:00:00Z"},
                        }
                    },
                },
                {
                    "eventName": "INSERT",
                    "dynamodb": {
                        "NewImage": {
                            "pipeline_id": {"S": "pipe-b"},
                            "run_id": {"S": "run-2"},
                            "status": {"S": "running"},
                            "current_step": {"S": "step-1"},
                            "updated_at": {"S": "2025-01-15T12:01:00Z"},
                        }
                    },
                },
            ],
        }

        result = ws_broadcast_nosub.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_apigw_client.post_to_connection.assert_not_called()
