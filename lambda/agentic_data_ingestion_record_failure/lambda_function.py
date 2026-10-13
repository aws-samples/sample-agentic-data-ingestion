# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RecordFailure Lambda — writes the Failure_Record when a pipeline step fails.

P28 R1.3 / R1.4 (error propagation): every task state in the pipeline state
machine has ``Catch: [{ErrorEquals: ["States.ALL"], Next: "RecordFailure",
ResultPath: "$.error"}]``. This Lambda receives the FULL execution state (plus
the error object at ``$.error``) and persists the ``pipeline_failure`` state
item to the pipeline-state DynamoDB table before the execution transitions to
the ``PipelineFailed`` Fail state.

Failure_Record contract (consumed by the dashboard's error surfaces and the
p16 retry-from-step machinery):

    Key:    run_id (PK) + step_name='pipeline_failure' (SK)
    Fields: failed_step        — originating step name (R1.4)
            error_type         — "permanent" | "transient" | "unknown"
            error_name         — the raw SFN Error field (exception class name)
            error_message      — human-readable message from the ORIGINATING
                                 step (never a downstream JSONPath error)
            original_pipeline_id / run_id — run & pipeline identifiers
            status='error', timestamp, step_sequence=99,
            pipeline_id='<run_id>.99' — legacy synthetic id kept for
                                 dashboard / e2e compatibility

Cause parsing (most → least structured):

    1. ``Cause`` is the Lambda runtime wrapper ``{"errorMessage", "errorType",
       ...}`` and ``errorMessage`` is the stable StepFailure JSON
       (``shared/step_errors.py`` contract: ``{"step_name", "error_type",
       "message", "details"}``) → authoritative step attribution.
    2. ``Cause`` is the runtime wrapper but ``errorMessage`` is a plain string
       (non-StepFailure exception) → message from the wrapper, step inferred.
    3. ``Cause`` is not JSON (States.Timeout, Lambda.ServiceException,
       GlueJobFailed, ...) → message from the Error name + raw Cause, step
       from the error-name map or inferred from execution progress.
    3a. ``States.Timeout`` on an execution parked at one of the three
       waitForTaskToken approval gates → the message names the approval type
       and the timeout as the cause (p34 R6.5), so an approval nobody answered
       is readable as such rather than as a bare ``States.Timeout``. The
       ``error_name`` field still carries the raw SFN Error verbatim, which is
       what keeps the three terminal causes of an approval gate apart:
       ``States.Timeout`` (nobody answered), ``ApprovalRejected`` (a human
       said no) and any other Error name (a Lambda or step failure).

Progress inference is best-effort: it names the first step whose result key
is missing after the last completed one. Approval-gate keys are conditional
(they are absent when no approval was required), so an inferred name may point
at the gate preceding the actual task — records carry
``failed_step_source='inferred'`` so consumers can tell authoritative
attribution (``'step_failure'``) apart from inference.

Defensive by design: a RecordFailure crash must never mask the original
failure. DynamoDB write errors are caught and logged; the handler still
returns so the state machine proceeds to lock release and PipelineFailed.

This Lambda is deliberately dependency-free (stdlib + boto3 only) — it is
deployed without the shared layer.
"""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STATE_TABLE_NAME = os.environ.get("STATE_TABLE", "agentic_data_ingestion_state")

FAILURE_STEP_NAME = "pipeline_failure"
FAILURE_STEP_SEQUENCE = 99
MAX_MESSAGE_CHARS = 2000
MAX_CAUSE_CHARS = 4000

_dynamodb = boto3.resource("dynamodb")
state_table = _dynamodb.Table(STATE_TABLE_NAME)

# Ordered (result key, step that produces it) pairs mirroring the state
# machine definition in deploy-cdk/lib/orchestration-stack.ts. Used only for
# best-effort inference when the Cause carries no StepFailure JSON. The first
# element is the ASL ResultPath key (an SFN-surface contract — NOT a step
# name); the second is the canonical step name per shared/step_names.py
# (P28 R8.2, task 14.2 — the legacy "schema_mapping_approval"/"code_approval"
# gate aliases are retired in favor of the canonical check_* names).
PROGRESS_ORDER: list[tuple[str, str]] = [
    ("file_validation_result", "file_validation"),
    ("schema_extract_result", "schema_extract"),
    ("schema_compare_result", "schema_compare"),
    ("schema_mapping_result", "schema_mapping"),
    ("schema_mapping_approval", "check_schema_mapping_approval"),
    ("create_code_result", "create_code"),
    ("test_code_result", "test_code"),
    # P28 R4.2 (task 5.2): ForceTestReviewApproval writes $.test_review_approval.
    # Like the other approval-gate keys this is conditional (absent when the
    # test passed), so inference may name it one gate early — documented above.
    # The gate has no Canonical_Step_Names member (it is not a pipeline step);
    # attribution points at the step under review.
    ("test_review_approval", "test_code"),
    ("code_approval", "check_code_approval"),
    ("create_infrastructure_result", "create_infrastructure"),
    ("create_glue_pipeline_result", "create_glue_pipeline"),
    ("create_quality_checks_result", "create_quality_checks"),
    ("run_pipeline_result", "run_pipeline"),
]

# SFN Error names with unambiguous step attribution. job_completion sends
# send_task_failure(error="GlueJobFailed") for the RunPipeline task token.
ERROR_NAME_TO_STEP: dict[str, str] = {
    "GlueJobFailed": "run_pipeline",
}

# ---------------------------------------------------------------------------
# Approval-gate timeouts (p34 R6.5)
#
# The SFN Catch error object carries only {Error, Cause}, so nothing in $.error
# says WHICH state timed out. The three waitForTaskToken approval gates are
# identified from the execution state instead: each writes its ResultPath key
# only when its approval is GRANTED, and the Choice state in front of it reads
# a field that is still in the state when RecordFailure runs.
#
#   gate                          ResultPath key            approval_type
#   WaitForSchemaMappingApproval  $.schema_mapping_approval  schema_mapping
#   ForceTestReviewApproval       $.test_review_approval     test_failure
#   WaitForCodeApproval           $.code_approval            code_generation
#
# The approval_type strings are the ones the gates' own Payloads carry
# (deploy-cdk/lib/orchestration-stack.ts — 'schema_mapping', 'test_failure',
# 'code_generation'), i.e. the value the approval record itself is keyed by.
# ---------------------------------------------------------------------------

TIMEOUT_ERROR_NAME = "States.Timeout"
APPROVAL_REJECTED_ERROR_NAME = "ApprovalRejected"

# ---------------------------------------------------------------------------
# Pipeline error rate — the failure half
#
# Vendored from shared/metrics_facade.py (RUN_ERROR_RATE_*), which this Lambda
# does not ship with. One datapoint of 100 per failed run; job_completion
# publishes 0 per successful run, so the series' Average is the percentage of
# finished runs that failed, which PipelineErrorRateAlarm reads.
# tests/contracts/test_pipeline_error_rate_cross_language.py pins the copies.
# ---------------------------------------------------------------------------
ERROR_RATE_NAMESPACE = "AgenticDataIngestion/Pipeline"
ERROR_RATE_METRIC = "Pipeline.ErrorRate"
ERROR_RATE_DIMENSION = "Environment"

APPROVAL_TYPE_SCHEMA_MAPPING = "schema_mapping"
APPROVAL_TYPE_TEST_FAILURE = "test_failure"
APPROVAL_TYPE_CODE_GENERATION = "code_generation"

# ---------------------------------------------------------------------------
# Step-name canonicalization (P28 R8.2, task 14.2)
#
# Vendored subset of shared/step_names.from_any: this Lambda is deliberately
# deployed WITHOUT the shared layer (see module docstring — a RecordFailure
# crash must never mask the original failure, so its dependency surface is
# stdlib + boto3 only). Keep the alias table in sync with
# shared/step_names.py; tests/unit/lambdas/record_failure cross-check it
# against the shared module.
# ---------------------------------------------------------------------------

_CANONICAL_STEP_NAMES: tuple[str, ...] = (
    "file_validation",
    "schema_extract",
    "schema_compare",
    "check_schema_action",
    "schema_mapping",
    "check_schema_mapping_approval",
    "create_code",
    "test_code",
    "check_code_approval",
    "create_infrastructure",
    "create_glue_pipeline",
    "create_quality_checks",
    "run_pipeline",
)

# Legacy variants that are not a casing/word-separator variant of a canonical
# name (mirrors shared/step_names._LEGACY_ALIASES).
_LEGACY_STEP_ALIASES: dict[str, str] = {
    "codegeneration": "create_code",
    "codetesting": "test_code",
    "schemamappingapproval": "check_schema_mapping_approval",
    "codeapproval": "check_code_approval",
    "infrastructure": "create_infrastructure",
    "firstrun": "create_quality_checks",
}


def _normalize(value: str) -> str:
    """Casing/word-separator-insensitive normal form (mirrors step_names)."""
    return value.replace("_", "").replace("-", "").lower()


_STEP_LOOKUP: dict[str, str] = {_normalize(name): name for name in _CANONICAL_STEP_NAMES}
_STEP_LOOKUP.update(_LEGACY_STEP_ALIASES)


def canonicalize_step_name(value: Any) -> str:
    """Best-effort canonicalization of an inbound step name.

    Maps canonical names, PascalCase SFN state ids, and the cataloged legacy
    variants onto the canonical snake_case name. Unknown or empty values pass
    through unchanged (never raise — honest pass-through beats masking the
    original failure with a lookup error).
    """
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return _STEP_LOOKUP.get(_normalize(text), text)


def _loads_or_none(text: Any) -> Any:
    """json.loads that returns None instead of raising on non-JSON input."""
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def infer_failed_step(error_name: str, state: dict[str, Any]) -> str:
    """Best-effort step attribution when the Cause has no StepFailure JSON.

    Prefers the error-name map (e.g. GlueJobFailed → run_pipeline), then the
    first step whose result key is missing after the last completed one.
    """
    if error_name in ERROR_NAME_TO_STEP:
        return ERROR_NAME_TO_STEP[error_name]

    last_present = -1
    for idx, (key, _step) in enumerate(PROGRESS_ORDER):
        if key in state:
            last_present = idx

    next_idx = last_present + 1
    if next_idx < len(PROGRESS_ORDER):
        return PROGRESS_ORDER[next_idx][1]
    return "unknown"


def _as_dict(value: Any) -> dict[str, Any]:
    """The value if it is a dict, else {} — never raises on a garbled state."""
    return value if isinstance(value, dict) else {}


def _regeneration_pending(test_code_result: dict[str, Any]) -> bool:
    """TestResultChoice's regeneration branch (P32 D2), mirrored.

    A failing run whose failures are regeneration triggers and whose attempt
    budget is not spent loops back to CreateCode INSIDE this execution, so it
    has not reached ForceTestReviewApproval. The previous test_code_result
    stays in the state until TestCode overwrites it, so without this guard a
    timeout on the regenerating CreateCode would read as a forced-review
    approval timeout.
    """
    if test_code_result.get("regeneration_needed") is not True:
        return False
    attempt = test_code_result.get("regeneration_attempt")
    limit = test_code_result.get("regeneration_max_attempts")
    if isinstance(attempt, bool) or isinstance(limit, bool):
        return False
    if not isinstance(attempt, int | float) or not isinstance(limit, int | float):
        return False
    return attempt < limit


def pending_approval_type(state: dict[str, Any]) -> str:
    """Which approval gate the execution was parked on, or "" if none (R6.5).

    Mirrors the three Choice states that route into the gates, reading the
    same fields they read:

    * ``TestResultChoice`` — ``$.test_code_result.test_result.status``: any
      status other than ``passed`` routes to ForceTestReviewApproval, unless
      the regeneration branch takes the run back to CreateCode first.
    * ``CheckCodeApproval`` — ``$.test_code_result.approval_required``.
    * ``CheckSchemaMappingApproval`` — ``$.schema_mapping_result.approval_required``.

    The two ``approval_required`` reads are fail-closed (p34 R4.1/R4.2: only an
    explicit boolean ``false`` skips a gate), so a malformed flag is not read as
    "no approval was pending". The downstream-result guards are what keep a
    container-task timeout from being attributed to a gate the run already
    passed through: a gate whose successor produced a result was not the state
    that timed out.

    Best-effort and never raises — an unrecognisable state yields "".
    """
    test_code_result = _as_dict(state.get("test_code_result"))
    if test_code_result:
        test_status = str(_as_dict(test_code_result.get("test_result")).get("status") or "")

        # ForceTestReviewApproval: entered on any non-passed status, and not
        # yet granted (its ResultPath key is absent).
        if (
            test_status
            and test_status != "passed"
            and "test_review_approval" not in state
            and not _regeneration_pending(test_code_result)
        ):
            return APPROVAL_TYPE_TEST_FAILURE

        # WaitForCodeApproval: reachable only from a passed test or from the
        # approve-anyway resume, and not yet granted.
        reached_code_gate = test_status == "passed" or "test_review_approval" in state
        if (
            reached_code_gate
            and "code_approval" not in state
            and "create_infrastructure_result" not in state
            and test_code_result.get("approval_required") is not False
        ):
            return APPROVAL_TYPE_CODE_GENERATION

    # WaitForSchemaMappingApproval: not yet granted, and the run has not moved
    # on to CreateCode (which is where the skip branch goes).
    schema_mapping_result = _as_dict(state.get("schema_mapping_result"))
    if (
        schema_mapping_result
        and "schema_mapping_approval" not in state
        and "create_code_result" not in state
        and schema_mapping_result.get("approval_required") is not False
    ):
        return APPROVAL_TYPE_SCHEMA_MAPPING

    return ""


def approval_timeout_reason(approval_type: str, raw_cause: Any = "") -> str:
    """The recorded failure reason for an approval nobody answered (R6.5).

    Names the approval type and the timeout as the cause. Any raw Cause the
    service supplied is appended rather than replaced, so nothing is lost.
    """
    reason = (
        f"{approval_type} approval timed out ({TIMEOUT_ERROR_NAME}): no approval decision "
        "was recorded before the approval window closed"
    )
    detail = str(raw_cause or "").strip()
    return f"{reason}. {detail}" if detail else reason


def parse_error(error: dict[str, Any], state: dict[str, Any]) -> dict[str, str]:
    """Parse the SFN Catch error object into Failure_Record fields.

    Returns a dict with failed_step, failed_step_source, error_type,
    error_name and error_message (untruncated — the caller bounds sizes).
    """
    error_name = str(error.get("Error") or "Unknown")
    raw_cause = error.get("Cause") or ""

    wrapper = _loads_or_none(raw_cause)
    if isinstance(wrapper, dict) and "errorMessage" in wrapper:
        error_message = str(wrapper.get("errorMessage") or "")

        step_payload = _loads_or_none(error_message)
        if isinstance(step_payload, dict) and "step_name" in step_payload:
            # StepFailure serialization contract (shared/step_errors.py).
            # step_handler names are already canonical; canonicalization
            # tolerates any legacy variant in older/foreign payloads (R8.2).
            return {
                "failed_step": canonicalize_step_name(step_payload.get("step_name")),
                "failed_step_source": "step_failure",
                "error_type": str(step_payload.get("error_type") or "permanent"),
                "error_name": error_name,
                "error_message": str(step_payload.get("message") or error_message),
            }

        # Plain (non-StepFailure) Lambda exception — runtime wrapper only.
        return {
            "failed_step": infer_failed_step(error_name, state),
            "failed_step_source": "inferred",
            "error_type": "unknown",
            "error_name": error_name,
            "error_message": error_message or error_name,
        }

    # Non-JSON cause: States.Timeout, Lambda.ServiceException, GlueJobFailed…
    source = "error_name" if error_name in ERROR_NAME_TO_STEP else "inferred"

    # p34 R6.5: an approval nobody answered gets a reason that names the
    # approval type and the timeout. failed_step, failed_step_source and
    # error_type are left exactly as the generic path computes them — the
    # attribution a timeout at a gate produces is what p16 retry-from-step
    # already consumes, and error_type's domain stays permanent/transient/
    # unknown (a timeout is none of the first two).
    if error_name == TIMEOUT_ERROR_NAME:
        approval_type = pending_approval_type(state)
        if approval_type:
            return {
                "failed_step": infer_failed_step(error_name, state),
                "failed_step_source": source,
                "error_type": "unknown",
                "error_name": error_name,
                "error_message": approval_timeout_reason(approval_type, raw_cause),
            }

    return {
        "failed_step": infer_failed_step(error_name, state),
        "failed_step_source": source,
        "error_type": "unknown",
        "error_name": error_name,
        "error_message": str(raw_cause) or error_name,
    }


def extract_identifiers(state: dict[str, Any], execution_name: str) -> tuple[str, str]:
    """Pull run_id and pipeline_id out of the execution state, defensively.

    run_id: file_validation_result.run_id → top-level run_id (retry runs)
    → execution name → 'unknown'. pipeline_id: the REAL pipeline id from
    file_validation_result.original_pipeline_id, falling back to the
    top-level pipeline_id set by the trigger (the lock key).
    """
    fv = state.get("file_validation_result")
    fv = fv if isinstance(fv, dict) else {}

    run_id = fv.get("run_id") or state.get("run_id") or execution_name or "unknown"
    pipeline_id = fv.get("original_pipeline_id") or state.get("pipeline_id") or "unknown"
    return str(run_id), str(pipeline_id)


def publish_failed_run(error_name: str) -> bool:
    """Publish this run's datapoint on the pipeline error-rate series, unless it was a rejection.

    An ``ApprovalRejected`` run is a human decision, not a failure: the metrics
    dashboard leaves it out of Failed Executions and the success rate, and so
    does the alarm. Every other caught failure counts, approval timeouts
    included. Returns whether a datapoint was published.

    The client is built per call rather than at import, because this function
    runs once per failed run and its import path carries only the DynamoDB
    resource. Best-effort, like the Failure_Record write: an error is logged
    and never raised, so it cannot mask the original failure.
    """
    if error_name == APPROVAL_REJECTED_ERROR_NAME:
        return False
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace=ERROR_RATE_NAMESPACE,
            MetricData=[
                {
                    "MetricName": ERROR_RATE_METRIC,
                    "Dimensions": [{"Name": ERROR_RATE_DIMENSION, "Value": os.environ.get("ENVIRONMENT", "dev")}],
                    "Value": 100.0,
                    "Unit": "Percent",
                }
            ],
        )
        return True
    except Exception:
        logger.exception("Failed to publish the pipeline error-rate datapoint")
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Write the Failure_Record for a caught pipeline failure.

    Payload shape (from the RecordFailure task state):
        {"state": <full execution state incl. $.error>, "execution_name": "…"}
    Direct invocations with the state at the top level are also tolerated.
    """
    state = event.get("state")
    if not isinstance(state, dict):
        state = event

    execution_name = str(event.get("execution_name") or "")

    error = state.get("error")
    if not isinstance(error, dict):
        top_error = event.get("error")
        error = top_error if isinstance(top_error, dict) else {}

    run_id, pipeline_id = extract_identifiers(state, execution_name)
    parsed = parse_error(error, state)
    timestamp = datetime.now(UTC).isoformat()

    item: dict[str, Any] = {
        "run_id": run_id,
        "step_name": FAILURE_STEP_NAME,
        # Legacy synthetic id — kept for dashboard / e2e compatibility.
        "pipeline_id": f"{run_id}.99",
        "original_pipeline_id": pipeline_id,
        "status": "error",
        "timestamp": timestamp,
        "step_sequence": FAILURE_STEP_SEQUENCE,
        "failed_step": parsed["failed_step"],
        "failed_step_source": parsed["failed_step_source"],
        "error_type": parsed["error_type"],
        "error_name": parsed["error_name"],
        "error_message": parsed["error_message"][:MAX_MESSAGE_CHARS],
        "error_cause": str(error.get("Cause") or "")[:MAX_CAUSE_CHARS],
        "execution_name": execution_name,
    }

    logger.info(
        "Recording failure for run %s: step=%s (%s) error=%s",
        run_id,
        parsed["failed_step"],
        parsed["failed_step_source"],
        parsed["error_name"],
    )

    recorded = True
    try:
        state_table.put_item(Item=item)
    except Exception:
        # A RecordFailure crash must not mask the original failure — log and
        # proceed so the state machine still releases the lock and reaches
        # the PipelineFailed state with the ORIGINAL error intact.
        logger.exception("Failed to write Failure_Record for run %s", run_id)
        recorded = False

    publish_failed_run(parsed["error_name"])

    return {
        "statusCode": 200,
        "status": "FAILED",
        "run_id": run_id,
        "failed_step": parsed["failed_step"],
        "recorded": recorded,
    }
