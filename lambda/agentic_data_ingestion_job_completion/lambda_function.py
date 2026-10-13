# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.exceptions import ClientError

# boto3-stubs are a dev dependency and are not on the Lambda's runtime path, so
# the Glue TypeDef is imported for the checker only — the same shape
# ``create_infrastructure/lambda_function.py:11-13`` uses for its own
# ``glue:UpdateTable`` call.
if TYPE_CHECKING:
    from mypy_boto3_glue.type_defs import TableInputTypeDef

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

# Initialize logger
from aws_lambda_powertools import Logger

# P28 R19.2: single shared float→Decimal converter (replaces the local copy).
from config import PROCESSED_FILE_LEDGER_TTL_SECONDS
from decimal_utils import convert_floats_to_decimal
from dynamo_typing import item_dict, item_str
from env_utils import get_aws_account_id, require_env

# p35 R7.3: the two shared definitions `_populate_mirror_metadata_location`
# needs. `glue_mirror_identifiers` is the ONE derivation of the mirror's
# (database, table) pair — `LGR-acca05d-1`'s revisit_when is precisely
# "create_infrastructure changes either derivation", so this writer is a caller
# rather than a second copy of `create_infrastructure:272-273`.
# `glue_table_input_from_stored` is the ONE allowlist of the keys
# `glue:UpdateTable` accepts inside a `TableInput`, shared with
# `create_infrastructure` because both Lambdas read-modify-write the SAME mirror
# table and two allowlists that disagree would silently produce two different
# tables from one read.
from glue_mirror_identifiers import glue_mirror_identifiers
from glue_table_input import glue_table_input_from_stored

# p39 R2.1: the per-run Glue cost publisher. Imported unguarded like every other
# shared module above it — this Lambda's layer carries `shared/`, and a guard
# would turn a missing layer into a silently mute metric rather than an import
# error. The publisher is itself best-effort (it logs and never raises).
from metrics_facade import publish_glue_run_cost, publish_run_outcome

# p30 R9.1: this handler is the ledger's only WRITER. The fingerprint's
# composition and the row's key live in shared/ because pipeline_trigger reads
# what this writes — see that module's docstring.
from processed_file_ledger import record_processed_file
from step_names import StepName

logger = Logger(service="agentic_data_ingestion")

# Step Functions error codes meaning the task token was already consumed or
# expired (same classification as the approvals handler, P28 R2.3). On a
# duplicate EventBridge delivery the record still exists but the first
# delivery consumed the token — these must terminate cleanly, not raise.
_TOKEN_CONSUMED_ERROR_CODES = frozenset({"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"})


class RecordNotFoundError(Exception):
    """No token record (yet) matches a Glue job completion event (P28 R5.2).

    Raised instead of returning an error-shaped payload. EventBridge invokes
    this function asynchronously, so a returned dict is a successful
    invocation whatever it says; only a raised exception is a function error.
    Lambda's async retry then runs the event twice more, 1 and 2 minutes
    later, which covers the residual race where the completion event beats
    run_pipeline's update_item attaching glue_job_run_id (GlueJobRunIndex GSI
    propagation). An event still unmatched after the retries goes to the
    function's on-failure destination, the Glue job monitor DLQ
    (``orchestration-stack.ts``), whose depth alarm pages.

    The EventBridge target's own retry policy and DLQ do not cover these:
    they apply when EventBridge fails to hand the event to Lambda, not when
    the function fails afterwards. Until 2026-10-10 this docstring said
    otherwise, and no on-failure destination existed, so an exhausted event
    was discarded without reaching the DLQ.
    """


class TaskTokenMissingError(Exception):
    """The run is still active, but its Glue-callback task token is gone.

    Neither the task-tokens row nor the legacy state-row copy holds a token, and
    the state row is not terminal, so this is not a duplicate delivery. run_pipeline
    persists the token BEFORE start_job_run (LGR-0ec143c-1), so this is lost or
    expired state, not a startup race. Raised rather than returned for the reason
    RecordNotFoundError gives: a returned dict is a successful asynchronous
    invocation, so the old ``{"statusCode": 400}`` was acknowledged and never
    reached the DLQ, and nothing paged while the execution waited for its task
    timeout. Raised, Lambda retries twice (a second ConsistentRead) and then
    delivers the event to the Glue job monitor DLQ, whose depth alarm pages.
    """


# Environment variables
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
PROCESSED_FILES_TABLE = require_env("PROCESSED_FILES_TABLE", "agentic_data_ingestion_processed_files")
TASK_TOKENS_TABLE = require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Key prefix of run_pipeline's Glue-callback token row in the task-tokens
# table — the same literal as run_pipeline's GLUE_RUN_TOKEN_KEY_PREFIX (the
# two Lambdas share no module for it; tests/unit/lambdas/job_completion/
# test_job_completion_token_table.py pins them equal). See that module for
# why the token lives there and not on the state row.
GLUE_RUN_TOKEN_KEY_PREFIX = "glue_run#"  # a table key prefix, not a secret  # nosec B105

# The statuses this handler's own update_item writes onto the run_pipeline
# state row. A row already in one of them, with no token left to read, is a
# duplicate delivery whose first copy consumed the token and deleted its row.
# ``cancelled`` is written by _finish_cancelled_run.
_TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled"})

# The run_pipeline state-row status for a run whose Glue job was stopped by a
# dashboard cancel. execution_updater derives CANCELLED from it.
_CANCELLED_RUN_STATUS = "cancelled"

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
glue_client = boto3.client("glue")
sfn_client = boto3.client("stepfunctions")
lambda_client = boto3.client("lambda")

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)
processed_files_table = dynamodb.Table(PROCESSED_FILES_TABLE)
task_tokens_table = dynamodb.Table(TASK_TOKENS_TABLE)


def glue_run_token_key(run_id: str) -> str:
    """The task-tokens table key holding a run's Glue-callback token."""
    return f"{GLUE_RUN_TOKEN_KEY_PREFIX}{run_id}"


def _resolve_task_token(run_id: str, pipeline_run: dict[str, Any]) -> tuple[str, bool, dict[str, Any]]:
    """Return ``(task_token, from_token_table, token_row)`` for the run the GSI lookup found.

    The token row is the source. ``ConsistentRead`` so that a miss is a real
    miss: for a run that is not terminal, it falls through to the fallback and
    then to ``TaskTokenMissingError``, which Lambda retries and then
    dead-letters. ``token_row`` is the row itself, ``{}`` when absent; it also
    carries the dashboard's cancel marker (:func:`_cancel_marker_execution`).

    A read error propagates, and the handler's outer ``except`` re-raises it
    like any other DynamoDB failure on this path, so Lambda retries the
    invocation. It is never answered with the fallback, which would hide a
    broken token-table read behind a 400.
    """
    token_item = task_tokens_table.get_item(Key={"approval_id": glue_run_token_key(run_id)}, ConsistentRead=True).get(
        "Item"
    )
    if token_item:
        return item_str(token_item, "task_token"), True, token_item

    # FALLBACK — exists only for runs in flight at deploy time, whose
    # run_pipeline predates the token table and stored the token on the state
    # row. Remove it once no run that started before the deploy can still be
    # waiting, i.e. once the deploy is older than the longest RunPipeline task
    # timeout (TimeoutSecondsPath, defaulted by ApplyDefaultJobTimeout in
    # deploy-cdk/lib/orchestration-stack.ts).
    legacy_token = item_str(pipeline_run, "task_token")
    if legacy_token:
        logger.warning(f"No task-token row for run {run_id}; using the token stored on its state row (pre-deploy run)")
    return legacy_token, False, {}


def _delete_task_token_row(run_id: str) -> None:
    """Delete a run's token row after a confirmed callback. Best-effort.

    A failure leaves a spent token that the row's TTL reaps; it must not turn
    a delivered callback into an error.
    """
    try:
        task_tokens_table.delete_item(Key={"approval_id": glue_run_token_key(run_id)})
    except Exception as delete_error:
        logger.warning(f"Failed to delete the task-token row for run {run_id}: {delete_error}")


def _cancel_marker_execution(token_row: dict[str, Any], job_run_id: str, state: str) -> str:
    """The execution to stop when a dashboard cancel stopped this Glue job run, else ``""``.

    Two writers leave the marker. ``cancel_run`` writes it with
    ``cancel_glue_job_run_id`` when it stops a running job, and that marker
    applies only to the job run it names. ``cancel_run`` also writes it WITHOUT
    a job run id when it finds no job yet (the cancel fence, assessment F1); if
    the launcher then started a job anyway, it stopped that job itself and
    stamped its id onto the same row as ``glue_job_run_id``, which is the
    identity checked here. Neither applies to a SUCCEEDED run: a job that
    finished before the stop took effect has loaded its data, so the run closes
    as a success through the ordinary path.
    """
    if state == "SUCCEEDED" or not token_row.get("cancel_requested_at"):
        return ""
    named = item_str(token_row, "cancel_glue_job_run_id")
    if named:
        return item_str(token_row, "cancel_execution_arn") if named == job_run_id else ""
    # A fence with no job named: the job was started under it by the launcher.
    if item_str(token_row, "glue_job_run_id") not in ("", job_run_id):
        return ""
    return item_str(token_row, "cancel_execution_arn")


def _finish_cancelled_run(
    run_id: str,
    pipeline_id: str,
    record_step_name: str,
    job_name: str,
    job_run_id: str,
    state: str,
    execution_arn: str,
) -> dict[str, Any]:
    """Close a run whose Glue job a dashboard cancel stopped.

    1. Stop the execution, which has been waiting on this callback. Its ABORTED
       event is what releases the pipeline lock (SfnTerminalLockRelease), so the
       lock is freed now, with the job stopped, and not when the cancel was
       requested. An aborted execution runs no Catch, so RecordFailure writes no
       failure record and publishes no error-rate datapoint.
    2. Write the run_pipeline state row ``cancelled``; execution_updater derives
       CANCELLED from it. Conditional, so a redelivery does not publish the cost
       twice.
    3. Delete the token row and publish the run's cost: Glue bills a stopped run.

    No task callback is sent and no self-healing is dispatched; the run was
    stopped on purpose. Retry-safe: the describe makes the stop idempotent, and
    each write is idempotent or conditional.
    """
    execution_status: str
    try:
        execution_status = str(sfn_client.describe_execution(executionArn=execution_arn).get("status", ""))
    except ClientError as describe_error:
        if describe_error.response.get("Error", {}).get("Code", "") != "ExecutionDoesNotExist":
            raise
        execution_status = ""
    if execution_status == "RUNNING":
        sfn_client.stop_execution(
            executionArn=execution_arn,
            error="UserCancelled",
            cause=f"Glue job {job_name} {state} after a cancel from the dashboard",
        )
        logger.info(f"Stopped execution {execution_arn} for cancelled run {run_id}")

    metrics = collect_job_metrics(job_name, job_run_id)
    first_close = True
    try:
        pipeline_runs_table.update_item(
            Key={"run_id": run_id, "step_name": record_step_name},
            UpdateExpression="SET #status = :status, completed_at = :timestamp, output_data = :metrics",
            ConditionExpression="attribute_not_exists(#status) OR #status <> :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=convert_floats_to_decimal(
                {
                    ":status": _CANCELLED_RUN_STATUS,
                    ":timestamp": datetime.now(UTC).isoformat(),
                    ":metrics": metrics,
                }
            ),
        )
    except ClientError as write_error:
        if write_error.response.get("Error", {}).get("Code", "") != "ConditionalCheckFailedException":
            raise
        first_close = False
        logger.warning(f"Run {run_id} is already closed as cancelled; duplicate delivery")

    _delete_task_token_row(run_id)
    if first_close:
        _publish_run_cost(run_id, pipeline_id, state, metrics)
    return {
        "statusCode": 200,
        "message": f"Cancelled run closed: Glue job {state}",
        "job_run_id": job_run_id,
        "pipeline_id": pipeline_id,
    }


def collect_job_metrics(job_name: str, job_run_id: str) -> dict[str, Any]:
    """
    Collect comprehensive metrics from completed Glue job.

    Args:
        job_name: Name of the Glue job
        job_run_id: Job run ID

    Returns:
        dict: Job metrics including timing, resources, cost, and data metrics
    """
    try:
        # Get job run details
        response = glue_client.get_job_run(JobName=job_name, RunId=job_run_id)
        job_run = response["JobRun"]

        # Extract timing metrics
        started_on = job_run.get("StartedOn")
        completed_on = job_run.get("CompletedOn")
        execution_time_seconds = job_run.get("ExecutionTime", 0)

        # Extract resource metrics
        max_capacity = job_run.get("MaxCapacity", 0)
        number_of_workers = job_run.get("NumberOfWorkers", 0)
        worker_type = job_run.get("WorkerType", "Unknown")

        # Calculate DPU-hours
        dpu_per_worker = {"G.1X": 1.0, "G.2X": 2.0, "G.4X": 4.0, "G.8X": 8.0, "Z.2X": 2.0}

        if number_of_workers > 0:
            total_dpu = number_of_workers * dpu_per_worker.get(worker_type, 1.0)
        else:
            total_dpu = max_capacity

        dpu_hours = (total_dpu * execution_time_seconds) / 3600.0

        # Calculate actual cost (Glue 5.0 pricing: $0.44 per DPU-hour)
        cost_per_dpu_hour = 0.44
        actual_cost = dpu_hours * cost_per_dpu_hour

        # Apply 10-minute minimum billing
        minimum_dpu_hours = (total_dpu * 600) / 3600.0  # 10 minutes
        if dpu_hours < minimum_dpu_hours:
            actual_cost = minimum_dpu_hours * cost_per_dpu_hour
            dpu_hours = minimum_dpu_hours

        # Extract error information if failed
        error_message = job_run.get("ErrorMessage", "")

        metrics = {
            "job_name": job_name,
            "job_run_id": job_run_id,
            "status": job_run.get("JobRunState", "UNKNOWN"),
            # Timing metrics
            "started_at": started_on.strftime("%Y-%m-%dT%H:%M:%SZ") if started_on else None,
            "completed_at": completed_on.strftime("%Y-%m-%dT%H:%M:%SZ") if completed_on else None,
            "execution_time_seconds": execution_time_seconds,
            "duration_minutes": round(execution_time_seconds / 60.0, 2),
            # Resource metrics
            "worker_type": worker_type,
            "number_of_workers": number_of_workers,
            "max_capacity": max_capacity,
            "total_dpu": total_dpu,
            "dpu_hours": round(dpu_hours, 4),
            # Cost metrics
            "actual_cost": round(actual_cost, 3),
            "cost_per_dpu_hour": cost_per_dpu_hour,
            # Error information
            "error_message": error_message if error_message else None,
            # Glue version
            "glue_version": job_run.get("GlueVersion", "Unknown"),
        }

        logger.info(f"Collected metrics: {json.dumps(metrics, default=str)}")
        return metrics

    except Exception as e:
        logger.error(f"Error collecting job metrics: {e}")
        return {"job_name": job_name, "job_run_id": job_run_id, "status": "UNKNOWN", "error": str(e)}


def calculate_performance_comparison(metrics: dict[str, Any], pipeline_id: str) -> dict[str, Any]:
    """
    Compare actual performance vs estimated from worker recommendation.

    Args:
        metrics: Actual job metrics
        pipeline_id: Pipeline identifier

    Returns:
        dict: Performance comparison metrics
    """
    try:
        # Retrieve estimated metrics from pipeline metadata
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            return {}

        pipeline_item = response["Item"]
        worker_config = item_dict(pipeline_item, "worker_configuration")
        cost_info = item_dict(pipeline_item, "cost_information")

        estimated_duration = worker_config.get("estimated_duration_minutes", 0)
        estimated_cost = cost_info.get("estimated_cost_per_run", 0)

        actual_duration = float(metrics.get("duration_minutes", 0))
        actual_cost = float(metrics.get("actual_cost", 0))

        # Calculate variances
        duration_variance: float = 0.0
        cost_variance: float = 0.0

        if estimated_duration > 0:
            duration_variance = ((actual_duration - float(estimated_duration)) / float(estimated_duration)) * 100

        if estimated_cost > 0:
            cost_variance = ((actual_cost - float(estimated_cost)) / float(estimated_cost)) * 100

        comparison = {
            "estimated_duration_minutes": estimated_duration,
            "actual_duration_minutes": actual_duration,
            "duration_variance_percent": round(duration_variance, 2),
            "estimated_cost": float(estimated_cost),
            "actual_cost": actual_cost,
            "cost_variance_percent": round(cost_variance, 2),
            "mcp_recommendation_accuracy": round(100 - abs(duration_variance), 2) if estimated_duration > 0 else 0,
        }

        logger.info(f"Performance comparison: {json.dumps(comparison, default=str)}")
        return comparison

    except Exception as e:
        logger.error(f"Error calculating performance comparison: {e}")
        return {}


def _link_heal_rerun_outcome(run_id: str, state: str) -> None:
    """Link a heal rerun's terminal result back to the original run's healing record (P28 R9.7).

    The self-healing agent records ``rerun_started`` + ``rerun_status="running"``
    the moment it successfully starts a heal rerun; THIS handler — processing
    that rerun's own Glue completion — is the linker. The rerun's
    file_validation identity row carries ``triggered_by="self_healing"`` and
    ``original_run_id`` (persisted from the rerun-contract SFN input), which
    resolves the ORIGINAL run whose ``self_healing`` state record gets the
    outcome: ``rerun_result`` (SUCCEEDED/FAILED), ``linked_run_id`` (the
    rerun's fresh run_id), and ``rerun_status`` → success|failed.

    Best-effort by design: the Step Functions callback is the run's source of
    truth and has already been sent — a linking failure must never fail
    job_completion. Update-only (ConditionExpression): a missing healing
    record (agent crashed before writing it) logs instead of spawning a
    phantom ``self_healing`` row.
    """
    original_run_id = ""
    try:
        fv_item = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"}).get("Item") or {}
        if item_str(fv_item, "triggered_by") != "self_healing":
            return
        original_run_id = item_str(fv_item, "original_run_id")
        if not original_run_id:
            logger.warning(
                f"Heal rerun {run_id} carries no original_run_id on its file_validation "
                f"row — cannot link the rerun result to a healing record"
            )
            return

        rerun_result = "SUCCEEDED" if state == "SUCCEEDED" else "FAILED"
        pipeline_runs_table.update_item(
            Key={"run_id": original_run_id, "step_name": "self_healing"},
            ConditionExpression="attribute_exists(run_id)",
            UpdateExpression=(
                "SET rerun_result = :res, linked_run_id = :lrid, rerun_status = :rst, rerun_completed_at = :ts"
            ),
            ExpressionAttributeValues={
                ":res": rerun_result,
                ":lrid": run_id,
                ":rst": "success" if state == "SUCCEEDED" else "failed",
                ":ts": datetime.now(UTC).isoformat(),
            },
        )
        logger.info(f"Linked heal rerun {run_id} result {rerun_result} to healing record of run {original_run_id}")
    except ClientError as link_error:
        if link_error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(
                f"No healing record exists for original run {original_run_id} — heal rerun {run_id} result not linked"
            )
        else:
            logger.error(f"Failed to link heal rerun outcome for run {run_id}: {link_error}")
    except Exception as link_error:
        logger.error(f"Failed to link heal rerun outcome for run {run_id}: {link_error}")


def _commit_target_schema_version(run_id: str, pipeline_id: str, state: str) -> None:
    """Commit this run's observed target schema version — only on success (p30 R8.4).

    ``schema_compare`` used to advance ``target_schema_version`` in the
    pipelines table the moment it detected a target-schema change, at step 3 of
    9. If the run then failed, the pipelines table claimed a version that had
    never been mapped against or written to: the *next* run compared against a
    version that did not reflect reality, saw no change, and skipped the remap
    the failure had left outstanding. The drift the check exists to catch became
    invisible. R8.4 requires the version to be committed only after a successful
    run, so a failure leaves the remap re-triggerable — which moves the write
    across a Lambda boundary, because terminal write success is known HERE and
    nowhere upstream.

    The version to commit is the one the run actually observed, carried on its
    own ``schema_compare`` state row as ``target_schema_new_version``
    (``schema_compare/store_comparison_result``). That key is written whether or
    not the version changed, so this single path covers all three cases: it
    advances the version after a successful remap, refreshes
    ``target_schema_last_checked`` when nothing changed, and establishes the
    baseline on a pipeline's first successful run — nothing else initialises it
    (``pipelines_crud.create_pipeline`` persists the bucket and key but no
    version).

    *Why the Glue callback is the right anchor.* ``RunPipeline`` is the last
    substantive state in the machine — only ``ReleaseLock`` and a state update
    follow it (``orchestration-stack.ts:718-719``), and
    ``CreateQualityChecks`` runs *before* the job (``:712``, ``:715``). So Glue
    SUCCEEDED means the target write landed, and there is no later step whose
    failure should re-open the remap: the data is in the target, and remapping
    it again would be wrong.

    Best-effort, matching ``_link_heal_rerun_outcome``: the Step Functions
    callback is the run's source of truth and has already been sent, so a
    failure to record the version must never fail job_completion. The
    ``ConditionExpression`` keeps a deleted pipeline from being resurrected as
    a stub row by an in-flight run's completion.

    Ordering note: this is last-writer-wins across runs, with no sequencing
    token available at the completion point. Two successful runs of one
    pipeline finishing out of order would leave the earlier one's version
    stored. ``pipeline_trigger`` serialises runs per pipeline behind
    ``_acquire_pipeline_lock``, so the window requires the lock's 24h TTL to
    lapse first; the committing run_id is logged so a stale version is
    diagnosable rather than silent.
    """
    if state != "SUCCEEDED":
        # A failed run deliberately leaves the stored version behind: that is
        # what keeps the remap re-triggerable on the next run (R8.4).
        return
    if not pipeline_id:
        return

    try:
        compare_item = (
            pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "schema_compare"}).get("Item") or {}
        )
        observed_version = item_str(compare_item, "target_schema_new_version")
        if not observed_version:
            # No schema_compare row (e.g. a retry that started after this step)
            # or a pipeline with no target schema object to version.
            return

        pipelines_table.update_item(
            Key={"pipeline_id": pipeline_id},
            ConditionExpression="attribute_exists(pipeline_id)",
            UpdateExpression="SET target_schema_version = :v, target_schema_last_checked = :ts",
            ExpressionAttributeValues={":v": observed_version, ":ts": datetime.now(UTC).isoformat()},
        )
        logger.info(
            f"Committed target schema version {observed_version} for pipeline {pipeline_id} after successful run {run_id}"
        )
    except ClientError as commit_error:
        if commit_error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(
                f"Pipeline {pipeline_id} no longer exists — target schema version from run {run_id} not committed"
            )
        else:
            logger.error(f"Failed to commit target schema version for pipeline {pipeline_id}: {commit_error}")
    except Exception as commit_error:
        logger.error(f"Failed to commit target schema version for pipeline {pipeline_id}: {commit_error}")


def _record_processed_file(run_id: str, pipeline_id: str, state: str) -> None:
    """Record this run's source content in the Processed_File_Ledger — on success only.

    p30 R9.1/R9.2. The ledger lets ``pipeline_trigger`` skip a submission whose
    content has already been ingested. Writing it HERE, on the Glue completion
    callback, is the whole point of the requirement's wording ("already has a
    **successful** run"): a row written at trigger time would mark a file
    processed before anything had processed it, and a run that then failed would
    leave that file permanently un-retriable — the same defect, in the same
    direction, that R8.4 fixed for ``target_schema_version``.

    *Why the Glue callback is the right anchor* — the reasoning is
    ``_commit_target_schema_version``'s verbatim, and reusing it rather than
    inventing a second notion of "successful" is deliberate: ``RunPipeline`` is
    the last substantive state in the machine (only ``ReleaseLock`` and a state
    update follow it, ``orchestration-stack.ts:718-719``), so SUCCEEDED means the
    target write landed and there is no later step whose failure should re-open
    the file for reprocessing.

    The fingerprint is carried on the run's own ``file_validation`` identity row
    (``content_fingerprint``), written by the Lambda that HEADs the object for
    its size check. Reading it back is a keyed ``get_item`` on the row
    ``_link_heal_rerun_outcome`` already reads. Absent for JDBC runs, for
    unsupported-format files, and for any object whose ETag was unavailable —
    all of which correctly get no ledger row.

    Best-effort, matching the two other post-callback writers in this handler:
    the Step Functions callback is the run's source of truth and has already been
    sent, so failing to record the ledger row must never fail job_completion. The
    cost of the failure is a missed *future* skip, never a wrong one.
    """
    if state != "SUCCEEDED":
        # A failed run records nothing, so the same file can be re-submitted.
        return
    if not pipeline_id:
        return

    try:
        fv_item = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"}).get("Item") or {}
        fingerprint = item_str(fv_item, "content_fingerprint")
        if not fingerprint:
            # No fingerprint: JDBC source, unsupported format, an ETag-less
            # object, or a run that started before this field existed. Nothing
            # to record, and deliberately not an error.
            return

        source_uri = item_str(fv_item, "original_s3_uri")
        record_processed_file(
            processed_files_table,
            pipeline_id,
            fingerprint,
            run_id=run_id,
            source_key=source_uri,
            ttl_seconds=PROCESSED_FILE_LEDGER_TTL_SECONDS,
        )
        logger.info(
            f"Recorded processed-file fingerprint {fingerprint[:12]}… for pipeline {pipeline_id} "
            f"after successful run {run_id}"
        )
    except ClientError as ledger_error:
        logger.error(f"Failed to record processed file for pipeline {pipeline_id}, run {run_id}: {ledger_error}")
    except Exception as ledger_error:
        logger.error(f"Failed to record processed file for pipeline {pipeline_id}, run {run_id}: {ledger_error}")


def _populate_mirror_metadata_location(run_id: str, pipeline_id: str, state: str) -> None:
    """Write the target table's ``metadataLocation`` into its Glue mirror — success only (p35 R7.3).

    Third application of ``LGR-eee8ac8-4``'s rule (**template / ratified**), not
    a new mechanism: a record that asserts an address for metadata which does
    not exist yet is a premature claim, and the only moment the metadata is
    known to exist is the Glue ``SUCCEEDED`` callback. ``create_infrastructure``
    cannot do this — it runs BEFORE the job, and p35's Phase 1 found a table 22
    minutes old still reporting no ``metadataLocation``, so no wait inside its
    600-second budget could succeed and that budget is on the Step Functions
    critical path. The guards, the ``except`` shape and the call-site position
    are ``_commit_target_schema_version``'s, copied in structure.

    **Read-modify-write, and the read is load-bearing.** Glue ``UpdateTable``
    **replaces** the stored table with the ``TableInput`` given rather than
    merging into it, so writing ``Parameters`` alone would erase the
    ``StorageDescriptor`` — the warehouse ``Location`` and the ``Columns``
    ``create_infrastructure`` populates for R7.7. That is why ``glue:GetTable``
    is granted alongside ``glue:UpdateTable`` and is not redundant with it
    (``foundation-stack.ts:679-712``), and why a failed read means **no write**:
    without the read there is nothing to merge into, so writing anyway is the
    clobber the read exists to prevent. ``EntityNotFoundException`` is also how
    a target with no Glue mirror presents, so the same branch covers it.

    This writer touches ``Parameters.metadata_location`` and nothing else.
    ``Columns``, ``StorageDescriptor`` and ``Description`` are carried forward
    from the read verbatim.

    **BLOCKED ON THE FOUNDATION DEPLOY, and until it lands this function writes
    nothing.** All three actions it needs — ``s3tables:GetTable``,
    ``glue:GetTable``, ``glue:UpdateTable`` — were verified ``implicitDeny`` for
    the job-completion role with ``iam:simulate-principal-policy`` against real
    resource ARNs (p35 design §2). Task 1.3 added them to
    ``foundation-stack.ts``; until Foundation is deployed, the first denied call
    lands in the ``except ClientError`` below and logs a warning on **every**
    successful run — the code is present and its effect is absent. That is
    exactly ``LGR-7c9378d-1``'s (**template / ratified**) recorded shape: a
    fixed handler in front of a missing permission, invisible to every test
    layer below CDK because the unit tests stub the boto3 clients. The
    ``AccessDeniedException`` branch is spelled out separately for that reason —
    so the warning names the missing grant rather than reading as a service
    problem.

    Best-effort, exactly like the two writers above: the Step Functions callback
    is the run's source of truth and has already been sent by the time this
    runs, so a failure here must never fail ``job_completion`` (R7.4). Nothing
    is raised and no exception is re-labelled on this path — ``LGR-78ce0b7-13``
    (**template / ratified**) — the error is logged as the class it arrived as.

    And this write is **synchronous, on the handler's own path**. Deferring it
    to a background worker thread is forbidden by ``LGR-b46a5bb-1``, a standing
    invariant: the Lambda execution environment freezes the moment the handler
    returns, so the worker is killed and a "non-blocking" write of this kind
    simply never happens. ``test_self_healing_async_dispatch.py`` scans this
    module's source for the pattern's name, which is why the name is not
    spelled here.
    """
    if state != "SUCCEEDED":
        # R7.5. A non-SUCCEEDED run makes no claim about the target's metadata:
        # the Iceberg commit either did not happen or did not complete, so the
        # mirror's existing state is still the truthful one.
        return
    if not pipeline_id:
        return

    try:
        pipeline_item = pipelines_table.get_item(Key={"pipeline_id": pipeline_id}).get("Item") or {}
        target_config = item_dict(pipeline_item, "target_config")

        bucket_name = item_str(target_config, "bucket_name").strip()
        # `create_infrastructure` normalises the namespace before deriving the
        # pair (`create_infrastructure:285-286`, "S3 Tables only supports
        # single-level namespaces") — the same normalisation has to happen here
        # or this writer addresses a table the registration never wrote. It is
        # NOT in `glue_mirror_identifiers`, deliberately: that module's docstring
        # records that a second normalisation there would make the helper
        # disagree with the writer it exists to describe.
        namespace = item_str(target_config, "namespace_name").strip().replace("-", "_")
        table_name = item_str(target_config, "table_name").strip()
        if not (bucket_name and namespace and table_name):
            # Not an S3 Tables target — a JDBC, Glue Catalog or S3 Parquet
            # target carries none of these three, and there is no mirror to
            # point anywhere. Deliberately not an error and deliberately not
            # keyed on `target_config["type"]`, which spells the same target two
            # ways (`S3_TABLES` and `NEW_S3_TABLE_BUCKET`).
            return

        # The table bucket ARN. `target_config` carries it whenever the wizard
        # selected an EXISTING bucket (`TargetForms.tsx:72`); the "new bucket"
        # path leaves it unset (`wizard/target-detect.ts:13-14`), so the
        # construction is the majority case rather than the exotic one. It is
        # the house form — `create_glue_pipeline/artifacts.py:688`,
        # `job_arguments.py:243`/`:402` and `iam_roles.py:293-299` all build the
        # identical string, and it is the shape `s3tables:ListTableBuckets`
        # itself returns (`create_infrastructure:301`).
        #
        # Resolving it by LISTING is NOT an option: Unit A granted
        # `s3tables:GetTable` only, so `s3tables:ListTableBuckets` would be
        # denied even after the Foundation deploy — a fourth grant, and out of
        # this spec's scope.
        table_bucket_arn = item_str(target_config, "table_bucket_arn").strip()
        if not table_bucket_arn:
            table_bucket_arn = f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/{bucket_name}"

        s3tables_client = boto3.client("s3tables", region_name=AWS_REGION)
        table_details = s3tables_client.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=table_name)
        resolved_location = str(table_details.get("metadataLocation") or "").strip()
        if not resolved_location:
            # R7.4: leave the registration's existing state alone and log the
            # omission. R7.1's rule applies to this writer too — a named
            # location is a claim the thing exists and `""` is a name
            # (`LGR-78ce0b7-14`, **template / ratified**), so there is nothing
            # honest to write. Notably this must NOT clear a pointer an earlier
            # successful run wrote (R7.6).
            logger.info(
                "Glue mirror for pipeline %s left unchanged after successful run %s: s3tables:GetTable "
                "reports no metadataLocation for %s.%s yet, so there is no address to write (p35 R7.4)",
                pipeline_id,
                run_id,
                namespace,
                table_name,
            )
            return

        catalog_db, catalog_table = glue_mirror_identifiers(bucket_name, namespace, table_name)

        # The read. A failure here is handled by the `except` clauses below and
        # produces NO write, because `UpdateTable` replaces rather than merges.
        stored_table = glue_client.get_table(DatabaseName=catalog_db, Name=catalog_table).get("Table") or {}
        if not stored_table:
            logger.info(
                "Glue mirror %s.%s returned no table body, so there is nothing to merge the "
                "metadata location into — leaving it unchanged (p35 R7.4)",
                catalog_db,
                catalog_table,
            )
            return

        table_input = glue_table_input_from_stored(stored_table)
        # `UpdateTable` requires the TableInput to name the table it addresses.
        table_input["Name"] = catalog_table
        # `Parameters` is the read's own dict, so it is copied before being
        # modified — the projection stays a description of what was read.
        parameters = dict(table_input.get("Parameters") or {})
        # The registration's two parameters are re-asserted rather than invented:
        # a mirror carrying no `Parameters` at all is one that is missing them,
        # and Glue does not require the key to be present.
        parameters.setdefault("table_type", "ICEBERG")
        parameters.setdefault("classification", "iceberg")
        parameters["metadata_location"] = resolved_location
        table_input["Parameters"] = parameters

        glue_client.update_table(DatabaseName=catalog_db, TableInput=cast("TableInputTypeDef", table_input))
        logger.info(
            "Wrote metadata_location %s into Glue mirror %s.%s after successful run %s (p35 R7.3); "
            "%d column(s) and the StorageDescriptor preserved from the read",
            resolved_location,
            catalog_db,
            catalog_table,
            run_id,
            len((table_input.get("StorageDescriptor") or {}).get("Columns") or []),
        )
    except ClientError as mirror_error:
        error_code = mirror_error.response.get("Error", {}).get("Code", "")
        if error_code in ("EntityNotFoundException", "NotFoundException", "ResourceNotFoundException"):
            # No mirror, or no S3 Tables table under that address. Nothing to
            # update and nothing wrong — this is also how a target that is not
            # an S3 Tables table presents once the three names are set.
            logger.info(
                f"No Glue mirror or S3 Tables table to update for pipeline {pipeline_id} "
                f"after run {run_id} ({error_code}) — registration left unchanged"
            )
        elif error_code in ("AccessDeniedException", "AccessDenied"):
            # The deployed state until Unit A's Foundation deploy lands. Named
            # explicitly so the log says "grant missing" rather than "Glue
            # broke" — LGR-7c9378d-1's whole lesson.
            logger.warning(
                f"Not authorized to populate the Glue mirror's metadata_location for pipeline "
                f"{pipeline_id} after run {run_id}: {mirror_error}. This is expected until the "
                f"Foundation stack carrying s3tables:GetTable + glue:GetTable + glue:UpdateTable "
                f"for the job-completion role is deployed (p35 R7.3); the run itself is unaffected"
            )
        else:
            logger.error(
                f"Failed to populate the Glue mirror's metadata_location for pipeline {pipeline_id} "
                f"after run {run_id}: {mirror_error}"
            )
    except Exception as mirror_error:
        logger.error(
            f"Failed to populate the Glue mirror's metadata_location for pipeline {pipeline_id} "
            f"after run {run_id}: {mirror_error}"
        )


def _publish_run_cost(run_id: str, pipeline_id: str, state: str, metrics: dict[str, Any]) -> None:
    """Publish this run's Glue cost estimate — on EVERY terminal state (p39 R2.1, R2.2).

    Fifth writer on ``LGR-eee8ac8-4``'s anchor (**template / ratified**), for
    that record's own reason: a side effect that depends on the run having
    happened is committed at the completion callback, not six steps before it. A
    cost estimate for a run that never ran is a fabricated number, which is why
    nothing publishes this from ``run_pipeline`` at job-start time.

    **Deliberate divergence from the three writers above it: this fires on every
    terminal state, not only SUCCEEDED.** They no-op on non-``SUCCEEDED``
    because they record *data* outcomes — a version that was written, a file
    that was ingested, a metadata address that now exists — and a failed run
    establishes none of those. This records *spend*, and AWS bills the
    DPU-minutes of a run that FAILED, was STOPPED or hit TIMEOUT exactly as it
    bills a successful one. Gating on ``SUCCEEDED`` would under-report the
    dashboard's cost tile by the whole of every failed run. (Note
    ``_link_heal_rerun_outcome`` already runs on all terminal states too, for its
    own reason, so the anchor's convention is "gate when the fact is
    success-only", not "gate always".)

    **Once per run (R2.2) is the existing idempotency, not a new mechanism.** A
    duplicate EventBridge delivery finds the record, but the first delivery
    already consumed the task token and deleted its token row, so the handler
    returns on finding no token (the terminal-row check in ``lambda_handler``)
    — or, if that delete failed, ``send_task_success`` / ``send_task_failure``
    raises a consumed-token code and it returns from
    ``_consumed_token_response``. Both returns come before this call. Nothing
    here dedupes and nothing here should.

    The values are the ones ``collect_job_metrics`` already computed, including
    the 10-minute per-run DPU minimum applied there — which is why every run's
    ``actual_cost`` is a constant 0.147 on short jobs. **Nothing in this system
    measures spend**; the metric is an estimate derived from DPU-minutes, which
    is why the publisher's unit is ``"None"`` rather than a currency claim.

    Best-effort, matching the four writers above: the Step Functions callback is
    the run's source of truth and has already been sent, so a failed publish must
    never change the run's outcome. It logs at WARNING with ``exc_info`` rather
    than swallowing — ``LGR-9d1c0e4-1`` and ``LGR-8f3f9e1-1`` (both
    **backfilled**) record a bare ``except`` in this same file dropping a metric
    silently on every run, and ``LGR-9e76249-2`` (**template / ratified**) names
    a fifth writer on this anchor as that exact risk when it rejected
    ``job_completion`` for a different derivation.

    Synchronous, on the handler's own path. ``LGR-b46a5bb-1`` (standing
    invariant) forbids deferring post-response work to a worker thread here: the
    execution environment freezes on return and the worker is killed, so the
    publish would simply never happen.
    """
    if not pipeline_id:
        # The metric's PipelineId dimension would be empty, which makes the
        # datapoint unattributable to any pipeline and is not a value CloudWatch
        # accepts. Same guard the three writers above use.
        return

    estimated_cost = metrics.get("actual_cost")
    if estimated_cost is None:
        # ``collect_job_metrics`` fell into its own ``except`` and returned the
        # error shape, which carries no cost. Publishing 0.0 would invent a
        # datapoint asserting this run was free — the fabricated number this
        # anchor exists to prevent. An absent cost stays absent; the dashboard
        # reports Not_Established for a window with no datapoint (p39 R2.4).
        logger.warning(
            "No cost estimate available for run %s (state %s) — the Glue job-run details were "
            "unreadable, so no GlueJob.RunCostEstimate datapoint is published for it",
            run_id,
            state,
        )
        return

    try:
        publish_glue_run_cost(
            pipeline_id=pipeline_id,
            job_name=str(metrics.get("job_name") or ""),
            estimated_cost=float(estimated_cost),
            worker_type=str(metrics.get("worker_type") or "Unknown"),
            environment=os.environ.get("ENVIRONMENT", "dev"),
        )
    except Exception as cost_error:
        logger.warning(
            f"Failed to publish the run cost estimate for pipeline {pipeline_id}, run {run_id} "
            f"(state {state}): {cost_error}",
            exc_info=True,
        )


def _consumed_token_response(
    error: ClientError, run_id: str, job_run_id: str, pipeline_id: str
) -> dict[str, Any] | None:
    """Classify a send_task_success/failure ClientError (P28 R5.2 idempotency).

    Already-consumed/expired token (TaskTimedOut, TaskDoesNotExist,
    InvalidToken): a duplicate EventBridge delivery, or the SFN task timed out
    before the event arrived. Either way the execution is no longer waiting on
    this token — return a clean success response so EventBridge does NOT
    redeliver (no raise-loop into the DLQ for an event that can never be
    processed again).

    Any other ClientError returns None so the caller re-raises, preserving the
    pre-existing semantics for those failure modes.
    """
    error_code = error.response.get("Error", {}).get("Code", "")
    if error_code not in _TOKEN_CONSUMED_ERROR_CODES:
        return None

    logger.warning(
        f"Task token for run {run_id} already consumed or expired ({error_code}) — "
        f"duplicate delivery or timed-out task; terminating cleanly"
    )
    return {
        "statusCode": 200,
        "message": f"Task token already consumed ({error_code}); duplicate delivery ignored",
        "job_run_id": job_run_id,
        "pipeline_id": pipeline_id,
    }


def lambda_handler(event, context):
    """
    Handle Glue job completion events from EventBridge and callback to Step Functions.

    This Lambda:
    1. Receives EventBridge event when Glue job state changes
    2. Finds the run via the GlueJobRunIndex GSI, then reads its task token
       from the task-tokens table (``glue_run#<run_id>``)
    3. Collects comprehensive job metrics
    4. Updates pipeline run status with metrics
    5. Sends success/failure callback to Step Functions, then deletes the
       token row

    Args:
        event: EventBridge event with Glue job state change
        context: Lambda context

    Returns:
        dict: Processing status
    """
    try:
        logger.info(f"Glue job completion event received: {json.dumps(event)}")

        # Extract job information from EventBridge event
        detail = event.get("detail", {})
        job_name = detail.get("jobName")
        job_run_id = detail.get("jobRunId")
        state = detail.get("state")  # SUCCEEDED, FAILED, STOPPED, TIMEOUT

        if not job_name or not job_run_id:
            raise ValueError("jobName and jobRunId are required in event detail")

        logger.info(f"Processing completion for job: {job_name}, run: {job_run_id}, state: {state}")

        # Find the pipeline run with this job_run_id via GSI (fast query, not scan)
        from boto3.dynamodb.conditions import Key

        response = pipeline_runs_table.query(
            IndexName="GlueJobRunIndex",
            KeyConditionExpression=Key("glue_job_run_id").eq(job_run_id),
            Limit=1,
        )

        if not response.get("Items"):
            # P28 R5.2: raise (function error → Lambda's async retry, see
            # RecordNotFoundError) instead of returning 404-as-success, which permanently
            # dropped the event and stranded the run for the full task
            # timeout. By the redelivery, run_pipeline's update_item has
            # landed and the record is GSI-findable.
            raise RecordNotFoundError(
                f"No pipeline run record found for Glue job completion event: "
                f"jobName={job_name}, jobRunId={job_run_id}, state={state}"
            )

        pipeline_run = response["Items"][0]
        run_id = item_str(pipeline_run, "run_id")
        pipeline_id = item_str(pipeline_run, "original_pipeline_id")

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name="JobCompletion")

        # The GSI row gives the run; the token lives in the task-tokens table
        # (run_pipeline's "Where the token lives").
        task_token, token_from_table, token_row = _resolve_task_token(run_id, pipeline_run)

        # A dashboard cancel stopped this job: close the run as cancelled instead
        # of failing it into RecordFailure and self-healing.
        cancel_execution_arn = _cancel_marker_execution(token_row, job_run_id, state)
        if cancel_execution_arn:
            return _finish_cancelled_run(
                run_id,
                pipeline_id,
                item_str(pipeline_run, "step_name") or StepName.RUN_PIPELINE,
                job_name,
                job_run_id,
                state,
                cancel_execution_arn,
            )

        if not task_token:
            # A duplicate EventBridge delivery arriving after the first copy
            # sent the callback and deleted the token row. Before the token
            # moved off the state row, a duplicate re-sent the spent token and
            # took the consumed-token return below; this keeps the same
            # outcome now that there is no token left to send.
            if item_str(pipeline_run, "status") in _TERMINAL_RUN_STATUSES:
                logger.warning(
                    f"No task token for run {run_id}, whose state row is already terminal — "
                    f"duplicate delivery after the token was consumed; terminating cleanly"
                )
                return {
                    "statusCode": 200,
                    "message": "Task token already consumed (token row deleted); duplicate delivery ignored",
                    "job_run_id": job_run_id,
                    "pipeline_id": pipeline_id,
                }
            raise TaskTokenMissingError(
                f"No task token for active run {run_id} (state row status "
                f"{item_str(pipeline_run, 'status') or 'absent'!r}): jobName={job_name}, "
                f"jobRunId={job_run_id}, state={state}. The execution cannot be called back."
            )

        logger.info(f"Found pipeline run: {run_id}, pipeline: {pipeline_id}")

        # Collect comprehensive metrics
        metrics = collect_job_metrics(job_name, job_run_id)

        # Calculate performance comparison
        comparison = calculate_performance_comparison(metrics, pipeline_id)
        metrics.update(comparison)

        # Update pipeline run with completion metrics
        timestamp = datetime.now(UTC).isoformat()

        # P28 R8.2 (task 14.2): update the exact row the GSI lookup found —
        # new rows carry the canonical "run_pipeline" step name, while runs
        # in flight across the rename deployment still carry the legacy
        # "RunPipeline" key. Reusing the found item's own key is tolerant of
        # both without guessing.
        record_step_name = item_str(pipeline_run, "step_name") or StepName.RUN_PIPELINE

        pipeline_runs_table.update_item(
            Key={"run_id": run_id, "step_name": record_step_name},
            UpdateExpression="""
                SET #status = :status,
                    completed_at = :timestamp,
                    output_data = :metrics
            """,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=convert_floats_to_decimal(
                {
                    ":status": "success" if state == "SUCCEEDED" else "failed",
                    ":timestamp": timestamp,
                    ":metrics": metrics,
                }
            ),
        )

        logger.info("Updated pipeline run with metrics")

        # Send callback to Step Functions.
        # P28 R5.2 idempotency: EventBridge may deliver the same completion
        # event twice. A duplicate normally returns above, finding no token
        # row. If the first delivery's row delete failed, or the token came
        # from the legacy state-row fallback, the duplicate still holds the
        # spent token here — Step Functions rejects the send with a
        # consumed-token code. That terminates cleanly (log + return): raising
        # here would redeliver a permanently unprocessable event until it
        # dead-letters. The token row is deleted only after a send succeeds.
        if state == "SUCCEEDED":
            logger.info("Sending task success to Step Functions")
            try:
                sfn_client.send_task_success(
                    taskToken=task_token, output=json.dumps(convert_floats_to_decimal(metrics), default=str)
                )
            except ClientError as send_error:
                consumed = _consumed_token_response(send_error, run_id, job_run_id, pipeline_id)
                if consumed is not None:
                    return consumed
                raise
            if token_from_table:
                _delete_task_token_row(run_id)
            logger.info("✅ Pipeline execution completed successfully")
        else:
            logger.info("Sending task failure to Step Functions")
            try:
                sfn_client.send_task_failure(
                    taskToken=task_token,
                    error="GlueJobFailed",
                    cause=f"Glue job {job_name} {state}: {metrics.get('error_message', 'Unknown error')}",
                )
            except ClientError as send_error:
                # Duplicate failure event: the first delivery already sent the
                # task failure and triggered self-healing — skip both here.
                consumed = _consumed_token_response(send_error, run_id, job_run_id, pipeline_id)
                if consumed is not None:
                    return consumed
                raise
            if token_from_table:
                _delete_task_token_row(run_id)
            logger.error(f"❌ Pipeline execution failed: {state}")

            # P28 R9.3: dispatch the self-healing agent via a fire-and-forget
            # async Lambda invoke (InvocationType="Event") of the dispatch
            # function, which owns the long synchronous AgentCore call inside
            # its own 15-minute timeout. The old pattern — a daemon thread
            # doing the synchronous call from here — was frozen/killed as
            # soon as this handler returned (Lambda freezes the execution
            # environment), and boto3's default 60s read timeout could never
            # survive the agent's ~10-minute workload anyway.
            self_healing_fn = os.environ.get("SELF_HEALING_FN", "")
            if self_healing_fn:
                try:
                    lambda_client.invoke(
                        FunctionName=self_healing_fn,
                        InvocationType="Event",
                        Payload=json.dumps(
                            {
                                "pipeline_id": pipeline_id,
                                "run_id": run_id,
                                "job_name": job_name,
                                "job_run_id": job_run_id,
                                "error_message": metrics.get("error_message", "Unknown error"),
                                "state": state,
                            }
                        ),
                    )
                    logger.info(f"Self-healing dispatch queued to {self_healing_fn}")
                except Exception as dispatch_error:
                    # Best-effort (R9.3): the Step Functions callback already
                    # succeeded — a failed healing DISPATCH must not fail
                    # job_completion.
                    logger.error(f"Failed to dispatch self-healing agent: {dispatch_error}")

        # P28 R9.7: if THIS run is a heal rerun, link its terminal result
        # back to the original run's healing record (best-effort). For a
        # FAILED heal rerun the dispatch above still fires — the agent's
        # Heal_Guard is what enforces max-one-heal, not this handler.
        _link_heal_rerun_outcome(run_id, state)

        # p30 R8.4: commit the target schema version this run observed, now
        # that the target write is known to have landed. No-op on any
        # non-SUCCEEDED state — a failure must leave the stored version behind
        # so the remap stays re-triggerable. Best-effort, after the callback.
        _commit_target_schema_version(run_id, pipeline_id, state)

        # p30 R9.1: record this run's source content in the Processed_File_Ledger
        # on the same anchor and for the same reason — "processed" is only true
        # once the target write has landed. No-op on any non-SUCCEEDED state, so
        # a failed run leaves the file re-submittable. Best-effort, after the
        # callback.
        _record_processed_file(run_id, pipeline_id, state)

        # p35 R7.3: write the target table's metadataLocation into its Glue
        # mirror, now that the Iceberg commit is known to have happened — the
        # third writer on this anchor, for LGR-eee8ac8-4's reason. No-op on any
        # non-SUCCEEDED state (R7.5) and on a target with no S3 Tables mirror.
        # Best-effort, after the callback: the run's outcome is already sent, so
        # nothing this does may change it (R7.4).
        _populate_mirror_metadata_location(run_id, pipeline_id, state)

        # p39 R2.1/R2.2: publish this run's Glue cost estimate — the fifth writer
        # on LGR-eee8ac8-4's anchor, for the same reason: a cost estimate for a
        # run that never ran is a fabricated number, so it is published here and
        # not from run_pipeline at job-start time. Unlike the three writers above
        # it, this does NOT gate on SUCCEEDED — DPU-minutes are billed for a run
        # that FAILED, was STOPPED or hit TIMEOUT, and this records spend rather
        # than a data outcome. Once-per-run needs no new mechanism: a duplicate
        # delivery has already returned above — on finding no token, or via
        # _consumed_token_response.
        # Best-effort, after the callback: nothing it does may change the run's
        # outcome.
        _publish_run_cost(run_id, pipeline_id, state, metrics)

        # The success half of the pipeline error rate. Failures are counted by
        # record_failure, which every caught failure reaches, GlueJobFailed
        # included, so publishing them here too would count a Glue failure twice.
        # Same anchor and same once-per-run guarantee as the cost publish above.
        if state == "SUCCEEDED":
            publish_run_outcome(failed=False, environment=os.environ.get("ENVIRONMENT", "dev"))

        return {
            "statusCode": 200,
            "message": f"Job completion processed: {state}",
            "job_run_id": job_run_id,
            "pipeline_id": pipeline_id,
        }

    except RecordNotFoundError:
        # P28 R5.2: propagate, so the event is retried (see RecordNotFoundError).
        raise
    except Exception:
        # Re-raised, never returned. This used to return
        # `{"statusCode": 500, ...}`, which is a SUCCESSFUL asynchronous
        # invocation: Lambda recorded no error and retried nothing, so a
        # throttled query, a failed token read or state update, or a failed
        # Step Functions callback left the run waiting until its task timeout.
        # As a function error it is retried twice and then delivered to the
        # Glue job monitor DLQ, whose alarm pages.
        #
        # A retry is safe at every point it can start from. Before the
        # callback, each step is a read or an idempotent overwrite. After it,
        # the token row is gone, so a retry takes the terminal-state return
        # above; if the row delete failed, it resends a spent token and takes
        # `_consumed_token_response`. The steps after the callback catch their
        # own errors, so they do not reach here.
        logger.exception("Error processing job completion")
        raise
