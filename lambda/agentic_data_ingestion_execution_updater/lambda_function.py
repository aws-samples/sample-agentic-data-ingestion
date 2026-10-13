# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Execution Updater Lambda — DynamoDB Stream handler.

Processes Stream records from the state table and upserts execution summary
rows in the agentic_data_ingestion_executions table. This provides a pre-aggregated view
of pipeline executions for fast dashboard queries.

Trigger: DynamoDB Stream (NEW_IMAGE) on the state table.
Output:  Upserts to agentic_data_ingestion_executions table.
"""

import os
import re
import sys
import time
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

# Lambda layer / local dev import paths
sys.path.append("/opt/python")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

from env_utils import require_env
from mapping_reuse import ANNOTATED_AT, annotation_is_latest_write
from step_names import STEP_SEQUENCE, StepName, from_any

logger = Logger(service="agentic_data_ingestion")

dynamodb = boto3.resource("dynamodb")
executions_table = dynamodb.Table(require_env("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"))
# NOTE: PIPELINES_TABLE env var must be added to the execution_updater Lambda
# in deploy-cdk/lib/compute-stack.ts (alongside EXECUTIONS_TABLE and STATE_TABLE).
pipelines_table = dynamodb.Table(require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"))
errors_table = dynamodb.Table(require_env("ERRORS_TABLE", "agentic_data_ingestion_errors"))

# ---------------------------------------------------------------------------
# Terminal steps — when these complete the whole execution is done
# ---------------------------------------------------------------------------
_FINAL_STEPS = frozenset(
    {
        "create_glue_pipeline",
        "create_quality_checks",
        "CreateGluePipeline",
        "CreateQualityChecks",
        "run_pipeline",
        "RunPipeline",
    }
)

# The self-healing agent's state rows (p30 R14.3). NOT a pipeline step: the
# outcome record keys on the ORIGINAL run's run_id and carries
# ``original_pipeline_id``, so it lands on that run's execution row — but the
# run's step sequence never contained it.
_SELF_HEALING_STEP = "self_healing"

# ---------------------------------------------------------------------------
# Step_Row classification (p37 R4.1 / R4.2 / R4.3) — an ALLOWLIST
# ---------------------------------------------------------------------------
# ``shared/step_names.py`` already DEFINES which state-table ``step_name``
# values are pipeline steps: ``STEP_SEQUENCE``'s ten state-row-writing steps,
# reachable from any cataloged variant through ``from_any`` (canonical
# snake_case, the PascalCase SFN state ids, and the legacy rows that persist
# in live tables — ``CodeGeneration``, ``CodeTesting``, ``infrastructure``,
# ``first_run``). Its module docstring names the values that are *records*
# rather than steps: ``pipeline_failure``, ``self_healing``, ``data_quality``,
# ``cache``.
#
# Keyed on that module rather than on a denylist of record types, because a
# denylist has to be extended for every new record type — and p37 C1 adds one
# (``load_metrics``). An allowlist keyed on the module that already defines
# what a step is makes that row safe by construction.
#
# The one deliberate non-step admission: the RecordFailure sentinel row.
# ``record_failure`` writes ``(run_id, "pipeline_failure")`` with
# ``status="error"`` and ``original_pipeline_id`` set to the real pipeline
# (``record_failure/lambda_function.py:432-447``), so it lands on the run's
# execution row and is the ONLY carrier of that run's terminal FAILED status
# whenever the failing step never got to write a row of its own — a Lambda
# timeout, ``States.Timeout`` parked at an approval gate, ``GlueJobFailed``.
# Every task state Catches to RecordFailure for exactly those cases (that
# handler's own docstring, cases 3/3a). Routing it away would leave those runs
# reading RUNNING forever and would stop ``_capture_error`` writing them to the
# errors table — the same inflated-running-count defect ``LGR-453f12d-5``
# (template / ratified) fixed for ``self_healing``, reintroduced by the fix for
# it. So it keeps today's handling verbatim. The run-detail reader already
# filters it out of a run's step list (``pipelines_runs.py:499``).
_STATUS_BEARING_RECORD_ROWS = frozenset({"pipeline_failure"})


def _to_step_or_none(value: str | None) -> StepName | None:
    """Canonicalize a state row's ``step_name``; ``None`` if it is not a Step_Row.

    **This classifier must never raise** (p37 risk N2). ``from_any`` raises
    ``ValueError`` on an unknown name *by contract*
    (``shared/step_names.py:228-231``) and :func:`lambda_handler` re-raises so
    DynamoDB Streams retries the batch (``:56-60``) — and every state row of a
    run shares the ``run_id`` partition key, so one unrecognised ``step_name``
    propagating out of here would stall the shard for the whole run. Same
    non-raising shape as ``shared/retry_utils.py:102-109``: unparseable ⇒ not a
    Step_Row ⇒ the caller returns.

    Members of ``STEP_SEQUENCE`` only. The three ``check_*`` Choice states parse
    but never write state rows, so they are not Step_Rows either.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        step = from_any(value)
    except ValueError:
        return None
    return step if step in STEP_SEQUENCE else None


def _drives_execution_row(step_name: str) -> bool:
    """True when a state row may derive a status onto the run's execution row.

    A Step_Row, or the one non-step record that carries a run's terminal status
    (see :data:`_STATUS_BEARING_RECORD_ROWS`). Everything else — a
    DQ_Report_Row, C1's ``load_metrics`` row, the metrics ``cache`` row, an
    unrecognised name — is routed away by :func:`_process_record` **before any
    status is derived** (``LGR-453f12d-5``).
    """
    return _to_step_or_none(step_name) is not None or step_name in _STATUS_BEARING_RECORD_ROWS


# Status normalization: raw Lambda status → dashboard-expected uppercase
_STATUS_MAP = {
    "success": "SUCCEEDED",
    "completed": "SUCCEEDED",
    "error": "FAILED",
    "failed": "FAILED",
    "running": "RUNNING",
    "in_progress": "RUNNING",
    "pending": "RUNNING",
    "waiting_approval": "WAITING_APPROVAL",
}


def lambda_handler(event, context):
    """Process DynamoDB Stream records and upsert execution summary rows."""
    logger.append_keys(step_name="ExecutionUpdater")
    records = event.get("Records", [])
    logger.info("Processing %d stream record(s)", len(records))

    for record in records:
        event_name = record.get("eventName")
        if event_name not in ("INSERT", "MODIFY"):
            logger.debug("Skipping %s event", event_name)
            continue

        try:
            _process_record(record)
        except Exception:
            logger.exception("Failed to process record: %s", record.get("eventID"))
            raise  # Let DynamoDB Streams retry the batch

    return {"statusCode": 200, "processed": len(records)}


# ---------------------------------------------------------------------------
# Record processing
# ---------------------------------------------------------------------------


def _process_record(record):
    """Extract fields from a Stream record and upsert the execution row."""
    new_image = record["dynamodb"].get("NewImage", {})

    run_id = _get_str(new_image, "run_id")
    pipeline_id = _get_str(new_image, "original_pipeline_id") or _get_str(new_image, "pipeline_id")
    step_name = _get_str(new_image, "step_name")
    raw_status = _get_str(new_image, "status")

    if not run_id or not pipeline_id:
        logger.warning("Skipping record — missing run_id or pipeline_id")
        return

    logger.append_keys(run_id=run_id, pipeline_id=pipeline_id)

    # p30 R14.3: agent records are routed away from the step machinery BEFORE
    # any status is derived. The self-healing outcome record reaches this
    # handler because it carries ``original_pipeline_id`` (which
    # ``pipeline_id`` above resolves first) keyed on the original run's
    # ``run_id`` — but it is not one of that run's steps, and letting it drive
    # the row's status actively lied: the record carries NO ``status``
    # attribute, so ``_derive_execution_status("self_healing", "")`` returned
    # RUNNING and a healed FAILED run showed up in the run list as
    # "RUNNING → self_healing" with ``steps_status.self_healing = UNKNOWN``.
    if step_name == _SELF_HEALING_STEP:
        _apply_self_healing_outcome(new_image, run_id, pipeline_id)
        return

    # p37 R4.3: every OTHER non-Step_Row returns here, before any status is
    # derived from it. Generalises the branch above from one record type to the
    # allowlist that ``shared/step_names.py`` already defines
    # (``LGR-453f12d-5``, template / ratified: "route non-step state rows away
    # from the step machinery before any status is derived"). It closes both
    # halves of the R4 defect at once, because the derivation is what produces
    # them: no ``steps_status`` entry (R4.1 — ``_normalize_status("")`` is
    # "UNKNOWN" at :515 for the DQ_Report_Row, which carries ``overall_status``
    # and no ``status``), and no write to ``#status`` or ``current_step`` (R4.2
    # — ``_derive_execution_status("data_quality", "")`` returns "RUNNING" at
    # :518-527 and the first update sets both unconditionally). Neither
    # ``_normalize_status`` nor ``_derive_execution_status`` is reached, so
    # neither needed changing: the blank-status arm is CORRECT for a genuine
    # step and stays as it is (do-NOT 5).
    if not _drives_execution_row(step_name):
        logger.info(
            "Skipping non-step state row: run=%s step_name=%s — not a Step_Row, "
            "no status derived and no execution-row write",
            run_id,
            step_name,
        )
        return

    # A Step_Row written without its run making progress: a user-triggered
    # mapping regeneration, or a later rejection revoking this row's mapping from
    # reuse (``shared/mapping_reuse.py``). Both stamp ``annotated_at``. Deriving a
    # status from them is what flipped a finished run back to RUNNING when the
    # user regenerated its mapping, because the regenerated row carries
    # ``status="success"`` and ``schema_mapping`` is not a final step.
    #
    # Skipped only while the annotation is the row's latest write. An approval
    # decision that lands afterwards sets a newer ``updated_at``, so its record
    # derives the run's status as it always has. Nothing is written here at all:
    # not ``steps_status``, not lineage, since neither write is run history.
    if annotation_is_latest_write(
        _get_str(new_image, ANNOTATED_AT),
        _get_str(new_image, "timestamp"),
        _get_str(new_image, "updated_at"),
    ):
        logger.info(
            "Skipping annotation write: run=%s step_name=%s — not progress of this run, "
            "no status derived and no execution-row write",
            run_id,
            step_name,
        )
        return

    # Normalize statuses to uppercase for dashboard compatibility
    step_status = _normalize_status(raw_status)
    execution_status = _derive_execution_status(step_name, raw_status)

    logger.info(
        "Upserting execution: pipeline=%s run=%s step=%s status=%s (exec=%s)",
        pipeline_id,
        run_id,
        step_name,
        step_status,
        execution_status,
    )

    now = _iso_now()
    key = {"pipeline_id": pipeline_id, "run_id": run_id}

    # ------------------------------------------------------------------
    # First update: set scalar fields and ensure steps_status map exists.
    # ------------------------------------------------------------------
    update_parts = [
        "#status = :status",
        "current_step = :step",
        "updated_at = :now",
        "#steps_status = if_not_exists(#steps_status, :empty_map)",
    ]
    expr_values = {
        ":status": execution_status,
        ":step": step_name,
        ":now": now,
        ":empty_map": {},
    }
    expr_names = {
        "#status": "status",
        "#steps_status": "steps_status",
    }

    # ------------------------------------------------------------------
    # p48 R9.2: EVERY step update sets `started_at`, not only the
    # file_validation INSERT.
    #
    # This update is an UPSERT (see the docstring) — it legitimately creates the
    # execution row, because stream events can arrive for a run whose row does
    # not exist yet or arrive out of order. Guarding it with
    # `attribute_exists(run_id)` the way the four approval-path writers are
    # guarded would therefore LOSE REAL RUNS, which is strictly worse than the
    # sparse-index exposure it would close. So the clause is hoisted out of the
    # `file_validation`/INSERT branch instead of the write being guarded.
    #
    # Why this matters now: `RunsByTimeIndex` ((pipeline_id, started_at), p48
    # C1) is SPARSE — a row carrying no `started_at` is absent from it and
    # therefore invisible in run history, with no error. Before this change, any
    # step OTHER than a `file_validation` INSERT could create the row, and 8 of
    # the 10 `started_at`-less rows measured live were exactly that. They render
    # today only because they carry `current_step` and `steps_status`, so the
    # phantom-row filter keeps them — the moment the read moves to the index they
    # would vanish silently.
    #
    # `if_not_exists` makes it idempotent and means it can never overwrite a real
    # `file_validation` timestamp. The value source is the record's OWN
    # `timestamp` (what the step Lambda wrote), falling back to `now` — the same
    # chain the `file_validation` branch has always used.
    #
    # The accepted cost, stated rather than hidden: when a later step's event
    # arrives BEFORE `file_validation`'s, `started_at` becomes that step's
    # instant instead of the run's true start. `if_not_exists` then refuses to
    # correct it. The error is bounded by the run's own duration and only occurs
    # on out-of-order delivery; the alternative is a row with no `started_at` at
    # all, which is unbounded invisibility rather than a bounded inaccuracy.
    # ------------------------------------------------------------------
    update_parts.append("started_at = if_not_exists(started_at, :started_at)")
    expr_values[":started_at"] = _get_str(new_image, "timestamp") or now

    # Set file_name and the run's provenance on the very first step
    if step_name == "file_validation" and record.get("eventName") == "INSERT":
        # p30 R14.3: carry the run's PROVENANCE onto the execution row.
        # file_validation persists the SFN input's trigger and (for reruns)
        # original_run_id on this identity row, but nothing propagated them to
        # the executions table — so a self-healing rerun appeared in the run
        # list as an ordinary run with no hint of where it came from, while the
        # dashboard's own retry path has shown a badge and a source link since
        # P16 (LGR-2600abb-1) because it writes its execution row itself.
        # ``trigger`` is a DynamoDB reserved word, hence the #alias.
        # ``if_not_exists`` so a row a producer wrote directly (the retry path's
        # put_item) keeps its own values.
        triggered_by = _get_str(new_image, "triggered_by") or _get_str(new_image, "trigger")
        if triggered_by:
            update_parts.append("#trigger = if_not_exists(#trigger, :triggered_by)")
            expr_values[":triggered_by"] = triggered_by
            expr_names["#trigger"] = "trigger"

        original_run_id = _get_str(new_image, "original_run_id")
        if original_run_id:
            update_parts.append("original_run_id = if_not_exists(original_run_id, :original_run_id)")
            expr_values[":original_run_id"] = original_run_id

        file_name = _get_str(new_image, "file_name")
        if not file_name:
            # Prefer original_s3_uri (actual uploaded file) over json_s3_uri (converted)
            s3_uri = _get_str(new_image, "original_s3_uri") or _get_str(new_image, "json_s3_uri")
            if s3_uri:
                file_name = s3_uri.split("/")[-1]
        update_parts.append("file_name = if_not_exists(file_name, :file_name)")
        expr_values[":file_name"] = file_name or ""

        # Fix 2: Look up ai_model from pipeline metadata
        try:
            pipeline_resp = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})
            pipeline_meta = pipeline_resp.get("Item", {})
            ai_model_from_pipeline = pipeline_meta.get("ai_model", "")
            if ai_model_from_pipeline:
                update_parts.append("ai_model_used = if_not_exists(ai_model_used, :ai_model)")
                expr_values[":ai_model"] = ai_model_from_pipeline
        except Exception as e:
            logger.warning("Failed to look up pipeline metadata for ai_model: %s", e)

    # Always try to set execution_id if present
    execution_id = _get_str(new_image, "execution_id")
    if execution_id:
        update_parts.append("execution_id = if_not_exists(execution_id, :exec_id)")
        expr_values[":exec_id"] = execution_id

    # Fallback: set ai_model from stream record if not already set by pipeline lookup
    ai_model = _get_str(new_image, "ai_model_used") or _get_str(new_image, "ai_model")
    if ai_model and ":ai_model" not in expr_values:
        update_parts.append("ai_model_used = if_not_exists(ai_model_used, :ai_model)")
        expr_values[":ai_model"] = ai_model

    # Fix 3: Set schema_version if present (from schema_extract or schema_compare steps)
    schema_version = _get_str(new_image, "schema_version")
    if not schema_version:
        # Try to get it from output_data
        output_data = new_image.get("output_data", {}).get("M", {})
        summary = output_data.get("summary", {}).get("M", {})
        schema_version = summary.get("schema_version", {}).get("S", "") or summary.get("schema_version", {}).get(
            "N", ""
        )
    if schema_version:
        update_parts.append("schema_version = if_not_exists(schema_version, :schema_version)")
        expr_values[":schema_version"] = schema_version

    # Denormalize schema_hash onto execution record (Task 17.1)
    # When processing schema_extract step, write schema_hash directly to executions table
    if step_name == "schema_extract":
        schema_hash = _get_str(new_image, "schema_hash")
        if schema_hash:
            update_parts.append("schema_hash = :schema_hash")
            expr_values[":schema_hash"] = schema_hash

    # Set completed_at when a terminal step reaches a terminal status
    if raw_status in ("success", "completed", "failed", "error", "cancelled") and _is_final_step(step_name):
        update_parts.append("completed_at = :now")

    # A CANCELLED run stays cancelled. The dashboard's cancel stops the Step
    # Functions execution, but not a step Lambda already running, and that
    # Lambda's state-row write still arrives here afterwards; deriving from it
    # would turn the run back into RUNNING, SUCCEEDED or FAILED. CANCELLING is
    # deliberately NOT protected: while a Glue stop is pending the run can still
    # end otherwise (the job succeeding before the stop lands), and that outcome
    # must reach the row. The condition admits a row that does not exist yet,
    # which keeps the upsert this write has to be (see p48 R9.2 above).
    expr_values[":cancelled"] = "CANCELLED"
    try:
        executions_table.update_item(
            Key=key,
            UpdateExpression="SET " + ", ".join(update_parts),
            ConditionExpression="attribute_not_exists(#status) OR #status <> :cancelled",
            ExpressionAttributeValues=expr_values,
            ExpressionAttributeNames=expr_names,
        )
    except ClientError as write_error:
        if write_error.response.get("Error", {}).get("Code", "") != "ConditionalCheckFailedException":
            raise
        logger.info(
            "Skipping step write for cancelled run: run=%s step_name=%s status=%s",
            run_id,
            step_name,
            raw_status,
        )
        return

    # ------------------------------------------------------------------
    # Second update: set the nested step status inside the map.
    # ------------------------------------------------------------------
    executions_table.update_item(
        Key=key,
        UpdateExpression="SET #steps_status.#step_name = :step_status",
        ExpressionAttributeValues={":step_status": step_status},
        ExpressionAttributeNames={
            "#steps_status": "steps_status",
            "#step_name": step_name,
        },
    )

    # ------------------------------------------------------------------
    # Error capture: write to agentic_data_ingestion_errors on failure
    # ------------------------------------------------------------------
    if raw_status in ("failed", "error"):
        _capture_error(new_image, run_id, pipeline_id, step_name)

    # ------------------------------------------------------------------
    # Lineage: append a lineage node on successful step completion
    # ------------------------------------------------------------------
    node = _build_lineage_node(step_name, new_image)
    if node and raw_status in ("success", "completed"):
        executions_table.update_item(
            Key=key,
            UpdateExpression="SET lineage = list_append(if_not_exists(lineage, :empty_list), :node)",
            ExpressionAttributeValues={":empty_list": [], ":node": [node]},
        )


# ---------------------------------------------------------------------------
# Self-healing visibility (p30 R14.3)
# ---------------------------------------------------------------------------


def _apply_self_healing_outcome(new_image: dict, run_id: str, pipeline_id: str) -> None:
    """Record the heal on the ORIGINAL run's execution row (p30 R14.3).

    R14.3 requires a heal to be visible in the pipeline run list, not only on
    the failed run's detail page. The run list reads the executions table, so
    the heal has to be denormalized onto the row for the run that was healed —
    which is this record's own ``run_id``, since the agent keys its outcome
    record on the run it is healing.

    **Four distinct states, deliberately not collapsed into one "healed"
    flag.** ``rerun_status`` is copied through verbatim rather than reduced,
    because three of the four would be misreported by a boolean:

    * ``running`` — ``start_execution`` succeeded and the re-run is in flight.
      Its outcome is NOT known yet.
    * ``success`` / ``failed`` — the re-run reached a terminal Glue state and
      its own ``job_completion`` linked the result back onto the agent's
      record (``_link_heal_rerun_outcome``). That link is a state-row
      ``update_item``, i.e. a MODIFY stream event, which is what carries the
      transition through to this row.
    * ``start_failed`` — a fix may have been applied but no re-run exists.
    * ``skipped_partial_write_guard`` — the Partial_Write_Guard (p30 R15.2)
      declined to re-run because the target duplicates on re-run.

    ``skipped_heal_guard`` never reaches here: the Heal_Guard skip record
    carries neither ``pipeline_id`` nor ``original_pipeline_id``, so
    ``_process_record`` drops it before this point. A heal-triggered run is
    already identifiable in the list by its own ``trigger="self_healing"``.

    **The forward link waits for ``linked_run_id``.** ``rerun_id`` is
    ``{original}-heal``, which is lineage only — ``file_validation`` generates
    a FRESH run_id for the re-run's state rows, so ``{original}-heal`` is not
    a run anything can navigate to. ``linked_run_id`` is that fresh id and only
    exists once the re-run terminates, so the list gains a link to the re-run
    at that point and not before.

    Update-only (``attribute_exists(run_id)``): a heal for a run with no
    execution row must log rather than spawn a phantom row keyed on an
    ``original_pipeline_id`` that may not be a pipeline at all.
    """
    update_parts = [
        "self_healing_status = :sh_status",
        "self_healing_remediation = :sh_remediation",
        "self_healing_error_class = :sh_error_class",
        "updated_at = :now",
    ]
    expr_values = {
        # Verbatim, not reduced — see the four-states note above. "unknown"
        # only when the agent wrote a record with no rerun_status at all.
        ":sh_status": _get_str(new_image, "rerun_status") or "unknown",
        ":sh_remediation": _get_str(new_image, "remediation_applied"),
        ":sh_error_class": _get_str(new_image, "error_classification"),
        ":now": _iso_now(),
    }

    linked_run_id = _get_str(new_image, "linked_run_id")
    if linked_run_id:
        update_parts.append("self_healing_linked_run_id = :sh_linked_run_id")
        expr_values[":sh_linked_run_id"] = linked_run_id

    skip_reason = _get_str(new_image, "skip_reason")
    if skip_reason:
        update_parts.append("self_healing_skip_reason = :sh_skip_reason")
        expr_values[":sh_skip_reason"] = skip_reason

    try:
        executions_table.update_item(
            Key={"pipeline_id": pipeline_id, "run_id": run_id},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(run_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # Permanent — retrying the batch cannot make the row appear.
            logger.warning(
                "No execution row for run=%s pipeline=%s — self-healing outcome not surfaced in the run list",
                run_id,
                pipeline_id,
            )
            return
        raise

    logger.info(
        "Surfaced self-healing outcome on execution row: run=%s status=%s",
        run_id,
        expr_values[":sh_status"],
    )


# ---------------------------------------------------------------------------
# Error capture
# ---------------------------------------------------------------------------


def _capture_error(new_image: dict, run_id: str, pipeline_id: str, step_name: str):
    """Write a failed step to the agentic_data_ingestion_errors table."""
    # Extract error message from multiple possible locations
    error_message = _get_str(new_image, "error_message")
    if not error_message:
        # Try output_data.error
        output_data = new_image.get("output_data", {}).get("M", {})
        error_message = output_data.get("error", {}).get("S", "")
    if not error_message:
        error_message = "Unknown error"

    now = datetime.now(UTC)
    try:
        errors_table.put_item(
            Item={
                "error_date": now.strftime("%Y-%m-%d"),
                "timestamp": f"{now.isoformat()}#{run_id}",
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "step_name": step_name,
                "error_type": classify_error(error_message),
                "error_message": (error_message or "")[:1000],
                "ttl": int(time.time()) + 90 * 86400,
            }
        )
        logger.info("Captured error for run=%s step=%s type=%s", run_id, step_name, classify_error(error_message))
    except Exception:
        logger.exception("Failed to capture error for run=%s step=%s", run_id, step_name)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = {
    # New specific categories (evaluated first — Python 3.7+ dicts maintain insertion order)
    "authentication": re.compile(
        r"ExpiredToken|InvalidSignature|SignatureDoesNotMatch|TokenExpired|UnrecognizedClientException",
        re.IGNORECASE,
    ),
    "schema_mismatch": re.compile(
        r"SchemaMismatch|field type conflict|incompatible schema|column mismatch",
        re.IGNORECASE,
    ),
    "resource_quota": re.compile(
        r"LimitExceeded|ServiceQuotaExceeded|ProvisionedThroughputExceeded|ItemCollectionSizeLimitExceeded",
        re.IGNORECASE,
    ),
    "data_quality": re.compile(
        r"QualityCheck|quality rule|data quality|constraint violation",
        re.IGNORECASE,
    ),
    "network": re.compile(
        r"ConnectionTimeout|ConnectTimeoutError|DNSError|EndpointConnectionError|ConnectionRefused",
        re.IGNORECASE,
    ),
    # Existing categories (preserved)
    "permission": re.compile(r"AccessDenied|Unauthorized|Forbidden", re.IGNORECASE),
    "timeout": re.compile(r"Timeout|TimedOut|TimeoutError", re.IGNORECASE),
    "data_format": re.compile(r"JSONDecodeError|CSV|schema|parse|ParseError", re.IGNORECASE),
    "code_error": re.compile(r"SyntaxError|NameError|TypeError|ValueError", re.IGNORECASE),
    "infrastructure": re.compile(r"ResourceNotFound|BucketNotFound|NoSuchBucket|NoSuchKey", re.IGNORECASE),
    "throttling": re.compile(r"Throttling|TooManyRequests|ThrottlingException", re.IGNORECASE),
}


def classify_error(error_message: str) -> str:
    """Classify an error message into a category based on known patterns."""
    if not error_message:
        return "unknown"
    for category, pattern in _ERROR_PATTERNS.items():
        if pattern.search(error_message):
            return category
    return "unknown"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lineage extraction
# ---------------------------------------------------------------------------

_LINEAGE_EXTRACTORS = {
    "file_validation": ("source_file", "original_s3_uri", True),
    "schema_extract": ("schema", "schema_hash", False),
    "schema_mapping": ("mapping", "mapping_id", False),
    # P28 R8.2 (task 14.2): step Lambdas now write canonical snake_case step
    # names. The legacy PascalCase keys are kept because this is a Stream
    # consumer — rows written before the rename can still be MODIFY'd and
    # must keep producing lineage.
    "create_code": ("code_version", "code_version_id", False),
    "CodeGeneration": ("code_version", "code_version_id", False),
    "run_pipeline": ("glue_job_run", "glue_job_run_id", False),
    "RunPipeline": ("glue_job_run", "glue_job_run_id", False),
}


def _build_lineage_node(step_name, new_image):
    """Build a lineage node dict for a completed pipeline step.

    Returns None if the step is not tracked or the artifact identifier
    is missing from the Stream record.
    """
    config = _LINEAGE_EXTRACTORS.get(step_name)
    if not config:
        return None
    artifact_type, field_name, extract_filename = config
    raw_value = _get_str(new_image, field_name)
    if not raw_value:
        # Try output_data
        output_data = new_image.get("output_data", {}).get("M", {})
        raw_value = output_data.get(field_name, {}).get("S", "")
    if not raw_value:
        logger.warning("No %s found for lineage in step %s", field_name, step_name)
        return None
    artifact_id = raw_value.split("/")[-1] if extract_filename else raw_value
    s3_uri = raw_value if extract_filename else _get_str(new_image, "json_s3_uri") or ""
    return {
        "step_name": step_name,
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "s3_uri": s3_uri,
        "timestamp": _get_str(new_image, "timestamp") or _iso_now(),
    }


def _normalize_status(raw: str) -> str:
    """Normalize a raw status string to dashboard-expected uppercase format."""
    return _STATUS_MAP.get(raw.lower(), raw.upper()) if raw else "UNKNOWN"


def _derive_execution_status(step_name: str, raw_status: str) -> str:
    """Map a step name + raw status to an execution-level status (uppercase)."""
    s = raw_status.lower()
    # ``rejected`` is the approval-decision echo's status (dashboard reject path
    # and ``approval_handler``). A rejection ends the run, and falling through to
    # RUNNING raced the reject path's own FAILED annotation of the same row.
    if s in ("failed", "error", "rejected"):
        return "FAILED"
    # ``cancelled`` is job_completion's run_pipeline status for a Glue job a
    # dashboard cancel stopped; it ends the run.
    if s == "cancelled":
        return "CANCELLED"
    if s == "waiting_approval":
        return "WAITING_APPROVAL"
    if s in ("success", "completed") and _is_final_step(step_name):
        return "SUCCEEDED"
    return "RUNNING"


def _is_final_step(step_name: str) -> bool:
    """Return True if *step_name* is a terminal pipeline step."""
    return step_name in _FINAL_STEPS


def _get_str(image: dict, key: str) -> str:
    """Extract a string value from a DynamoDB Stream image attribute."""
    return image.get(key, {}).get("S", "")


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()
