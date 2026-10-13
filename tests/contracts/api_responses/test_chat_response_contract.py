# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: POST /api/chat response shape (p47 R3).

``handle_chat_message`` (``handlers/agents.py:314``) forwards a message to the
``nl_pipeline_builder`` AgentCore runtime and returns the agent's answer. The
agent returned one of two shapes and **neither carries ``body`` or ``response``**:

* ``{status: "proposal", reasoning_steps, proposal, conversation_id, total_duration_ms}``
  (``agentcore/nl_pipeline_builder/main.py``, the ``handler`` return)
* ``{status: "created", reasoning_steps, pipeline_id, pipeline_name, …}`` — **removed
  by threat model T-05**. The agent no longer creates pipelines, so it returns
  only the proposal shape; ``TestCreatedPathContract`` below says what is left of
  this one.

The handler does ``result.get("body", result.get("response", str(result)))``, so
it falls through to ``str(result)`` — a Python ``dict`` repr — then tries
``json.loads`` on it, which raises on the single quotes and is swallowed by
``pass``. The repr is returned as ``response``. The structured proposal is not
dropped, it is **stringified**, which is worse: it renders as a wall of Python
syntax inside the assistant bubble.

**The frontend is already correct** (design §2.9 — no frontend change is part of
R3). ``use-chat-agent.ts`` declares ``ChatResponse`` and reads all three of its
fields: ``response.reasoning_steps?.join('\\n') || response.response || ''`` and
``proposal: response.proposal``. ``ChatPage.tsx:82-90`` renders ``msg.proposal``
through ``PipelineProposalCard``.

**Pinned against the frontend interface, parsed at test time.** This follows the
ratified pattern in ``LGR-28b4d75-5``: *"Rejected transcribing the field list
into the test or into a JSON schema: a test written from the producer's output
cannot detect the producer drifting, because the output is its own oracle."* The
field names, their optionality and their declared types below come from
``read_interface`` reading ``use-chat-agent.ts``, never from a list written here.
The parser is the shared one in ``helpers.py`` (``LGR-99050e6-5``: extracted
rather than copied per endpoint, so the copies cannot diverge). It grew two
capabilities for this file, both widenings and neither a narrowing of the
frontend type:

* ``interface ChatResponse`` is **not** exported — only the hook in its own file
  instantiates ``apiClient.post<ChatResponse>`` with it — so ``export`` became
  optional in the reader.
* ``PipelineProposal.mappings`` is spelled ``Array<{…}>`` rather than ``{…}[]``.
  Same type, same JSON shape, one spelling the reader did not know.

Three fields this endpoint must carry are **not** in ``ChatResponse`` and so are
asserted from ``requirements.md`` instead, with the reason stated at each site:
``status`` (R3.3), ``pipeline_id`` and ``pipeline_name`` (R3.6). The interface is
the oracle for everything it declares; it cannot be the oracle for a field the
client does not read yet. R3.3 exists because the client needs to distinguish the
two agent paths, and the interface predates that requirement.

**Written before the fix, and now unmarked.** Every assertion below runs
unconditionally — no ``xfail`` markers remain in this file. They were here, and
that history is why the file reads as it does: each assertion describing the
target contract was written against the broken handler and carried
``xfail(strict=True)``, naming the task that would remove it (**B2** for the
response shape, **B3** for the ``created`` path), with the reason recording the
failure actually observed with the marker off rather than an assumed one.
``strict=True`` is what made writing them early safe — an XPASS fails the suite,
so a stale marker could not quietly hide a contract that had started working, and
removing each one was forced on the task that fixed the path it covered rather
than left to notice. **B2**, **B3** and **B4** removed all of them, and the two
helpers that built their reasons, as the handler caught up. What is left is an
ordinary contract test whose expectations happen to predate the implementation
they check.

Deliberately **not** covered here, to say where it lives rather than duplicate it:

* What the handler sends upstream is a *request* shape, not a response contract.
  Since T-05 it never forwards ``confirm`` / ``proposal`` (reversing R3.5); that
  is pinned in the unit layer by
  ``tests/unit/lambdas/dashboard_api/test_agents_chat_confirm_request.py``.
* That ``/chat`` stays a synchronous ``invoke_agentcore_runtime`` call rather than
  growing a thread or an async rework is invariant ``LGR-b46a5bb-1``, enforced by
  ``tests/unit/test_no_lambda_threads_prop.py`` (AST-based with no allowlist, per
  ``LGR-b46a5bb-4``) and by
  ``test_agents_async_dispatch.py::test_frozen_thread_antipattern_removed_from_source``,
  which scopes its AgentCore assertion to the other two endpoints precisely
  because this one is *supposed* to call inline. Nothing here asks the handler to
  wait on, defer, or thread agent work.

``handle_chat_message`` has **no covering tests** — codegraph flags it and the
disk check agrees: the only reference under ``tests/`` is a docstring mention in
``test_agents_async_dispatch.py:251``. So every expectation below is derived from
``requirements.md`` R3 and from the frontend interface, never from current
output, which is the defect.

Validates: Requirements R3.1, R3.2, R3.3, R3.4, R3.6, R3.7, R3.8
"""

from __future__ import annotations

import ast
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — handlers/common.py resolves its table names at import time
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parents[3]
LAMBDA_DIR = REPO_DIR / "lambda" / "agentic_data_ingestion_dashboard_api"
SHARED_DIR = REPO_DIR / "shared"
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
# handlers/common.py calls require_env for each table at import time, before any
# fixture can run — mirrors test_pipeline_costs_contract.py. Nothing here touches
# AWS: the only outbound call this handler makes is patched out below.
for _table_env, _table_name in (
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_table_env, _table_name)

sys.modules.pop("lambda_function", None)

from handlers import agents as _agents

from .helpers import load_interfaces, matches_json_type, required_fields

#: The consumer this contract is pinned against. ``load_interfaces`` asserts the
#: file exists and that each interface parsed non-empty, so a moved hook fails
#: loudly instead of quietly deriving an empty field set and passing.
FRONTEND_TYPE_FILE = REPO_DIR / "dashboard-react" / "src" / "hooks" / "use-chat-agent.ts"
RESPONSE_TYPE = "ChatResponse"
PROPOSAL_TYPE = "PipelineProposal"

#: Named interfaces in scope that serialize to a JSON object. The reader will not
#: guess this — an identifier it was not told about raises, so a renamed or
#: mistyped type cannot slip through as "some object".
OBJECT_TYPES = frozenset({PROPOSAL_TYPE})

#: The endpoint env var the handler branches on. A truthy value is what takes it
#: past the R3.8 degraded path and into the agent call.
ENDPOINT_ENV = "NL_PIPELINE_BUILDER_AGENT_ENDPOINT"
ENDPOINT = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/nlpb-abc123/invocations?qualifier=DEFAULT"

MESSAGE = "Load customer data from s3://my-bucket/customers/ into an Iceberg table partitioned by region"
CONVERSATION_ID = "conv-chat-contract"


# ---------------------------------------------------------------------------
# Fixture data — the two shapes the agent actually returns
# ---------------------------------------------------------------------------
# Faithful to build_pipeline_config (main.py:490-508): the proposal carries the
# five fields PipelineProposal declares PLUS four the interface does not
# (files_found, representative_file, custom_transforms, description). The extra
# keys are deliberate — TypeScript structural typing tolerates them, and R3.2
# says "structurally unmodified", so a handler that helpfully pruned the payload
# down to the declared fields would be violating the requirement.
_PROPOSAL: dict[str, Any] = {
    "pipeline_name": "customers-iceberg",
    "source": {
        "type": "s3",
        "location": "s3://my-bucket/customers/",
        "format": "csv",
        "files_found": 4,
        "representative_file": "s3://my-bucket/customers/part-0000.csv",
    },
    "target": {
        "type": "s3_tables",
        "table_name": "customers",
        "partitioning": ["region"],
    },
    "schema": {
        "fields": [
            {"name": "customer_id", "type": "string"},
            {"name": "region", "type": "string"},
            {"name": "signup_ts", "type": "timestamp"},
        ]
    },
    "mappings": [
        {"source": "customer_id", "target": "customer_id", "confidence": 1.0},
        {"source": "cust_region", "target": "region", "confidence": 0.82},
    ],
    "custom_transforms": [],
    "description": "Customer master load, partitioned by region",
}

_REASONING_STEPS: list[str] = [
    'Parsing intent from: "Load customer data from s3://my-bucket/customers/..."',
    "Extracted intent: source=s3://my-bucket/customers/, target=s3_tables, table=customers",
    "Scanning s3://my-bucket/customers/...",
    "Found 4 files, inferred 3 fields",
    "Generated 2 field mappings",
]

#: main.py ``handler`` return — the proposal path, the only one since T-05.
_AGENT_PROPOSAL_RESULT: dict[str, Any] = {
    "status": "proposal",
    "reasoning_steps": _REASONING_STEPS,
    "proposal": _PROPOSAL,
    "conversation_id": CONVERSATION_ID,
    "total_duration_ms": 8421,
}

#: The shape the agent's confirmation path returned before T-05 removed it. No
#: ``proposal`` key at all. Kept only to pin the handler's passthrough of
#: ``pipeline_id`` / ``pipeline_name`` while ``AGENT_RESULT_PASSTHROUGH`` lists them.
_AGENT_CREATED_RESULT: dict[str, Any] = {
    "status": "created",
    "reasoning_steps": [
        "User confirmed the pipeline proposal",
        "Creating pipeline from confirmed configuration...",
        "Pipeline 'customers-iceberg' created (ID: pipe-9f2c41a0b7de)",
    ],
    "pipeline_id": "pipe-9f2c41a0b7de",
    "pipeline_name": "customers-iceberg",
    "conversation_id": CONVERSATION_ID,
    "total_duration_ms": 12_004,
}


@pytest.fixture(scope="module")
def frontend_types() -> dict[str, dict[str, tuple[bool, str]]]:
    """``ChatResponse`` and the ``PipelineProposal`` it nests, parsed at test time."""
    return load_interfaces(FRONTEND_TYPE_FILE, RESPONSE_TYPE, PROPOSAL_TYPE)


@pytest.fixture
def agent_configured(mock_lambda_env, monkeypatch):
    """The endpoint is configured, so the handler reaches the agent call."""
    monkeypatch.setenv(ENDPOINT_ENV, ENDPOINT)


@pytest.fixture
def agent_unconfigured(mock_lambda_env, monkeypatch):
    """The documented degraded path (R3.8): no endpoint on this environment."""
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)


def _chat(agent_result: dict[str, Any], **body_extra: Any) -> tuple[int, dict]:
    """POST /api/chat with the agent returning ``agent_result``.

    ``invoke_agentcore_runtime`` is patched on ``agentcore_proxy`` rather than on
    the handler module: the handler imports it inside the function body, so the
    name is resolved out of the proxy module at call time. Patching there is what
    intercepts it, and it keeps the synchronous request/response shape the
    ``/chat`` path must keep (``LGR-b46a5bb-1``) — no thread, no dispatch Lambda.

    The result is deep-copied per call because a handler that mutated the agent's
    dict in place would otherwise leak into the module-level fixtures and make
    the R3.2 "unmodified" assertion compare a value against itself.
    """
    body = json.dumps({"message": MESSAGE, "conversation_id": CONVERSATION_ID, **body_extra})
    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=deepcopy(agent_result)) as invoke:
        response = _agents.handle_chat_message(body)
    assert invoke.call_count == 1, "the handler must invoke the agent exactly once per message"
    return response["statusCode"], json.loads(response["body"])


def _is_python_repr_of_a_dict(value: object) -> bool:
    """Whether ``value`` is a string that is really a ``str()`` of a ``dict``.

    R3.4's prohibition, checked by parsing rather than by sniffing for a brace:
    a legitimate prose answer could contain ``{`` (a code snippet, say), and a
    prose answer that happens to look dict-ish is not the defect. ``str(dict)``
    is valid Python literal syntax by construction, so ``ast.literal_eval``
    returning a ``dict`` is the precise test. ``json.loads`` would NOT work — the
    repr uses single quotes, which is exactly why the handler's own
    ``json.loads`` attempt fails and leaves the repr in place.
    """
    if not isinstance(value, str):
        return False
    try:
        return isinstance(ast.literal_eval(value.strip()), dict)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return False


# ---------------------------------------------------------------------------
# The oracle itself
# ---------------------------------------------------------------------------
class TestTheOracleIsNotVacuous:
    """Guards on the parse, so no assertion below can pass by checking nothing."""

    def test_the_interface_declares_exactly_the_fields_this_file_knows_about(self, frontend_types):
        """A fourth field in ``ChatResponse`` must fail here, not pass silently.

        Every other test reads the interface for names and types, so a field
        added to the TypeScript that this file has no assertion for would be
        invisible. This is the one place the *set* is pinned: if the frontend
        starts reading something new, extend the file rather than deleting this.
        """
        assert set(frontend_types[RESPONSE_TYPE]) == {"response", "reasoning_steps", "proposal"}, (
            f"{RESPONSE_TYPE} in {FRONTEND_TYPE_FILE.name} now declares "
            f"{sorted(frontend_types[RESPONSE_TYPE])}. useChatAgent reads every field it declares, so a new "
            f"one is a new contract obligation on POST /api/chat — add an assertion for it here."
        )

    def test_every_declared_field_is_optional(self, frontend_types):
        """``required_fields`` is empty here, and that is the interface's doing.

        Stated rather than left implicit, because it is why this file asserts
        presence per *path* — ``reasoning_steps`` and ``proposal`` on the proposal
        path — instead of leaning on a required-field diff the way the
        ``/costs`` and ``/cost-recommendations`` contracts can. If the frontend
        ever drops a ``?``, this fails and the path-scoped assertions below
        become the weaker statement.
        """
        assert required_fields(frontend_types[RESPONSE_TYPE]) == set(), (
            f"{RESPONSE_TYPE} now declares required fields "
            f"{sorted(required_fields(frontend_types[RESPONSE_TYPE]))}; a required-field diff is now the "
            f"stronger assertion and this file should use it."
        )

    def test_the_nested_proposal_type_is_the_one_declared_on_the_field(self, frontend_types):
        """``proposal``'s declared type must still be ``PipelineProposal``.

        ``OBJECT_TYPES`` tells the reader that name serializes to a JSON object.
        If the field were retyped, the type assertions on ``proposal`` would be
        checking a type nothing declares.
        """
        _, declared = frontend_types[RESPONSE_TYPE]["proposal"]
        assert declared == PROPOSAL_TYPE, (
            f"{RESPONSE_TYPE}.proposal is declared {declared!r}, not {PROPOSAL_TYPE!r} — repoint "
            f"PROPOSAL_TYPE/OBJECT_TYPES rather than leaving the proposal assertions pinned to a type "
            f"the interface no longer uses."
        )


# ---------------------------------------------------------------------------
# R3.1 / R3.2 / R3.3 / R3.4 — the proposal path
# ---------------------------------------------------------------------------
class TestProposalPathContract:
    """``status: "proposal"`` — the shape ``ChatPage`` renders a proposal card from."""

    def test_the_response_carries_the_fields_the_agent_supplied(self, agent_configured, frontend_types):
        """R3.1 / R3.2: the two structured fields survive, under the names the client reads.

        The names come from the interface, so a producer that emitted ``steps``
        or ``pipeline_proposal`` fails here even though it would look correct in
        isolation — that is the drift ``LGR-28b4d75-5`` exists to catch.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        declared = set(frontend_types[RESPONSE_TYPE])
        supplied = declared & set(_AGENT_PROPOSAL_RESULT)
        missing = supplied - set(body)
        assert not missing, (
            f"POST /api/chat dropped {sorted(missing)}. The agent supplied them "
            f"(nl_pipeline_builder/main.py, handler return) and {RESPONSE_TYPE} ({FRONTEND_TYPE_FILE.name}) declares "
            f"them, so useChatAgent reads them off the top level of the body."
        )

    def test_reasoning_steps_is_an_array_of_strings(self, agent_configured, frontend_types):
        """R3.1: ``string[]``, because the client calls ``.join('\\n')`` on it.

        The element type is read off the declaration rather than assumed: a list
        of objects would satisfy "is a list" and then render as ``[object
        Object]`` joined by newlines.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        _, ts_type = frontend_types[RESPONSE_TYPE]["reasoning_steps"]
        assert matches_json_type(body["reasoning_steps"], ts_type), (
            f"{RESPONSE_TYPE}.reasoning_steps is declared {ts_type}, body carried "
            f"{type(body['reasoning_steps']).__name__}"
        )
        assert ts_type.endswith("[]"), (
            f"reasoning_steps is declared {ts_type}; the element assertion below assumes an array type"
        )
        element_type = ts_type[:-2]
        for idx, step in enumerate(body["reasoning_steps"]):
            assert matches_json_type(step, element_type), (
                f"reasoning_steps[{idx}] is {type(step).__name__}, but {RESPONSE_TYPE}.reasoning_steps is "
                f"declared {ts_type} and useChatAgent renders it with .join('\\n')"
            )
        assert body["reasoning_steps"] == _REASONING_STEPS, (
            "the steps must arrive in the agent's order, unabridged — they are the assistant's whole "
            "answer when the agent returns no prose."
        )

    def test_proposal_is_a_json_object_passed_through_unmodified(self, agent_configured, frontend_types):
        """R3.2: same object, not a re-shaped or pruned one.

        Deep equality is the assertion because "structurally unmodified" is the
        requirement's own word. A handler that dropped the four keys
        ``PipelineProposal`` does not declare would pass a field-name check and
        fail this — correctly, since ``PipelineProposalCard`` is free to start
        rendering them.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        _, ts_type = frontend_types[RESPONSE_TYPE]["proposal"]
        assert matches_json_type(body["proposal"], ts_type, OBJECT_TYPES), (
            f"{RESPONSE_TYPE}.proposal is declared {ts_type}, body carried {type(body['proposal']).__name__}"
        )
        assert body["proposal"] == _PROPOSAL, (
            "the proposal was modified in transit. R3.2 requires it structurally unmodified: ChatPage "
            "builds its POST /pipelines body from source/target/schema/mappings on it."
        )

    def test_the_proposal_satisfies_the_nested_frontend_type(self, agent_configured, frontend_types):
        """R3.2, from the consumer's side: every field ``PipelineProposal`` requires.

        Field names and declared types both come from the interface. Extra keys
        are not an error — see the fixture comment — so this checks presence and
        type, never that the key set matches exactly.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        proposal = body["proposal"]
        missing = required_fields(frontend_types[PROPOSAL_TYPE]) - set(proposal)
        assert not missing, (
            f"proposal is missing {sorted(missing)}, which {PROPOSAL_TYPE} ({FRONTEND_TYPE_FILE.name}) "
            f"declares required and PipelineProposalCard renders."
        )
        for field, (required, ts_type) in frontend_types[PROPOSAL_TYPE].items():
            if not required and field not in proposal:
                continue
            assert matches_json_type(proposal[field], ts_type, OBJECT_TYPES), (
                f"{PROPOSAL_TYPE}.{field} is declared {ts_type}, proposal carried {type(proposal[field]).__name__}"
            )

    def test_status_distinguishes_the_two_agent_paths(self, agent_configured):
        """R3.3. Not in ``ChatResponse``, so the expectation is the requirement's.

        The interface cannot be the oracle for a field the client does not read
        yet; R3.3 says the client must be able to tell ``proposal`` from
        ``created``, and the agent emits the discriminator on every answer (only
        ``proposal`` since T-05). Forwarding it is strictly cheaper than
        inferring the path from which keys happen to be present.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        assert body["status"] == "proposal", (
            f"status is {body.get('status')!r}; the agent emitted "
            f"{_AGENT_PROPOSAL_RESULT['status']!r} and R3.3 requires it forwarded verbatim."
        )

    def test_response_is_prose_and_never_a_python_dict_repr(self, agent_configured, frontend_types):
        """R3.4: the defect, stated as the property it violates.

        The agent's proposal path returns no prose at all, so R3.4 permits
        ``response`` to be absent or empty here — the client falls back to
        ``reasoning_steps``. What it forbids is the current value: a
        ``str(result)`` that renders as Python syntax in the chat bubble.
        """
        _, body = _chat(_AGENT_PROPOSAL_RESULT)
        if "response" in body:
            _, ts_type = frontend_types[RESPONSE_TYPE]["response"]
            assert matches_json_type(body["response"], ts_type), (
                f"{RESPONSE_TYPE}.response is declared {ts_type}, body carried {type(body['response']).__name__}"
            )
        assert not _is_python_repr_of_a_dict(body.get("response")), (
            "response is a str() of a dict. R3.4 forbids it outright: ChatPage renders this value into "
            "the assistant bubble with white-space: pre-wrap, so the user reads a Python repr. It comes "
            'from `result.get("body", result.get("response", str(result)))` falling all the way '
            "through, because the agent returns neither `body` nor `response`."
        )


# ---------------------------------------------------------------------------
# R3.6 — the created path
# ---------------------------------------------------------------------------
class TestCreatedPathContract:
    """``status: "created"`` — the passthrough of a shape the agent no longer emits.

    Threat model T-05 removed the agent's confirmation path: the agent never
    creates a pipeline, and the handler no longer forwards ``confirm`` /
    ``proposal`` (pinned in
    ``tests/unit/lambdas/dashboard_api/test_agents_chat_confirm_request.py``).
    No request can produce this shape from the real agent any more.

    What remains is the handler's side: ``AGENT_RESULT_PASSTHROUGH`` still lists
    ``pipeline_id`` and ``pipeline_name`` (R3.6), so these tests feed a
    created-shaped result straight from the patched agent and check that the
    passthrough copies it faithfully and invents nothing. They go when those keys
    leave the allowlist. The request carries no ``confirm`` because it would no
    longer change anything.
    """

    def test_the_created_pipeline_is_identified(self, agent_configured):
        """R3.6. Neither field is in ``ChatResponse``; both are the requirement's.

        Without the id the client cannot link to the pipeline it just created,
        which is the only useful thing to do next — so R3.6 asks for it even
        though ``useChatAgent`` does not read it today.
        """
        _, body = _chat(_AGENT_CREATED_RESULT)
        assert body["pipeline_id"] == _AGENT_CREATED_RESULT["pipeline_id"]
        assert body["pipeline_name"] == _AGENT_CREATED_RESULT["pipeline_name"]

    def test_status_says_created(self, agent_configured):
        """R3.3 on this path: the discriminator the client branches on."""
        _, body = _chat(_AGENT_CREATED_RESULT)
        assert body["status"] == "created"

    def test_reasoning_steps_survive_the_created_path_too(self, agent_configured, frontend_types):
        """R3.1 is not scoped to one agent path; both supply the steps."""
        _, body = _chat(_AGENT_CREATED_RESULT)
        _, ts_type = frontend_types[RESPONSE_TYPE]["reasoning_steps"]
        assert matches_json_type(body["reasoning_steps"], ts_type)
        assert body["reasoning_steps"] == _AGENT_CREATED_RESULT["reasoning_steps"]

    def test_response_is_not_a_dict_repr_on_the_created_path_either(self, agent_configured):
        """R3.4 holds on both paths — same fall-through, same repr."""
        _, body = _chat(_AGENT_CREATED_RESULT)
        assert not _is_python_repr_of_a_dict(body.get("response"))

    def test_no_proposal_key_is_invented_when_the_agent_sent_none(self, agent_configured):
        """R3.2's other half: ``proposal`` is passed through, not synthesised.

        The created result has no ``proposal`` key. ``ChatPage`` renders a
        proposal card on truthiness of ``msg.proposal``, so a handler that echoed
        the confirmed config back — or defaulted the field to ``{}`` — would put
        a second card under the "pipeline created" message. Passes today for the
        uninteresting reason that the handler forwards nothing at all; it is here
        so the B2 fix cannot introduce the defect while adding the field.
        """
        _, body = _chat(_AGENT_CREATED_RESULT)
        assert not body.get("proposal"), (
            f"the created response carried proposal={body.get('proposal')!r}, but the agent sent none "
            f"(the pre-T-05 created shape). ChatPage renders PipelineProposalCard on any truthy msg.proposal."
        )


# ---------------------------------------------------------------------------
# R3.8 — the documented degraded path, which must not change
# ---------------------------------------------------------------------------
class TestUnconfiguredEndpointEarlyReturn:
    """No ``NL_PIPELINE_BUILDER_AGENT_ENDPOINT``: 200 with prose, unchanged.

    Passes today and must keep passing. R3.8 names this the documented degraded
    path, so it is pinned here to stop the B2 fix from generalising over it —
    the natural shape of that fix (forward ``status``, require the structured
    fields) would turn a working explanation into a 500 or an empty bubble on
    every environment where the agent is not deployed.
    """

    def test_it_returns_200_with_a_prose_response(self, agent_unconfigured, frontend_types):
        with patch("agentcore_proxy.invoke_agentcore_runtime") as invoke:
            response = _agents.handle_chat_message(json.dumps({"message": MESSAGE}))
        invoke.assert_not_called()

        status, body = response["statusCode"], json.loads(response["body"])
        assert status == 200, f"the degraded path returns 200 by decision (R3.8), got {status}"

        _, ts_type = frontend_types[RESPONSE_TYPE]["response"]
        assert matches_json_type(body["response"], ts_type), (
            f"{RESPONSE_TYPE}.response is declared {ts_type}, degraded path carried "
            f"{type(body.get('response')).__name__}"
        )
        assert body["response"].strip(), "the degraded path must explain itself, not return an empty string"
        assert not _is_python_repr_of_a_dict(body["response"])

    def test_it_carries_no_structured_fields(self, agent_unconfigured):
        """There is no agent result, so there is nothing to pass through.

        ``useChatAgent`` prefers ``reasoning_steps`` over ``response``, so an
        empty list here would render an empty assistant bubble instead of the
        explanation — ``[].join('\\n')`` is ``''``, which is falsy, but a
        one-element list of ``''`` is not.
        """
        body = json.loads(_agents.handle_chat_message(json.dumps({"message": MESSAGE}))["body"])
        assert not body.get("reasoning_steps"), (
            f"the degraded path returned reasoning_steps={body.get('reasoning_steps')!r}; there is no "
            f"agent result to derive them from, and useChatAgent prefers them over the prose response."
        )
        assert not body.get("proposal")
