# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for strands_mcp_factory tool_allowlist feature (Task 2.13).

Validates:
- _filter_tools_by_allowlist returns only matching tools
- _get_tool_name extracts names from various tool object formats
- Backward compatibility: no allowlist returns all tools
"""

import os
import sys
from unittest.mock import MagicMock

# Disable SigV4 transport before importing (streamable_http_sigv4 is not on test path)
os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))


class TestGetToolName:
    """Tests for _get_tool_name() helper."""

    def _get_fn(self):
        from strands_mcp_factory import _get_tool_name

        return _get_tool_name

    def test_tool_dot_tool_dot_name(self):
        """tool.tool.name pattern (Strands MCPClient format)."""
        get_name = self._get_fn()
        tool = MagicMock()
        tool.tool.name = "manage_aws_glue_jobs"
        assert get_name(tool) == "manage_aws_glue_jobs"

    def test_direct_name_attribute(self):
        """tool.name pattern."""
        get_name = self._get_fn()
        tool = MagicMock(spec=["name"])
        tool.name = "my_tool"
        assert get_name(tool) == "my_tool"

    def test_function_dot_name(self):
        """tool.function.name pattern (OpenAI-style)."""
        get_name = self._get_fn()
        tool = MagicMock(spec=["function"])
        tool.function.name = "func_tool"
        assert get_name(tool) == "func_tool"

    def test_dict_with_name_key(self):
        """Dict tool with 'name' key."""
        get_name = self._get_fn()
        tool = {"name": "dict_tool", "description": "A tool"}
        assert get_name(tool) == "dict_tool"

    def test_fallback_to_class_name(self):
        """Unknown format falls back to class name."""
        get_name = self._get_fn()

        class CustomTool:
            pass

        tool = CustomTool()
        assert get_name(tool) == "CustomTool"


class TestFilterToolsByAllowlist:
    """Tests for _filter_tools_by_allowlist()."""

    def _get_fn(self):
        from strands_mcp_factory import _filter_tools_by_allowlist

        return _filter_tools_by_allowlist

    def _make_tool(self, name):
        """Create a mock tool with tool.tool.name pattern."""
        tool = MagicMock()
        tool.tool.name = name
        return tool

    def test_filters_to_matching_tools_only(self):
        """Only tools in the allowlist should be returned."""
        filter_fn = self._get_fn()
        tools = [self._make_tool(n) for n in ["tool_a", "tool_b", "tool_c", "tool_d"]]
        result = filter_fn(tools, ["tool_a", "tool_c"])
        names = [t.tool.name for t in result]
        assert names == ["tool_a", "tool_c"]

    def test_empty_allowlist_returns_empty(self):
        """Empty allowlist should return no tools."""
        filter_fn = self._get_fn()
        tools = [self._make_tool(n) for n in ["tool_a", "tool_b"]]
        result = filter_fn(tools, [])
        assert result == []

    def test_allowlist_with_nonexistent_tool_returns_partial(self):
        """Allowlist with a tool that doesn't exist returns only matching ones."""
        filter_fn = self._get_fn()
        tools = [self._make_tool(n) for n in ["tool_a", "tool_b"]]
        result = filter_fn(tools, ["tool_a", "tool_nonexistent"])
        names = [t.tool.name for t in result]
        assert names == ["tool_a"]

    def test_preserves_original_order(self):
        """Filtered tools should maintain their original order."""
        filter_fn = self._get_fn()
        tools = [self._make_tool(n) for n in ["z_tool", "a_tool", "m_tool"]]
        result = filter_fn(tools, ["m_tool", "z_tool"])
        names = [t.tool.name for t in result]
        assert names == ["z_tool", "m_tool"]

    def test_all_tools_in_allowlist(self):
        """When all tools are in the allowlist, all should be returned."""
        filter_fn = self._get_fn()
        tools = [self._make_tool(n) for n in ["tool_a", "tool_b"]]
        result = filter_fn(tools, ["tool_a", "tool_b"])
        assert len(result) == 2


class TestInitializeWithAllowlist:
    """Tests for initialize_strands_agent_with_mcp with tool_allowlist parameter."""

    def test_signature_accepts_tool_allowlist(self):
        """The function should accept tool_allowlist as a keyword argument."""
        import inspect

        from strands_mcp_factory import initialize_strands_agent_with_mcp

        sig = inspect.signature(initialize_strands_agent_with_mcp)
        assert "tool_allowlist" in sig.parameters
        param = sig.parameters["tool_allowlist"]
        assert param.default is None


class TestCreateMcpTools:
    """Tests for the standalone create_mcp_tools() function."""

    def test_signature_accepts_tool_allowlist(self):
        """create_mcp_tools should accept endpoint and tool_allowlist."""
        import inspect

        from strands_mcp_factory import create_mcp_tools

        sig = inspect.signature(create_mcp_tools)
        assert "endpoint" in sig.parameters
        assert "tool_allowlist" in sig.parameters
        assert sig.parameters["tool_allowlist"].default is None

    def test_signature_accepts_timeout(self):
        """create_mcp_tools should accept timeout parameter."""
        import inspect

        from strands_mcp_factory import create_mcp_tools

        sig = inspect.signature(create_mcp_tools)
        assert "timeout" in sig.parameters
        assert sig.parameters["timeout"].default == 120
