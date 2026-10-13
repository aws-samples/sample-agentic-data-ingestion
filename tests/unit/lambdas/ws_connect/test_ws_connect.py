# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the WebSocket connect / default-route Lambda.

Validates AC-2.2: ws-connect Lambda stores connection ID and subscribed
pipeline IDs in the agentic_data_ingestion_ws_connections DynamoDB table on $connect,
and handles subscribe action on $default route.
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_ws_connect"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3.resource before importing the handler
# ---------------------------------------------------------------------------
mock_connections_table = MagicMock()

# Clear any cached lambda_function from other test files to avoid cross-contamination
sys.modules.pop("lambda_function", None)
# Re-insert our Lambda directory at position 0 to ensure correct module is found
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

with patch("boto3.resource") as mock_resource:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_connections_table
    mock_resource.return_value = mock_dynamo
    import lambda_function as ws_connect


@pytest.fixture(autouse=True)
def _reset_mock():
    mock_connections_table.reset_mock()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
CONNECTION_ID = "test-conn-abc123="


def _connect_event(connection_id=CONNECTION_ID):
    """Build a $connect WebSocket event."""
    return {
        "requestContext": {
            "connectionId": connection_id,
            "routeKey": "$connect",
        },
    }


def _subscribe_event(pipeline_ids, connection_id=CONNECTION_ID):
    """Build a $default route event with a subscribe action."""
    return {
        "requestContext": {
            "connectionId": connection_id,
            "routeKey": "$default",
        },
        "body": json.dumps(
            {
                "action": "subscribe",
                "pipeline_ids": pipeline_ids,
            }
        ),
    }


# ---------------------------------------------------------------------------
# Tests: $connect route
# ---------------------------------------------------------------------------


class TestConnect:
    """$connect route stores connection with __all__ subscription."""

    def test_connect_returns_200(self):
        result = ws_connect.lambda_handler(_connect_event(), None)
        assert result["statusCode"] == 200

    def test_connect_stores_all_subscription(self):
        ws_connect.lambda_handler(_connect_event(), None)

        mock_connections_table.put_item.assert_called_once()
        item = mock_connections_table.put_item.call_args[1]["Item"]
        assert item["connection_id"] == CONNECTION_ID
        assert item["pipeline_id"] == "__all__"
        assert "connected_at" in item
        assert "ttl" in item
        assert isinstance(item["ttl"], int)


# ---------------------------------------------------------------------------
# Tests: $default route — subscribe action
# ---------------------------------------------------------------------------


class TestSubscribe:
    """$default route with subscribe action creates per-pipeline records."""

    def test_subscribe_returns_200(self):
        result = ws_connect.lambda_handler(_subscribe_event(["pipe-1", "pipe-2"]), None)
        assert result["statusCode"] == 200

    def test_subscribe_deletes_all_placeholder(self):
        ws_connect.lambda_handler(_subscribe_event(["pipe-1"]), None)
        mock_connections_table.delete_item.assert_called_once_with(
            Key={
                "connection_id": CONNECTION_ID,
                "pipeline_id": "__all__",
            }
        )

    def test_subscribe_creates_per_pipeline_records(self):
        pipeline_ids = ["pipe-alpha", "pipe-beta", "pipe-gamma"]
        ws_connect.lambda_handler(_subscribe_event(pipeline_ids), None)

        # One put_item per pipeline
        assert mock_connections_table.put_item.call_count == len(pipeline_ids)
        stored_pids = [c[1]["Item"]["pipeline_id"] for c in mock_connections_table.put_item.call_args_list]
        assert sorted(stored_pids) == sorted(pipeline_ids)

    def test_subscribe_empty_list_only_deletes_placeholder(self):
        ws_connect.lambda_handler(_subscribe_event([]), None)
        mock_connections_table.delete_item.assert_called_once()
        mock_connections_table.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: $default route — subscribe limits (security finding S8)
# ---------------------------------------------------------------------------
# A subscribe used to write one row per entry of a caller-supplied list with no
# bound and no type check. Now a bad list is refused whole: 400, nothing
# deleted, nothing written. It is never truncated to fit.

# The Glue job for pipeline <id> is "agentic-data-ingestion-job-<id>" and Glue
# job names are at most 255 characters, so this is the longest id a working
# pipeline can have. Derived from that limit, not from the module's constant.
LONGEST_DEPLOYABLE_PIPELINE_ID = 255 - len("agentic-data-ingestion-job-")


def _distinct_ids(count):
    return [f"pipe-{i:04d}" for i in range(count)]


def _assert_refused(result):
    assert result == {"statusCode": 400}
    mock_connections_table.put_item.assert_not_called()
    mock_connections_table.delete_item.assert_not_called()


def _stored_pipeline_ids():
    return [c.kwargs["Item"]["pipeline_id"] for c in mock_connections_table.put_item.call_args_list]


class TestSubscribeLimits:
    """The list is bounded in count and per-entry shape, and deduplicated."""

    def test_the_cap_admits_what_the_dashboard_sends(self):
        """The dashboard sends one id per frame (all topics on one /pipelines/:id route)."""
        assert ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS >= 1
        assert ws_connect.MAX_PIPELINE_ID_LENGTH >= LONGEST_DEPLOYABLE_PIPELINE_ID

    def test_over_the_cap_is_refused_and_writes_nothing(self):
        ids = _distinct_ids(ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS + 1)
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(ids), None))

    def test_far_over_the_cap_is_refused_not_truncated(self):
        ids = _distinct_ids(10_000)
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(ids), None))

    def test_at_the_cap_is_accepted(self):
        ids = _distinct_ids(ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS)
        result = ws_connect.lambda_handler(_subscribe_event(ids), None)
        assert result == {"statusCode": 200}
        mock_connections_table.delete_item.assert_called_once()
        assert _stored_pipeline_ids() == ids

    def test_the_cap_counts_the_list_as_sent_before_deduplication(self):
        ids = ["pipe-1"] * (ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS + 1)
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(ids), None))

    @pytest.mark.parametrize(
        "pipeline_ids",
        ["pipe-1", {"pipe-1": True}, 42, None, True],
        ids=["string", "object", "number", "null", "bool"],
    )
    def test_non_list_is_refused(self, pipeline_ids):
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(pipeline_ids), None))

    @pytest.mark.parametrize(
        "bad_entry",
        [None, 7, 1.5, True, ["pipe-1"], {"id": "pipe-1"}],
        ids=["null", "int", "float", "bool", "list", "object"],
    )
    def test_non_string_entry_is_refused(self, bad_entry):
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(["pipe-ok", bad_entry]), None))

    def test_empty_string_entry_is_refused(self):
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(["pipe-ok", ""]), None))

    @pytest.mark.parametrize("bad_entry", ["a/b", "/", "pipe-1/", "/pipe-1", "../pipe-1"])
    def test_entry_with_a_slash_is_refused(self, bad_entry):
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(["pipe-ok", bad_entry]), None))

    def test_overlong_entry_is_refused(self):
        too_long = "p" * (ws_connect.MAX_PIPELINE_ID_LENGTH + 1)
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(["pipe-ok", too_long]), None))

    def test_longest_deployable_pipeline_id_is_accepted(self):
        pid = "a" * (LONGEST_DEPLOYABLE_PIPELINE_ID - 9) + "-0123abcd"
        assert len(pid) == LONGEST_DEPLOYABLE_PIPELINE_ID
        assert ws_connect.lambda_handler(_subscribe_event([pid]), None) == {"statusCode": 200}
        assert _stored_pipeline_ids() == [pid]

    def test_entry_at_the_length_limit_is_accepted(self):
        pid = "p" * ws_connect.MAX_PIPELINE_ID_LENGTH
        assert ws_connect.lambda_handler(_subscribe_event([pid]), None) == {"statusCode": 200}
        assert _stored_pipeline_ids() == [pid]

    def test_one_bad_entry_refuses_the_valid_ones_too(self):
        """Refused whole: the valid entries before the bad one are not written."""
        ids = ["pipe-1", "pipe-2", "pipe-3", 99]
        _assert_refused(ws_connect.lambda_handler(_subscribe_event(ids), None))

    def test_duplicates_are_written_once_in_first_seen_order(self):
        ids = ["pipe-b", "pipe-a", "pipe-b", "pipe-b", "pipe-a", "pipe-c"]
        result = ws_connect.lambda_handler(_subscribe_event(ids), None)
        assert result == {"statusCode": 200}
        assert _stored_pipeline_ids() == ["pipe-b", "pipe-a", "pipe-c"]

    def test_accepted_rows_keep_the_24_hour_ttl(self):
        before = int(time.time())
        ws_connect.lambda_handler(_subscribe_event(["pipe-1", "pipe-2"]), None)
        after = int(time.time())
        for c in mock_connections_table.put_item.call_args_list:
            item = c.kwargs["Item"]
            assert item["connection_id"] == CONNECTION_ID
            assert isinstance(item["ttl"], int)
            assert before + 86400 <= item["ttl"] <= after + 86400
            assert "connected_at" in item

    def test_refusal_does_not_echo_the_ids_into_the_log(self):
        ids = _distinct_ids(ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS + 1)
        with patch.object(ws_connect.logger, "warning") as log_warning:
            ws_connect.lambda_handler(_subscribe_event(ids), None)
        log_warning.assert_called_once()
        logged = " ".join(str(a) for a in log_warning.call_args.args)
        assert "pipe-0000" not in logged
        assert str(ws_connect.MAX_SUBSCRIBE_PIPELINE_IDS) in logged


class TestMalformedBody:
    """A $default body that is not a JSON object is refused without side effects."""

    def _event(self, body):
        return {
            "requestContext": {"connectionId": CONNECTION_ID, "routeKey": "$default"},
            "body": body,
        }

    @pytest.mark.parametrize(
        "body",
        ["{not json", "[1, 2]", '"subscribe"', "42", "null", "[" * 100_000],
        ids=["invalid-json", "array", "string", "number", "null", "deeply-nested"],
    )
    def test_non_object_body_is_refused(self, body):
        _assert_refused(ws_connect.lambda_handler(self._event(body), None))

    @pytest.mark.parametrize("body", [None, ""])
    def test_absent_body_is_a_no_op(self, body):
        result = ws_connect.lambda_handler(self._event(body), None)
        assert result == {"statusCode": 200}
        mock_connections_table.put_item.assert_not_called()
        mock_connections_table.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: $default route — unknown action
# ---------------------------------------------------------------------------


class TestUnknownAction:
    """$default route with unknown action returns 200 without side effects."""

    def test_unknown_action_returns_200(self):
        event = {
            "requestContext": {
                "connectionId": CONNECTION_ID,
                "routeKey": "$default",
            },
            "body": json.dumps({"action": "ping"}),
        }
        result = ws_connect.lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_connections_table.put_item.assert_not_called()
        mock_connections_table.delete_item.assert_not_called()
