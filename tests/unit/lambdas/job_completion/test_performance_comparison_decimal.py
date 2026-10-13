# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression: performance comparison survives Decimal estimates from DynamoDB.

``calculate_performance_comparison`` reads ``estimated_duration_minutes`` and
``estimated_cost_per_run`` straight out of the pipeline item. Real DynamoDB
reads (and moto) return those numbers as ``decimal.Decimal``. The comparison
dict then carries a ``Decimal`` (``estimated_duration_minutes`` is stored
raw), so the debug log line ``json.dumps(comparison)`` raised
``TypeError: Object of type Decimal is not JSON serializable`` on EVERY
completion. The bare ``except`` swallowed it and returned ``{}`` — the
performance-comparison metric was silently dropped on every run (non-fatal
but lossy/noisy).

The fix adds ``default=str`` to that ``json.dumps`` (matching the existing
pattern in ``collect_job_metrics``). This test seeds a pipeline item with
``Decimal`` estimates via the shared moto fixture (mirroring real DynamoDB
reads) and asserts the comparison comes back POPULATED — including
``estimated_duration_minutes`` — instead of the empty dict the serialization
error produced before the fix.
"""

import os
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_job_completion_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
sys.path.insert(0, _job_completion_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _seed_pipeline_with_decimal_estimates(pipelines_table, pipeline_id="pipe-1"):
    """Store a pipeline whose estimates are Decimals, exactly as DynamoDB
    returns numeric attributes on read."""
    pipelines_table.put_item(
        Item={
            "pipeline_id": pipeline_id,
            "worker_configuration": {"estimated_duration_minutes": Decimal("10")},
            "cost_information": {"estimated_cost_per_run": Decimal("0.44")},
        }
    )


def _metrics():
    """Actual job metrics as produced by collect_job_metrics (plain floats)."""
    return {"duration_minutes": 12.5, "actual_cost": 0.5}


def test_decimal_estimates_produce_nonempty_comparison(mock_dynamodb_tables):
    """Decimal estimates from DynamoDB → populated comparison (not ``{}``).

    Before the fix, the ``json.dumps(comparison)`` log line raised on the
    ``Decimal`` and the ``except`` returned ``{}``; after it, the comparison
    is returned intact.
    """
    pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    _seed_pipeline_with_decimal_estimates(pipelines_table)

    with patch.object(_mod, "pipelines_table", pipelines_table):
        comparison = _mod.calculate_performance_comparison(_metrics(), "pipe-1")

    # The serialization error path returns {} — a populated dict proves the
    # Decimal no longer trips json.dumps.
    assert comparison != {}
    assert comparison["estimated_duration_minutes"] == Decimal("10")
    assert comparison["actual_duration_minutes"] == 12.5
    # estimated_cost is coerced to float in the comparison payload.
    assert comparison["estimated_cost"] == 0.44
    assert comparison["actual_cost"] == 0.5
    # Variances/accuracy are computed only when a comparison is produced.
    assert "duration_variance_percent" in comparison
    assert "mcp_recommendation_accuracy" in comparison


def test_decimal_estimates_do_not_log_serialization_error(mock_dynamodb_tables, caplog):
    """The fixed path logs the comparison, not the swallowed error."""
    pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    _seed_pipeline_with_decimal_estimates(pipelines_table)

    with patch.object(_mod, "pipelines_table", pipelines_table), caplog.at_level("ERROR"):
        comparison = _mod.calculate_performance_comparison(_metrics(), "pipe-1")

    assert comparison != {}
    assert "Error calculating performance comparison" not in caplog.text
