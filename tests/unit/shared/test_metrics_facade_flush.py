# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for ``MetricsFacade.flush()`` (P30 R16.1).

``flush()`` read ``self._powertools_metrics``, an attribute ``__init__`` never
assigns, and the guard reading it sat *outside* the method's ``try``, so every
call raised ``AttributeError`` on both ``POWERTOOLS_ENABLED`` paths. It survived
because it was doubly invisible: nothing in the repo calls ``flush()`` (every
production flush path calls ``flush_direct()``), and no test called it either.
These tests close the second gap.

Expectations here derive from R16.1 — ``flush()`` must be callable and must
flush the direct buffer — not from the pre-fix behaviour, which was a crash.

CloudWatch is stubbed with ``MagicMock`` on ``_cloudwatch``, matching
``test_metrics_facade_props.py``; the shared fixtures in
``tests/fixtures/conftest.py`` cover DynamoDB, S3 and SQS only.
"""

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_metrics_facade(monkeypatch, enabled: str):
    """Force-reload ``metrics_facade`` with the given POWERTOOLS_ENABLED value.

    The module resolves the flag at import time, so both paths are only
    reachable by dropping it from the module cache and importing again. Mirrors
    the helper in ``test_metrics_facade_flags.py``.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", enabled)
    sys.modules.pop("metrics_facade", None)

    import metrics_facade

    return metrics_facade


@pytest.fixture(autouse=True)
def _drop_reloaded_module():
    """Evict the reloaded module afterwards so no flag state leaks to other files."""
    yield
    sys.modules.pop("metrics_facade", None)


@pytest.fixture
def facade_module(monkeypatch):
    """``metrics_facade`` imported with Powertools disabled (the default)."""
    return _reload_metrics_facade(monkeypatch, "false")


def test_flush_does_not_raise_on_fresh_instance(facade_module):
    """flush() is callable on a freshly constructed facade (R16.1).

    This is the regression assertion: pre-fix this raised
    ``AttributeError: 'MetricsFacade' object has no attribute
    '_powertools_metrics'``.
    """
    facade = facade_module.MetricsFacade(namespace="Test/Flush")

    facade.flush()


def test_flush_publishes_buffered_direct_metrics(facade_module):
    """flush() publishes everything put_direct_metric buffered, and drains it."""
    facade = facade_module.MetricsFacade(namespace="Test/Flush", batch_size=100)
    mock_cw = MagicMock()
    facade._cloudwatch = mock_cw

    facade.put_direct_metric("Flush.MetricA", 1.0, "Count", {"Dim": "a"})
    facade.put_direct_metric("Flush.MetricB", 2.0, "Count")
    assert facade.buffer_length == 2, "put_direct_metric should buffer below batch_size"

    facade.flush()

    assert mock_cw.put_metric_data.call_count == 1
    kwargs = mock_cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "Test/Flush"
    assert [m["MetricName"] for m in kwargs["MetricData"]] == [
        "Flush.MetricA",
        "Flush.MetricB",
    ]
    assert facade.buffer_length == 0, "flush() should leave the buffer empty"


def test_flush_with_empty_buffer_makes_no_api_call(facade_module):
    """flush() with nothing buffered issues no CloudWatch call at all.

    ``_cloudwatch`` staying ``None`` proves ``_get_cloudwatch()`` was never
    reached, so no boto3 client was constructed either.
    """
    facade = facade_module.MetricsFacade(namespace="Test/Flush")

    facade.flush()

    assert facade._cloudwatch is None


def test_flush_succeeds_with_powertools_enabled(monkeypatch):
    """flush() works on the POWERTOOLS_ENABLED=true path too.

    The broken guard ran before any ``POWERTOOLS_AVAILABLE`` check, so enabling
    Powertools did not avoid the ``AttributeError``. Both paths are pinned.
    """
    mod = _reload_metrics_facade(monkeypatch, "true")
    assert mod.POWERTOOLS_AVAILABLE is True

    facade = mod.MetricsFacade(namespace="Test/Flush", batch_size=100)
    mock_cw = MagicMock()
    facade._cloudwatch = mock_cw

    facade.put_direct_metric("Flush.Enabled", 1.0, "Count")
    facade.flush()

    assert mock_cw.put_metric_data.call_count == 1
    assert facade.buffer_length == 0
