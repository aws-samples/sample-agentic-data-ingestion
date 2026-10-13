# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Processed_File_Ledger duplicate-content gate in pipeline_trigger (p30 R9.1, R9.2).

The trigger is the READER of a ledger job_completion writes. Four trigger paths
exist and only three of them have a file: SQS/s3_upload, manual S3_UPLOAD,
scheduled S3_UPLOAD, and JDBC (no file at all).

Two orderings are asserted deliberately rather than incidentally:

* the check runs BEFORE ``_acquire_pipeline_lock``, so a skip never leaves a lock
  to release (4.10 / R10.3) — a leaked lock would wedge the pipeline for 24 h
  every time someone re-uploaded a file;
* on the SQS path a duplicate ``continue``s instead of raising, so a file whose
  correct outcome is "skipped" is not retried and eventually DLQ'd.

Validates: Requirements 9.1, 9.2
"""

import importlib
import importlib.util
import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

_ENV = {
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
    "MAX_CONCURRENT_EXECUTIONS": "10",
    "STATE_TABLE": "test-state",
    "EXECUTIONS_TABLE": "test-executions",
    "LOCKS_TABLE": _LOCKS_TABLE,
    "PROCESSED_FILES_TABLE": _PROCESSED_TABLE,
    "SOURCE_BUCKET": "src-bucket",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

# Single-part ETag: the content MD5. Quoted by boto3, bare in EventBridge.
_ETAG = "d41d8cd98f00b204e9800998ecf8427e"


_ACTIVE_PATCHERS: list = []


@pytest.fixture(autouse=True)
def _lambda_env():
    """Keep the env and the boto3 patches live for the whole test body.

    Both are read at CALL time, not import time: the manual/scheduled handlers
    read ``SOURCE_BUCKET`` from the environment and construct their own
    ``boto3.client("s3")`` inside the function. Patching only around the module
    import gives "SOURCE_BUCKET not configured" and a real S3 call.
    """
    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        yield
    for patcher in _ACTIVE_PATCHERS:
        patcher.stop()
    _ACTIVE_PATCHERS.clear()


def _load_handler(mock_sfn, mock_s3=None):
    """Load pipeline_trigger with three independently addressable table mocks."""
    locks = MagicMock()
    locks.put_item.return_value = {}
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
    return mod, locks, executions, processed


def _sqs_event(key="orders/data.csv", size=1024, etag=_ETAG, bucket="src-bucket"):
    """An EventBridge S3 Object Created event wrapped in an SQS record.

    ``size`` is required by the event schema; ``etag`` is optional there, so
    ``etag=None`` is a real production shape and not a contrived one.
    """
    obj = {"key": key}
    if size is not None:
        obj["size"] = size
    if etag is not None:
        obj["etag"] = etag
    body = {
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": bucket}, "object": obj},
    }
    # messageId as real SQS sends it. p30 R9.3 turned the SQS path into a
    # partial-batch reporter, and a skip must NOT appear in batchItemFailures —
    # it is a success. Asserted in `TestSqsUploadPath` below and again, against
    # mixed batches, in test_sqs_partial_batch_failure.py.
    return {"Records": [{"messageId": "msg-1", "body": json.dumps(body)}]}


def _idle_sfn():
    sfn = MagicMock()
    sfn.list_executions.return_value = {"executions": []}
    sfn.start_execution.return_value = {"executionArn": "arn:exec:1"}
    return sfn


def _s3_with_one_object(key="orders/data.csv", size=1024, etag=f'"{_ETAG}"'):
    """A ``list_objects_v2`` response as boto3 returns it: ETag is QUOTED."""
    entry = {"Key": key, "LastModified": 1}
    if size is not None:
        entry["Size"] = size
    if etag is not None:
        entry["ETag"] = etag
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {"Contents": [entry]}
    return s3


def _ledger_hit(
    processed,
    run_id="run-2026-08-02-11-00-00-abcd1234",
    source_key="orders/first-upload.csv",
    content_size=1024,
    content_etag=_ETAG,
):
    """A matched ledger row, shaped as ``record_processed_file`` writes one.

    ``content_size`` arrives as a ``Decimal`` off DynamoDB in production, so the
    default is that type rather than ``int``. Any of the three pre-image
    attributes can be passed ``None`` to model a row written before p30 added
    them — the omission is a real shape, not a contrived one.
    """
    item = {"run_id": run_id}
    if source_key is not None:
        item["source_key"] = source_key
    if content_size is not None:
        item["content_size"] = Decimal(content_size)
    if content_etag is not None:
        item["content_etag"] = content_etag
    processed.get_item.return_value = {"Item": item}


class TestSqsUploadPath:
    """SQS/s3_upload — the path a re-upload actually arrives on."""

    def test_duplicate_content_skips_without_starting_an_execution(self):
        sfn = _idle_sfn()
        handler, locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        result = handler.lambda_handler(_sqs_event(), None)

        assert result["statusCode"] == 200
        sfn.start_execution.assert_not_called()
        locks.put_item.assert_not_called()

    def test_duplicate_does_not_raise_so_sqs_does_not_retry_or_dlq(self):
        """The correct outcome is "skipped". Raising would return the message to
        the queue and eventually DLQ a successful decision."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        # No pytest.raises: the absence of an exception IS the assertion.
        assert handler.lambda_handler(_sqs_event(), None)["statusCode"] == 200

    def test_duplicate_is_not_reported_as_a_batch_item_failure(self):
        """p30 R9.3: a skip is a SUCCESS, so it must not be named for redelivery.

        Reporting it would have SQS redeliver a message whose correct outcome was
        "do nothing" and eventually DLQ it — the same defect as the raise, just
        expressed through the newer mechanism.
        """
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        result = handler.lambda_handler(_sqs_event(), None)

        assert result["batchItemFailures"] == []

    def test_ledger_is_read_before_the_concurrency_snapshot(self):
        """Queueing and retrying a file that will be skipped anyway wastes the
        retry budget and leaves a QUEUED row the user has to interpret."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(), None)

        processed.get_item.assert_called_once()
        sfn.list_executions.assert_not_called()

    def test_fingerprint_uses_the_bare_event_etag_and_event_size(self):
        """No S3 call is added: size and ETag come off the EventBridge detail."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(size=4096, etag=_ETAG), None)

        expected = handler.content_fingerprint(4096, _ETAG)
        assert processed.get_item.call_args.kwargs["Key"]["fingerprint"] == expected

    def test_unseen_content_starts_normally(self):
        sfn = _idle_sfn()
        handler, locks, _executions, processed = _load_handler(sfn)
        processed.get_item.return_value = {}

        handler.lambda_handler(_sqs_event(), None)

        assert sfn.start_execution.call_count == 1
        assert locks.put_item.call_count == 1

    def test_absent_etag_skips_the_ledger_entirely_and_proceeds(self):
        """``detail.object.etag`` is optional in the event schema. No
        fingerprint → no dedup → proceed. The harmless direction."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)

        handler.lambda_handler(_sqs_event(etag=None), None)

        processed.get_item.assert_not_called()
        assert sfn.start_execution.call_count == 1

    def test_ledger_read_failure_fails_open(self):
        """A ledger outage costs the dedup, never the ingestion."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        processed.get_item.side_effect = RuntimeError("ProvisionedThroughputExceeded")

        handler.lambda_handler(_sqs_event(), None)

        assert sfn.start_execution.call_count == 1

    def test_stale_queued_tracker_is_cleared_on_skip(self):
        """The file will never process, so leaving it listed as queued would be
        a lie until the tracker's TTL expired."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(), None)

        assert executions.delete_item.call_count == 1


class TestSkipIsVisible:
    """R9.2: the skip is recorded with a reason visible in the UI, not just logged."""

    def test_skip_record_is_written_to_the_executions_table(self):
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, run_id="run-earlier")

        handler.lambda_handler(_sqs_event(key="orders/data.csv"), None)

        item = executions.put_item.call_args.kwargs["Item"]
        assert item["pipeline_id"] == "orders"
        assert item["status"] == "SKIPPED"
        assert item["skip_reason"] == "duplicate_content"
        assert item["previous_run_id"] == "run-earlier"
        assert item["source_key"] == "orders/data.csv"
        assert item["ttl"] > 0

    def test_skip_detail_names_the_earlier_run_and_the_override(self):
        """A user looking at the row needs to know which run already did this and
        how to force a re-run."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, run_id="run-earlier")

        handler.lambda_handler(_sqs_event(), None)

        detail = executions.put_item.call_args.kwargs["Item"]["skip_detail"]
        assert "run-earlier" in detail
        assert "Run Now" in detail

    def test_repeat_skips_of_the_same_content_overwrite_one_row(self):
        """Deterministic in the fingerprint, so dragging the same file in ten
        times does not bury the run history under ten skip rows."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(), None)
        handler.lambda_handler(_sqs_event(), None)

        run_ids = {call.kwargs["Item"]["run_id"] for call in executions.put_item.call_args_list}
        assert len(run_ids) == 1

    def test_a_failed_skip_record_does_not_turn_the_skip_into_an_error(self):
        """Best-effort: the skip has already been decided."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)
        executions.put_item.side_effect = RuntimeError("throttled")

        assert handler.lambda_handler(_sqs_event(), None)["statusCode"] == 200
        sfn.start_execution.assert_not_called()


class TestSkipIsDiagnosable:
    """p64 R5.1/R5.2: a skip explains itself well enough to be investigated.

    The defect this covers is not a wrong skip — the fingerprint is content-based
    and a false positive is not reachable by accident. It is that a *suspected*
    false positive could not be checked: the record named the earlier ``run_id``
    and nothing else, so neither the file that had been matched nor the size and
    ETag the fingerprint was computed from were recoverable without recomputing a
    digest. ``record_processed_file`` persists that pre-image for exactly this
    purpose (``processed_file_ledger.py:202-211``) and the trigger discarded it.

    Expectations here come from R5.1/R5.2, not from the record's current shape —
    the current shape is the defect.

    Validates: p64 Requirements 5.1, 5.2
    """

    def _skip_item(self, handler, executions):
        return executions.put_item.call_args.kwargs["Item"]

    def test_record_carries_the_matched_rows_pre_image(self):
        """R5.1: the matched row's own size and ETag, under ``previous_`` names."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, content_size=4096, content_etag=_ETAG)

        handler.lambda_handler(_sqs_event(size=4096, etag=_ETAG), None)

        item = self._skip_item(handler, executions)
        assert item["previous_content_size"] == 4096
        assert item["previous_content_etag"] == _ETAG

    def test_record_carries_the_matched_rows_source_key(self):
        """R5.2: which file was already processed, not only which run."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, source_key="orders/january/first.csv")

        handler.lambda_handler(_sqs_event(key="orders/february/second.csv"), None)

        item = self._skip_item(handler, executions)
        assert item["previous_source_key"] == "orders/january/first.csv"
        # The rejected object keeps the unprefixed names, so the two are
        # distinguishable rather than one overwriting the other.
        assert item["source_key"] == "orders/february/second.csv"

    def test_record_carries_the_rejected_objects_own_pre_image(self):
        """R5.1: "alongside the rejected object's own size and ETag".

        Load-bearing when the matched row predates the pre-image attributes: the
        operator can still see what THIS submission hashed to, which is otherwise
        unrecoverable from the record.
        """
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(size=2048, etag=_ETAG), None)

        item = self._skip_item(handler, executions)
        assert item["content_size"] == 2048
        assert item["content_etag"] == _ETAG

    def test_the_two_etags_on_the_record_are_comparably_normalised(self):
        """The manual path's ETag is QUOTED by ``list_objects_v2``; the ledger
        stores it bare. Storing both raw would make an identical pair look
        different, which is precisely the misreading R5 exists to prevent."""
        sfn = _idle_sfn()
        s3 = _s3_with_one_object(size=1024, etag=f'"{_ETAG}"')
        handler, _locks, executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed, content_etag=_ETAG)

        handler.lambda_handler({"trigger": "manual", "pipeline_id": "orders"}, None)

        item = self._skip_item(handler, executions)
        assert item["content_etag"] == item["previous_content_etag"] == _ETAG

    def test_skip_detail_names_the_previously_processed_file(self):
        """R5.2, on the attribute the UI actually renders."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, run_id="run-earlier", source_key="orders/january/first.csv")

        handler.lambda_handler(_sqs_event(), None)

        detail = self._skip_item(handler, executions)["skip_detail"]
        assert "first.csv" in detail
        assert "run-earlier" in detail

    def test_skip_detail_shows_both_pre_images(self):
        """Both sides, so "this is a duplicate of that" is checkable in the row."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, content_size=4096, content_etag=_ETAG)

        handler.lambda_handler(_sqs_event(size=4096, etag=_ETAG), None)

        detail = self._skip_item(handler, executions)["skip_detail"]
        assert "4096 bytes" in detail
        assert _ETAG in detail
        # What the match was made on, so nobody re-reads it as a schema or
        # header comparison — the mechanism the original report assumed.
        assert "size and ETag" in detail

    def test_a_matched_row_with_no_pre_image_omits_the_attributes(self):
        """A row written before p30 added the pre-image has nothing to show.

        Writing ``None`` would render as a value in the UI and read as "size
        unknown-but-measured" rather than "this row is older than the attribute".
        """
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed, source_key=None, content_size=None, content_etag=None)

        handler.lambda_handler(_sqs_event(), None)

        item = self._skip_item(handler, executions)
        assert "previous_content_size" not in item
        assert "previous_content_etag" not in item
        assert "previous_source_key" not in item
        # Still explains itself with what it does have.
        assert "run-2026-08-02-11-00-00-abcd1234" in item["skip_detail"]
        assert "None" not in item["skip_detail"]

    def test_the_record_id_shape_is_unchanged(self):
        """``LGR-21a1b5b-4``: the ``skipped-`` prefix and fingerprint truncation
        are the deliberate shape for this path — deterministic so re-uploads
        overwrite one row. Threading extra attributes must not touch it."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(size=1024, etag=_ETAG), None)

        fingerprint = handler.content_fingerprint(1024, _ETAG)
        assert self._skip_item(handler, executions)["run_id"] == f"skipped-{fingerprint[:32]}"

    def test_started_at_is_still_written(self):
        """``LGR-7d19eca-2``: ``RunsByTimeIndex`` is SPARSE, so a row with no
        ``started_at`` is absent from run history silently. Asserted here as well
        as in the tree-wide AST sweep because this is the write being reshaped."""
        sfn = _idle_sfn()
        handler, _locks, executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(), None)

        assert self._skip_item(handler, executions)["started_at"]

    def test_the_ledger_read_is_still_a_single_get(self):
        """do-NOT 4's neighbour: diagnosability is threaded off the row already in
        hand, never bought with a second read or an S3 ``head_object``."""
        sfn = _idle_sfn()
        handler, _locks, _executions, processed = _load_handler(sfn)
        _ledger_hit(processed)

        handler.lambda_handler(_sqs_event(), None)

        processed.get_item.assert_called_once()


class TestManualPath:
    """Manual Run Now — the one path with a human who can acknowledge (R9.4)."""

    def test_duplicate_returns_409_and_a_skip_reason(self):
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "orders"}, None)

        assert result["statusCode"] == 409
        assert result["skip_reason"] == "duplicate_content"
        assert result["skipped"] is True
        sfn.start_execution.assert_not_called()
        locks.put_item.assert_not_called()

    def test_fingerprint_uses_the_quoted_listing_etag(self):
        """boto3 quotes the ETag; the trigger must still land on the same
        fingerprint the bare event form produces."""
        sfn = _idle_sfn()
        s3 = _s3_with_one_object(size=4096, etag=f'"{_ETAG}"')
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        handler.lambda_handler({"trigger": "manual", "pipeline_id": "orders"}, None)

        assert processed.get_item.call_args.kwargs["Key"]["fingerprint"] == handler.content_fingerprint(4096, _ETAG)

    @pytest.mark.parametrize("ack", [True, "true", "True"])
    def test_explicit_acknowledgement_overrides_the_check(self, ack):
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        result = handler.lambda_handler(
            {"trigger": "manual", "pipeline_id": "orders", "acknowledge_duplicate": ack}, None
        )

        assert result["statusCode"] == 200
        assert sfn.start_execution.call_count == 1
        processed.get_item.assert_not_called()

    @pytest.mark.parametrize("ack", [False, "false", "", None, "yes"])
    def test_a_non_affirmative_acknowledgement_does_not_override(self, ack):
        """R9.4 requires the override to be EXPLICIT — anything else must still
        be blocked, or the most common path would bypass the ledger for free."""
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        result = handler.lambda_handler(
            {"trigger": "manual", "pipeline_id": "orders", "acknowledge_duplicate": ack}, None
        )

        assert result["statusCode"] == 409

    def test_unseen_content_starts_normally(self):
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        processed.get_item.return_value = {}

        result = handler.lambda_handler({"trigger": "manual", "pipeline_id": "orders"}, None)

        assert result["statusCode"] == 200
        assert sfn.start_execution.call_count == 1


class TestScheduledPath:
    """Scheduled S3_UPLOAD — where duplicate ingestion is the DEFAULT, because a
    schedule with no new file re-picks the same newest object every time."""

    def test_duplicate_returns_200_so_eventbridge_does_not_retry(self):
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        result = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "orders"}, None)

        assert result["statusCode"] == 200
        assert result["skipped"] is True
        assert result["skip_reason"] == "duplicate_content"
        sfn.start_execution.assert_not_called()
        locks.put_item.assert_not_called()

    def test_scheduled_has_no_override(self):
        """No human is present to acknowledge anything; R9.4 scopes the override
        to Run Now."""
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        _ledger_hit(processed)

        result = handler.lambda_handler(
            {"trigger": "scheduled", "pipeline_id": "orders", "acknowledge_duplicate": True}, None
        )

        assert result["skipped"] is True
        sfn.start_execution.assert_not_called()

    def test_unseen_content_starts_normally(self):
        sfn = _idle_sfn()
        s3 = _s3_with_one_object()
        handler, _locks, _executions, processed = _load_handler(sfn, mock_s3=s3)
        processed.get_item.return_value = {}

        result = handler.lambda_handler({"trigger": "scheduled", "pipeline_id": "orders"}, None)

        assert result["statusCode"] == 200
        assert sfn.start_execution.call_count == 1


class TestJdbcPathIsUnaffected:
    """A JDBC source has no file, so the ledger is not applicable and must not
    misfire. Incremental-read idempotency belongs to the watermark table."""

    @pytest.mark.parametrize("trigger", ["manual", "scheduled"])
    def test_jdbc_never_reads_the_ledger(self, trigger):
        sfn = _idle_sfn()
        handler, locks, _executions, processed = _load_handler(sfn)
        # A hit is armed on purpose: if the JDBC path consulted the ledger at
        # all, it would find one and wrongly skip.
        _ledger_hit(processed)

        result = handler.lambda_handler({"trigger": trigger, "pipeline_id": "orders", "source_type": "JDBC"}, None)

        assert result["statusCode"] == 200
        processed.get_item.assert_not_called()
        assert sfn.start_execution.call_count == 1
        assert locks.put_item.call_count == 1


_PURE_MODULE: list = []


def _composer_module():
    """Load pipeline_trigger once for the pure-helper tests below.

    The four helpers under test are pure string/number work: they need the module
    in the namespace and nothing else, no per-test table mocks. Caching the load
    also stops the property test re-exec'ing the module once per example. The
    autouse fixture stops this load's boto3 patchers when the first test using it
    ends, which is harmless precisely because none of these helpers touch boto3.
    """
    if not _PURE_MODULE:
        _PURE_MODULE.append(_load_handler(_idle_sfn())[0])
    return _PURE_MODULE[0]


class TestComposerHelpers:
    """The three pure helpers ``_duplicate_skip_detail`` is built from.

    1e.1 covered the composer only end-to-end through ``lambda_handler``, which
    reaches exactly one clause combination per test. The clauses drop
    *independently*, so the space that matters is combinatorial and is far cheaper
    to cover here at the seam than through the handler. This class tests the
    pieces; ``TestSkipDetailComposition`` tests their assembly.

    **Validates: Requirements 5.1, 5.2**
    """

    def test_file_name_of_strips_the_prefix(self):
        assert _composer_module()._file_name_of("orders/january/first.csv") == "first.csv"

    def test_file_name_of_returns_an_unprefixed_key_unchanged(self):
        """A key at the bucket root has no ``/`` to split on."""
        assert _composer_module()._file_name_of("first.csv") == "first.csv"

    def test_file_name_of_an_empty_key_is_empty(self):
        """Feeds the ``origin`` clause's own omission — an empty basename must not
        render as ``from ''``."""
        assert _composer_module()._file_name_of("") == ""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (1024, 1024),  # EventBridge detail: JSON number
            ("1024", 1024),  # numeric string
            (Decimal(1024), 1024),  # read back off DynamoDB
            (0, 0),  # a zero-byte object is a REAL size, not an absence
            (None, None),
            (True, None),  # bool is an int subclass; a True size is a bug
            (False, None),
            ("not-a-number", None),
            ([], None),
        ],
    )
    def test_as_int_normalises_the_three_wire_forms_and_rejects_the_rest(self, value, expected):
        """The same three-way normalisation ``content_fingerprint`` performs, so a
        size rendered in ``skip_detail`` matches the one the digest was taken over.

        ``0`` is the load-bearing case: it is falsy, so a ``if size:`` guard
        anywhere downstream would silently drop a zero-byte object's size and
        report it as unknown.
        """
        result = _composer_module()._as_int(value)
        # `is None` rather than `== expected` for the rejected cases: `False == 0`
        # is true, so a bool leaking through as a size would pass an `==` check.
        if expected is None:
            assert result is None
        else:
            assert result == expected

    def test_pre_image_phrase_renders_both_halves(self):
        assert _composer_module()._pre_image_phrase(1024, _ETAG) == f"1024 bytes, ETag {_ETAG}"

    def test_pre_image_phrase_renders_size_alone(self):
        assert _composer_module()._pre_image_phrase(1024, "") == "1024 bytes"

    def test_pre_image_phrase_renders_etag_alone(self):
        assert _composer_module()._pre_image_phrase(None, _ETAG) == f"ETag {_ETAG}"

    def test_pre_image_phrase_is_empty_when_neither_half_is_known(self):
        """The "never render None" contract at its source: an older ledger row has
        no pre-image, and the caller keys the whole clause off this empty string."""
        assert _composer_module()._pre_image_phrase(None, "") == ""

    def test_pre_image_phrase_keeps_a_zero_size(self):
        """Consistent with ``_as_int(0) == 0``: the guard is ``is not None``, not
        truthiness, so a zero-byte object still reports its size."""
        assert _composer_module()._pre_image_phrase(0, "") == "0 bytes"


class TestSkipDetailComposition:
    """``_duplicate_skip_detail``'s clause matrix, asserted at the seam.

    Three of the sentence's clauses are conditional — the file origin, the matched
    row's pre-image, and this submission's — and each drops when its input is
    unknown. Two are unconditional. 1e.1 asserted fragments of the assembled
    string through the handler; this pins which clauses appear for which inputs,
    which is the part a future edit is most likely to get wrong.

    Expectations come from R5.1/R5.2 and the composer's documented contract, not
    from the current output.

    **Validates: Requirements 5.1, 5.2**
    """

    _FULL = {
        "previous_run_id": "run-earlier",
        "previous_source_key": "orders/january/first.csv",
        "previous_content_size": 4096,
        "previous_content_etag": _ETAG,
        "size": 4096,
        "etag": _ETAG,
    }

    def _detail(self, **overrides):
        kwargs = {**self._FULL, **overrides}
        mod = _composer_module()
        return mod._duplicate_skip_detail(
            kwargs["previous_run_id"],
            kwargs["previous_source_key"],
            kwargs["previous_content_size"],
            kwargs["previous_content_etag"],
            kwargs["size"],
            kwargs["etag"],
        )

    def test_all_five_clauses_render_when_everything_is_known(self):
        detail = self._detail()
        assert "run 'run-earlier'" in detail
        assert "from 'first.csv'" in detail
        assert "That content was 4096 bytes" in detail
        assert "This submission is 4096 bytes" in detail
        assert "size and ETag" in detail
        assert "Run Now" in detail

    def test_the_origin_clause_drops_when_the_matched_row_has_no_source_key(self):
        detail = self._detail(previous_source_key="")
        assert "from '" not in detail
        # The sentence still identifies the run, so it is not left contentless.
        assert "run 'run-earlier'" in detail

    def test_the_matched_rows_pre_image_clause_drops_when_it_has_none(self):
        """A row written before p30 added the pre-image attributes."""
        detail = self._detail(previous_content_size=None, previous_content_etag="")
        assert "That content was" not in detail
        # This submission's own pre-image is still shown — the case that makes it
        # load-bearing, since it is otherwise unrecoverable from the record.
        assert "This submission is 4096 bytes" in detail

    def test_this_submissions_pre_image_clause_drops_when_unknown(self):
        detail = self._detail(size=None, etag="")
        assert "This submission is" not in detail
        assert "That content was 4096 bytes" in detail

    def test_both_pre_image_clauses_drop_together(self):
        detail = self._detail(previous_content_size=None, previous_content_etag="", size=None, etag="")
        assert "That content was" not in detail
        assert "This submission is" not in detail

    def test_an_unknown_run_id_renders_as_unknown_rather_than_empty(self):
        """``previous_run_id`` is the one input with a substituted fallback rather
        than an omission, because the sentence's first clause is unconditional."""
        detail = self._detail(previous_run_id="")
        assert "run 'unknown'" in detail

    def test_the_two_unconditional_clauses_survive_the_emptiest_input(self):
        """What the match was made on, and how to override it, are the two things
        an operator needs even when the record carries nothing else."""
        detail = self._detail(
            previous_run_id="",
            previous_source_key="",
            previous_content_size=None,
            previous_content_etag="",
            size=None,
            etag="",
        )
        assert "The match is on size and ETag only." in detail
        assert "Run Now" in detail
        assert "None" not in detail

    def test_the_composition_helper_is_the_shared_one_not_a_local_copy(self):
        """R5.3's second half: the fingerprint stays defined ONCE, in ``shared/``.

        Asserted by *provenance* rather than by object identity. Identity is the
        obvious instrument and the wrong one: this harness loads the lambda via
        ``spec_from_file_location`` and the shared module gets imported twice
        under two paths, so ``is`` compares two distinct function objects wrapping
        identical source and fails for a reason unrelated to the requirement.
        ``__module__`` plus the defining file is what actually discriminates —
        a copy pasted into ``lambda_function.py`` would report itself there.

        do-NOT 4's other half (the composition itself) is pinned by
        ``tests/unit/shared/test_processed_file_ledger.py``, which this task
        leaves untouched.
        """
        shared_ledger = os.path.realpath(os.path.join(LAMBDA_DIR, "..", "..", "shared", "processed_file_ledger.py"))
        mod = _composer_module()

        for helper in (mod.content_fingerprint, mod.normalize_etag):
            assert helper.__module__ == "processed_file_ledger"
            assert os.path.realpath(helper.__code__.co_filename) == shared_ledger


# A size is an int off the event, a numeric string, or a Decimal off DynamoDB;
# an unknown one is None. An ETag reaching the composer has already been through
# `normalize_etag`, so it is bare lowercase hex or "".
_sizes = st.one_of(st.none(), st.integers(min_value=0, max_value=5 * 1024**4))
_etags = st.one_of(st.just(""), st.text(alphabet="0123456789abcdef", min_size=32, max_size=32))
_keys = st.one_of(
    st.just(""),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789/.-_", min_size=1, max_size=40),
)
# A run id is minted by the system, not supplied by a caller: `run-<timestamp>-<hex>`
# (file_validation `generate_run_id`), `run-<hex>` (agentcore payload defaults) or
# `run-<timestamp>-skip-<reason>`. Every form is lowercase alphanumerics, hyphens and
# underscores. Arbitrary text generated `"\n"` here once, which no minted id can
# contain, and failed the one-line assertion for an input the composer never receives.
_run_ids = st.one_of(
    st.just(""),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=40),
)


@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    previous_run_id=_run_ids,
    previous_source_key=_keys,
    previous_content_size=_sizes,
    previous_content_etag=_etags,
    size=_sizes,
    etag=_etags,
)
def test_skip_detail_never_renders_a_none_for_any_input(
    previous_run_id,
    previous_source_key,
    previous_content_size,
    previous_content_etag,
    size,
    etag,
):
    """The composer's central promise, over the whole input space rather than one
    example of it.

    1e.1 asserted ``"None" not in skip_detail`` for the single case where all
    three of the matched row's attributes are absent. The clauses drop
    independently, so that is one corner of the space; a rendered ``None`` would
    read to an operator as "size unknown-but-measured" rather than "this row
    predates the attribute", which is the misreading R5.1 exists to prevent.

    The two unconditional clauses are asserted here too: whatever else is unknown,
    the sentence must still say what the match was made on and how to override it.

    **Validates: Requirements 5.1, 5.2**
    """
    detail = _composer_module()._duplicate_skip_detail(
        previous_run_id,
        previous_source_key,
        previous_content_size,
        previous_content_etag,
        size,
        etag,
    )

    assert "None" not in detail
    assert "The match is on size and ETag only." in detail
    assert "Run Now" in detail
    # One line: the attribute is rendered in a UI table cell.
    assert "\n" not in detail
