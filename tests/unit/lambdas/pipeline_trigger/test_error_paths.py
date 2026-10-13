# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Error path unit tests for the pipeline_trigger Lambda.

Covers:
- Malformed SQS events (missing Records, missing body)
- SFN ExecutionAlreadyExists exception handling
- Concurrency gate exceeded (MAX_CONCURRENT_EXECUTIONS)

Validates: Requirements 17.1, 17.2, 17.3
"""

import importlib
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_handler(mock_sfn, mock_dynamo_resource=None):
    """Load the pipeline_trigger lambda_function using importlib to avoid module caching."""
    if mock_dynamo_resource is None:
        mock_dynamo_resource = MagicMock()

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.client", return_value=mock_sfn), patch("boto3.resource", return_value=mock_dynamo_resource):
            # Remove cached module
            sys.modules.pop("lambda_function", None)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.sfn_client = mock_sfn
            return mod


def _eventbridge_body(bucket="src-bucket", key="data/file.csv"):
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": {"key": key}},
    }


def _sqs_event(body, message_id="msg-1"):
    """Wrap *body* in an SQS record.

    ``messageId`` is always present, as it is on every real SQS record: under
    p30 R9.3 the handler reports a failed record by its messageId, and a record
    without one is unreportable and fails the whole batch instead — a path
    production never reaches, covered on its own in
    ``test_sqs_partial_batch_failure.py``.
    """
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"Records": [{"messageId": message_id, "body": body}]}


def _reported(result):
    """The messageIds the handler asked SQS to redeliver (p30 R9.3)."""
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


# ---------------------------------------------------------------------------
# Tests — Malformed SQS events
# ---------------------------------------------------------------------------


class TestMalformedSQSEvent:
    """Error paths for malformed or missing SQS event data.

    Since p30 R9.3 these do not raise: each is a deterministic, per-record
    failure reported through ``batchItemFailures``, so the message reaches the
    DLQ under its own id after ``maxReceiveCount`` without taking the rest of
    the batch with it.
    """

    def test_missing_records_returns_200(self):
        """Empty event with no Records key returns 200 with no-op message."""
        mock_sfn = MagicMock()
        handler = _load_handler(mock_sfn)
        result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 200
        mock_sfn.start_execution.assert_not_called()

    def test_empty_records_returns_200(self):
        """Event with empty Records list returns 200."""
        mock_sfn = MagicMock()
        handler = _load_handler(mock_sfn)
        result = handler.lambda_handler({"Records": []}, None)
        assert result["statusCode"] == 200

    def test_malformed_body_reports_the_record(self):
        """A non-JSON SQS body returns that record to the queue."""
        mock_sfn = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": []}
        handler = _load_handler(mock_sfn)

        result = handler.lambda_handler(_sqs_event("not-json!!!"), None)

        assert _reported(result) == ["msg-1"]

    def test_missing_bucket_reports_the_record(self):
        """EventBridge detail missing bucket returns that record to the queue."""
        mock_sfn = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": []}
        handler = _load_handler(mock_sfn)
        event = _sqs_event({"detail": {"object": {"key": "file.csv"}}})

        result = handler.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]

    def test_missing_key_reports_the_record(self):
        """EventBridge detail missing object key returns that record."""
        mock_sfn = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": []}
        handler = _load_handler(mock_sfn)
        event = _sqs_event({"detail": {"bucket": {"name": "b"}}})

        result = handler.lambda_handler(event, None)

        assert _reported(result) == ["msg-1"]


# ---------------------------------------------------------------------------
# Tests — ExecutionAlreadyExists
# ---------------------------------------------------------------------------


class TestExecutionAlreadyExists:
    """SFN ExecutionAlreadyExists returns the record for SQS retry.

    The redelivery is what the old raise existed to buy: the retry mints a fresh
    timestamped execution name. Under p30 R9.3 it is bought for this record
    alone.
    """

    def test_execution_already_exists_reports_the_record(self):
        mock_sfn = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": []}
        # Per-pipeline dedup: no running executions
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"executions": []}]
        mock_sfn.get_paginator.return_value = mock_paginator

        # Simulate ExecutionAlreadyExists
        exc_class = type("ExecutionAlreadyExists", (Exception,), {})
        mock_sfn.exceptions = MagicMock()
        mock_sfn.exceptions.ExecutionAlreadyExists = exc_class
        mock_sfn.start_execution.side_effect = exc_class("name collision")

        handler = _load_handler(mock_sfn)
        result = handler.lambda_handler(_sqs_event(_eventbridge_body()), None)

        assert _reported(result) == ["msg-1"]


# ---------------------------------------------------------------------------
# Tests — Concurrency gate exceeded
# ---------------------------------------------------------------------------


class TestConcurrencyGateExceeded:
    """Global concurrency gate blocks when MAX_CONCURRENT_EXECUTIONS reached.

    Reported per record rather than batch-wide: the snapshot is re-read for each
    record and is a lower bound at one instant, so a batch that is saturated
    early may genuinely have drained later (p30 R9.3).
    """

    def test_at_limit_reports_the_record_for_sqs_retry(self):
        mock_sfn = MagicMock()
        # Return exactly MAX_CONCURRENT_EXECUTIONS running executions
        mock_sfn.list_executions.return_value = {"executions": [{"executionArn": f"arn:exec:{i}"} for i in range(10)]}
        handler = _load_handler(mock_sfn)

        result = handler.lambda_handler(_sqs_event(_eventbridge_body()), None)

        assert _reported(result) == ["msg-1"]
        mock_sfn.start_execution.assert_not_called()

    def test_above_limit_reports_the_record_for_sqs_retry(self):
        mock_sfn = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": [{"executionArn": f"arn:exec:{i}"} for i in range(15)]}
        handler = _load_handler(mock_sfn)

        result = handler.lambda_handler(_sqs_event(_eventbridge_body()), None)

        assert _reported(result) == ["msg-1"]
        mock_sfn.start_execution.assert_not_called()
