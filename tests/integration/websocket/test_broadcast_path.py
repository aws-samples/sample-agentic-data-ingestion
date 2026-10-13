# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: DynamoDB Stream record -> ws_broadcast handler -> WebSocket message.

Validates: Requirements 22.1, 22.2, 22.3
"""

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_ws_broadcast"
_SHARED_DIR = _PROJECT_ROOT / "shared"


def _build_stream_record(pipeline_id, run_id, status, current_step, event_name="MODIFY"):
    return {
        "Records": [
            {
                "eventID": "test",
                "eventName": event_name,
                "eventVersion": "1.1",
                "eventSource": "aws:dynamodb",
                "awsRegion": "us-east-1",
                "dynamodb": {
                    "Keys": {"pipeline_id": {"S": pipeline_id}, "run_id": {"S": run_id}},
                    "NewImage": {
                        "pipeline_id": {"S": pipeline_id},
                        "run_id": {"S": run_id},
                        "status": {"S": status},
                        "current_step": {"S": current_step},
                        "updated_at": {"S": "2024-01-01T12:00:00Z"},
                    },
                    "StreamViewType": "NEW_IMAGE",
                },
            }
        ]
    }


def _create_ws_table(region):
    dynamodb = boto3.resource("dynamodb", region_name=region)
    dynamodb.create_table(
        TableName="agentic_data_ingestion_ws_connections",
        KeySchema=[
            {"AttributeName": "connection_id", "KeyType": "HASH"},
            {"AttributeName": "pipeline_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "connection_id", "AttributeType": "S"},
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "PipelineIndex",
                "KeySchema": [{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb.Table("agentic_data_ingestion_ws_connections")


def _load_ws_handler(env_vars):
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if "aws_lambda_powertools" not in sys.modules:
        m = MagicMock()
        m.Logger.return_value = MagicMock()
        sys.modules["aws_lambda_powertools"] = m
    eu = MagicMock()
    eu.require_env = lambda name, default=None: os.environ.get(name, default or name)
    sys.modules["env_utils"] = eu
    for k, v in env_vars.items():
        os.environ[k] = v
    sys.modules.pop("lambda_function", None)
    spec = importlib.util.spec_from_file_location("lambda_function", str(_LAMBDA_DIR / "lambda_function.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lambda_function"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestBroadcastPath:
    @mock_aws
    def test_broadcast_sends_correct_payload_fields(self, mock_lambda_env):
        """WebSocket message should contain pipeline_id, run_id, status, current_step."""
        region = "us-east-1"
        ws_table = _create_ws_table(region)
        ws_table.put_item(
            Item={
                "connection_id": "conn-abc123",
                "pipeline_id": "test-pipeline-1",
                "connected_at": "2024-01-01T00:00:00Z",
            }
        )
        env = {
            "WS_CONNECTIONS_TABLE": "agentic_data_ingestion_ws_connections",
            "WS_API_ENDPOINT": "wss://test123.execute-api.us-east-1.amazonaws.com/prod",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_ws_handler(env)
        stream_event = _build_stream_record("test-pipeline-1", "run-001", "running", "schema_extract")
        mock_apigw = MagicMock()
        mock_apigw.exceptions = MagicMock()
        mock_apigw.exceptions.GoneException = type("GoneException", (Exception,), {})
        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_apigw
            result = module.lambda_handler(stream_event, MagicMock())
        assert result["statusCode"] == 200
        assert mock_apigw.post_to_connection.called
        data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        assert msg["pipeline_id"] == "test-pipeline-1"
        assert msg["run_id"] == "run-001"
        assert msg["status"] == "running"
        assert msg["current_step"] == "schema_extract"
        assert msg["type"] == "execution_update"

    @mock_aws
    def test_broadcast_includes_updated_at(self, mock_lambda_env):
        """WebSocket message should include updated_at timestamp."""
        region = "us-east-1"
        ws_table = _create_ws_table(region)
        ws_table.put_item(
            Item={"connection_id": "conn-xyz", "pipeline_id": "p2", "connected_at": "2024-01-01T00:00:00Z"}
        )
        env = {
            "WS_CONNECTIONS_TABLE": "agentic_data_ingestion_ws_connections",
            "WS_API_ENDPOINT": "wss://test123.execute-api.us-east-1.amazonaws.com/prod",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_ws_handler(env)
        stream_event = _build_stream_record("p2", "run-002", "completed", "create_glue_pipeline")
        mock_apigw = MagicMock()
        mock_apigw.exceptions = MagicMock()
        mock_apigw.exceptions.GoneException = type("GoneException", (Exception,), {})
        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_apigw
            module.lambda_handler(stream_event, MagicMock())
        data = mock_apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        assert "updated_at" in msg
        assert msg["updated_at"] == "2024-01-01T12:00:00Z"

    @mock_aws
    def test_broadcast_skips_delete_events(self, mock_lambda_env):
        """DELETE events should be skipped."""
        region = "us-east-1"
        _create_ws_table(region)
        env = {
            "WS_CONNECTIONS_TABLE": "agentic_data_ingestion_ws_connections",
            "WS_API_ENDPOINT": "wss://test123.execute-api.us-east-1.amazonaws.com/prod",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_ws_handler(env)
        delete_event = {
            "Records": [
                {
                    "eventID": "del",
                    "eventName": "REMOVE",
                    "eventVersion": "1.1",
                    "eventSource": "aws:dynamodb",
                    "awsRegion": "us-east-1",
                    "dynamodb": {
                        "Keys": {"pipeline_id": {"S": "p"}, "run_id": {"S": "r"}},
                        "OldImage": {"pipeline_id": {"S": "p"}, "run_id": {"S": "r"}},
                        "StreamViewType": "NEW_AND_OLD_IMAGES",
                    },
                }
            ]
        }
        mock_apigw = MagicMock()
        mock_apigw.exceptions = MagicMock()
        mock_apigw.exceptions.GoneException = type("GoneException", (Exception,), {})
        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_apigw
            result = module.lambda_handler(delete_event, MagicMock())
        assert result["statusCode"] == 200
        mock_apigw.post_to_connection.assert_not_called()

    @mock_aws
    def test_broadcast_to_all_subscribers(self, mock_lambda_env):
        """Broadcast should reach both specific and __all__ subscribers."""
        region = "us-east-1"
        ws_table = _create_ws_table(region)
        ws_table.put_item(
            Item={"connection_id": "conn-specific", "pipeline_id": "pm", "connected_at": "2024-01-01T00:00:00Z"}
        )
        ws_table.put_item(
            Item={"connection_id": "conn-all", "pipeline_id": "__all__", "connected_at": "2024-01-01T00:00:00Z"}
        )
        env = {
            "WS_CONNECTIONS_TABLE": "agentic_data_ingestion_ws_connections",
            "WS_API_ENDPOINT": "wss://test123.execute-api.us-east-1.amazonaws.com/prod",
            "AWS_DEFAULT_REGION": region,
            "AWS_REGION": region,
        }
        module = _load_ws_handler(env)
        stream_event = _build_stream_record("pm", "run-m", "running", "file_validation")
        mock_apigw = MagicMock()
        mock_apigw.exceptions = MagicMock()
        mock_apigw.exceptions.GoneException = type("GoneException", (Exception,), {})
        with patch("boto3.client") as mock_client:
            mock_client.return_value = mock_apigw
            module.lambda_handler(stream_event, MagicMock())
        assert mock_apigw.post_to_connection.call_count == 2
        cids = [c[1]["ConnectionId"] for c in mock_apigw.post_to_connection.call_args_list]
        assert "conn-specific" in cids
        assert "conn-all" in cids
