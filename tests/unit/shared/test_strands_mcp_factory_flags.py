# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for shared/strands_mcp_factory.py MCP_SIGV4_ENABLED feature flag.

Tests that:
- MCP_SIGV4_ENABLED=true (default) + package available → SIGV4_AVAILABLE=True
- MCP_SIGV4_ENABLED=true + package missing → ImportError propagates (fail fast)
- MCP_SIGV4_ENABLED=false → SIGV4_AVAILABLE=False, no import attempted
"""

import pathlib
import sys
import types

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_strands_mcp_factory():
    """Force-reload strands_mcp_factory to pick up env var changes."""
    for mod_name in list(sys.modules):
        if "strands_mcp_factory" in mod_name:
            del sys.modules[mod_name]
    import strands_mcp_factory

    return strands_mcp_factory


class TestSigV4Disabled:
    """When MCP_SIGV4_ENABLED=false."""

    def test_sigv4_not_available_when_disabled(self, monkeypatch):
        """SIGV4_AVAILABLE should be False and no import attempted."""
        monkeypatch.setenv("MCP_SIGV4_ENABLED", "false")

        # Remove the module to prove it's not imported
        monkeypatch.delitem(sys.modules, "streamable_http_sigv4", raising=False)

        mod = _reload_strands_mcp_factory()
        assert mod.SIGV4_AVAILABLE is False


class TestSigV4Enabled:
    """When MCP_SIGV4_ENABLED=true (default)."""

    def test_sigv4_available_when_package_present(self, monkeypatch):
        """Should import and set SIGV4_AVAILABLE=True."""
        monkeypatch.setenv("MCP_SIGV4_ENABLED", "true")

        # Create a fake streamable_http_sigv4 module
        fake_module = types.ModuleType("streamable_http_sigv4")
        fake_module.streamablehttp_client_with_sigv4 = lambda **kw: None
        monkeypatch.setitem(sys.modules, "streamable_http_sigv4", fake_module)

        mod = _reload_strands_mcp_factory()
        assert mod.SIGV4_AVAILABLE is True

    def test_raises_import_error_when_package_missing(self, monkeypatch):
        """Should raise ImportError (fail fast) when package is missing."""
        monkeypatch.setenv("MCP_SIGV4_ENABLED", "true")

        # Ensure streamable_http_sigv4 is NOT importable
        monkeypatch.delitem(sys.modules, "streamable_http_sigv4", raising=False)

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def blocking_import(name, *args, **kwargs):
            if name == "streamable_http_sigv4":
                raise ImportError("No module named 'streamable_http_sigv4'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", blocking_import)

        with pytest.raises(ImportError, match="streamable_http_sigv4"):
            _reload_strands_mcp_factory()

    def test_default_is_enabled(self, monkeypatch):
        """Default (no env var) should behave as enabled=true."""
        monkeypatch.delenv("MCP_SIGV4_ENABLED", raising=False)

        fake_module = types.ModuleType("streamable_http_sigv4")
        fake_module.streamablehttp_client_with_sigv4 = lambda **kw: None
        monkeypatch.setitem(sys.modules, "streamable_http_sigv4", fake_module)

        mod = _reload_strands_mcp_factory()
        assert mod.SIGV4_AVAILABLE is True
