# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: how the approval handler resolves the advertised deadline.

``timeout_at`` on the approval record is the deadline the approver is told
about (``send_approval_notifications`` recomputes ``timeout_hours`` from
``timeout_at - created_at`` and puts it in the notification body), so the
resolution order behind it is load-bearing. p34 R6 moved the *default* from
168 h to 72 h so the advertised deadline matches the gate that enforces it;
these tests pin the two override paths that must keep working across that
move, neither of which had a test before:

1. env ``DEFAULT_TIMEOUT_HOURS`` overrides the shared default, and
2. a per-pipeline ``approvals.timeout_hours`` in ``pipeline_config`` takes
   precedence over both.

The env var is read once at module import (``DEFAULT_TIMEOUT_HOURS =
default_timeout_hours()``), which is why test 1 reloads the module under a
patched environment rather than setting the variable and calling the handler.

Validates: Requirements 18.1, 18.2 (P28); 6.1 (p34)
"""

import importlib.util
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

for _p in (_LAMBDA_DIR, _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REQUIRED_ENV = {
    "AGENTIC_DATA_INGESTION_ENV": "local",
    "APPROVALS_TABLE": "test-approvals",
    "STATE_TABLE": "test-state",
    "TASK_TOKENS_TABLE": "test-task-tokens",
}

_EVENT = {
    "approval_type": "schema_mapping",
    "pipeline_id": "pipe-1",
    "run_id": "run-1",
}


def _load_handler(alias: str):
    """Import the approval handler fresh, with AWS clients mocked out.

    A fresh import is required because ``DEFAULT_TIMEOUT_HOURS`` is resolved
    at module scope.
    """
    for name, value in _REQUIRED_ENV.items():
        os.environ.setdefault(name, value)

    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.side_effect = lambda _name: MagicMock()
        sys.modules.pop(alias, None)
        spec = importlib.util.spec_from_file_location(alias, os.path.join(_LAMBDA_DIR, "lambda_function.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    return module


def _advertised_hours(handler, pipeline_config: dict) -> float:
    """Create an approval and return the window it advertised, in hours."""
    captured: dict = {}
    handler.approvals_table = MagicMock()
    handler.approvals_table.put_item.side_effect = lambda **kw: captured.update(kw["Item"])

    with (
        patch.object(handler, "get_pipeline_config", return_value=pipeline_config),
        patch.object(handler, "get_approval_data", return_value={}),
        patch.object(handler, "dynamodb", MagicMock()),
    ):
        handler.create_approval_request(dict(_EVENT))

    created_at = datetime.fromisoformat(captured["created_at"])
    timeout_at = datetime.fromisoformat(captured["timeout_at"])
    return (timeout_at - created_at).total_seconds() / 3600


class TestAdvertisedWindowResolution:
    def test_default_is_the_shared_advertised_window(self, monkeypatch):
        """No env override, no per-pipeline value → config.APPROVAL_WINDOW_HOURS."""
        monkeypatch.delenv("DEFAULT_TIMEOUT_HOURS", raising=False)
        handler = _load_handler("ah_window_default")

        from config import APPROVAL_WINDOW_HOURS

        assert handler.DEFAULT_TIMEOUT_HOURS == APPROVAL_WINDOW_HOURS
        assert _advertised_hours(handler, {"approvals": {}}) == pytest.approx(APPROVAL_WINDOW_HOURS, abs=0.01)

    def test_env_override_still_applies(self, monkeypatch):
        """env DEFAULT_TIMEOUT_HOURS overrides the shared default (unchanged by p34 R6)."""
        monkeypatch.setenv("DEFAULT_TIMEOUT_HOURS", "36")
        handler = _load_handler("ah_window_env")

        assert handler.DEFAULT_TIMEOUT_HOURS == 36
        assert _advertised_hours(handler, {"approvals": {}}) == pytest.approx(36, abs=0.01)

    def test_per_pipeline_timeout_hours_takes_precedence(self, monkeypatch):
        """approvals.timeout_hours beats both the env override and the default."""
        monkeypatch.setenv("DEFAULT_TIMEOUT_HOURS", "36")
        handler = _load_handler("ah_window_pipeline")

        advertised = _advertised_hours(handler, {"approvals": {"timeout_hours": 8}})
        assert advertised == pytest.approx(8, abs=0.01)
