#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handlers for P13 agentic capabilities API endpoints.

Provides endpoints for:
- Self-healing activity (GET)
- Data quality reports (GET/POST)
- Cost recommendations (GET/POST apply)
- Chat with NL pipeline builder (POST)

Agent invocation here is ALWAYS a fire-and-forget async Lambda invoke of
``AGENT_DISPATCH_FN`` (p30a R2.1/R2.2). Do NOT reintroduce a
``threading.Thread`` that does the AgentCore call from inside a request
handler: Lambda freezes the execution environment the moment the handler
returns, so the thread is killed mid-call and the work is silently lost.
That was the live failure mode these two endpoints shipped with — they
returned 202 "triggered" for work that never ran. ``job_completion`` ->
``SELF_HEALING_FN`` is the working reference shape this mirrors.
"""

import json
import os

import boto3

from cost_recommendation_ids import derive_recommendation_id

from .common import AWS_REGION, create_response, logger, state_table

#: Name of the environment variable holding the generic Agent_Dispatch_Lambda's
#: function name. Set by compute-stack.ts on the dashboard API Lambda; the
#: matching ``lambda:InvokeFunction`` grant is on infrastructureRole in
#: foundation-stack.ts. Without it there is nothing to dispatch to, which is a
#: misconfiguration and reported as one (R2.3) rather than as a success.
DISPATCH_FN_ENV = "AGENT_DISPATCH_FN"

#: Fields of the NL pipeline builder agent's result that ``POST /api/chat``
#: forwards verbatim (p47 R3.1-R3.3, R3.6). An allowlist rather than a blanket
#: copy of the agent dict, so the endpoint's response shape stays auditable, and
#: each key is copied ONLY when the agent actually supplied it: ``proposal`` is
#: absent on the ``created`` path and must stay absent, because ``ChatPage``
#: renders ``PipelineProposalCard`` on any truthy ``msg.proposal`` — defaulting
#: the key to ``{}`` would draw a second, empty proposal card underneath the
#: "pipeline created" message. Since T-05 the agent no longer emits the
#: ``created`` shape, so ``pipeline_id`` and ``pipeline_name`` are never present
#: today; they stay listed until the response contract (R3.6) is revisited.
AGENT_RESULT_PASSTHROUGH = ("status", "reasoning_steps", "proposal", "pipeline_id", "pipeline_name")


def _dispatch_agent(
    *,
    endpoint_env: str,
    step_name: str,
    payload: dict,
) -> tuple[bool, str]:
    """Fire-and-forget the Agent_Dispatch_Lambda for one agent invocation.

    The dispatch Lambda owns the long synchronous
    ``bedrock-agentcore:InvokeAgentRuntime`` call inside its own 15-minute
    timeout; this handler must return well inside the dashboard API's 25s
    timeout, so the invoke is ``InvocationType="Event"``.

    ``endpoint_env`` is passed as the NAME of an environment variable, not a
    URL: it is resolved inside the dispatch Lambda's own process, which
    carries the same endpoint values from the same CDK props. The pre-flight
    check below reads this Lambda's copy purely so a misconfigured
    environment fails honestly here instead of dispatching into a void.

    Args:
        endpoint_env: Name of the env var naming the target AgentCore
            endpoint (e.g. ``"DATA_QUALITY_AGENT_ENDPOINT"``).
        step_name: Step name for failure attribution in the dispatch Lambda.
        payload: Forwarded to the agent unchanged.

    Returns:
        tuple: ``(dispatched, problem)``. ``problem`` is a human-readable
        reason when ``dispatched`` is False, otherwise ``""``.
    """
    if not os.environ.get(endpoint_env, ""):
        return False, f"the {step_name} agent endpoint ({endpoint_env}) is not configured"

    dispatch_fn = os.environ.get(DISPATCH_FN_ENV, "")
    if not dispatch_fn:
        return False, f"the agent dispatch function ({DISPATCH_FN_ENV}) is not configured"

    lambda_client = boto3.client("lambda", region_name=AWS_REGION)
    lambda_client.invoke(
        FunctionName=dispatch_fn,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "agent_endpoint_env": endpoint_env,
                "step_name": step_name,
                "payload": payload,
            }
        ),
    )
    logger.info("Dispatched %s agent via %s", step_name, dispatch_fn)
    return True, ""


def get_self_healing_activity(pipeline_id: str, run_id: str) -> dict:
    """GET /api/pipelines/{id}/runs/{runId}/self-healing

    Returns self-healing activity for a specific pipeline run.
    """
    try:
        resp = state_table.get_item(Key={"run_id": run_id, "step_name": "self_healing"})
        item = resp.get("Item")
        if not item:
            return create_response(200, {"activity": None})
        return create_response(200, {"activity": item})
    except Exception as e:
        logger.error("Error fetching self-healing activity for %s/%s: %s", pipeline_id, run_id, e)
        return create_response(500, {"error": str(e)})


def get_quality_report(pipeline_id: str, run_id: str) -> dict:
    """GET /api/pipelines/{id}/runs/{runId}/quality-report

    Returns the data quality report for a specific pipeline run.
    """
    try:
        resp = state_table.get_item(Key={"run_id": run_id, "step_name": "data_quality"})
        item = resp.get("Item")
        if not item:
            return create_response(200, {"report": None})
        return create_response(200, {"report": item})
    except Exception as e:
        logger.error("Error fetching quality report for %s/%s: %s", pipeline_id, run_id, e)
        return create_response(500, {"error": str(e)})


# The on-demand data quality check (POST .../quality-report, formerly
# `trigger_quality_check`) was removed on 2026-10-11. It sent the agent only
# pipeline_id and run_id, while the agent needs the target database and table,
# and the agent's runtime role holds no Athena, catalog or target-read grant. So
# every check failed, and the agent's put_item on (run_id, "data_quality")
# replaced the inline Glue report, which shares that key. Finishing it needs
# target resolution, scoped IAM, a results location and a separate state-row key
# for the agent's report. The agent runtime and `_dispatch_agent` remain.


def get_cost_recommendations() -> dict:
    """GET /api/cost-recommendations

    Returns the latest cost optimization recommendations.
    """
    try:
        # cost_optimization rows are a tiny fraction of the agentic_data_ingestion_state
        # table, so a Limit-capped single scan almost never includes one:
        # DynamoDB's Limit caps items *evaluated*, not items *matched* by the
        # filter. Paginate through the filtered scan and keep the most recent
        # row by timestamp. (The state table is small; a bounded page guard
        # prevents any runaway.)
        items: list[dict] = []
        scan_kwargs: dict = {
            "FilterExpression": "step_name = :sn",
            "ExpressionAttributeValues": {":sn": "cost_optimization"},
        }
        for _ in range(100):
            resp = state_table.scan(**scan_kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
        # Sort by timestamp descending and take the latest
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        latest = items[0] if items else None
        if not latest:
            return create_response(
                200,
                {"generated_at": "", "recommendations": [], "total_projected_savings_monthly": 0},
            )
        # The cost_optimization state row nests the recommendations list and the
        # savings total. The dashboard (CostRecommendationsResponse) expects them
        # at the TOP LEVEL — returning the whole row under "recommendations" makes
        # the frontend see an object where it expects a list, so the section stays
        # hidden. Also synthesize a stable per-rec `id` (used as the React key and
        # by the Apply action); the agent does not emit one.
        #
        # The derivation lives in shared/ because the agent's apply branch has to
        # resolve the same id back to the same recommendation in the same row
        # (p30a R1.4). Duplicating it here would let the producer and consumer of
        # these ids drift apart silently.
        recs = latest.get("recommendations") or []
        for idx, rec in enumerate(recs):
            rec["id"] = derive_recommendation_id(rec, idx)
        return create_response(
            200,
            {
                "generated_at": latest.get("generated_at", latest.get("timestamp", "")),
                "recommendations": recs,
                "total_projected_savings_monthly": latest.get("total_projected_savings_monthly", 0),
            },
        )
    except Exception as e:
        logger.error("Error fetching cost recommendations: %s", e)
        return create_response(500, {"error": str(e)})


def apply_cost_recommendation(recommendation_id: str) -> dict:
    """POST /api/cost-recommendations/{id}/apply

    Dispatches the cost optimization agent to apply the ONE recommendation
    named by ``{id}`` (p30a R1.4). ``recommendation_id`` travels in the
    dispatch payload; the agent-side branch that acts on a single
    recommendation instead of rescanning the fleet is task 1.6.

    ``AUTO_APPLY_ENABLED`` is checked in BOTH places on purpose:

    * HERE, because the dispatch is fire-and-forget — the agent's own answer
      never reaches this caller, so the handler is the only thing that can
      make the HTTP response honest (R1.4). It also means a run that could
      not change anything is not started at all.
    * In the AGENT (task 1.6), because that is the process holding the
      permissions to mutate a Glue job. A guard that only lives in the caller
      is a suggestion, not a control.

    The flag is read from THIS Lambda's environment. Enabling auto-apply
    therefore means setting it on the cost-optimization AgentCore runtime
    (enforcement) AND on the dashboard API Lambda (response honesty). It is
    set on neither today, so this endpoint correctly reports recommend-only.
    """
    try:
        if os.environ.get("AUTO_APPLY_ENABLED", "").lower() != "true":
            # R1.4: say so rather than reporting success. Not an error status —
            # recommend-only is a valid, deliberate posture, so this is a 200
            # carrying an explicit applied=false rather than a 202 implying
            # work is in flight.
            logger.info(
                "AUTO_APPLY_ENABLED is not 'true' — not dispatching apply for recommendation %s",
                recommendation_id,
            )
            return create_response(
                200,
                {
                    "message": (
                        "Auto-apply is disabled (AUTO_APPLY_ENABLED is not 'true'), so this "
                        "recommendation was NOT applied and no agent run was started."
                    ),
                    "applied": False,
                    "dispatched": False,
                    "auto_apply_enabled": False,
                    "recommendation_id": recommendation_id,
                },
            )

        dispatched, problem = _dispatch_agent(
            endpoint_env="COST_OPTIMIZATION_AGENT_ENDPOINT",
            step_name="cost_optimization",
            payload={"recommendation_id": recommendation_id, "action": "apply_recommendation"},
        )
        if not dispatched:
            # R2.3: honest body, no dispatch, never a bare success message.
            logger.warning("Apply not dispatched for recommendation %s: %s", recommendation_id, problem)
            return create_response(
                503,
                {
                    "error": "agent_not_configured",
                    "message": f"Cost optimization agent is not configured: {problem}. Nothing was dispatched.",
                    "applied": False,
                    "dispatched": False,
                    "recommendation_id": recommendation_id,
                },
            )

        return create_response(
            202,
            {
                "message": (
                    "Apply request dispatched to the cost optimization agent for this "
                    "recommendation. It has not been applied yet."
                ),
                "applied": False,
                "dispatched": True,
                "auto_apply_enabled": True,
                "recommendation_id": recommendation_id,
            },
        )
    except Exception as e:
        logger.error("Error dispatching apply for cost recommendation %s: %s", recommendation_id, e)
        return create_response(500, {"error": str(e), "applied": False, "dispatched": False})


def handle_chat_message(body: str) -> dict:
    """POST /api/chat

    Sends a message to the NL pipeline builder agent via AgentCore Runtime and
    returns the agent's structured answer.

    This call is deliberately SYNCHRONOUS and stays so (invariant
    ``LGR-b46a5bb-1``): chat is request/response, so there is no post-response
    work to defer and nothing to route through the dispatch Lambda. The module
    docstring above explains why the two endpoints that DO defer work must not
    use a thread; nothing here waits on, defers, or threads agent work.

    The agent returns one shape (``nl_pipeline_builder/main.py``, the
    ``handler`` return), which carries neither ``body`` nor ``response``::

        {status: "proposal", reasoning_steps, proposal, conversation_id, total_duration_ms}

    Every field in ``AGENT_RESULT_PASSTHROUGH`` is forwarded verbatim, and only
    when the agent supplied it (p47 R3.1-R3.3, R3.6). ``useChatAgent`` reads
    ``reasoning_steps`` and ``proposal`` off the top level of the body, so the
    proposal is never re-shaped or pruned here, including the keys
    ``PipelineProposal`` does not declare.

    ``response`` stays PROSE ONLY (R3.4) and is included only when the agent
    supplies a non-empty string; the client already falls back to
    ``reasoning_steps`` when it is absent. It is never a ``str()`` of a dict.
    The previous ``result.get("body", result.get("response", str(result)))``
    fell all the way through to that repr, whose single quotes then failed the
    ``json.loads`` retry, so the assistant bubble rendered Python syntax.

    Upstream, only ``message`` and ``conversation_id`` are sent. Chat proposes;
    it never creates. The dashboard creates a pipeline from a proposal through
    ``POST /pipelines``, which owns input validation, so a ``confirm`` or
    ``proposal`` in the body is dropped here rather than forwarded (threat
    model T-05, which reverses p47 R3.5's forwarding).
    """
    try:
        payload = json.loads(body) if isinstance(body, str) else body
        message = payload.get("message", "")
        if not message:
            return create_response(400, {"error": "message is required"})

        endpoint = os.environ.get("NL_PIPELINE_BUILDER_AGENT_ENDPOINT", "")
        if not endpoint:
            return create_response(
                200,
                {
                    "response": "NL pipeline builder agent endpoint not configured.",
                    "message": message,
                },
            )

        from agentcore_proxy import invoke_agentcore_runtime

        # The agent gets exactly these two keys. ``confirm`` and ``proposal``
        # from the body are deliberately NOT forwarded (threat model T-05):
        # pipelines are created only through ``POST /pipelines``, which owns
        # validation, and the agent no longer has a write path for them to reach.
        agent_payload: dict = {
            "message": message,
            "conversation_id": payload.get("conversation_id", ""),
        }

        result = invoke_agentcore_runtime(endpoint=endpoint, payload=agent_payload)

        # Pass the agent's structured result through unmodified (R3.1-R3.3,
        # R3.6). Presence is tested per key rather than defaulted, so a field
        # the agent did not send does not appear in the body at all.
        chat_response: dict = {"message": message}
        for field in AGENT_RESULT_PASSTHROUGH:
            if field in result:
                chat_response[field] = result[field]

        # R3.4: prose only. Today the agent emits no ``response`` on either
        # path, so the key is normally absent and the client renders
        # ``reasoning_steps`` instead. A non-string value is not prose, so it is
        # dropped rather than stringified — that stringification was the defect.
        prose = result.get("response")
        if isinstance(prose, str) and prose.strip():
            chat_response["response"] = prose

        return create_response(200, chat_response)
    except json.JSONDecodeError:
        return create_response(400, {"error": "Invalid JSON body"})
    except Exception as e:
        logger.error("Error handling chat message: %s", e)
        return create_response(500, {"error": str(e)})
