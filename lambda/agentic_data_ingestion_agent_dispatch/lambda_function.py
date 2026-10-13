# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generic agent dispatch Lambda (p30a R1.2, R2.2).

The one thing this function does is the long SYNCHRONOUS
``bedrock-agentcore:InvokeAgentRuntime`` call, inside its own 15-minute
Lambda timeout. Callers fire-and-forget it with
``InvocationType="Event"`` (dashboard API handlers) or target it directly
from an EventBridge rule with a static input.

It exists because of two constraints that are not style preferences:

1. **AgentCore Runtime is not an EventBridge target.** An EventBridge Rule
   can only reach a fixed target list, and API Destinations cannot sign
   SigV4, so a schedule cannot invoke an agent directly. The route has to
   be Rule -> Lambda -> AgentCore.
2. **Lambda freezes the execution environment on return.** The pattern this
   replaces (``threading.Thread`` doing the agent call from inside a
   request handler, then returning 202) loses the work: the thread is
   frozen/killed the moment the handler returns. boto3's default 60s read
   timeout could not have survived a ~10-minute agent workload anyway.
   ``agentic_data_ingestion_self_healing_dispatch`` is the working
   reference implementation of this same shape; this is its generalisation,
   not a new idea. Do NOT reintroduce a thread here or in any caller.

Event shape::

    {
      "agent_endpoint_env": "COST_OPTIMIZATION_AGENT_ENDPOINT",  # or...
      "agent_endpoint": "https://bedrock-agentcore.../invocations?...",
      "payload": { ... forwarded to the agent unchanged ... },
      "step_name": "cost_optimization"                           # optional
    }

``agent_endpoint`` (a literal URL) wins over ``agent_endpoint_env`` (the
*name* of an environment variable holding one) when both are present.

Failure semantics, and the reason they differ by failure class:

* **Misconfiguration** (no endpoint identifier in the event, or the named
  env var is unset) returns an honest ``statusCode: 400`` body saying the
  agent is not configured, and dispatches nothing. It is never reported as
  a success. Nothing downstream can fix a missing endpoint by retrying, so
  raising here would only burn Lambda's two async retries and bury the
  cause in a stack trace.
* **Agent/transport failures** RAISE. Lambda's built-in async retry (2
  attempts) then covers transient AgentCore hiccups, and terminal failures
  land in the async failure metrics instead of vanishing.
"""

import os
import sys
from typing import Any

sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from aws_lambda_powertools import Logger

from agentcore_proxy import invoke_agentcore_runtime
from env_utils import require_env

logger = Logger(service="agentic_data_ingestion")

#: Data-plane read timeout for the agent call, in seconds.
#:
#: 870s is a CEILING, not an estimate of any agent's runtime. This function's
#: Lambda timeout is 900s (compute-stack.ts), which is Lambda's hard maximum,
#: and ``shared/agentcore_proxy.py`` already defaults to 900s. The reduction
#: to 870 buys ~30s in which boto3's read timeout can fire and produce the
#: handled failure this module is written to raise, instead of Lambda killing
#: the invocation mid-call. There is no larger number available here.
#:
#: MEASURED (task 2.3, design §5 O3): the cost-optimization fleet sweep took
#: 528ms internally for 2 in-scope Glue jobs on 2026-08-01 — three orders of
#: magnitude inside this budget. That sweep drives no model call, only boto3,
#: and scales at ~2 serial Glue ``get_job_runs`` per in-scope job, so 870s
#: covers roughly 1,700-4,300 jobs. Same order of magnitude as the Glue quota
#: of 2,000 jobs per account, so this value is adequate by construction rather
#: than proven: a fleet-scale sweep has NOT been measured.
#:
#: If a sweep ever needs more than this, the fix is removing that per-job
#: fan-out, NOT raising this constant — there is nowhere to raise it to.
AGENT_READ_TIMEOUT_SECONDS = 870

#: Default step name carried on raised StepFailures. Callers override it per
#: invocation so a failure is attributable to the agent that failed rather
#: than to the shared dispatcher.
DEFAULT_STEP_NAME = "agent_dispatch"


def _resolve_endpoint(event: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve the target AgentCore endpoint from the event.

    Args:
        event: The dispatch event.

    Returns:
        tuple: ``(endpoint, reference, problem)``. ``endpoint`` is the
        resolved URL, or ``""`` when it could not be resolved.
        ``reference`` names the source for logging (the env var name, or
        ``"agent_endpoint"`` for a literal). ``problem`` is a human-readable
        reason when ``endpoint`` is empty, otherwise ``""``.
    """
    literal = str(event.get("agent_endpoint") or "")
    if literal:
        return literal, "agent_endpoint", ""

    env_name = str(event.get("agent_endpoint_env") or "")
    if not env_name:
        return "", "", "event carried neither 'agent_endpoint' nor 'agent_endpoint_env'"

    try:
        # require_env is the project convention for reading required config,
        # but a missing endpoint is a handled response here rather than an
        # unhandled crash (R2.3), so its OSError is caught.
        return require_env(env_name), env_name, ""
    except OSError:
        return "", env_name, f"environment variable '{env_name}' is not set"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Relay a payload to an AgentCore Runtime endpoint.

    Args:
        event: Dispatch event carrying ``agent_endpoint_env`` or
            ``agent_endpoint``, an optional ``payload`` forwarded to the
            agent unchanged, and an optional ``step_name`` for failure
            attribution.
        context: Lambda context (unused).

    Returns:
        dict: ``statusCode 200`` with the agent's response once the call
        completes, or ``statusCode 400`` with a body stating the agent is
        not configured when the endpoint could not be resolved.

    Raises:
        StepFailure: Propagated from the AgentCore proxy on a terminal
            invocation failure.
        TransientStepFailure: Propagated on a retryable failure. Both drive
            Lambda's async retry rather than being swallowed.
    """
    step_name = str(event.get("step_name") or DEFAULT_STEP_NAME)
    payload = event.get("payload") or {}
    endpoint, reference, problem = _resolve_endpoint(event)

    logger.append_keys(dispatch_step=step_name, endpoint_ref=reference)

    if not endpoint:
        # Honest handled response (R2.3): a misconfigured dispatch reports
        # that the agent is not configured and dispatches nothing. It never
        # reports success, and it does not raise — no retry can supply a
        # missing endpoint.
        message = f"Agent is not configured: {problem}. Nothing was dispatched."
        logger.error(message)
        return {
            "statusCode": 400,
            "dispatched": False,
            "error": "agent_not_configured",
            "body": message,
        }

    logger.info(f"Dispatching {step_name} agent via {reference}")

    result = invoke_agentcore_runtime(
        endpoint=endpoint,
        payload=payload,
        step_name=step_name,
        read_timeout=AGENT_READ_TIMEOUT_SECONDS,
    )

    logger.info(f"Agent {step_name} completed: {str(result)[:200]}")
    return {
        "statusCode": 200,
        "dispatched": True,
        "step_name": step_name,
        "result": result,
    }
