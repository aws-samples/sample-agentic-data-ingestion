# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for shared/agentcore_proxy.py — boto3 invoke_agent_runtime proxy.

P28 R1.2: the proxy raises StepFailure (TransientStepFailure for retryable
conditions) on container/proxy errors instead of returning error payloads
that Step Functions would treat as successful task completions.

P28 R14.1: a container body that fails JSON parsing raises StepFailure —
the old ``{"statusCode": 200, "body": <raw>}`` swallow is gone.
P28 R14.2: the data-plane client always gets explicit connect/read timeouts
(connect 10s, read 900s default; explicit caller read_timeout overrides).

Validates: P28 Requirements 1.2, 14.1, 14.2, 14.4
"""

import json
import os
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

# Add shared to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from agentcore_proxy import (
    DEFAULT_STEP_NAME,
    _extract_runtime_id,
    invoke_agentcore_runtime,
)
from step_errors import StepFailure, TransientStepFailure

# ── Fixtures ────────────────────────────────────────────────────────

ENDPOINT = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/abc123/invocations?qualifier=DEFAULT"
PAYLOAD = {"run_id": "run-1", "pipeline_id": "pipe-1"}


def _mock_client(response=None, side_effect=None):
    client = MagicMock()
    if side_effect is not None:
        client.invoke_agent_runtime.side_effect = side_effect
    else:
        client.invoke_agent_runtime.return_value = response
    return client


# ── _extract_runtime_id ────────────────────────────────────────────


def test_extract_runtime_id_from_valid_endpoint():
    assert _extract_runtime_id(ENDPOINT) == "abc123"


def test_extract_runtime_id_returns_none_for_invalid():
    assert _extract_runtime_id("https://example.com/foo") is None


# ── Success Path ────────────────────────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_success_returns_parsed_json(mock_client_fn):
    expected = {"statusCode": 200, "run_id": "run-1", "code_s3_uri": "s3://bucket/code.py"}
    mock_client_fn.return_value = _mock_client({"response": BytesIO(json.dumps(expected).encode()), "statusCode": 200})

    result = invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    assert result == expected
    mock_client_fn.return_value.invoke_agent_runtime.assert_called_once()


# ── Configuration errors raise permanent StepFailure ───────────────


def test_invoke_empty_endpoint_raises_step_failure():
    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint="", payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.step_name == DEFAULT_STEP_NAME
    assert exc.error_type == "permanent"
    assert "endpoint is empty" in exc.message
    assert exc.details["error_code"] == "ConfigurationError"


def test_invoke_invalid_endpoint_raises_step_failure():
    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint="https://example.com/foo", payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "permanent"
    assert "Could not extract runtime ID" in exc.message
    assert exc.details["error_code"] == "ConfigurationError"
    assert exc.details["endpoint"] == "https://example.com/foo"


# ── step_name pass-through ──────────────────────────────────────────


def test_invoke_carries_caller_step_name_on_failure():
    """Callers identify the proxied step; raises carry that step name."""
    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint="", payload=PAYLOAD, region="us-west-2", step_name="create_code")

    assert exc_info.value.step_name == "create_code"


# ── ClientError classification ──────────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_client_error_raises_permanent_step_failure(mock_client_fn):
    from botocore.exceptions import ClientError

    original = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "Invalid payload"}},
        "InvokeAgentRuntime",
    )
    mock_client_fn.return_value = _mock_client(side_effect=original)

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.error_type == "permanent"
    assert "ValidationException" in exc.message
    assert "Invalid payload" in exc.message
    assert exc.details["error_code"] == "ValidationException"
    assert exc.details["runtime_id"] == "abc123"
    assert isinstance(exc.__cause__, ClientError)


@patch("agentcore_proxy.boto3.client")
def test_invoke_throttling_raises_transient_step_failure(mock_client_fn):
    from botocore.exceptions import ClientError

    mock_client_fn.return_value = _mock_client(
        side_effect=ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeAgentRuntime",
        )
    )

    with pytest.raises(TransientStepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "transient"
    assert "Rate exceeded" in exc.message
    assert exc.details["error_code"] == "ThrottlingException"


# ── Runtime HTTP status classification ─────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_http_500_raises_transient_with_truncated_response(mock_client_fn):
    raw_error = "container blew up: " + "x" * 2000
    mock_client_fn.return_value = _mock_client(
        {
            "response": BytesIO(raw_error.encode()),
            "statusCode": 500,
            "runtimeSessionId": "sess-42",
        }
    )

    with pytest.raises(TransientStepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "transient"
    assert "HTTP 500" in exc.message
    assert "container blew up" in exc.message
    assert exc.details["status_code"] == 500
    assert exc.details["runtime_id"] == "abc123"
    assert exc.details["session_id"] == "sess-42"
    # Raw response preserved but truncated (bounded SFN Cause size)
    assert exc.details["response"] == raw_error[:1000]


@patch("agentcore_proxy.boto3.client")
def test_invoke_http_429_raises_transient(mock_client_fn):
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b"throttled"), "statusCode": 429})

    with pytest.raises(TransientStepFailure):
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")


@patch("agentcore_proxy.boto3.client")
def test_invoke_http_400_raises_permanent(mock_client_fn):
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b"bad request"), "statusCode": 400})

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.error_type == "permanent"
    assert "HTTP 400" in exc.message
    assert "bad request" in exc.details["response"]


# ── Garbled (non-JSON) response body — P28 R14.1 ────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_non_json_body_raises_step_failure(mock_client_fn):
    """HTTP 200 with a non-JSON body raises — never a {"statusCode": 200} return."""
    raw = "<!DOCTYPE html><html>bad gateway page</html>" + "x" * 2000
    mock_client_fn.return_value = _mock_client({"response": BytesIO(raw.encode()), "statusCode": 200})

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2", step_name="create_code")

    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.error_type == "permanent"
    # Carries the caller-provided step name, not the module default
    assert exc.step_name == "create_code"
    assert "non-JSON" in exc.message
    assert exc.details["error_code"] == "NonJSONResponse"
    assert exc.details["runtime_id"] == "abc123"
    # Raw body preserved but truncated (bounded SFN Cause size)
    assert exc.details["response"] == raw[:1000]


@patch("agentcore_proxy.boto3.client")
def test_invoke_non_utf8_body_raises_step_failure(mock_client_fn):
    """Undecodable bytes are the same failure class: container returned non-JSON."""
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b"\xff\xfe garbled \x80"), "statusCode": 200})

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "permanent"
    assert "non-JSON" in exc.message
    assert exc.details["error_code"] == "NonJSONResponse"


# ── Boto client timeouts — P28 R14.2 ────────────────────────────────


def _bedrock_client_config(mock_client_fn):
    """Extract the Config passed to the bedrock-agentcore client call."""
    calls = [c for c in mock_client_fn.call_args_list if c[0][0] == "bedrock-agentcore"]
    assert calls, "bedrock-agentcore client was never constructed"
    return calls[-1][1]["config"]


@patch("agentcore_proxy.boto3.client")
def test_invoke_default_timeouts_applied(mock_client_fn):
    """Without an override, the client gets connect_timeout=10 / read_timeout=900."""
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b'{"statusCode": 200}'), "statusCode": 200})

    invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    config = _bedrock_client_config(mock_client_fn)
    assert config.connect_timeout == 10
    assert config.read_timeout == 900


@patch("agentcore_proxy.boto3.client")
def test_invoke_explicit_read_timeout_overrides_default(mock_client_fn):
    """An explicit caller read_timeout overrides the 900s default; connect stays 10s."""
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b'{"statusCode": 200}'), "statusCode": 200})

    invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2", read_timeout=870)

    config = _bedrock_client_config(mock_client_fn)
    assert config.connect_timeout == 10
    assert config.read_timeout == 870


# ── Connection / BotoCore errors ─────────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_connection_error_raises_transient(mock_client_fn):
    from botocore.exceptions import EndpointConnectionError

    original = EndpointConnectionError(endpoint_url="https://bedrock-agentcore.us-west-2.amazonaws.com")
    mock_client_fn.return_value = _mock_client(side_effect=original)

    with pytest.raises(TransientStepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "transient"
    assert exc.details["error_code"] == "EndpointConnectionError"
    assert isinstance(exc.__cause__, EndpointConnectionError)


@patch("agentcore_proxy.boto3.client")
def test_invoke_botocore_error_raises_permanent(mock_client_fn):
    from botocore.exceptions import ParamValidationError

    mock_client_fn.return_value = _mock_client(side_effect=ParamValidationError(report="bad param"))

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert not isinstance(exc, TransientStepFailure)
    assert exc.error_type == "permanent"
    assert exc.details["error_code"] == "ParamValidationError"


# ── Generic exception handling ──────────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_unexpected_error_raises_permanent_step_failure(mock_client_fn):
    original = RuntimeError("Connection refused")
    mock_client_fn.return_value = _mock_client(side_effect=original)

    with pytest.raises(StepFailure) as exc_info:
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")

    exc = exc_info.value
    assert exc.error_type == "permanent"
    assert "Connection refused" in exc.message
    assert exc.details["error_code"] == "RuntimeError"
    assert exc.__cause__ is original


# ── No error payloads ever returned ─────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_never_returns_error_payload(mock_client_fn):
    """R1.2: every failure raises — no {"statusCode": 5xx, "error": ...} dicts."""
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b"boom"), "statusCode": 502})

    with pytest.raises(StepFailure):
        invoke_agentcore_runtime(endpoint=ENDPOINT, payload=PAYLOAD, region="us-west-2")


# ── Qualifier extraction ────────────────────────────────────────────


@patch("agentcore_proxy.boto3.client")
def test_invoke_extracts_qualifier_from_endpoint(mock_client_fn):
    mock_client_fn.return_value = _mock_client({"response": BytesIO(b'{"statusCode": 200}'), "statusCode": 200})

    endpoint_with_qualifier = (
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/rt123/invocations?qualifier=PROD"
    )
    invoke_agentcore_runtime(endpoint=endpoint_with_qualifier, payload=PAYLOAD, region="us-west-2")

    call_kwargs = mock_client_fn.return_value.invoke_agent_runtime.call_args[1]
    assert call_kwargs["qualifier"] == "PROD"
