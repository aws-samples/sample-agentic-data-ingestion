# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for ``publish_glue_run_cost`` (p39 task 5.3, R2.1).

The publisher task 5.1 added (``metrics_facade.py:686-727``) is the source the
dashboard's monthly-cost tile moves to. It had **one** production caller
(``job_completion/lambda_function.py:699``, via ``_publish_run_cost``) and no
covering tests; this file closes that. The call-site half — which terminal states
reach it, and that a duplicate delivery does not — lives in
``tests/unit/lambdas/job_completion/test_run_cost_publication.py``.

What is asserted, and why each matters to the reader on the other side of the
seam:

* **The metric name.** ``GlueJob.RunCostEstimate``, not
  ``GlueJob.EstimatedCostPerRun``. The latter is published once per job
  *creation* by ``publish_glue_job_creation_metrics`` (pinned separately in
  ``test_metrics_facade_glue_creation.py``), and summing it counted creations and
  priced them at the estimate — the defect p39 C2 exists to correct. A rename
  here silently empties the tile.
* **The four dimensions.** ``PipelineId``, ``JobName``, ``WorkerType``,
  ``Environment``. ``PipelineId`` is what makes a datapoint attributable, and
  ``Environment`` is what keeps dev spend out of a prod window.
* **The unit is ``"None"``.** Deliberately not a currency unit: nothing in this
  system measures spend, the figure is derived from Glue DPU-minutes with a
  10-minute per-run minimum, and R2.7 requires that stated rather than implied.
* **It flushes.** ``put_direct_metric`` buffers and auto-flushes only at
  ``batch_size`` (20), so one metric per Lambda invocation would sit in the
  buffer and die with the execution environment — the exact loss mode p39 C5
  fixes for ``record_stage_execution``. The explicit ``flush_direct()`` at
  ``metrics_facade.py:724`` is why it does not apply here.
* **It never raises.** The Step Functions callback has already been sent by the
  time the caller reaches it, so a publish failure must not change a run's
  outcome.

Expectations derive from R2.1 and R2.7, not from observed output.

The publisher writes through the module-level ``metrics_facade`` singleton and
calls ``put_direct_metric`` + ``flush_direct`` **unconditionally**, so
``POWERTOOLS_ENABLED`` does not route it and it does not depend on task 8.1's
flush fix. Reloading the module (the helper pattern from
``test_metrics_facade_flush.py:32-45``, reused by
``test_metrics_facade_glue_creation.py``) yields a fresh singleton with an empty
buffer and no sampling configured, so publication is deterministic.

CloudWatch is stubbed with ``MagicMock``; the shared fixtures in
``tests/fixtures/conftest.py`` cover DynamoDB, S3 and SQS only.
"""

import logging
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


def _call(facade_module, **overrides):
    """Invoke the publisher with the ``job_completion`` call site's argument shape.

    Mirrors ``_publish_run_cost`` at ``job_completion/lambda_function.py:699-705``,
    whose values come from ``collect_job_metrics``. 0.147 is the constant a short
    run actually produces, because of the 10-minute DPU minimum.
    """
    kwargs: dict[str, Any] = {
        "pipeline_id": "pipeline-abc",
        "job_name": "agentic-data-ingestion-job-pipeline-abc",
        "estimated_cost": 0.147,
        "worker_type": "G.1X",
        "environment": "dev",
    }
    kwargs.update(overrides)
    facade_module.publish_glue_run_cost(**kwargs)


def test_publishes_run_cost_estimate_with_its_four_dimensions(facade_module, mock_cw):
    """One GlueJob.RunCostEstimate datapoint, carrying the cost and all four dimensions.

    **Validates: Requirement 2.1**
    """
    _call(facade_module)

    entry = _one(mock_cw, "GlueJob.RunCostEstimate")
    assert entry["Value"] == 0.147
    assert _dims(entry) == {
        "PipelineId": "pipeline-abc",
        "JobName": "agentic-data-ingestion-job-pipeline-abc",
        "WorkerType": "G.1X",
        "Environment": "dev",
    }

    namespaces = {c.kwargs["Namespace"] for c in mock_cw.put_metric_data.call_args_list}
    assert namespaces == {"AgenticDataIngestion/Pipeline"}

    assert [e["MetricName"] for e in _published(mock_cw)] == ["GlueJob.RunCostEstimate"]


def test_the_unit_is_none_not_a_currency_claim(facade_module, mock_cw):
    """The unit is ``"None"``, because the figure is an estimate and not measured spend.

    Derived from R2.7: the value is DPU-minutes × a published rate, with a
    10-minute per-run minimum applied upstream. Nothing in this system measures
    spend, so a currency unit would be a claim the number cannot support.

    **Validates: Requirements 2.1, 2.7**
    """
    _call(facade_module)

    assert _one(mock_cw, "GlueJob.RunCostEstimate")["Unit"] == "None"


def test_it_is_not_the_creation_metric(facade_module, mock_cw):
    """The name is the per-run metric, never the per-creation one.

    ``GlueJob.EstimatedCostPerRun`` is emitted once per job *created*
    (``publish_glue_job_creation_metrics``), so summing it over a window counts
    creations. Sourcing the run cost from it is the defect p39 C2 corrects; this
    assertion is what stops the two being confused again.

    **Validates: Requirement 2.1**
    """
    _call(facade_module)

    published = {e["MetricName"] for e in _published(mock_cw)}
    assert "GlueJob.EstimatedCostPerRun" not in published
    assert "GlueJob.RunCostEstimate" in published


def test_publish_flushes_the_buffer_before_returning(facade_module, mock_cw):
    """The publisher flushes, so its one datapoint is not left buffered.

    ``put_direct_metric`` auto-flushes only at ``batch_size`` (20) and a Glue run
    contributes exactly one metric, so without the explicit ``flush_direct()``
    every datapoint would die with the execution environment. That is the loss
    mode p39 C5 fixes elsewhere; here the flush is already present and this pins
    it.

    **Validates: Requirement 2.1**
    """
    _call(facade_module)

    assert mock_cw.put_metric_data.call_count == 1
    assert facade_module.metrics_facade.buffer_length == 0


def test_default_environment_is_dev(facade_module, mock_cw):
    """Omitting *environment* dimensions the datapoint as ``dev``.

    The caller reads ``ENVIRONMENT`` from the Lambda's own environment and passes
    it explicitly, so this covers the signature's default rather than a live
    path — worth pinning because the dimension is what keeps windows from
    different deployments out of one another.

    **Validates: Requirement 2.1**
    """
    facade_module.publish_glue_run_cost(
        pipeline_id="pipeline-abc",
        job_name="job-abc",
        estimated_cost=0.5,
        worker_type="G.2X",
    )

    assert _dims(_one(mock_cw, "GlueJob.RunCostEstimate"))["Environment"] == "dev"


def test_a_raising_cloudwatch_client_does_not_propagate(facade_module, mock_cw, caplog):
    """A CloudWatch failure is logged and swallowed, never raised at the caller.

    The caller publishes *after* the Step Functions callback has been sent, so
    the run's outcome is already committed — a publish failure must not change
    it. Note the swallow happens twice over: ``flush_direct`` catches its own
    ``put_metric_data`` failure (``metrics_facade.py:240-241``) and the publisher
    wraps the whole sequence as well, so this holds wherever inside the sequence
    the failure lands.

    **Validates: Requirement 2.1**
    """
    mock_cw.put_metric_data.side_effect = RuntimeError("cloudwatch unavailable")

    with caplog.at_level(logging.ERROR):
        _call(facade_module)

    assert mock_cw.put_metric_data.call_count == 1
    assert facade_module.metrics_facade.buffer_length == 0, "the buffer is drained even on failure"
    assert any("cloudwatch unavailable" in r.getMessage() for r in caplog.records)
