# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import os
import sys
import time
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

# Initialize logger
from aws_lambda_powertools import Logger

# The same TTL backstop the approval path stamps on its token rows
# (approval_handler.store_task_token). It outlives the RunPipeline task
# timeout by a wide margin, so it never races a live run.
from config import TASK_TOKEN_TTL_SECONDS

# P28 R19.2: single shared float→Decimal converter (replaces the local copy).
from decimal_utils import convert_floats_to_decimal
from env_utils import get_aws_account_id, require_env

# P28 R1.1: Step_Lambdas raise on failure instead of returning error payloads.
from step_errors import StepFailure, TransientStepFailure, step_handler
from step_names import STEP_SEQUENCE, StepName

logger = Logger(service="agentic_data_ingestion")

# Glue start_job_run error codes that are genuinely retryable (P28 R1.1):
# concurrency limits clear when a running job finishes; throttles and internal
# service errors clear on backoff. These raise TransientStepFailure so the
# state machine's Retry policy can back off and retry.
_TRANSIENT_GLUE_ERROR_CODES = frozenset(
    {
        "ConcurrentRunsExceededException",
        "ThrottlingException",
        "TooManyRequestsException",
        "InternalServiceException",
        "OperationTimeoutException",
    }
)

# Environment variables
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
PIPELINES_TABLE = require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
TASK_TOKENS_TABLE = require_env("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Key prefix of the Glue-callback token row in the task-tokens table. The
# table's partition key is named `approval_id` because it was built for the
# approval gates; approval ids are `{pipeline_id}_{approval_type}_{epoch}` and
# every pipeline_id generator emits only [a-z0-9-], so no approval id can
# start with `glue_run#`. job_completion builds the same key; the two literals
# are pinned equal by tests/unit/lambdas/job_completion/
# test_job_completion_token_table.py.
GLUE_RUN_TOKEN_KEY_PREFIX = "glue_run#"  # a table key prefix, not a secret  # nosec B105

# Event keys never written to the log. The task token lets its holder report
# the Glue job's result to Step Functions, and the dashboard reads this
# function's log group back to the browser (step-log retrieval).
_UNLOGGED_EVENT_KEYS = frozenset({"taskToken"})

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
glue_client = boto3.client("glue")
sfn_client = boto3.client("stepfunctions")

# Get AWS Account ID via the canonical resolver (env var → STS; P21 Req 5.5/5.10).
AWS_ACCOUNT_ID = get_aws_account_id()

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)
pipelines_table = dynamodb.Table(PIPELINES_TABLE)
task_tokens_table = dynamodb.Table(TASK_TOKENS_TABLE)


def glue_run_token_key(run_id: str) -> str:
    """The task-tokens table key holding this run's Glue-callback token."""
    return f"{GLUE_RUN_TOKEN_KEY_PREFIX}{run_id}"


# The attribute the dashboard's cancel_run stamps on the token row when it
# aborts a run that has no Glue job yet. Both writes below refuse to proceed
# past it; see "Cancel fence" in the handler docstring.
CANCEL_FENCE_ATTRIBUTE = "cancel_requested_at"
_NO_CANCEL_FENCE = f"attribute_not_exists({CANCEL_FENCE_ATTRIBUTE})"


class RunCancelledBeforeStart(StepFailure):
    """The run was cancelled before this step wrote anything; no Glue job started."""

    def __init__(self, run_id: str, pipeline_id: str):
        super().__init__(
            "run_pipeline",
            f"Run {run_id} was cancelled before its Glue job started",
            details={"run_id": run_id, "pipeline_id": pipeline_id},
        )


class RunCancelledAfterStart(StepFailure):
    """The run was cancelled while its Glue job was starting; the job has been asked to stop."""

    def __init__(self, run_id: str, pipeline_id: str, glue_job_name: str, job_run_id: str):
        super().__init__(
            "run_pipeline",
            f"Run {run_id} was cancelled as its Glue job started; stop requested for {job_run_id}",
            details={
                "run_id": run_id,
                "pipeline_id": pipeline_id,
                "glue_job_name": glue_job_name,
                "glue_job_run_id": job_run_id,
            },
        )


def _is_condition_failure(error: Exception) -> bool:
    return (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code", "") == "ConditionalCheckFailedException"
    )


def _stop_started_job(glue_job_name: str, job_run_id: str, reason: str) -> None:
    """Ask Glue to stop a job this invocation started. Best-effort, logged either way.

    A failed stop is not re-raised: the caller is already on a failure path
    whose cause must reach the operator, and the job run id is on every row
    that path writes, so an operator can stop it by hand.
    """
    try:
        result = glue_client.batch_stop_job_run(JobName=glue_job_name, JobRunIds=[job_run_id])
        errors = result.get("Errors") or []
        if errors:
            logger.error(f"Glue refused to stop job run {job_run_id} ({reason}): {errors}")
        else:
            logger.info(f"Stop requested for Glue job run {job_run_id} ({reason})")
    except Exception as stop_error:
        logger.error(f"Failed to request a stop for Glue job run {job_run_id} ({reason}): {stop_error}")


@step_handler("run_pipeline")
def lambda_handler(event, context):
    """
    Start Glue job execution and wait for completion via Step Functions callback.

    This Lambda:
    1. Retrieves Glue job name from pipeline metadata
    2. Persists the task token in the task-tokens table, keyed
       ``glue_run#<run_id>`` (BEFORE starting the job — P28 R5.1)
    3. Persists the RUNNING state row, which carries NO task token
    4. Starts the Glue job
    5. Attaches the glue_job_run_id to the state row (GlueJobRunIndex GSI)
    6. Returns immediately (Step Functions waits for callback)

    Where the token lives:
        Not on the state row. AgentCore agents read the state table and the
        dashboard returns state rows to the browser, and a live token lets
        its holder report any result for the Glue job. The task-tokens table
        is the one the approval gates already use, and the AgentCore runtime
        role is denied every action on it (``DenyApprovalTaskTokens``,
        LGR-8f59603-3, which named this token as its known residual). The
        row is keyed by run_id rather than glue_job_run_id because it must
        exist before start_job_run returns.

    Ordering rationale (P28 R5.1 — fast-fail race):
        job_completion finds the run via the GlueJobRunIndex GSI (keyed on
        glue_job_run_id — the Glue state-change event carries only
        jobName/jobRunId/state, no job arguments), then reads the token row
        by run_id. The old order (start_job_run → put_item) meant a
        fast-failing job could emit its completion event before the token
        existed, stranding the run. Both rows are written first, the token
        row before the state row, so whenever the state row is findable its
        token is already stored. The glue_job_run_id attribute is attached
        with a single update_item immediately after start returns
        (milliseconds), while Glue's start→terminal-state latency is tens of
        seconds. The residual GSI-propagation window is covered by
        job_completion raising on a missing record (R5.2 → EventBridge
        redelivery with backoff → DLQ + alarm, R5.3).

    Idempotency (P28 R5.1):
        The state row is keyed by (run_id, step_name="run_pipeline") and the
        token row by ``glue_run#<run_id>``; the state row is written with an
        unconditional put_item and the token row with one conditional only on
        the cancel fence below, so a retried task overwrites its own rows
        (and the retry's fresh token replaces the spent one) before starting
        a new job run attempt — retries never hit a duplicate-key failure.

    Cancel fence (2026-10-11, assessment F1):
        A dashboard cancel that finds no Glue job stops the execution and
        marks the run CANCELLED, then this Lambda, if it was already running,
        could carry on and start a job nobody tracks or stops. The cancel now
        leaves ``cancel_requested_at`` on this run's token row, and this
        Lambda refuses to proceed past it at two points:

        1. The pre-start token put_item is conditional on no fence. If it is
           already there, nothing has started and the step raises
           ``RunCancelledBeforeStart`` without touching Glue.
        2. The post-start write that stamps ``glue_job_run_id`` onto the token
           row is conditional on no fence too. If the cancel landed in the
           milliseconds between the put and start_job_run returning, the job
           has started: this Lambda stops it (BatchStopJobRun) and raises
           ``RunCancelledAfterStart``. job_completion then closes the run as
           cancelled from the Glue STOPPED event, exactly as it does for a
           cancel that found the job running.

        Both raises hit the RunPipeline task's Catch while the execution is
        already ABORTED, which Step Functions ignores, so no RecordFailure
        and no error-rate datapoint follow.

    Post-start persistence failure (2026-10-11, assessment F2):
        Once start_job_run has returned, a failed DynamoDB write is not a
        failed start. The job is stopped before the usual cleanup runs, and
        the error row carries ``glue_job_run_id`` so the job that ran can be
        found. Until this change the generic except path deleted the rows and
        failed the task with the job still running and unfindable.

    Args:
        event: Contains pipeline_id, run_id, and taskToken from Step Functions
        context: Lambda context

    Returns:
        None - Step Functions will receive callback when job completes
    """
    # Each set only after THIS invocation persists that row, so the failure
    # path cleans up exactly its own pre-start writes (P28 R5.1).
    token_row_written = False
    state_row_written = False
    # Set the moment start_job_run returns. On the failure path it decides
    # between "a start that never happened" and "a job that is running".
    started_job: tuple[str, str] | None = None
    try:
        loggable_event = {k: v for k, v in event.items() if k not in _UNLOGGED_EVENT_KEYS}
        logger.info(f"Starting Glue job execution for pipeline: {json.dumps(loggable_event)}")

        # Extract required parameters
        pipeline_id = event.get("pipeline_id")
        run_id = event.get("run_id")
        task_token = event.get("taskToken")  # Provided by Step Functions

        if not pipeline_id or not run_id:
            raise ValueError("pipeline_id and run_id are required")

        logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name=StepName.RUN_PIPELINE)

        if not task_token:
            raise ValueError("taskToken is required for Step Functions callback pattern")

        # Retrieve Glue job name from pipelines table
        logger.info(f"Retrieving Glue job metadata for pipeline: {pipeline_id}")
        response = pipelines_table.get_item(Key={"pipeline_id": pipeline_id})

        if "Item" not in response:
            raise ValueError(f"Pipeline {pipeline_id} not found in pipelines table")

        pipeline_item = response["Item"]
        glue_job_metadata = pipeline_item.get("glue_job_metadata", {})
        glue_job_name = glue_job_metadata.get("glue_job_name")

        if not glue_job_name:
            raise ValueError(f"Glue job name not found for pipeline {pipeline_id}")

        logger.info(f"Starting Glue job: {glue_job_name}")

        # Build job arguments - only runtime parameters
        # DO NOT override Glue job DefaultArguments (bucket, table, namespace, etc.)
        # Those are set correctly when the job is created
        job_arguments = {
            "--pipeline_id": pipeline_id,
            "--run_id": run_id,
            "--execution_timestamp": datetime.now(UTC).isoformat(),
            "--state_table": os.environ.get("STATE_TABLE", "agentic_data_ingestion_state"),
        }

        logger.info(f"Job arguments (runtime only): {job_arguments}")

        # P28 R5.1: persist the task token BEFORE start_job_run so it can
        # never be lost once the job is running — in the task-tokens table,
        # not on the state row (see "Where the token lives" above).
        # Conditional only on the cancel fence: a retried task still
        # overwrites its own row with the retry's token before a new start
        # attempt, but a run the dashboard has already cancelled stops here.
        timestamp = datetime.now(UTC).isoformat()

        try:
            task_tokens_table.put_item(
                Item={
                    "approval_id": glue_run_token_key(run_id),  # Partition key
                    "task_token": task_token,
                    "run_id": run_id,
                    "pipeline_id": pipeline_id,
                    "ttl": int(time.time()) + TASK_TOKEN_TTL_SECONDS,
                    "created_at": timestamp,
                },
                ConditionExpression=_NO_CANCEL_FENCE,
            )
        except ClientError as put_error:
            if _is_condition_failure(put_error):
                logger.warning("Run was cancelled before its Glue job started; not starting it")
                raise RunCancelledBeforeStart(run_id, pipeline_id) from put_error
            raise
        token_row_written = True

        logger.info("Glue-callback task token persisted (pre-start) in the task-tokens table")

        # The RUNNING state row. Unconditional put_item on (run_id,
        # "run_pipeline") — same idempotency as the token row. It cannot carry
        # glue_job_run_id yet (it doesn't exist until start returns), so it is
        # absent from the sparse GlueJobRunIndex GSI until the update below.
        pipeline_runs_table.put_item(
            Item=convert_floats_to_decimal(
                {
                    "run_id": run_id,  # Partition key
                    # P28 R8.2 (task 14.2): canonical step name — the legacy
                    # "RunPipeline" state-row variant is retired.
                    "step_name": StepName.RUN_PIPELINE,
                    "status": "RUNNING",
                    "step_sequence": STEP_SEQUENCE[StepName.RUN_PIPELINE],
                    "timestamp": timestamp,
                    "pipeline_id": pipeline_id,  # For PipelineIndex GSI
                    "original_pipeline_id": pipeline_id,
                    "glue_job_name": glue_job_name,
                    "started_at": timestamp,
                    "output_data": {"glue_job_name": glue_job_name, "status": "RUNNING"},
                }
            )
        )
        state_row_written = True

        logger.info("RUNNING state row persisted (pre-start) in DynamoDB")

        # Start Glue job
        job_response = glue_client.start_job_run(JobName=glue_job_name, Arguments=job_arguments)

        job_run_id = job_response["JobRunId"]
        started_job = (glue_job_name, job_run_id)
        logger.info(f"Glue job started successfully: {glue_job_name}, JobRunId: {job_run_id}")

        # Second fence check, now that a job exists. Stamping the run id on the
        # token row is conditional on no cancel having landed since the put
        # above; if one did, the execution is already aborted and this is the
        # only writer that knows a job started, so it stops the job itself.
        # job_completion closes the run from the resulting STOPPED event.
        try:
            task_tokens_table.update_item(
                Key={"approval_id": glue_run_token_key(run_id)},
                UpdateExpression="SET glue_job_run_id = :jid, glue_job_name = :jname",
                ConditionExpression=_NO_CANCEL_FENCE,
                ExpressionAttributeValues={":jid": job_run_id, ":jname": glue_job_name},
            )
        except ClientError as stamp_error:
            if _is_condition_failure(stamp_error):
                logger.warning("Run was cancelled as its Glue job started; stopping the job")
                _stop_started_job(glue_job_name, job_run_id, "cancelled during start")
                # The state row keeps the run id so job_completion's GSI lookup
                # finds this run when the STOPPED event arrives.
                pipeline_runs_table.update_item(
                    Key={"run_id": run_id, "step_name": StepName.RUN_PIPELINE},
                    UpdateExpression="SET glue_job_run_id = :jid, output_data.glue_job_run_id = :jid",
                    ExpressionAttributeValues={":jid": job_run_id},
                )
                raise RunCancelledAfterStart(run_id, pipeline_id, glue_job_name, job_run_id) from stamp_error
            raise

        # Attach the job run id — this makes the state row findable via the
        # GlueJobRunIndex GSI, which is how job_completion matches the
        # completion event back to this run (and so to its token row). A
        # failure here falls into the failure path below, which now stops the
        # job it started before the usual cleanup (assessment F2).
        pipeline_runs_table.update_item(
            Key={"run_id": run_id, "step_name": StepName.RUN_PIPELINE},
            UpdateExpression="SET glue_job_run_id = :jid, output_data.glue_job_run_id = :jid",
            ExpressionAttributeValues={":jid": job_run_id},
        )

        logger.info("Job run id attached to the RUNNING state row (GlueJobRunIndex)")
        logger.info("Step Functions will wait for callback when job completes")

        # DON'T return anything - Step Functions is waiting for callback
        # The job_completion Lambda will call send_task_success/failure

    except (RunCancelledBeforeStart, RunCancelledAfterStart):
        # The execution is already ABORTED by the cancel, so there is no task
        # to fail and nothing of this run's to tidy: before start, the fenced
        # token row is the cancel's and the state row was never written;
        # after start, the rows must stay so job_completion can find the run
        # when the STOPPED event lands. Raised as-is for step_handler's log.
        raise

    except Exception as e:
        error_message = f"Error starting Glue job: {e!s}"
        logger.error(error_message)

        # 2026-10-11 (assessment F2): a failure AFTER start_job_run returned is
        # not a failed start. Stop the job before cleaning up, so the cleanup
        # below never leaves a running job with no row that names it. The run
        # id is also kept on the error row written further down.
        if started_job is not None:
            _stop_started_job(*started_job, "persistence failed after start")

        # P28 R5.1: the start failed (or a later call did) — clean up the
        # pre-start rows so no RUNNING row and no stored task token linger
        # for a run this invocation is failing. Each guarded by its own flag
        # so we only ever delete what THIS invocation wrote (early validation
        # failures wrote neither). Best-effort: the error-state put_item below
        # replaces the state row's key anyway, the token row's TTL is the
        # backstop, and cleanup failure must not mask the real error. The
        # task failure below is sent with the event's token, so deleting the
        # stored copy first loses nothing.
        if state_row_written:
            try:
                pipeline_runs_table.delete_item(Key={"run_id": run_id, "step_name": StepName.RUN_PIPELINE})
                logger.info("Cleaned up pre-start RUNNING state row")
            except Exception as del_error:
                logger.error(f"Failed to clean up RUNNING state row: {del_error}")
        if token_row_written:
            try:
                task_tokens_table.delete_item(Key={"approval_id": glue_run_token_key(run_id)})
                logger.info("Cleaned up pre-start Glue-callback task token")
            except Exception as del_error:
                logger.error(f"Failed to clean up Glue-callback task token: {del_error}")

        # If we have a task token, send failure to Step Functions
        task_token = event.get("taskToken")
        if task_token:
            try:
                sfn_client.send_task_failure(taskToken=task_token, error="GlueJobStartFailed", cause=error_message)
                logger.info("Sent task failure to Step Functions")
            except Exception as sfn_error:
                logger.error(f"Failed to send task failure: {sfn_error}")

        # Also write error to DynamoDB. If a job did start, its identity goes
        # on the row: a stop request is best-effort, and an operator finding
        # this row must be able to see what ran (assessment F2).
        try:
            run_id = event.get("run_id", "unknown")
            error_item: dict = {
                "run_id": run_id,  # Partition key
                "step_name": StepName.RUN_PIPELINE,
                "status": "failed",
                "step_sequence": STEP_SEQUENCE[StepName.RUN_PIPELINE],
                "timestamp": datetime.now(UTC).isoformat(),
                "pipeline_id": event.get("pipeline_id", "unknown"),  # For PipelineIndex GSI
                "original_pipeline_id": event.get("pipeline_id", "unknown"),
                "error_message": error_message,
                "error_type": "GlueJobStartError" if started_job is None else "GlueJobPostStartError",
            }
            if started_job is not None:
                error_item["glue_job_name"], error_item["glue_job_run_id"] = started_job
                error_item["glue_job_stop_requested"] = True
            pipeline_runs_table.put_item(Item=convert_floats_to_decimal(error_item))
        except Exception as db_error:
            logger.error(f"Failed to write error to DynamoDB: {db_error}")

        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire.
        # Glue concurrency/throttling errors are genuinely retryable — surface
        # those as TransientStepFailure so the Retry policy can back off.
        details = {
            "run_id": event.get("run_id", "unknown"),
            "pipeline_id": event.get("pipeline_id", "unknown"),
        }
        if started_job is not None:
            details["glue_job_name"], details["glue_job_run_id"] = started_job
        error_code = e.response.get("Error", {}).get("Code", "") if isinstance(e, ClientError) else ""
        if error_code in _TRANSIENT_GLUE_ERROR_CODES:
            raise TransientStepFailure("run_pipeline", error_message, details=details) from e
        raise StepFailure("run_pipeline", error_message, details=details) from e
