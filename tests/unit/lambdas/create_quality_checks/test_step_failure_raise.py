# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_quality_checks failure path raises StepFailure (P28 R1.1).

The handler must raise (so the Step Functions task fails and routes to
UpdateFailureState via addCatch) instead of returning an error payload that
SFN would treat as a successful task completion. Failure-path side effects
(error-state DynamoDB write) still happen before the raise.

Validates: P28 Requirements 1.1
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_quality_checks",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _load_module():
    """Load the create_quality_checks lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_quality_checks_lf_p28",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["create_quality_checks_lf_p28"] = mod
    spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def test_missing_required_ids_raises_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    mock_runs_table = mock.MagicMock()
    with (
        mock.patch.object(_lf, "pipeline_runs_table", mock_runs_table),
        pytest.raises(_lf.StepFailure) as exc_info,
    ):
        _lf.lambda_handler({}, None)

    exc = exc_info.value
    assert exc.step_name == "create_quality_checks"
    assert exc.error_type == "permanent"
    assert "run_id and pipeline_id are required" in exc.message
    assert isinstance(exc.__cause__, ValueError)

    # Error-state row written before the raise (side effect preserved).
    mock_runs_table.put_item.assert_called_once()
    item = mock_runs_table.put_item.call_args.kwargs["Item"]
    assert item["status"] == "failed"
    assert item["step_name"] == "create_quality_checks"


def test_error_write_failure_still_raises_step_failure():
    """Even when the DynamoDB error write itself fails, the handler raises."""
    mock_runs_table = mock.MagicMock()
    mock_runs_table.put_item.side_effect = Exception("ProvisionedThroughputExceeded")

    with (
        mock.patch.object(_lf, "pipeline_runs_table", mock_runs_table),
        pytest.raises(_lf.StepFailure) as exc_info,
    ):
        _lf.lambda_handler({"pipeline_id": "pipe-1"}, None)

    exc = exc_info.value
    assert exc.step_name == "create_quality_checks"
    assert exc.details["pipeline_id"] == "pipe-1"
