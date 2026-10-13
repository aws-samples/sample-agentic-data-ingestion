# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: duration_ms uses actual elapsed time, not hardcoded 2500.

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
_schema_mapping_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_schema_mapping",
    )
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Stub heavy dependencies
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


class _FakeTimeModule:
    """Drop-in for ``time`` module used inside lambda_handler.

    The handler has two time references:
    1. ``time.time()`` — the module-level ``import time`` (patched via
       ``lambda_function.time``)
    2. ``_time.time()`` — the handler-local ``import time as _time``
       (patched via ``sys.modules["time"]``)

    First call → start value; all subsequent calls → end value.
    """

    def __init__(self, start: float, end: float):
        self._call_count = 0
        self._start = start
        self._end = end

    def time(self):
        self._call_count += 1
        if self._call_count == 1:
            return self._start
        return self._end

    def __getattr__(self, name):
        return getattr(_real_time_module, name)


def _make_happy_path_patches(capture_dict):
    """Return context-manager patches for a successful handler run."""

    def _capture_publish(**kwargs):
        capture_dict.update(kwargs.get("metrics", {}))

    mock_table = MagicMock()
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": "r1",
            "step_name": "schema_extract",
            "original_pipeline_id": "p1",
            "extracted_schema": ('{"type":"object","properties":{"id":{"type":"string"}}}'),
        }
    }

    return [
        patch.object(
            _mod,
            "publish_pipeline_state_change",
            side_effect=_capture_publish,
        ),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "table", mock_table),
        patch.object(_mod, "mappings_table", MagicMock()),
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "pipeline_id": "p1",
                "target_schema_s3_key": "t/s.json",
                "target_schema_bucket": "agentic-data-ingestion-target-schemas",
                "approval_config": {"schema_mapping_enabled": False},
            },
        ),
        patch.object(
            _mod,
            "get_target_schema",
            return_value=(
                {"type": "object", "properties": {"id": {"type": "string"}}},
                "hash456",
            ),
        ),
        patch.object(
            _mod,
            "generate_mapping_with_bedrock",
            return_value={
                "mappings": [
                    {
                        "target_field": "id",
                        "source_field": "id",
                        "confidence": 0.95,
                    }
                ],
                "unmapped_source_fields": [],
                "unmapped_target_fields": [],
                "mapping_summary": "test",
            },
        ),
        patch.object(_mod, "determine_approval_requirement", return_value=False),
        patch.object(_mod, "memory_client", None),
    ]


def test_duration_ms_uses_actual_elapsed_time():
    """Verify duration_ms equals the mocked elapsed time, not 2500."""
    captured = {}
    fake_time = _FakeTimeModule(start=200.0, end=201.5)  # 1500ms

    event = {
        "run_id": "test-run-dur",
        "pipeline_id": "test-pipe-dur",
        "source_schema_hash": "hash123",
    }

    patches = _make_happy_path_patches(captured)
    # Patch BOTH the module-level ``time`` reference (used by
    # ``_start_time = time.time()``) AND sys.modules["time"] (used by
    # the handler-local ``import time as _time``).
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
        patch.object(_mod, "time", fake_time),
        patch.dict("sys.modules", {"time": fake_time}),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["duration_ms"] == 1500
    assert captured["duration_ms"] != 2500


def test_duration_ms_is_non_negative():
    """Verify duration_ms is a non-negative integer (Property 4)."""
    captured = {}
    fake_time = _FakeTimeModule(start=50.0, end=50.002)  # 2ms

    event = {
        "run_id": "test-run-nn",
        "pipeline_id": "test-pipe-nn",
        "source_schema_hash": "hash123",
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
        patch.object(_mod, "time", fake_time),
        patch.dict("sys.modules", {"time": fake_time}),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert captured["duration_ms"] >= 0
    assert isinstance(captured["duration_ms"], int)
