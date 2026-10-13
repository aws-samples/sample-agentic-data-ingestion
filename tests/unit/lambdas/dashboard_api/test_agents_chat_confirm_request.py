# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: ``POST /api/chat`` does NOT forward ``confirm`` or ``proposal`` (threat model T-05).

This is the REQUEST side of the chat contract. The response side is pinned by
``tests/contracts/api_responses/test_chat_response_contract.py`` and by
``test_agents_chat_prose.py``. Neither can see what the handler sends upstream:
both patch the agent and assert on what comes back. So the assertions here are on
the payload passed to ``invoke_agentcore_runtime``.

**History.** p47 R3.5 made this handler forward ``confirm`` and ``proposal`` so
the agent's ``if confirm and proposal_config`` gate could open and the agent could
write the pipeline itself; ``LGR-cd58706-2`` recorded how ``confirm`` was coerced
on the way. T-05 reverses that. The agent's write went straight to the pipelines
table and skipped every input check ``POST /pipelines`` applies, and it had no
client: the dashboard creates pipelines from a chat proposal by calling
``POST /pipelines`` (``ChatPage.handleCreatePipeline``) and ``useChatAgent`` posts
only ``{ message }``. The agent's write path is gone, and this handler no longer
forwards the two keys that reached it. This file used to assert the forwarding;
it now asserts its absence.

Expectations come from the T-05 fix, not from the handler's output: the agent
receives exactly ``message`` and ``conversation_id``, whatever else the body holds,
and a body that still carries ``confirm`` is answered as an ordinary message
rather than rejected.

``invoke_agentcore_runtime`` is patched on ``agentcore_proxy`` rather than on the
handler module, because the handler imports the name inside the function body and
resolves it out of the proxy module at call time. The call stays synchronous
request/response (invariant ``LGR-b46a5bb-1``) — no thread, no dispatch Lambda.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, LAMBDA_DIR)
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

from handlers import agents

ENDPOINT_ENV = "NL_PIPELINE_BUILDER_AGENT_ENDPOINT"
ENDPOINT = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/nlpb-abc123/invocations?qualifier=DEFAULT"
MESSAGE = "Load customer data from s3://my-bucket/customers/ into an Iceberg table partitioned by region"
CONVERSATION_ID = "conv-t05-request"

#: A proposal as a client would post it back: the shape ``build_pipeline_config``
#: returns. Its content does not matter; what matters is that it never arrives.
_PROPOSAL: dict = {
    "pipeline_name": "customers-iceberg",
    "source": {
        "type": "s3",
        "location": "s3://my-bucket/customers/",
        "format": "csv",
        "files_found": 4,
        "representative_file": "s3://my-bucket/customers/part-0000.csv",
    },
    "target": {"type": "s3_tables", "table_name": "customers", "partitioning": ["region"]},
    "schema": {"fields": [{"name": "customer_id", "type": "string"}, {"name": "region", "type": "string"}]},
    "mappings": [{"source": "customer_id", "target": "customer_id", "confidence": 1.0}],
    "custom_transforms": [],
    "description": "Iceberg load partitioned by region",
}

#: The only shape the agent returns since T-05.
_AGENT_PROPOSAL_RESULT: dict = {
    "status": "proposal",
    "reasoning_steps": ["Parsing intent...", "Scanning s3://my-bucket/customers/...", "Proposing pipeline"],
    "proposal": _PROPOSAL,
    "conversation_id": CONVERSATION_ID,
    "total_duration_ms": 4210,
}


@pytest.fixture
def agent_configured(mock_lambda_env, monkeypatch):
    """A configured endpoint, so the handler reaches the agent call.

    ``mock_lambda_env`` supplies the shared Lambda env baseline; the one var this
    handler branches on is not in it, so it is set here. Without it the handler
    takes the R3.8 degraded path and never builds a payload at all.
    """
    monkeypatch.setenv(ENDPOINT_ENV, ENDPOINT)


def _post(request_body: dict) -> tuple[dict, dict]:
    """POST ``request_body`` to /api/chat; return (payload the agent got, response)."""
    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=_AGENT_PROPOSAL_RESULT) as invoke:
        response = agents.handle_chat_message(json.dumps(request_body))

    assert response["statusCode"] == 200, f"handler returned {response['statusCode']}: {response['body']}"
    assert invoke.call_count == 1, "the handler must invoke the agent exactly once per message"
    return invoke.call_args.kwargs["payload"], response


# ---------------------------------------------------------------------------
# T-05 — the confirmation keys never reach the agent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"confirm": True, "proposal": _PROPOSAL}, id="confirm-true-with-proposal"),
        pytest.param({"confirm": True}, id="confirm-true-alone"),
        pytest.param({"proposal": _PROPOSAL}, id="proposal-alone"),
        pytest.param({"confirm": "true", "proposal": _PROPOSAL}, id="confirm-string-true"),
        pytest.param({"confirm": False, "proposal": _PROPOSAL}, id="confirm-false"),
        pytest.param({"confirm": True, "proposal": _PROPOSAL, "pipeline_id": "pipe-x"}, id="with-other-keys"),
    ],
)
def test_the_agent_receives_only_message_and_conversation_id(agent_configured, extra):
    """Whatever the body carries, the agent gets exactly two keys.

    ``confirm: true`` with a proposal is the case that used to open the agent's
    write gate, so it is the one that matters; the rest show the payload is built
    from an allowlist of two keys rather than by removing known-bad ones.
    """
    payload, _ = _post({"message": MESSAGE, "conversation_id": CONVERSATION_ID, **extra})

    assert payload == {"message": MESSAGE, "conversation_id": CONVERSATION_ID}, (
        f"the agent received {sorted(payload)}. Since T-05 only message and conversation_id are "
        f"forwarded; confirm and proposal must not reach the agent."
    )


def test_a_plain_message_sends_the_same_two_keys(agent_configured):
    """The ordinary path is unchanged: the same payload a confirm request now gets."""
    payload, _ = _post({"message": MESSAGE, "conversation_id": CONVERSATION_ID})

    assert payload == {"message": MESSAGE, "conversation_id": CONVERSATION_ID}


def test_a_missing_conversation_id_is_forwarded_as_empty(agent_configured):
    """``conversation_id`` defaults to ``""``; the dashboard never sends one today."""
    payload, _ = _post({"message": MESSAGE, "confirm": True, "proposal": _PROPOSAL})

    assert payload == {"message": MESSAGE, "conversation_id": ""}


def test_a_confirm_request_is_answered_as_an_ordinary_message(agent_configured):
    """A body that still carries ``confirm`` is not an error.

    The keys are dropped silently, and the caller gets the agent's proposal
    answer back, the same as for any message.
    """
    _, response = _post({"message": "Yes, create it", "confirm": True, "proposal": _PROPOSAL})

    body = json.loads(response["body"])
    assert body["status"] == "proposal"
    assert body["proposal"] == _PROPOSAL
    assert "pipeline_id" not in body, "nothing was created, so no pipeline id can be reported"


def test_the_endpoint_stays_synchronous(agent_configured):
    """Invariant ``LGR-b46a5bb-1``: no thread, no dispatch Lambda on this path.

    The observable property is that the agent call happens inside the handler,
    so its result is already in the response by the time the handler returns.
    """
    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=_AGENT_PROPOSAL_RESULT) as invoke:
        response = agents.handle_chat_message(json.dumps({"message": MESSAGE}))

    invoke.assert_called_once()
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["reasoning_steps"] == _AGENT_PROPOSAL_RESULT["reasoning_steps"], (
        "the agent's steps are in the response body, so the agent ran inline rather than being deferred"
    )
