# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Lost_File record: its shape, its key, and the two agreements it rests on.

p54 R10.1/R10.3, arm (d). ``queue_loss_recorder`` is the producer that
``ExecutionsPage.tsx:576``'s ``failed_dlq`` branch has been waiting for since it
was written — one occurrence repo-wide, zero producers until now.

**Three things could break this mechanism silently, and each gets its own test
here rather than a comment.**

1. **The key.** The handler finds the file's row with no lookup because the row
   id is content-addressed on ``source_key`` — the same
   ``queued-{sha256(source_key)[:16]}`` that
   ``pipeline_trigger._track_queued_file`` writes. The two spellings live in two
   Lambda packages that cannot import each other, so if one drifts the recorder
   writes a row nobody reads and the file still looks queued. The agreement is
   asserted by reading the sibling's source, which is this repo's technique for a
   value that must agree across a boundary an import cannot cross
   (``tests/contracts/test_approval_window_cross_language.py``).

2. **The attribute name and value.** The lost fact rides on ``queue_outcome``
   rather than on ``status``, because p54 do-NOT 19 / ``LGR-21a1b5b-2`` refuse a
   fourth executions ``status``. Its only consumer is
   ``pipelines_runs._split_derive_and_filter``'s step-1 comprehension, which
   matches the value EXACTLY and neither normalises nor case-folds it
   (``LGR-ad78946-1``). A drift is therefore not a degraded render — it silently
   reverts to publishing ``"queued"`` for a file that is gone. Asserted against
   the consumer's own source.

3. **The row must be indexable.** ``RunsByTimeIndex`` is
   ``(pipeline_id, started_at)`` and DynamoDB indexes only items carrying BOTH
   key attributes, so a row without ``started_at`` is absent from it **with no
   error** — invariant ``LGR-7d19eca-2``. A record nobody can read is not a
   record, which is exactly what R10.3 forbids.

**Why ``put_item`` and not ``update_item``, asserted rather than assumed.**
Design §4.8 candidate 1 proposed annotating the existing Queued_Placeholder, on
the arithmetic that ~25 min of redelivery budget against a 60-min
``QUEUED_RECORD_TTL_SECONDS`` left ~35 min of slack. Task 7.7 raised
``maxReceiveCount`` to 1000, so the budget is now 8 h 20 min against that same
1 h TTL and **the ordering reversed**: the placeholder has normally expired by
the time its message reaches the DLQ. An ``update_item`` against a missing item
CREATES it, so annotation would now mint ``{pipeline_id, run_id, queue_outcome}``
with no ``status`` and no ``started_at`` — failing (2) and (3) at once. The
handler is given ``dynamodb:PutItem`` and nothing else in
``orchestration-stack.ts``, and the test below asserts the module contains no
``update_item``/``delete_item`` call, so the mistake is unmakeable from two
directions.

Validates: p54 Requirements 10.1, 10.3
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_TESTS_ROOT = Path(__file__).resolve().parents[3]
_PROJECT_ROOT = _TESTS_ROOT.parent
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_RECORDER_DIR = _LAMBDA_ROOT / "agentic_data_ingestion_queue_loss_recorder"
_TRIGGER_SOURCE = _LAMBDA_ROOT / "agentic_data_ingestion_pipeline_trigger" / "lambda_function.py"
_CONSUMER_SOURCE = _LAMBDA_ROOT / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines_runs.py"

_EXECUTIONS_TABLE = "test-executions"
_ENV = {
    "EXECUTIONS_TABLE": _EXECUTIONS_TABLE,
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

_BUCKET = "src-bucket"
_SOURCE_KEY = "mynewtesting/customer_data_7.csv"
_PIPELINE_PREFIX = "mynewtesting"
_FILE_NAME = "customer_data_7.csv"

#: The eight attributes ``_track_queued_file`` writes. The recorder's row must be
#: a SUPERSET of these — every reader of a queued row keeps working unchanged,
#: which is what makes do-NOT 19's "no fourth status" satisfiable at all.
_PLACEHOLDER_ATTRIBUTES = frozenset(
    {
        "pipeline_id",
        "run_id",
        "status",
        "file_name",
        "source_key",
        "started_at",
        "current_step",
        "ttl",
    }
)


def _eventbridge_body(*, bucket: str = _BUCKET, key: str = _SOURCE_KEY) -> str:
    """An S3 Object Created body as it arrives on the queue, and then the DLQ.

    A dead-lettered message carries the SAME body the trigger read — that is the
    fact the whole no-lookup design rests on, so the fixture is shaped like
    ``_process_sqs_record``'s input rather than invented.
    """
    return json.dumps(
        {
            "version": "0",
            "source": "aws.s3",
            "detail-type": "Object Created",
            "detail": {
                "bucket": {"name": bucket},
                "object": {"key": key, "size": 1024, "etag": "d41d8cd98f00b204e9800998ecf8427e"},
            },
        }
    )


def _sqs_record(body: str, *, message_id: str = "msg-1", receive_count: str = "1000") -> dict:
    return {
        "messageId": message_id,
        "body": body,
        "attributes": {"ApproximateReceiveCount": receive_count},
    }


def _load_recorder():
    """Load the handler with a mock executions table.

    ``importlib.util.spec_from_file_location`` after dropping any cached
    ``lambda_function`` — ``LGR-b7d5490-1``: several Lambdas share that module
    name and a plain import cross-contaminates. Same loader shape as this
    directory's sibling suites.
    """
    executions = MagicMock()
    executions.put_item.return_value = {}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = executions

    with (
        patch.dict(os.environ, _ENV),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.resource", return_value=dynamodb),
    ):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", str(_RECORDER_DIR / "lambda_function.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.executions_table = executions
    return mod, executions


@pytest.fixture
def recorder():
    mod, executions = _load_recorder()
    with patch.dict(os.environ, _ENV):
        yield mod, executions


def _written_item(executions: MagicMock) -> dict:
    assert executions.put_item.call_count == 1, (
        f"expected exactly one row written, saw {executions.put_item.call_count}"
    )
    return executions.put_item.call_args.kwargs["Item"]


class TestTheRecordIsAWholeQueuedRowPlusTheLoss:
    """R10.3's record: what it carries, and what it deliberately does not change."""

    def test_it_writes_every_attribute_a_queued_placeholder_carries(self, recorder):
        """A superset of the eight, so no reader of a queued row learns a new shape."""
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        item = _written_item(executions)
        missing = _PLACEHOLDER_ATTRIBUTES - set(item)
        assert not missing, (
            f"the Lost_File row is missing {sorted(missing)} — it must be a complete "
            f"queued row, not a partial annotation, because the placeholder has usually "
            f"expired by the time its message reaches the DLQ"
        )

    def test_status_stays_QUEUED_and_the_loss_rides_on_its_own_attribute(self, recorder):
        """do-NOT 19 / ``LGR-21a1b5b-2``: no fourth executions ``status``.

        Its reasoning, verbatim, is why: the executions table's Stream drives the
        WebSocket broadcast, ``StatusIndex`` is partitioned on ``status``, and
        *"the run-history read path treats every non-``QUEUED`` row as a run"*, so
        a novel status *"would render as a phantom run unless three separate
        readers learned to ignore it."*
        """
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        item = _written_item(executions)
        assert item["status"] == "QUEUED"
        assert item[mod.QUEUE_OUTCOME_ATTRIBUTE] == mod.QUEUE_OUTCOME_LOST

    def test_it_carries_started_at_so_the_row_is_indexable(self, recorder):
        """Without it the record is invisible and R10.3 is unmet — ``LGR-7d19eca-2``.

        ``RunsByTimeIndex`` is ``(pipeline_id, started_at)`` and a GSI indexes only
        items carrying BOTH key attributes, so a row without this is absent from
        the index silently. Non-empty is the assertion, not a particular value:
        the original queue time is not recoverable from a DLQ message, and
        inventing one would put a fabricated value on the index's sort key.
        """
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        item = _written_item(executions)
        assert item["started_at"], "an executions row with no started_at is never indexed"

    def test_it_names_the_file_and_the_reason(self, recorder):
        """R10.3 in as many words: *the record SHALL name the file and the reason*."""
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        item = _written_item(executions)
        assert item["file_name"] == _FILE_NAME
        assert item["source_key"] == _SOURCE_KEY
        assert "dead-letter queue" in item["lost_reason"]
        assert "redelivery budget" in item["lost_reason"]

    def test_the_reason_does_not_interpolate_the_receive_count(self, recorder):
        """A number that moves must not be dressed as an explanation.

        The receive count observed on the DLQ is incremented by anyone who has
        since received the message there — including a deliberately
        non-destructive operator read like p54 task 7.1's. SQS records only THAT
        the budget was exhausted, never why, so the sentence states the mechanism
        and the count is logged instead.
        """
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body(), receive_count="997")]}, None)
        assert "997" not in _written_item(executions)["lost_reason"]

    def test_the_ttl_is_the_lost_file_constant_and_not_the_queued_one(self, recorder):
        """R10.3's *"readable for longer than the window in which the loss occurred"*.

        ``QUEUED_RECORD_TTL_SECONDS`` (1 h) is now SHORTER than the 8 h 20 min
        redelivery budget, so inheriting it would expire the record before — or
        barely after — the loss it describes. p54 R8.13 / do-NOT 16 require a
        fourth constant rather than a coupling, and this asserts the row uses it.
        """
        sys.path.insert(0, str(_PROJECT_ROOT / "shared"))
        from config import LOST_FILE_RECORD_TTL_SECONDS, QUEUED_RECORD_TTL_SECONDS

        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        ttl = _written_item(executions)["ttl"]

        assert LOST_FILE_RECORD_TTL_SECONDS > QUEUED_RECORD_TTL_SECONDS, (
            "a Lost_File record must outlive a queued tracker; if these ever cross, "
            "the record expires before anyone can read it"
        )
        # A window rather than an equality: `int(time.time())` moves between the
        # handler's call and this assertion.
        import time as _time

        expected = int(_time.time()) + LOST_FILE_RECORD_TTL_SECONDS
        assert abs(ttl - expected) <= 60, f"ttl {ttl} is not LOST_FILE_RECORD_TTL_SECONDS from now ({expected})"


class TestTheKeyAgreesWithTheTriggerThatWroteThePlaceholder:
    """Agreement 1 — the content-addressed key, across a package boundary."""

    @pytest.mark.parametrize(
        ("source_key", "expected_prefix"),
        [
            ("mynewtesting/customer_data_7.csv", "mynewtesting"),
            ("deep/nested/prefix/file.parquet", "deep/nested/prefix"),
            # No slash at all: the trigger's `_pipeline_prefix` returns the whole
            # key, and `_track_queued_file`'s file_name does too. Both spellings
            # are `if "/" in ...`-guarded, so this is the branch that would drift.
            ("bare-object.csv", "bare-object.csv"),
        ],
    )
    def test_the_pipeline_prefix_matches_the_triggers_spelling(self, recorder, source_key, expected_prefix):
        mod, _ = recorder
        assert mod.pipeline_prefix_of(source_key) == expected_prefix

    def test_the_run_id_is_the_content_addressed_queued_id(self, recorder):
        """Computed independently here, not by calling the code under test."""
        mod, _ = recorder
        expected = f"queued-{hashlib.sha256(_SOURCE_KEY.encode()).hexdigest()[:16]}"
        key = mod.queued_row_key(_PIPELINE_PREFIX, _SOURCE_KEY)
        assert key == {"pipeline_id": _PIPELINE_PREFIX, "run_id": expected}

    def test_the_trigger_still_derives_the_key_the_same_way(self):
        """Read the sibling's source, because an import cannot cross the boundary.

        If ``_track_queued_file`` changes its derivation, the recorder writes a
        row no reader looks for and the file goes on displaying as queued. Nothing
        else in the tree would notice, which is why this reads the literal.
        """
        source = _TRIGGER_SOURCE.read_text(encoding="utf-8")
        derivation = 'f"queued-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}"'
        occurrences = source.count(derivation)
        assert occurrences == 2, (
            f"expected the queued-row id derivation in pipeline_trigger twice "
            f"(_track_queued_file and _remove_queued_file), found {occurrences}. "
            f"queue_loss_recorder.queued_row_key duplicates this expression on "
            f"purpose — the two packages cannot import each other — so a change "
            f"there must be mirrored there."
        )

    def test_the_written_row_uses_that_key(self, recorder):
        mod, executions = recorder
        mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body())]}, None)
        item = _written_item(executions)
        assert item["pipeline_id"] == _PIPELINE_PREFIX
        assert item["run_id"] == f"queued-{hashlib.sha256(_SOURCE_KEY.encode()).hexdigest()[:16]}"

    def test_two_deliveries_of_one_file_address_the_same_row(self, recorder):
        """Idempotent by construction: the key is a function of the content, and
        the write is an unconditional ``put_item``. SQS is at-least-once, and the
        DLQ mapping reports per-record failures, so a redelivery after a partial
        batch must not produce a second Lost_File row for one file."""
        mod, executions = recorder
        mod.lambda_handler(
            {
                "Records": [
                    _sqs_record(_eventbridge_body(), message_id="msg-1"),
                    _sqs_record(_eventbridge_body(), message_id="msg-2"),
                ]
            },
            None,
        )
        keys = [
            (c.kwargs["Item"]["pipeline_id"], c.kwargs["Item"]["run_id"]) for c in executions.put_item.call_args_list
        ]
        assert len(set(keys)) == 1, f"two deliveries wrote two different rows: {keys}"


class TestTheOutcomeAttributeAgreesWithItsOnlyConsumer:
    """Agreement 2 — the attribute name and value, across the same boundary."""

    def test_the_consumer_reads_exactly_these_two_spellings(self, recorder):
        """``_split_derive_and_filter``'s constants are the contract.

        Asserted by reading ``pipelines_runs.py`` rather than importing it: the
        dashboard API Lambda pulls in the whole handler package. A drift here does
        not degrade the render — it silently republishes ``"queued"`` for a file
        that is gone, because that consumer matches the value exactly and
        deliberately does not normalise or case-fold it (``LGR-ad78946-1``).
        """
        mod, _ = recorder
        source = _CONSUMER_SOURCE.read_text(encoding="utf-8")

        attribute = re.search(r'^_QUEUE_OUTCOME_ATTRIBUTE = "([^"]+)"', source, re.MULTILINE)
        lost = re.search(r'^_QUEUE_OUTCOME_LOST = "([^"]+)"', source, re.MULTILINE)
        assert attribute is not None and lost is not None, (
            "the queue-outcome constants are no longer declared at module level in "
            "dashboard_api/handlers/pipelines_runs.py — the recorder's producer side "
            "can no longer be checked against its consumer"
        )
        assert attribute.group(1) == mod.QUEUE_OUTCOME_ATTRIBUTE
        assert lost.group(1) == mod.QUEUE_OUTCOME_LOST


class TestTheFailurePosture:
    """A permanent failure is dropped; a transient one is returned."""

    @pytest.mark.parametrize(
        ("body", "why"),
        [
            ("{not json", "a body that will never parse"),
            (json.dumps({"detail": {}}), "an EventBridge detail with no bucket/key"),
            (json.dumps({"detail": {"bucket": {"name": _BUCKET}}}), "a bucket with no object key"),
            (json.dumps({"detail": {"object": {"key": _SOURCE_KEY}}}), "an object with no bucket"),
            (json.dumps(["not", "an", "object"]), "a JSON body that is not an object"),
        ],
    )
    def test_an_unusable_body_writes_nothing_and_is_reported_as_success(self, recorder, body, why):
        """Permanent, and there is no DLQ behind this DLQ.

        Reporting it as a failure would redeliver it until the 14-day retention
        expired, burning invocations to no effect — and there is no ``source_key``
        to write a record under anyway. Same judgement
        ``_process_sqs_record``'s docstring makes about a deterministic failure.
        """
        mod, executions = recorder
        result = mod.lambda_handler({"Records": [_sqs_record(body)]}, None)
        assert executions.put_item.call_count == 0, f"{why} produced a write"
        assert result == {"batchItemFailures": []}, f"{why} was reported as a failure"

    def test_a_dynamodb_failure_is_reported_so_the_message_returns(self, recorder):
        """Transient: the record did not land, and that is the one outcome this
        Lambda exists to prevent. Reporting it returns the message so the next
        delivery can write the record. Inert without ``ReportBatchItemFailures``
        on the event source mapping, which ``orchestration-stack.test.ts``
        asserts."""
        mod, executions = recorder
        executions.put_item.side_effect = RuntimeError("throughput exceeded")
        result = mod.lambda_handler({"Records": [_sqs_record(_eventbridge_body(), message_id="msg-7")]}, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-7"}]}

    def test_one_bad_record_does_not_take_its_siblings_with_it(self, recorder):
        """Per-record reporting, not per-batch."""
        mod, executions = recorder

        def _fail_second(**kwargs):
            if kwargs["Item"]["source_key"].endswith("second.csv"):
                raise RuntimeError("throughput exceeded")
            return {}

        executions.put_item.side_effect = _fail_second
        result = mod.lambda_handler(
            {
                "Records": [
                    _sqs_record(_eventbridge_body(key="p/first.csv"), message_id="msg-a"),
                    _sqs_record(_eventbridge_body(key="p/second.csv"), message_id="msg-b"),
                    _sqs_record(_eventbridge_body(key="p/third.csv"), message_id="msg-c"),
                ]
            },
            None,
        )
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-b"}]}
        assert executions.put_item.call_count == 3

    def test_an_empty_event_is_a_no_op(self, recorder):
        mod, executions = recorder
        assert mod.lambda_handler({}, None) == {"batchItemFailures": []}
        assert executions.put_item.call_count == 0


class TestTheHandlerCannotCreateAPhantomRow:
    """The structural guard, from the code side.

    ``orchestration-stack.ts`` grants ``dynamodb:PutItem`` and nothing else, and
    ``orchestration-stack.test.ts`` asserts that. This is the other direction: the
    module contains no update or delete call, so it cannot annotate a missing item
    into ``{pipeline_id, run_id, queue_outcome}`` with no ``started_at`` — the
    unindexable row ``LGR-7d19eca-2`` describes.
    """

    def test_the_module_never_calls_update_item_or_delete_item(self):
        source = (_RECORDER_DIR / "lambda_function.py").read_text(encoding="utf-8")
        # Docstrings name both on purpose (they explain why they are absent), so
        # this checks for a CALL rather than a mention.
        for forbidden in ("update_item(", "delete_item("):
            assert forbidden not in source, (
                f"queue_loss_recorder calls {forbidden} — an update against a missing "
                f"item CREATES it, and by DLQ time the Queued_Placeholder has normally "
                f"expired (1 h TTL against an 8 h 20 min redelivery budget), so the "
                f"created row would have no status and no started_at. Write a whole "
                f"row with put_item."
            )
