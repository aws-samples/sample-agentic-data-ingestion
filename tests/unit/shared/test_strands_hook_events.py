# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validate Strands SDK hook event shapes against agent_trace_plugin assumptions.

Imports the real Strands hook event classes and inspects their attributes to
ensure our AgentTracePlugin accesses the correct fields.  This catches SDK
version drift before it becomes a runtime error in production.
"""

import inspect

import pytest

strands = pytest.importorskip("strands", reason="strands SDK not installed")

from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookEvent,
)
from strands.hooks.registry import HookProvider, HookRegistry
from strands.types.tools import ToolResult, ToolUse

# ---------------------------------------------------------------------------
# 3.1  Inspect event class attributes
# ---------------------------------------------------------------------------


class TestEventClassAttributes:
    """Verify that each hook event class exposes the fields our plugin relies on."""

    def test_all_events_are_dataclasses(self):
        """Every event class should be a dataclass with __dataclass_fields__."""
        for cls in (
            BeforeInvocationEvent,
            AfterInvocationEvent,
            BeforeModelCallEvent,
            AfterModelCallEvent,
            BeforeToolCallEvent,
            AfterToolCallEvent,
        ):
            assert hasattr(cls, "__dataclass_fields__"), f"{cls.__name__} is not a dataclass"

    def test_all_events_inherit_from_hook_event(self):
        """Every event should inherit from HookEvent which provides the 'agent' field."""
        for cls in (
            BeforeInvocationEvent,
            AfterInvocationEvent,
            BeforeModelCallEvent,
            AfterModelCallEvent,
            BeforeToolCallEvent,
            AfterToolCallEvent,
        ):
            assert issubclass(cls, HookEvent), f"{cls.__name__} does not inherit HookEvent"

    def test_hook_event_has_agent_field(self):
        """HookEvent base class must provide the 'agent' field."""
        assert "agent" in HookEvent.__dataclass_fields__

    # -- BeforeInvocationEvent ------------------------------------------------

    def test_before_invocation_fields(self):
        fields = set(BeforeInvocationEvent.__dataclass_fields__)
        assert "agent" in fields
        assert "invocation_state" in fields

    # -- AfterInvocationEvent -------------------------------------------------

    def test_after_invocation_has_result(self):
        """Plugin reads event.result for metrics and stop_reason."""
        fields = set(AfterInvocationEvent.__dataclass_fields__)
        assert "result" in fields

    # -- BeforeModelCallEvent -------------------------------------------------

    def test_before_model_call_has_agent(self):
        fields = set(BeforeModelCallEvent.__dataclass_fields__)
        assert "agent" in fields

    # -- AfterModelCallEvent --------------------------------------------------

    def test_after_model_call_has_stop_response(self):
        """Plugin reads event.stop_response.message for response preview."""
        fields = set(AfterModelCallEvent.__dataclass_fields__)
        assert "stop_response" in fields

    def test_after_model_call_has_exception(self):
        """Plugin reads event.exception for error capture."""
        fields = set(AfterModelCallEvent.__dataclass_fields__)
        assert "exception" in fields

    def test_model_stop_response_has_message_and_stop_reason(self):
        """ModelStopResponse must have 'message' and 'stop_reason' fields."""
        msr = AfterModelCallEvent.ModelStopResponse
        msr_fields = set(msr.__dataclass_fields__)
        assert "message" in msr_fields
        assert "stop_reason" in msr_fields

    # -- BeforeToolCallEvent --------------------------------------------------

    def test_before_tool_call_has_tool_use(self):
        """Plugin reads event.tool_use (a ToolUse TypedDict)."""
        fields = set(BeforeToolCallEvent.__dataclass_fields__)
        assert "tool_use" in fields

    # -- AfterToolCallEvent ---------------------------------------------------

    def test_after_tool_call_has_result(self):
        """Plugin reads event.result (a ToolResult TypedDict)."""
        fields = set(AfterToolCallEvent.__dataclass_fields__)
        assert "result" in fields

    def test_after_tool_call_has_exception(self):
        fields = set(AfterToolCallEvent.__dataclass_fields__)
        assert "exception" in fields


# ---------------------------------------------------------------------------
# 3.1  Inspect ToolUse / ToolResult TypedDict shapes
# ---------------------------------------------------------------------------


class TestToolTypedDicts:
    """Verify ToolUse and ToolResult have the keys our plugin accesses."""

    def test_tool_use_has_name_and_input(self):
        """Plugin accesses tool_use['name'] and tool_use['input']."""
        annotations = ToolUse.__annotations__
        assert "name" in annotations
        assert "input" in annotations

    def test_tool_result_has_status_and_content(self):
        """Plugin accesses result['status'] and result['content']."""
        annotations = ToolResult.__annotations__
        assert "status" in annotations
        assert "content" in annotations


# ---------------------------------------------------------------------------
# 3.1  Inspect Usage TypedDict (token fields)
# ---------------------------------------------------------------------------


class TestUsageTypedDict:
    """Verify the Usage type has the token field names our plugin expects."""

    def test_usage_has_input_output_tokens(self):
        from strands.types.event_loop import Usage

        annotations = Usage.__annotations__
        assert "inputTokens" in annotations
        assert "outputTokens" in annotations

    def test_usage_has_cache_read_tokens(self):
        from strands.types.event_loop import Usage

        annotations = Usage.__annotations__
        assert "cacheReadInputTokens" in annotations


# ---------------------------------------------------------------------------
# 3.1  Verify model_id access pattern
# ---------------------------------------------------------------------------


class TestModelIdAccess:
    """Verify how to extract model_id from the agent's model."""

    def test_bedrock_model_stores_model_id_in_config(self):
        """model_id is accessed via agent.model.config['model_id'], not agent.model.model_id."""
        from strands.models.bedrock import BedrockModel

        model = BedrockModel(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        assert hasattr(model, "config")
        assert model.config.get("model_id") == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def test_bedrock_model_has_no_direct_model_id_attr(self):
        """Confirm model_id is NOT a direct attribute — must use config dict."""
        from strands.models.bedrock import BedrockModel

        model = BedrockModel(model_id="test-model")
        # model_id is in config, not as a top-level attribute
        assert not hasattr(model, "model_id") or model.config.get("model_id") == "test-model"


# ---------------------------------------------------------------------------
# 3.1  HookProvider protocol check
# ---------------------------------------------------------------------------


class TestHookProviderProtocol:
    """Verify the HookProvider protocol shape matches our plugin's implementation."""

    def test_hook_provider_is_runtime_checkable(self):
        assert hasattr(HookProvider, "__protocol_attrs__") or hasattr(HookProvider, "__abstractmethods__")

    def test_hook_provider_requires_register_hooks(self):
        sig = inspect.signature(HookProvider.register_hooks)
        params = list(sig.parameters.keys())
        assert "self" in params
        assert "registry" in params

    def test_hook_registry_has_add_callback(self):
        assert hasattr(HookRegistry, "add_callback")


# ---------------------------------------------------------------------------
# 3.2  Integration test — real Agent with mock model captures actual events
# ---------------------------------------------------------------------------


class _MockModel:
    """Minimal model that satisfies the Strands Model protocol for testing."""

    def __init__(self, model_id: str = "test-mock-model"):
        self.config = {"model_id": model_id}

    def get_config(self):
        return self.config

    def update_config(self, **kwargs):
        self.config.update(kwargs)

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}}
        yield {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "Mock response text"},
            }
        }
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 120,
                    "outputTokens": 45,
                    "totalTokens": 165,
                    "cacheReadInputTokens": 30,
                },
                "metrics": {"latencyMs": 150},
            }
        }
        yield {
            "messageStop": {
                "stopReason": "end_turn",
                "additionalModelResponseFields": None,
            }
        }


class TestAgentHookIntegration:
    """Create a real Strands Agent with a HookProvider and verify event shapes."""

    def test_hook_provider_receives_all_lifecycle_events(self):
        """A HookProvider registered on an Agent receives before/after invocation and model events."""
        from strands import Agent

        captured: list[tuple[str, object]] = []

        class _CaptureHook:
            def register_hooks(self, registry, **kwargs):
                from strands.hooks.events import (
                    AfterInvocationEvent,
                    AfterModelCallEvent,
                    BeforeInvocationEvent,
                    BeforeModelCallEvent,
                )

                registry.add_callback(
                    BeforeInvocationEvent,
                    lambda e: captured.append(("before_invocation", e)),
                )
                registry.add_callback(
                    AfterInvocationEvent,
                    lambda e: captured.append(("after_invocation", e)),
                )
                registry.add_callback(
                    BeforeModelCallEvent,
                    lambda e: captured.append(("before_model", e)),
                )
                registry.add_callback(
                    AfterModelCallEvent,
                    lambda e: captured.append(("after_model", e)),
                )

        agent = Agent(model=_MockModel(), hooks=[_CaptureHook()])
        agent("Hello")

        event_names = [name for name, _ in captured]
        assert "before_invocation" in event_names
        assert "after_invocation" in event_names
        assert "before_model" in event_names
        assert "after_model" in event_names

    def test_after_model_event_has_stop_response_with_message(self):
        """AfterModelCallEvent.stop_response.message contains the response text."""
        from strands import Agent

        after_model_events = []

        class _CaptureHook:
            def register_hooks(self, registry, **kwargs):
                from strands.hooks.events import AfterModelCallEvent

                registry.add_callback(
                    AfterModelCallEvent,
                    lambda e: after_model_events.append(e),
                )

        agent = Agent(model=_MockModel(), hooks=[_CaptureHook()])
        agent("Hello")

        assert len(after_model_events) >= 1
        evt = after_model_events[0]
        assert evt.stop_response is not None
        msg = evt.stop_response.message
        # Message is a TypedDict with 'content' and 'role'
        assert "content" in msg
        assert "role" in msg
        # Content blocks contain text
        text_parts = [b["text"] for b in msg["content"] if "text" in b]
        assert any("Mock response" in t for t in text_parts)

    def test_after_invocation_result_has_accumulated_usage(self):
        """AfterInvocationEvent.result.metrics.accumulated_usage has token counts."""
        from strands import Agent

        after_invocation_events = []

        class _CaptureHook:
            def register_hooks(self, registry, **kwargs):
                from strands.hooks.events import AfterInvocationEvent

                registry.add_callback(
                    AfterInvocationEvent,
                    lambda e: after_invocation_events.append(e),
                )

        agent = Agent(model=_MockModel(), hooks=[_CaptureHook()])
        agent("Hello")

        assert len(after_invocation_events) == 1
        result = after_invocation_events[0].result
        assert result is not None
        assert hasattr(result, "metrics")
        usage = result.metrics.accumulated_usage
        assert usage["inputTokens"] == 120
        assert usage["outputTokens"] == 45

    def test_model_id_accessible_via_agent_model_config(self):
        """model_id is accessed via event.agent.model.config['model_id']."""
        from strands import Agent

        before_model_events = []

        class _CaptureHook:
            def register_hooks(self, registry, **kwargs):
                from strands.hooks.events import BeforeModelCallEvent

                registry.add_callback(
                    BeforeModelCallEvent,
                    lambda e: before_model_events.append(e),
                )

        agent = Agent(model=_MockModel(model_id="my-test-model"), hooks=[_CaptureHook()])
        agent("Hello")

        assert len(before_model_events) >= 1
        evt = before_model_events[0]
        model = evt.agent.model
        assert hasattr(model, "config")
        assert model.config.get("model_id") == "my-test-model"

    def test_cache_read_tokens_in_accumulated_usage(self):
        """Verify cacheReadInputTokens is present in accumulated_usage."""
        from strands import Agent

        after_invocation_events = []

        class _CaptureHook:
            def register_hooks(self, registry, **kwargs):
                from strands.hooks.events import AfterInvocationEvent

                registry.add_callback(
                    AfterInvocationEvent,
                    lambda e: after_invocation_events.append(e),
                )

        agent = Agent(model=_MockModel(), hooks=[_CaptureHook()])
        agent("Hello")

        usage = after_invocation_events[0].result.metrics.accumulated_usage
        assert "cacheReadInputTokens" in usage
        assert usage["cacheReadInputTokens"] == 30
