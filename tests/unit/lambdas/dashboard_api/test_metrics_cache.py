# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: metrics caching — returns cached data when fresh (< 5 min),
refreshes when stale (> 5 min).

Validates: AC-9.3
"""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)


SAMPLE_METRICS = {
    "performance": {"lines_ingested_24h": 100},
    "stages": {},
    "infrastructure": {},
    "period": "24h",
    "generated_at": "2024-01-01T00:00:00+00:00",
}


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_returns_cached_data_when_fresh(mock_cw, mock_cache):
    """When cache TTL has not expired, return cached data without querying CloudWatch."""
    future_ttl = int(time.time()) + 600  # 10 minutes from now
    mock_cache.get_item.return_value = {
        "Item": {
            "run_id": "metrics_cache#24h",
            "step_name": "cache",
            "data": json.dumps(SAMPLE_METRICS),
            "ttl": future_ttl,
        }
    }

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})
    body = json.loads(result["body"])

    assert result["statusCode"] == 200
    assert body["performance"]["lines_ingested_24h"] == 100

    # CloudWatch should NOT have been called
    mock_cw.get_metric_data.assert_not_called()


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_refreshes_when_cache_is_stale(mock_cw, mock_cache):
    """When cache TTL has expired, query CloudWatch and update cache."""
    past_ttl = int(time.time()) - 60  # 1 minute ago (expired)
    mock_cache.get_item.return_value = {
        "Item": {
            "run_id": "metrics_cache#24h",
            "step_name": "cache",
            "data": json.dumps(SAMPLE_METRICS),
            "ttl": past_ttl,
        }
    }

    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "24h"}})

    assert result["statusCode"] == 200

    # CloudWatch SHOULD have been called since cache was stale
    assert mock_cw.get_metric_data.call_count >= 3

    # Cache should have been updated
    mock_cache.put_item.assert_called()


@patch("handlers.metrics._cache_table")
@patch("handlers.metrics.cloudwatch_client")
def test_refreshes_when_no_cache_exists(mock_cw, mock_cache):
    """When no cache entry exists, query CloudWatch."""
    mock_cache.get_item.return_value = {}  # No Item key

    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}

    from handlers.metrics import get_comprehensive_metrics

    result = get_comprehensive_metrics({"queryStringParameters": {"period": "1h"}})

    assert result["statusCode"] == 200
    assert mock_cw.get_metric_data.call_count >= 3
    mock_cache.put_item.assert_called()


@patch("handlers.metrics._cache_table")
def test_cache_write_sets_5_minute_ttl(mock_cache):
    """_cache_metrics writes with a TTL ~5 minutes in the future."""
    from handlers.metrics import _cache_metrics

    before = int(time.time())
    _cache_metrics("24h", SAMPLE_METRICS)
    after = int(time.time())

    mock_cache.put_item.assert_called_once()
    item = mock_cache.put_item.call_args.kwargs.get("Item") or mock_cache.put_item.call_args[1].get("Item")
    ttl = item["ttl"]

    # TTL should be between now+295 and now+305 (5 min ± 5s tolerance)
    assert before + 295 <= ttl <= after + 305


@patch("handlers.metrics._cache_table")
def test_cache_read_failure_returns_none(mock_cache):
    """If DynamoDB read fails, _get_cached_metrics returns None (non-fatal)."""
    mock_cache.get_item.side_effect = Exception("DynamoDB error")

    from handlers.metrics import _get_cached_metrics

    result = _get_cached_metrics("24h")
    assert result is None
