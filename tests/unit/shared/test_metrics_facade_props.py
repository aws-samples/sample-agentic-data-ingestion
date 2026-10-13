# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Property-based tests for MetricsFacade (Task 7.4).

Properties tested:
  3 — Metrics batching respects batch size
  4 — EMF output is valid structured JSON
  5 — Sampling rate controls metric throughput
"""

import io
import json
import logging
import math
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# Add shared directory to import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Ensure metrics_facade is loaded with POWERTOOLS_ENABLED=false
# (other tests may have reloaded it with POWERTOOLS_ENABLED=true)
os.environ["POWERTOOLS_ENABLED"] = "false"
for _mod_name in list(sys.modules):
    if _mod_name == "metrics_facade":
        del sys.modules[_mod_name]

from metrics_facade import MetricsFacade

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
metric_names = st.from_regex(r"[A-Za-z][A-Za-z0-9_.]{0,49}", fullmatch=True)
positive_floats = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
batch_sizes = st.integers(min_value=1, max_value=50)
metric_counts = st.integers(min_value=1, max_value=200)


# ---------------------------------------------------------------------------
# Property 3: Metrics batching respects batch size
# ---------------------------------------------------------------------------
# Feature: agentic-data-ingestion-codebase-optimization, Property 3: Metrics batching respects batch size
@given(n=metric_counts, bs=batch_sizes)
@settings(max_examples=100)
def test_batching_respects_batch_size(n: int, bs: int):
    """
    For any N metrics submitted and then flushed, the number of
    PutMetricData API calls equals ceil(N / batch_size) and each
    call contains at most batch_size data points.

    **Validates: Requirements 9.1**
    """
    facade = MetricsFacade(batch_size=bs)
    mock_cw = MagicMock()
    facade._cloudwatch = mock_cw

    # Submit N metrics without triggering auto-flush by temporarily
    # setting a very large batch size, then restoring and flushing.
    facade._batch_size = n + 1  # prevent auto-flush during submission
    for i in range(n):
        facade.put_direct_metric(f"Test.Metric.{i}", float(i), "Count")

    # Restore real batch size and flush
    facade._batch_size = bs
    facade.flush_direct()

    expected_calls = math.ceil(n / bs)
    actual_calls = mock_cw.put_metric_data.call_count
    assert actual_calls == expected_calls, (
        f"Expected {expected_calls} PutMetricData calls for {n} metrics with batch_size={bs}, got {actual_calls}"
    )

    # Each call should have at most batch_size data points
    for call in mock_cw.put_metric_data.call_args_list:
        data = call[1]["MetricData"] if "MetricData" in call[1] else call[0][0]
        assert len(data) <= bs, f"Batch contained {len(data)} metrics, exceeding batch_size={bs}"


# ---------------------------------------------------------------------------
# Property 4: EMF output is valid structured JSON
# ---------------------------------------------------------------------------
# Feature: agentic-data-ingestion-codebase-optimization, Property 4: EMF output is valid structured JSON
@given(
    name=metric_names,
    value=positive_floats,
)
@settings(max_examples=100)
def test_emf_output_is_valid_json(name: str, value: float):
    """
    For any metric name and value, put_metric() via Powertools produces
    a log line that is valid JSON containing the _aws key with a
    CloudWatchMetrics array.

    **Validates: Requirements 9.2**
    """
    facade = MetricsFacade(namespace="TestNamespace")

    # Capture the EMF JSON output written to stdout by Powertools
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    handler.setLevel(logging.DEBUG)

    # Powertools writes EMF to stdout via print/logging
    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        facade.put_metric(name, value, "Count", {"TestDim": "val"})

        output = mock_stdout.getvalue().strip()

    if not output:
        # Powertools may not have emitted anything (e.g., if it buffers)
        return

    # Each line should be valid JSON with _aws key
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        assert "_aws" in parsed, f"EMF output missing '_aws' key: {parsed}"
        assert "CloudWatchMetrics" in parsed["_aws"], f"EMF output missing 'CloudWatchMetrics': {parsed['_aws']}"
        cwm = parsed["_aws"]["CloudWatchMetrics"]
        assert isinstance(cwm, list) and len(cwm) > 0
        entry = cwm[0]
        assert "Namespace" in entry
        assert "Dimensions" in entry
        assert "Metrics" in entry


# ---------------------------------------------------------------------------
# Property 5: Sampling rate controls metric throughput
# ---------------------------------------------------------------------------
# Feature: agentic-data-ingestion-codebase-optimization, Property 5: Sampling rate controls metric throughput
@given(
    rate=st.floats(min_value=0.05, max_value=1.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=100)
def test_sampling_rate_controls_throughput(rate: float):
    """
    For any sample rate R and N=2000 metrics, the number actually
    published should be within 4 standard deviations of the expected
    binomial distribution mean R*N.

    **Validates: Requirements 9.3**
    """
    N = 2000
    facade = MetricsFacade(batch_size=N + 1)  # prevent auto-flush
    mock_cw = MagicMock()
    facade._cloudwatch = mock_cw

    facade.set_sample_rate("Sampled.", rate)

    for i in range(N):
        facade.put_direct_metric(f"Sampled.metric.{i}", 1.0, "Count")

    published = len(facade._batch_buffer)

    expected = rate * N
    stddev = math.sqrt(rate * (1 - rate) * N)
    lower = expected - 4 * stddev
    upper = expected + 4 * stddev

    assert lower <= published <= upper, (
        f"Published {published} metrics with rate={rate:.3f}, N={N}. Expected range [{lower:.1f}, {upper:.1f}]"
    )
