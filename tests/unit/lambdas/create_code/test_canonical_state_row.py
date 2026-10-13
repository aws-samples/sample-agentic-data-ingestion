# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code state rows carry the canonical step name (P28 R8.2).

Task 14.2 retires the legacy ``"CodeGeneration"`` state-row variant — the
failure path (exercised here end-to-end through the handler) must write its
row keyed ``(run_id, "create_code")`` with the shared step_sequence.

Validates: P28 Requirements 8.2, 8.5
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies (mirrors test_failure_ids.py)
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
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

from step_names import STEP_SEQUENCE, StepName


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def test_failure_state_row_uses_canonical_create_code_name():
    """The failure row is keyed (run_id, 'create_code') — never 'CodeGeneration'."""
    runs_table = MagicMock()
    event = {"run_id": "run-14-2", "pipeline_id": "pipe-1"}

    with (
        patch.object(_mod, "get_pipeline_metadata", side_effect=RuntimeError("forced")),
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "publish_pipeline_state_change"),
        pytest.raises(_mod.StepFailure),
    ):
        _mod.lambda_handler(event, None)

    items = [c.kwargs["Item"] for c in runs_table.put_item.call_args_list]
    assert len(items) == 1
    item = items[0]
    assert item["step_name"] == StepName.CREATE_CODE
    assert item["step_name"] == "create_code"
    assert item["step_sequence"] == STEP_SEQUENCE[StepName.CREATE_CODE]
    assert item["status"] == "failed"
    assert item["run_id"] == "run-14-2"
