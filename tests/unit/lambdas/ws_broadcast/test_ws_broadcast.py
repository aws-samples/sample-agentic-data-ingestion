# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the WebSocket broadcast Lambda (AC-2.4, AC-2.5)."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_ws_broadcast"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")
os.environ.setdefault("WS_API_ENDPOINT", "wss://test.execute-api.us-west-2.amazonaws.com/prod")

mock_conn_tbl = MagicMock()
if "lambda_function" in sys.modules:
    del sys.modules["lambda_function"]
with patch("boto3.resource") as mr:
    md = MagicMock()
    md.Table.return_value = mock_conn_tbl
    mr.return_value = md
    import lambda_function as ws_broadcast


class _GoneException(Exception):
    pass


PID = "pipe-broadcast-001"
RID = "run-2025-01-15-abc"


def _make_apigw():
    m = MagicMock()
    m.exceptions.GoneException = _GoneException
    return m


def _rec(pid=PID, rid=RID, status="running", step="schema_extract", updated="2025-01-15T12:00:00Z", ename="MODIFY"):
    return {
        "eventName": ename,
        "dynamodb": {
            "NewImage": {
                "pipeline_id": {"S": pid},
                "run_id": {"S": rid},
                "status": {"S": status},
                "current_step": {"S": step},
                "updated_at": {"S": updated},
            }
        },
    }


def _evt(*recs):
    return {"Records": list(recs)}


def _subs(pipe_subs, all_subs=None):
    mock_conn_tbl.query.side_effect = [{"Items": pipe_subs}, {"Items": all_subs or []}]


@pytest.fixture(autouse=True)
def _reset():
    mock_conn_tbl.reset_mock()
    yield


class TestBroadcastToSubscribers:
    @patch("lambda_function.boto3.client")
    def test_posts_to_pipeline_subscribers(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs([{"connection_id": "c1", "pipeline_id": PID}, {"connection_id": "c2", "pipeline_id": PID}])
        ws_broadcast.lambda_handler(_evt(_rec()), None)
        assert apigw.post_to_connection.call_count == 2
        ids = [c[1]["ConnectionId"] for c in apigw.post_to_connection.call_args_list]
        assert "c1" in ids and "c2" in ids

    @patch("lambda_function.boto3.client")
    def test_posts_correct_message_payload(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs([{"connection_id": "c1", "pipeline_id": PID}])
        ws_broadcast.lambda_handler(
            _evt(_rec(status="completed", step="create_glue_pipeline", updated="2025-01-15T12:05:00Z")), None
        )
        data = apigw.post_to_connection.call_args[1]["Data"]
        msg = json.loads(data.decode("utf-8"))
        assert msg == {
            "type": "execution_update",
            "pipeline_id": PID,
            "run_id": RID,
            "status": "completed",
            "current_step": "create_glue_pipeline",
            "updated_at": "2025-01-15T12:05:00Z",
        }

    @patch("lambda_function.boto3.client")
    def test_includes_all_subscribers(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs(
            pipe_subs=[{"connection_id": "cs", "pipeline_id": PID}],
            all_subs=[{"connection_id": "ca", "pipeline_id": "__all__"}],
        )
        ws_broadcast.lambda_handler(_evt(_rec()), None)
        ids = [c[1]["ConnectionId"] for c in apigw.post_to_connection.call_args_list]
        assert "cs" in ids and "ca" in ids

    @patch("lambda_function.boto3.client")
    def test_deduplicates_connections(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs(
            pipe_subs=[{"connection_id": "cd", "pipeline_id": PID}],
            all_subs=[{"connection_id": "cd", "pipeline_id": "__all__"}],
        )
        ws_broadcast.lambda_handler(_evt(_rec()), None)
        assert apigw.post_to_connection.call_count == 1


class TestGoneExceptionCleanup:
    @patch("lambda_function.boto3.client")
    def test_deletes_stale_connection_on_gone(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs([{"connection_id": "cs", "pipeline_id": PID}])
        apigw.post_to_connection.side_effect = _GoneException("gone")
        result = ws_broadcast.lambda_handler(_evt(_rec()), None)
        assert result["statusCode"] == 200
        mock_conn_tbl.delete_item.assert_called_once_with(Key={"connection_id": "cs", "pipeline_id": PID})

    @patch("lambda_function.boto3.client")
    def test_continues_after_gone_exception(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        _subs([{"connection_id": "cs", "pipeline_id": PID}, {"connection_id": "cg", "pipeline_id": PID}])

        def fx(**kw):
            if kw["ConnectionId"] == "cs":
                raise _GoneException("gone")

        apigw.post_to_connection.side_effect = fx
        result = ws_broadcast.lambda_handler(_evt(_rec()), None)
        assert result["statusCode"] == 200
        assert apigw.post_to_connection.call_count == 2


class TestSkipsIrrelevantEvents:
    @patch("lambda_function.boto3.client")
    def test_skips_remove_events(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        ws_broadcast.lambda_handler(_evt(_rec(ename="REMOVE")), None)
        apigw.post_to_connection.assert_not_called()

    @patch("lambda_function.boto3.client")
    def test_skips_records_without_pipeline_id(self, mcf):
        apigw = _make_apigw()
        mcf.return_value = apigw
        rec = {"eventName": "MODIFY", "dynamodb": {"NewImage": {"run_id": {"S": "r1"}}}}
        ws_broadcast.lambda_handler(_evt(rec), None)
        apigw.post_to_connection.assert_not_called()
