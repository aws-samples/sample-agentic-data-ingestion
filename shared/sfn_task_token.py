# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step Functions task-token callback for container-backed steps (P28 R19.11).

The four container-backed steps (create_code, test_code, create_glue_pipeline,
create_infrastructure) run as ``waitForTaskToken`` task states so the proxy
Lambda does NOT idle-bill for the container's full duration:

1. The task state passes ``$$.Task.Token`` as ``task_token`` in the Lambda
   payload; the proxy Lambda forwards the payload (token included) to the
   AgentCore container.
2. The container entrypoint pops the token, registers a background task with
   the AgentCore runtime app (ping status → HealthyBusy, which keeps the
   microVM scheduled), spawns the work on a thread, and returns an immediate
   dispatch acknowledgment — so the proxy Lambda returns within seconds.
3. When the background work finishes, the container calls
   ``SendTaskSuccess`` (same JSON shape the handler returns, so downstream
   states like TestResultChoice read the exact payload they read today) or
   ``SendTaskFailure`` with the exception class name as ``error`` — the SFN
   Retry/Catch ``ErrorEquals`` keep matching ``StepFailure`` /
   ``TransientStepFailure`` exactly as they do for Lambda invocation errors
   (composes with P28 R1).

Callback ownership (exactly one sender per execution path):
    - Flag ON, new container: the container entrypoint pops ``task_token``
      before invoking the vendored handler, backgrounds the work, and owns
      the callback (:func:`dispatch_background_step`).
    - Flag ON, old container (version skew during deploy): the container ran
      the work synchronously and returned a final result instead of the
      dispatch ack — the Lambda-side :func:`step_task_token` wrapper detects
      the missing ack marker and relays the result via ``SendTaskSuccess``
      so the execution is never stranded.
    - Flag OFF (direct in-Lambda execution): the handler result reaches the
      :func:`step_task_token` wrapper with the token still in the event —
      the wrapper sends ``SendTaskSuccess`` itself (SFN discards the Lambda
      return value in ``waitForTaskToken`` mode).
    - Failures need no Lambda-side ``SendTaskFailure``: an unhandled raise is
      a Lambda invocation error, which fails a ``waitForTaskToken`` task
      immediately with the exception class name as the error — identical to
      the pre-token behavior.

Orphan discipline (mirrors run_pipeline's token-before-work ordering, task
6.1): the token travels IN the dispatch payload, so a dispatch failure raises
before any work starts (no orphan window), and the task state's explicit
``TimeoutSeconds`` is the backstop for a container that crashes after
acknowledging the dispatch.
"""

import json
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import ClientError

from step_errors import TRANSIENT_CLIENT_ERROR_CODES, StepFailure, TransientStepFailure

logger = logging.getLogger(__name__)

#: Marker key on the container's immediate response that acknowledges the
#: async dispatch. Its presence tells the Lambda-side wrapper that the
#: container owns the SendTaskSuccess/SendTaskFailure callback.
ASYNC_DISPATCH_MARKER = "agentcore_async_dispatch"

#: Payload field carrying the Step Functions task token (set by the task
#: state's ``'task_token.$': '$$.Task.Token'``).
TASK_TOKEN_FIELD = "task_token"

#: ClientError codes meaning the token was already consumed or the task is no
#: longer waiting (duplicate send, or the SFN task timed out first). The
#: execution is not listening anymore — sending again can never succeed, so
#: these terminate cleanly instead of raising. Mirrors job_completion's
#: idempotent-send classification (P28 R5.2).
TOKEN_CONSUMED_ERROR_CODES = frozenset({"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"})

#: SendTaskFailure API limits (AWS-defined).
_MAX_ERROR_CHARS = 256
_MAX_CAUSE_CHARS = 32768

#: Handler signature shared by the step Lambdas: ``(event, context) -> result``.
StepHandler = Callable[[Any, Any], Any]


def _sfn_client(region: str | None = None) -> Any:
    """Step Functions client (region defaults to AWS_REGION, like the proxy)."""
    return boto3.client("stepfunctions", region_name=region or os.environ.get("AWS_REGION", "us-west-2"))


def build_dispatch_ack(step_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Immediate container response acknowledging an async dispatch.

    Step Functions discards this value (the real result arrives via
    ``SendTaskSuccess``); the proxy Lambda and the :func:`step_task_token`
    wrapper use the marker to know the container owns the callback.
    """
    return {
        ASYNC_DISPATCH_MARKER: True,
        "status": "dispatched",
        "step_name": step_name,
        "run_id": str(payload.get("run_id", "")),
        "pipeline_id": str(payload.get("pipeline_id", "")),
    }


def send_task_success(
    task_token: str,
    result: dict[str, Any],
    *,
    step_name: str,
    region: str | None = None,
) -> None:
    """Send the step result to Step Functions via ``SendTaskSuccess``.

    The serialized ``result`` lands at the task state's ``ResultPath`` with
    the exact shape the handler returned — downstream states (e.g.
    TestResultChoice reading ``$.test_code_result.test_result.status``) see
    the same payload as the pre-token synchronous shape.

    Already-consumed/expired tokens (:data:`TOKEN_CONSUMED_ERROR_CODES`) log
    and return: the execution is no longer waiting on this token (duplicate
    send or the task timed out first) and re-sending can never succeed. Any
    other ClientError raises with the proxy's transient/permanent
    classification so the failure is loud and SFN Retry policies can fire.
    """
    try:
        _sfn_client(region).send_task_success(
            taskToken=task_token,
            output=json.dumps(result, default=str),
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code in TOKEN_CONSUMED_ERROR_CODES:
            logger.warning(
                "Task token for step '%s' already consumed or expired (%s) — "
                "duplicate send or the SFN task timed out first; nothing to do",
                step_name,
                error_code,
            )
            return
        message = exc.response.get("Error", {}).get("Message", str(exc))
        details = {"error_code": error_code, "operation": "send_task_success"}
        if error_code in TRANSIENT_CLIENT_ERROR_CODES:
            raise TransientStepFailure(
                step_name, f"send_task_success failed: {error_code}: {message}", details=details
            ) from exc
        raise StepFailure(step_name, f"send_task_success failed: {error_code}: {message}", details=details) from exc


def send_task_failure_for_exception(
    task_token: str,
    exc: BaseException,
    *,
    step_name: str,
    region: str | None = None,
) -> None:
    """Report a failed background step to Step Functions via ``SendTaskFailure``.

    ``error`` is the exception CLASS NAME (``StepFailure`` /
    ``TransientStepFailure`` — the vendored handlers raise through
    ``step_handler``, which guarantees one of the two), so the task state's
    Retry/Catch ``ErrorEquals`` match exactly as they do for Lambda
    invocation errors. ``cause`` mimics the Lambda runtime error envelope
    (``{"errorMessage", "errorType"}``) so the RecordFailure Lambda's Cause
    parsing recovers the structured StepFailure JSON for authoritative step
    attribution (its documented path 1).

    Best-effort by design: this runs on a container background thread where a
    raise would only kill the thread. Send failures are logged (consumed
    token → warning, anything else → error) and swallowed — the task state's
    explicit ``TimeoutSeconds`` is the backstop that fails the execution.
    """
    error_name = type(exc).__name__[:_MAX_ERROR_CHARS]
    cause = json.dumps({"errorMessage": str(exc), "errorType": type(exc).__name__}, default=str)[:_MAX_CAUSE_CHARS]
    try:
        _sfn_client(region).send_task_failure(taskToken=task_token, error=error_name, cause=cause)
    except ClientError as send_exc:
        error_code = send_exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code in TOKEN_CONSUMED_ERROR_CODES:
            logger.warning(
                "Task token for step '%s' already consumed or expired (%s) while reporting failure",
                step_name,
                error_code,
            )
            return
        logger.error(
            "send_task_failure for step '%s' failed (%s) — the task state's TimeoutSeconds is the backstop",
            step_name,
            error_code,
            exc_info=True,
        )
    except Exception:
        logger.error(
            "send_task_failure for step '%s' failed — the task state's TimeoutSeconds is the backstop",
            step_name,
            exc_info=True,
        )


def dispatch_background_step(
    *,
    app: Any,
    handler: StepHandler,
    payload: dict[str, Any],
    task_token: str,
    step_name: str,
    region: str | None = None,
) -> dict[str, Any]:
    """Run a step on a container background thread and ack the dispatch.

    Used by the AgentCore container entrypoints: registers the work with the
    runtime app's async-task tracking (``add_async_task`` flips the ``/ping``
    status to ``HealthyBusy``, which keeps the microVM scheduled while the
    background thread runs — bedrock-agentcore SDK contract), spawns the
    handler on a thread that closes the loop via ``SendTaskSuccess`` /
    ``SendTaskFailure``, and returns the immediate dispatch ack so the proxy
    Lambda (and its idle billing) stops within seconds.

    ``app`` is duck-typed (``add_async_task(name)`` / ``complete_async_task(id)``)
    so this module never imports ``bedrock_agentcore`` — it must stay
    importable inside the Lambda layer and the test environment.
    """
    task_id = app.add_async_task(step_name)

    def _run_and_callback() -> None:
        try:
            result = handler(payload, None)
        except BaseException as exc:  # noqa: BLE001 — thread boundary: report, never die silently
            logger.exception("Step '%s' background execution failed — sending task failure", step_name)
            send_task_failure_for_exception(task_token, exc, step_name=step_name, region=region)
        else:
            try:
                send_task_success(task_token, result, step_name=step_name, region=region)
            except Exception:
                logger.exception(
                    "send_task_success for step '%s' failed — the task state's TimeoutSeconds is the backstop",
                    step_name,
                )
        finally:
            app.complete_async_task(task_id)

    thread = threading.Thread(target=_run_and_callback, name=f"{step_name}-task-token-callback")
    try:
        thread.start()
    except BaseException:
        # Could not even spawn the worker: clear the busy state and raise so
        # the entrypoint errors, the proxy raises TransientStepFailure (HTTP
        # 5xx), and SFN Retry re-dispatches. No orphaned execution.
        app.complete_async_task(task_id)
        raise
    return build_dispatch_ack(step_name, payload)


def step_task_token(step_name: str) -> Callable[[StepHandler], StepHandler]:
    """Decorator closing the task-token loop on the Lambda side (P28 R19.11).

    Apply INSIDE ``@step_handler`` on the four container-backed step
    handlers::

        @step_handler("create_code")
        @step_task_token("create_code")
        def lambda_handler(event, context): ...

    Behavior when the event carries ``task_token``:
        - result carries :data:`ASYNC_DISPATCH_MARKER` → the container
          acknowledged the dispatch and owns the callback; pass through.
        - any other result → this Lambda owns the callback (direct in-Lambda
          execution with the flag off, or an old container that ran the work
          synchronously during a deploy's version-skew window) →
          ``SendTaskSuccess`` with the result.
        - raises propagate untouched: the Lambda invocation error fails the
          ``waitForTaskToken`` task with the exception class name, exactly
          like the pre-token shape — no ``SendTaskFailure`` needed here.

    Without a token (direct invocations, tests, container-vendored calls
    where the entrypoint already popped it) the wrapper is a pass-through.
    """

    def decorator(func: StepHandler) -> StepHandler:
        def wrapper(event: Any, context: Any) -> Any:
            result = func(event, context)
            task_token = event.get(TASK_TOKEN_FIELD) if isinstance(event, dict) else None
            if not task_token:
                return result
            if isinstance(result, dict) and result.get(ASYNC_DISPATCH_MARKER):
                logger.info("Step '%s' dispatched to AgentCore container — container owns the callback", step_name)
                return result
            send_task_success(task_token, result, step_name=step_name)
            return result

        wrapper.__name__ = getattr(func, "__name__", "lambda_handler")
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator
