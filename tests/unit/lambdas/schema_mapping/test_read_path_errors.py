# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_mapping read helpers classify infra errors (P28 R17.3).

``get_source_schema`` and ``get_pipeline_metadata`` previously swallowed
every exception and returned None, so a DynamoDB throttle produced the
caller's "No source schema found for run_id" error. Narrowed behavior:

- genuine not-found (missing item, ResourceNotFoundException) → None (unchanged)
- throttles / transient infra codes → TransientStepFailure (SFN Retry fires)
- access denied / other ClientErrors → StepFailure (permanent)

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

# Stub heavy dependencies (mirrors test_step_failure_raise.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(
    require_env=lambda name, default=None: default or name,
    get_aws_account_id=lambda: "123456789012",
)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
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


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


RUN_ID = "run-read-path"
PIPELINE_ID = "pipe-read-path"


class TestGetSourceSchema:
    def test_throttle_raises_transient_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ProvisionedThroughputExceededException")
        with patch.object(_mod, "table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_source_schema(RUN_ID)
        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "schema_mapping"
        assert exc_info.value.details["operation"] == "get_source_schema"

    def test_access_denied_raises_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("AccessDeniedException")
        with patch.object(_mod, "table", table), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_source_schema(RUN_ID)
        assert type(exc_info.value).__name__ == "StepFailure"
        assert exc_info.value.error_type == "permanent"

    def test_missing_item_returns_none(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with patch.object(_mod, "table", table):
            assert _mod.get_source_schema(RUN_ID) is None

    def test_table_resource_not_found_returns_none(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ResourceNotFoundException")
        with patch.object(_mod, "table", table):
            assert _mod.get_source_schema(RUN_ID) is None

    def test_valid_schema_still_returned(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {"extracted_schema": '{"type": "object", "properties": {"id": {"type": "string"}}}'}
        }
        with patch.object(_mod, "table", table):
            schema = _mod.get_source_schema(RUN_ID)
        assert schema == {"type": "object", "properties": {"id": {"type": "string"}}}


class TestGetPipelineMetadata:
    def _patched_dynamodb(self, table: MagicMock) -> MagicMock:
        dynamodb = MagicMock()
        dynamodb.Table.return_value = table
        return dynamodb

    def test_throttle_raises_transient_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ThrottlingException")
        with (
            patch.object(_mod, "dynamodb", self._patched_dynamodb(table)),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)
        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "schema_mapping"
        assert exc_info.value.details["operation"] == "get_pipeline_metadata"

    def test_access_denied_raises_step_failure(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("AccessDenied")
        with (
            patch.object(_mod, "dynamodb", self._patched_dynamodb(table)),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)
        assert type(exc_info.value).__name__ == "StepFailure"

    def test_missing_item_returns_none(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with patch.object(_mod, "dynamodb", self._patched_dynamodb(table)):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) is None

    def test_found_item_returned(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_ID, "ai_model": "model-x"}}
        with patch.object(_mod, "dynamodb", self._patched_dynamodb(table)):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) == {"pipeline_id": PIPELINE_ID, "ai_model": "model-x"}


class TestGetAiModelForPipeline:
    """The AI-model lookup no longer swallows infra errors into the default model."""

    def test_throttle_propagates_instead_of_defaulting(self):
        table = MagicMock()
        table.get_item.side_effect = _client_error("ThrottlingException")
        dynamodb = MagicMock()
        dynamodb.Table.return_value = table
        with patch.object(_mod, "dynamodb", dynamodb), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_ai_model_for_pipeline(PIPELINE_ID)
        assert type(exc_info.value).__name__ == "TransientStepFailure"

    def test_missing_metadata_still_falls_back_to_default(self):
        table = MagicMock()
        table.get_item.return_value = {}
        dynamodb = MagicMock()
        dynamodb.Table.return_value = table
        with patch.object(_mod, "dynamodb", dynamodb):
            assert _mod.get_ai_model_for_pipeline(PIPELINE_ID) == _mod.BEDROCK_MODEL_ID
