# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for complex mapping agent: sample data reading, question generation, response
parsing, and the agent's own trace plumbing (p52 task 4.2 — see the banner near the
bottom of this file for why that last one lives here rather than under tests/unit/shared/).
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "complex_mapping_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

_mock_s3 = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.return_value = _mock_s3
_mock_boto3.resource.return_value = MagicMock()

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
        "mapping_memory": MagicMock(),
    },
):
    import main as cma_main

    cma_main._s3_client = _mock_s3


# ── read_sample_data ────────────────────────────────────────────────


class TestReadSampleData:
    def test_csv_file(self):
        csv_body = "name,age,city\nAlice,30,Seattle\nBob,25,Portland\n"
        _mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=csv_body.encode()))}
        result = cma_main.read_sample_data("s3://bucket/data.csv")
        assert "name" in result
        assert "Alice" in result["name"]
        assert "30" in result["age"]

    def test_json_file(self):
        json_body = json.dumps(
            [
                {"id": "1", "email": "a@b.com"},
                {"id": "2", "email": "c@d.com"},
            ]
        )
        _mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=json_body.encode()))}
        result = cma_main.read_sample_data("s3://bucket/data.json")
        assert "id" in result
        assert "1" in result["id"]

    def test_invalid_uri_returns_empty(self):
        result = cma_main.read_sample_data("")
        assert result == {}

    def test_non_s3_uri_returns_empty(self):
        result = cma_main.read_sample_data("https://example.com/data.csv")
        assert result == {}

    def test_s3_error_returns_empty(self):
        _mock_s3.get_object.side_effect = Exception("AccessDenied")
        result = cma_main.read_sample_data("s3://bucket/data.csv")
        assert result == {}
        _mock_s3.get_object.side_effect = None


# ── generate_questions ──────────────────────────────────────────────


class TestGenerateQuestions:
    def test_low_confidence_mapping_generates_question(self):
        mappings = [
            {"source": "addr_1", "target": "street_address", "confidence": 0.5, "reasoning": "unsure"},
        ]
        samples = {"addr_1": ["123 Main St", "456 Oak Ave"]}
        questions = cma_main.generate_questions(mappings, samples)
        assert len(questions) == 1
        assert questions[0]["field"] == "addr_1"
        assert "123 Main St" in questions[0]["question"]

    def test_no_low_confidence_returns_empty(self):
        questions = cma_main.generate_questions([], {})
        assert questions == []

    def test_needs_review_target(self):
        mappings = [
            {"source": "code_x", "target": "needs_review", "confidence": 0.3, "reasoning": ""},
        ]
        samples = {"code_x": ["A1", "B2"]}
        questions = cma_main.generate_questions(mappings, samples)
        assert len(questions) == 1
        assert "unmapped" in questions[0]["options"]

    def test_empty_source_skipped(self):
        mappings = [{"source": "", "target": "x", "confidence": 0.1}]
        questions = cma_main.generate_questions(mappings, {})
        assert questions == []


# ── _parse_agent_response ───────────────────────────────────────────


class TestParseAgentResponse:
    def test_plain_json(self):
        text = json.dumps({"mappings": [{"source": "a", "target": "b"}]})
        result = cma_main._parse_agent_response(text)
        assert len(result) == 1
        assert result[0]["source"] == "a"

    def test_json_with_code_fences(self):
        text = '```json\n{"mappings": [{"source": "x", "target": "y"}]}\n```'
        result = cma_main._parse_agent_response(text)
        assert len(result) == 1
        assert result[0]["target"] == "y"

    def test_json_with_generic_code_fences(self):
        text = '```\n{"mappings": [{"source": "a", "target": "b"}]}\n```'
        result = cma_main._parse_agent_response(text)
        assert len(result) == 1

    def test_json_array_response(self):
        text = json.dumps([{"source": "a", "target": "b"}])
        result = cma_main._parse_agent_response(text)
        assert len(result) == 1

    def test_invalid_json_returns_empty(self):
        result = cma_main._parse_agent_response("not json at all")
        assert result == []

    def test_json_embedded_in_text(self):
        text = 'Here is the mapping:\n{"mappings": [{"source": "a", "target": "b"}]}'
        result = cma_main._parse_agent_response(text)
        assert len(result) == 1

    def test_idempotent_parsing(self):
        """Parsing the same response twice yields the same result."""
        text = json.dumps({"mappings": [{"source": "a", "target": "b", "confidence": 0.9}]})
        r1 = cma_main._parse_agent_response(text)
        r2 = cma_main._parse_agent_response(text)
        assert r1 == r2


# ═════════════════════════════════════════════════════════════════════════════
# p52 task 4.2 — THIS agent's trace plumbing, and why it needs its own case
# ═════════════════════════════════════════════════════════════════════════════
#
# WHY THIS PATH GETS A CASE OF ITS OWN: IT IS THE **OLDER** EXPOSURE.
# ------------------------------------------------------------------
# The framing matters as much as the assertions, so it is written down rather
# than assumed.
#
# `main.py` constructs an `AgentTracePlugin` (`handler`, plugin construction at
# `:493`), hands it to the Strands agent as `hooks=[trace_plugin]` (`:525`), and
# calls `trace_plugin.flush()` (`:616`).  Its prompt comes from
# `build_evidence_prompt` (def `:269`), which renders `Sample values: [...]` at
# `:292` from the values `read_sample_data` (def `:84`) pulled out of the
# customer's own file.  Put together: **sampled customer values have reached the
# agent_traces table on every complex-mapping run since P13** (`b48a349b7b`,
# "P13: Agentic capabilities + P13a: Agent observability" — the commit that
# added both the five autonomous agents and `AgentTracePlugin`).
#
# p49 did **not** create that exposure.  It widened a *different* one: it added
# sample values to `generate_mapping_with_bedrock`'s prompt in
# `schema_mapping/lambda_function.py`, taking sampled values in traces from
# low-confidence runs only to **every** mapping run.  So the correct framing is
# "a pre-existing exposure that p49 made unconditional", not "a p49 regression".
#
# That distinction is the whole reason this file has a test at all.  A reader who
# believes p49 introduced the exposure will look only where p49 edited — and this
# agent is not there.  They will not think to cover this path, and this path is
# the one that has been leaking the longest.
#
# LINE CITES ARE BY NAME **AND** LINE, deliberately.  This area has produced
# five stale cites so far: `LGR-a419843-2` cites a stale line for its own fence
# test; p52 gate 1.2 found three more; and task 3.4 found that
# `render_sample_values_section`'s docstring cites `build_evidence_prompt:275`,
# which is now `:292`.  Every number above was re-verified against this tree
# while writing this section, and two of the numbers task 4.2 was handed were
# already wrong: `hooks=[trace_plugin]` is at **`:525`**, not `:508`, and
# `flush()` is at **`:616`**, not `:599` — task 3.1's comment block above
# `read_sample_data`'s normalisation pushed everything below it down.
#
# WHAT THIS ADDS THAT tests/unit/shared/ AND tests/integration/ DO NOT
# -------------------------------------------------------------------
# Three p52 tests already exist and none of them covers what is below.  This is
# **not** a fourth test of the redactor:
#
# * `tests/unit/shared/test_trace_sample_redaction_prop.py` (task 3.2) drives the
#   real `read_sample_data` and `build_evidence_prompt` under Hypothesis and
#   asserts no generated value survives redaction.  Redaction of this producer's
#   output is settled there, under generation, over a hostile alphabet.
# * `tests/unit/shared/test_trace_sample_redaction.py` (task 3.4) pins
#   `build_evidence_prompt`'s literal rendered form as a format canary.
# * `tests/integration/test_agent_trace_integration.py` (task 4.1) drives both
#   Preview_Field writers through one `flush()` against a moto-backed table.
#
# All three enter at a *function* — a render, or a hand-built plugin.  None of
# them goes through **this agent's own wiring**.  What is asserted below is that
# the agent still hands its plugin to the agent constructor, that the prompt the
# plugin captures is the one `build_evidence_prompt` rendered, and that `flush()`
# still runs after the model call — so that a change to *this* agent which
# bypassed, reordered or dropped the trace plumbing fails **in the agent's own
# test file**, next to the code that broke, rather than only in `shared/` or not
# at all.  Concretely, each of these agent-side edits turns the tests below red:
#
#   * dropping `hooks=[trace_plugin]` (`:525`)         → no model call in the row
#   * dropping `trace_plugin.flush()` (`:616`)         → no row at all
#   * moving `flush()` above `agent(prompt)`           → no model call in the row
#   * building the plugin with the wrong run_id (`:493`) → row not found by key
#   * deleting `read_sample_data`'s `\n` collapse (`:133`) → the newline test's
#     value survives and its count degrades
#
# SCOPE, STATED SO THE ASSERTIONS ARE NOT OVERREAD
# ------------------------------------------------
# "No sampled value persists" below means **in the agent_traces row**, which is
# R5's subject.  The same `handler` deliberately writes sample values elsewhere
# in the same invocation — `evidence[source]["sample_values"]` into the state
# table (`:559`, written at `:596`) and `sample_values` inside each generated
# question (`generate_questions`, `:392`).  Those are not Preview_Fields, they
# are not written by `flush()`, and p52 does not touch them.  Do not read a green
# run here as "this agent stores no sample values anywhere".
#
# LEDGER GAP — a gap, not an all-clear
# ------------------------------------
# Nothing governs this agent's sample-value handling from either direction.
# `why` on `agentcore/complex_mapping_agent/main.py` returns five broad snapshot
# commits — `0176ccc686`, `2b45b72912`, `7ad6ca1ad9`, `5feb9b7647`,
# `dd80c6d048` — every one with `records: []`, and `decisions(scope=…)` returns a
# single unrelated record (`LGR-d5bcee9-1`, about cdk.out).  The same gap covers
# `shared/agent_trace_plugin.py`.  `why` on **this test file** returns the same
# shape: `7ad6ca1ad9`, `5feb9b7647`, `dd80c6d048` and `b48a349b7b`, all
# `records: []` — which is how the P13 date above was confirmed rather than
# recalled.  `LGR-1e135d2-4`'s `revisit_when` ("the trace table's
# retention/redaction posture changes") is the record Part B answers to.  Ledger
# at `047778f`: 239 records, 239 template, 0 backfilled.
#
# CodeGraph, cross-checked against grep per design risk N6, was wrong again in
# both directions on exactly these symbols: it reports `read_sample_data` as
# having "no covering tests" when this very file calls it five times (`:45`,
# `:58`, `:63`, `:67`, `:72`), and reports `AgentTracePlugin` as having 17
# callers against 9 grep-verified production construction sites, listing only the
# integration file as coverage while `tests/unit/shared/test_agent_trace_plugin.py`
# collects 51 tests (36 when design risk N6 was written; task 3.3 is adding more,
# so re-collect rather than cite this).  Its `build_evidence_prompt` figures were right — 1 production
# caller (`:509`), and zero test call sites before p52; task 3.2 and task 3.4 gave
# it its first two, and this file gives it its first inside the agent's own suite.
#
# FIXTURES, MOCKS AND THE ONE PLACE MOCKING IS UNAVOIDABLE
# --------------------------------------------------------
# The traces table — the thing actually asserted on — is the **shared**
# `mock_dynamodb_tables` fixture from `tests/fixtures/conftest.py`.  No
# `moto.mock_aws()` is created here (`tests/unit/test_no_duplicate_mocking_prop.py`)
# and no region is set (`LGR-3e45e65-1`: the unit layer pins it in
# `tests/unit/conftest.py`).  The fixture's Table handle is bound straight onto
# the plugin module's `_traces_table`, which is what `_get_traces_table()`
# returns when it is not `None` — the same binding
# `tests/unit/lambdas/dashboard_api/test_schema_delete_endpoint.py` uses, and it
# side-steps the fixture's us-east-1 / unit-layer us-west-2 region split.
#
# The agent module's *own* `_s3_client` and `_dynamodb` handles are `MagicMock`s
# swapped in with `monkeypatch.setattr`, as
# `tests/unit/agents/data_quality_agent/test_report_bounds.py:_wire` already does
# for this same class of module.  That is not a second copy of a shared fixture:
# both handles are bound at *import* time from a stubbed `boto3` (unavoidable —
# `bedrock_agentcore` is not installed, so the module cannot be imported without
# stubbing it), so there is no seam a moto-backed client could be threaded
# through, and neither S3 nor the state table is this test's subject.
#
# LOADING: TWO PRIVATE COPIES, NEITHER PUBLISHED TO sys.modules
# -------------------------------------------------------------
# `main.py` is loaded a **second** time here, under a private alias with
# `@app.entrypoint` as the identity decorator, because the module-scope import at
# the top of this file mocks `BedrockAgentCoreApp` wholesale and so leaves
# `cma_main.handler` a `MagicMock` — the real handler is unreachable through it.
# Same reason, same shape as
# `tests/unit/agents/data_quality_agent/test_report_bounds.py:42`.
#
# `shared/agent_trace_plugin.py` is loaded the same way rather than plainly
# imported, because two tracked modules —
# `tests/unit/lambdas/schema_mapping/test_no_ghost_approval_publication.py:45`
# and `test_schema_mapping_complex_agent.py:46` — run
# `sys.modules.setdefault("agent_trace_plugin", MagicMock())` at **module**
# scope.  pytest imports every test module during collection, so a plain
# `import agent_trace_plugin` here would bind the real module or their mock
# depending on collection order alone: green in a scoped run, and a silently
# vacuous test in `make test-fast`.  That is precisely the defect
# `LGR-c58f276-4` (**template / ratified**) records, and the loader below plus
# the two import-time assertions are the remedy it ratified.  Neither copy is
# published under a public name; the plugin copy is placed under
# `"agent_trace_plugin"` only inside a `patch.dict`, for the duration of the
# handler call, which is how the real module reaches `main.py:491`'s
# call-time import.

import contextlib
import importlib.util
from types import SimpleNamespace

# Pre-imported so it is in the entry snapshot of the patch.dict blocks below and
# survives their restore. `patch.dict("sys.modules", …)` does not merely undo the
# keys it overrode: it clears sys.modules and re-applies the entry snapshot, so a
# module first imported *inside* a block is evicted on exit and the next importer
# gets a second, distinct copy. Same hazard the jinja2 import in
# `tests/conftest.py` exists to prevent.
import decimal_utils  # noqa: F401

_MAIN_PATH = os.path.join(AGENT_DIR, "main.py")
_PLUGIN_PATH = os.path.join(SHARED_DIR, "agent_trace_plugin.py")


def _load_private(alias: str, path: str, stubs: dict):
    """Exec *path* as a module named *alias*, without publishing it to sys.modules.

    A module needing no stubs is exec'd with **no** ``patch.dict`` at all rather
    than with an empty one: the restore clears sys.modules and re-applies the
    entry snapshot either way, so an empty block would still evict whatever the
    module imported for the first time (``env_utils``, ``aws_lambda_powertools``)
    and hand the next importer a second copy — for no gain.
    """
    spec = importlib.util.spec_from_file_location(alias, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", stubs) if stubs else contextlib.nullcontext():
        spec.loader.exec_module(module)
    return module


# ── The real plugin, under a private alias ───────────────────────────
_TRACE_PLUGIN = _load_private("_p52_cma_agent_trace_plugin", _PLUGIN_PATH, {})

# Import-time non-vacuity assertions, per LGR-c58f276-4: fail loudly at
# collection if what was bound is a mock or is missing the thing under test,
# rather than passing vacuously later.
assert not isinstance(_TRACE_PLUGIN, MagicMock), "agent_trace_plugin resolved to a mock"
assert hasattr(_TRACE_PLUGIN, "_redact_sample_values"), (
    "the real shared/agent_trace_plugin.py has no _redact_sample_values — p52 task 3.1's "
    "redactor is what these tests assert the effect of"
)
assert _TRACE_PLUGIN._SAMPLE_MARKER == "Sample values: [", (
    f"the redactor's marker constant moved to {_TRACE_PLUGIN._SAMPLE_MARKER!r}; "
    "build_evidence_prompt:292 still emits 'Sample values: ['"
)

# ── The real handler, under a private alias ──────────────────────────
_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

cma = _load_private(
    "_p52_cma_main_real_handler",
    _MAIN_PATH,
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": MagicMock(),
    },
)

assert not isinstance(cma.handler, MagicMock), (
    "the passthrough @app.entrypoint did not take — cma.handler is still a MagicMock, so "
    "the agent's real wiring (plugin construction :493, hooks=[…] :525, flush() :616) "
    "would not be exercised at all"
)


# ── A Strands stand-in that exercises the real hook handoff ──────────
#
# Strands is not installed in this venv (`test_strands_hook_events.py` guards its
# own import with `pytest.importorskip`), so the SDK's event classes are stood in
# for with real classes under the module names the plugin imports at call time —
# the shape `_mock_strands_events`
# (`tests/unit/shared/test_agent_trace_plugin.py:78`) established.
#
# The stand-in agent below is deliberately *not* a MagicMock: it calls the
# plugin's real `register_hooks`, keeps whatever that registers, and dispatches
# real events to it. So the handoff at `main.py:525` is exercised end to end
# rather than asserted about — if `hooks=[trace_plugin]` stops being passed,
# nothing registers, no model call is captured, and the trace row goes empty.

_EVENT_NAMES = (
    "BeforeInvocationEvent",
    "AfterInvocationEvent",
    "BeforeModelCallEvent",
    "AfterModelCallEvent",
    "BeforeToolCallEvent",
    "AfterToolCallEvent",
)
_EVENT_CLASSES = {name: type(name, (), {}) for name in _EVENT_NAMES}
_events_module = MagicMock()
for _name, _cls in _EVENT_CLASSES.items():
    setattr(_events_module, _name, _cls)

_MODEL_ID = "us.anthropic.claude-sonnet-4-6"

#: What the stand-in model "returns". Shape only — `_parse_agent_response` has to
#: get something parseable, and the response text is not this test's subject.
_AGENT_RESPONSE = '{"mappings": [{"source": "addr_1", "target": "street_address", '
_AGENT_RESPONSE += '"confidence": 0.95, "reasoning": "sample values look like street addresses"}]}'


class _HookRegistry:
    """The two methods of Strands' HookRegistry that AgentTracePlugin touches."""

    def __init__(self) -> None:
        self.callbacks: dict[type, list] = {}

    def add_callback(self, event_type, callback) -> None:
        self.callbacks.setdefault(event_type, []).append(callback)

    def emit(self, event_name: str, event) -> None:
        for callback in self.callbacks.get(_EVENT_CLASSES[event_name], []):
            callback(event)


class _RecordingAgent:
    """Stands in for `strands.Agent`, firing the lifecycle the plugin listens for."""

    instances: list = []

    def __init__(self, model=None, system_prompt=None, hooks=None, **kwargs) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.hooks = list(hooks or [])
        self.messages: list[dict] = []
        self.prompt_seen: str | None = None
        self.registry = _HookRegistry()
        for hook in self.hooks:
            hook.register_hooks(self.registry)
        _RecordingAgent.instances.append(self)

    def __call__(self, prompt: str) -> str:
        self.prompt_seen = prompt
        # The plugin reads the prompt off `event.agent.messages` in
        # `_on_before_model`, not off the call argument, so the message history is
        # populated the way a real invocation would leave it.
        self.messages = [{"role": "user", "content": [{"text": prompt}]}]
        usage = {"inputTokens": 1200, "outputTokens": 90, "cacheReadInputTokens": 0}

        self.registry.emit("BeforeInvocationEvent", SimpleNamespace(agent=self))
        self.registry.emit("BeforeModelCallEvent", SimpleNamespace(agent=self))
        self.registry.emit(
            "AfterModelCallEvent",
            SimpleNamespace(
                agent=self,
                stop_response=SimpleNamespace(usage=usage, message={"content": [{"text": _AGENT_RESPONSE}]}),
                exception=None,
            ),
        )
        self.registry.emit(
            "AfterInvocationEvent",
            SimpleNamespace(
                agent=self,
                result=SimpleNamespace(
                    metrics=SimpleNamespace(accumulated_usage=usage),
                    stop_reason="end_turn",
                ),
            ),
        )
        return _AGENT_RESPONSE


_TRACES_TABLE = "agentic_data_ingestion_agent_traces"
_MARKER = "Sample values: ["
_RUN_ID = "run-p52-cma-trace-001"
_PIPELINE_ID = "pipe-p52-cma-001"


def _payload(field_names: list[str], csv_key: str = "samples.csv") -> dict:
    return {
        "run_id": _RUN_ID,
        "pipeline_id": _PIPELINE_ID,
        "source_schema": {"fields": [{"name": name, "type": "string"} for name in field_names]},
        "target_schema": {"fields": [{"name": "street_address", "type": "string"}]},
        "sample_data_uri": f"s3://p52-cma-samples/{csv_key}",
        "confidence_threshold": 0.7,
    }


def _drive_handler(monkeypatch, traces_table, csv_body: str, payload: dict):
    """Run the REAL handler end to end; return (agent stand-in, persisted trace item).

    Everything the trace path touches is real: `read_sample_data`,
    `build_evidence_prompt`, `AgentTracePlugin`, `register_hooks`,
    `_on_before_model`, `_on_after_model`, `_redact_sample_values` and `flush()`.
    """
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=csv_body.encode()))}
    monkeypatch.setattr(cma, "_s3_client", s3)
    monkeypatch.setattr(cma, "_dynamodb", MagicMock())  # the state table — not this test's subject
    monkeypatch.setattr(cma, "_require_env", lambda name, default=None: default or name)

    # The one binding that matters: the shared fixture's moto-backed Table, onto
    # the plugin's lazy handle. `_get_traces_table()` short-circuits while this is
    # not None, so no region or env lookup happens.
    monkeypatch.setattr(_TRACE_PLUGIN, "_traces_table", traces_table)

    _RecordingAgent.instances.clear()

    memory = MagicMock()
    memory.MappingMemoryClient.return_value.enabled = False  # historical lookup returns []

    strands_module = MagicMock()
    strands_module.Agent = _RecordingAgent
    bedrock_module = MagicMock()
    bedrock_module.BedrockModel = lambda model_id=None, **kw: SimpleNamespace(
        config={"model_id": model_id or _MODEL_ID}
    )

    with patch.dict(
        "sys.modules",
        {
            # The real plugin, under the name main.py:491 imports at call time.
            "agent_trace_plugin": _TRACE_PLUGIN,
            "audit_utils": MagicMock(),
            "mapping_memory": memory,
            "model_config": MagicMock(resolve_model_id=lambda: _MODEL_ID),
            "strands": strands_module,
            "strands.models": MagicMock(),
            "strands.models.bedrock": bedrock_module,
            "strands.hooks": MagicMock(),
            "strands.hooks.events": _events_module,
            "strands.hooks.registry": MagicMock(),
        },
    ):
        cma.handler(payload)

    agent = _RecordingAgent.instances[-1] if _RecordingAgent.instances else None
    item = traces_table.get_item(Key={"run_id": payload["run_id"], "step_name": "complex_mapping"}).get("Item")
    return agent, item


class TestComplexMappingTraceCarriesNoSampledValues:
    """The agent's own wiring: prompt → plugin → flush → table, with values gone.

    R5.5, and the older half of Part B's exposure. See the banner above for why
    this path is covered separately from `tests/unit/shared/`.
    """

    _SECRETS = ("CUST-0001-SECRET", "CUST-0002-SECRET", "APT-4-SECRET", "UNIT-9-SECRET")

    _CSV = "customer_ref,addr_1\nCUST-0001-SECRET,123 Main St APT-4-SECRET\nCUST-0002-SECRET,77 Elm Ave UNIT-9-SECRET\n"

    def test_the_persisted_trace_keeps_the_prompt_and_none_of_its_sample_values(
        self, monkeypatch, mock_dynamodb_tables
    ):
        """One real invocation, one flush, no sampled value in the row.

        Covers R5.1, R5.5, R6.1, R6.2, R6.3, R6.4 as they apply to this path, and
        the wiring at `main.py:493` / `:525` / `:616`.
        """
        agent, item = _drive_handler(
            monkeypatch,
            mock_dynamodb_tables[_TRACES_TABLE],
            self._CSV,
            _payload(["customer_ref", "addr_1"]),
        )

        # --- the wiring: the plugin the agent was handed is the real one -----
        assert agent is not None, "main.py never constructed a Strands agent"
        assert agent.hooks, (
            "the agent was constructed with no hooks — `hooks=[trace_plugin]` "
            "(main.py:525) is how this agent's trace data is captured at all, and "
            "without it every invocation writes an empty trace"
        )
        plugin = agent.hooks[0]
        assert isinstance(plugin, _TRACE_PLUGIN.AgentTracePlugin), f"hooks[0] is not the trace plugin: {plugin!r}"
        assert (plugin.pipeline_id, plugin.run_id, plugin.step_name) == (_PIPELINE_ID, _RUN_ID, "complex_mapping"), (
            "the plugin was constructed with different identifiers than the payload carried "
            "(main.py:493-497), so its row lands under the wrong key"
        )

        # --- non-vacuity: the model really was shown the values -------------
        # Without this, every assertion below would also pass on a prompt that
        # never carried a sample value in the first place.
        assert agent.prompt_seen is not None
        assert _MARKER in agent.prompt_seen, (
            "build_evidence_prompt (main.py:292) rendered no Sample_Block, so this test is "
            "not exercising the exposure it exists to cover"
        )
        for secret in self._SECRETS:
            assert secret in agent.prompt_seen, f"{secret!r} never reached the prompt — check the CSV fixture"

        # --- the row exists, and carries the model call ---------------------
        assert item is not None, (
            "no trace row was written for this run. `trace_plugin.flush()` (main.py:616) is "
            "what persists it; flush() is fire-and-forget, so a failure inside it is logged "
            "and swallowed rather than raised"
        )
        assert item["pipeline_id"] == _PIPELINE_ID
        calls = item["trace_data"]["model_calls"]
        assert len(calls) == 1, (
            f"expected exactly one captured model call, got {len(calls)}. An empty list means the "
            "hook handoff did not happen, or flush() ran before the model call did"
        )

        preview = calls[0]["prompt_preview"]
        assert preview != _TRACE_PLUGIN._REDACTION_FAILED, (
            "redaction raised and the whole preview was withheld — R7.3's degraded path, not "
            "the intended one. The point of this test is the redacted prompt, not the fallback"
        )
        assert "## Source Schema" in preview, (
            f"the captured prompt is not build_evidence_prompt's render: {preview[:200]!r}"
        )

        # --- R5.1/R5.5: no sampled value survives anywhere in the row -------
        serialised = json.dumps(item, default=str)
        for secret in self._SECRETS:
            assert secret not in serialised, (
                f"sampled customer value {secret!r} persisted into the agent_traces row, where it "
                "would sit for the table's 90-day TTL. This is the exposure that has been live on "
                "this path since P13 (b48a349b7b)"
            )

        # --- R6: the diagnostic fact survives -------------------------------
        assert _MARKER in preview, "R6.4: the marker must survive so a reader can still find the line"
        assert preview.count("2 values redacted") == 2, (
            "R6.1/R6.2: both sampled fields carried two values, so both blocks should read "
            f"'2 values redacted'. Got: {[ln for ln in preview.splitlines() if _MARKER in ln]!r}"
        )
        for field_name in ("customer_ref", "addr_1"):
            assert field_name in preview, f"R6.3: field name {field_name!r} was lost with its values"

    def test_a_value_that_spanned_a_line_break_in_the_source_file_is_still_removed(
        self, monkeypatch, mock_dynamodb_tables
    ):
        """The newline case, on the real path, through the real flush().

        A quoted CSV cell spanning a line break arrives from `csv.DictReader` as
        one value carrying `\\n`. Marker-to-end-of-line redaction cannot reach
        past a line break, so before p52 task 3.1 this put every value after the
        break onto an **unmarked** line, outside redaction — and gate 1.2's render
        of that state reported `0 values redacted` for a field that had one.
        `read_sample_data`'s `\\n`/`\\r` collapse (`main.py:133`) is what closes
        it, and closing it is a **prompt-content** change: the model now sees the
        address space-joined.

        Two facts are therefore asserted together below, because they fail
        together: the value is gone from the row, **and** the count is exact.
        Measured against a mutated copy of the producer with the collapse
        removed, the shipped redactor degrades to its documented floor —
        `1+ values redacted, exact count unavailable: list continued past a line
        break` — and `APT-4-SECRET` survives into the row. Not a bare `0`, which
        is what a naive count would have read; the floor is the redactor's own
        refusal to state a count it cannot derive.

        Task 3.2's `TestRawNewlineResidueIsTheDocumentedLimit` owns this as a
        property over the two producers' renders. What is added here is the same
        fact **through this agent's flush() and into the table** — the surface the
        residue would actually have been persisted on.
        """
        csv_body = 'addr_1\n"123 Main St\nAPT-4-SECRET"\n'

        agent, item = _drive_handler(
            monkeypatch,
            mock_dynamodb_tables[_TRACES_TABLE],
            csv_body,
            _payload(["addr_1"]),
        )

        assert agent is not None and item is not None
        # The producer collapsed the break, so the whole value is on the marker's line.
        assert "123 Main St APT-4-SECRET" in agent.prompt_seen, (
            "read_sample_data (main.py:133) no longer collapses the embedded newline, so the "
            "rendered value list is split across lines"
        )
        marker_lines = [line for line in agent.prompt_seen.splitlines() if _MARKER in line]
        assert len(marker_lines) == 1 and marker_lines[0].endswith("]"), (
            f"the value list no longer closes on the marker's own line: {marker_lines!r}"
        )

        preview = item["trace_data"]["model_calls"][0]["prompt_preview"]
        assert "APT-4-SECRET" not in json.dumps(item, default=str), (
            "the value carried past the line break survived into the trace row — the exact leak "
            "p52 gate 1.2 rendered, reproduced through this agent's own flush()"
        )
        assert "1 values redacted" in preview, (
            "R6.2: one value was present, so the count must read 1. A degraded count here means "
            "the redactor's span ended on an unterminated quote, i.e. the producer stopped "
            f"normalising newlines. Got: {[ln for ln in preview.splitlines() if _MARKER in ln]!r}"
        )
        assert "exact count unavailable" not in preview, (
            "the count degraded to its 1+ floor, which only happens when the marker's line holds "
            "part of a value — so the producer-side normalisation at main.py:133 is gone"
        )
