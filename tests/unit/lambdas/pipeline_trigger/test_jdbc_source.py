# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the JDBC source-type branches in the pipeline trigger Lambda.

Covers Tasks 8.1 – 8.5 of the p17-jdbc-data-source spec:
  * Task 8.1: ``trigger == "manual"`` top-level branch dispatches to
    ``_handle_manual_trigger``.
  * Task 8.2: ``_handle_manual_trigger`` validates ``pipeline_id`` and
    branches on ``source_type``.
  * Task 8.3: ``_start_jdbc_execution`` enforces the global concurrency gate,
    acquires a per-pipeline lock keyed on ``pipeline_id``, builds a JDBC
    execution name, and starts the State Machine with
    ``{pipeline_id, source_type, trigger}``.
  * Task 8.4: ``_handle_scheduled_trigger`` routes ``source_type == "JDBC"``
    through ``_start_jdbc_execution`` (carrying ``trigger="scheduled"``)
    instead of listing S3.
  * Task 8.5: every S3_UPLOAD path (SQS records loop, scheduled S3 branch,
    manual S3 helper) embeds both ``pipeline_id`` and
    ``source_type: "S3_UPLOAD"`` in the SFN input.

Validates: Requirements 22.1
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
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

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")


# ---------------------------------------------------------------------------
# Fixture & helpers
# ---------------------------------------------------------------------------


_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-west-2:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "SOURCE_BUCKET": "agentic-data-ingestion-source",
}

PIPELINE_ID = "pid-abc"


@pytest.fixture()
def trigger_mock():
    """Yield the lambda module wired to mocked SFN, S3, locks, and executions tables.

    Mirrors the pattern used by ``test_scheduled_trigger.py``:
      * Module-level ``sfn_client`` and DynamoDB tables are replaced.
      * Lazy ``boto3.client("s3")`` calls inside the Lambda are routed to a
        shared S3 mock via ``boto3.client`` patching.
      * ``aws_lambda_powertools`` is stubbed so the test environment doesn't
        need the real dependency.

    Defaults: lock acquisition succeeds, SFN returns no running executions,
    ``start_execution`` returns a stub ARN — happy-path-friendly out of the
    box; individual tests override as needed.
    """
    mock_sfn = MagicMock()
    mock_sfn.list_executions.return_value = {"executions": []}
    mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:exec-1"}

    # The production code uses ``sfn_client.exceptions.ExecutionAlreadyExists``
    # as the rendezvous for collisions. Provide a real exception class so
    # ``except`` matches and ``raise`` works without surprises.
    class _ExecutionAlreadyExists(Exception):
        pass

    mock_sfn.exceptions = MagicMock()
    mock_sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists

    mock_s3 = MagicMock()
    mock_locks_table = MagicMock()
    mock_locks_table.put_item.return_value = {}  # default: lock acquired
    mock_executions_table = MagicMock()

    mock_dynamodb = MagicMock()

    def _table_factory(name):
        if name == "agentic_data_ingestion_pipeline_locks":
            return mock_locks_table
        return mock_executions_table

    mock_dynamodb.Table.side_effect = _table_factory

    def _client_factory(service, **kwargs):
        if service == "stepfunctions":
            return mock_sfn
        if service == "s3":
            return mock_s3
        return MagicMock()

    trigger_dir = os.path.abspath(
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
    sys.path.insert(0, trigger_dir)
    with mock.patch.dict(os.environ, _ENV), mock.patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]
        with (
            mock.patch("boto3.client", side_effect=_client_factory),
            mock.patch("boto3.resource", return_value=mock_dynamodb),
        ):
            import lambda_function

            # Defensive: ensure module-level handles point at our mocks
            # even if a future refactor reassigns them during import.
            lambda_function.sfn_client = mock_sfn
            lambda_function.locks_table = mock_locks_table
            lambda_function.executions_table = mock_executions_table

            yield lambda_function, mock_sfn, mock_s3, mock_locks_table, mock_executions_table

    if trigger_dir in sys.path:
        sys.path.remove(trigger_dir)


def _conditional_check_failed_error() -> ClientError:
    """A ClientError shaped like a DynamoDB conditional-check failure."""
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


def _sqs_event(bucket: str, key: str) -> dict:
    """Build a minimal SQS event wrapping an EventBridge S3 Object Created body."""
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": bucket},
            "object": {"key": key},
        },
    }
    return {"Records": [{"body": json.dumps(body)}]}


# ---------------------------------------------------------------------------
# Tests — Manual JDBC trigger path (Tasks 8.1, 8.2, 8.3)
# ---------------------------------------------------------------------------


class TestManualJdbcTrigger:
    """``trigger == "manual"`` + ``source_type == "JDBC"`` end-to-end.

    Validates: Requirement 5.9, Requirement 5.10.
    """

    JDBC_NAME_RE = re.compile(r"^jdbc-pid-abc-\d{8}T\d{6}-[a-f0-9]{12}$")

    def test_happy_path_starts_jdbc_execution(self, trigger_mock):
        """Manual JDBC trigger → JDBC-shaped SFN execution + 200 response."""
        lf, sfn, s3, locks, _execs = trigger_mock

        event = {
            "trigger": "manual",
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
        }
        result = lf.lambda_handler(event, None)

        # Response shape per Requirement 5.9 / Task 8.3
        assert result["statusCode"] == 200
        assert result["pipeline_id"] == PIPELINE_ID
        assert result["execution_name"].startswith("jdbc-pid-abc-")
        assert result["execution_arn"].startswith("arn:aws:states:")

        # SFN was called with the JDBC-flavoured execution name and input
        sfn.start_execution.assert_called_once()
        call_kwargs = sfn.start_execution.call_args.kwargs
        assert call_kwargs["stateMachineArn"] == _ENV["STATE_MACHINE_ARN"]
        assert self.JDBC_NAME_RE.match(call_kwargs["name"]), (
            f"execution name {call_kwargs['name']!r} doesn't match jdbc-{{pipeline}}-{{ts}}-{{hash}}"
        )

        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input == {
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
            "trigger": "manual",
            # P28 R9.2: the shared trigger contract mirrors the trigger as the
            # canonical triggered_by key; JDBC inputs carry no S3 source keys.
            "triggered_by": "manual",
        }
        # JDBC bypasses S3 listing entirely
        s3.list_objects_v2.assert_not_called()

        # Lock keyed on pipeline_id (no S3-key-derived prefix for JDBC)
        locks.put_item.assert_called_once()
        lock_item = locks.put_item.call_args.kwargs["Item"]
        assert lock_item["pipeline_prefix"] == PIPELINE_ID

    def test_missing_pipeline_id_returns_400(self, trigger_mock):
        """``_handle_manual_trigger`` rejects events without a pipeline_id."""
        lf, sfn, _s3, locks, _execs = trigger_mock

        event = {"trigger": "manual", "source_type": "JDBC"}
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 400
        assert result["body"] == "Missing pipeline_id"
        sfn.start_execution.assert_not_called()
        sfn.list_executions.assert_not_called()
        locks.put_item.assert_not_called()

    def test_concurrency_saturation_returns_429(self, trigger_mock):
        """Global concurrency gate blocks manual JDBC triggers."""
        lf, sfn, _s3, locks, _execs = trigger_mock
        sfn.list_executions.return_value = {"executions": [{}] * int(_ENV["MAX_CONCURRENT_EXECUTIONS"])}

        event = {
            "trigger": "manual",
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
        }
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 429
        assert "concurrency" in result["body"].lower()
        sfn.start_execution.assert_not_called()
        locks.put_item.assert_not_called()

    def test_pipeline_lock_contention_returns_429(self, trigger_mock):
        """A pre-existing per-pipeline lock blocks the manual JDBC trigger."""
        lf, sfn, _s3, locks, _execs = trigger_mock
        locks.put_item.side_effect = _conditional_check_failed_error()

        event = {
            "trigger": "manual",
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
        }
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 429
        assert "already has a running execution" in result["body"].lower()
        sfn.start_execution.assert_not_called()

    def test_unknown_source_type_returns_400(self, trigger_mock):
        """``_handle_manual_trigger`` rejects unrecognised source_type values."""
        lf, sfn, _s3, locks, _execs = trigger_mock

        event = {
            "trigger": "manual",
            "pipeline_id": PIPELINE_ID,
            "source_type": "WEIRD",
        }
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 400
        assert "unknown source_type" in result["body"].lower()
        sfn.start_execution.assert_not_called()
        sfn.list_executions.assert_not_called()
        locks.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Scheduled JDBC path (Task 8.4)
# ---------------------------------------------------------------------------


class TestScheduledJdbcTrigger:
    """``_handle_scheduled_trigger`` branches on ``source_type``.

    Validates: Requirement 5.10, Requirement 5.11.
    """

    def test_scheduled_jdbc_routes_through_jdbc_helper(self, trigger_mock):
        """Scheduled JDBC events bypass S3 listing and start a JDBC execution."""
        lf, sfn, s3, locks, _execs = trigger_mock

        event = {
            "trigger": "scheduled",
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
        }
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        # JDBC bypasses S3 entirely — no listing should occur
        s3.list_objects_v2.assert_not_called()

        sfn.start_execution.assert_called_once()
        call_kwargs = sfn.start_execution.call_args.kwargs
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input == {
            "pipeline_id": PIPELINE_ID,
            "source_type": "JDBC",
            "trigger": "scheduled",  # carried through from the event
            "triggered_by": "scheduled",  # contract mirror key (P28 R9.2)
        }
        # Execution name uses the JDBC prefix
        assert call_kwargs["name"].startswith("jdbc-pid-abc-")

    def test_scheduled_s3_upload_default_includes_pipeline_id_and_source_type(self, trigger_mock):
        """Scheduled S3_UPLOAD (no source_type set, falls back to default) still
        embeds ``pipeline_id`` AND ``source_type: "S3_UPLOAD"`` in the SFN input
        per Task 8.5."""
        lf, sfn, s3, _locks, _execs = trigger_mock
        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/file.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        event = {"trigger": "scheduled", "pipeline_id": PIPELINE_ID}  # no source_type
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        sfn_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sfn_input["pipeline_id"] == PIPELINE_ID  # = pipeline_prefix
        assert sfn_input["source_type"] == "S3_UPLOAD"
        assert sfn_input["source_bucket"] == _ENV["SOURCE_BUCKET"]
        assert sfn_input["source_key"] == f"{PIPELINE_ID}/file.csv"
        assert sfn_input["trigger"] == "scheduled"

    def test_scheduled_unknown_source_type_returns_400(self, trigger_mock):
        """Unknown source_type on a scheduled event yields a 400 (no SFN call)."""
        lf, sfn, _s3, _locks, _execs = trigger_mock

        event = {
            "trigger": "scheduled",
            "pipeline_id": PIPELINE_ID,
            "source_type": "WEIRD",
        }
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 400
        assert "unknown source_type" in result["body"].lower()
        sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Explicit pipeline_id on every S3_UPLOAD path (Task 8.5)
# ---------------------------------------------------------------------------


class TestS3UploadPipelineIdAndSourceType:
    """Every S3_UPLOAD code path embeds ``pipeline_id`` AND
    ``source_type: "S3_UPLOAD"`` in the Step Functions input.

    Validates: Requirement 12 (explicit pipeline_id) for the trigger Lambda,
    Requirement 22.1.
    """

    def test_sqs_records_loop_includes_pipeline_id_and_source_type(self, trigger_mock):
        """SQS-driven S3 events emit ``pipeline_id`` (= pipeline_prefix) AND
        ``source_type: "S3_UPLOAD"`` alongside the legacy ``source_bucket``/
        ``source_key``/``trigger: "s3_upload"`` keys."""
        lf, sfn, _s3, _locks, _execs = trigger_mock

        event = _sqs_event("src-bucket", "my-pipeline/data.csv")
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        sfn_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sfn_input["pipeline_id"] == "my-pipeline"  # _pipeline_prefix(source_key)
        assert sfn_input["source_type"] == "S3_UPLOAD"
        assert sfn_input["source_bucket"] == "src-bucket"
        assert sfn_input["source_key"] == "my-pipeline/data.csv"
        assert sfn_input["trigger"] == "s3_upload"

    def test_manual_s3_upload_default_includes_pipeline_id_and_source_type(self, trigger_mock):
        """Manual trigger without ``source_type`` defaults to S3_UPLOAD and
        emits both ``pipeline_id`` AND ``source_type: "S3_UPLOAD"`` in the
        SFN input (with ``trigger: "manual"``)."""
        lf, sfn, s3, _locks, _execs = trigger_mock
        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/latest.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        event = {"trigger": "manual", "pipeline_id": PIPELINE_ID}  # no source_type
        result = lf.lambda_handler(event, None)

        assert result["statusCode"] == 200
        # The Run Now response shape carries execution_arn / execution_name
        assert "execution_arn" in result
        assert "execution_name" in result

        sfn.start_execution.assert_called_once()
        sfn_input = json.loads(sfn.start_execution.call_args.kwargs["input"])
        assert sfn_input["pipeline_id"] == PIPELINE_ID  # = pipeline_prefix
        assert sfn_input["source_type"] == "S3_UPLOAD"
        assert sfn_input["source_bucket"] == _ENV["SOURCE_BUCKET"]
        assert sfn_input["source_key"] == f"{PIPELINE_ID}/latest.csv"
        assert sfn_input["trigger"] == "manual"
