# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Queue Loss Recorder — the DLQ's consumer, and R10.3's durable record.

p54 R10.1/R10.3, arm (d). A file uploaded to the source bucket rides an
EventBridge event through ``PipelineTriggerQueue`` to the pipeline trigger. When
the per-pipeline lock is held — by a run parked at a 72 h human approval gate,
say — the trigger returns that record in ``batchItemFailures`` and SQS
redelivers it. That is correct and is NOT a loss: ``LGR-21a1b5b-4`` is right
that *"an SQS rejection is a redelivery"*. What nobody wrote down is the BOUND
on it. After ``maxReceiveCount`` receives SQS moves the message to
``PipelineTriggerDLQ`` and the file is gone, and until this Lambda existed the
last thing the dashboard said about it was ``queued`` — under a footer promising
it would process after the current execution finished.

**What this function does, and the one thing it does not.** It writes the
Lost_File record. It does NOT redrive, resubmit, or start anything: p54
requirements place automatic redrive out of scope (*"R9.3/R9.4 require the loss
be visible; who presses the button is out of scope"*). Making the loss visible
is the whole job.

## The record's shape, and why it is a whole row rather than an annotation

The row it writes is the **Queued_Placeholder shape** — byte-for-byte the eight
attributes ``pipeline_trigger._track_queued_file`` writes — plus
``queue_outcome`` and two loss fields, on the SAME content-addressed key. Three
constraints force that shape and each one is load-bearing:

1. **``status`` stays ``"QUEUED"``.** p54 do-NOT 19 / ``LGR-21a1b5b-2``
   (**template / ratified**) refuse a fourth executions ``status``: the
   executions table's DynamoDB Stream drives the WebSocket broadcast, its
   ``StatusIndex`` GSI is partitioned on ``status``, and *"the run-history read
   path treats every non-``QUEUED`` row as a run"*, so a novel status *"would
   render as a phantom run unless three separate readers learned to ignore
   it."* The lost fact therefore rides on a **separate attribute**,
   ``queue_outcome``, which only ``_split_derive_and_filter``'s step-1
   comprehension reads (``dashboard_api/handlers/pipelines_runs.py``). Every
   other reader — the split, the phantom filter, the GSI, the Stream, the
   run-history pager — behaves exactly as it did before p54.

2. **``started_at`` must be present.** ``RunsByTimeIndex`` is
   ``(pipeline_id, started_at)`` and DynamoDB indexes only items carrying BOTH
   key attributes, so a row without it is absent from the index **silently** —
   invariant ``LGR-7d19eca-2``. A record nobody can read is not a record, which
   is precisely what R10.3 forbids, so this is the attribute that makes the
   whole mechanism work.

3. **``put_item``, not ``update_item``** — and this is the correction p54 task
   7.7 had to make to design §4.8's candidate 1. That candidate assumed the
   placeholder would still be there to annotate, on the arithmetic *"~25 min of
   Redelivery_Budget against a 60-min ``QUEUED_RECORD_TTL_SECONDS`` leaves ~35
   min of slack"*. Task 7.7 raised ``maxReceiveCount`` to 1000, so the budget is
   now **8 h 20 min against a 1 h TTL** and the ordering REVERSED: by the time a
   message reaches the DLQ its placeholder has normally expired. An
   ``update_item`` against a missing item CREATES it, so the annotation approach
   would now mint ``{pipeline_id, run_id, queue_outcome}`` with no ``status`` and
   no ``started_at`` — an unindexed, invisible phantom, failing constraints 1 and
   2 at once. A complete ``put_item`` is the only form that survives the raise,
   and it is idempotent by construction because the key is content-addressed.

## Failure posture

* **A body that will never parse is reported as SUCCESS.** There is no DLQ
  behind this DLQ, so raising on a permanently-malformed body would redeliver it
  until the 14-day retention expired, burning invocations to no effect. It is
  also the case with nothing to write: no ``source_key`` means no key to write
  under. It is logged at ERROR and dropped, which is the same judgement
  ``pipeline_trigger._process_sqs_record`` makes about a deterministic failure.
* **A DynamoDB write failure is reported as a FAILURE.** That one IS transient,
  the record did not land, and reporting it returns the message so the next
  delivery can try again. This is why the event source mapping declares
  ``ReportBatchItemFailures``: without it this return value is inert
  (``LGR-21a1b5b-3``'s lesson, from the trigger's own mapping).

Validates: p54 Requirements 10.1, 10.3
"""

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger

# Shared modules: /opt/python in the Lambda runtime (PowertoolsTriggerLayer in
# orchestration-stack.ts copies shared/*.py there); the relative path covers
# local imports (tests, tooling). Same preamble as the pipeline trigger.
sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# The Lost_File record's lifetime is owned by shared/config.py, not by a CDK
# environment variable — one home per behavioural value. It is a FOURTH TTL
# constant and deliberately not any of the other three (p54 R8.13 / do-NOT 16);
# in particular QUEUED_RECORD_TTL_SECONDS is now SHORTER than the redelivery
# budget, which is why this row cannot inherit it. See the constant's docstring.
from config import LOST_FILE_RECORD_TTL_SECONDS

logger = Logger(service="agentic_data_ingestion")

EXECUTIONS_TABLE = os.environ.get("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")

dynamodb = boto3.resource("dynamodb")
executions_table = dynamodb.Table(EXECUTIONS_TABLE)

#: The stored attribute carrying a queued row's outcome, and the value meaning
#: "lost". Read by exactly one consumer —
#: ``dashboard_api/handlers/pipelines_runs._split_derive_and_filter``'s step-1
#: comprehension, whose ``_QUEUE_OUTCOME_ATTRIBUTE`` / ``_QUEUE_OUTCOME_LOST``
#: these two must equal. That consumer matches the value EXACTLY and is neither
#: normalised nor case-folded (``LGR-ad78946-1``: *"normalising removes the only
#: signal that the deployed value is not the intended one"*), so a drift here is
#: not a degraded render — it is a silent reversion to publishing ``"queued"``
#: for a file that is gone. ``test_queue_loss_record.py`` asserts the two
#: spellings agree by reading the consumer's source.
QUEUE_OUTCOME_ATTRIBUTE = "queue_outcome"
QUEUE_OUTCOME_LOST = "lost_to_dlq"

#: ``current_step`` on the row. Distinct from ``_track_queued_file``'s
#: ``"queued_trigger"`` on purpose: it is the only attribute that says which
#: writer produced the row, and the published ``queued_files`` entry does not
#: carry it (do-NOT 3 fences that entry to four keys), so it costs the wire
#: nothing and is worth having in the table when reading a row by hand.
LOST_FILE_CURRENT_STEP = "queued_dlq"

#: The reason, in the operator's terms, stated once (R10.3: the record names the
#: file **and the reason**).
#:
#: It does NOT interpolate ``ApproximateReceiveCount``, and that omission is
#: deliberate rather than lazy. SQS does not record *why* a message exhausted
#: its budget — only that it did — and the receive count observed on the DLQ is
#: incremented by anyone who has since received the message there, including a
#: non-destructive operator read like p54 task 7.1's. Interpolating it would
#: dress a number that moves as an explanation that does not. The mechanism is
#: what is certain, so the mechanism is what the sentence states; the observed
#: count is logged instead.
LOST_REASON = (
    "The upload's SQS message exhausted its redelivery budget before the "
    "pipeline lock became free, so SQS moved it to the dead-letter queue and no "
    "run was started for this file."
)


def queued_row_key(pipeline_prefix: str, source_key: str) -> dict[str, str]:
    """The executions-table key for a file's queued row.

    **This must agree byte-for-byte with
    ``pipeline_trigger._track_queued_file``/``_remove_queued_file``**, which is
    what lets this handler find the right row with no lookup — the key is
    content-addressed on ``source_key``, and the DLQ message carries the same
    EventBridge body the trigger saw. The two spellings live in two Lambda
    packages that cannot import each other, so the agreement is asserted by
    reading the sibling's source rather than hoped for (see
    ``test_queue_loss_record.py``).
    """
    return {
        "pipeline_id": pipeline_prefix,
        "run_id": f"queued-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}",
    }


def pipeline_prefix_of(source_key: str) -> str:
    """The pipeline prefix of an S3 key — the sibling's ``_pipeline_prefix``."""
    parts = source_key.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else source_key


def file_name_of(source_key: str) -> str:
    """The file name of an S3 key — the sibling's ``_track_queued_file`` spelling."""
    return source_key.rsplit("/", 1)[-1] if "/" in source_key else source_key


def _source_key_from_body(body_raw: str) -> tuple[str, str] | None:
    """``(source_bucket, source_key)`` off a DLQ'd EventBridge body, or ``None``.

    ``None`` means the record is unusable and permanently so — see the module
    docstring on why that is reported as success. The two shapes it covers are
    exactly the two ``_process_sqs_record`` rejects deterministically: a body
    that is not JSON, and a body whose ``detail`` carries no bucket/key.
    """
    try:
        eb_event = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(eb_event, dict):
        return None
    detail = eb_event.get("detail")
    if not isinstance(detail, dict):
        return None
    bucket = detail.get("bucket")
    obj = detail.get("object")
    source_bucket = bucket.get("name") if isinstance(bucket, dict) else None
    source_key = obj.get("key") if isinstance(obj, dict) else None
    if not source_bucket or not source_key:
        return None
    return str(source_bucket), str(source_key)


def record_lost_file(source_bucket: str, source_key: str, receive_count: str | None) -> dict[str, str]:
    """Write the Lost_File record for one dead-lettered upload event.

    Returns the key written, so the caller can log it. Raises on a DynamoDB
    failure — that is the transient case the batch response reports.
    """
    key = queued_row_key(pipeline_prefix_of(source_key), source_key)
    now = datetime.now(UTC).isoformat()
    executions_table.put_item(
        Item={
            # ── the Queued_Placeholder's eight attributes, unchanged ──
            **key,
            # NOT a new status value — do-NOT 19 / LGR-21a1b5b-2. See module docstring.
            "status": "QUEUED",
            "file_name": file_name_of(source_key),
            "source_key": source_key,
            # Load-bearing: RunsByTimeIndex is sparse, so a row without this is
            # invisible with no error (LGR-7d19eca-2). `now` rather than the
            # original queue time because the original is not recoverable from a
            # DLQ message and inventing one would put a fabricated value on the
            # index's sort key. What this timestamp means is therefore "when the
            # loss was recorded", which is also what `queued_at` renders as.
            "started_at": now,
            "current_step": LOST_FILE_CURRENT_STEP,
            "ttl": int(time.time()) + LOST_FILE_RECORD_TTL_SECONDS,
            # ── the loss, on its own attribute ──
            QUEUE_OUTCOME_ATTRIBUTE: QUEUE_OUTCOME_LOST,
            "lost_reason": LOST_REASON,
            "lost_at": now,
            "source_bucket": source_bucket,
        }
    )
    logger.warning(
        "Recorded lost file: its SQS message reached the DLQ",
        extra={
            "pipeline_id": key["pipeline_id"],
            "run_id": key["run_id"],
            "source_key": source_key,
            "source_bucket": source_bucket,
            # Logged rather than interpolated into `lost_reason` — see LOST_REASON.
            "approximate_receive_count": receive_count,
        },
    )
    return key


def lambda_handler(event, context):
    """Record every dead-lettered upload event as a Lost_File.

    Returns ``{"batchItemFailures": [...]}``. Inert without
    ``ReportBatchItemFailures`` on the event source mapping.
    """
    failures: list[dict[str, str]] = []
    recorded = 0
    unusable = 0

    for record in event.get("Records") or []:
        message_id = record.get("messageId", "")
        parsed = _source_key_from_body(record.get("body", ""))
        if parsed is None:
            # Permanent. Reported as success on purpose — there is no DLQ behind
            # this one, and there is no source_key to write a record under.
            unusable += 1
            logger.error(
                "Dead-lettered message carries no usable bucket/key; no Lost_File record can be written for it",
                extra={"message_id": message_id, "body": str(record.get("body", ""))[:500]},
            )
            continue

        source_bucket, source_key = parsed
        attributes = record.get("attributes") or {}
        try:
            record_lost_file(source_bucket, source_key, attributes.get("ApproximateReceiveCount"))
            recorded += 1
        except Exception as exc:
            # Transient. Report it so the message returns and the next delivery
            # can write the record; a lost file with no record is the one outcome
            # this Lambda exists to prevent.
            logger.error(
                f"Failed to record lost file: {exc}",
                extra={"message_id": message_id, "source_key": source_key},
            )
            failures.append({"itemIdentifier": message_id})

    logger.info(
        "Queue loss recorder finished",
        extra={"recorded": recorded, "unusable": unusable, "reported_failures": len(failures)},
    )
    return {"batchItemFailures": failures}
