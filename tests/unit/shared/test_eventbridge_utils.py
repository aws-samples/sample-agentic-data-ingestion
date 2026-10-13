# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for shared/eventbridge_utils.py feature flag behaviour.

Tests that:
- EVENTBRIDGE_ENABLED=true + module available → returns real publishers
- EVENTBRIDGE_ENABLED=true + module missing → raises ImportError (fail fast)
- EVENTBRIDGE_ENABLED=false → returns noop functions regardless
"""

import pathlib
import sys
import types

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_eventbridge_utils():
    """Force-reload eventbridge_utils to pick up env var changes."""
    # Remove cached module so it re-evaluates on import
    for mod_name in list(sys.modules):
        if "eventbridge_utils" in mod_name:
            del sys.modules[mod_name]
    import eventbridge_utils

    return eventbridge_utils


class TestEventBridgeEnabledTrue:
    """When EVENTBRIDGE_ENABLED=true (default) and module is available."""

    def test_returns_real_publishers_when_module_available(self, monkeypatch):
        """Should return the real publisher functions from eventbridge_publisher."""
        monkeypatch.setenv("EVENTBRIDGE_ENABLED", "true")

        # Create a fake eventbridge_publisher module
        fake_module = types.ModuleType("eventbridge_publisher")
        fake_module.publish_pipeline_state_change = lambda *a, **kw: True
        fake_module.publish_approval_status_change = lambda *a, **kw: True
        fake_module.publish_mcp_operation_event = lambda *a, **kw: True
        monkeypatch.setitem(sys.modules, "eventbridge_publisher", fake_module)

        mod = _reload_eventbridge_utils()
        pub1, pub2, pub3 = mod.get_eventbridge_publishers()

        assert pub1 is fake_module.publish_pipeline_state_change
        assert pub2 is fake_module.publish_approval_status_change
        assert pub3 is fake_module.publish_mcp_operation_event

    def test_raises_import_error_when_module_missing(self, monkeypatch):
        """Should raise ImportError (fail fast) when module is missing."""
        monkeypatch.setenv("EVENTBRIDGE_ENABLED", "true")

        # Ensure eventbridge_publisher is NOT importable
        monkeypatch.delitem(sys.modules, "eventbridge_publisher", raising=False)

        # Also block it from being found by the import system
        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def blocking_import(name, *args, **kwargs):
            if name == "eventbridge_publisher":
                raise ImportError("No module named 'eventbridge_publisher'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", blocking_import)

        mod = _reload_eventbridge_utils()
        with pytest.raises(ImportError, match="eventbridge_publisher"):
            mod.get_eventbridge_publishers()


class TestEventBridgeEnabledFalse:
    """When EVENTBRIDGE_ENABLED=false."""

    def test_returns_noop_when_disabled(self, monkeypatch):
        """Should return noop functions without attempting import."""
        monkeypatch.setenv("EVENTBRIDGE_ENABLED", "false")

        # Remove eventbridge_publisher to prove it's not imported
        monkeypatch.delitem(sys.modules, "eventbridge_publisher", raising=False)

        mod = _reload_eventbridge_utils()
        pub1, pub2, pub3 = mod.get_eventbridge_publishers()

        # All should be the _noop function
        assert pub1 is pub2 is pub3
        assert pub1() is False  # _noop returns False

    def test_default_is_enabled(self, monkeypatch):
        """Default (no env var) should behave as enabled=true."""
        monkeypatch.delenv("EVENTBRIDGE_ENABLED", raising=False)

        fake_module = types.ModuleType("eventbridge_publisher")
        fake_module.publish_pipeline_state_change = lambda *a, **kw: True
        fake_module.publish_approval_status_change = lambda *a, **kw: True
        fake_module.publish_mcp_operation_event = lambda *a, **kw: True
        monkeypatch.setitem(sys.modules, "eventbridge_publisher", fake_module)

        mod = _reload_eventbridge_utils()
        pub1, pub2, pub3 = mod.get_eventbridge_publishers()

        assert pub1 is fake_module.publish_pipeline_state_change
