# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The pipeline error-rate producers and the alarm name the same CloudWatch series.

A CloudWatch metric is identified by namespace, name AND the full set of
dimensions. Two producers write ``Pipeline.ErrorRate``: job_completion through
``shared/metrics_facade.publish_run_outcome`` (0 per success) and
record_failure, which ships without the shared layer and vendors the three
values (100 per failure). PipelineErrorRateAlarm in orchestration-stack.ts
reads the series. If any of the four drifts, the alarm reads a series nobody
writes and can never fire, which is the state it was in until 2026-10-11.

Both producers must also take the Environment value from the same place: the
ENVIRONMENT variable, set on both functions to the stack's environment, which
is the value the alarm's dimension uses.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

# record_failure builds its DynamoDB resource at import.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION = (ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts").read_text(encoding="utf-8")
COMPUTE = (ROOT / "deploy-cdk" / "lib" / "compute-stack.ts").read_text(encoding="utf-8")

if str(ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared"))
import metrics_facade


def _record_failure():
    spec = importlib.util.spec_from_file_location(
        "record_failure_for_error_rate_contract",
        ROOT / "lambda" / "agentic_data_ingestion_record_failure" / "lambda_function.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block(source: str, anchor: str, end: str) -> str:
    start = source.index(anchor)
    return source[start : source.index(end, start)]


def _alarm_block() -> str:
    return _block(ORCHESTRATION, "new cloudwatch.Alarm(this, 'PipelineErrorRateAlarm'", "addAlarmAction")


def test_record_failure_vendors_the_shared_series():
    rf = _record_failure()

    assert rf.ERROR_RATE_NAMESPACE == metrics_facade.RUN_ERROR_RATE_NAMESPACE
    assert rf.ERROR_RATE_METRIC == metrics_facade.RUN_ERROR_RATE_METRIC
    assert rf.ERROR_RATE_DIMENSION == metrics_facade.RUN_ERROR_RATE_DIMENSION


def test_the_alarm_reads_the_series_the_producers_write():
    alarm = _alarm_block()

    assert f"namespace: '{metrics_facade.RUN_ERROR_RATE_NAMESPACE}'" in alarm
    assert f"metricName: '{metrics_facade.RUN_ERROR_RATE_METRIC}'" in alarm
    dimensions = re.search(r"dimensionsMap:\s*\{([^}]*)\}", alarm)
    assert dimensions, "the alarm must name its dimensions; a dimensionless alarm reads a series nobody writes"
    keys = re.findall(r"(\w+)\s*:", dimensions.group(1))
    assert keys == [metrics_facade.RUN_ERROR_RATE_DIMENSION]
    assert "statistic: 'Average'" in alarm, "the failed share is the Average of 0/100 datapoints"


def test_both_producers_set_the_environment_the_alarm_filters_on():
    job_completion = _block(COMPUTE, "new lambda.Function(this, 'JobCompletion'", "});")
    record_failure = _block(ORCHESTRATION, "new lambda.Function(this, 'FailureRecorderFunction'", "});")

    for name, block in (("job_completion", job_completion), ("record_failure", record_failure)):
        assert re.search(r"\bENVIRONMENT:\s*environment\b", block), f"{name} must set ENVIRONMENT"
    assert re.search(r"dimensionsMap:\s*\{\s*Environment:\s*environment\s*\}", _alarm_block())
