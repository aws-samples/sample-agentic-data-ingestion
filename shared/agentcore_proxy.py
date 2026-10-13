# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore Runtime proxy — forwards Lambda payloads to AgentCore Runtime
containers via boto3 invoke_agent_runtime.

P28 R1.2: container/proxy errors RAISE ``StepFailure`` (or
``TransientStepFailure`` for genuinely retryable conditions such as throttles
and data-plane connection failures) instead of returning error payloads that
Step Functions would treat as successful task completions.

Task-token flow (P28 R19.11) — recorded decision:
    ``invoke_agent_runtime`` is synchronous, so this proxy is deliberately
    TOKEN-TRANSPARENT rather than growing an async dispatch variant. The
    container-backed steps' task states pass ``$$.Task.Token`` as
    ``task_token`` in the Lambda payload; this proxy forwards the payload
    verbatim (token included). What makes the dispatch async is the
    CONTAINER side: the entrypoint pops the token, backgrounds the work
    (``shared/sfn_task_token.dispatch_background_step`` — the runtime's
    ``add_async_task`` keeps the microVM scheduled via HealthyBusy /ping),
    and responds with an immediate dispatch ack — so this very same
    synchronous call returns within seconds and the proxy Lambda stops
    billing. The container closes the loop with SendTaskSuccess/Failure.

    The synchronous request/response contract below is UNCHANGED and load-
    bearing for tokenless callers — self_healing_dispatch invokes this
    synchronously with ``read_timeout=870`` and consumes the parsed result,
    and the version-skew relay in ``sfn_task_token.step_task_token`` relies
    on an old container's synchronous final result parsing exactly as before.
    The default 900s read timeout stays: it covers both the fast-ack path
    (seconds) and a legacy container running synchronously during a deploy's
    version-skew window.

Usage:
    from agentcore_proxy import invoke_agentcore_runtime

    result = invoke_agentcore_runtime(
        endpoint=os.environ['AGENTCORE_CREATE_CODE_ENDPOINT'],
        payload=event,
        step_name="create_code",  # proxied step; defaults to "agentcore_proxy"
    )
"""

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from botocore.exceptions import ConnectionError as BotocoreConnectionError

from step_errors import TRANSIENT_CLIENT_ERROR_CODES, StepFailure, TransientStepFailure

logger = logging.getLogger(__name__)

#: step_name used on raised StepFailures when the caller does not identify
#: the proxied step.
DEFAULT_STEP_NAME = "agentcore_proxy"

#: Default data-plane timeouts (P28 R14.2). Every call gets an explicit
#: connect/read timeout — boto3's 60s default read timeout cannot survive
#: long-running agent workloads. The read timeout matches the container
#: workload ceiling (900s); callers with tighter budgets override it.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_READ_TIMEOUT_SECONDS = 900

# ClientError codes from the AgentCore data plane that are genuinely
# retryable (throttling, transient service-side failures, timeouts).
# P28 R17.3: single-sourced from step_errors so the proxy's classification
# and the read-path helpers' classification cannot drift.
_TRANSIENT_CLIENT_ERROR_CODES = TRANSIENT_CLIENT_ERROR_CODES


def invoke_agentcore_runtime(
    endpoint: str,
    payload: dict,
    region: str | None = None,
    step_name: str = DEFAULT_STEP_NAME,
    read_timeout: int | None = None,
) -> dict:
    """
    Forward a Lambda event payload to an AgentCore Runtime container.

    Args:
        endpoint: Full AgentCore Runtime invocation URL (contains runtime ID)
        payload: The Lambda event dict to forward
        region: AWS region (defaults to AWS_REGION env var)
        step_name: Canonical name of the proxied step, carried on raised
            StepFailures (defaults to "agentcore_proxy")
        read_timeout: Optional data-plane read timeout in seconds for
            long-running agent workloads (P28 R9.3 — boto3's default 60s
            cannot survive the self-healing agent's ~10-minute run). When
            None, ``DEFAULT_READ_TIMEOUT_SECONDS`` (900s, the container
            workload ceiling) applies (P28 R14.2).

    Returns:
        The parsed JSON response from the AgentCore Runtime container.

    Raises:
        StepFailure: On configuration errors, container HTTP errors, and
            non-retryable AWS errors (P28 R1.2 — never returns an error
            payload).
        TransientStepFailure: On retryable conditions (throttles, data-plane
            connection failures) so SFN Retry policies fire.
    """
    region = region or os.environ.get("AWS_REGION", "us-west-2")
    account_id = os.environ.get("AWS_ACCOUNT_ID", "")

    if not endpoint:
        raise StepFailure(
            step_name,
            "AGENTCORE endpoint is empty",
            details={"error_code": "ConfigurationError"},
        )

    # Extract runtime ID from endpoint URL
    # Format: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{runtimeId}/invocations?qualifier=DEFAULT
    runtime_id = _extract_runtime_id(endpoint)
    if not runtime_id:
        raise StepFailure(
            step_name,
            f"Could not extract runtime ID from endpoint: {endpoint}",
            details={"error_code": "ConfigurationError", "endpoint": endpoint},
        )

    # Extract qualifier
    qualifier = "DEFAULT"
    if "qualifier=" in endpoint:
        qualifier = endpoint.split("qualifier=")[-1].split("&")[0]

    # Build the runtime ARN
    runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"

    # If we don't have account_id, try to get it
    if not account_id:
        try:
            sts = boto3.client("sts", region_name=region)
            account_id = sts.get_caller_identity()["Account"]
            runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_id}"
        except Exception:
            # Fall back to using just the runtime ID as the ARN
            runtime_arn = runtime_id

    body = json.dumps(payload, default=str).encode("utf-8")

    # P28 R14.2: every call gets explicit connect/read timeouts. The default
    # read timeout matches the container workload ceiling (900s); an explicit
    # caller ``read_timeout`` (e.g. the self-healing dispatcher's 870s, R9.3)
    # overrides just the read timeout.
    client_config = BotoConfig(
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout=read_timeout if read_timeout is not None else DEFAULT_READ_TIMEOUT_SECONDS,
    )

    try:
        client = boto3.client("bedrock-agentcore", region_name=region, config=client_config)

        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            contentType="application/json",
            accept="application/json",
            payload=body,
        )

        # Read the streaming response
        result_bytes = b""
        event_stream = response.get("response", response.get("body", response.get("payload")))
        if event_stream:
            if hasattr(event_stream, "read"):
                result_bytes = event_stream.read()
            else:
                # EventStream — iterate events
                for event in event_stream:
                    if "chunk" in event:
                        result_bytes += event["chunk"].get("bytes", b"")
                    elif "bytes" in event:
                        result_bytes += event["bytes"]

        # Check HTTP status from the response
        status_code = response.get("statusCode", 200)
        if status_code and status_code >= 400:
            error_msg = result_bytes.decode("utf-8") if result_bytes else f"Runtime returned HTTP {status_code}"
            logger.error("AgentCore Runtime returned status %d: %s", status_code, error_msg[:500])
            details: dict[str, Any] = {
                "status_code": status_code,
                "runtime_id": runtime_id,
                "response": error_msg[:1000],
            }
            session_id = response.get("runtimeSessionId")
            if session_id:
                details["session_id"] = session_id
            message = f"AgentCore Runtime returned HTTP {status_code}: {error_msg[:500]}"
            if status_code == 429 or status_code >= 500:
                # Throttles and container/server-side errors may succeed on retry
                raise TransientStepFailure(step_name, message, details=details)
            raise StepFailure(step_name, message, details=details)

        if result_bytes:
            # P28 R14.1: a body that fails JSON parsing raises — the old
            # ``{"statusCode": 200, "body": <raw>}`` swallow let garbled
            # container output continue downstream as a successful step.
            try:
                parsed: dict = json.loads(result_bytes.decode("utf-8"))
                return parsed
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raw = result_bytes.decode("utf-8", errors="replace")
                logger.error("AgentCore Runtime returned non-JSON body: %s", raw[:500])
                raise StepFailure(
                    step_name,
                    "container returned non-JSON",
                    details={
                        "error_code": "NonJSONResponse",
                        "runtime_id": runtime_id,
                        "response": raw[:1000],
                    },
                ) from exc

        return {"statusCode": 200, "body": ""}

    except StepFailure:
        # Raised above (HTTP-status branch) — propagate without re-wrapping.
        raise

    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        error_msg = exc.response.get("Error", {}).get("Message", str(exc))
        logger.error("AgentCore Runtime ClientError: %s - %s", error_code, error_msg)
        client_details: dict[str, Any] = {"error_code": error_code, "runtime_id": runtime_id}
        if error_code in _TRANSIENT_CLIENT_ERROR_CODES:
            raise TransientStepFailure(step_name, f"{error_code}: {error_msg}", details=client_details) from exc
        raise StepFailure(step_name, f"{error_code}: {error_msg}", details=client_details) from exc

    except BotocoreConnectionError as exc:
        # Connection-level failures (endpoint unreachable, connect/read
        # timeouts) are genuinely retryable.
        logger.error("AgentCore Runtime connection error: %s", exc)
        raise TransientStepFailure(
            step_name,
            f"AgentCore Runtime connection error: {exc}",
            details={"error_code": type(exc).__name__, "runtime_id": runtime_id},
        ) from exc

    except BotoCoreError as exc:
        logger.error("AgentCore Runtime BotoCoreError: %s", exc)
        raise StepFailure(
            step_name,
            f"BotoCoreError: {exc}",
            details={"error_code": type(exc).__name__, "runtime_id": runtime_id},
        ) from exc

    except Exception as exc:
        logger.error("Unexpected error invoking AgentCore Runtime: %s", exc)
        raise StepFailure(
            step_name,
            f"Unexpected error invoking AgentCore Runtime: {exc}",
            details={"error_code": type(exc).__name__, "runtime_id": runtime_id},
        ) from exc


def _extract_runtime_id(endpoint: str) -> str | None:
    """Extract runtime ID from AgentCore endpoint URL."""
    # Pattern: /runtimes/{runtimeId}/invocations
    match = re.search(r"/runtimes/([^/]+)/invocations", endpoint)
    return match.group(1) if match else None
