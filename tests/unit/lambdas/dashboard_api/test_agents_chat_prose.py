# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: ``POST /api/chat`` returns prose or nothing, never a dict (p47 R3.4).

The response-shape contract for this endpoint lives in
``tests/contracts/api_responses/test_chat_response_contract.py``, pinned against
the frontend ``ChatResponse`` interface parsed at test time (``LGR-28b4d75-5``).
That file is the oracle for what the endpoint must carry. It cannot exercise the
``response`` branch, though: the ``nl_pipeline_builder`` agent emits no prose on
either of its two paths (``main.py:694``, ``:812``), so the contract test only
gets to assert that ``response`` is *absent or not a repr*.

This file covers the branch itself — the one piece of logic task B2 added:

    prose = result.get("response")
    if isinstance(prose, str) and prose.strip():
        chat_response["response"] = prose

Three properties follow from R3.4, and each is a way the deleted
``result.get("body", result.get("response", str(result)))`` fall-through used to
fail:

* prose the agent DOES supply is forwarded unchanged, not re-wrapped;
* a non-string ``response`` is dropped rather than stringified — ``str()`` of a
  dict is what R3.4 forbids outright, and the old code produced exactly that;
* ``body`` is no longer consulted at all. The AgentCore proxy only ever returns a
  ``body`` key on its empty-response path (``agentcore_proxy.py``, final
  ``return``), so treating it as prose was never right.

An omitted ``response`` is the correct outcome, not a degradation:
``useChatAgent`` reads ``response.reasoning_steps?.join('\\n') || response.response``,
so the steps are the assistant's answer when there is no prose.

``invoke_agentcore_runtime`` is patched on ``agentcore_proxy`` rather than on the
handler module, because the handler imports the name inside the function body and
resolves it out of the proxy module at call time. Patching keeps the synchronous
request/response shape ``/chat`` must keep (invariant ``LGR-b46a5bb-1``) — no
thread, no dispatch Lambda.

Validates: Requirements R3.4
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
MESSAGE = "Load customer data from s3://my-bucket/customers/ into an Iceberg table"


@pytest.fixture
def agent_configured(mock_lambda_env, monkeypatch):
    """A configured endpoint, so the handler reaches the agent call.

    ``mock_lambda_env`` supplies the shared Lambda env baseline; the one var this
    handler branches on is not in it, so it is set here.
    """
    monkeypatch.setenv(ENDPOINT_ENV, ENDPOINT)


def _chat(agent_result: dict) -> dict:
    """POST /api/chat with the agent returning ``agent_result``; parsed body."""
    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=agent_result):
        response = agents.handle_chat_message(json.dumps({"message": MESSAGE}))
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def test_prose_from_the_agent_is_forwarded_unchanged(agent_configured):
    """A string ``response`` is the assistant's answer; it travels verbatim."""
    prose = "I found 4 CSV files under s3://my-bucket/customers/. Shall I propose a pipeline?"

    body = _chat({"status": "proposal", "reasoning_steps": ["Scanning..."], "response": prose})

    assert body["response"] == prose


def test_a_non_string_response_is_dropped_rather_than_stringified(agent_configured):
    """R3.4: a dict under ``response`` is not prose, so it must not be rendered.

    ``ChatPage`` writes this value straight into the assistant bubble with
    ``white-space: pre-wrap``, so a ``str()`` of a dict reaches the user as
    Python syntax. Dropping the key lets ``useChatAgent`` fall back to
    ``reasoning_steps``, which is a real answer.
    """
    steps = ["Scanning s3://my-bucket/customers/...", "Found 4 files, inferred 3 fields"]

    body = _chat({"status": "proposal", "reasoning_steps": steps, "response": {"text": "nested"}})

    assert "response" not in body, f"a non-string response was forwarded as {body.get('response')!r}"
    assert body["reasoning_steps"] == steps, "the steps are the fallback answer and must survive"


def test_an_empty_prose_response_is_omitted(agent_configured):
    """Whitespace is not an answer, and an empty string is not either.

    ``useChatAgent`` prefers ``reasoning_steps`` over ``response`` and both are
    optional, so omitting the key is what renders the steps. Forwarding ``"  "``
    would render a blank bubble on a path that has something to say.
    """
    body = _chat({"status": "proposal", "reasoning_steps": ["Scanning..."], "response": "   "})

    assert "response" not in body


def test_a_body_key_on_the_agent_result_is_not_treated_as_prose(agent_configured):
    """The deleted fall-through, pinned so it cannot come back.

    ``result.get("body", ...)`` was first in the old chain. The proxy returns a
    ``body`` key only on its empty-response path, never as an answer, so
    consulting it was wrong even before it fell through to ``str(result)``.
    """
    body = _chat({"status": "proposal", "reasoning_steps": ["Scanning..."], "body": "{'not': 'prose'}"})

    assert "response" not in body
    assert "body" not in body, "the agent's transport-level `body` key must not leak into the response"
