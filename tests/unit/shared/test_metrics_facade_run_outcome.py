# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``publish_run_outcome``: the pipeline error-rate series the alarm reads.

PipelineErrorRateAlarm (deploy-cdk/lib/orchestration-stack.ts) reads
``AgenticDataIngestion/Pipeline`` / ``Pipeline.ErrorRate`` with exactly one
dimension, ``Environment``, and averages it. Each finished run contributes one
datapoint, 100 for a failure and 0 for a success, so the average is the failed
share. A CloudWatch metric is identified by its whole dimension set, so one
extra dimension (the ``service`` dimension ``put_metric``'s fallback adds, see
LGR-ba3dfab-5) would put the datapoint on a series the alarm never reads, which
is how the alarm used to be connected to nothing.

Expectations come from the alarm's definition, not from the publisher.
"""

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)


@pytest.fixture
def facade_module(monkeypatch):
    """A fresh ``metrics_facade`` (empty buffer, no sampling), evicted afterwards."""
    monkeypatch.setenv("POWERTOOLS_ENABLED", "false")
    sys.modules.pop("metrics_facade", None)
    import metrics_facade

    yield metrics_facade
    sys.modules.pop("metrics_facade", None)


@pytest.fixture
def mock_cw(facade_module):
    cw = MagicMock()
    facade_module.metrics_facade._cloudwatch = cw
    return cw


def _entries(mock_cw) -> list[tuple[str, dict]]:
    return [
        (call.kwargs["Namespace"], entry)
        for call in mock_cw.put_metric_data.call_args_list
        for entry in call.kwargs["MetricData"]
    ]


@pytest.mark.parametrize(("failed", "value"), [(True, 100.0), (False, 0.0)])
def test_one_datapoint_on_exactly_the_series_the_alarm_reads(facade_module, mock_cw, failed, value):
    facade_module.publish_run_outcome(failed=failed, environment="prod")

    [(namespace, entry)] = _entries(mock_cw)
    assert namespace == "AgenticDataIngestion/Pipeline"
    assert entry["MetricName"] == "Pipeline.ErrorRate"
    assert entry["Dimensions"] == [{"Name": "Environment", "Value": "prod"}]
    assert entry["Value"] == value
    assert entry["Unit"] == "Percent"


def test_the_average_of_the_datapoints_is_the_failed_share(facade_module, mock_cw):
    """The property the alarm's Average statistic relies on: 1 failure in 5 runs is 20%."""
    for failed in (True, False, False, False, False):
        facade_module.publish_run_outcome(failed=failed)

    values = [entry["Value"] for _, entry in _entries(mock_cw)]
    assert sum(values) / len(values) == 20.0


def test_a_publish_failure_is_logged_not_raised(facade_module, mock_cw):
    mock_cw.put_metric_data.side_effect = RuntimeError("throttled")

    facade_module.publish_run_outcome(failed=True)  # must not raise


def test_the_constants_name_the_published_series(facade_module, mock_cw):
    facade_module.publish_run_outcome(failed=False)

    [(namespace, entry)] = _entries(mock_cw)
    assert namespace == facade_module.RUN_ERROR_RATE_NAMESPACE
    assert entry["MetricName"] == facade_module.RUN_ERROR_RATE_METRIC
    assert [d["Name"] for d in entry["Dimensions"]] == [facade_module.RUN_ERROR_RATE_DIMENSION]
