# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: duration_ms uses actual elapsed time, not hardcoded 3500.

Validates: Requirement 4 — Hardcoded Duration Metric Fixes
"""

import os
import sys
import time as _real_time_module
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Stub heavy dependencies
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(
        extract_code_only=MagicMock(return_value="# code"),
        validate_generated_code=MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={
                "required_params": [],
                "optional_params": [],
                "parameter_defaults": {},
            }
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _make_happy_path_patches(capture_dict):
    """Return a list of context-manager patches for a successful handler run."""

    def _capture_publish(**kwargs):
        capture_dict.update(kwargs.get("metrics", {}))

    return [
        patch.object(_mod, "publish_pipeline_state_change", side_effect=_capture_publish),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(
            _mod,
            "pipeline_runs_table",
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "extracted_schema": '{"properties": {}}',
                        }
                    }
                )
            ),
        ),
        patch.object(
            _mod,
            "mappings_table",
            # p33 task 4.6 harness amendment, NOT an assertion change. The two
            # schema hashes were absent here and the handler substituted
            # `str(uuid.uuid4())` for each; requirement 2.9 makes their absence a
            # StepFailure instead, because a generated identifier matches no
            # schema. `store_mapping_separately` writes both on every real record
            # (`schema_mapping/lambda_function.py:1166-1169`), so the fixture was
            # under-specified relative to the live writer. This file is about
            # duration metrics, so it needs a mapping record that resolves.
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "mapping_id": "m1",
                            "source_schema_hash": "src-hash-m1",
                            "target_schema_hash": "tgt-hash-m1",
                        }
                    }
                )
            ),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(
            _mod,
            "generate_code",
            return_value="# code\ndf = spark.read.json('s3://b/k')\n",
        ),
        patch.object(
            _mod,
            "validate_generated_code",
            return_value={"is_valid": True, "errors": [], "summary": {}},
        ),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ]


class _FakeTimeModule:
    """Drop-in replacement for the ``time`` module used inside lambda_handler.

    The handler does ``import time as _time`` then calls ``_time.time()``.
    We intercept only those calls while letting the stdlib logging framework
    use the real ``time.time()`` (it imports ``time`` at module level, so it
    keeps its own reference).
    """

    def __init__(self, start: float, end: float):
        self._call_count = 0
        self._start = start
        self._end = end

    def time(self):
        self._call_count += 1
        # First call is _metrics_start; all subsequent are "now"
        if self._call_count == 1:
            return self._start
        return self._end

    def __getattr__(self, name):
        # Delegate anything else to the real time module
        return getattr(_real_time_module, name)


def test_duration_ms_uses_actual_elapsed_time():
    """Verify duration_ms equals the mocked elapsed time, not 3500."""
    captured = {}
    fake_time = _FakeTimeModule(start=100.0, end=102.5)  # 2500 ms

    event = {
        "run_id": "test-run-123",
        "pipeline_id": "test-pipe-456",
        "mapping_id": "test-map-789",
    }

    patches = _make_happy_path_patches(captured)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patch.dict("sys.modules", {"time": fake_time}),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["duration_ms"] == 2500
    assert captured["duration_ms"] != 3500


def test_duration_ms_is_non_negative():
    """Verify duration_ms is a non-negative integer (Property 4)."""
    captured = {}
    fake_time = _FakeTimeModule(start=50.0, end=50.001)  # 1 ms

    event = {
        "run_id": "test-run-nn",
        "pipeline_id": "test-pipe-nn",
        "mapping_id": "test-map-nn",
    }

    patches = _make_happy_path_patches(captured)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patch.dict("sys.modules", {"time": fake_time}),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["duration_ms"] >= 0
    assert isinstance(captured["duration_ms"], int)
