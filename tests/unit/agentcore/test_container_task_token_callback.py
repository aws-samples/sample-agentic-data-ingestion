# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Container entrypoints dispatch task-token work asynchronously (P28 R19.11).

When the proxied payload carries a Step Functions task token, each container
entrypoint must:

1. POP the token from the payload — the entrypoint is the single callback
   owner inside the container; the vendored handler's own step_task_token
   wrapper must never see it (double-send guard).
2. Hand the work to ``sfn_task_token.dispatch_background_step`` with the
   runtime ``app`` (async-task tracking keeps /ping at HealthyBusy so the
   microVM stays scheduled) and return the immediate dispatch ack so the
   proxy Lambda stops idle-billing within seconds.

Without a token, the synchronous delegation contract is unchanged.

Validates: P28 Requirements 19.11
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

AGENTCORE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "agentcore")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

CONTAINERS = ["create_code", "test_code", "create_glue_pipeline", "create_infrastructure"]

TOKEN = "AAAAKgAAAAIAAAAAAAAAAexample-task-token"
SAMPLE_RESULT = {"statusCode": 200, "run_id": "run-1"}


class _FakeRuntimeApp:
    """Real (non-MagicMock) stand-in so @app.entrypoint keeps the function."""

    def __init__(self, *args, **kwargs):
        self.added = []
        self.completed = []

    def entrypoint(self, func):
        self.entrypoint_fn = func
        return func

    def add_async_task(self, name, metadata=None):
        self.added.append(name)
        return 7

    def complete_async_task(self, task_id):
        self.completed.append(task_id)
        return True

    def run(self, *args, **kwargs):  # pragma: no cover - never called in tests
        pass


def _fake_bedrock_agentcore_modules():
    runtime_mod = types.ModuleType("bedrock_agentcore.runtime")
    runtime_mod.BedrockAgentCoreApp = _FakeRuntimeApp
    pkg = types.ModuleType("bedrock_agentcore")
    pkg.runtime = runtime_mod
    return {"bedrock_agentcore": pkg, "bedrock_agentcore.runtime": runtime_mod}


def _load_main(container: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(AGENTCORE_DIR, container, "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCreateCodeAsyncDispatch:
    """Behavioral checks on the create_code entrypoint (the pattern is
    identical across the four containers — static checks below pin the rest)."""

    def _run_handler(self, payload):
        module_name = "agentcore_create_code_task_token_main"
        mock_lambda_handler = MagicMock(return_value=SAMPLE_RESULT)
        mock_dispatch = MagicMock(return_value={"agentcore_async_dispatch": True, "status": "dispatched"})
        with (
            patch.dict("sys.modules", _fake_bedrock_agentcore_modules()),
            patch.dict("sys.modules", {"lambda_function": MagicMock(lambda_handler=mock_lambda_handler)}),
            patch("sfn_task_token.dispatch_background_step", mock_dispatch),
        ):
            module = _load_main("create_code", module_name)
            try:
                result = module.handler(payload)
            finally:
                sys.modules.pop(module_name, None)
        return result, module, mock_lambda_handler, mock_dispatch

    def test_token_payload_dispatches_background_and_acks(self):
        payload = {"run_id": "run-1", "pipeline_id": "pipe-1", "task_token": TOKEN}

        result, module, mock_lambda_handler, mock_dispatch = self._run_handler(payload)

        # Immediate ack returned — the proxy Lambda stops idle-billing here
        assert result["agentcore_async_dispatch"] is True
        kwargs = mock_dispatch.call_args[1]
        assert kwargs["app"] is module.app
        assert kwargs["task_token"] == TOKEN
        assert kwargs["step_name"] == "create_code"
        # The token was POPPED before the work payload was handed over: the
        # vendored handler's own step_task_token wrapper must never see it.
        assert "task_token" not in kwargs["payload"]
        assert kwargs["payload"]["run_id"] == "run-1"
        # The entrypoint itself must not run the work synchronously
        mock_lambda_handler.assert_not_called()

    def test_tokenless_payload_keeps_synchronous_delegation(self):
        payload = {"run_id": "run-1", "pipeline_id": "pipe-1"}

        result, _module, mock_lambda_handler, mock_dispatch = self._run_handler(payload)

        assert result == SAMPLE_RESULT
        mock_lambda_handler.assert_called_once_with(payload, None)
        mock_dispatch.assert_not_called()


class TestAsyncDispatchPresence:
    """All four containers carry the identical dispatch pattern (static pins,
    mirroring the recursion-guard style checks)."""

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_main_py_pops_task_token(self, container):
        """The token must be POPPED (single callback owner) before the
        vendored handler runs."""
        with open(os.path.join(AGENTCORE_DIR, container, "main.py")) as f:
            content = f.read()
        assert "payload.pop('task_token', None)" in content, (
            f"{container}/main.py must pop task_token before invoking the vendored handler"
        )

    @pytest.mark.parametrize("container", CONTAINERS)
    def test_main_py_uses_shared_dispatch(self, container):
        with open(os.path.join(AGENTCORE_DIR, container, "main.py")) as f:
            content = f.read()
        assert "dispatch_background_step" in content, (
            f"{container}/main.py must dispatch token work via sfn_task_token.dispatch_background_step"
        )
        assert f"step_name='{container}'" in content, (
            f"{container}/main.py must dispatch with its canonical step name"
        )
