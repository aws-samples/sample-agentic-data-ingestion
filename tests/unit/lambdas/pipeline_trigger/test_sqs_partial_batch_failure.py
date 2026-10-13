# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The SQS path reports per-record failures instead of failing the batch.

p30 R9.3: "THE SQS handler in `pipeline_trigger` SHALL return a
partial-batch-failure response (`batchItemFailures`) rather than raising, so a
lock conflict on one record does not return already-started records to the
queue; THE Lambda's event source mapping SHALL enable
`ReportBatchItemFailures`."

Two mechanical properties carry the whole feature and both fail *silently*:

* ``itemIdentifier`` must be the record's ``messageId`` verbatim. Lambda ignores
  an identifier that does not match a message in the batch and treats that
  message as SUCCESSFUL — so a derived key (object key, execution name,
  fingerprint) looks like partial-batch reporting while discarding files.
* Nothing may escape the handler. If any exception propagates, SQS fails the
  whole batch regardless of the returned value, and the feature is inert.

The negative direction is asserted at least as hard as the positive one,
because the two ways of getting this wrong fail in opposite directions:
reporting a SUCCESS as a failure causes redelivery until the DLQ (the
duplicate-content skip is the live case), and omitting a real failure loses the
file silently.

Validates: Requirements 9.3
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

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": "test-executions",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "PROCESSED_FILES_TABLE": "agentic_data_ingestion_processed_files",
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

# An ETag/size pair for the duplicate-content fixtures. The bare (unquoted)
# spelling is what the EventBridge S3 notification carries.
_ETAG = "d41d8cd98f00b204e9800998ecf8427e"
_SIZE = 1024


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(os.environ, _ENV):
        yield


class _ExecutionAlreadyExists(Exception):
    """Stand-in for the botocore-generated SFN exception.

    The production code names ``sfn_client.exceptions.ExecutionAlreadyExists`` in
    an ``except`` clause; on a bare MagicMock that attribute is not an exception
    class and Python raises TypeError as soon as anything in the ``try`` raises.
    Same approach as ``test_error_paths.py`` and
    ``test_lock_release_on_start_failure.py``.
    """


def _load_handler(mock_sfn):
    """Load the trigger Lambda with mocked SFN / DynamoDB.

    Per ``LGR-b7d5490-1``, this directory loads the module via
    ``importlib.util.spec_from_file_location`` after dropping any cached
    ``lambda_function`` — several Lambdas share that module name and a plain
    import cross-contaminates.
    """
    locks = MagicMock()
    executions = MagicMock()
    processed = MagicMock()
    # Default: no ledger row, so nothing is a duplicate unless a test says so.
    processed.get_item.return_value = {}

    mock_dynamodb = MagicMock()

    def _table(name):
        if name == _ENV["LOCKS_TABLE"]:
            return locks
        if name == _ENV["PROCESSED_FILES_TABLE"]:
            return processed
        return executions

    mock_dynamodb.Table.side_effect = _table

    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.client", return_value=mock_sfn),
        patch("boto3.resource", return_value=mock_dynamodb),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    mod.sfn_client = mock_sfn
    mod.locks_table = locks
    mod.executions_table = executions
    mod.processed_files_table = processed
    return mod, locks, executions, processed


def _sfn(*, running=0, start_error=None):
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": [{}] * running}
    sfn.exceptions = MagicMock()
    sfn.exceptions.ExecutionAlreadyExists = _ExecutionAlreadyExists
    if start_error is not None:
        sfn.start_execution.side_effect = start_error
    else:
        sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return sfn


def _record(message_id, key, *, size=None, etag=None, body=None):
    """One SQS record wrapping an EventBridge S3 Object Created notification."""
    if body is None:
        obj = {"key": key}
        if size is not None:
            obj["size"] = size
        if etag is not None:
            obj["etag"] = etag
        body = json.dumps(
            {
                "source": "aws.s3",
                "detail-type": "Object Created",
                "detail": {"bucket": {"name": "src-bucket"}, "object": obj},
            }
        )
    rec = {"body": body, "eventSource": "aws:sqs"}
    if message_id is not None:
        rec["messageId"] = message_id
    return rec


def _event(*records):
    return {"Records": list(records)}


def _reported(result):
    """The messageIds the handler asked SQS to redeliver, in order."""
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


def _lock_contention():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


def _throttling():
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "StartExecution",
    )


def _started_keys(sfn):
    """The source_key of every execution actually started."""
    return [json.loads(c.kwargs["input"])["source_key"] for c in sfn.start_execution.call_args_list]


# ---------------------------------------------------------------------------
# The response shape
# ---------------------------------------------------------------------------


class TestResponseShape:
    """The contract Lambda reads. Getting the shape wrong is silently ignored."""

    def test_full_success_reports_an_empty_failure_list(self):
        """Explicit "all succeeded" rather than relying on the absent-key default."""
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "p/a.csv")), None)

        assert result["batchItemFailures"] == []
        assert _started_keys(sfn) == ["p/a.csv"]

    def test_empty_batch_still_carries_the_key(self):
        """An empty batch is a success, and says so in the same shape."""
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler({"Records": []}, None)

        assert result["batchItemFailures"] == []
        assert "No records" in result["body"]

    def test_failures_are_dicts_keyed_by_itemIdentifier_only(self):
        """Lambda discards a response whose entries are not this exact shape."""
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "p/a.csv")), None)

        assert result["batchItemFailures"] == [{"itemIdentifier": "m-1"}]

    def test_body_states_how_many_succeeded_and_how_many_came_back(self):
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [{}, _lock_contention()]

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert "1 succeeded" in result["body"]
        assert "1 returned to the queue" in result["body"]

    def test_the_direct_invocation_paths_do_not_report_batch_failures(self):
        """Manual and scheduled triggers have no messages to report on.

        Emitting the key there would be meaningless at best; the Run Now
        endpoint forwards this dict to an HTTP caller.
        """
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)
        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "p/a.csv", "LastModified": 1, "Size": _SIZE, "ETag": f'"{_ETAG}"'}]
        }
        handler.boto3 = MagicMock()
        handler.boto3.client.return_value = mock_s3

        manual = handler.lambda_handler({"trigger": "manual", "pipeline_id": "p"}, None)
        scheduled = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "p"}, None)

        assert "batchItemFailures" not in manual
        assert "batchItemFailures" not in scheduled


# ---------------------------------------------------------------------------
# The identifier is the messageId, verbatim
# ---------------------------------------------------------------------------


class TestItemIdentifierIsTheMessageId:
    """A wrong identifier is ignored and the message is treated as successful."""

    def test_identifier_equals_the_records_own_messageId(self):
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)
        message_id = "0d4f8a1e-9c3b-4a77-8f2e-1b6d5c4a3e2f"

        result = handler.lambda_handler(_event(_record(message_id, "p/a.csv")), None)

        assert _reported(result) == [message_id]

    def test_identifier_is_not_derived_from_the_object_key_or_execution_name(self):
        """Pins that no plausible-looking substitute leaked in.

        The object key, the pipeline prefix and the execution name are all in
        scope at the failure point and all of them would be silently discarded
        by Lambda.
        """
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/data.csv")), None)

        (identifier,) = _reported(result)
        assert identifier == "m-1"
        assert "my-pipeline" not in identifier
        assert "data.csv" not in identifier
        assert not identifier.startswith("file-")

    def test_each_failing_record_is_reported_under_its_own_id(self):
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1", "m-2"]

    def test_a_repeated_messageId_is_reported_once(self):
        """A duplicated identifier makes the whole response invalid to Lambda."""
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-1", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]

    def test_no_identifier_is_ever_empty(self):
        """Lambda discards the entire response on an empty itemIdentifier."""
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert all(f["itemIdentifier"] for f in result["batchItemFailures"])


# ---------------------------------------------------------------------------
# R9.3's own example: one conflict must not undo its siblings
# ---------------------------------------------------------------------------


class TestSiblingsAreNotDraggedBack:
    """The defect R9.3 exists to fix, from both ends of the batch."""

    def test_record_2_conflicting_leaves_record_1_started_and_unreported(self):
        """R9.3 verbatim: a lock conflict on one record does not return
        already-started records to the queue."""
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [{}, _lock_contention()]

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _started_keys(sfn) == ["one/a.csv"]
        assert _reported(result) == ["m-2"]

    def test_record_1_conflicting_does_not_prevent_record_2(self):
        """The loop continues past a failure rather than abandoning the batch."""
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [_lock_contention(), {}]

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _started_keys(sfn) == ["two/b.csv"]
        assert _reported(result) == ["m-1"]

    def test_a_failure_in_the_middle_isolates_to_itself(self):
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [{}, _lock_contention(), {}]

        result = handler.lambda_handler(
            _event(
                _record("m-1", "one/a.csv"),
                _record("m-2", "two/b.csv"),
                _record("m-3", "three/c.csv"),
            ),
            None,
        )

        assert _started_keys(sfn) == ["one/a.csv", "three/c.csv"]
        assert _reported(result) == ["m-2"]

    def test_two_records_for_the_SAME_pipeline_start_one_and_return_the_other(self):
        """The per-pipeline lock is doing its job; only the loser comes back.

        Before R9.3 this returned both, so the winner's execution ran twice.
        """
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [{}, _lock_contention()]

        result = handler.lambda_handler(_event(_record("m-1", "same/a.csv"), _record("m-2", "same/b.csv")), None)

        assert _started_keys(sfn) == ["same/a.csv"]
        assert _reported(result) == ["m-2"]


# ---------------------------------------------------------------------------
# Per-record vs batch-wide, decided case by case
# ---------------------------------------------------------------------------


class TestPerRecordClassification:
    """Every failure on this path is per-record. Each for its own reason."""

    def test_malformed_body_is_per_record(self):
        """Deterministic: it will never parse, so it belongs in the DLQ alone."""
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(
            _event(_record("m-1", None, body="this is not json {{{"), _record("m-2", "two/b.csv")),
            None,
        )

        assert _reported(result) == ["m-1"]
        assert _started_keys(sfn) == ["two/b.csv"]

    def test_missing_bucket_or_key_is_per_record(self):
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)
        no_bucket = json.dumps({"detail": {"object": {"key": "a.csv"}}})
        no_key = json.dumps({"detail": {"bucket": {"name": "b"}}})

        result = handler.lambda_handler(
            _event(
                _record("m-1", None, body=no_bucket),
                _record("m-2", None, body=no_key),
                _record("m-3", "three/c.csv"),
            ),
            None,
        )

        assert _reported(result) == ["m-1", "m-2"]
        assert _started_keys(sfn) == ["three/c.csv"]

    def test_global_concurrency_is_per_record_not_batch_wide(self):
        """The snapshot is re-read per record and is a lower bound at one instant.

        A batch saturated at record 1 may genuinely have drained by record 2, so
        failing the later records without checking would report a fleet state
        that was never observed. Here the fleet drains between the two reads and
        record 2 starts.
        """
        sfn = _sfn()
        # Record 1 sees saturation; record 2 sees a drained fleet.
        sfn.list_executions.side_effect = [
            {"executions": [{}] * 10},
            {"executions": [{}] * 2},
        ]
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]
        assert _started_keys(sfn) == ["two/b.csv"]

    def test_concurrency_rejection_still_writes_the_queued_tracker(self):
        """The record IS coming back, so the QUEUED row remains truthful.

        BOUNDED, and the bound lives outside this test (p54 task 7.6). "Coming
        back" holds for the redelivery budget only: ``maxReceiveCount ×
        visibilityTimeout`` = 50 × 30 s = **25 minutes**
        (``deploy-cdk/lib/orchestration-stack.ts``). Past that SQS moves the
        message to the DLQ, and this row stops being truthful — it still reads
        ``QUEUED`` for a file that is never coming back. A run parked at an
        approval gate holds the pipeline lock for up to 72 h
        (``LGR-69ca78b-2``; ``LGR-5d68eeb-4`` — nothing releases a parked
        approval before that gate), so the budget is the shorter number by a
        factor of 173. p54 R10.2 records that as unsatisfied; closing it is p54
        task 7.7's, and no value moved here.

        The assertion below is deliberately UNCHANGED and must stay: inside the
        budget it is correct, and it is one of the few pins on
        ``_track_queued_file`` writing the row at all.
        """
        sfn = _sfn(running=10)
        handler, _locks, executions, _pf = _load_handler(sfn)

        handler.lambda_handler(_event(_record("m-1", "p/a.csv")), None)

        item = executions.put_item.call_args.kwargs["Item"]
        assert item["status"] == "QUEUED"

    def test_contract_violation_is_per_record(self):
        """Deterministic, so DLQ under its own id rather than dragging siblings."""
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)
        real_builder = handler.build_trigger_input

        def _selective(pipeline, **kwargs):
            if kwargs.get("source_key") == "one/a.csv":
                raise ValueError("bad shape")
            return real_builder(pipeline, **kwargs)

        handler.build_trigger_input = _selective

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]
        assert _started_keys(sfn) == ["two/b.csv"]

    def test_execution_already_exists_is_per_record(self):
        """The redelivery mints a fresh timestamped name; only this one needs it."""
        sfn = _sfn()
        sfn.start_execution.side_effect = [
            _ExecutionAlreadyExists("collision"),
            {"executionArn": "arn:exec:2"},
        ]
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]

    def test_transient_start_execution_error_is_per_record(self):
        sfn = _sfn()
        sfn.start_execution.side_effect = [_throttling(), {"executionArn": "arn:exec:2"}]
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]
        assert _started_keys(sfn) == ["one/a.csv", "two/b.csv"]

    def test_an_unexpected_error_does_not_escape_and_fail_the_batch(self):
        """Nothing may propagate: SQS ignores the return value if it does.

        A ledger read that raises is handled inside the ledger helper, so this
        uses an unexpected failure from the lock write — a bare RuntimeError,
        which no ``except ClientError`` covers.
        """
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = [RuntimeError("DynamoDB unavailable"), {}]

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert _reported(result) == ["m-1"]
        assert _started_keys(sfn) == ["two/b.csv"]


# ---------------------------------------------------------------------------
# A duplicate-content skip is a SUCCESS (R9.2 × R9.3)
# ---------------------------------------------------------------------------


class TestDuplicateSkipIsNotAFailure:
    """Reporting a skip would redeliver a message whose work is already done."""

    def _ledger_hit(self, processed):
        processed.get_item.return_value = {
            "Item": {"run_id": "run-earlier", "content_size": _SIZE, "content_etag": _ETAG}
        }

    def test_a_duplicate_is_absent_from_batchItemFailures(self):
        sfn = _sfn()
        handler, _locks, _ex, processed = _load_handler(sfn)
        self._ledger_hit(processed)

        result = handler.lambda_handler(_event(_record("m-1", "p/a.csv", size=_SIZE, etag=_ETAG)), None)

        assert result["batchItemFailures"] == []
        sfn.start_execution.assert_not_called()

    def test_a_duplicate_alongside_a_real_failure_reports_only_the_failure(self):
        """The two outcomes must not be conflated: one is done, one must retry."""
        sfn = _sfn()
        handler, locks, _ex, processed = _load_handler(sfn)
        self._ledger_hit(processed)
        locks.put_item.side_effect = _lock_contention()

        result = handler.lambda_handler(
            _event(
                _record("m-dup", "p/a.csv", size=_SIZE, etag=_ETAG),
                # No size/etag → no fingerprint → not a duplicate; it reaches
                # the lock and loses.
                _record("m-conflict", "q/b.csv"),
            ),
            None,
        )

        assert _reported(result) == ["m-conflict"]

    def test_a_duplicate_alongside_a_start_still_reports_nothing(self):
        sfn = _sfn()
        handler, _locks, _ex, processed = _load_handler(sfn)
        # Only the fingerprinted record hits the ledger; the other has no etag.
        self._ledger_hit(processed)

        result = handler.lambda_handler(
            _event(
                _record("m-dup", "p/a.csv", size=_SIZE, etag=_ETAG),
                _record("m-ok", "q/b.csv"),
            ),
            None,
        )

        assert result["batchItemFailures"] == []
        assert _started_keys(sfn) == ["q/b.csv"]


# ---------------------------------------------------------------------------
# 4.10's lock-release rule survives partial-batch reporting
# ---------------------------------------------------------------------------


class TestLockReleasedBeforeBeingReported:
    """A record that fails AFTER acquiring the lock must still release it.

    Otherwise the redelivery this failure buys is rejected by the lock the same
    invocation left behind, and the pipeline stays wedged for the 24 h
    LOCK_TTL_SECONDS (p30 R10.3, task 4.10).
    """

    def _released(self, locks):
        return [c.kwargs["Key"]["pipeline_prefix"] for c in locks.delete_item.call_args_list]

    def test_start_failure_releases_the_lock_and_reports_the_record(self):
        sfn = _sfn(start_error=_throttling())
        handler, locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/a.csv")), None)

        assert self._released(locks) == ["my-pipeline"]
        assert _reported(result) == ["m-1"]

    def test_contract_violation_releases_the_lock_and_reports_the_record(self):
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        handler.build_trigger_input = MagicMock(side_effect=ValueError("bad shape"))

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/a.csv")), None)

        assert self._released(locks) == ["my-pipeline"]
        assert _reported(result) == ["m-1"]
        sfn.start_execution.assert_not_called()

    def test_name_collision_releases_the_lock_and_reports_the_record(self):
        sfn = _sfn(start_error=_ExecutionAlreadyExists("collision"))
        handler, locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/a.csv")), None)

        assert self._released(locks) == ["my-pipeline"]
        assert _reported(result) == ["m-1"]

    def test_contention_reports_without_releasing_someone_elses_lock(self):
        """Contention rejects BEFORE acquisition, so there is nothing to release.

        Releasing here would delete the lock held by the genuinely running
        execution.
        """
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)
        locks.put_item.side_effect = _lock_contention()

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/a.csv")), None)

        assert _reported(result) == ["m-1"]
        locks.delete_item.assert_not_called()

    def test_a_successful_record_keeps_its_lock(self):
        """The lock IS the concurrency gate; releasing it on success defeats it."""
        sfn = _sfn()
        handler, locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "my-pipeline/a.csv")), None)

        assert result["batchItemFailures"] == []
        locks.delete_item.assert_not_called()

    def test_one_records_release_does_not_disturb_another_pipelines_lock(self):
        sfn = _sfn()
        sfn.start_execution.side_effect = [_throttling(), {"executionArn": "arn:exec:2"}]
        handler, locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record("m-1", "one/a.csv"), _record("m-2", "two/b.csv")), None)

        assert self._released(locks) == ["one"]
        assert _reported(result) == ["m-1"]


# ---------------------------------------------------------------------------
# The one case that is deliberately batch-wide
# ---------------------------------------------------------------------------


class TestUnreportableFailureFailsLoudly:
    """A failure with no messageId cannot be reported, so it must not be hidden.

    Dropping it would report a failure as a success and lose the file. Emitting
    an empty or unmatched itemIdentifier is worse, because Lambda discards the
    whole response. Raising is bad for the record's siblings and is still the
    only option that cannot lose data. Real SQS always supplies messageId, so
    this is reachable only from a hand-built or non-SQS invocation.
    """

    def test_a_failing_record_without_a_messageId_raises(self):
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        with pytest.raises(Exception, match="concurrency limit reached"):
            handler.lambda_handler(_event(_record(None, "p/a.csv")), None)

    def test_the_original_cause_is_what_surfaces(self):
        """Not a wrapper about reporting — the operator needs the real failure."""
        sfn = _sfn(start_error=_throttling())
        handler, _locks, _ex, _pf = _load_handler(sfn)

        with pytest.raises(ClientError) as exc_info:
            handler.lambda_handler(_event(_record(None, "p/a.csv")), None)

        assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"

    def test_a_SUCCESSFUL_record_without_a_messageId_is_fine(self):
        """Only a *failure* needs an identifier, so success must not be blocked."""
        sfn = _sfn()
        handler, _locks, _ex, _pf = _load_handler(sfn)

        result = handler.lambda_handler(_event(_record(None, "p/a.csv")), None)

        assert result["batchItemFailures"] == []
        assert _started_keys(sfn) == ["p/a.csv"]

    def test_an_empty_string_messageId_counts_as_absent(self):
        """An empty itemIdentifier would make Lambda discard the response."""
        sfn = _sfn(running=10)
        handler, _locks, _ex, _pf = _load_handler(sfn)

        with pytest.raises(Exception, match="concurrency limit reached"):
            handler.lambda_handler(_event(_record("", "p/a.csv")), None)
