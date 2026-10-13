# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/agent_trace_plugin.py.

Covers: hook callback registration, model call capture, tool call capture,
token accumulation, cost calculation, DynamoDB write, truncation behavior,
fire-and-forget flush, and pricing lookup.
"""

import json
import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Set env before import so require_env() uses the default
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
# agent_trace_plugin builds a boto3 resource at import time, and section 8b's
# import below runs at collection rather than inside a test, so a region has to
# be resolvable without relying on the ambient shell.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# Imported rather than transcribed (p52 task 3.3).  The placeholder literal and
# the Preview_Field tuple are the module's own constants, so renaming either —
# or adding a fifth Preview_Field — cannot leave section 8b asserting a stale
# copy.  Section 8b re-checks this equality against the freshly reimported
# module, because ``_fresh_module()`` hands tests a different module object than
# the one this import bound.
from agent_trace_plugin import _PREVIEW_FIELDS, _REDACTION_FAILED


def _fresh_module():
    """Force reimport of agent_trace_plugin inside a clean context."""
    for mod_name in list(sys.modules):
        if "agent_trace_plugin" in mod_name:
            del sys.modules[mod_name]
    import agent_trace_plugin

    return agent_trace_plugin


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the module-level _traces_table between tests."""
    mod = _fresh_module()
    mod._traces_table = None
    yield


@pytest.fixture
def mod():
    """Return a fresh agent_trace_plugin module."""
    return _fresh_module()


@pytest.fixture
def plugin(mod):
    """Return a fresh AgentTracePlugin instance."""
    return mod.AgentTracePlugin(
        pipeline_id="pipe-001",
        run_id="run-001",
        step_name="code_generation",
    )


# ---------------------------------------------------------------------------
# 1. Hook callback registration
# ---------------------------------------------------------------------------


def _mock_strands_events():
    """Create mock strands event classes and patch them into sys.modules."""
    event_classes = {
        "BeforeInvocationEvent": type("BeforeInvocationEvent", (), {}),
        "AfterInvocationEvent": type("AfterInvocationEvent", (), {}),
        "BeforeModelCallEvent": type("BeforeModelCallEvent", (), {}),
        "AfterModelCallEvent": type("AfterModelCallEvent", (), {}),
        "BeforeToolCallEvent": type("BeforeToolCallEvent", (), {}),
        "AfterToolCallEvent": type("AfterToolCallEvent", (), {}),
    }
    mock_events_module = MagicMock(**event_classes)
    for name, cls in event_classes.items():
        setattr(mock_events_module, name, cls)
    return event_classes, {
        "strands": MagicMock(),
        "strands.hooks": MagicMock(),
        "strands.hooks.events": mock_events_module,
        "strands.hooks.registry": MagicMock(),
    }


class TestRegisterHooks:
    """register_hooks adds all 6 callbacks to the registry."""

    def test_register_hooks_adds_six_callbacks(self, plugin):
        _, modules = _mock_strands_events()
        with patch.dict("sys.modules", modules):
            registry = MagicMock()
            plugin.register_hooks(registry)
            assert registry.add_callback.call_count == 6

    def test_register_hooks_event_types(self, plugin):
        event_classes, modules = _mock_strands_events()
        with patch.dict("sys.modules", modules):
            registry = MagicMock()
            plugin.register_hooks(registry)

            registered_events = {call.args[0] for call in registry.add_callback.call_args_list}
            expected = set(event_classes.values())
            assert registered_events == expected


# ---------------------------------------------------------------------------
# 2. Model call capture
# ---------------------------------------------------------------------------


class TestModelCallCapture:
    """_on_before_model / _on_after_model records model calls."""

    def test_model_call_records_duration(self, plugin):
        before_event = MagicMock()
        plugin._on_before_model(before_event)

        after_event = MagicMock()
        after_event.stop_response = None
        after_event.exception = None
        after_event.agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": "claude-sonnet-4-5"}))
        plugin._on_after_model(after_event)

        assert len(plugin._model_calls) == 1
        call = plugin._model_calls[0]
        assert call["duration_ms"] >= 0
        assert call["model_id"] == "claude-sonnet-4-5"

    def test_model_call_extracts_response_preview(self, plugin):
        plugin._on_before_model(MagicMock())

        after_event = MagicMock()
        after_event.exception = None
        after_event.agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": "test-model"}))
        msg = {"content": [{"text": "Hello world response"}]}
        after_event.stop_response = SimpleNamespace(message=msg)
        plugin._on_after_model(after_event)

        call = plugin._model_calls[0]
        assert "Hello world response" in call["response_preview"]

    def test_model_call_captures_exception(self, plugin):
        plugin._on_before_model(MagicMock())

        after_event = MagicMock()
        after_event.stop_response = None
        after_event.exception = ValueError("model error")
        after_event.agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": ""}))
        plugin._on_after_model(after_event)

        call = plugin._model_calls[0]
        assert "model error" in call["error"]

    def test_model_call_without_before_uses_current_time(self, plugin):
        """_on_after_model still works if _on_before_model was not called."""
        after_event = MagicMock()
        after_event.stop_response = None
        after_event.exception = None
        after_event.agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": ""}))
        plugin._on_after_model(after_event)

        assert len(plugin._model_calls) == 1
        assert plugin._model_calls[0]["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# 3. Tool call capture
# ---------------------------------------------------------------------------


class TestToolCallCapture:
    """_on_before_tool / _on_after_tool records tool calls."""

    def test_tool_call_records_name_params_duration_status(self, plugin):
        before_event = MagicMock()
        before_event.tool_use = {"name": "create_glue_job", "input": {"job_name": "test-job"}}
        plugin._on_before_tool(before_event)

        after_event = MagicMock()
        after_event.exception = None
        after_event.result = {
            "status": "success",
            "content": [{"text": "Job created"}],
        }
        plugin._on_after_tool(after_event)

        assert len(plugin._tool_calls) == 1
        call = plugin._tool_calls[0]
        assert call["tool_name"] == "create_glue_job"
        assert "test-job" in call["tool_params"]
        assert call["duration_ms"] >= 0
        assert call["status"] == "success"
        assert "Job created" in call["result_preview"]

    def test_tool_call_error_status_from_exception(self, plugin):
        before_event = MagicMock()
        before_event.tool_use = {"name": "failing_tool", "input": {}}
        plugin._on_before_tool(before_event)

        after_event = MagicMock()
        after_event.exception = RuntimeError("tool failed")
        after_event.result = None
        plugin._on_after_tool(after_event)

        call = plugin._tool_calls[0]
        assert call["status"] == "error"
        assert "tool failed" in call["result_preview"]

    def test_after_tool_without_before_is_noop(self, plugin):
        """_on_after_tool does nothing if _on_before_tool was not called."""
        after_event = MagicMock()
        after_event.exception = None
        after_event.result = {"status": "success", "content": []}
        plugin._on_after_tool(after_event)

        assert len(plugin._tool_calls) == 0

    def test_tool_call_json_result_preview(self, plugin):
        before_event = MagicMock()
        before_event.tool_use = {"name": "query_tool", "input": {}}
        plugin._on_before_tool(before_event)

        after_event = MagicMock()
        after_event.exception = None
        after_event.result = {
            "status": "success",
            "content": [{"json": {"rows": 42}}],
        }
        plugin._on_after_tool(after_event)

        call = plugin._tool_calls[0]
        assert "42" in call["result_preview"]


# ---------------------------------------------------------------------------
# 4. Token accumulation from AfterInvocationEvent
# ---------------------------------------------------------------------------


class TestTokenAccumulation:
    """_on_after_invocation extracts accumulated token usage from AgentResult."""

    def test_tokens_accumulated_from_result_metrics(self, plugin):
        event = MagicMock()
        event.result = SimpleNamespace(
            metrics=SimpleNamespace(
                accumulated_usage={
                    "inputTokens": 1000,
                    "outputTokens": 500,
                    "cacheReadInputTokens": 200,
                }
            ),
            stop_reason="end_turn",
        )
        plugin._on_after_invocation(event)

        assert plugin._total_input_tokens == 1000
        assert plugin._total_output_tokens == 500
        assert plugin._total_cache_read_tokens == 200

    def test_tokens_default_to_zero_when_no_metrics(self, plugin):
        event = MagicMock()
        # Use a plain object without a metrics attribute
        result = SimpleNamespace(stop_reason="end_turn")
        assert not hasattr(result, "metrics")
        event.result = result
        plugin._on_after_invocation(event)

        assert plugin._total_input_tokens == 0
        assert plugin._total_output_tokens == 0

    def test_error_stop_reason_captured(self, plugin):
        event = MagicMock()
        event.result = SimpleNamespace(
            metrics=SimpleNamespace(accumulated_usage={}),
            stop_reason="content_filtered",
        )
        plugin._on_after_invocation(event)

        assert plugin._error == "stop_reason=content_filtered"

    def test_end_time_set_on_after_invocation(self, plugin):
        plugin._on_before_invocation(MagicMock())
        assert plugin._start_time is not None

        event = MagicMock()
        event.result = None
        plugin._on_after_invocation(event)

        assert plugin._end_time is not None
        assert plugin._end_time >= plugin._start_time


# ---------------------------------------------------------------------------
# 5. Cost calculation (estimate_cost)
# ---------------------------------------------------------------------------


class TestEstimateCost:
    """estimate_cost function with different models."""

    def test_cost_with_default_pricing(self, mod):
        # Default: $0.003/1K input, $0.015/1K output
        cost = mod.estimate_cost(1000, 1000, model_id="unknown-model")
        expected = Decimal("0.003") + Decimal("0.015")
        assert cost == expected

    def test_cost_with_claude_sonnet_pricing(self, mod):
        cost = mod.estimate_cost(1000, 1000, model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        expected = Decimal("0.003") + Decimal("0.015")
        assert cost == expected

    def test_cost_with_nova_pro_pricing(self, mod):
        cost = mod.estimate_cost(1000, 1000, model_id="amazon.nova-pro-v1:0")
        expected = Decimal("0.0008") + Decimal("0.0032")
        assert cost == expected

    def test_cost_with_nova_micro_pricing(self, mod):
        cost = mod.estimate_cost(1000, 1000, model_id="amazon.nova-micro-v1:0")
        expected = Decimal("0.000035") + Decimal("0.00014")
        assert cost == expected

    def test_cost_zero_tokens(self, mod):
        cost = mod.estimate_cost(0, 0)
        assert cost == Decimal("0")

    def test_cost_scales_linearly(self, mod):
        cost_1k = mod.estimate_cost(1000, 0)
        cost_2k = mod.estimate_cost(2000, 0)
        assert cost_2k == cost_1k * 2


# ---------------------------------------------------------------------------
# 6. DynamoDB write in flush()
# ---------------------------------------------------------------------------


class TestFlush:
    """flush() writes trace record to DynamoDB."""

    def test_flush_writes_correct_item(self, mod, plugin):
        mock_table = MagicMock()
        mod._traces_table = mock_table

        plugin._on_before_invocation(MagicMock())
        plugin._total_input_tokens = 500
        plugin._total_output_tokens = 200
        plugin._model_id = "claude-sonnet-4-5"

        plugin.flush()

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["run_id"] == "run-001"
        assert item["step_name"] == "code_generation"
        assert item["pipeline_id"] == "pipe-001"
        assert "trace_data" in item
        assert "timestamp" in item
        assert "ttl" in item

    def test_flush_trace_data_contains_expected_fields(self, mod, plugin):
        mock_table = MagicMock()
        mod._traces_table = mock_table

        plugin._total_input_tokens = 100
        plugin._total_output_tokens = 50
        plugin.flush()

        item = mock_table.put_item.call_args[1]["Item"]
        trace = item["trace_data"]
        assert "model_calls" in trace
        assert "tool_calls" in trace
        assert "total_input_tokens" in trace
        assert "total_output_tokens" in trace
        assert "estimated_cost_usd" in trace

    def test_flush_ttl_is_approximately_90_days(self, mod, plugin):
        mock_table = MagicMock()
        mod._traces_table = mock_table

        before = int(time.time())
        plugin.flush()
        after = int(time.time())

        item = mock_table.put_item.call_args[1]["Item"]
        ttl = item["ttl"]
        ninety_days = 90 * 86_400
        assert before + ninety_days <= ttl <= after + ninety_days


# ---------------------------------------------------------------------------
# 7. Truncation behavior (_truncate and _safe_json)
# ---------------------------------------------------------------------------


class TestTruncation:
    """_truncate and _safe_json limit string length."""

    def test_truncate_short_string_unchanged(self, mod):
        assert mod._truncate("hello", 10) == "hello"

    def test_truncate_long_string_trimmed_with_ellipsis(self, mod):
        result = mod._truncate("a" * 600, 500)
        assert len(result) == 500
        assert result.endswith("…")

    def test_truncate_exact_length_unchanged(self, mod):
        s = "x" * 500
        assert mod._truncate(s, 500) == s

    def test_safe_json_serialises_dict(self, mod):
        result = mod._safe_json({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_safe_json_truncates_large_output(self, mod):
        big = {"data": "x" * 1000}
        result = mod._safe_json(big, 100)
        assert len(result) <= 100
        assert result.endswith("…")

    def test_safe_json_handles_unserializable(self, mod):
        """_safe_json falls back to str() for non-serializable objects."""
        obj = object()
        result = mod._safe_json(obj)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 8. Fire-and-forget: flush() catches exceptions
# ---------------------------------------------------------------------------
#
# Section 8b (p52 task 3.3) extends this class.  The two tests immediately below
# pin the *outer* contract — flush() raises nothing at its caller — and they
# were already true before p52.  What p52 added is a per-field inner guard in
# the redaction loop, and what that guard protects is a different thing:
# without it a raising redactor is caught by flush()'s outer ``except`` and the
# ENTIRE trace row is never written.  The caller stays safe either way; the
# diagnostic does not.  Section 8b's assertions are therefore about the row
# existing and about degradation being per field, not about "flush did not
# raise" — that weaker claim passes on the broken version.
#
# ---------------------------------------------------------------------------
# 8b helpers — building Preview_Fields the two hook builders really produce
# ---------------------------------------------------------------------------

#: A value that must never reach a persisted item (R5.1, R7.3).  Borrowed from
#: p52 gate 1.2's render, where this fragment was the one that leaked.
_SECRET_VALUE = "APT-4-SECRET"

#: The two producers' rendered Sample_Block shapes.  Cited by name as well as
#: by line, because p52 gate 1.2 found three line cites in this area stale:
#: ``render_sample_values_section`` (schema_mapping/lambda_function.py:1013) and
#: ``build_evidence_prompt`` (agentcore/complex_mapping_agent/main.py:275).
_SCHEMA_MAPPING_SHAPE = "schema_mapping"
_COMPLEX_MAPPING_SHAPE = "complex_mapping"

#: Which Preview_Fields each entry kind actually carries.  ``_on_after_model``
#: builds the first pair, ``_on_after_tool`` the second; the redaction loop
#: iterates all four over every entry and ``continue``s on the absent ones, so a
#: raise on ``tool_params`` is only reachable through a tool-call entry.
#: ``TestFlushSurvivesARaisingRedactor.test_the_two_entry_kinds_cover_every_preview_field``
#: pins that these two together are still the whole of ``_PREVIEW_FIELDS``.
_MODEL_ENTRY_FIELDS = ("prompt_preview", "response_preview")
_TOOL_ENTRY_FIELDS = ("tool_params", "result_preview")


def _preview_text(tag: str, shape: str = _SCHEMA_MAPPING_SHAPE) -> str:
    """A Preview_Field carrying one Sample_Block, tagged for the fake redactor.

    The tag sits on its own line *before* the marker, so a successful redaction
    leaves it in place — marker-to-end-of-line only rewrites the marker's line.
    That is what lets a test tell "this field went through the real redactor"
    apart from "this field was replaced wholesale by the placeholder".

    The secret sits *inside* the Sample_Block, so it is exactly what redaction
    is supposed to remove and what the placeholder must not reproduce.
    """
    values = f'"1 Main St {_SECRET_VALUE}", "Seattle"'
    if shape == _COMPLEX_MAPPING_SHAPE:
        block = f"  - addr_1 (type: string)\n    Sample values: [{values}]\n"
    else:
        block = f"  - addr_1: Sample values: [{values}]\n"
    return f"trigger={tag}\n{block}"


def _model_entry(tag_prefix: str, shape: str = _SCHEMA_MAPPING_SHAPE) -> dict:
    """A model-call entry in the shape ``_on_after_model`` appends.

    Appending by hand is not a shortcut here: it is also a live production path.
    ``schema_mapping/lambda_function.py:2125`` appends to ``_model_calls``
    directly because its Converse call is not a strands agent and no hook fires
    for it (R5.4) — the reason redaction lives at ``flush()`` and not in the
    hook.
    """
    return {
        "model_id": "claude-sonnet-4-5",
        "duration_ms": 12.3,
        "prompt_preview": _preview_text(f"{tag_prefix}-prompt_preview", shape),
        "response_preview": _preview_text(f"{tag_prefix}-response_preview", shape),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
    }


def _tool_entry(tag_prefix: str, shape: str = _SCHEMA_MAPPING_SHAPE) -> dict:
    """A tool-call entry in the shape ``_on_after_tool`` appends."""
    return {
        "tool_name": "read_sample_data",
        "tool_params": _preview_text(f"{tag_prefix}-tool_params", shape),
        "duration_ms": 4.5,
        "status": "success",
        "result_preview": _preview_text(f"{tag_prefix}-result_preview", shape),
    }


def _redactor_raising_on(real, raise_tags, exc_type=RuntimeError):
    """Wrap *real* so it raises for the fields tagged in *raise_tags*.

    Selection has to be keyed on the text, because the redactor is called with
    nothing else — ``flush()`` passes it one string.  Every other field is
    handed to the **real** redactor, so "the other fields are still redacted
    normally" is asserted against real behaviour rather than against a stub.
    """

    def fake(text: str) -> str:
        for tag in raise_tags:
            if f"trigger={tag}\n" in text:
                raise exc_type(f"redactor exploded on {tag}")
        return real(text)

    return fake


def _flush_with_raising_redactor(
    mod,
    plugin,
    *,
    model_tags=(),
    tool_tags=(),
    raise_tags=(),
    exc_type=RuntimeError,
    shape=_SCHEMA_MAPPING_SHAPE,
):
    """Append entries, make the redactor raise for *raise_tags*, then flush.

    ``flush()`` is called bare and outside any ``pytest.raises``: if it raises,
    the test fails on the exception, which is R7.1.

    Returns ``(mock_table, warning_mock, exception_mock)``.  ``logger.exception``
    is captured as well as ``logger.warning`` because it is the tell that
    distinguishes the two failure modes — ``exception`` firing means flush()'s
    outer handler caught something and the row was lost.
    """
    real = mod._redact_sample_values
    for tag in model_tags:
        plugin._model_calls.append(_model_entry(tag, shape))
    for tag in tool_tags:
        plugin._tool_calls.append(_tool_entry(tag, shape))

    mock_table = MagicMock()
    mod._traces_table = mock_table

    with (
        patch.object(mod, "_redact_sample_values", _redactor_raising_on(real, raise_tags, exc_type)),
        patch.object(mod.logger, "warning") as warning,
        patch.object(mod.logger, "exception") as exception,
    ):
        plugin.flush()

    return mock_table, warning, exception


@st.composite
def _degradation_plan(draw):
    """A set of (entry, field) pairs that raise, plus the shape and exception type.

    This is the one axis worth generating — see the note above section 8b's
    tests.  The raising set is drawn as a subset of every (entry, field) pair
    the entries actually carry, and the empty subset is allowed on purpose: it
    asserts the guard is inert when nothing raises.

    Tag prefixes are hyphen-free (``m0``, ``t1``) because the WARNING assertion
    recovers the field name with ``tag.split("-", 1)[1]``.
    """
    model_tags = tuple(f"m{index}" for index in range(draw(st.integers(min_value=1, max_value=3))))
    tool_tags = tuple(f"t{index}" for index in range(draw(st.integers(min_value=0, max_value=3))))

    pairs = [f"{prefix}-{field}" for prefix in model_tags for field in _MODEL_ENTRY_FIELDS]
    pairs += [f"{prefix}-{field}" for prefix in tool_tags for field in _TOOL_ENTRY_FIELDS]

    raise_tags = tuple(draw(st.lists(st.sampled_from(pairs), unique=True, max_size=len(pairs))))

    return {
        "model_tags": model_tags,
        "tool_tags": tool_tags,
        "raise_tags": raise_tags,
        "shape": draw(st.sampled_from((_SCHEMA_MAPPING_SHAPE, _COMPLEX_MAPPING_SHAPE))),
        "exc_type": draw(st.sampled_from((RuntimeError, ValueError, TypeError, KeyError, ZeroDivisionError))),
    }


def _assert_row_written_once_and_degraded_per_field(
    mod,
    plugin,
    mock_table,
    warning,
    exception,
    *,
    model_tags=(),
    tool_tags=(),
    raise_tags=(),
    exc_type=RuntimeError,
    shape=_SCHEMA_MAPPING_SHAPE,
):
    """The load-bearing assertions for Property 7.

    In order of what each one catches:

    1. ``put_item`` called **exactly once** — the row was written.  Drop the
       inner guard and this is what fails; a test asserting only "flush did not
       raise" passes on that broken version.
    2. ``logger.exception`` never called — the outer handler did not fire, so
       the row was not lost on the way out.
    3. Every raising field is **exactly** the fixed placeholder, and every
       non-raising field is **exactly** what the real redactor returns.  This is
       the per-field claim: degradation is scoped to the field that raised, not
       to the entry and not to the row (do-NOT 10).
    4. The secret appears nowhere in the serialised item — R7.3's "no
       source-derived text", checked against the whole row rather than against
       the placeholder alone, so a leak into any other field also fails here.
    5. One WARNING per raising field, carrying run_id, step_name, field and the
       exception type name — and no source-derived text (R7.4).  WARNING and not
       INFO per ``LGR-0fe3e5d-2``'s reasoning: a redaction failure leaves no
       other trace, and WARNING survives an INFO-filtered log view.
    """
    mock_table.put_item.assert_called_once()
    assert not exception.called, (
        "flush() reached its outer except handler, which means the trace row was lost — "
        "this is exactly the failure the per-field inner guard exists to prevent"
    )

    item = mock_table.put_item.call_args[1]["Item"]
    trace = item["trace_data"]
    assert len(trace["model_calls"]) == len(model_tags)
    assert len(trace["tool_calls"]) == len(tool_tags)

    real = mod._redact_sample_values
    for entries, tags, fields in (
        (trace["model_calls"], model_tags, _MODEL_ENTRY_FIELDS),
        (trace["tool_calls"], tool_tags, _TOOL_ENTRY_FIELDS),
    ):
        for entry, tag_prefix in zip(entries, tags, strict=True):
            for field in fields:
                tag = f"{tag_prefix}-{field}"
                persisted = entry[field]
                if tag in raise_tags:
                    assert persisted == mod._REDACTION_FAILED, (
                        f"{tag} raised, so it should be the fixed placeholder; got {persisted!r}"
                    )
                else:
                    assert persisted == real(_preview_text(tag, shape)), (
                        f"{tag} did not raise, so it should be redacted normally; got {persisted!r}"
                    )
                    assert "values redacted" in persisted, f"{tag} lost its redaction summary: {persisted!r}"

    serialised = json.dumps(item, default=str)
    assert _SECRET_VALUE not in serialised, "a sampled value reached the persisted trace row"

    assert warning.call_count == len(raise_tags), (
        f"expected one WARNING per degraded field ({len(raise_tags)}), got {warning.call_count}"
    )
    named_fields = []
    for call in warning.call_args_list:
        # The call is %-style with positional args, so render it the way the
        # logger would and assert about the message that would actually be
        # emitted rather than about the format string.
        rendered = call.args[0] % call.args[1:]
        assert plugin.run_id in rendered, f"WARNING omitted the run id: {rendered!r}"
        assert plugin.step_name in rendered, f"WARNING omitted the step name: {rendered!r}"
        assert exc_type.__name__ in rendered, f"WARNING omitted the exception type: {rendered!r}"
        assert _SECRET_VALUE not in rendered, "the WARNING carried source-derived text"
        named_fields.append(call.args[3])

    expected_fields = sorted(tag.split("-", 1)[1] for tag in raise_tags)
    assert sorted(named_fields) == expected_fields, (
        f"WARNINGs named {sorted(named_fields)}, expected one per degraded field: {expected_fields}"
    )


class TestFlushFireAndForget:
    """flush() catches exceptions and logs them."""

    def test_flush_catches_dynamodb_exception(self, mod, plugin):
        mock_table = MagicMock()
        mock_table.put_item.side_effect = Exception("DynamoDB down")
        mod._traces_table = mock_table

        with patch.object(mod.logger, "exception") as mock_log:
            # Should NOT raise
            plugin.flush()
            mock_log.assert_called_once()
            assert "Failed to write agent trace" in mock_log.call_args[0][0]

    def test_flush_does_not_propagate_error(self, mod, plugin):
        mock_table = MagicMock()
        mock_table.put_item.side_effect = RuntimeError("network error")
        mod._traces_table = mock_table

        # Must not raise
        plugin.flush()

    # -----------------------------------------------------------------------
    # 8b. Property 7: a trace is always written and never raises (p52 task 3.3)
    #
    # **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    #
    # FORM: mostly ``parametrize``, with one generated arm — and that split is
    # deliberate rather than a property test dressed down.
    #
    # ``flush()``'s redaction loop branches on **structure**, not on content: it
    # picks the four ``_PREVIEW_FIELDS`` off each entry and skips whatever is not
    # a ``str``.  Nothing in it reads the text.  So generating Preview_Field
    # *content* here would re-run one code path with different strings — and the
    # content dimension is already owned by
    # ``test_trace_sample_redaction_prop.py`` (properties 4, 5 and 6), which
    # drives the real producers.
    #
    # There is a second reason the design's "*for any* Preview_Field content,
    # including content that makes the Trace_Redactor raise" cannot be taken
    # literally as a generator: no known input makes the real redactor raise.
    # It is pure, marker-keyed and has no failure mode we can reach from the
    # alphabet.  The raise has to be *injected*, which means the interesting
    # axis is **which fields raise**, not what they contain.
    #
    # That axis is where generation does earn its place, so it gets the one
    # ``@given`` arm.  The raising set is a subset of (entry x field) pairs, and
    # with one to three entries of each kind the lattice is far too large to
    # enumerate: the parametrized cases below name the ones worth naming (each
    # field independently, partial within one entry, across both lists, all of
    # them at once, none of them) and the property covers the rest of the
    # lattice, including the mixed cases nobody thought to name.
    #
    # WHY ``put_item`` COUNT AND NOT "DID NOT RAISE" — measured, not assumed.
    # A throwaway probe loaded this module's source, stripped the inner
    # try/except out of flush()'s redaction loop, exec'd it as a separate module
    # (the file itself was never touched) and drove the same flow.  On that
    # guard-stripped version, with one field's redactor raising:
    #
    #     flush() raised to caller ...... None      <- "does not raise" PASSES
    #     put_item call_count ........... 0         <- the row is GONE
    #     logger.exception call_count ... 1
    #
    # Against the real module, same input: no raise, ``put_item`` once,
    # ``logger.exception`` never, one field on the placeholder and the other
    # three really redacted.  So the ``put_item.assert_called_once()`` and the
    # ``logger.exception`` check below are the two assertions that actually
    # separate the fixed version from the broken one.
    # -----------------------------------------------------------------------

    def test_the_covered_fields_and_the_placeholder_match_the_module(self, mod):
        """The parametrization and the placeholder cannot drift from the module.

        Two drift guards in one place.  ``_MODEL_ENTRY_FIELDS`` and
        ``_TOOL_ENTRY_FIELDS`` are this file's split of ``_PREVIEW_FIELDS`` by
        entry kind; if a fifth Preview_Field is added to the module, the
        parametrized cases below would silently stop covering it.  And the
        placeholder is read off the module rather than retyped, so this pins that
        the collection-time import and the per-test reimport agree.
        """
        assert set(_MODEL_ENTRY_FIELDS) | set(_TOOL_ENTRY_FIELDS) == set(_PREVIEW_FIELDS), (
            "a Preview_Field exists that section 8b does not place in either entry kind"
        )
        assert not set(_MODEL_ENTRY_FIELDS) & set(_TOOL_ENTRY_FIELDS)
        assert mod._PREVIEW_FIELDS == _PREVIEW_FIELDS
        assert mod._REDACTION_FAILED == _REDACTION_FAILED
        assert _SECRET_VALUE not in _REDACTION_FAILED, "R7.3: the placeholder must be free of source-derived text"

    @pytest.mark.parametrize("field", _PREVIEW_FIELDS)
    def test_a_raising_redactor_degrades_only_that_field(self, mod, plugin, field):
        """One field raises; the row is still written and nothing else degrades.

        Both entry kinds are present in every case, so the non-raising fields
        include the **sibling field on the same entry** (degradation is per
        field, not per entry) and both fields of the **other entry** (not per
        row).  ``tool_params`` and ``result_preview`` are only reachable through
        a tool-call entry — the loop ``continue``s on a model entry, which
        carries neither.
        """
        tag_prefix = "m0" if field in _MODEL_ENTRY_FIELDS else "t0"
        tag = f"{tag_prefix}-{field}"

        mock_table, warning, exception = _flush_with_raising_redactor(
            mod, plugin, model_tags=("m0",), tool_tags=("t0",), raise_tags=(tag,)
        )
        _assert_row_written_once_and_degraded_per_field(
            mod, plugin, mock_table, warning, exception, model_tags=("m0",), tool_tags=("t0",), raise_tags=(tag,)
        )

    def test_a_raise_in_each_list_degrades_only_those_fields(self, mod, plugin):
        """Raises in both ``_model_calls`` and ``_tool_calls``, one row still written.

        A second model entry raises on nothing, so the case also pins that an
        untouched entry is redacted normally while its neighbour degrades.
        """
        raise_tags = ("m0-prompt_preview", "t0-result_preview")

        mock_table, warning, exception = _flush_with_raising_redactor(
            mod, plugin, model_tags=("m0", "m1"), tool_tags=("t0",), raise_tags=raise_tags
        )
        _assert_row_written_once_and_degraded_per_field(
            mod,
            plugin,
            mock_table,
            warning,
            exception,
            model_tags=("m0", "m1"),
            tool_tags=("t0",),
            raise_tags=raise_tags,
        )

    def test_every_preview_field_raising_still_writes_the_row(self, mod, plugin):
        """The worst case: nothing can be redacted, and the row survives anyway.

        This is the case that separates the two contracts most sharply.  Without
        the per-field inner guard the first raise reaches ``flush()``'s outer
        ``except``, the caller is still unaffected — and there is no trace row at
        all.
        """
        model_tags = ("m0",)
        tool_tags = ("t0",)
        raise_tags = tuple(
            [f"m0-{field}" for field in _MODEL_ENTRY_FIELDS] + [f"t0-{field}" for field in _TOOL_ENTRY_FIELDS]
        )

        mock_table, warning, exception = _flush_with_raising_redactor(
            mod, plugin, model_tags=model_tags, tool_tags=tool_tags, raise_tags=raise_tags
        )
        _assert_row_written_once_and_degraded_per_field(
            mod,
            plugin,
            mock_table,
            warning,
            exception,
            model_tags=model_tags,
            tool_tags=tool_tags,
            raise_tags=raise_tags,
        )

        item = mock_table.put_item.call_args[1]["Item"]
        persisted = [entry[field] for entry in item["trace_data"]["model_calls"] for field in _MODEL_ENTRY_FIELDS]
        persisted += [entry[field] for entry in item["trace_data"]["tool_calls"] for field in _TOOL_ENTRY_FIELDS]
        assert persisted == [_REDACTION_FAILED] * 4

    def test_no_raise_leaves_every_field_redacted_normally(self, mod, plugin):
        """The contrast case: with nothing raising, no field carries the placeholder.

        Without this, every assertion above would still pass if the placeholder
        were substituted unconditionally.
        """
        mock_table, warning, exception = _flush_with_raising_redactor(
            mod, plugin, model_tags=("m0",), tool_tags=("t0",), raise_tags=()
        )
        _assert_row_written_once_and_degraded_per_field(
            mod, plugin, mock_table, warning, exception, model_tags=("m0",), tool_tags=("t0",), raise_tags=()
        )

        assert not warning.called
        assert _REDACTION_FAILED not in json.dumps(mock_table.put_item.call_args[1]["Item"], default=str)

    @pytest.mark.parametrize(
        "exc_type", [RuntimeError, ValueError, TypeError, KeyError, AttributeError, ZeroDivisionError]
    )
    def test_exception_type_does_not_change_the_outcome(self, mod, plugin, exc_type):
        """The guard catches ``Exception``, so the type only shows up in the log."""
        mock_table, warning, exception = _flush_with_raising_redactor(
            mod,
            plugin,
            model_tags=("m0",),
            raise_tags=("m0-prompt_preview",),
            exc_type=exc_type,
        )
        _assert_row_written_once_and_degraded_per_field(
            mod,
            plugin,
            mock_table,
            warning,
            exception,
            model_tags=("m0",),
            raise_tags=("m0-prompt_preview",),
            exc_type=exc_type,
        )

    @given(plan=_degradation_plan())
    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_any_subset_of_raising_fields_still_writes_exactly_one_row(self, mod, plan):
        """Property 7, over the (entry x field) subset lattice.

        **Validates: Requirements 7.1, 7.2, 7.3, 7.4**

        For any set of Preview_Fields that make the redactor raise — none of
        them, all of them, or any mixture across any number of model-call and
        tool-call entries, in either producer's rendered shape — ``flush()``
        returns normally, writes **exactly one** item, degrades exactly the
        fields that raised, and leaves every other field really redacted.

        ``mod`` is function-scoped and therefore health-check-suppressed: it is
        used only to reach the module, and every piece of mutable state (the
        plugin, the mock table, the patched redactor) is rebuilt inside each
        example, so nothing carries across.
        """
        plugin = mod.AgentTracePlugin(pipeline_id="pipe-001", run_id="run-001", step_name="code_generation")

        mock_table, warning, exception = _flush_with_raising_redactor(mod, plugin, **plan)
        _assert_row_written_once_and_degraded_per_field(mod, plugin, mock_table, warning, exception, **plan)


# ---------------------------------------------------------------------------
# 9. _lookup_pricing returns correct pricing
# ---------------------------------------------------------------------------


class TestLookupPricing:
    """_lookup_pricing returns correct pricing for known models and falls back."""

    def test_known_model_claude_sonnet(self, mod):
        pricing = mod._lookup_pricing("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        assert pricing["input_per_1k"] == Decimal("0.003")
        assert pricing["output_per_1k"] == Decimal("0.015")

    def test_known_model_nova_pro(self, mod):
        pricing = mod._lookup_pricing("amazon.nova-pro-v1:0")
        assert pricing["input_per_1k"] == Decimal("0.0008")

    def test_known_model_nova_micro(self, mod):
        pricing = mod._lookup_pricing("amazon.nova-micro-v1:0")
        assert pricing["input_per_1k"] == Decimal("0.000035")

    def test_unknown_model_falls_back_to_default(self, mod):
        pricing = mod._lookup_pricing("some-unknown-model-v99")
        assert pricing == mod.MODEL_PRICING["default"]

    def test_case_insensitive_lookup(self, mod):
        pricing = mod._lookup_pricing("CLAUDE-SONNET-4-5-BIG")
        assert pricing["input_per_1k"] == Decimal("0.003")
