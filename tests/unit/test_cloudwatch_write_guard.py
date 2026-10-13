# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the unit layer's CloudWatch write guard (``tests/unit/conftest.py``).

SCOPE NOTE: the guard and this file are OUTSIDE p39-metrics-truth's task plan.
They were approved by the user during execution of task 10.2, and task 24.1
records the addition as a deviation.

A guard nobody has seen fail is not a guard. What is asserted here is the
property the guard exists for — a unit test that reaches the *real*
``PipelineMetrics.record_stage_execution`` makes no boto3 API call — plus the
two conditions that make that property meaningful rather than vacuous:

- the emit really happened (``put_metric_data`` was called on the stub), so the
  "no API call" result is not the trivial one that a mute producer would give;
- the guard is narrow (only ``cloudwatch`` is stubbed, only ``PutMetricData`` is
  refused) and does not displace a test's own locally-assigned double, which is
  the contract tasks 5.3, 5.4 and 8.3's facade tests depend on.

Evidence, for the record. Before the guard, a botocore-level probe over
``tests/unit/lambdas/create_quality_checks/`` counted 21 attempted
``PutMetricData`` calls into ``AgenticDataIngestion/Pipeline`` — the leak p39
task 8.1's ``flush()`` opened in every test directory that does not stub
``metrics_facade``.
"""

import pathlib
import sys
from unittest.mock import MagicMock

import boto3
import botocore.client
import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

STAGE = "GuardProbe"
PIPELINE = "pipe-cloudwatch-guard"


def _reload_metrics_facade(monkeypatch, enabled: str = "false"):
    """Import ``metrics_facade`` fresh with the given POWERTOOLS_ENABLED value.

    The module resolves the flag at import time (``metrics_facade.py:37-46``),
    so the buffered path is only reachable by dropping it from the module cache
    and importing again. Same helper as ``tests/unit/shared/
    test_metrics_facade_flush.py:32-45``.
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
def api_calls(monkeypatch):
    """Record every botocore API call attempted during the test, and make none.

    This sits *on top of* the guard, so it is reached only if the guard let a
    call through. ``recorded == []`` is therefore direct evidence that no boto3
    call was made — of any operation, to any service — rather than an inference
    from the guard being installed.
    """
    recorded: list[str] = []

    def _recording_make_api_call(self, operation_name, api_params):
        recorded.append(operation_name)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _recording_make_api_call)
    return recorded


# ── The property the guard exists for ────────────────────────────────────────


def test_the_real_record_stage_execution_makes_no_boto3_call(monkeypatch, api_calls):
    """A unit test reaching the real producer publishes nothing over the wire.

    ``_cloudwatch`` is deliberately left at its initial ``None`` — this is the
    unguarded shape, the one 26 tests in
    ``tests/unit/lambdas/create_quality_checks/`` have. Pre-guard, the flush
    added by task 8.1 turned that into a real ``PutMetricData`` against whatever
    account the ambient credentials resolved to.
    """
    mod = _reload_metrics_facade(monkeypatch)
    assert mod.POWERTOOLS_AVAILABLE is False, "the buffered path is the one that reaches PutMetricData"
    assert mod.metrics_facade._cloudwatch is None, "no client built yet — the guard must supply it"

    mod.PipelineMetrics.record_stage_execution(
        stage_name=STAGE,
        pipeline_id=PIPELINE,
        status="completed",
        duration_ms=42.0,
    )

    assert api_calls == [], f"a unit test reached boto3: {api_calls}"


def test_the_emit_still_reaches_put_metric_data_on_the_stub(monkeypatch, api_calls):
    """Non-vacuity: the producer did run, so the empty call list means something.

    Without this, a mute producer — or an emit made unreachable by a
    ``MagicMock(PipelineMetrics=None)`` stub — would satisfy the assertion above
    for entirely the wrong reason.
    """
    mod = _reload_metrics_facade(monkeypatch)

    mod.PipelineMetrics.record_stage_execution(
        stage_name=STAGE,
        pipeline_id=PIPELINE,
        status="completed",
        duration_ms=42.0,
    )

    stub = mod.metrics_facade._cloudwatch
    assert stub is not None, "the flush should have asked for a client"
    assert not isinstance(stub, botocore.client.BaseClient), "the guard should have supplied a stub, not a real client"
    assert stub.put_metric_data.call_count == 1
    kwargs = stub.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "AgenticDataIngestion/Pipeline"
    assert {m["MetricName"] for m in kwargs["MetricData"]} == {
        "Pipeline.StageExecution",
        "Pipeline.StageDuration",
        "Pipeline.StageSuccessRate",
    }
    assert api_calls == []


# ── The backstop, for a client the construction seam cannot see ──────────────


def test_a_session_built_client_still_cannot_publish(cloudwatch_guard_violation):
    """``boto3.Session().client`` bypasses the ``boto3.client`` seam; the operation seam holds.

    Same hole a module-level client built at import time falls through, before
    any fixture has run.
    """
    client = boto3.Session().client("cloudwatch", region_name="us-west-2")
    assert isinstance(client, botocore.client.BaseClient), "this path is meant to yield a real client"

    with pytest.raises(cloudwatch_guard_violation):
        client.put_metric_data(
            Namespace="Test/Guard",
            MetricData=[{"MetricName": "Guard.Probe", "Value": 1.0, "Unit": "Count"}],
        )


# ── The guard is narrow, and yields to a test's own double ───────────────────


def test_only_cloudwatch_is_stubbed():
    """Every other service still gets a real client, so the shared fixtures work."""
    assert not isinstance(boto3.client("cloudwatch"), botocore.client.BaseClient)
    assert isinstance(boto3.client("dynamodb", region_name="us-west-2"), botocore.client.BaseClient)
    assert isinstance(boto3.client("s3", region_name="us-west-2"), botocore.client.BaseClient)


def test_a_locally_assigned_cloudwatch_double_takes_precedence(monkeypatch, api_calls):
    """The contract tasks 5.3, 5.4 and 8.3's facade tests rely on.

    ``_get_cloudwatch()`` only builds a client while ``_cloudwatch is None``, so
    a test that assigns its own double observes its own calls and the guard never
    intervenes.
    """
    mod = _reload_metrics_facade(monkeypatch)
    own_double = MagicMock(name="the_tests_own_double")
    mod.metrics_facade._cloudwatch = own_double

    mod.PipelineMetrics.record_stage_execution(
        stage_name=STAGE,
        pipeline_id=PIPELINE,
        status="failed",
        duration_ms=1.0,
    )

    assert mod.metrics_facade._cloudwatch is own_double
    assert own_double.put_metric_data.call_count == 1
    assert api_calls == []


def test_a_non_guarded_operation_is_untouched(api_calls):
    """Only the guarded write is refused; other CloudWatch calls are not rewritten.

    Asserted through the read path the dashboard handler uses, so the guard
    cannot quietly grow into a blanket CloudWatch block.
    """
    client = boto3.Session().client("cloudwatch", region_name="us-west-2")

    client.get_metric_data(
        MetricDataQueries=[
            {"Id": "m1", "Expression": "SEARCH('{AWS/S3} MetricName=\"BucketSizeBytes\"', 'Average', 86400)"}
        ],
        StartTime="2026-01-01T00:00:00Z",
        EndTime="2026-01-02T00:00:00Z",
    )

    assert api_calls == ["GetMetricData"], "the read reached the recorder, i.e. the guard did not refuse it"
