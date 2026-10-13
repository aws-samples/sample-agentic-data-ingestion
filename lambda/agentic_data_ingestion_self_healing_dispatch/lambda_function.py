# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Self-healing dispatch Lambda (P28 R9.3).

job_completion fire-and-forgets the failure payload here via an async Lambda
invoke (``InvocationType="Event"``) and returns immediately. This function
then performs the long SYNCHRONOUS AgentCore Runtime call inside its own
15-minute timeout.

This replaces the old daemon-thread-inside-job_completion pattern, which was
broken twice over: the thread was frozen/killed the moment the handler
returned (Lambda freezes the execution environment), and boto3's default 60s
read timeout could never survive the agent's ~10-minute workload.

The self-healing agent has no Lambda surface of its own — it exists only as
an AgentCore Runtime container (agentcore-stack.ts). This thin dispatcher IS
the ``SELF_HEALING_FN`` the design invokes asynchronously.

Failure semantics: healing is best-effort for the ORIGINAL run
(job_completion already sent its Step Functions callback), but a dispatch
that reaches this function should not vanish silently — errors raise, so
Lambda's built-in async retry (2 attempts) covers transient AgentCore
hiccups and terminal failures land in Lambda's async failure metrics.
"""

import os
import sys
from typing import Any

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

from agentcore_proxy import invoke_agentcore_runtime

logger = Logger(service="agentic_data_ingestion")

# The agent classifies the error, applies a remediation, and re-triggers the
# pipeline — ~10 minutes worst case. The read timeout must outlive that
# workload while staying inside this function's 900s Lambda timeout
# (compute-stack.ts).
AGENT_READ_TIMEOUT_SECONDS = 870


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Relay a job-failure payload to the self-healing AgentCore Runtime.

    Args:
        event: The self-healing payload assembled by job_completion —
            pipeline_id, run_id, job_name, job_run_id, error_message, state.
        context: Lambda context (unused).

    Returns:
        dict: 200 status with the run_id once the agent call completes.

    Raises:
        RuntimeError: When SELF_HEALING_AGENT_ENDPOINT is not configured.
        StepFailure: Propagated from the AgentCore proxy on invocation
            failure (drives Lambda async retry).
    """
    endpoint = os.environ.get("SELF_HEALING_AGENT_ENDPOINT", "")
    if not endpoint:
        # Configuration problem: raise so the failed dispatch is visible in
        # async-invoke error metrics rather than silently dropped.
        raise RuntimeError("SELF_HEALING_AGENT_ENDPOINT is not configured")

    run_id = event.get("run_id", "unknown")
    logger.append_keys(run_id=run_id, pipeline_id=event.get("pipeline_id", ""))
    logger.info(f"Dispatching self-healing agent for run {run_id}")

    result = invoke_agentcore_runtime(
        endpoint=endpoint,
        payload=event,
        step_name="self_healing_dispatch",
        read_timeout=AGENT_READ_TIMEOUT_SECONDS,
    )

    logger.info(f"Self-healing agent completed: {str(result)[:200]}")
    return {"statusCode": 200, "run_id": run_id}
