# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code read helpers classify infra errors (P28 R17.3).

``get_pipeline_metadata`` and ``get_regeneration_feedback`` previously
swallowed every exception into {}, so a DynamoDB throttle silently dropped
approval config / regeneration feedback. Narrowed behavior:

- genuine not-found (missing item, ResourceNotFoundException) → {} (unchanged)
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)

Exception-class assertions go through ``_mod.StepFailure`` (the class as the
module under test sees it) plus class-name checks — the class NAME is what
SFN ``ErrorEquals`` matches on, and module-identity is not stable across the
full test session.

Validates: P28 Requirements 17.3
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies (mirrors test_mapping_freshness.py)
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
    "prompt_template_loader": MagicMock(
        load_prompt_template=MagicMock(return_value="mock prompt"),
    ),
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
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


PIPELINE_ID = "pipe-read-path"
RUN_ID = "run-read-path"


class TestGetPipelineMetadata:
    def test_throttle_raises_transient_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        with patch.object(_mod, "pipelines_table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_pipeline_metadata(PIPELINE_ID)
        exc = exc_info.value
        assert type(exc).__name__ == "TransientStepFailure"
        assert exc.error_type == "transient"
        assert exc.step_name == "create_code"
        assert exc.details["operation"] == "get_pipeline_metadata"

    def test_access_denied_raises_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("AccessDeniedException")
        with patch.object(_mod, "pipelines_table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_pipeline_metadata(PIPELINE_ID)
        exc = exc_info.value
        assert type(exc).__name__ == "StepFailure"
        assert exc.error_type == "permanent"

    def test_missing_item_returns_empty_dict(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with patch.object(_mod, "pipelines_table", table):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) == {}

    def test_table_resource_not_found_returns_empty_dict(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ResourceNotFoundException")
        with patch.object(_mod, "pipelines_table", table):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) == {}


class TestGetRegenerationFeedback:
    def test_throttle_raises_transient_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ThrottlingException")
        with patch.object(_mod, "pipeline_runs_table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_regeneration_feedback(RUN_ID)
        exc = exc_info.value
        assert type(exc).__name__ == "TransientStepFailure"
        assert exc.error_type == "transient"
        assert exc.details["operation"] == "get_regeneration_feedback"

    def test_access_denied_raises_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("AccessDenied")
        with patch.object(_mod, "pipeline_runs_table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_regeneration_feedback(RUN_ID)
        exc = exc_info.value
        assert type(exc).__name__ == "StepFailure"
        assert exc.error_type == "permanent"

    def test_missing_item_returns_empty_dict(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with patch.object(_mod, "pipeline_runs_table", table):
            assert _mod.get_regeneration_feedback(RUN_ID) == {}
