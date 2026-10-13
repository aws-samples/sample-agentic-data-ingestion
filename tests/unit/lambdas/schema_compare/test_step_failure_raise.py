# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_compare failure path raises StepFailure (P28 R1.1).

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
    "agentic_data_ingestion_schema_compare",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_p28",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p28"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def test_missing_run_id_raises_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    with (
        mock.patch.object(_lf, "table", mock.MagicMock()),
        mock.patch.object(_lf, "pipelines_table", mock.MagicMock()),
        pytest.raises(_lf.StepFailure) as exc_info,
    ):
        _lf.lambda_handler({}, None)

    exc = exc_info.value
    assert exc.step_name == "schema_compare"
    assert exc.error_type == "permanent"
    assert "Run ID not provided" in exc.message
    assert isinstance(exc.__cause__, ValueError)


def test_missing_pipeline_metadata_writes_error_state_then_raises():
    """Failure-path side effects (error-state write) are kept, then the raise."""
    mock_table = mock.MagicMock()
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": "run-1",
            "extracted_schema": '{"type": "object", "properties": {}}',
            "schema_hash": "hash-1",
            "original_pipeline_id": "pipe-1",
        }
    }
    mock_pipelines = mock.MagicMock()
    mock_pipelines.get_item.return_value = {}  # no Item → metadata not found

    with (
        mock.patch.object(_lf, "table", mock_table),
        mock.patch.object(_lf, "pipelines_table", mock_pipelines),
        pytest.raises(_lf.StepFailure) as exc_info,
    ):
        _lf.lambda_handler({"run_id": "run-1"}, None)

    exc = exc_info.value
    assert exc.step_name == "schema_compare"
    assert "Pipeline metadata not found" in exc.message
    assert exc.details["run_id"] == "run-1"

    # Failure-state row written before the raise (side effect preserved).
    # P28 R8.5 (task 14.2): the record carries the table's FULL key —
    # the old item lacked run_id (100% ValidationException, silently
    # swallowed) — plus the canonical step name, "failed" status, and the
    # shared step_sequence.
    error_writes = [
        c for c in mock_table.put_item.call_args_list if c.kwargs.get("Item", {}).get("status") == "failed"
    ]
    assert len(error_writes) == 1
    item = error_writes[0].kwargs["Item"]
    assert item["run_id"] == "run-1"
    assert item["step_name"] == "schema_compare"
    assert item["step_sequence"] == 3
    assert item["pipeline_id"] == "pipe-1"
