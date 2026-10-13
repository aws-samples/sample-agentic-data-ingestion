# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pipeline Trigger Lambda — SQS → concurrency check → Step Functions.

Reads EventBridge S3 Object Created events from SQS, checks concurrency at
two levels before starting a Step Functions execution:

1. Global gate: total running executions < MAX_CONCURRENT_EXECUTIONS
2. Per-pipeline gate: no running execution for the same pipeline prefix

If either gate blocks, the Lambda tracks the file as "queued" in DynamoDB
and reports that ONE record as a partial batch failure so SQS redelivers it
after the visibility timeout. When a file starts processing, its queued
record is removed.

p30 R10.1: a **scheduled** trigger rejected by either gate writes a
Skipped_Run_Record and notifies once per collapse window — nothing retries a
rejected schedule, so the load is lost unless it is made visible. The SQS path
is deliberately NOT recorded that way: its rejection is a redelivery, not a
skip. See :func:`_record_contention_skip`.

p30 R9.3: the SQS path returns ``batchItemFailures`` and never raises.
Raising made SQS redeliver the whole batch, so one blocked record caused every
already-started record in the same batch to be processed again — duplicate rows
on an append-mode target, or a retry rejected by the per-pipeline lock. See
:func:`_handle_sqs_batch`, and note the response is inert without
``ReportBatchItemFailures`` on the event source mapping.
"""

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

# Shared modules: /opt/python in the Lambda runtime (the trigger layer copies
# shared/*.py there — see PowertoolsTriggerLayer in orchestration-stack.ts);
# the relative path covers local imports (tests, tooling).
sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# P28 R18.2: lock/queued-record TTLs come from the single config source.
# LOCK_TTL_SECONDS is the 11-day safety net for a lock whose execution vanished
# (it exceeds the state machine timeout); QUEUED_RECORD_TTL_SECONDS bounds transient queue trackers.
from config import (
    LOCK_TTL_SECONDS,
    PROCESSED_FILE_LEDGER_TTL_SECONDS,
    QUEUED_RECORD_TTL_SECONDS,
    SKIPPED_RUN_NOTIFY_WINDOW_SECONDS,
    SKIPPED_RUN_RECORD_TTL_SECONDS,
)

# p30 R9.1/R9.2: the Processed_File_Ledger's fingerprint composition and row key
# are defined once, in shared/, because this Lambda READS rows that
# job_completion WRITES. A second definition here that drifted by a single
# character would disable the check silently — it would simply never match.
from processed_file_ledger import (
    SKIP_REASON_DUPLICATE,
    content_fingerprint,
    find_processed_file,
    normalize_etag,
)

# P28 R9.2: every SFN execution input is built by the shared, contract-tested
# builder so trigger shapes can't drift between producers.
from rerun_contract import build_trigger_input

logger = Logger(service="agentic_data_ingestion")

# Environment variables
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
MAX_CONCURRENT_EXECUTIONS = int(os.environ.get("MAX_CONCURRENT_EXECUTIONS", "10"))
STATE_TABLE = os.environ.get("STATE_TABLE", "agentic_data_ingestion_state")
EXECUTIONS_TABLE = os.environ.get("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
LOCKS_TABLE = os.environ.get("LOCKS_TABLE", "agentic_data_ingestion_pipeline_locks")
PROCESSED_FILES_TABLE = os.environ.get("PROCESSED_FILES_TABLE", "agentic_data_ingestion_processed_files")

# AWS clients
sfn_client = boto3.client("stepfunctions")
dynamodb = boto3.resource("dynamodb")
executions_table = dynamodb.Table(EXECUTIONS_TABLE)
locks_table = dynamodb.Table(LOCKS_TABLE)
processed_files_table = dynamodb.Table(PROCESSED_FILES_TABLE)


def _execution_name(source_key: str) -> str:
    """Generate a unique SFN-safe execution name from source key + timestamp.

    Includes a timestamp so re-uploads of the same filename get new execution
    names (SFN rejects reused names even after execution completes/aborts).
    The per-pipeline dedup gate handles actual deduplication.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    key_hash = hashlib.sha256(source_key.encode()).hexdigest()[:12]
    filename = source_key.rsplit("/", 1)[-1] if "/" in source_key else source_key
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in filename)[:30]
    return f"file-{safe_name}-{ts}-{key_hash}"


def _pipeline_prefix(source_key: str) -> str:
    """Extract the pipeline prefix from an S3 key."""
    parts = source_key.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else source_key


def _running_execution_snapshot() -> tuple[int, bool]:
    """Count RUNNING executions for the state machine. Returns (count, is_floor).

    The page size is deliberately ``MAX_CONCURRENT_EXECUTIONS + 1``: the gate
    only needs to know whether the count has reached the ceiling, and one extra
    row is enough to answer that without paying for a full enumeration on a
    path that runs once per SQS record. ``nextToken`` is NOT followed, so the
    count saturates at that page size.

    That truncation is why the second element exists. When ``nextToken`` comes
    back there are more RUNNING executions than were counted, and the number is
    a lower bound, not a total — reporting it as a total would understate
    saturation in the reassuring direction ("11 running" when 40 are). Callers
    pass the flag to :func:`_concurrency_rejection_message`, which renders a
    floor as "at least N".

    Both values describe a single moment. Executions start and finish
    independently of this Lambda, so the count is a snapshot that may be stale
    by the time anyone reads the message built from it.
    """
    resp = sfn_client.list_executions(
        stateMachineArn=STATE_MACHINE_ARN,
        statusFilter="RUNNING",
        maxResults=MAX_CONCURRENT_EXECUTIONS + 1,
    )
    return len(resp.get("executions", [])), bool(resp.get("nextToken"))


def _concurrency_rejection_message(running_count: int, count_is_floor: bool, consequence: str) -> str:
    """Build the global-concurrency rejection message. p30 R10.4.

    R10.4: the message surfaced to a manual caller states the current and the
    maximum running-execution counts. Both numbers are labelled rather than
    written as a bare ``n/max`` pair, because at saturation the two are equal
    and ``(10/10)`` gives an operator no way to tell which side is the ceiling.

    Kept distinct from the per-pipeline lock-contention message ("Pipeline 'x'
    already has a running execution"), which is a different rejection with a
    different remedy: this one clears as the fleet drains, that one needs the
    pipeline's own lock row gone. The counts appear only here — a lock
    rejection has no fleet-wide number to report.

    The "observed" wording and the snapshot note are load-bearing: the count is
    read once, before the lock is taken, and is not a guarantee about the state
    of the fleet at any later instant.

    ``Global concurrency limit reached`` is retained verbatim as the prefix;
    callers and tests across the suite match on it.
    """
    observed = f"at least {running_count}" if count_is_floor else str(running_count)
    return (
        f"Global concurrency limit reached: {observed} running of a maximum "
        f"{MAX_CONCURRENT_EXECUTIONS}, observed when the trigger was checked. {consequence}"
    )


def _duplicate_override_requested(event: dict) -> bool:
    """Whether a manual caller explicitly acknowledged the duplicate (p30 R9.4).

    R9.4: the ledger must not prevent a deliberate re-run started from the UI,
    but only with an *explicit* acknowledgement — a Run Now that happens to hit
    a duplicate is still worth telling the user about, otherwise the ledger
    would be trivially bypassable by the most common path and would protect
    nothing. Accepts the boolean and the string spelling because API Gateway /
    dashboard payloads have historically sent both.

    Only the manual path consults this. A scheduled trigger and an S3 upload
    event have no human behind them to acknowledge anything.
    """
    raw = event.get("acknowledge_duplicate", event.get("force", False))
    if isinstance(raw, bool):
        return raw
    return isinstance(raw, str) and raw.strip().lower() == "true"


def _duplicate_skip_record_id(fingerprint: str) -> str:
    """Deterministic executions-table ``run_id`` for a duplicate-skip record.

    Deterministic in the fingerprint, so repeated re-uploads of the SAME content
    overwrite ONE row instead of accumulating one per attempt. That bounds the
    number of skip rows by the number of distinct duplicate contents (itself
    bounded by the ledger's TTL) rather than by how many times someone drags the
    same file into the bucket, and it makes the row read as "this content was
    skipped, most recently at …" — which is the useful framing.
    """
    return f"skipped-{fingerprint[:32]}"


def _file_name_of(source_key: str) -> str:
    """Basename of an S3 key, or the key itself when it has no prefix."""
    return source_key.rsplit("/", 1)[-1] if "/" in source_key else source_key


def _as_int(value) -> int | None:
    """``value`` as an ``int``, or ``None`` when it is not usable as one.

    Sizes reach this module as ``int`` (EventBridge detail), as a numeric string,
    and as ``Decimal`` (read back off a DynamoDB ledger row), so the three are
    normalised before they are stored or rendered side by side.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pre_image_phrase(size: int | None, etag: str) -> str:
    """``"1024 bytes, ETag abc…"`` for a fingerprint's pre-image, or ``""``.

    Empty when neither half is known — an older ledger row predating
    ``record_processed_file``'s pre-image attributes has no size or ETag to show,
    and printing "None bytes" would read as a value rather than as an absence.
    """
    parts = []
    if size is not None:
        parts.append(f"{size} bytes")
    if etag:
        parts.append(f"ETag {etag}")
    return ", ".join(parts)


def _duplicate_skip_detail(
    previous_run_id: str,
    previous_source_key: str,
    previous_content_size: int | None,
    previous_content_etag: str,
    size: int | None,
    etag: str,
) -> str:
    """Operator-facing explanation of one duplicate skip (p64 R5.1, R5.2).

    The skip used to name only the earlier ``run_id``, which is why a *suspected*
    false positive could not be investigated: nothing on the record said which
    file had been matched, or what the fingerprint had been computed from. Both
    sides' pre-image go in — the matched ledger row's and this submission's —
    because they are what makes "this is a duplicate of that" checkable without
    recomputing a digest.

    A fingerprint match implies the two pre-images are equal, so seeing them
    disagree here means the matched row is missing its pre-image (an older row)
    rather than that the hash misfired. That is the distinction an operator
    needs, and it is the reason both sides are rendered rather than one.

    Composition of the fingerprint itself is untouched (p64 do-NOT 4); this
    describes it, and the description lives here rather than beside the digest so
    there is still exactly one definition in ``shared/``.

    Each clause is omitted when its input is unknown, so the sentence never
    contains a rendered ``None``.
    """
    previous_file = _file_name_of(previous_source_key)
    origin = f" from '{previous_file}'" if previous_file else ""
    parts = [f"Identical content was already processed successfully by run '{previous_run_id or 'unknown'}'{origin}."]
    previous_pre_image = _pre_image_phrase(previous_content_size, previous_content_etag)
    if previous_pre_image:
        parts.append(f"That content was {previous_pre_image}.")
    this_pre_image = _pre_image_phrase(size, etag)
    if this_pre_image:
        parts.append(f"This submission is {this_pre_image}.")
    parts.append("The match is on size and ETag only.")
    parts.append("Use Run Now with the duplicate acknowledgement to process it again.")
    return " ".join(parts)


def _record_duplicate_skip(
    pipeline_prefix: str,
    fingerprint: str,
    source_key: str,
    trigger: str,
    previous_run_id: str,
    *,
    matched_row: dict,
    size=None,
    etag=None,
) -> None:
    """Make a duplicate skip visible in the pipeline's run history (p30 R9.2).

    R9.2 requires the skip to be *recorded with a reason visible in the UI*, not
    merely logged: a re-upload that quietly does nothing is indistinguishable
    from a broken trigger, which is the failure this clause exists to prevent.

    Shape is an executions-table row with ``status="SKIPPED"`` — the same vehicle
    the Skipped_Run_Record glossary entry defines for task 4.9's
    concurrency/lock-contention skips. Two overlapping notes for 4.9, which owns
    that record and its notification:

    * ``skip_reason`` is the discriminator. This path writes
      ``duplicate_content``; 4.9's contention skips want their own reason on the
      same attribute rather than a second row type.
    * ``run_id`` sorts ABOVE ``run-…`` under the run-history query's descending
      sort (``s`` > ``r``), so skip rows appear at the top of page 1. Bounded by
      the deterministic id above, but 4.9 owns run-history display and should
      decide whether that ordering is what it wants.

    NOT written here: the notification (R10.1, task 4.9) and the UI's
    failure-vs-skip distinction (R10.2, task 4.9).

    **Attribute naming, because both sides of the comparison are on one row
    (p64 R5.1).** Unprefixed attributes describe the object being *rejected* —
    ``source_key``, ``file_name``, ``content_size``, ``content_etag`` — following
    the convention the first three already set. ``previous_``-prefixed ones
    describe the ledger row it matched, extending the existing
    ``previous_run_id``. ``content_fingerprint`` is unprefixed and belongs to
    neither side alone: a match means both share it.

    The matched row's pre-image attributes are written only when the row carries
    them. ``record_processed_file`` has persisted them since p30, but a row older
    than that, or one read back partially, has neither — and a stored ``None``
    would read as a measured zero-ish value rather than as an absence.

    ``previous_source_key`` deliberately does **not** get a ``previous_file_name``
    companion: the basename is already in ``skip_detail``, which is what the UI
    renders, and a fourth attribute would be a second place to keep it correct.

    ``matched_row`` is passed whole rather than pre-split by the caller so that
    every decision about what the *record* looks like stays here. It is the row
    ``find_processed_file`` returned, so a ``Decimal`` size and an already
    normalised ETag are both expected shapes.

    Best-effort. The skip has already been decided; failing to record it must not
    turn a skip into an error, and the log line below still carries the reason.
    """
    try:
        previous_source_key = str(matched_row.get("source_key", "") or "")
        previous_content_size = _as_int(matched_row.get("content_size"))
        previous_content_etag = normalize_etag(matched_row.get("content_etag"))
        # This submission's own pre-image, normalised through the SAME helper the
        # ledger writes with, so the two ETags on this row are comparable rather
        # than one quoted and one bare (`list_objects_v2` quotes it; the
        # EventBridge detail does not).
        this_size = _as_int(size)
        this_etag = normalize_etag(etag)

        item = {
            "pipeline_id": pipeline_prefix,
            "run_id": _duplicate_skip_record_id(fingerprint),
            "status": "SKIPPED",
            "skip_reason": SKIP_REASON_DUPLICATE,
            "skip_detail": _duplicate_skip_detail(
                previous_run_id,
                previous_source_key,
                previous_content_size,
                previous_content_etag,
                this_size,
                this_etag,
            ),
            "file_name": _file_name_of(source_key),
            "source_key": source_key,
            "content_fingerprint": fingerprint,
            "previous_run_id": previous_run_id,
            "triggered_by": trigger,
            "started_at": datetime.now(UTC).isoformat(),
            "current_step": "duplicate_check",
            "ttl": int(time.time()) + PROCESSED_FILE_LEDGER_TTL_SECONDS,
        }
        if previous_source_key:
            item["previous_source_key"] = previous_source_key
        if previous_content_size is not None:
            item["previous_content_size"] = previous_content_size
        if previous_content_etag:
            item["previous_content_etag"] = previous_content_etag
        if this_size is not None:
            item["content_size"] = this_size
        if this_etag:
            item["content_etag"] = this_etag

        executions_table.put_item(Item=item)
    except Exception as e:
        logger.warning(f"Failed to record duplicate skip: {e}")


def _duplicate_content_skip(
    pipeline_prefix: str,
    source_key: str,
    size,
    etag,
    trigger: str,
) -> str | None:
    """Skip message if this content already ran successfully here, else ``None``.

    p30 R9.1/R9.2. Called on every S3_UPLOAD trigger path and deliberately
    **before** ``_acquire_pipeline_lock`` on all of them. Ordering matters: a
    skip is a path on which no execution starts, so under 4.10's rule the lock
    would have to be released again — and a lock leaked here would wedge the
    pipeline for the 11-day ``LOCK_TTL_SECONDS`` every time someone re-uploaded a
    file. Not taking the lock at all is strictly safer than taking it and
    remembering to give it back.

    ``size`` and ``etag`` come from data the caller ALREADY has, never from a new
    S3 call: ``detail.object.size``/``detail.object.etag`` on the SQS path, and
    the ``Size``/``ETag`` of the chosen entry from the ``list_objects_v2`` the
    manual and scheduled paths already issue to find the newest file. When either
    is missing the fingerprint is ``None`` and this returns ``None`` — no
    fingerprint, no dedup, proceed. (``etag`` is optional in the EventBridge S3
    schema, so that is a real case, not a defensive one.)

    JDBC never reaches here: there is no object, so there is nothing to
    fingerprint. See the note in :func:`_start_jdbc_execution`.
    """
    fingerprint = content_fingerprint(size, etag)
    if not fingerprint:
        return None

    existing = find_processed_file(processed_files_table, pipeline_prefix, fingerprint)
    if not existing:
        return None

    previous_run_id = str(existing.get("run_id", "") or "")
    msg = (
        f"Duplicate content skipped for pipeline '{pipeline_prefix}': "
        f"'{source_key}' has the same size and ETag as content already processed "
        f"successfully by run '{previous_run_id or 'unknown'}'."
    )
    logger.info(
        msg,
        extra={
            "pipeline_prefix": pipeline_prefix,
            "source_key": source_key,
            "content_fingerprint": fingerprint,
            "previous_run_id": previous_run_id,
            "skip_reason": SKIP_REASON_DUPLICATE,
            "triggered_by": trigger,
        },
    )
    _record_duplicate_skip(
        pipeline_prefix,
        fingerprint,
        source_key,
        trigger,
        previous_run_id,
        matched_row=existing,
        size=size,
        etag=etag,
    )
    return msg


# ── Skipped_Run_Record: contention reasons (p30 R10.1) ──────────────────
#
# Written on the ``skip_reason`` attribute of an executions-table row with
# ``status="SKIPPED"`` — the SAME vehicle and the SAME discriminator
# ``_record_duplicate_skip`` uses for ``duplicate_content`` (4.7's note). One
# row type, distinguished by reason, not three row types.
#
# The dashboard carries a matching table in
# ``dashboard-react/src/utils/skip-reason.ts``. That duplication is
# unavoidable across the Python/TypeScript boundary; adding a reason here
# without adding it there renders as an "unrecognised reason" badge, which is
# the deliberate fallback rather than a silent mislabel.

#: Global concurrency ceiling reached — a fleet-wide condition.
SKIP_REASON_CONCURRENCY = "global_concurrency"

#: Another execution already holds this pipeline's lock — a per-pipeline condition.
SKIP_REASON_LOCK_CONTENTION = "pipeline_lock_contention"


def _skip_window_start(now: datetime) -> datetime:
    """Start of the collapse window containing ``now``.

    Windows are aligned to the epoch, not to the first skip, so two invocations
    a few seconds apart derive the SAME window and therefore the same record id
    without coordinating. That is the whole mechanism: the id is the idempotency
    key, so no read is needed to decide whether a window already has a record.
    """
    epoch_second = int(now.timestamp())
    aligned = epoch_second - (epoch_second % SKIPPED_RUN_NOTIFY_WINDOW_SECONDS)
    return datetime.fromtimestamp(aligned, tz=UTC)


def _contention_skip_record_id(skip_reason: str, window_start: datetime) -> str:
    """Executions-table ``run_id`` for a contention Skipped_Run_Record.

    **Deliberately shaped like a real run id, and deliberately NOT shaped like
    4.7's ``skipped-{fingerprint}``.** 4.7 flagged that its prefix sorts above
    ``run-`` under the run-history query's descending sort (``s`` > ``r``) and
    left the ordering decision here. The decision is: do not inherit it.

    ``get_pipeline_executions`` queries ``ScanIndexForward=False`` with
    ``Limit=page_size`` (10). Any prefix that sorts above ``run-`` makes page 1
    "the most recent SKIPS" instead of "the most recent activity" — a pipeline
    that skipped ten times last month and has succeeded every day since would
    show ten skip rows and no runs. That is a worse lie than the silent miss
    R10 exists to fix. A contention skip also has no fingerprint to be
    deterministic in, so there is nothing forcing the other shape.

    ``run-{YYYY-MM-DD-HH-MM-SS}-skip-{reason}`` matches
    ``file_validation.generate_run_id``'s ``run-{YYYY-MM-DD-HH-MM-SS}-{uuid8}``
    up to the timestamp, so skip rows interleave chronologically with real runs
    and page 1 is honestly the ten most recent things that happened.

    Two consequences, accepted:

    * The timestamp is the WINDOW start, not the skip instant, so a skip at
      14:59 sorts as though it happened at 14:00 and can appear below a run
      that started at 14:30. ``started_at``/``last_skipped_at`` on the row carry
      the real times; the sort is imprecise by at most one window. That is the
      price of collapsing 12 five-minute skips into one row, and the alternative
      (one row per skip) reintroduces the crowding above.
    * Nothing in the repo derives meaning from a ``run_id`` prefix — no
      ``startswith("run-")`` exists in ``lambda/``, ``shared/``, ``agentcore/``
      or ``dashboard-react/src`` — so reusing the prefix cannot make a skip row
      be mistaken for an execution. ``status`` and ``skip_reason`` are what
      readers branch on.
    """
    return f"run-{window_start.strftime('%Y-%m-%d-%H-%M-%S')}-skip-{skip_reason}"


def _notify_scheduled_run_skipped(
    pipeline_key: str,
    run_id: str,
    skip_reason: str,
    skip_detail: str,
    running_count: int | None,
    count_is_floor: bool,
) -> None:
    """Send the R10.1 notification for a newly-recorded scheduled skip.

    ``notification_service`` is imported HERE, not at module top, and the
    ImportError is swallowed. It is not an optional nicety: that module imports
    ``requests`` at import time for its Slack channel, and this Lambda's layer
    installs ``aws-lambda-powertools`` only (``PowertoolsTriggerLayer`` in
    orchestration-stack.ts) — ``requests`` is not in
    ``shared/runtime_requirements.txt`` and reaches the Strands layer only as a
    transitive dependency this layer does not have. A module-level import would
    therefore risk failing at COLD START and taking every trigger path down —
    S3 uploads, manual runs, schedules — to add a notification. Trading the
    notification for the trigger is not a trade R10.1 asks for.

    Never raises, for the same reason ``_record_duplicate_skip`` never raises:
    the skip is already decided, and turning a skip into an invocation error
    would make EventBridge retry a decision that will not change. The log line
    below is the fallback record.

    SNS is a no-op here as deployed — the trigger Lambda has no
    ``*_TOPIC_ARN`` in its environment, so ``SNSChannel`` returns
    ``{"success": False}`` without a publish. The dashboard channel does land,
    writing to the notifications table. That is worth knowing when reading a
    result: an all-channels-failed result means the dashboard write failed, not
    that SNS was missing.
    """
    try:
        from notification_service import send_scheduled_run_skipped_alert

        send_scheduled_run_skipped_alert(
            pipeline_key,
            run_id,
            skip_reason,
            skip_detail,
            window_seconds=SKIPPED_RUN_NOTIFY_WINDOW_SECONDS,
            running_count=running_count,
            running_max=MAX_CONCURRENT_EXECUTIONS if running_count is not None else None,
            running_count_is_floor=count_is_floor,
        )
    except Exception as e:
        logger.warning(
            f"Failed to send scheduled-skip notification: {e}",
            extra={"pipeline_id": pipeline_key, "run_id": run_id, "skip_reason": skip_reason},
        )


def _record_contention_skip(
    pipeline_key: str,
    skip_reason: str,
    skip_detail: str,
    *,
    source_key: str = "",
    running_count: int | None = None,
    count_is_floor: bool = False,
) -> None:
    """Record a skipped scheduled run and notify once per window (p30 R10.1).

    Called ONLY from scheduled-trigger contention paths. Which contentions are
    and are not recorded here is the substance of R10.1:

    * **Scheduled + global concurrency**, **scheduled + pipeline lock** — both
      recorded, on the S3_UPLOAD and the JDBC branch alike. Nothing retries a
      rejected schedule: the handler returns 429 to EventBridge, which does not
      re-fire, so the next attempt is the next scheduled tick and this load is
      genuinely lost. That is precisely the "silent miss" R10 names.
    * **SQS/s3_upload contention — NOT recorded.** The message returns to the
      queue as a ``batchItemFailures`` entry (R9.3) and is redelivered after the
      visibility timeout, so the work is about to happen. A SKIPPED row there
      would claim a run was lost while it was in fact queued, and it would
      contradict the QUEUED tracker ``_track_queued_file`` writes on the same
      decision — which stays truthful for exactly the same reason. Recording
      that path would put a false miss on the run list for work that is about to
      succeed.
    * **Manual Run Now contention — NOT recorded.** R10.1 scopes this to
      scheduled triggers, and the manual caller is told synchronously: it gets
      429 with the counts (R10.4) in front of the human who asked. A row
      restating what the user just saw rejected adds nothing to the run history.

    **Collapse and rate-limit are the same mechanism.** The conditional put
    below succeeds only for the FIRST skip of this pipeline+reason in the
    window, and only that skip notifies; later skips fold into the row via
    ``skip_count``/``last_skipped_at``. One DynamoDB call in the common case,
    two when folding. That bounds both the row count and the notification count
    at one per window per pipeline per reason without a second rate-limiter to
    keep consistent — see ``SKIPPED_RUN_NOTIFY_WINDOW_SECONDS`` for why 288
    notifications a day is the failure being avoided.

    ``running_count`` is stored WITH ``running_count_is_floor`` or not at all
    (4.11). The count saturates at a page boundary in
    :func:`_running_execution_snapshot`, so a stored bare count would let a
    reader of this row conclude "11 running" when 40 were running — the exact
    over-confidence the rejection message was changed to shed. A lock-contention
    skip stores neither: there is no fleet-wide number behind that decision, and
    a zero would read as one.

    Best-effort throughout. The skip has already happened; failing to record it
    must not turn a skip into an error.
    """
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    run_id = _contention_skip_record_id(skip_reason, _skip_window_start(now))

    item: dict = {
        "pipeline_id": pipeline_key,
        "run_id": run_id,
        "status": "SKIPPED",
        "skip_reason": skip_reason,
        "skip_detail": skip_detail,
        "triggered_by": "scheduled",
        "started_at": now_iso,
        "last_skipped_at": now_iso,
        "skip_count": 1,
        "skip_window_seconds": SKIPPED_RUN_NOTIFY_WINDOW_SECONDS,
        "current_step": "concurrency_gate" if skip_reason == SKIP_REASON_CONCURRENCY else "pipeline_lock_gate",
        "ttl": int(time.time()) + SKIPPED_RUN_RECORD_TTL_SECONDS,
    }
    if source_key:
        item["source_key"] = source_key
        item["file_name"] = source_key.rsplit("/", 1)[-1] if "/" in source_key else source_key
    if running_count is not None:
        item["running_count"] = running_count
        item["running_count_is_floor"] = count_is_floor
        item["running_max"] = MAX_CONCURRENT_EXECUTIONS

    try:
        executions_table.put_item(Item=item, ConditionExpression="attribute_not_exists(run_id)")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning(f"Failed to record scheduled skip: {e}", extra={"pipeline_id": pipeline_key})
            return
        # A record for this window already exists: fold into it and do NOT
        # notify. The refreshed detail and count keep the row describing the
        # most recent skip rather than only the first.
        try:
            executions_table.update_item(
                Key={"pipeline_id": pipeline_key, "run_id": run_id},
                UpdateExpression=(
                    "SET last_skipped_at = :now, skip_detail = :detail, #ttl = :ttl ADD skip_count :one"
                ),
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":now": now_iso,
                    ":detail": skip_detail,
                    ":ttl": int(time.time()) + SKIPPED_RUN_RECORD_TTL_SECONDS,
                    ":one": 1,
                },
            )
        except Exception as update_err:
            logger.warning(
                f"Failed to fold scheduled skip into existing record: {update_err}",
                extra={"pipeline_id": pipeline_key, "run_id": run_id},
            )
        logger.warning(
            "Scheduled run skipped; folded into the current window's Skipped_Run_Record",
            extra={
                "pipeline_id": pipeline_key,
                "run_id": run_id,
                "skip_reason": skip_reason,
                "notified": False,
            },
        )
        return
    except Exception as e:
        logger.warning(f"Failed to record scheduled skip: {e}", extra={"pipeline_id": pipeline_key})
        return

    logger.warning(
        "Scheduled run skipped; Skipped_Run_Record written",
        extra={
            "pipeline_id": pipeline_key,
            "run_id": run_id,
            "skip_reason": skip_reason,
            "running_count": running_count,
            "running_count_is_floor": count_is_floor,
            "notified": True,
        },
    )
    _notify_scheduled_run_skipped(pipeline_key, run_id, skip_reason, skip_detail, running_count, count_is_floor)


def _acquire_pipeline_lock(pipeline_prefix: str, execution_name: str) -> bool:
    """Try to acquire a DynamoDB lock for the pipeline prefix.

    Uses a conditional put_item that fails if a lock already exists,
    replacing the old SFN list_executions + describe_execution approach
    with a single ~5ms DynamoDB call.

    Returns True if the lock was acquired, False if the pipeline is already locked.
    """
    now = datetime.now(UTC).isoformat()
    ttl_epoch = int(time.time()) + LOCK_TTL_SECONDS
    try:
        locks_table.put_item(
            Item={
                "pipeline_prefix": pipeline_prefix,
                "execution_name": execution_name,
                "started_at": now,
                "ttl": ttl_epoch,
            },
            ConditionExpression="attribute_not_exists(pipeline_prefix)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _release_pipeline_lock(pipeline_prefix: str, execution_name: str) -> bool:
    """Release a lock THIS invocation acquired, when no execution will start.

    p30 R10.3. The normal release happens elsewhere: the state machine invokes
    the lock-release Lambda (``LockReleaseFunction`` in orchestration-stack.ts)
    when an execution reaches a terminal state, and pipeline deletion clears the
    row as part of its cleanup (``pipelines_crud`` step 5e). Neither fires when
    the *start* fails, so without this the row survives until the 11-day
    LOCK_TTL_SECONDS safety net and every later trigger for the pipeline is
    rejected as "already has a running execution".

    Conditional on ``execution_name`` so only the writer can release: if the row
    was deleted in between (pipeline delete) and re-acquired by another trigger,
    the condition fails and that lock is left alone. A missing row also fails the
    condition, which makes the call idempotent — releasing twice is a no-op.
    The existing release paths delete unconditionally because they run after the
    execution they own has finished; this one runs while other triggers for the
    same pipeline may be live, so it checks the holder.

    Never raises. Every caller is already on an error path carrying the real
    cause, and a secondary failure about lock cleanup would replace that cause
    with a symptom. Returns whether the lock was actually released.
    """
    try:
        locks_table.delete_item(
            Key={"pipeline_prefix": pipeline_prefix},
            ConditionExpression="execution_name = :execution_name",
            ExpressionAttributeValues={":execution_name": execution_name},
        )
        logger.info(
            "Released pipeline lock after failed execution start",
            extra={"pipeline_prefix": pipeline_prefix, "execution_name": execution_name},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Already gone, or now held by a different invocation. Both are
            # correct outcomes for this call, not errors.
            logger.info(
                "Pipeline lock not released: no longer held by this invocation",
                extra={"pipeline_prefix": pipeline_prefix, "execution_name": execution_name},
            )
            return False
        logger.error(
            f"Failed to release pipeline lock: {e}",
            extra={"pipeline_prefix": pipeline_prefix, "execution_name": execution_name},
        )
        return False
    except Exception as e:
        logger.error(
            f"Failed to release pipeline lock: {e}",
            extra={"pipeline_prefix": pipeline_prefix, "execution_name": execution_name},
        )
        return False


def _start_execution_releasing_lock_on_failure(execution_name: str, sfn_input: str, lock_key: str):
    """Start the SFN execution; release the pipeline lock if the start fails.

    p30 R10.3. The lock MUST survive a successful start — it is the
    per-pipeline concurrency gate, and the state machine's lock-release Lambda
    owns deleting it once the execution terminates. It must NOT survive a
    failed start.

    The release therefore lives in the ``except`` arm and deliberately NOT in a
    ``finally``: a ``finally`` would also release on the success path, leaving
    the pipeline unlocked while its execution runs and defeating the gate
    entirely. The exception is always re-raised, so callers keep their existing
    ``ExecutionAlreadyExists`` handling and the original cause reaches the
    operator unchanged.
    """
    try:
        return sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name,
            input=sfn_input,
        )
    except Exception:
        _release_pipeline_lock(lock_key, execution_name)
        raise


def _track_queued_file(pipeline_prefix: str, source_key: str, source_bucket: str):
    """Write a queued-file record to the executions table (no DynamoDB Stream side effects)."""
    try:
        file_name = source_key.rsplit("/", 1)[-1] if "/" in source_key else source_key
        queued_id = f"queued-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}"
        executions_table.put_item(
            Item={
                "pipeline_id": pipeline_prefix,
                "run_id": queued_id,
                "status": "QUEUED",
                "file_name": file_name,
                "source_key": source_key,
                "started_at": datetime.now(UTC).isoformat(),
                "current_step": "queued_trigger",
                "ttl": int(time.time()) + QUEUED_RECORD_TTL_SECONDS,
            }
        )
    except Exception as e:
        logger.warning(f"Failed to track queued file: {e}")


def _remove_queued_file(pipeline_prefix: str, source_key: str):
    """Remove the queued-file record when the file starts processing."""
    try:
        queued_id = f"queued-{hashlib.sha256(source_key.encode()).hexdigest()[:16]}"
        executions_table.delete_item(Key={"pipeline_id": pipeline_prefix, "run_id": queued_id})
    except Exception as e:
        logger.warning(f"Failed to remove queued file record: {e}")


def _handle_manual_trigger(event, context):
    """Manual trigger dispatcher invoked by the Run Now endpoint.

    Branches on ``source_type``:
      * ``JDBC`` → :func:`_start_jdbc_execution`
      * ``S3_UPLOAD`` (default) → :func:`_start_s3_upload_manual_execution`,
        which lists the newest file under the pipeline's S3 prefix and starts
        the State Machine.

    Returns the structured Run Now response shape on success
    (``execution_arn`` + ``execution_name``), HTTP 400 for bad input,
    HTTP 404 if no S3 files are found, HTTP 429 on concurrency or lock
    contention, and HTTP 500 on execution-name collision.

    Implements Requirement 5.9 of the p17-jdbc-data-source spec.
    """
    pipeline_id = event.get("pipeline_id", "")
    if not pipeline_id:
        logger.error("Manual trigger missing pipeline_id")
        return {"statusCode": 400, "body": "Missing pipeline_id"}

    # Default to S3_UPLOAD for back-compat — the Run Now endpoint always sets
    # source_type explicitly, but any caller from before Task 12 may not.
    source_type = event.get("source_type", "S3_UPLOAD")

    if source_type == "JDBC":
        return _start_jdbc_execution(pipeline_id, event, context)

    if source_type == "S3_UPLOAD":
        return _start_s3_upload_manual_execution(pipeline_id, event, context)

    msg = f"Unknown source_type: {source_type}"
    logger.error(msg, extra={"pipeline_id": pipeline_id, "source_type": source_type})
    return {"statusCode": 400, "body": msg}


def _start_s3_upload_manual_execution(pipeline_id: str, event: dict, context) -> dict:
    """Start an SFN execution for a manual S3_UPLOAD trigger.

    Mirrors :func:`_handle_scheduled_trigger`'s S3-listing logic, but uses
    ``trigger="manual"`` in the SFN input and returns the structured Run Now
    response shape so :ref:`Task 12 <run-now>` can surface
    ``execution_arn``/``execution_name`` to the dashboard.
    """
    source_prefix = event.get("source_prefix", pipeline_id)
    source_bucket = os.environ.get("SOURCE_BUCKET", "")

    if not source_bucket:
        logger.error("SOURCE_BUCKET environment variable not set")
        return {"statusCode": 500, "body": "SOURCE_BUCKET not configured"}

    logger.info(
        "Processing manual S3_UPLOAD trigger",
        extra={
            "pipeline_id": pipeline_id,
            "source_prefix": source_prefix,
            "source_bucket": source_bucket,
        },
    )

    # Find the most recent file under the pipeline's S3 prefix
    s3_client = boto3.client("s3")
    try:
        response = s3_client.list_objects_v2(
            Bucket=source_bucket,
            Prefix=f"{source_prefix}/",
            MaxKeys=1000,
        )
    except Exception as e:
        logger.error(f"Failed to list S3 objects: {e}")
        raise

    objects = [obj for obj in response.get("Contents", []) if not obj["Key"].endswith("/")]
    if not objects:
        msg = f"No source files found under '{source_prefix}/'"
        logger.warning(msg, extra={"source_prefix": source_prefix})
        return {"statusCode": 404, "body": msg}

    # Sort by LastModified descending, pick the most recent
    objects.sort(key=lambda x: x["LastModified"], reverse=True)
    newest = objects[0]
    source_key = newest["Key"]

    pipeline_prefix = _pipeline_prefix(source_key)
    logger.info(
        "Found most recent file for manual trigger",
        extra={"source_key": source_key, "pipeline_prefix": pipeline_prefix},
    )

    # ── Duplicate-content gate (p30 R9.2) ───────────────────────
    # Before the lock, so a skip never has one to release (4.10 / R10.3).
    # size + ETag come from the listing above — no extra S3 call.
    # R9.4: an explicit acknowledgement from the UI bypasses the check.
    if _duplicate_override_requested(event):
        logger.info(
            "Duplicate check overridden by explicit acknowledgement",
            extra={"pipeline_id": pipeline_id, "source_key": source_key},
        )
    else:
        duplicate_msg = _duplicate_content_skip(
            pipeline_prefix, source_key, newest.get("Size"), newest.get("ETag"), "manual"
        )
        if duplicate_msg:
            # 409, not 429: a human asked for this run and the answer is
            # "already done", which is a conflict to acknowledge — not
            # contention that will clear on its own if they wait.
            return {
                "statusCode": 409,
                "body": duplicate_msg,
                "skipped": True,
                "skip_reason": SKIP_REASON_DUPLICATE,
                "pipeline_id": pipeline_id,
            }

    # ── Global concurrency gate ─────────────────────────────────
    running_count, count_is_floor = _running_execution_snapshot()

    if running_count >= MAX_CONCURRENT_EXECUTIONS:
        msg = _concurrency_rejection_message(running_count, count_is_floor, "Manual trigger rejected.")
        logger.warning(
            msg,
            extra={
                "running_count": running_count,
                "running_max": MAX_CONCURRENT_EXECUTIONS,
                "running_count_is_floor": count_is_floor,
            },
        )
        return {"statusCode": 429, "body": msg}

    # ── Per-pipeline dedup gate ──────────────────────────────────
    execution_name = _execution_name(source_key)
    if not _acquire_pipeline_lock(pipeline_prefix, execution_name):
        msg = f"Pipeline '{pipeline_prefix}' already has a running execution"
        logger.warning(msg, extra={"pipeline_prefix": pipeline_prefix})
        return {"statusCode": 429, "body": msg}

    # ── Start Step Functions execution ───────────────────────────
    # The top-level pipeline_id is the LOCK KEY (the S3-key-derived prefix);
    # the minimal-mapping shape is documented in shared/rerun_contract.py.
    try:
        sfn_input = json.dumps(
            build_trigger_input(
                {"pipeline_id": pipeline_prefix, "source_type": "S3_UPLOAD"},
                triggered_by="manual",
                source_bucket=source_bucket,
                source_key=source_key,
            )
        )
    except ValueError as exc:
        logger.error(f"Trigger contract violation on manual S3_UPLOAD trigger: {exc}")
        # R10.3: the lock was acquired above and no execution will start.
        _release_pipeline_lock(pipeline_prefix, execution_name)
        return {"statusCode": 400, "body": f"Invalid trigger input: {exc}"}

    logger.info(
        "Starting Step Functions execution from manual S3_UPLOAD trigger",
        extra={
            "execution_name": execution_name,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "pipeline_prefix": pipeline_prefix,
        },
    )

    try:
        response = _start_execution_releasing_lock_on_failure(execution_name, sfn_input, pipeline_prefix)
    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.warning(
            "Execution name collision on manual S3_UPLOAD trigger",
            extra={"execution_name": execution_name},
        )
        return {
            "statusCode": 500,
            "body": f"Execution name collision for '{execution_name}'. Please retry.",
        }

    logger.info(
        "Step Functions execution started for manual S3_UPLOAD trigger",
        extra={"execution_name": execution_name},
    )

    return {
        "statusCode": 200,
        "execution_name": execution_name,
        "execution_arn": response["executionArn"],
        "pipeline_id": pipeline_id,
    }


def _start_jdbc_execution(pipeline_id: str, event: dict, context) -> dict:
    """Start a JDBC Step Functions execution.

    Used by both the manual trigger path (Task 8.2) and the scheduled JDBC
    path (Task 8.4 will route through here as well). For JDBC there is no
    S3 key, so the SFN input shape is ``{pipeline_id, source_type, trigger}``
    and the per-pipeline lock key is the ``pipeline_id`` itself.

    Returns:
      * HTTP 200 with ``execution_arn``/``execution_name``/``pipeline_id`` on success
      * HTTP 429 on global-concurrency saturation OR pipeline-lock contention
      * HTTP 500 on execution-name collision (extremely unlikely; user retries)

    p30 R9: the Processed_File_Ledger is **not applicable** on this path and is
    deliberately not consulted. There is no file — the source is a query against
    a live table — so there is no size, no ETag, and nothing that could be
    fingerprinted. "The same content twice" is not even well defined for a
    watermark-based incremental read, whose whole job is to return different rows
    each run. Incremental JDBC idempotency is the watermark's concern
    (``agentic_data_ingestion_pipeline_watermarks``), not this ledger's. Nothing
    to guard against here: the check is reached only from the S3_UPLOAD branches,
    so a JDBC trigger cannot misfire it.

    p30 R10.1: this helper serves BOTH the manual and the scheduled JDBC
    trigger, so the contention paths below record a Skipped_Run_Record only when
    ``trigger == "scheduled"``. A rejected schedule is a lost load — EventBridge
    does not re-fire — while a rejected Run Now was refused in front of the
    human who asked for it and needs no row. Read before the gates rather than
    just before ``build_trigger_input`` for that reason.

    Implements Requirement 5.10 of the p17-jdbc-data-source spec.
    """
    # Reflect the actual trigger so a future scheduled-JDBC path (Task 8.4)
    # can re-use this helper without rewriting it.
    trigger = event.get("trigger", "manual")
    is_scheduled = trigger == "scheduled"

    # ── Global concurrency gate ─────────────────────────────────
    running_count, count_is_floor = _running_execution_snapshot()

    if running_count >= MAX_CONCURRENT_EXECUTIONS:
        msg = _concurrency_rejection_message(running_count, count_is_floor, "JDBC trigger rejected.")
        logger.warning(
            msg,
            extra={
                "running_count": running_count,
                "running_max": MAX_CONCURRENT_EXECUTIONS,
                "running_count_is_floor": count_is_floor,
                "pipeline_id": pipeline_id,
            },
        )
        if is_scheduled:
            _record_contention_skip(
                pipeline_id,
                SKIP_REASON_CONCURRENCY,
                msg,
                running_count=running_count,
                count_is_floor=count_is_floor,
            )
        return {"statusCode": 429, "body": msg}

    # ── Build a JDBC-flavoured execution name ───────────────────
    # No S3 key for JDBC, so derive the name from pipeline_id + timestamp + sha256
    # short hash. SFN execution names are limited to 80 chars; this fits well under.
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    pipeline_hash = hashlib.sha256(pipeline_id.encode()).hexdigest()[:12]
    safe_pipeline_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in pipeline_id)[:30]
    execution_name = f"jdbc-{safe_pipeline_id}-{ts}-{pipeline_hash}"

    # ── Per-pipeline dedup gate ──────────────────────────────────
    # For JDBC the lock key is just the pipeline_id (no S3-key-derived prefix).
    if not _acquire_pipeline_lock(pipeline_id, execution_name):
        msg = f"Pipeline '{pipeline_id}' already has a running execution"
        logger.warning(msg, extra={"pipeline_id": pipeline_id})
        if is_scheduled:
            _record_contention_skip(pipeline_id, SKIP_REASON_LOCK_CONTENTION, msg)
        return {"statusCode": 429, "body": msg}

    # ── Start Step Functions execution ───────────────────────────
    try:
        sfn_input = json.dumps(
            build_trigger_input(
                {"pipeline_id": pipeline_id, "source_type": "JDBC"},
                triggered_by=trigger,
            )
        )
    except ValueError as exc:
        logger.error(f"Trigger contract violation on JDBC trigger: {exc}", extra={"pipeline_id": pipeline_id})
        # R10.3: the lock was acquired above and no execution will start.
        _release_pipeline_lock(pipeline_id, execution_name)
        return {"statusCode": 400, "body": f"Invalid trigger input: {exc}"}

    logger.info(
        "Starting Step Functions execution for JDBC source",
        extra={
            "execution_name": execution_name,
            "pipeline_id": pipeline_id,
            "trigger": trigger,
        },
    )

    try:
        # For JDBC the lock key is the pipeline_id itself (see the acquire above).
        response = _start_execution_releasing_lock_on_failure(execution_name, sfn_input, pipeline_id)
    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.warning(
            "Execution name collision on JDBC trigger",
            extra={"execution_name": execution_name, "pipeline_id": pipeline_id},
        )
        return {
            "statusCode": 500,
            "body": f"Execution name collision for '{execution_name}'. Please retry.",
        }

    logger.info(
        "Step Functions JDBC execution started",
        extra={"execution_name": execution_name, "pipeline_id": pipeline_id},
    )

    return {
        "statusCode": 200,
        "execution_name": execution_name,
        "execution_arn": response["executionArn"],
        "pipeline_id": pipeline_id,
    }


def _handle_scheduled_trigger(event, context):
    """Handle a scheduled pipeline trigger from EventBridge.

    Branches on ``source_type``:
      * ``JDBC`` → :func:`_start_jdbc_execution` (bypasses S3 listing entirely;
        the JDBC helper carries through ``trigger="scheduled"`` from the event)
      * ``S3_UPLOAD`` (default) → list the most recent file under the pipeline's
        S3 prefix, run concurrency checks, and start a Step Functions execution

    Implements Requirements 5.3, 5.10, 5.11 of the p17-jdbc-data-source spec.
    """
    pipeline_id = event.get("pipeline_id", "")
    if not pipeline_id:
        logger.error("Scheduled trigger missing pipeline_id")
        return {"statusCode": 400, "body": "Missing pipeline_id"}

    # Default to S3_UPLOAD for back-compat with EventBridge schedules created
    # before Task 12. New JDBC schedules set source_type explicitly.
    source_type = event.get("source_type", "S3_UPLOAD")

    if source_type == "JDBC":
        # _start_jdbc_execution reads event.get("trigger", "manual") so the
        # SFN input correctly reflects trigger="scheduled" here.
        return _start_jdbc_execution(pipeline_id, event, context)

    if source_type != "S3_UPLOAD":
        msg = f"Unknown source_type: {source_type}"
        logger.error(msg, extra={"pipeline_id": pipeline_id, "source_type": source_type})
        return {"statusCode": 400, "body": msg}

    # ── S3_UPLOAD branch ────────────────────────────────────────
    source_prefix = event.get("source_prefix", pipeline_id)
    source_bucket = os.environ.get("SOURCE_BUCKET", "")

    if not source_bucket:
        logger.error("SOURCE_BUCKET environment variable not set")
        return {"statusCode": 500, "body": "SOURCE_BUCKET not configured"}

    logger.info(
        "Processing scheduled trigger",
        extra={"pipeline_id": pipeline_id, "source_prefix": source_prefix, "source_bucket": source_bucket},
    )

    # Find the most recent file under the pipeline's S3 prefix
    s3_client = boto3.client("s3")
    try:
        response = s3_client.list_objects_v2(
            Bucket=source_bucket,
            Prefix=f"{source_prefix}/",
            MaxKeys=1000,
        )
    except Exception as e:
        logger.error(f"Failed to list S3 objects: {e}")
        raise

    objects = [obj for obj in response.get("Contents", []) if not obj["Key"].endswith("/")]
    if not objects:
        logger.warning(f"No source files found for scheduled trigger: {source_prefix}")
        return {"statusCode": 200, "body": "No files to process"}

    # Sort by LastModified descending, pick the most recent
    objects.sort(key=lambda x: x["LastModified"], reverse=True)
    newest = objects[0]
    source_key = newest["Key"]

    pipeline_prefix = _pipeline_prefix(source_key)
    logger.info(
        "Found most recent file for scheduled trigger",
        extra={"source_key": source_key, "pipeline_prefix": pipeline_prefix},
    )

    # ── Duplicate-content gate (p30 R9.2) ───────────────────────
    # A schedule that fires with no new file lands on the SAME newest object
    # every time, so this is the path where duplicate ingestion is not an
    # accident but the default behaviour. Before the lock (4.10 / R10.3); no
    # override, because no human is present to acknowledge one (R9.4 scopes the
    # override to Run Now).
    duplicate_msg = _duplicate_content_skip(
        pipeline_prefix, source_key, newest.get("Size"), newest.get("ETag"), "scheduled"
    )
    if duplicate_msg:
        # 200, not 409: the caller is EventBridge, and a non-2xx here buys a
        # retry of a decision that will not change. The skip is recorded and
        # visible; the schedule simply had nothing new to do.
        return {"statusCode": 200, "body": duplicate_msg, "skipped": True, "skip_reason": SKIP_REASON_DUPLICATE}

    # ── Global concurrency gate ─────────────────────────────────
    running_count, count_is_floor = _running_execution_snapshot()

    if running_count >= MAX_CONCURRENT_EXECUTIONS:
        msg = _concurrency_rejection_message(running_count, count_is_floor, "Scheduled trigger skipped.")
        logger.warning(
            msg,
            extra={
                "running_count": running_count,
                "running_max": MAX_CONCURRENT_EXECUTIONS,
                "running_count_is_floor": count_is_floor,
            },
        )
        # R10.1: nothing retries a rejected schedule, so this load is lost
        # unless it is recorded and someone is told.
        _record_contention_skip(
            pipeline_prefix,
            SKIP_REASON_CONCURRENCY,
            msg,
            source_key=source_key,
            running_count=running_count,
            count_is_floor=count_is_floor,
        )
        return {"statusCode": 429, "body": msg, "skipped": True, "skip_reason": SKIP_REASON_CONCURRENCY}

    # ── Per-pipeline dedup gate ──────────────────────────────────
    execution_name = _execution_name(source_key)
    if not _acquire_pipeline_lock(pipeline_prefix, execution_name):
        msg = f"Pipeline '{pipeline_prefix}' already has a running execution. Scheduled trigger skipped."
        logger.warning(msg, extra={"pipeline_prefix": pipeline_prefix})
        # R10.1. No running count is passed: this rejection was decided by the
        # pipeline's own lock row, not by a fleet-wide number.
        _record_contention_skip(
            pipeline_prefix,
            SKIP_REASON_LOCK_CONTENTION,
            msg,
            source_key=source_key,
        )
        return {"statusCode": 429, "body": msg, "skipped": True, "skip_reason": SKIP_REASON_LOCK_CONTENTION}

    # ── Start Step Functions execution ───────────────────────────
    try:
        sfn_input = json.dumps(
            build_trigger_input(
                {"pipeline_id": pipeline_prefix, "source_type": "S3_UPLOAD"},
                triggered_by="scheduled",
                source_bucket=source_bucket,
                source_key=source_key,
            )
        )
    except ValueError as exc:
        logger.error(f"Trigger contract violation on scheduled trigger: {exc}")
        # R10.3: the lock was acquired above and no execution will start.
        _release_pipeline_lock(pipeline_prefix, execution_name)
        return {"statusCode": 400, "body": f"Invalid trigger input: {exc}"}

    logger.info(
        "Starting Step Functions execution from scheduled trigger",
        extra={
            "execution_name": execution_name,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "pipeline_prefix": pipeline_prefix,
        },
    )

    try:
        _start_execution_releasing_lock_on_failure(execution_name, sfn_input, pipeline_prefix)
        logger.info("Step Functions execution started", extra={"execution_name": execution_name})
    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.warning(
            "Execution name collision on scheduled trigger",
            extra={"execution_name": execution_name},
        )
        raise

    return {"statusCode": 200, "body": f"Scheduled execution started: {execution_name}"}


def _process_sqs_record(record: dict) -> None:
    """Process one SQS record: parse the S3 event, run the gates, start the run.

    p30 R9.3. Raising from here does **not** fail the batch.
    :func:`_handle_sqs_batch` turns the exception into a single
    ``batchItemFailures`` entry naming *this* record's ``messageId``, so the
    message returns to the queue on its own and — after ``maxReceiveCount`` —
    reaches the DLQ on its own. Records already processed in the same batch keep
    the executions they started, which is the whole point of R9.3: before this,
    one lock conflict re-delivered the entire batch and every already-started
    record was processed a second time.

    Returning normally reports the record as **successful**, and that
    deliberately includes a duplicate-content skip (R9.2): "do nothing" is the
    correct outcome there, so naming it a failure would have SQS redeliver a
    message whose work is already done and eventually DLQ a correct decision.

    Every failure on this path is per-record, and each for its own reason:

    * **Malformed body / missing bucket-key** — deterministic. The message will
      never parse, so retrying it forever helps nobody and it belongs in the
      DLQ under its own id rather than dragging its siblings there.
    * **Contract violation** (``build_trigger_input``) — deterministic, same
      reasoning. The lock is released first (R10.3) so the DLQ trip does not
      also wedge the pipeline for the 11-day lock TTL.
    * **Global concurrency saturation** — a fleet-wide condition, but *not*
      treated as batch-wide. The snapshot is re-read per record, and it is a
      lower bound taken at one instant (see :func:`_running_execution_snapshot`),
      so a batch that is saturated at record 1 may genuinely have drained by
      record 3. Failing the later records without checking would report a fleet
      state that was never observed.
    * **Pipeline-lock contention** — per-pipeline by definition. Two records for
      two different pipelines are wholly independent; this is R9.3's own
      example.
    * **``ExecutionAlreadyExists`` / any other ``StartExecution`` error** —
      transient. The retry mints a fresh timestamped execution name, and the
      lock is already released by
      :func:`_start_execution_releasing_lock_on_failure`, so the retry can
      re-acquire it.
    """
    body_raw = record.get("body", "{}")
    try:
        eb_event = json.loads(body_raw)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse SQS body as JSON", extra={"error": str(exc), "body": body_raw[:500]})
        raise ValueError(f"Malformed SQS body: {exc}") from exc

    detail = eb_event.get("detail", {})
    source_bucket = detail.get("bucket", {}).get("name")
    source_key = detail.get("object", {}).get("key")

    if not source_bucket or not source_key:
        logger.error("Missing bucket or key in EventBridge detail", extra={"detail": detail})
        raise ValueError(f"EventBridge detail missing bucket/key: {json.dumps(detail)}")

    pipeline_prefix = _pipeline_prefix(source_key)
    logger.info(
        "Processing S3 event",
        extra={"source_bucket": source_bucket, "source_key": source_key, "pipeline_prefix": pipeline_prefix},
    )

    # ── Duplicate-content gate (p30 R9.2) ───────────────────────
    # First gate on the record, ahead of BOTH the concurrency gate and the
    # lock. Ahead of the lock because a skip must not leave one to release
    # (4.10 / R10.3); ahead of the concurrency gate because queueing and
    # retrying a file that is going to be skipped anyway wastes the retry
    # budget and leaves a QUEUED row the user has to interpret.
    #
    # size and etag come straight off the EventBridge detail — no S3 call.
    # `etag` is optional in that schema; absent → no fingerprint → proceed.
    obj_detail = detail.get("object", {})
    duplicate_msg = _duplicate_content_skip(
        pipeline_prefix, source_key, obj_detail.get("size"), obj_detail.get("etag"), "s3_upload"
    )
    if duplicate_msg:
        # `return`, never `raise`. A skip is a SUCCESS, so this record must not
        # appear in batchItemFailures — reporting it would return the message to
        # SQS and, after maxReceiveCount, DLQ a file whose correct outcome is
        # "skipped", turning a successful decision into an operator alarm.
        # Any queued tracker for this key is cleared: the file will never
        # process, so leaving it listed as queued would be a lie until its
        # TTL expired.
        _remove_queued_file(pipeline_prefix, source_key)
        return

    # ── Global concurrency gate ─────────────────────────────────
    running_count, count_is_floor = _running_execution_snapshot()

    if running_count >= MAX_CONCURRENT_EXECUTIONS:
        _track_queued_file(pipeline_prefix, source_key, source_bucket)
        msg = _concurrency_rejection_message(
            running_count,
            count_is_floor,
            "Message will return to SQS for retry after visibility timeout.",
        )
        logger.warning(
            msg,
            extra={
                "running_count": running_count,
                "running_max": MAX_CONCURRENT_EXECUTIONS,
                "running_count_is_floor": count_is_floor,
            },
        )
        raise Exception(msg)

    # ── Per-pipeline dedup gate ──────────────────────────────────
    execution_name = _execution_name(source_key)
    if not _acquire_pipeline_lock(pipeline_prefix, execution_name):
        _track_queued_file(pipeline_prefix, source_key, source_bucket)
        msg = (
            f"Pipeline '{pipeline_prefix}' already has a running execution. "
            "Message will return to SQS for retry after visibility timeout. "
            "Use batch trigger API for multi-file ingestion."
        )
        logger.warning(msg, extra={"pipeline_prefix": pipeline_prefix})
        raise Exception(msg)

    # ── Start Step Functions execution ───────────────────────────
    _remove_queued_file(pipeline_prefix, source_key)  # Clean up queued record if it existed

    # A contract violation is deliberately NOT swallowed on the SQS path: it
    # propagates, so _handle_sqs_batch reports THIS record and, after
    # maxReceiveCount, routes it to the DLQ — the honest surface for a
    # malformed upload event. The lock is released first (R10.3), otherwise a
    # single malformed event wedges the pipeline for the lock TTL; the exception is
    # re-raised unchanged so the reported identifier and DLQ routing are
    # unaffected.
    try:
        sfn_input = json.dumps(
            build_trigger_input(
                {"pipeline_id": pipeline_prefix, "source_type": "S3_UPLOAD"},
                triggered_by="s3_upload",
                source_bucket=source_bucket,
                source_key=source_key,
            )
        )
    except Exception:
        _release_pipeline_lock(pipeline_prefix, execution_name)
        raise

    logger.info(
        "Starting Step Functions execution",
        extra={
            "execution_name": execution_name,
            "source_bucket": source_bucket,
            "source_key": source_key,
            "pipeline_prefix": pipeline_prefix,
            "running_count": running_count,
        },
    )

    try:
        _start_execution_releasing_lock_on_failure(execution_name, sfn_input, pipeline_prefix)
        logger.info("Step Functions execution started", extra={"execution_name": execution_name})
    except sfn_client.exceptions.ExecutionAlreadyExists:
        # Timestamp-based name collision (extremely unlikely). The lock was
        # released by the wrapper above, so the redelivery this failure buys can
        # re-acquire it with a fresh timestamp.
        logger.warning(
            "Execution name collision, will retry on next SQS delivery",
            extra={"execution_name": execution_name, "source_key": source_key},
        )
        raise


def _handle_sqs_batch(event) -> dict:
    """Process an SQS batch and report per-record outcomes. p30 R9.3.

    Returns ``{"batchItemFailures": [{"itemIdentifier": <messageId>}, ...]}``.
    Both halves of R9.3 are required and neither works alone: this return value
    is ignored unless the event source mapping declares
    ``ReportBatchItemFailures`` (``functionResponseTypes`` on ``TriggerSqsMapping``
    in orchestration-stack.ts), and that declaration does nothing unless the
    handler stops raising. An exception escaping this function makes SQS treat
    the whole batch as failed *regardless of what was returned*, which is why
    every record is processed inside its own ``try``.

    ``itemIdentifier`` MUST be the record's ``messageId`` verbatim. Lambda
    silently ignores an identifier that does not match a message in the batch
    and treats that message as successful, so a derived key (the S3 object key,
    the execution name, a fingerprint) would look like partial-batch reporting
    while quietly discarding files.

    The empty list on full success is deliberate and is not the same as
    returning nothing: it states "all records succeeded" explicitly rather than
    relying on the absent-key default.
    """
    records = event.get("Records", [])
    if not records:
        logger.warning("No SQS records in event")
        return {"statusCode": 200, "body": "No records to process", "batchItemFailures": []}

    failed_message_ids: list[str] = []
    unreportable: Exception | None = None

    for record in records:
        try:
            _process_sqs_record(record)
        except Exception as exc:
            message_id = record.get("messageId")
            if not message_id:
                # This record failed and cannot be named in the response.
                # Dropping it would report a failure as a success and lose the
                # file outright; an empty or unmatched itemIdentifier is worse
                # still, because Lambda discards the whole response. So fail the
                # batch loudly instead — worse for the record's siblings, but the
                # only option that cannot lose data. Real SQS always supplies
                # messageId, so this is reachable only from a hand-built or
                # non-SQS invocation.
                logger.error(
                    "SQS record failed and carries no messageId; failing the whole batch "
                    "because the failure cannot be reported per-record",
                    extra={"error": str(exc), "record_keys": sorted(record.keys())},
                )
                if unreportable is None:
                    unreportable = exc
                continue

            if message_id not in failed_message_ids:
                failed_message_ids.append(message_id)
            logger.warning(
                "SQS record returned to the queue as a partial batch failure",
                extra={"message_id": message_id, "error": str(exc), "error_type": type(exc).__name__},
            )

    if unreportable is not None:
        raise unreportable

    succeeded = len(records) - len(failed_message_ids)
    return {
        # The invocation itself succeeded: with ReportBatchItemFailures the
        # per-record outcome is carried by batchItemFailures, not by failing.
        "statusCode": 200,
        "body": (
            f"Processed {len(records)} record(s): {succeeded} succeeded, "
            f"{len(failed_message_ids)} returned to the queue"
        ),
        "batchItemFailures": [{"itemIdentifier": mid} for mid in failed_message_ids],
    }


def lambda_handler(event, context):
    """Process SQS records containing EventBridge S3 events, or scheduled triggers."""
    # Manual trigger from the Run Now endpoint (direct Lambda invocation)
    if event.get("trigger") == "manual":
        return _handle_manual_trigger(event, context)

    # Check if this is a scheduled trigger (direct Lambda invocation from EventBridge)
    if event.get("trigger") == "scheduled":
        return _handle_scheduled_trigger(event, context)

    # SQS batch. Only this path returns batchItemFailures — the two direct
    # invocations above have no messages to report on.
    return _handle_sqs_batch(event)
