# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for shared/metrics_facade.py POWERTOOLS_ENABLED feature flag.

Tests that:
- POWERTOOLS_ENABLED=true → imports Powertools, MetricsFacade uses EMF
- POWERTOOLS_ENABLED=false (default) → no Powertools import, falls back to direct CloudWatch
"""

import pathlib
import sys
import types
from unittest.mock import MagicMock

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_metrics_facade(monkeypatch, enabled: str):
    """Force-reload metrics_facade with the given POWERTOOLS_ENABLED value."""
    monkeypatch.setenv("POWERTOOLS_ENABLED", enabled)

    # Clear cached modules
    for mod_name in list(sys.modules):
        if "metrics_facade" in mod_name:
            del sys.modules[mod_name]

    import metrics_facade

    return metrics_facade


class TestPowertoolsDisabled:
    """When POWERTOOLS_ENABLED=false (default)."""

    def test_powertools_not_imported(self, monkeypatch):
        """PowertoolsMetrics and MetricUnit should be None."""
        # Remove any fake powertools modules that might be cached
        for mod_name in list(sys.modules):
            if "aws_lambda_powertools" in mod_name:
                del sys.modules[mod_name]
        mod = _reload_metrics_facade(monkeypatch, "false")
        assert mod.POWERTOOLS_AVAILABLE is False
        assert mod.MetricUnit is None

    def test_facade_falls_back_to_direct_cloudwatch(self, monkeypatch):
        """MetricsFacade.put_metric should use put_direct_metric when Powertools disabled."""
        for mod_name in list(sys.modules):
            if "aws_lambda_powertools" in mod_name:
                del sys.modules[mod_name]
        mod = _reload_metrics_facade(monkeypatch, "false")
        facade = mod.MetricsFacade(namespace="Test/Namespace")

        # The facade should have no powertools metrics instance
        assert not mod.POWERTOOLS_AVAILABLE

        # put_metric should delegate to put_direct_metric (buffer)
        facade.put_metric("TestMetric", 1.0, "Count", {"Key": "Value"})
        assert facade.buffer_length == 1

    def test_default_is_disabled(self, monkeypatch):
        """Default (no env var) should behave as disabled."""
        monkeypatch.delenv("POWERTOOLS_ENABLED", raising=False)

        for mod_name in list(sys.modules):
            if "metrics_facade" in mod_name or "aws_lambda_powertools" in mod_name:
                del sys.modules[mod_name]

        import metrics_facade

        assert metrics_facade.POWERTOOLS_AVAILABLE is False


class TestPowertoolsEnabled:
    """When POWERTOOLS_ENABLED=true."""

    def _install_fake_powertools(self, monkeypatch):
        """Install fake aws_lambda_powertools modules with all required attributes."""
        fake_powertools = types.ModuleType("aws_lambda_powertools")
        fake_metrics_mod = types.ModuleType("aws_lambda_powertools.metrics")

        mock_metrics_class = MagicMock()
        fake_powertools.Metrics = mock_metrics_class
        fake_metrics_mod.MetricUnit = MagicMock()
        fake_metrics_mod.single_metric = MagicMock()

        monkeypatch.setitem(sys.modules, "aws_lambda_powertools", fake_powertools)
        monkeypatch.setitem(sys.modules, "aws_lambda_powertools.metrics", fake_metrics_mod)
        return mock_metrics_class, fake_metrics_mod

    def test_powertools_imported_when_available(self, monkeypatch):
        """Should import Powertools and set POWERTOOLS_AVAILABLE=True."""
        mock_metrics_class, fake_metrics_mod = self._install_fake_powertools(monkeypatch)

        mod = _reload_metrics_facade(monkeypatch, "true")
        assert mod.POWERTOOLS_AVAILABLE is True
        assert mod.MetricUnit is fake_metrics_mod.MetricUnit

    def test_facade_uses_powertools_when_enabled(self, monkeypatch):
        """POWERTOOLS_AVAILABLE should be True so put_metric takes the EMF path.

        MetricsFacade deliberately does NOT construct a Powertools ``Metrics``
        aggregator — ``put_metric`` emits per metric via ``single_metric()``.
        The previous docstring here claimed it created one; that was a vestige
        of an earlier design and the source of the ``flush()`` defect fixed
        under P30 R16.1.
        """
        mock_metrics_class, fake_metrics_mod = self._install_fake_powertools(monkeypatch)

        mod = _reload_metrics_facade(monkeypatch, "true")
        assert mod.POWERTOOLS_AVAILABLE is True
