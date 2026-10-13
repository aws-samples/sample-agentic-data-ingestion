# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: handler failures raise StepFailure carrying a deterministic
``error-{run_id}`` correlation id in details.

P28 R1.1: create_code raises StepFailure instead of returning
``{"statusCode": 500, ...}`` so the Step Functions task actually fails.

Validates: Requirement 5 — Placeholder Failure ID Fixes; P28 Requirement 1.1
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

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
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _create_code_module


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _create_code_module
    yield


def test_failure_raises_step_failure_with_run_id_correlation():
    """Force exception in lambda_handler, verify it raises StepFailure carrying error-{run_id}."""
    run_id = "run-abc-123"
    event = {
        "run_id": run_id,
        "pipeline_id": "test-pipeline-fail",
    }

    # Force an exception by making get_pipeline_metadata raise
    with (
        patch.object(
            _create_code_module,
            "get_pipeline_metadata",
            side_effect=RuntimeError("forced test failure"),
        ),
        patch.object(_create_code_module, "pipeline_runs_table", MagicMock()),
        patch.object(_create_code_module, "publish_pipeline_state_change"),
        pytest.raises(_create_code_module.StepFailure) as exc_info,
    ):
        _create_code_module.lambda_handler(event, None)

    assert exc_info.value.step_name == "create_code"
    assert exc_info.value.message == "forced test failure"
    assert exc_info.value.details["code_version_id"] == f"error-{run_id}"
    assert exc_info.value.details["run_id"] == run_id


def test_failure_correlation_id_uses_unknown_when_no_run_id():
    """When run_id is missing from event, the correlation id should use 'unknown'."""
    event = {
        "pipeline_id": "test-pipeline-no-run-id",
    }

    with (
        patch.object(
            _create_code_module,
            "get_pipeline_metadata",
            side_effect=RuntimeError("forced test failure"),
        ),
        patch.object(_create_code_module, "pipeline_runs_table", MagicMock()),
        patch.object(_create_code_module, "publish_pipeline_state_change"),
        pytest.raises(_create_code_module.StepFailure) as exc_info,
    ):
        _create_code_module.lambda_handler(event, None)

    assert exc_info.value.details["code_version_id"] == "error-unknown"


def test_failure_id_is_deterministic():
    """Given the same run_id, two invocations produce identical correlation ids (Property 5)."""
    run_id = "deterministic-run-456"
    event = {
        "run_id": run_id,
        "pipeline_id": "test-pipeline-det",
    }

    failures = []
    for _ in range(2):
        with (
            patch.object(
                _create_code_module,
                "get_pipeline_metadata",
                side_effect=RuntimeError("forced test failure"),
            ),
            patch.object(_create_code_module, "pipeline_runs_table", MagicMock()),
            patch.object(_create_code_module, "publish_pipeline_state_change"),
            pytest.raises(_create_code_module.StepFailure) as exc_info,
        ):
            _create_code_module.lambda_handler(event, None)
        failures.append(exc_info.value)

    assert failures[0].details["code_version_id"] == failures[1].details["code_version_id"]
    assert failures[0].details["code_version_id"] == f"error-{run_id}"
