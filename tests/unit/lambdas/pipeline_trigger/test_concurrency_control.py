# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the pipeline trigger Lambda — concurrency control, SQS parsing."""

import json
import os
import sys
from unittest import mock
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

# Ensure the trigger Lambda is importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_pipeline_trigger",
    ),
)

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
}


def _sqs_event(body: str | dict, message_id: str = "msg-1") -> dict:
    """Build a minimal SQS event wrapping *body* (string or dict).

    ``messageId`` is present because real SQS always supplies one and p30 R9.3
    reports a failed record by it. Without it the handler cannot name the record
    in ``batchItemFailures`` and fails the whole batch instead — so an event
    built without it would exercise a path production never reaches. That
    fail-loud path has its own coverage in
    ``test_sqs_partial_batch_failure.py``.
    """
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"Records": [{"messageId": message_id, "body": body}]}


def _reported(result: dict) -> list[str]:
    """The messageIds the handler asked SQS to redeliver (p30 R9.3)."""
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


def _eventbridge_body(bucket: str = "src-bucket", key: str = "data/file.csv") -> dict:
    """Return a minimal EventBridge S3 Object Created payload."""
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": bucket},
            "object": {"key": key},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sfn_mock():
    """Yield a mock SFN client, locks table, and patch them into the Lambda module."""
    mock_sfn = MagicMock()
    mock_locks_table = MagicMock()
    mock_executions_table = MagicMock()

    # Default: locks_table.put_item succeeds (lock acquired)
    mock_locks_table.put_item.return_value = {}

    # Mock DynamoDB resource to return our mock tables
    mock_dynamodb = MagicMock()

    def _table_factory(name):
        if name == "agentic_data_ingestion_pipeline_locks":
            return mock_locks_table
        return mock_executions_table

    mock_dynamodb.Table.side_effect = _table_factory

    # Ensure our Lambda directory is at the front of sys.path so the correct
    # lambda_function is found even if other test files inserted their own paths.
    trigger_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_pipeline_trigger",
    )
    trigger_dir = os.path.abspath(trigger_dir)
    sys.path.insert(0, trigger_dir)
    with mock.patch.dict(os.environ, _ENV), mock.patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        # Remove cached module so it re-reads env vars
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]
        with (
            mock.patch("boto3.client", return_value=mock_sfn),
            mock.patch("boto3.resource", return_value=mock_dynamodb),
        ):
            import lambda_function

            lambda_function.sfn_client = mock_sfn
            lambda_function.locks_table = mock_locks_table
            lambda_function.executions_table = mock_executions_table
            yield lambda_function, mock_sfn, mock_locks_table
    # Clean up sys.path
    if trigger_dir in sys.path:
        sys.path.remove(trigger_dir)


# ---------------------------------------------------------------------------
# Tests — Concurrency gate
# ---------------------------------------------------------------------------


class TestConcurrencyGate:
    """AC-2: No more than MAX_CONCURRENT_EXECUTIONS run simultaneously."""

    def test_under_limit_starts_execution(self, sfn_mock):
        """When running count < limit, execution starts successfully."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": [{}] * 5}  # 5 < 10
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        event = _sqs_event(_eventbridge_body())
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        call_kwargs = sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_bucket"] == "src-bucket"
        assert sfn_input["source_key"] == "data/file.csv"
        assert sfn_input["trigger"] == "s3_upload"

    def test_sqs_input_carries_full_trigger_contract(self, sfn_mock):
        """The SQS path builds the SFN input via the shared trigger contract.

        Validates: Requirements 9.2 (P28) — the input carries the canonical
        triggered_by mirror plus source_bucket AND source_prefix (the prefix
        being the S3-key-derived pipeline prefix), so every producer emits
        the same shape.
        """
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        event = _sqs_event(_eventbridge_body(bucket="src-bucket", key="my-pipeline/data/file.csv"))
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn_input = json.loads(sfn.start_execution.call_args[1]["input"])
        assert sfn_input["pipeline_id"] == "my-pipeline/data"
        assert sfn_input["source_type"] == "S3_UPLOAD"
        assert sfn_input["triggered_by"] == "s3_upload"
        assert sfn_input["trigger"] == "s3_upload"
        assert sfn_input["source_bucket"] == "src-bucket"
        assert sfn_input["source_prefix"] == "my-pipeline/data"
        assert sfn_input["source_key"] == "my-pipeline/data/file.csv"
        # Single-file trigger: never routed to the prefix handler
        assert "trigger_mode" not in sfn_input

    def test_at_limit_returns_the_record_for_sqs_retry(self, sfn_mock):
        """When running count >= limit, the record is reported for redelivery.

        p30 R9.3 replaced the raise with a per-record ``batchItemFailures``
        entry: the message still returns to the queue, but siblings that already
        started an execution are no longer dragged back with it.
        """
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": [{}] * 10}  # 10 == limit

        event = _sqs_event(_eventbridge_body())
        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]
        sfn.start_execution.assert_not_called()

    def test_above_limit_returns_the_record_for_sqs_retry(self, sfn_mock):
        """When running count > limit, the record is also reported."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": [{}] * 11}

        event = _sqs_event(_eventbridge_body())
        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]


# ---------------------------------------------------------------------------
# Tests — SQS / EventBridge parsing
# ---------------------------------------------------------------------------


class TestSQSParsing:
    """Correctly extracts source_bucket and source_key from EventBridge JSON."""

    def test_extracts_bucket_and_key(self, sfn_mock):
        """Parses bucket name and object key from EventBridge detail."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        body = _eventbridge_body(bucket="my-bucket", key="uploads/report.csv")
        event = _sqs_event(body)
        lf.lambda_handler(event, None)

        call_kwargs = sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_bucket"] == "my-bucket"
        assert sfn_input["source_key"] == "uploads/report.csv"

    def test_special_characters_in_key(self, sfn_mock):
        """Keys with spaces and special chars are passed through."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        body = _eventbridge_body(key="path/to/my file (1).csv")
        event = _sqs_event(body)
        lf.lambda_handler(event, None)

        call_kwargs = sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_key"] == "path/to/my file (1).csv"


# ---------------------------------------------------------------------------
# Tests — Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Empty batch, malformed body, missing bucket/key.

    All three are per-record failures under p30 R9.3 rather than raises: they are
    deterministic, so the message needs to reach the DLQ under its own
    ``messageId`` instead of taking its siblings with it.
    """

    def test_empty_batch_returns_200(self, sfn_mock):
        """Returns 200 with 'No records' when Records list is empty."""
        lf, sfn, locks = sfn_mock
        result = lf.lambda_handler({"Records": []}, None)

        assert result["statusCode"] == 200
        assert "No records" in result["body"]
        sfn.list_executions.assert_not_called()

    def test_malformed_body_reports_the_record(self, sfn_mock):
        """A body that is not valid JSON returns that record to the queue."""
        lf, _, locks = sfn_mock
        event = _sqs_event("this is not json {{{")

        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]

    def test_missing_bucket_reports_the_record(self, sfn_mock):
        """EventBridge detail with no bucket returns that record to the queue."""
        lf, _, locks = sfn_mock
        body = {"detail": {"object": {"key": "file.csv"}}}
        event = _sqs_event(body)

        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]

    def test_missing_key_reports_the_record(self, sfn_mock):
        """EventBridge detail with no object key returns that record."""
        lf, _, locks = sfn_mock
        body = {"detail": {"bucket": {"name": "b"}}}
        event = _sqs_event(body)

        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]

    def test_empty_detail_reports_the_record(self, sfn_mock):
        """A completely empty detail returns that record."""
        lf, _, locks = sfn_mock
        body = {"detail": {}}
        event = _sqs_event(body)

        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]


# ---------------------------------------------------------------------------
# Tests — Per-pipeline deduplication gate
# ---------------------------------------------------------------------------


class TestPerPipelineDedup:
    """Files for the same pipeline are queued if a lock already exists."""

    def test_lock_exists_reports_the_record_for_retry(self, sfn_mock):
        """A held pipeline lock returns only that record to the queue.

        This is p30 R9.3's own example. Before it, the raise re-delivered the
        whole batch, so a conflict on one file re-processed every file that had
        already started.
        """
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": [{"executionArn": "arn:exec:1"}]}

        # Simulate lock already held — conditional put fails
        locks.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        event = _sqs_event(_eventbridge_body(key="my-pipeline/new_file.csv"))
        result = lf.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]
        sfn.start_execution.assert_not_called()

    def test_lock_acquired_starts_execution(self, sfn_mock):
        """When no lock exists, lock is acquired and execution starts."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": [{"executionArn": "arn:exec:1"}]}
        sfn.start_execution.return_value = {"executionArn": "arn:exec:2"}

        # Lock acquired successfully (default — put_item returns {})
        event = _sqs_event(_eventbridge_body(key="my-pipeline/file.csv"))
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        # Verify lock was written with correct pipeline_prefix
        locks.put_item.assert_called_once()
        put_kwargs = locks.put_item.call_args[1]
        assert put_kwargs["Item"]["pipeline_prefix"] == "my-pipeline"
        assert put_kwargs["ConditionExpression"] == "attribute_not_exists(pipeline_prefix)"

    def test_no_running_executions_acquires_lock_and_starts(self, sfn_mock):
        """When no executions are running, acquires lock and starts normally."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}

        event = _sqs_event(_eventbridge_body(key="my-pipeline/file.csv"))
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        locks.put_item.assert_called_once()

    def test_lock_item_has_ttl(self, sfn_mock):
        """Lock item includes a TTL field for safety-net expiration."""
        lf, sfn, locks = sfn_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}

        event = _sqs_event(_eventbridge_body(key="my-pipeline/file.csv"))
        lf.lambda_handler(event, None)

        put_kwargs = locks.put_item.call_args[1]
        item = put_kwargs["Item"]
        assert "ttl" in item
        assert isinstance(item["ttl"], int)
        assert "execution_name" in item
        assert "started_at" in item

    class TestQueuedRecordTTL:
        """AC-4: QUEUED records in executions table have TTL of 1 hour."""

        def test_queued_record_has_ttl_on_global_limit(self, sfn_mock):
            """When global concurrency blocks, the queued record includes a ttl field."""
            lf, sfn, locks = sfn_mock
            sfn.list_executions.return_value = {"executions": [{}] * 10}

            event = _sqs_event(_eventbridge_body(key="my-pipeline/file.csv"))
            result = lf.lambda_handler(event, None)
            assert _reported(result) == ["msg-1"]

            exec_table = lf.executions_table
            exec_table.put_item.assert_called_once()
            item = exec_table.put_item.call_args[1]["Item"]
            assert "ttl" in item
            assert isinstance(item["ttl"], int)
            # TTL should be ~1 hour (3600s) from now
            import time

            now = int(time.time())
            assert item["ttl"] > now
            assert item["ttl"] <= now + 3601

        def test_queued_record_has_ttl_on_pipeline_lock(self, sfn_mock):
            """When per-pipeline lock blocks, the queued record includes a ttl field."""
            lf, sfn, locks = sfn_mock
            sfn.list_executions.return_value = {"executions": [{"executionArn": "arn:exec:1"}]}
            locks.put_item.side_effect = ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )

            event = _sqs_event(_eventbridge_body(key="my-pipeline/file.csv"))
            result = lf.lambda_handler(event, None)
            assert _reported(result) == ["msg-1"]

            exec_table = lf.executions_table
            exec_table.put_item.assert_called_once()
            item = exec_table.put_item.call_args[1]["Item"]
            assert "ttl" in item
            assert isinstance(item["ttl"], int)
            import time

            now = int(time.time())
            assert item["ttl"] > now
            assert item["ttl"] <= now + 3601


class TestPipelinePrefix:
    """_pipeline_prefix extracts the correct prefix from S3 keys."""

    def test_standard_key(self, sfn_mock):
        lf, _, _ = sfn_mock
        assert lf._pipeline_prefix("my-pipeline-abc123/data.csv") == "my-pipeline-abc123"

    def test_nested_key(self, sfn_mock):
        lf, _, _ = sfn_mock
        assert lf._pipeline_prefix("prefix/sub/file.csv") == "prefix/sub"

    def test_no_slash(self, sfn_mock):
        lf, _, _ = sfn_mock
        assert lf._pipeline_prefix("file.csv") == "file.csv"
