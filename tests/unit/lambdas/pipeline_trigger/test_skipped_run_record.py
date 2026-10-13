# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Skipped_Run_Record on scheduled contention, plus its notification (p30 R10.1).

The requirement is narrow and the narrowness is the point: only a **scheduled**
trigger's contention is a skip. Three of the four contention sites in this
Lambda are NOT recorded, each for its own reason, and the tests below pin all
four so a later change cannot quietly widen the definition:

* scheduled + global concurrency, scheduled + pipeline lock — recorded. Nothing
  retries a rejected schedule; the load is lost.
* SQS/s3_upload contention — NOT recorded. The record returns to the queue as a
  ``batchItemFailures`` entry (R9.3) and is redelivered, so a SKIPPED row would
  claim a loss for work that is about to succeed. It keeps writing the QUEUED
  tracker, which stays truthful.
* manual Run Now contention — NOT recorded. R10.1 scopes this to scheduled
  triggers, and the human who asked was told synchronously with the counts.

Also pinned: the collapse window is what rate-limits the notification (one per
pipeline+reason+window, not one per skip), and a stored running count never
travels without its floor flag (4.11) — a bare count would let a reader of the
row conclude "11 running" when 40 were.

Validates: Requirements 10.1, 10.2
"""

import importlib.util
import json
import os
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

_LOCKS_TABLE = "agentic_data_ingestion_pipeline_locks"
_PROCESSED_TABLE = "test-processed-files"
_EXECUTIONS_TABLE = "test-executions"

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": _EXECUTIONS_TABLE,
    "LOCKS_TABLE": _LOCKS_TABLE,
    "PROCESSED_FILES_TABLE": _PROCESSED_TABLE,
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

_ETAG = "d41d8cd98f00b204e9800998ecf8427e"
_ACTIVE_PATCHERS: list = []


@pytest.fixture(autouse=True)
def _lambda_env():
    """Env and boto3 patches stay live for the whole body, not just the import.

    The scheduled/manual handlers read ``SOURCE_BUCKET`` and build their own
    ``boto3.client("s3")`` inside the function, so patching only around the
    module import gives "SOURCE_BUCKET not configured" and a real S3 call.
    """
    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        yield
    for patcher in _ACTIVE_PATCHERS:
        patcher.stop()
    _ACTIVE_PATCHERS.clear()


def _conditional_check_failed() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


def _load_handler(mock_sfn, mock_s3=None, lock_available=True):
    """Load pipeline_trigger with independently addressable table mocks.

    ``importlib.util`` file loading per LGR-b7d5490-1: the Lambda directory is
    not an importable package, and the module reads its env and builds its boto3
    clients at import time.
    """
    locks = MagicMock()
    if lock_available:
        locks.put_item.return_value = {}
    else:
        locks.put_item.side_effect = _conditional_check_failed()
    executions = MagicMock()
    processed = MagicMock()
    processed.get_item.return_value = {}

    mock_dynamo = MagicMock()

    def _table(name):
        if name == _LOCKS_TABLE:
            return locks
        if name == _PROCESSED_TABLE:
            return processed
        return executions

    mock_dynamo.Table.side_effect = _table

    def _client(service, *args, **kwargs):
        if service == "s3" and mock_s3 is not None:
            return mock_s3
        return mock_sfn

    client_patcher = patch("boto3.client", side_effect=_client)
    resource_patcher = patch("boto3.resource", return_value=mock_dynamo)
    client_patcher.start()
    resource_patcher.start()
    _ACTIVE_PATCHERS.extend([client_patcher, resource_patcher])

    sys.modules.pop("lambda_function", None)
    spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.sfn_client = mock_sfn
    mod.locks_table = locks
    mod.executions_table = executions
    mod.processed_files_table = processed
    return mod, locks, executions


def _idle_sfn():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return sfn


def _saturated_sfn(count=10, truncated=False):
    """A snapshot at or above the ceiling. ``truncated`` adds a nextToken, which
    is what makes the count a floor rather than a total."""
    sfn = MagicMock()
    resp = {"executions": [{"name": f"e{i}"} for i in range(count)]}
    if truncated:
        resp["nextToken"] = "more"
    sfn.list_executions.return_value = resp
    sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return sfn


def _s3_with_one_object(key="orders/data.csv"):
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {
        "Contents": [{"Key": key, "LastModified": 1, "Size": 1024, "ETag": f'"{_ETAG}"'}]
    }
    return s3


def _scheduled_event(pipeline_id="orders"):
    return {"trigger": "scheduled", "pipeline_id": pipeline_id, "source_prefix": "orders"}


def _sqs_event(key="orders/data.csv"):
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": "src-bucket"}, "object": {"key": key, "size": 1024, "etag": _ETAG}},
    }
    return {"Records": [{"messageId": "msg-1", "body": json.dumps(body)}]}


def _skip_rows(executions):
    """Every SKIPPED item the handler put_item'd, in call order."""
    rows = []
    for call in executions.put_item.call_args_list:
        item = call.kwargs.get("Item") or (call.args[0] if call.args else {})
        if item.get("status") == "SKIPPED":
            rows.append(item)
    return rows


def _queued_rows(executions):
    rows = []
    for call in executions.put_item.call_args_list:
        item = call.kwargs.get("Item") or (call.args[0] if call.args else {})
        if item.get("status") == "QUEUED":
            rows.append(item)
    return rows


class TestScheduledConcurrencySkip:
    """Scheduled S3_UPLOAD rejected by the global gate — R10.1's headline case."""

    def test_writes_a_skipped_run_record(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(_scheduled_event(), None)

        rows = _skip_rows(executions)
        assert len(rows) == 1
        row = rows[0]
        assert row["skip_reason"] == handler.SKIP_REASON_CONCURRENCY
        assert row["triggered_by"] == "scheduled"
        assert row["pipeline_id"] == "orders"
        assert result["statusCode"] == 429
        assert result["skipped"] is True
        assert notify.call_count == 1

    def test_no_execution_is_started(self):
        sfn = _saturated_sfn()
        handler, _locks, _executions = _load_handler(sfn, _s3_with_one_object())
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)
        sfn.start_execution.assert_not_called()

    def test_stores_the_running_count_with_its_floor_flag(self):
        """4.11: the count saturates at a page boundary, so it never travels alone."""
        handler, _locks, executions = _load_handler(
            _saturated_sfn(count=11, truncated=True), _s3_with_one_object()
        )
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)

        row = _skip_rows(executions)[0]
        assert row["running_count"] == 11
        assert row["running_count_is_floor"] is True
        assert row["running_max"] == 10

    def test_a_complete_count_is_flagged_as_not_a_floor(self):
        handler, _locks, executions = _load_handler(
            _saturated_sfn(count=10, truncated=False), _s3_with_one_object()
        )
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)

        row = _skip_rows(executions)[0]
        assert row["running_count"] == 10
        assert row["running_count_is_floor"] is False

    def test_the_floor_flag_reaches_the_notification(self):
        handler, _locks, _executions = _load_handler(
            _saturated_sfn(count=11, truncated=True), _s3_with_one_object()
        )
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            handler.lambda_handler(_scheduled_event(), None)

        # (pipeline, run_id, reason, detail, running_count, count_is_floor)
        args = notify.call_args.args
        assert args[4] == 11
        assert args[5] is True


class TestScheduledLockContentionSkip:
    """Scheduled S3_UPLOAD rejected by the per-pipeline lock."""

    def test_writes_a_skipped_run_record_with_the_lock_reason(self):
        handler, _locks, executions = _load_handler(
            _idle_sfn(), _s3_with_one_object(), lock_available=False
        )
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(_scheduled_event(), None)

        rows = _skip_rows(executions)
        assert len(rows) == 1
        assert rows[0]["skip_reason"] == handler.SKIP_REASON_LOCK_CONTENTION
        assert result["skip_reason"] == handler.SKIP_REASON_LOCK_CONTENTION
        assert notify.call_count == 1

    def test_stores_no_running_count(self):
        """A lock rejection has no fleet-wide number behind it, and a zero would
        read as one."""
        handler, _locks, executions = _load_handler(
            _idle_sfn(), _s3_with_one_object(), lock_available=False
        )
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)

        row = _skip_rows(executions)[0]
        assert "running_count" not in row
        assert "running_count_is_floor" not in row


class TestScheduledJdbcSkip:
    """The JDBC helper serves manual AND scheduled, so it gates on the trigger."""

    def test_scheduled_jdbc_concurrency_is_recorded(self):
        handler, _locks, executions = _load_handler(_saturated_sfn())
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "jdbc-pipe", "source_type": "JDBC"}, None)

        rows = _skip_rows(executions)
        assert len(rows) == 1
        assert rows[0]["skip_reason"] == handler.SKIP_REASON_CONCURRENCY
        assert rows[0]["pipeline_id"] == "jdbc-pipe"
        assert notify.call_count == 1

    def test_scheduled_jdbc_lock_contention_is_recorded(self):
        handler, _locks, executions = _load_handler(_idle_sfn(), lock_available=False)
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "jdbc-pipe", "source_type": "JDBC"}, None)

        assert [r["skip_reason"] for r in _skip_rows(executions)] == [handler.SKIP_REASON_LOCK_CONTENTION]

    def test_manual_jdbc_contention_is_not_recorded(self):
        handler, _locks, executions = _load_handler(_saturated_sfn())
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "jdbc-pipe", "source_type": "JDBC"}, None)

        assert _skip_rows(executions) == []
        assert notify.call_count == 0
        assert result["statusCode"] == 429


class TestNonScheduledContentionIsNotASkip:
    """The three paths that must NOT produce a Skipped_Run_Record."""

    def test_sqs_concurrency_contention_writes_queued_not_skipped(self):
        """4.8: the message comes back. A SKIPPED row would claim a loss for work
        that is about to succeed on redelivery."""
        handler, _locks, executions = _load_handler(_saturated_sfn())
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(_sqs_event(), None)

        assert _skip_rows(executions) == []
        assert len(_queued_rows(executions)) == 1
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
        assert notify.call_count == 0

    def test_sqs_lock_contention_writes_queued_not_skipped(self):
        handler, _locks, executions = _load_handler(_idle_sfn(), lock_available=False)
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(_sqs_event(), None)

        assert _skip_rows(executions) == []
        assert len(_queued_rows(executions)) == 1
        assert result["batchItemFailures"] == [{"itemIdentifier": "msg-1"}]
        assert notify.call_count == 0

    def test_manual_s3_contention_is_not_recorded(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(
                {"trigger": "manual", "pipeline_id": "orders", "source_prefix": "orders"}, None
            )

        assert _skip_rows(executions) == []
        assert notify.call_count == 0
        assert result["statusCode"] == 429


class TestRunIdSchemeAndSortBehaviour:
    """The id shape is the ordering decision 4.7 left open."""

    def test_id_shares_the_run_prefix_so_it_sorts_chronologically(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)

        run_id = _skip_rows(executions)[0]["run_id"]
        assert run_id.startswith("run-")
        assert run_id.endswith(f"-skip-{handler.SKIP_REASON_CONCURRENCY}")

    def test_id_does_not_sort_above_a_real_run_id(self):
        """4.7's ``skipped-`` prefix sorts above ``run-`` under the run-history
        descending sort, putting skips on top of page 1 regardless of age. This
        scheme must not inherit that."""
        from datetime import UTC, datetime

        handler, _locks, _executions = _load_handler(_idle_sfn())
        window = datetime(2026, 8, 2, 14, 0, 0, tzinfo=UTC)
        skip_id = handler._contention_skip_record_id(handler.SKIP_REASON_CONCURRENCY, window)

        older_real_run = "run-2026-08-01-09-15-00-a1b2c3d4"
        newer_real_run = "run-2026-08-02-16-30-00-a1b2c3d4"
        assert skip_id > older_real_run
        assert skip_id < newer_real_run

    def test_windows_are_epoch_aligned_so_two_invocations_agree(self):
        from datetime import UTC, datetime

        handler, _locks, _executions = _load_handler(_idle_sfn())
        early = datetime(2026, 8, 2, 14, 3, 11, tzinfo=UTC)
        late = datetime(2026, 8, 2, 14, 58, 59, tzinfo=UTC)
        assert handler._skip_window_start(early) == handler._skip_window_start(late)
        next_window = datetime(2026, 8, 2, 15, 0, 1, tzinfo=UTC)
        assert handler._skip_window_start(next_window) != handler._skip_window_start(late)

    def test_the_two_reasons_get_different_records_in_the_same_window(self):
        from datetime import UTC, datetime

        handler, _locks, _executions = _load_handler(_idle_sfn())
        window = datetime(2026, 8, 2, 14, 0, 0, tzinfo=UTC)
        assert handler._contention_skip_record_id(
            handler.SKIP_REASON_CONCURRENCY, window
        ) != handler._contention_skip_record_id(handler.SKIP_REASON_LOCK_CONTENTION, window)


class TestWindowCollapseRateLimitsTheNotification:
    """The conditional put is both the collapse and the rate-limit."""

    def test_second_skip_in_the_window_folds_and_does_not_notify(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        executions.put_item.side_effect = _conditional_check_failed()

        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            handler.lambda_handler(_scheduled_event(), None)

        assert notify.call_count == 0
        assert executions.update_item.call_count == 1
        update = executions.update_item.call_args.kwargs
        assert "ADD skip_count :one" in update["UpdateExpression"]
        assert update["ExpressionAttributeValues"][":one"] == 1

    def test_the_fold_targets_the_same_record_the_put_tried(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        attempted: list = []

        def _put(**kwargs):
            attempted.append(kwargs["Item"]["run_id"])
            raise _conditional_check_failed()

        executions.put_item.side_effect = _put
        with patch.object(handler, "_notify_scheduled_run_skipped"):
            handler.lambda_handler(_scheduled_event(), None)

        assert executions.update_item.call_args.kwargs["Key"]["run_id"] == attempted[0]

    def test_a_failed_record_write_does_not_turn_a_skip_into_an_error(self):
        """The skip is already decided; a non-2xx would make EventBridge retry a
        decision that will not change."""
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        executions.put_item.side_effect = RuntimeError("dynamo down")

        with patch.object(handler, "_notify_scheduled_run_skipped") as notify:
            result = handler.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 429
        assert notify.call_count == 0


class TestNotificationIsBestEffort:
    """R10.1 asks for a notification, not for the trigger to depend on one."""

    def test_an_unimportable_notification_service_does_not_break_the_skip(self):
        """``notification_service`` imports ``requests`` at module top and this
        Lambda's layer installs powertools only, so the import can genuinely
        fail in production."""
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())

        real_import = __import__

        def _no_notification_service(name, *args, **kwargs):
            if name == "notification_service":
                raise ImportError("No module named 'requests'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_no_notification_service):
            result = handler.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 429
        assert len(_skip_rows(executions)) == 1

    def test_a_raising_notification_does_not_break_the_skip(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        fake_module = MagicMock()
        fake_module.send_scheduled_run_skipped_alert.side_effect = RuntimeError("sns exploded")

        with patch.dict("sys.modules", {"notification_service": fake_module}):
            result = handler.lambda_handler(_scheduled_event(), None)

        assert result["statusCode"] == 429
        assert len(_skip_rows(executions)) == 1

    def test_the_notification_receives_the_record_id_and_the_window(self):
        handler, _locks, executions = _load_handler(_saturated_sfn(), _s3_with_one_object())
        fake_module = MagicMock()

        with patch.dict("sys.modules", {"notification_service": fake_module}):
            handler.lambda_handler(_scheduled_event(), None)

        kwargs = fake_module.send_scheduled_run_skipped_alert.call_args.kwargs
        args = fake_module.send_scheduled_run_skipped_alert.call_args.args
        assert args[1] == _skip_rows(executions)[0]["run_id"]
        assert kwargs["window_seconds"] == handler.SKIPPED_RUN_NOTIFY_WINDOW_SECONDS
        assert kwargs["running_max"] == 10

    def test_a_lock_skip_reports_no_fleet_numbers_to_the_notification(self):
        handler, _locks, _executions = _load_handler(
            _idle_sfn(), _s3_with_one_object(), lock_available=False
        )
        fake_module = MagicMock()

        with patch.dict("sys.modules", {"notification_service": fake_module}):
            handler.lambda_handler(_scheduled_event(), None)

        kwargs = fake_module.send_scheduled_run_skipped_alert.call_args.kwargs
        assert kwargs["running_count"] is None
        assert kwargs["running_max"] is None
