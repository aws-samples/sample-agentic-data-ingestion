# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: test_code failure path raises StepFailure (P28 R1.1).

The handler must raise (so the Step Functions task fails and Retry/Catch
fire) instead of returning ``{"statusCode": 500, ...}`` which SFN would
treat as a successful task completion.

Validates: P28 Requirements 1.1
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

# Add Lambda dir and shared dir to path
_test_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _test_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies before import
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "code_interpreter_runner": MagicMock(CodeInterpreterTestRunner=MagicMock()),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _fake_dynamodb() -> MagicMock:
    """A ``dynamodb`` stand-in whose pipelines table returns a keyless record.

    p33 task 4.3 plumbing, not an assertion change: the handler now reads the
    pipeline record — and raises StepFailure when it is absent (requirement
    2.8) — *before* the code-generation lookup this test is about. Without this
    stub the handler still raises StepFailure, but for the wrong reason, and the
    message assertion below would be what caught it. The record exists and
    carries no target schema (requirement 2.6's legitimate state), so control
    reaches the code-generation lookup exactly as before.
    """
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1"}}
    dynamodb = MagicMock()
    dynamodb.Table.return_value = pipelines_table
    return dynamodb


def test_missing_code_generation_step_raises_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    mock_runs_table = MagicMock()
    mock_runs_table.get_item.return_value = {}  # no Item → code generation not found

    event = {"run_id": "run-xyz", "pipeline_id": "pipe-1"}

    with (
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "dynamodb", _fake_dynamodb()),
        patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler(event, None)

    exc = exc_info.value
    assert exc.step_name == "test_code"
    assert exc.error_type == "permanent"
    assert "Code generation step not found" in exc.message
    assert exc.details["run_id"] == "run-xyz"
    assert exc.details["pipeline_id"] == "pipe-1"
    assert isinstance(exc.__cause__, ValueError)
