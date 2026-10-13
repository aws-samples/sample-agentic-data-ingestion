# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Retry utilities for Lambda handlers to detect retry context.

Provides helper functions that individual pipeline step Lambdas can use
to determine whether they should skip execution during a retry run
(because their step already completed in the original run).

Resume mechanism (p32 2.18)
---------------------------
:func:`should_skip_step` shipped in P16 (`LGR-2600abb-1`) with no production
caller — the P16 commit body records that the CDK skip states built alongside
it were reverted to "the original linear chain", so the deciding half existed
and the acting half did not. Task 12 confirmed that live: a retry-from-step
execution re-entered ``FileValidation``, the step RAN, and its output
**overwrote** the pre-seeded ``file_validation_result``, after which the next
state's ``$.file_validation_result.run_id`` could not be found. A retry was a
full re-run that was *more* fragile than a fresh run.

:func:`resolve_step_resume` is that missing acting half and the first
production caller of :func:`should_skip_step`. Three constraints shape it:

1. **The served value must be the step's SFN return contract, not its
   display artifact.** The ``{step}_result`` values the retry endpoint
   pre-seeds (``rerun_contract`` ``cached_step_results`` →
   ``pipelines_runs.py``) come from the state row's ``output_data``, which
   ``shared/artifact_schema.create_output_data`` fixes to five keys
   (``artifact_type``, ``primary_id``, ``status``, ``summary``, ``s3_uris``).
   Downstream ASL reads ``$.file_validation_result.run_id``,
   ``.original_pipeline_id``, ``.source_format``,
   ``$.schema_extract_result.extracted_schema`` — **none** of which
   ``output_data`` carries. Serving it would reproduce exactly the JSONPath
   failure task 12 observed. So the value a step returns to Step Functions is
   cached separately, by :func:`cache_step_result`, under
   :data:`SFN_RESULT_ATTRIBUTE`.

2. **No cached value ⟹ do not skip.** Skipping with nothing to serve writes
   ``null`` at the step's ``resultPath`` and breaks the next state — the same
   failure by another route. Falling through to execute is P16's own
   documented handling ("Cached result missing for a skipped step → falls
   through to execute the step instead of skipping") and is what makes this
   safe on a retry of a run recorded before the cache existed.

3. **The cache annotates an existing row; it never creates one.** The write
   is conditional on ``attribute_exists(run_id)``, the same technique
   `LGR-620555b-1` uses for the ghost-approval condition, so a step that
   never wrote its state row cannot leave a phantom row behind for the
   dashboard's step readers to render.
"""

import logging
import os
import sys
from collections.abc import Mapping
from typing import Any, Final

import boto3

# Add shared directory to path for env_utils import
sys.path.append(os.path.join(os.path.dirname(__file__)))

from decimal_utils import convert_decimals_to_float, convert_floats_to_decimal
from env_utils import require_env
from step_names import SFN_STATE_ORDER, StepName, from_any

logger = logging.getLogger(__name__)

#: Execution-input key naming the step a retry resumes at. Normalized to an
#: SFN state id by ``rerun_contract.build_trigger_input``.
RESUME_MARKER_KEY: Final[str] = "retry_from_step"

#: State-row attribute holding the value a step returned to Step Functions —
#: the step's ``resultPath`` contract, distinct from the ``output_data``
#: display artifact on the same row. See constraint 1 in the module docstring.
SFN_RESULT_ATTRIBUTE: Final[str] = "sfn_result"

#: Marker stamped onto a served result so a resumed step is identifiable in
#: execution state and in the state row it flows into. 2.18 forbids a silent
#: re-execution; it equally forbids a silent skip.
RESUMED_FROM_CACHE_KEY: Final[str] = "resumed_from_cache"

#: Both spellings of the task-token field in use. ``task_token`` is
#: ``sfn_task_token.TASK_TOKEN_FIELD``, carried by the four container-backed
#: steps; ``taskToken`` is RunPipeline's, which manages its own callback
#: (``run_pipeline/lambda_function.py:105``).
#:
#: Decision D3: a step whose completion is reported by RETURNING is resumable;
#: a step whose completion is reported by a TASK TOKEN is not. ``step_handler``
#: is the OUTERMOST decorator on all four container steps
#: (``@step_handler`` then ``@step_task_token``), so a short-circuit there
#: would return before the token is ever sent and the ``waitForTaskToken``
#: state would hang for its full ``TimeoutSeconds`` — trading a wasteful
#: re-execution for a 20-minute stall, which is worse. The five resumable
#: steps are exactly the ``tasks.LambdaInvoke`` states.
TASK_TOKEN_FIELDS: Final[tuple[str, ...]] = ("task_token", "taskToken")

# Canonical pipeline step order — the SFN state ids in execution order,
# adopted from shared/step_names.py (P28 R8.2, task 14.2). Kept as SFN
# state ids because the rerun contract normalizes retry_from_step to the
# SFN state id and dashboard callers echo these values.
STEP_ORDER: list[str] = list(SFN_STATE_ORDER)


def _to_step_or_none(value: str | None) -> StepName | None:
    """Canonicalize any known step-name variant; None for unknown/missing."""
    if not value:
        return None
    try:
        return from_any(value)
    except ValueError:
        return None


def should_skip_step(event: dict, current_step: str, step_order: list[str]) -> bool:
    """Return True if this step should be skipped during a retry.

    During a retry execution, steps that completed before the failed step
    should be skipped. This function compares the current step's position
    in the pipeline ordering against the retry_from_step position.

    P28 R8.2 (task 14.2): both sides of the comparison are normalized via
    ``step_names.from_any`` — a snake_case ``failed_step`` from a
    Failure_Record and a PascalCase SFN state id in ``retry_from_step``
    meet on the canonical StepName, so casing variants can no longer make
    the comparison silently miss.

    Args:
        event: The Lambda event containing optional retry_from_step field.
        current_step: The name of the step currently executing (any known
            variant: canonical snake_case, SFN state id, or legacy alias).
        step_order: Ordered list of all pipeline step names (any variant).

    Returns:
        True if the step should be skipped (it completed before the retry point).
    """
    retry_from = _to_step_or_none(event.get("retry_from_step"))
    if retry_from is None:
        return False

    current = _to_step_or_none(current_step)
    if current is None:
        return False

    canonical_order = [step for step in (_to_step_or_none(name) for name in step_order) if step is not None]

    current_idx = canonical_order.index(current) if current in canonical_order else -1
    retry_idx = canonical_order.index(retry_from) if retry_from in canonical_order else -1

    return current_idx < retry_idx and current_idx >= 0


def get_cached_step_result(run_id: str, step_name: str) -> dict | None:
    """Fetch the cached result for a completed step from the state table.

    Queries the state table for the given run_id + step_name and returns
    the output_data if the step completed successfully.

    Args:
        run_id: The original run ID to look up.
        step_name: The step name to fetch cached results for.

    Returns:
        The output_data dict if the step succeeded, or None.
    """
    try:
        state_table_name = require_env("STATE_TABLE", "agentic_data_ingestion_state")
        dynamodb = boto3.resource("dynamodb")
        state_table = dynamodb.Table(state_table_name)

        resp = state_table.get_item(Key={"run_id": run_id, "step_name": step_name})
        item = resp.get("Item")

        if not item:
            return None

        status = (item.get("status") or "").lower()
        if status != "success":
            return None

        return item.get("output_data")
    except Exception as e:
        logger.warning(f"Failed to fetch cached step result for {run_id}/{step_name}: {e}")
        return None


def resume_marker(event: Mapping[str, Any]) -> str:
    """Return the event's resume marker, or ``""`` when it carries none.

    ``""`` rather than ``None`` because the value is echoed into execution
    state for downstream ASL Payloads to read, and an absent JSONPath in a
    Payload is a ``States.ParameterPathFailure`` that no Retry or Catch can
    intercept. A guaranteed-present empty string reads as "not a retry"
    through :func:`should_skip_step`, which already returns ``False`` for a
    falsy marker (``:61-63``).
    """
    value = event.get(RESUME_MARKER_KEY)
    return value if isinstance(value, str) else ""


def _state_table() -> Any:
    """The state table resource. Isolated so callers can fail soft around it."""
    state_table_name = require_env("STATE_TABLE", "agentic_data_ingestion_state")
    return boto3.resource("dynamodb").Table(state_table_name)


def cache_step_result(run_id: str, step_name: str, result: Any) -> bool:
    """Record the value *step_name* returned to Step Functions on its state row.

    This is the write half of the resume mechanism. It stores the step's
    ``resultPath`` contract — the dict the handler returned — which is a
    different shape from the ``output_data`` artifact the same row carries
    (module docstring, constraint 1). Serving ``output_data`` at a
    ``resultPath`` is what broke the retry task 12 observed.

    Never raises: a cache miss on a later retry costs a re-executed step,
    while a raise here would fail a step that had already succeeded.

    Args:
        run_id: The run whose state row is being annotated.
        step_name: Any known step-name variant; normalized before use so the
            read and the write agree on one key.
        result: The value the handler returned. Non-dict returns are ignored —
            only a JSON object can be merged into execution state at a
            ``resultPath``.

    Returns:
        True when the row was annotated, False otherwise (including the
        conditional-check failure that means the row does not exist).
    """
    if not run_id or not isinstance(result, dict):
        return False

    step = _to_step_or_none(step_name)
    if step is None:
        return False

    try:
        # attribute_exists(run_id) — annotate an EXISTING row only. Without
        # it update_item would CREATE a row carrying neither `status` nor
        # `output_data`, and the dashboard's step readers treat a state row as
        # a step. Same technique as `LGR-620555b-1`'s ghost-approval guard.
        _state_table().update_item(
            Key={"run_id": run_id, "step_name": str(step)},
            UpdateExpression="SET #attr = :value",
            ExpressionAttributeNames={"#attr": SFN_RESULT_ATTRIBUTE},
            ExpressionAttributeValues={":value": convert_floats_to_decimal(result)},
            ConditionExpression="attribute_exists(run_id)",
        )
        return True
    except Exception as e:
        logger.warning(f"Could not cache the SFN result for {run_id}/{step}: {e}")
        return False


def get_cached_sfn_result(run_id: str, step_name: str) -> dict | None:
    """Read back a value cached by :func:`cache_step_result`.

    Returns None when the row, the attribute, or a success status is missing —
    every one of which means "nothing safe to serve", and therefore
    "execute the step" (module docstring, constraint 2).
    """
    if not run_id:
        return None

    step = _to_step_or_none(step_name)
    if step is None:
        return None

    try:
        item = _state_table().get_item(Key={"run_id": run_id, "step_name": str(step)}).get("Item")
    except Exception as e:
        logger.warning(f"Could not read the cached SFN result for {run_id}/{step}: {e}")
        return None

    if not item:
        return None
    if (item.get("status") or "").lower() != "success":
        # A failed or in-flight step proved nothing worth resuming from.
        return None

    cached = item.get(SFN_RESULT_ATTRIBUTE)
    if not isinstance(cached, dict):
        return None

    # DynamoDB reads carry Decimals; Step Functions input must be JSON.
    jsonified: dict = convert_decimals_to_float(cached)
    return jsonified


def resolve_step_resume(event: Mapping[str, Any], step_name: str) -> dict | None:
    """Return the cached result to serve for *step_name*, or None to execute it.

    The production caller of :func:`should_skip_step` (p32 2.18). Called from
    ``shared/step_errors.step_handler`` before the wrapped handler runs, so
    all ten decorated step Lambdas get the behaviour from one place.

    2.18 offers "skipped or served from their cached results" as alternatives,
    but task 12's finding collapses them into one: a re-run step's own
    ``resultPath`` clobbers the pre-seeded ``{step}_result`` key, so whichever
    is chosen the key must survive the step it belongs to. Serving the cached
    value AS the skipped step's return does exactly that — the key is
    re-emitted through the same ``resultPath``, carrying the bytes the
    original run put there.

    Args:
        event: The step's Lambda event. Must carry :data:`RESUME_MARKER_KEY`
            and ``run_id`` for a resume to be possible; a retry's ``run_id``
            IS the original run id (``rerun_contract.build_trigger_input``),
            so the cached rows resolve without a second identifier.
        step_name: The step being considered, any known variant.

    Returns:
        The result to return in the step's place, or None when the step must
        run — no resume marker, this step is at or after the resume point, no
        run identity, or nothing cached to serve.
    """
    if not should_skip_step(dict(event), step_name, STEP_ORDER):
        return None

    # Decision D3 (see TASK_TOKEN_FIELDS): never short-circuit a step whose
    # completion is reported by a task token — the token would never be sent
    # and the state would stall until its timeout.
    held_token = next((field for field in TASK_TOKEN_FIELDS if event.get(field)), None)
    if held_token is not None:
        logger.info(
            f"Step '{step_name}' is before the resume point but holds a {held_token} — "
            f"executing it, because a served result would never close the callback"
        )
        return None

    run_id = event.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        logger.info(f"Step '{step_name}' is before the resume point but the event carries no run_id — executing it")
        return None

    cached = get_cached_sfn_result(run_id, step_name)
    if cached is None:
        # Constraint 2: refuse to skip with nothing to serve. Writing null at
        # the resultPath is the failure task 12 observed, by another route.
        logger.info(
            f"Step '{step_name}' is before the resume point for run {run_id} but has no cached "
            f"{SFN_RESULT_ATTRIBUTE} to serve — executing it rather than emptying its resultPath"
        )
        return None

    served = dict(cached)
    # Keep the resume marker alive across a served step: downstream Payloads
    # read it from the FileValidation result, and the cached bytes were
    # written by a run that was not itself a retry.
    served[RESUME_MARKER_KEY] = resume_marker(event)
    served[RESUMED_FROM_CACHE_KEY] = True
    logger.info(f"Resuming run {run_id}: serving step '{step_name}' from its cached result instead of re-executing it")
    return served
