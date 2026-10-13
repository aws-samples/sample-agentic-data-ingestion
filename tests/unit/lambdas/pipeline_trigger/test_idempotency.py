# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Idempotency unit tests for the pipeline_trigger Lambda.

Covers:
- Same SQS message processed twice → only one SFN execution started (dedup gate)

Validates: Requirements 18.1
"""

import importlib
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

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
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}


def _load_handler(mock_sfn, mock_locks_table=None, mock_executions_table=None):
    """Load pipeline_trigger via importlib with mocked DynamoDB tables."""
    if mock_locks_table is None:
        mock_locks_table = MagicMock()
        mock_locks_table.put_item.return_value = {}
    if mock_executions_table is None:
        mock_executions_table = MagicMock()

    mock_dynamo = MagicMock()

    def _table(name):
        if name == "agentic_data_ingestion_pipeline_locks":
            return mock_locks_table
        return mock_executions_table

    mock_dynamo.Table.side_effect = _table

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.client", return_value=mock_sfn), patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.sfn_client = mock_sfn
            mod.locks_table = mock_locks_table
            mod.executions_table = mock_executions_table
            return mod, mock_locks_table


def _eventbridge_body(bucket="src-bucket", key="data/file.csv"):
    return {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": {"key": key}},
    }


def _sqs_event(body, message_id="msg-1"):
    """Wrap *body* in an SQS record, with the ``messageId`` real SQS always sends.

    p30 R9.3 reports a blocked record through ``batchItemFailures`` keyed on that
    id, so an event without one exercises the unreportable fallback rather than
    the production path.
    """
    if isinstance(body, dict):
        body = json.dumps(body)
    return {"Records": [{"messageId": message_id, "body": body}]}


# ---------------------------------------------------------------------------
# Tests — Idempotent SFN execution start
# ---------------------------------------------------------------------------


class TestIdempotentTrigger:
    """Same SQS message processed twice → per-pipeline dedup gate blocks second."""

    def test_second_invocation_blocked_by_dedup_gate(self):
        """After first execution starts, second invocation for same pipeline is blocked by DynamoDB lock."""
        mock_sfn = MagicMock()
        mock_locks = MagicMock()

        # First invocation: no running executions, lock acquired
        mock_sfn.list_executions.return_value = {"executions": []}
        mock_sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
        mock_locks.put_item.return_value = {}  # Lock acquired successfully

        handler, _ = _load_handler(mock_sfn, mock_locks_table=mock_locks)
        event = _sqs_event(_eventbridge_body())

        # First call succeeds
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert mock_sfn.start_execution.call_count == 1

        # Second invocation: lock already held (ConditionalCheckFailedException)
        mock_sfn.reset_mock()
        mock_sfn.list_executions.return_value = {"executions": []}
        mock_locks.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        # Second call is blocked by the DynamoDB lock and reported for
        # redelivery rather than raised (p30 R9.3) — the dedup guarantee is
        # unchanged, only how the block is surfaced to SQS.
        result = handler.lambda_handler(event, None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
        mock_sfn.start_execution.assert_not_called()

    def test_same_message_different_pipeline_both_start(self):
        """Messages for different pipelines both start executions (separate locks)."""
        mock_sfn = MagicMock()
        mock_locks = MagicMock()
        mock_sfn.list_executions.return_value = {"executions": []}
        mock_sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
        mock_locks.put_item.return_value = {}  # Lock acquired

        handler, _ = _load_handler(mock_sfn, mock_locks_table=mock_locks)

        # First pipeline
        event1 = _sqs_event(_eventbridge_body(key="pipeline-a/file.csv"))
        handler.lambda_handler(event1, None)
        assert mock_sfn.start_execution.call_count == 1

        # Second pipeline (different prefix) — lock also acquired
        mock_sfn.reset_mock()
        mock_sfn.list_executions.return_value = {"executions": []}

        event2 = _sqs_event(_eventbridge_body(key="pipeline-b/file.csv"))
        handler.lambda_handler(event2, None)
        assert mock_sfn.start_execution.call_count == 1
