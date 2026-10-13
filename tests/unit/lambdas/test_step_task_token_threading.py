# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handler-side task-token threading for the container-backed steps (P28 R19.11).

The four container-backed step Lambdas (create_code, test_code,
create_glue_pipeline, create_infrastructure) run under waitForTaskToken task
states. The token arrives in the event (`'task_token.$': '$$.Task.Token'`)
and must be threaded through unchanged:

- Proxy path (USE_AGENTCORE_RUNTIME=true): the FULL event — token included —
  is forwarded to the container (the proxy is token-transparent); the
  container acks the dispatch and owns the callback, so the Lambda-side
  wrapper passes the ack through WITHOUT sending.
- Version-skew / inline path: a plain (non-ack) result with a token present
  means this Lambda owns the callback — the step_task_token wrapper relays
  the result via SendTaskSuccess (SFN discards the Lambda return value in
  waitForTaskToken mode; the token callback is the only channel).
- Decorator composition is load-bearing: @step_handler OUTSIDE
  @step_task_token, so raises pass through the token wrapper untouched and
  step_handler's StepFailure conversion still produces the Lambda invocation
  error whose class name the SFN Retry/Catch ErrorEquals match.

Validates: P28 Requirements 19.11
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from sfn_task_token import build_dispatch_ack

TOKEN = "AAAAKgAAAAIAAAAAAAAAAexample-task-token"

STEP_BY_LAMBDA_DIR = {
    "agentic_data_ingestion_create_code": "create_code",
    "agentic_data_ingestion_test_code": "test_code",
    "agentic_data_ingestion_create_glue_pipeline": "create_glue_pipeline",
    "agentic_data_ingestion_create_infrastructure": "create_infrastructure",
}


def _handler_source(lambda_dir: str) -> str:
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "lambda", lambda_dir, "lambda_function.py"
    )
    with open(path) as f:
        return f.read()


class TestTokenDecoratorPresence:
    """Static pins: every container-backed handler threads the token."""

    @pytest.mark.parametrize(("lambda_dir", "step_name"), sorted(STEP_BY_LAMBDA_DIR.items()))
    def test_handler_composes_step_handler_over_step_task_token(self, lambda_dir, step_name):
        """@step_handler directly above @step_task_token directly above
        lambda_handler — the order keeps raises as Lambda invocation errors
        with StepFailure class names (no SendTaskFailure needed Lambda-side)."""
        source = _handler_source(lambda_dir)
        pattern = (
            rf'@step_handler\("{step_name}"\)\s*\n'
            rf'@step_task_token\("{step_name}"\)\s*\n'
            r"def lambda_handler\("
        )
        assert re.search(pattern, source), (
            f"{lambda_dir}: lambda_handler must be decorated @step_handler over "
            f'@step_task_token (both with "{step_name}")'
        )

    @pytest.mark.parametrize("lambda_dir", sorted(STEP_BY_LAMBDA_DIR))
    def test_handler_imports_step_task_token(self, lambda_dir):
        source = _handler_source(lambda_dir)
        assert "from sfn_task_token import step_task_token" in source


class TestCreateCodeTokenThreading:
    """Behavioral checks through the create_code handler (the decorator and
    proxy pattern are identical across the four — static pins above freeze
    the rest)."""

    ENV = {
        "USE_AGENTCORE_RUNTIME": "true",
        "AGENTCORE_CREATE_CODE_ENDPOINT": "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/abc/invocations",
        "AWS_DEFAULT_REGION": "us-west-2",
        "AWS_REGION": "us-west-2",
        "STATE_TABLE": "test-state",
        "PIPELINES_TABLE": "test-pipelines",
        "GENERATED_CODE_TABLE": "test-code",
        "MAPPINGS_TABLE": "test-mappings",
        "PROCESSED_BUCKET": "test-bucket",
        "TEMPLATES_BUCKET": "test-templates",
    }

    def _invoke(self, event, proxy_return, mock_send_success):
        """Import create_code's lambda_function with heavy deps mocked and
        invoke it with the proxy patched (mirrors test_agentcore_feature_flag)."""
        lambda_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code"
        )
        sys.path.insert(0, lambda_dir)
        mock_eb = MagicMock()
        mock_eb.get_eventbridge_publishers = MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
        try:
            with (
                patch.dict(os.environ, self.ENV),
                patch.dict(
                    "sys.modules",
                    {
                        "strands": MagicMock(),
                        "strands.Agent": MagicMock(),
                        "aws_lambda_powertools": MagicMock(),
                        "strands_mcp_factory": MagicMock(),
                        "code_validator": MagicMock(),
                        "agentcore_client": MagicMock(),
                        "env_utils": MagicMock(),
                        "prompt_template_loader": MagicMock(),
                        "artifact_schema": MagicMock(),
                        "metrics_facade": MagicMock(),
                        "eventbridge_utils": mock_eb,
                        "jinja2": MagicMock(),
                    },
                ),
                patch("agentcore_proxy.invoke_agentcore_runtime", return_value=proxy_return) as mock_proxy,
                patch("sfn_task_token.send_task_success", mock_send_success),
            ):
                if "lambda_function" in sys.modules:
                    del sys.modules["lambda_function"]
                from lambda_function import lambda_handler

                result = lambda_handler(event, None)
            return result, mock_proxy
        finally:
            sys.path.remove(lambda_dir)
            sys.modules.pop("lambda_function", None)

    def test_token_forwarded_to_container_and_ack_passes_through(self):
        """The proxy payload is the FULL event (token included) and the
        container's dispatch ack returns without a Lambda-side send — the
        container owns the SendTaskSuccess/SendTaskFailure callback."""
        event = {"run_id": "run-1", "pipeline_id": "pipe-1", "task_token": TOKEN}
        ack = build_dispatch_ack("create_code", event)
        mock_send = MagicMock()

        result, mock_proxy = self._invoke(event, proxy_return=ack, mock_send_success=mock_send)

        assert result == ack
        # Token threaded through: the proxied payload IS the event, token intact
        assert mock_proxy.call_args[1]["payload"]["task_token"] == TOKEN
        mock_send.assert_not_called()

    def test_synchronous_result_with_token_relays_send_task_success(self):
        """Version-skew window (old container ran the work synchronously) or
        inline execution: a plain result + token → the Lambda closes the loop."""
        event = {"run_id": "run-1", "pipeline_id": "pipe-1", "task_token": TOKEN}
        final_result = {"statusCode": 200, "run_id": "run-1", "code_version_id": "v1"}
        mock_send = MagicMock()

        result, _ = self._invoke(event, proxy_return=final_result, mock_send_success=mock_send)

        assert result == final_result
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == TOKEN
        assert args[1] == final_result
        assert kwargs["step_name"] == "create_code"

    def test_tokenless_event_never_sends(self):
        """Without a token the wrapper is a pass-through (direct invocations,
        container-vendored calls where the entrypoint already popped it)."""
        event = {"run_id": "run-1", "pipeline_id": "pipe-1"}
        final_result = {"statusCode": 200, "run_id": "run-1"}
        mock_send = MagicMock()

        result, _ = self._invoke(event, proxy_return=final_result, mock_send_success=mock_send)

        assert result == final_result
        mock_send.assert_not_called()
