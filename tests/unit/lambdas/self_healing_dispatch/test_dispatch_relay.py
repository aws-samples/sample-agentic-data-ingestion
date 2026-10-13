# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: self-healing dispatch Lambda relays to AgentCore (P28 R9.3).

The dispatch Lambda is the async (InvocationType="Event") target of
job_completion's fire-and-forget invoke. It owns the long SYNCHRONOUS
AgentCore Runtime call inside its own 15-minute timeout, with a read timeout
sized to outlive the agent's ~10-minute workload (fixing the 60s boto3
default that killed the old daemon-thread pattern).

Failure semantics: errors RAISE so Lambda's built-in async retry covers
transient AgentCore hiccups — the original run is unaffected either way
(job_completion already sent its Step Functions callback).

Validates: P28 Requirements 9.3
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_dispatch_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_self_healing_dispatch"
    )
)
sys.path.insert(0, _dispatch_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod

from step_errors import StepFailure

_ENDPOINT = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/adi_agent_self_healing-abc/invocations?qualifier=DEFAULT"


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


@pytest.fixture(autouse=True)
def _endpoint_env(monkeypatch):
    monkeypatch.setenv("SELF_HEALING_AGENT_ENDPOINT", _ENDPOINT)
    yield


def _payload():
    return {
        "pipeline_id": "pipe-1",
        "run_id": "run-1",
        "job_name": "agentic-data-ingestion-pipe-1",
        "job_run_id": "jr-1",
        "error_message": "OutOfMemoryError",
        "state": "FAILED",
    }


def test_forwards_event_payload_to_agentcore_with_long_read_timeout():
    """The received event is forwarded unchanged to the self-healing runtime,
    with a read timeout sized for the ~10-minute agent workload."""
    mock_invoke = MagicMock(return_value={"statusCode": 200, "healing_id": "h-1"})

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke):
        result = _mod.lambda_handler(_payload(), None)

    assert result["statusCode"] == 200
    assert result["run_id"] == "run-1"
    mock_invoke.assert_called_once()
    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["endpoint"] == _ENDPOINT
    assert kwargs["payload"] == _payload()
    assert kwargs["step_name"] == "self_healing_dispatch"
    # 60s default read timeout could never survive the agent workload; the
    # dispatcher must request one that outlives ~10 minutes and stays inside
    # its 900s Lambda timeout.
    assert 600 < kwargs["read_timeout"] <= 900


def test_missing_endpoint_configuration_raises(monkeypatch):
    """No SELF_HEALING_AGENT_ENDPOINT → raise (visible async-invoke failure),
    never a silent drop."""
    monkeypatch.setenv("SELF_HEALING_AGENT_ENDPOINT", "")
    mock_invoke = MagicMock()

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke), pytest.raises(RuntimeError):
        _mod.lambda_handler(_payload(), None)

    mock_invoke.assert_not_called()


def test_agentcore_failure_propagates_for_async_retry():
    """Proxy failures raise out of the handler so Lambda's built-in async
    retry (2 attempts) covers transient AgentCore hiccups."""
    mock_invoke = MagicMock(side_effect=StepFailure("self_healing_dispatch", "container returned HTTP 500"))

    with patch.object(_mod, "invoke_agentcore_runtime", mock_invoke), pytest.raises(StepFailure):
        _mod.lambda_handler(_payload(), None)
