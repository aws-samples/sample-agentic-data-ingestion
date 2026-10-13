# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for shared/sfn_task_token.py — task-token callback for container steps.

P28 R19.11: container-backed steps run under waitForTaskToken so the proxy
Lambda does not idle-bill for the container's full duration. This module owns
the callback contract:

- ``step_task_token`` (Lambda side): pass-through for dispatch acks (container
  owns the callback), SendTaskSuccess relay for direct execution and
  version-skew results, raises propagate untouched.
- ``dispatch_background_step`` (container side): registers the work with the
  runtime app's async-task tracking, backgrounds the handler, acks
  immediately, and closes the loop via SendTaskSuccess/SendTaskFailure.
- ``send_task_failure_for_exception`` serializes the exception class name as
  the SFN ``error`` (Retry/Catch ErrorEquals keep matching StepFailure /
  TransientStepFailure) and a Lambda-runtime-shaped ``cause`` envelope so
  RecordFailure's Cause parsing recovers authoritative step attribution.

Validates: P28 Requirements 19.11
"""

import json
import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Add shared to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from sfn_task_token import (
    ASYNC_DISPATCH_MARKER,
    TASK_TOKEN_FIELD,
    build_dispatch_ack,
    dispatch_background_step,
    send_task_failure_for_exception,
    send_task_success,
    step_task_token,
)
from step_errors import StepFailure, TransientStepFailure

TOKEN = "AAAAKgAAAAIAAAAAAAAAAexample-task-token"
RESULT = {"statusCode": 200, "run_id": "run-1", "test_result": {"status": "passed"}}


def _client_error(code: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "SendTaskSuccess")


class _FakeApp:
    """Duck-typed stand-in for BedrockAgentCoreApp async-task tracking."""

    def __init__(self):
        self.added: list[str] = []
        self.completed: list[int] = []
        self.completed_event = threading.Event()

    def add_async_task(self, name, metadata=None):
        self.added.append(name)
        return 42

    def complete_async_task(self, task_id):
        self.completed.append(task_id)
        self.completed_event.set()
        return True


# ── build_dispatch_ack ──────────────────────────────────────────────


def test_build_dispatch_ack_carries_marker_and_identifiers():
    ack = build_dispatch_ack("create_code", {"run_id": "r-1", "pipeline_id": "p-1"})
    assert ack[ASYNC_DISPATCH_MARKER] is True
    assert ack["status"] == "dispatched"
    assert ack["step_name"] == "create_code"
    assert ack["run_id"] == "r-1"
    assert ack["pipeline_id"] == "p-1"


# ── send_task_success ───────────────────────────────────────────────


@patch("sfn_task_token.boto3.client")
def test_send_task_success_sends_result_json(mock_client_fn):
    sfn = MagicMock()
    mock_client_fn.return_value = sfn

    send_task_success(TOKEN, RESULT, step_name="test_code", region="us-west-2")

    kwargs = sfn.send_task_success.call_args[1]
    assert kwargs["taskToken"] == TOKEN
    # The output is the handler result verbatim — downstream states (e.g.
    # TestResultChoice reading test_result.status) see the same shape as the
    # pre-token synchronous LambdaInvoke.
    assert json.loads(kwargs["output"]) == RESULT


@patch("sfn_task_token.boto3.client")
def test_send_task_success_consumed_token_terminates_cleanly(mock_client_fn):
    """TaskTimedOut/TaskDoesNotExist/InvalidToken: the execution is no longer
    listening — log and return instead of raising (mirrors job_completion)."""
    sfn = MagicMock()
    sfn.send_task_success.side_effect = _client_error("TaskTimedOut")
    mock_client_fn.return_value = sfn

    send_task_success(TOKEN, RESULT, step_name="test_code")  # must not raise


@patch("sfn_task_token.boto3.client")
def test_send_task_success_throttle_raises_transient(mock_client_fn):
    sfn = MagicMock()
    sfn.send_task_success.side_effect = _client_error("ThrottlingException")
    mock_client_fn.return_value = sfn

    with pytest.raises(TransientStepFailure) as exc_info:
        send_task_success(TOKEN, RESULT, step_name="test_code")
    assert exc_info.value.details["error_code"] == "ThrottlingException"


@patch("sfn_task_token.boto3.client")
def test_send_task_success_access_denied_raises_permanent(mock_client_fn):
    sfn = MagicMock()
    sfn.send_task_success.side_effect = _client_error("AccessDeniedException")
    mock_client_fn.return_value = sfn

    with pytest.raises(StepFailure) as exc_info:
        send_task_success(TOKEN, RESULT, step_name="test_code")
    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.step_name == "test_code"


# ── send_task_failure_for_exception ────────────────────────────────


@patch("sfn_task_token.boto3.client")
def test_send_task_failure_uses_exception_class_name_as_error(mock_client_fn):
    """error=<ExceptionClassName> so SFN Retry/Catch ErrorEquals keep matching
    StepFailure/TransientStepFailure exactly like Lambda invocation errors."""
    sfn = MagicMock()
    mock_client_fn.return_value = sfn
    exc = StepFailure("create_code", "generation failed", details={"attempt": 1})

    send_task_failure_for_exception(TOKEN, exc, step_name="create_code")

    kwargs = sfn.send_task_failure.call_args[1]
    assert kwargs["taskToken"] == TOKEN
    assert kwargs["error"] == "StepFailure"
    # Cause mimics the Lambda runtime error envelope so RecordFailure's Cause
    # parsing (path 1) recovers the structured StepFailure JSON.
    cause = json.loads(kwargs["cause"])
    assert cause["errorType"] == "StepFailure"
    inner = json.loads(cause["errorMessage"])
    assert inner["step_name"] == "create_code"
    assert inner["error_type"] == "permanent"
    assert inner["message"] == "generation failed"


@patch("sfn_task_token.boto3.client")
def test_send_task_failure_transient_error_name_is_retryable(mock_client_fn):
    sfn = MagicMock()
    mock_client_fn.return_value = sfn

    send_task_failure_for_exception(
        TOKEN, TransientStepFailure("test_code", "throttled"), step_name="test_code"
    )

    assert sfn.send_task_failure.call_args[1]["error"] == "TransientStepFailure"


@patch("sfn_task_token.boto3.client")
def test_send_task_failure_send_errors_are_swallowed(mock_client_fn):
    """Best-effort from a background thread: a failed failure-send is logged,
    never raised — the task state's TimeoutSeconds is the backstop."""
    sfn = MagicMock()
    sfn.send_task_failure.side_effect = _client_error("AccessDeniedException")
    mock_client_fn.return_value = sfn

    send_task_failure_for_exception(
        TOKEN, StepFailure("create_code", "boom"), step_name="create_code"
    )  # must not raise


@patch("sfn_task_token.boto3.client")
def test_send_task_failure_truncates_error_to_api_limit(mock_client_fn):
    sfn = MagicMock()
    mock_client_fn.return_value = sfn

    long_name_exc = type("X" * 300, (Exception,), {})("boom")
    send_task_failure_for_exception(TOKEN, long_name_exc, step_name="create_code")

    assert len(sfn.send_task_failure.call_args[1]["error"]) <= 256


# ── step_task_token decorator (Lambda side) ─────────────────────────


@patch("sfn_task_token.boto3.client")
def test_decorator_without_token_is_passthrough(mock_client_fn):
    @step_task_token("create_code")
    def handler(event, context):
        return RESULT

    assert handler({"run_id": "r-1"}, None) == RESULT
    mock_client_fn.assert_not_called()


@patch("sfn_task_token.boto3.client")
def test_decorator_dispatch_ack_passes_through_without_send(mock_client_fn):
    """The container acked the dispatch — it owns the callback; the Lambda
    must NOT double-send."""
    ack = build_dispatch_ack("create_code", {"run_id": "r-1"})

    @step_task_token("create_code")
    def handler(event, context):
        return ack

    assert handler({TASK_TOKEN_FIELD: TOKEN}, None) == ack
    mock_client_fn.assert_not_called()


@patch("sfn_task_token.boto3.client")
def test_decorator_plain_result_with_token_sends_success(mock_client_fn):
    """Direct in-Lambda execution (flag off) or a legacy container's
    synchronous result (version skew): the Lambda owns the callback."""
    sfn = MagicMock()
    mock_client_fn.return_value = sfn

    @step_task_token("test_code")
    def handler(event, context):
        return RESULT

    assert handler({TASK_TOKEN_FIELD: TOKEN, "run_id": "r-1"}, None) == RESULT
    kwargs = sfn.send_task_success.call_args[1]
    assert kwargs["taskToken"] == TOKEN
    assert json.loads(kwargs["output"]) == RESULT


@patch("sfn_task_token.boto3.client")
def test_decorator_raise_propagates_without_send(mock_client_fn):
    """A raise is a Lambda invocation error, which already fails the
    waitForTaskToken task with the exception class name — no send needed."""

    @step_task_token("create_code")
    def handler(event, context):
        raise StepFailure("create_code", "boom")

    with pytest.raises(StepFailure):
        handler({TASK_TOKEN_FIELD: TOKEN}, None)
    mock_client_fn.assert_not_called()


# ── dispatch_background_step (container side) ───────────────────────


@patch("sfn_task_token.boto3.client")
def test_dispatch_acks_and_sends_success_from_background_thread(mock_client_fn):
    sfn = MagicMock()
    mock_client_fn.return_value = sfn
    app = _FakeApp()

    ack = dispatch_background_step(
        app=app,
        handler=lambda payload, ctx: RESULT,
        payload={"run_id": "r-1", "pipeline_id": "p-1"},
        task_token=TOKEN,
        step_name="create_code",
        region="us-west-2",
    )

    assert ack[ASYNC_DISPATCH_MARKER] is True
    assert ack["step_name"] == "create_code"
    # Async-task registration keeps /ping at HealthyBusy while the work runs
    assert app.added == ["create_code"]
    assert app.completed_event.wait(timeout=10), "background callback never completed"
    assert app.completed == [42]
    kwargs = sfn.send_task_success.call_args[1]
    assert kwargs["taskToken"] == TOKEN
    assert json.loads(kwargs["output"]) == RESULT
    sfn.send_task_failure.assert_not_called()


@patch("sfn_task_token.boto3.client")
def test_dispatch_handler_failure_sends_task_failure(mock_client_fn):
    sfn = MagicMock()
    mock_client_fn.return_value = sfn
    app = _FakeApp()

    def failing_handler(payload, ctx):
        raise TransientStepFailure("test_code", "container throttled")

    dispatch_background_step(
        app=app,
        handler=failing_handler,
        payload={"run_id": "r-1"},
        task_token=TOKEN,
        step_name="test_code",
    )

    assert app.completed_event.wait(timeout=10), "background callback never completed"
    kwargs = sfn.send_task_failure.call_args[1]
    assert kwargs["error"] == "TransientStepFailure"
    sfn.send_task_success.assert_not_called()
    # Busy state cleared even on failure
    assert app.completed == [42]


@patch("sfn_task_token.boto3.client")
def test_dispatch_success_send_failure_is_contained(mock_client_fn):
    """If SendTaskSuccess itself fails, the thread logs and completes the
    async task — the task state's TimeoutSeconds is the backstop."""
    sfn = MagicMock()
    sfn.send_task_success.side_effect = _client_error("AccessDeniedException")
    mock_client_fn.return_value = sfn
    app = _FakeApp()

    dispatch_background_step(
        app=app,
        handler=lambda payload, ctx: RESULT,
        payload={"run_id": "r-1"},
        task_token=TOKEN,
        step_name="create_code",
    )

    assert app.completed_event.wait(timeout=10), "background callback never completed"
    assert app.completed == [42]


@patch("sfn_task_token.boto3.client")
def test_dispatch_thread_spawn_failure_clears_busy_and_raises(mock_client_fn):
    """A dispatch that cannot even spawn its worker must clear the busy state
    and raise so the proxy fails the invocation and SFN Retry re-dispatches."""
    app = _FakeApp()

    with patch("sfn_task_token.threading.Thread") as thread_cls:
        thread_cls.return_value.start.side_effect = RuntimeError("no threads")
        with pytest.raises(RuntimeError):
            dispatch_background_step(
                app=app,
                handler=lambda payload, ctx: RESULT,
                payload={},
                task_token=TOKEN,
                step_name="create_code",
            )

    assert app.completed == [42]
