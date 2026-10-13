# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The global-concurrency rejection message reports the running counts.

p30 R10.4: "THE concurrency-rejection message surfaced to a manual caller SHALL
state the current and maximum running-execution counts."

Why the message text is worth asserting on. A trigger is rejected with 429 for
two unrelated reasons — the fleet is at MAX_CONCURRENT_EXECUTIONS, or this one
pipeline already holds its lock — and the remedies differ: the first clears as
the fleet drains, the second needs the pipeline's lock row gone (by the
lock-release Lambda, by pipeline delete, or by the 24 h TTL). An operator who
cannot tell them apart from the message looks in the wrong place, so these tests
pin both that the fleet counts appear on the concurrency path and that they do
NOT appear on the contention path.

The count is read from ``list_executions`` with ``maxResults`` capped at
MAX_CONCURRENT_EXECUTIONS + 1 and ``nextToken`` is not followed, so a saturated
fleet can hold far more executions than were counted. The message must not
present a truncated count as a total: under-reporting here errs in the
reassuring direction. ``TestTruncatedCountIsReportedAsAFloor`` covers that.

The lock-contention wording is deliberately left alone here — it is asserted on
by ``test_lock_release_on_start_failure.py`` (R10.3).

Validates: Requirements 10.4
"""

import importlib.util
import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_pipeline_trigger",
    )
)

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": "test-executions",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


@pytest.fixture(autouse=True)
def _env():
    """SOURCE_BUCKET is read at call time, not import time, on the S3 paths."""
    with patch.dict(os.environ, _ENV):
        yield


class _ExecutionAlreadyExists(Exception):
    """Stand-in for the botocore-generated SFN exception (see test_error_paths)."""


def _load_handler(mock_sfn, mock_s3=None, env_overrides=None):
    """Load the trigger Lambda with mocked SFN / DynamoDB / S3.

    ``env_overrides`` matters for the ceiling: MAX_CONCURRENT_EXECUTIONS is read
    into a module constant at import time, so a test that wants a different
    ceiling has to change it before the module body runs.
    """
    env = dict(_ENV, **(env_overrides or {}))
    mock_dynamodb = MagicMock()

    with (
        patch.dict(os.environ, env),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.client", return_value=mock_sfn),
        patch("boto3.resource", return_value=mock_dynamodb),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location(
            "lambda_function",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    # Clients are built at import time; rebind so the paths under test use these.
    mod.sfn_client = mock_sfn
    mod.locks_table = MagicMock()
    mod.executions_table = MagicMock()
    if mock_s3 is not None:
        mod.boto3 = MagicMock()
        mod.boto3.client.return_value = mock_s3
    return mod


def _sfn_mock(*, running, next_token=None):
    mock_sfn = MagicMock()
    resp = {"executions": [{}] * running}
    if next_token is not None:
        resp["nextToken"] = next_token
    mock_sfn.list_executions.return_value = resp
    mock_sfn.exceptions = MagicMock()
    mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
    mock_sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return mock_sfn


def _s3_mock(key="my-pipeline/file.csv"):
    mock_s3 = MagicMock()
    mock_s3.list_objects_v2.return_value = {"Contents": [{"Key": key, "LastModified": 1}]}
    return mock_s3


def _sqs_event(key="my-pipeline/file.csv", message_id="msg-1"):
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": "src-bucket"}, "object": {"key": key}},
    }
    # messageId as real SQS sends it: p30 R9.3 reports the rejected record
    # through batchItemFailures keyed on it instead of raising.
    return {"Records": [{"messageId": message_id, "body": json.dumps(body)}]}


def _assert_states_counts(msg, *, current, maximum):
    """Both numbers present, each attached to a word saying which one it is."""
    assert re.search(rf"\b{current}\b\s+running", msg), msg
    assert re.search(rf"maximum\s+{maximum}\b", msg), msg


# ---------------------------------------------------------------------------
# Every rejection path reports both numbers
# ---------------------------------------------------------------------------


class TestCountsAppearOnEveryPath:
    """R10.4 — the manual caller is the requirement; the rest share the builder."""

    def test_manual_s3_upload_429_states_current_and_maximum(self):
        """The Run Now endpoint forwards this body verbatim as `details`."""
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        _assert_states_counts(result["body"], current=10, maximum=10)

    def test_manual_jdbc_429_states_current_and_maximum(self):
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn)

        result = handler.lambda_handler(
            {"trigger": "manual", "pipeline_id": "jdbc-pipeline", "source_type": "JDBC"}, None
        )

        assert result["statusCode"] == 429
        _assert_states_counts(result["body"], current=10, maximum=10)

    def test_scheduled_429_states_current_and_maximum(self):
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        _assert_states_counts(result["body"], current=10, maximum=10)

    def test_sqs_path_logs_current_and_maximum(self):
        """The SQS path has no caller to answer, so the counts land in the log.

        R10.4 names the *manual* caller, and only that path returns the message
        in a response body. Since p30 R9.3 the SQS path neither returns nor
        raises the message — the record is reported through ``batchItemFailures``
        and the built message is what the gate logs, so that is where the counts
        have to be asserted. The builder itself is shared with the three
        returning paths above, which is why one assertion here is enough.
        """
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn)

        result = handler.lambda_handler(_sqs_event(), None)

        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
        logged = " ".join(str(c) for c in handler.logger.warning.call_args_list)
        _assert_states_counts(logged, current=10, maximum=10)


# ---------------------------------------------------------------------------
# The numbers are the real ones, not the ceiling twice
# ---------------------------------------------------------------------------


class TestNumbersAreObservedNotAssumed:
    def test_maximum_comes_from_the_configured_ceiling(self):
        """A different MAX_CONCURRENT_EXECUTIONS shows up in the message."""
        sfn = _sfn_mock(running=3)
        handler = _load_handler(sfn, mock_s3=_s3_mock(), env_overrides={"MAX_CONCURRENT_EXECUTIONS": "3"})

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        _assert_states_counts(result["body"], current=3, maximum=3)
        assert "10" not in result["body"]

    def test_current_can_exceed_the_maximum(self):
        """The gate runs before the lock, so the fleet can be over the ceiling.

        Reporting `max/max` whenever saturated would hide that, and "11 running
        of a maximum 10" is the operator's cue that something started outside
        this gate.
        """
        sfn = _sfn_mock(running=11)
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        _assert_states_counts(result["body"], current=11, maximum=10)

    def test_the_count_is_not_presented_as_a_guarantee(self):
        """It is read once, before the lock, and can be stale when read."""
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert "observed" in result["body"].lower()


# ---------------------------------------------------------------------------
# A truncated count must not read as a total
# ---------------------------------------------------------------------------


class TestTruncatedCountIsReportedAsAFloor:
    """maxResults caps the page and nextToken is not followed."""

    def test_next_token_makes_the_count_a_lower_bound(self):
        sfn = _sfn_mock(running=11, next_token="more-pages")
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        assert "at least 11 running" in result["body"], result["body"]
        _assert_states_counts(result["body"], current=11, maximum=10)

    def test_no_next_token_reports_the_count_plainly(self):
        """Without more pages the count is exact for that instant — no hedge."""
        sfn = _sfn_mock(running=10)
        handler = _load_handler(sfn, mock_s3=_s3_mock())

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert "at least" not in result["body"], result["body"]


# ---------------------------------------------------------------------------
# Lock contention is a different rejection and says so
# ---------------------------------------------------------------------------


class TestLockContentionIsDistinguishable:
    """Both reject with 429; only one of them is about the fleet."""

    def test_contention_message_carries_no_fleet_counts(self):
        """Fleet numbers on a lock rejection would send the operator to the wrong dial."""
        sfn = _sfn_mock(running=0)
        handler = _load_handler(sfn, mock_s3=_s3_mock())
        handler.locks_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "my-pipeline"}, None)

        assert result["statusCode"] == 429
        assert "already has a running execution" in result["body"]
        assert "concurrency limit" not in result["body"].lower()
        assert "maximum" not in result["body"].lower()
