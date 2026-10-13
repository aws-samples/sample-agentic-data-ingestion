# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: generic agent dispatch Lambda (p30a R1.2, R2.2).

This Lambda is the async (``InvocationType="Event"``) target that owns the
long SYNCHRONOUS AgentCore Runtime call inside its own 15-minute timeout —
the replacement for the frozen-thread pattern, generalised from
``self_healing_dispatch`` so any caller can reuse it.

Two behaviours are pinned here:

* payload construction for both endpoint forms (``agent_endpoint_env``,
  the name of an env var, and ``agent_endpoint``, a literal URL), including
  the long read timeout that the 60s boto3 default cannot substitute for;
* the missing-endpoint path, which must be an honest handled response
  saying the agent is not configured — never a bare success, and never an
  unhandled crash.

Agent-side failures propagate so Lambda's async retry sees them.

Validates: Requirements 1.2, 2.2
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region/account must be set before any boto3 import.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_dispatch_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_agent_dispatch")
)
sys.path.insert(0, _dispatch_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod

from step_errors import StepFailure, TransientStepFailure

_ENDPOINT = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com"
    "/runtimes/agentic_data_ingestion_agent_cost_optimization-abc/invocations?qualifier=DEFAULT"
)
_ENDPOINT_ENV = "COST_OPTIMIZATION_AGENT_ENDPOINT"


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Guard against another test file's lambda_function winning the cache."""
    sys.modules["lambda_function"] = _mod
    yield


def _payload():
    return {"action": "weekly_scan", "trigger": "schedule"}


# ---------------------------------------------------------------------------
# Payload construction — both endpoint forms
# ---------------------------------------------------------------------------


def test_resolves_endpoint_from_named_env_var_and_forwards_payload(mock_lambda_env, monkeypatch):
    """``agent_endpoint_env`` names an env var; its value is the endpoint, and
    ``payload`` reaches the agent unchanged."""
    monkeypatch.setenv(_ENDPOINT_ENV, _ENDPOINT)
    mock_invoke = MagicMock(return_value={"statusCode": 200, "recommendations": []})

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler(
            {"agent_endpoint_env": _ENDPOINT_ENV, "payload": _payload(), "step_name": "cost_optimization"},
            None,
        )

    assert result["statusCode"] == 200
    assert result["dispatched"] is True
    assert result["step_name"] == "cost_optimization"
    assert result["result"] == {"statusCode": 200, "recommendations": []}

    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["endpoint"] == _ENDPOINT
    assert kwargs["payload"] == _payload()
    assert kwargs["step_name"] == "cost_optimization"
    # The agent call must outlive a ~10-minute workload while staying inside
    # the 900s Lambda timeout — the 60s boto3 default cannot.
    assert 600 < kwargs["read_timeout"] <= 900


def test_accepts_literal_endpoint_without_any_env_var(mock_lambda_env):
    """``agent_endpoint`` carries the URL directly — no env var involved."""
    mock_invoke = MagicMock(return_value={"statusCode": 200})

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler({"agent_endpoint": _ENDPOINT, "payload": _payload()}, None)

    assert result["statusCode"] == 200
    assert result["dispatched"] is True
    # No step_name in the event → the shared dispatcher default.
    assert result["step_name"] == "agent_dispatch"
    assert mock_invoke.call_args.kwargs["endpoint"] == _ENDPOINT
    assert mock_invoke.call_args.kwargs["step_name"] == "agent_dispatch"


def test_literal_endpoint_wins_over_env_var_form(mock_lambda_env, monkeypatch):
    """Both forms present → the literal is used, so a caller can override
    configuration without editing the environment."""
    monkeypatch.setenv(_ENDPOINT_ENV, "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/other/invocations")
    mock_invoke = MagicMock(return_value={"statusCode": 200})

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        _mod.lambda_handler(
            {"agent_endpoint": _ENDPOINT, "agent_endpoint_env": _ENDPOINT_ENV, "payload": _payload()},
            None,
        )

    assert mock_invoke.call_args.kwargs["endpoint"] == _ENDPOINT


def test_absent_payload_forwards_empty_dict(mock_lambda_env):
    """A dispatch with no payload still invokes the agent, with ``{}`` — an
    EventBridge rule whose only job is "run now" needs no arguments."""
    mock_invoke = MagicMock(return_value={"statusCode": 200})

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler({"agent_endpoint": _ENDPOINT}, None)

    assert result["dispatched"] is True
    assert mock_invoke.call_args.kwargs["payload"] == {}


# ---------------------------------------------------------------------------
# Missing endpoint — honest handled response (R2.3 posture)
# ---------------------------------------------------------------------------


def test_unset_endpoint_env_var_returns_not_configured_and_dispatches_nothing(mock_lambda_env, monkeypatch):
    """Named env var unset → a body saying the agent is not configured, and
    no agent call. Not a success, not a crash."""
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)
    mock_invoke = MagicMock()

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler({"agent_endpoint_env": _ENDPOINT_ENV, "payload": _payload()}, None)

    mock_invoke.assert_not_called()
    assert result["statusCode"] == 400
    assert result["dispatched"] is False
    assert result["error"] == "agent_not_configured"
    assert "not configured" in result["body"]
    assert _ENDPOINT_ENV in result["body"]


def test_empty_endpoint_env_var_is_treated_as_unset(mock_lambda_env, monkeypatch):
    """An env var present but empty is a misconfiguration, not an endpoint."""
    monkeypatch.setenv(_ENDPOINT_ENV, "")
    mock_invoke = MagicMock()

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler({"agent_endpoint_env": _ENDPOINT_ENV}, None)

    mock_invoke.assert_not_called()
    assert result["statusCode"] == 400
    assert result["error"] == "agent_not_configured"


def test_event_naming_no_endpoint_at_all_returns_not_configured(mock_lambda_env):
    """Neither form present → honest response naming the omission."""
    mock_invoke = MagicMock()

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler({"payload": _payload()}, None)

    mock_invoke.assert_not_called()
    assert result["statusCode"] == 400
    assert result["error"] == "agent_not_configured"
    assert "agent_endpoint_env" in result["body"]


# ---------------------------------------------------------------------------
# Agent failures propagate for Lambda async retry
# ---------------------------------------------------------------------------


def test_terminal_agent_failure_propagates(mock_lambda_env):
    """A terminal proxy failure raises out of the handler so it lands in
    Lambda's async failure metrics rather than being swallowed."""
    mock_invoke = MagicMock(side_effect=StepFailure("cost_optimization", "container returned HTTP 500"))

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke), pytest.raises(StepFailure):
        _mod.lambda_handler({"agent_endpoint": _ENDPOINT, "step_name": "cost_optimization"}, None)


def test_transient_agent_failure_propagates(mock_lambda_env):
    """A retryable failure also raises, so Lambda's built-in async retry
    (2 attempts) covers transient AgentCore hiccups."""
    mock_invoke = MagicMock(side_effect=TransientStepFailure("cost_optimization", "ThrottlingException"))

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke), pytest.raises(TransientStepFailure):
        _mod.lambda_handler({"agent_endpoint": _ENDPOINT}, None)
