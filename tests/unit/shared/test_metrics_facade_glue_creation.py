# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Characterization pin for ``publish_glue_job_creation_metrics`` (p39 task 5.4, R2.1).

**This is a pin, not a change.** p39 *demotes* this publisher — it stops the
dashboard's monthly-cost tile sourcing its figure from ``GlueJob.EstimatedCostPerRun``
(p39 C2, tasks 6.1/6.2) in favour of the new per-run ``GlueJob.RunCostEstimate``
(``publish_glue_run_cost``, task 5.1) — but it does not edit this function, and
nothing here asserts it should change. The function had **no covering tests**
(CodeGraph, re-confirmed by grep over ``tests/``) while carrying two live call
sites, both in ``create_glue_pipeline/job_builders.py``: the success path at
``:761`` and the ``except`` path at ``:785``. These tests close that gap so the
demotion cannot quietly become a deletion.

What is pinned, and why each:

* ``GlueJob.Created`` and ``GlueJob.WorkerConfiguration`` — the two metrics task
  5.4 names. They are the surviving purpose of this publisher.
* ``GlueJob.EstimatedCostPerRun`` — pinned **deliberately**, though 5.4 does not
  ask for it. p39 stops *reading* this metric; it does not stop it being
  published, and the two are separate decisions. Without a pin, a later reader
  who finds no consumer could delete the publish as dead code and believe p39
  sanctioned it. This assertion says it did not.
* ``GlueJob.CreationFailed`` on the failure path, and the absence of the
  worker/cost pair there — that is the shape of the second call site
  (``job_builders.py:785`` passes ``creation_success=False``,
  ``worker_type="unknown"``, ``num_workers=0``).

Expectations here derive from the function as read (``metrics_facade.py:608-654``),
which is correct for characterization: this is behaviour that must survive the
change set, not behaviour p39 corrects.

The publisher writes through the module-level ``metrics_facade`` singleton and
calls ``put_direct_metric`` unconditionally, so ``POWERTOOLS_ENABLED`` does not
route it. Reloading the module (the helper pattern from
``test_metrics_facade_flush.py:32-45``) yields a fresh singleton with an empty
buffer and no sampling configured, so publication is deterministic.

CloudWatch is stubbed with ``MagicMock``, matching
``test_metrics_facade_flush.py`` / ``test_metrics_facade_props.py``; the shared
fixtures in ``tests/fixtures/conftest.py`` cover DynamoDB, S3 and SQS only.
"""

import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


def _reload_metrics_facade(monkeypatch, enabled: str):
    """Force-reload ``metrics_facade`` with the given POWERTOOLS_ENABLED value.

    The module resolves the flag at import time, and the reload also rebuilds
    the module-level ``metrics_facade`` singleton, so each test gets an empty
    buffer and no inherited sample rates.
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


@pytest.fixture
def mock_cw(facade_module):
    """Stub CloudWatch on the singleton the publisher actually writes through."""
    cw = MagicMock()
    facade_module.metrics_facade._cloudwatch = cw
    return cw


def _published(mock_cw) -> list[dict[str, Any]]:
    """Every MetricData entry across every put_metric_data call, in order."""
    entries: list[dict[str, Any]] = []
    for call in mock_cw.put_metric_data.call_args_list:
        entries.extend(call.kwargs["MetricData"])
    return entries


def _one(mock_cw, name: str) -> dict[str, Any]:
    """The single entry published under *name*, asserting there is exactly one."""
    matches = [e for e in _published(mock_cw) if e["MetricName"] == name]
    assert len(matches) == 1, f"expected exactly one {name} datapoint, got {len(matches)}"
    return matches[0]


def _dims(entry: dict[str, Any]) -> dict[str, str]:
    return {d["Name"]: d["Value"] for d in entry.get("Dimensions", [])}


def _call_success(facade_module, **overrides):
    """Invoke the publisher with the success call site's argument shape.

    Mirrors ``job_builders.py:761``.
    """
    kwargs: dict[str, Any] = {
        "pipeline_id": "pipeline-abc",
        "job_name": "agentic-data-ingestion-job-pipeline-abc",
        "creation_success": True,
        "worker_type": "G.1X",
        "num_workers": 5,
        "estimated_cost_per_run": 0.44,
        "mcp_enabled": False,
        "fallback_used": False,
        "environment": "dev",
    }
    kwargs.update(overrides)
    facade_module.publish_glue_job_creation_metrics(**kwargs)


def test_creation_success_publishes_created_and_worker_configuration(facade_module, mock_cw):
    """A successful creation publishes GlueJob.Created and GlueJob.WorkerConfiguration.

    The pin task 5.4 asks for: both metrics, their units, their dimension sets,
    and the namespace they land in — a rename of any of those breaks the
    dashboard's ability to find them.
    """
    _call_success(facade_module)

    created = _one(mock_cw, "GlueJob.Created")
    assert created["Value"] == 1
    assert created["Unit"] == "Count"
    assert _dims(created) == {"PipelineId": "pipeline-abc", "Environment": "dev"}

    worker_config = _one(mock_cw, "GlueJob.WorkerConfiguration")
    assert worker_config["Value"] == 5, "value is the worker count, not a flag"
    assert worker_config["Unit"] == "Count"
    assert _dims(worker_config) == {"WorkerType": "G.1X", "Environment": "dev"}

    namespaces = {c.kwargs["Namespace"] for c in mock_cw.put_metric_data.call_args_list}
    assert namespaces == {"AgenticDataIngestion/Pipeline"}

    assert "GlueJob.CreationFailed" not in {e["MetricName"] for e in _published(mock_cw)}


def test_creation_success_still_publishes_estimated_cost_per_run(facade_module, mock_cw):
    """GlueJob.EstimatedCostPerRun is still published — p39 demotes its reader, not it.

    p39 tasks 6.1/6.2 stop the dashboard handler *reading* this metric and delete
    the flat per-run cost fallback that consumed it; the monthly-cost tile moves
    to ``GlueJob.RunCostEstimate`` from ``publish_glue_run_cost``. Neither of
    those touches this emit. Removing it is a separate decision that has not
    been taken, and this assertion is what makes that explicit.
    """
    _call_success(facade_module, estimated_cost_per_run=0.147)

    cost = _one(mock_cw, "GlueJob.EstimatedCostPerRun")
    assert cost["Value"] == 0.147
    assert cost["Unit"] == "None", "not a currency claim — nothing here measures spend"
    assert _dims(cost) == {
        "PipelineId": "pipeline-abc",
        "WorkerType": "G.1X",
        "Environment": "dev",
    }


def test_creation_failure_publishes_creation_failed_without_the_worker_pair(facade_module, mock_cw):
    """The failure call site publishes only GlueJob.CreationFailed.

    ``job_builders.py:785`` passes ``creation_success=False`` with
    ``worker_type="unknown"`` and ``num_workers=0``. Neither
    ``GlueJob.WorkerConfiguration`` nor ``GlueJob.EstimatedCostPerRun`` may be
    emitted there — a zero-worker configuration datapoint would drag the
    WorkerConfiguration average down for every failed creation.
    """
    facade_module.publish_glue_job_creation_metrics(
        pipeline_id="pipeline-abc",
        job_name="agentic-data-ingestion-job-pipeline-abc",
        creation_success=False,
        worker_type="unknown",
        num_workers=0,
        estimated_cost_per_run=0.0,
        mcp_enabled=False,
        fallback_used=True,
        environment="dev",
    )

    failed = _one(mock_cw, "GlueJob.CreationFailed")
    assert failed["Value"] == 1
    assert failed["Unit"] == "Count"
    assert _dims(failed) == {"PipelineId": "pipeline-abc", "Environment": "dev"}

    assert [e["MetricName"] for e in _published(mock_cw)] == ["GlueJob.CreationFailed"]


def test_mcp_enabled_adds_the_mcp_enabled_datapoint(facade_module, mock_cw):
    """MCP.Enabled is published only when the caller reports MCP was used.

    Both call sites pass ``job_builders.MCP_ENABLED``, so this branch is
    deploy-configuration driven and its absence in the other tests is the
    ``mcp_enabled=False`` case, not an oversight.
    """
    _call_success(facade_module, mcp_enabled=True, fallback_used=True)

    mcp = _one(mock_cw, "MCP.Enabled")
    assert mcp["Value"] == 1
    assert mcp["Unit"] == "Count"
    assert _dims(mcp) == {
        "Context": "GlueJobCreation",
        "FallbackUsed": "True",
        "Environment": "dev",
    }


def test_publish_flushes_the_buffer_before_returning(facade_module, mock_cw):
    """The publisher flushes, so nothing is left buffered when the process ends.

    ``put_direct_metric`` buffers and auto-flushes only at ``batch_size`` (20);
    four metrics would otherwise sit in the buffer and be lost. This is the same
    failure mode p39 C5 fixes for ``record_stage_execution``, and the reason it
    does not apply here is the explicit ``flush_direct()`` at
    ``metrics_facade.py:651`` — pinned so it cannot be dropped.
    """
    _call_success(facade_module, mcp_enabled=True)

    assert mock_cw.put_metric_data.call_count == 1, "one batch, not one call per metric"
    assert facade_module.metrics_facade.buffer_length == 0
    assert [e["MetricName"] for e in _published(mock_cw)] == [
        "GlueJob.Created",
        "MCP.Enabled",
        "GlueJob.WorkerConfiguration",
        "GlueJob.EstimatedCostPerRun",
    ]
