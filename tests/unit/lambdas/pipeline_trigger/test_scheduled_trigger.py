# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for scheduled pipeline trigger (Req 3).

Validates: Requirements 3.4, 3.5, 3.12
Tests file discovery, no-file skip, concurrency check, and SFN start
for the _handle_scheduled_trigger code path.
"""

import json
import os
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

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "SOURCE_BUCKET": "agentic-data-ingestion-source",
}

PIPELINE_ID = "test-scheduled-pipeline"


def _scheduled_event(pipeline_id: str = PIPELINE_ID) -> dict:
    """Build a scheduled trigger event."""
    return {
        "trigger": "scheduled",
        "pipeline_id": pipeline_id,
        "source_prefix": pipeline_id,
    }


@pytest.fixture()
def trigger_mock():
    """Yield the Lambda module with mocked SFN, S3, locks, and executions."""
    mock_sfn = MagicMock()
    mock_s3 = MagicMock()
    mock_locks_table = MagicMock()
    mock_executions_table = MagicMock()

    # Default: lock acquired successfully
    mock_locks_table.put_item.return_value = {}

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
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]
        with (
            mock.patch("boto3.client", side_effect=_client_factory),
            mock.patch("boto3.resource", return_value=mock_dynamodb),
        ):
            import lambda_function

            lambda_function.sfn_client = mock_sfn
            lambda_function.locks_table = mock_locks_table
            lambda_function.executions_table = mock_executions_table
            yield lambda_function, mock_sfn, mock_s3, mock_locks_table
    if trigger_dir in sys.path:
        sys.path.remove(trigger_dir)


# ---------------------------------------------------------------------------
# Tests — File discovery
# ---------------------------------------------------------------------------


class TestScheduledTriggerFileDiscovery:
    """Validates: Requirement 3.5 — finds most recent file under prefix."""

    def test_finds_most_recent_file(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        # Two files, second is newer
        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/old_file.csv",
                    "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
                },
                {
                    "Key": f"{PIPELINE_ID}/new_file.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        call_kwargs = sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_key"] == f"{PIPELINE_ID}/new_file.csv"
        assert sfn_input["trigger"] == "scheduled"
        assert sfn_input["source_bucket"] == "agentic-data-ingestion-source"

    def test_ignores_folder_marker_objects(self, trigger_mock):
        """Folder markers (keys ending with /) are excluded from file selection."""
        lf, sfn, s3, locks = trigger_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/",
                    "LastModified": datetime(2024, 6, 2, tzinfo=UTC),
                },
                {
                    "Key": f"{PIPELINE_ID}/data.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 200
        call_kwargs = sfn.start_execution.call_args[1]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_key"] == f"{PIPELINE_ID}/data.csv"


# ---------------------------------------------------------------------------
# Tests — No-file skip
# ---------------------------------------------------------------------------


class TestScheduledTriggerNoFileSkip:
    """Validates: Requirement 3.12 — skip when no source files exist."""

    def test_skips_when_no_files(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock

        s3.list_objects_v2.return_value = {"Contents": []}

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 200
        assert "No files to process" in result["body"]
        sfn.start_execution.assert_not_called()

    def test_skips_when_only_folder_markers(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/",
                    "LastModified": datetime(2024, 1, 1, tzinfo=UTC),
                },
            ]
        }

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 200
        assert "No files to process" in result["body"]
        sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — Concurrency check
# ---------------------------------------------------------------------------


class TestScheduledTriggerConcurrency:
    """Validates: Requirement 3.5 — concurrency gates apply to scheduled triggers."""

    def test_blocked_by_global_concurrency(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/file.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }
        sfn.list_executions.return_value = {"executions": [{}] * 10}

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 429
        assert "concurrency limit" in result["body"].lower()
        sfn.start_execution.assert_not_called()

    def test_blocked_by_pipeline_lock(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/file.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }
        sfn.list_executions.return_value = {"executions": []}
        locks.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 429
        assert "already has a running execution" in result["body"].lower()
        sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Tests — SFN start
# ---------------------------------------------------------------------------


class TestScheduledTriggerSFNStart:
    """Validates: Requirement 3.4, 3.5 — starts SFN execution correctly."""

    def test_starts_execution_with_correct_input(self, trigger_mock):
        lf, sfn, s3, locks = trigger_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/latest.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        result = lf.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
        call_kwargs = sfn.start_execution.call_args[1]
        assert call_kwargs["stateMachineArn"] == _ENV["STATE_MACHINE_ARN"]
        sfn_input = json.loads(call_kwargs["input"])
        assert sfn_input["source_bucket"] == "agentic-data-ingestion-source"
        assert sfn_input["source_key"] == f"{PIPELINE_ID}/latest.csv"
        assert sfn_input["trigger"] == "scheduled"

    def test_routes_scheduled_event_not_sqs(self, trigger_mock):
        """Scheduled events bypass SQS parsing and go to _handle_scheduled_trigger."""
        lf, sfn, s3, locks = trigger_mock
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:exec"}

        s3.list_objects_v2.return_value = {
            "Contents": [
                {
                    "Key": f"{PIPELINE_ID}/file.csv",
                    "LastModified": datetime(2024, 6, 1, tzinfo=UTC),
                },
            ]
        }

        # This event has trigger=scheduled but no Records key
        event = _scheduled_event()
        assert "Records" not in event

        result = lf.lambda_handler(event, None)
        assert result["statusCode"] == 200
        sfn.start_execution.assert_called_once()
