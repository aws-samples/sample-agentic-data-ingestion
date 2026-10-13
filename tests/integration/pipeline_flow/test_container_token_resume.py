# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration tests: container task-token callback resumes execution (P28 R19.11).

The four container-backed steps (create_code, test_code, create_glue_pipeline,
create_infrastructure) run as ``waitForTaskToken`` task states: the proxy
Lambda dispatches the token to the AgentCore container and returns within
seconds; the container closes the loop via SendTaskSuccess/SendTaskFailure.
These tests exercise the full dispatch→callback loop across the REAL modules
(shared/sfn_task_token.py, shared/step_errors.py, the RecordFailure Lambda)
with only the AWS boundary substituted.

moto capability finding (probed against moto 5.2.2, the pinned version): moto
does NOT model ``waitForTaskToken`` — a task state with Resource
``arn:aws:states:::lambda:invoke.waitForTaskToken`` executes as a Pass state
(the execution history shows PassStateEntered/PassStateExited and the
execution completes immediately), and ``send_task_success`` accepts fabricated
tokens without validating or resuming anything. A "real" state-machine resume
test against moto would therefore assert nothing. Per design.md's integration
strategy ("SFN failure paths simulated at the Lambda level"), the SFN surface
is simulated exactly at its documented contract boundaries:

    - SendTaskSuccess ``output`` → parsed JSON lands at the task state's
      ResultPath (``$.test_code_result`` …) — the resume payload.
    - SendTaskFailure ``error``/``cause`` → the Catch object
      ``{"Error": <error>, "Cause": <cause>}`` delivered at ``$.error`` —
      consumed by the REAL RecordFailure Lambda against moto DynamoDB.

Validates: Requirements 19.11
"""

import importlib.util
import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_SHARED_DIR = _PROJECT_ROOT / "shared"

if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from sfn_task_token import (
    ASYNC_DISPATCH_MARKER,
    build_dispatch_ack,
    dispatch_background_step,
    step_task_token,
)
from step_errors import StepFailure, TransientStepFailure

TOKEN = "AAAAKgAAAAIAAAAAAAAAAintegration-task-token"
RUN_ID = "run-2026-01-01-00-00-00-tokenres1"
PIPELINE_ID = "customer-pipeline"
EXECUTION_NAME = "exec-itest-token-resume"

# The Retry policy on the container-backed task states
# (deploy-cdk/lib/orchestration-stack.ts CONTAINER_STEP_RETRY) — the error
# name the container sends must literally match this entry for SFN to retry.
RETRYABLE_ERROR_EQUALS = "TransientStepFailure"


class _FakeRuntimeApp:
    """Duck-typed BedrockAgentCoreApp async-task tracking (unit-test pattern)."""

    def __init__(self):
        self.added: list[str] = []
        self.completed: list[int] = []
        self.completed_event = threading.Event()

    def add_async_task(self, name, metadata=None):
        self.added.append(name)
        return 7

    def complete_async_task(self, task_id):
        self.completed.append(task_id)
        self.completed_event.set()
        return True


def _load_record_failure_module():
    """Load the real RecordFailure Lambda under a unique alias.

    Called INSIDE a test (after mock_lambda_env set the table-name env vars
    and the moto context is active) so its import-time boto3 resource binds
    to moto.
    """
    alias = "itest_token_record_failure_lambda"
    sys.modules.pop(alias, None)
    spec = importlib.util.spec_from_file_location(
        alias,
        str(_LAMBDA_ROOT / "agentic_data_ingestion_record_failure" / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _dispatch_and_capture(handler, step_name: str) -> tuple[dict, MagicMock, _FakeRuntimeApp]:
    """Run the REAL container-side dispatch and wait for the callback.

    Returns (dispatch ack, mocked sfn client, fake app) after the background
    thread has closed the loop.
    """
    sfn = MagicMock()
    app = _FakeRuntimeApp()
    with patch("sfn_task_token.boto3.client", return_value=sfn):
        ack = dispatch_background_step(
            app=app,
            handler=handler,
            payload={"run_id": RUN_ID, "pipeline_id": PIPELINE_ID},
            task_token=TOKEN,
            step_name=step_name,
            region="us-east-1",
        )
        assert app.completed_event.wait(timeout=10), "background callback never completed"
    return ack, sfn, app


# ---------------------------------------------------------------------------
# 1. Success path: the container result resumes the execution
# ---------------------------------------------------------------------------


class TestContainerResultResumesExecution:
    """A container-produced result travels SendTaskSuccess → ResultPath →
    next state, with the proxy Lambda returning immediately at dispatch.

    Validates: Requirements 19.11
    """

    def test_dispatch_ack_then_callback_resumes_with_exact_handler_result(self):
        """Full loop: proxy ack pass-through → container callback → resume payload.

        The handler result is the canonical test_code shape; the resumed
        execution must read ``test_result.status == "passed"`` at the exact
        JSONPath TestResultChoice evaluates.
        """
        handler_result = {
            "statusCode": 200,
            "run_id": RUN_ID,
            "test_result": {
                "status": "passed",
                "checks": [{"name": "row_count", "passed": True, "detail": "10 == 10"}],
                "report_s3_uri": "s3://test-outputs/report.json",
            },
        }

        # Container side (REAL dispatch_background_step): immediate ack,
        # background thread owns the callback.
        ack, sfn, app = _dispatch_and_capture(lambda payload, ctx: handler_result, "test_code")

        assert ack[ASYNC_DISPATCH_MARKER] is True
        assert ack["run_id"] == RUN_ID
        # /ping stayed HealthyBusy while the work ran, then cleared
        assert app.added == ["test_code"]
        assert app.completed == [7]

        # Lambda side (REAL step_task_token relay): the proxy returns the
        # container's ack as-is and does NOT double-send — the container is
        # the single callback owner. This is the moment idle billing stops.
        lambda_side_send = MagicMock()
        with patch("sfn_task_token.send_task_success", lambda_side_send):
            proxied = step_task_token("test_code")(lambda event, ctx: ack)(
                {"run_id": RUN_ID, "task_token": TOKEN}, None
            )
        assert proxied == ack
        lambda_side_send.assert_not_called()

        # The container callback carried the EXACT handler JSON.
        sfn.send_task_failure.assert_not_called()
        kwargs = sfn.send_task_success.call_args[1]
        assert kwargs["taskToken"] == TOKEN
        assert json.loads(kwargs["output"]) == handler_result

        # SFN contract: the SendTaskSuccess output lands at the task state's
        # ResultPath ($.test_code_result). Simulate that placement and
        # evaluate the documented TestResultChoice condition —
        # $.test_code_result.test_result.status == "passed" → CheckCodeApproval.
        execution_state = {"file_validation_result": {"run_id": RUN_ID}}
        execution_state["test_code_result"] = json.loads(kwargs["output"])
        assert execution_state["test_code_result"]["test_result"]["status"] == "passed"

    def test_resume_payload_shape_is_identical_to_pre_token_lambda_invoke(self):
        """SendTaskSuccess output round-trips the handler result byte-exactly.

        Downstream JSONPath consumers were written against the old
        payloadResponseOnly LambdaInvoke shape; the token shape must hand
        them the same document (key order and values preserved through the
        JSON round-trip).
        """
        handler_result = {
            "statusCode": 200,
            "run_id": RUN_ID,
            "code_version_id": "v-123",
            "code_s3_uri": "s3://generated-code/v-123/etl.py",
        }

        _ack, sfn, _app = _dispatch_and_capture(lambda payload, ctx: handler_result, "create_code")

        kwargs = sfn.send_task_success.call_args[1]
        resumed = json.loads(kwargs["output"])
        assert resumed == handler_result
        # The fields TestCode's payload bindings pluck from $.create_code_result
        assert resumed["code_version_id"] == "v-123"
        assert resumed["code_s3_uri"] == "s3://generated-code/v-123/etl.py"


# ---------------------------------------------------------------------------
# 2. Failure path: the container failure fails the execution honestly
# ---------------------------------------------------------------------------


class TestContainerFailureFailsExecutionHonestly:
    """SendTaskFailure(error, cause) → Catch object → REAL RecordFailure →
    Failure_Record with authoritative step attribution.

    Validates: Requirements 19.11
    """

    def test_step_failure_cause_envelope_lands_full_failure_record(self, mock_dynamodb_tables, mock_lambda_env):
        """End-to-end honesty check: the cause envelope the container sends is
        exactly what RecordFailure's most-structured parse path consumes.

        Chain: handler raises StepFailure → send_task_failure(error=<class
        name>, cause=<runtime envelope with the stable StepFailure JSON>) →
        SFN Catch ``{"Error", "Cause"}`` at $.error → real RecordFailure
        handler → Failure_Record in the moto state table with
        failed_step_source='step_failure' (authoritative, not inferred).
        """

        def failing_handler(payload, ctx):
            raise StepFailure(
                "create_code",
                "AI transform generation failed for field customer_id",
                details={"field": "customer_id"},
            )

        _ack, sfn, _app = _dispatch_and_capture(failing_handler, "create_code")

        sfn.send_task_success.assert_not_called()
        kwargs = sfn.send_task_failure.call_args[1]
        assert kwargs["taskToken"] == TOKEN
        # SFN Retry/Catch ErrorEquals match on the error name — the class name
        # keeps the failure semantics identical to a Lambda invocation error.
        assert kwargs["error"] == "StepFailure"

        # SFN surfaces SendTaskFailure's params verbatim as the Catch object.
        catch_error_object = {"Error": kwargs["error"], "Cause": kwargs["cause"]}

        # The execution state as it exists when CreateCode's Catch fires
        # (completed steps at their resultPath keys, error at $.error).
        execution_state = {
            "pipeline_id": PIPELINE_ID,
            "file_validation_result": {
                "statusCode": 200,
                "run_id": RUN_ID,
                "original_pipeline_id": PIPELINE_ID,
                "source_format": "csv",
            },
            "schema_extract_result": {"statusCode": 200, "run_id": RUN_ID},
            "schema_compare_result": {"statusCode": 200, "schema_change_action": "proceed"},
            "schema_mapping_result": {"statusCode": 200, "mapping_id": "map-1"},
            "error": catch_error_object,
        }

        rf_module = _load_record_failure_module()
        result = rf_module.lambda_handler({"state": execution_state, "execution_name": EXECUTION_NAME}, MagicMock())
        assert result["recorded"] is True

        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        record = state_table.get_item(Key={"run_id": RUN_ID, "step_name": "pipeline_failure"})["Item"]

        # Authoritative attribution recovered from the StepFailure JSON that
        # traveled inside the container's cause envelope — never inference.
        assert record["failed_step"] == "create_code"
        assert record["failed_step_source"] == "step_failure"
        assert record["error_type"] == "permanent"
        assert record["error_name"] == "StepFailure"
        assert "AI transform generation failed for field customer_id" in record["error_message"]
        assert record["run_id"] == RUN_ID
        assert record["original_pipeline_id"] == PIPELINE_ID
        assert record["execution_name"] == EXECUTION_NAME

    def test_transient_failure_error_name_matches_sfn_retry_policy(self):
        """A transient container failure is retryable BY NAME.

        The container sends error=<exception class name>; the task states'
        Retry policy matches ``ErrorEquals: ["TransientStepFailure", ...]``
        (pinned against the synthesized template in
        orchestration-stack.test.ts). The two sides of that contract meet on
        this literal string.
        """

        def throttled_handler(payload, ctx):
            raise TransientStepFailure("test_code", "AgentCore runtime throttled the dispatch")

        _ack, sfn, _app = _dispatch_and_capture(throttled_handler, "test_code")

        kwargs = sfn.send_task_failure.call_args[1]
        assert kwargs["error"] == RETRYABLE_ERROR_EQUALS
        # The cause still carries the parseable envelope for RecordFailure
        # (relevant when retries exhaust and the Catch fires).
        envelope = json.loads(kwargs["cause"])
        step_payload = json.loads(envelope["errorMessage"])
        assert step_payload["step_name"] == "test_code"
        assert step_payload["error_type"] == "transient"

    def test_dispatch_ack_from_failing_step_still_returned_before_failure(self):
        """The ack precedes the failure: the proxy already returned (billing
        stopped) by the time the background failure is reported — exactly the
        contract that makes the explicit TimeoutSeconds the only backstop."""

        started = threading.Event()

        def slow_failing_handler(payload, ctx):
            started.wait(timeout=10)
            raise StepFailure("create_infrastructure", "warehouse bucket creation failed")

        sfn = MagicMock()
        app = _FakeRuntimeApp()
        with patch("sfn_task_token.boto3.client", return_value=sfn):
            ack = dispatch_background_step(
                app=app,
                handler=slow_failing_handler,
                payload={"run_id": RUN_ID},
                task_token=TOKEN,
                step_name="create_infrastructure",
                region="us-east-1",
            )
            # Ack is in hand while the work has not even begun.
            assert ack[ASYNC_DISPATCH_MARKER] is True
            assert not sfn.send_task_failure.called
            started.set()
            assert app.completed_event.wait(timeout=10), "background callback never completed"

        assert sfn.send_task_failure.call_args[1]["error"] == "StepFailure"


# ---------------------------------------------------------------------------
# 3. Ack contract compatibility across the two sides
# ---------------------------------------------------------------------------


class TestDispatchAckContract:
    """The ack the container emits is the ack the Lambda relay recognizes —
    the two modules can never drift apart on the marker.

    Validates: Requirements 19.11
    """

    def test_container_ack_satisfies_lambda_side_marker_check(self):
        """build_dispatch_ack (container) and step_task_token (Lambda) agree
        on ASYNC_DISPATCH_MARKER: the relay never double-sends for a real
        container ack, and always relays for a plain (version-skew) result."""
        ack = build_dispatch_ack("create_glue_pipeline", {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID})

        relay_send = MagicMock()
        with patch("sfn_task_token.send_task_success", relay_send):
            # Container acked → pass-through, no Lambda-side send
            step_task_token("create_glue_pipeline")(lambda e, c: ack)({"task_token": TOKEN}, None)
            relay_send.assert_not_called()

            # Old container returned a final result (version skew) → the
            # Lambda closes the loop so the execution is never stranded.
            final = {"statusCode": 200, "run_id": RUN_ID}
            step_task_token("create_glue_pipeline")(lambda e, c: final)({"task_token": TOKEN}, None)
            relay_send.assert_called_once()
            args, _kwargs = relay_send.call_args
            assert args[0] == TOKEN
            assert args[1] == final
