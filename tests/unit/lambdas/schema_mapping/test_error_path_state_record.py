# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_mapping failure path reliably writes its state record (P28 R8.4).

The old error path was doubly broken: gated on a ``pipeline_key`` variable
that was never assigned (always None → unreachable), and — had it ever run —
the item lacked the state table's ``run_id`` partition key, so the write was
a guaranteed ValidationException. The rewrite must:

- ALWAYS write the failed state record keyed ``(run_id, "schema_mapping")``
  with status/step_sequence consistent with the sibling step Lambdas,
- write it BEFORE the StepFailure propagates (R1's Catch/RecordFailure then
  supersedes/uses it),
- never mask the original failure with a state-write error, and
- keep the StepFailure raise semantics intact (an already-structured
  StepFailure is re-raised as-is, never re-wrapped).

Validates: P28 Requirements 8.4, 8.5
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add Lambda dir and shared dir to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub out Lambda-layer dependencies (mirrors the sibling schema_mapping tests)
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

_spec = importlib.util.spec_from_file_location(
    "schema_mapping_error_path_lf",
    os.path.join(_schema_mapping_dir, "lambda_function.py"),
)
_mod = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_mod)

    from step_errors import StepFailure
    from step_names import STEP_SEQUENCE, StepName
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

RUN_ID = "run-r84"


@pytest.fixture(autouse=True)
def _mock_tables():
    """Replace the module's DynamoDB tables with mocks for every test."""
    _mod.table = MagicMock()
    _mod.mappings_table = MagicMock()
    _mod.pipelines_table = MagicMock()
    _mod.approvals_table = MagicMock()
    # PipelineMetrics stubbed to None at import; keep it that way defensively
    _mod.PipelineMetrics = None
    yield


def _failure_state_writes():
    """All put_item Items written to the state table mock."""
    return [c.kwargs["Item"] for c in _mod.table.put_item.call_args_list]


def test_failure_writes_state_record_before_stepfailure_propagates():
    """A mapping failure writes (run_id, schema_mapping) BEFORE raising."""
    # get_source_schema returning None triggers the handler's ValueError
    _mod.table.get_item.return_value = {}

    with pytest.raises(StepFailure) as exc_info:
        _mod.lambda_handler({"run_id": RUN_ID, "pipeline_id": "pipe-1"}, None)

    writes = _failure_state_writes()
    assert len(writes) == 1
    item = writes[0]
    assert item["run_id"] == RUN_ID
    assert item["step_name"] == StepName.SCHEMA_MAPPING
    assert item["step_name"] == "schema_mapping"
    assert item["status"] == "failed"
    assert item["step_sequence"] == STEP_SEQUENCE[StepName.SCHEMA_MAPPING]
    assert "No source schema found" in item["error_message"]
    assert item["error_type"] == "ProcessingError"

    # The raise still carries the schema_mapping attribution
    assert exc_info.value.step_name == "schema_mapping"


def test_inner_stepfailure_is_reraised_as_is_with_honest_record_message():
    """An already-structured StepFailure is never re-wrapped, and the state
    record carries its human-readable message (not its JSON serialization)."""
    inner = StepFailure("schema_mapping", "get_source_schema failed: ThrottlingException")
    _mod.table.get_item.side_effect = inner

    with pytest.raises(StepFailure) as exc_info:
        _mod.lambda_handler({"run_id": RUN_ID, "pipeline_id": "pipe-1"}, None)

    assert exc_info.value is inner  # re-raised as-is, never re-wrapped

    writes = _failure_state_writes()
    assert len(writes) == 1
    message = writes[0]["error_message"]
    assert "get_source_schema failed: ThrottlingException" in message
    # The record must not embed the StepFailure JSON serialization
    assert '"step_name"' not in message


def test_state_write_failure_never_masks_the_original_error():
    """A DynamoDB write error is logged, not raised — the StepFailure wins."""
    _mod.table.get_item.return_value = {}
    _mod.table.put_item.side_effect = Exception("Dynamo down")

    with pytest.raises(StepFailure) as exc_info:
        _mod.lambda_handler({"run_id": RUN_ID, "pipeline_id": "pipe-1"}, None)

    assert exc_info.value.step_name == "schema_mapping"
    assert "No source schema found" in exc_info.value.message


def test_missing_run_id_still_writes_a_record_under_unknown():
    """Even a malformed event produces a state record (run_id 'unknown'),
    matching the sibling step Lambdas' failure rows."""
    with pytest.raises(StepFailure):
        _mod.lambda_handler({}, None)

    writes = _failure_state_writes()
    assert len(writes) == 1
    assert writes[0]["run_id"] == "unknown"
    assert writes[0]["step_name"] == "schema_mapping"
    assert writes[0]["status"] == "failed"
