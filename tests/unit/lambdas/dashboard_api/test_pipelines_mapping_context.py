# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for edit_pipeline mapping_context support and ExpressionAttributeNames bugfix.

Validates: Requirements 2.3, 2.4, 2.5, 2.6
Tests mapping_context SET/REMOVE via edit_pipeline, length validation,
and the ExpressionAttributeNames fix (empty dict instead of None).
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "PIPELINE_TRIGGER_ARN": "",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

# P21 Task 7.2 — edit_pipeline now lives in handlers.pipelines_crud.
try:
    from handlers import pipelines_crud as pipelines
    from handlers.pipelines import edit_pipeline
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-mapping-context-pipeline"

# ---------------------------------------------------------------------------
# Mock tables
# ---------------------------------------------------------------------------
mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_s3_client = MagicMock()
mock_dynamodb = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
    """Reset all mocks before each test and wire them into the module."""
    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_executions_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_s3_client.reset_mock()
    mock_dynamodb.reset_mock()

    mock_state_table.query.return_value = {"Items": []}
    mock_executions_table.query.return_value = {"Items": []}

    pipelines.pipelines_table = mock_pipelines_table
    pipelines.state_table = mock_state_table
    pipelines.executions_table = mock_executions_table
    pipelines.approvals_table = mock_approvals_table
    pipelines.dynamodb = mock_dynamodb
    pipelines.s3_client = mock_s3_client

    original_require_env = pipelines.require_env

    def _patched_require_env(name, default=None):
        overrides = {
            "UPLOAD_BUCKET": "agentic-data-ingestion-source",
            "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "PIPELINE_TRIGGER_ARN": "",
        }
        if name in overrides:
            return overrides[name]
        return original_require_env(name, default)

    pipelines.require_env = _patched_require_env
    yield
    pipelines.require_env = original_require_env


# ---------------------------------------------------------------------------
# mapping_context tests
# ---------------------------------------------------------------------------


class TestEditPipelineMappingContext:
    """Validates: Requirements 2.3, 2.4, 2.5, 2.6"""

    def test_set_mapping_context(self):
        """Setting mapping_context persists the value via SET expression."""
        body = json.dumps({"mapping_context": "Map fname to first_name"})

        result = edit_pipeline(PIPELINE_ID, body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body_resp["success"] is True

        update_call = mock_pipelines_table.update_item.call_args
        assert ":mc" in update_call[1]["ExpressionAttributeValues"]
        assert update_call[1]["ExpressionAttributeValues"][":mc"] == "Map fname to first_name"
        assert "mapping_context = :mc" in update_call[1]["UpdateExpression"]

    def test_clear_mapping_context_removes_attribute(self):
        """Empty string mapping_context triggers REMOVE expression."""
        body = json.dumps({"mapping_context": ""})

        result = edit_pipeline(PIPELINE_ID, body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body_resp["success"] is True

        update_call = mock_pipelines_table.update_item.call_args
        update_expression = update_call[1]["UpdateExpression"]
        assert "REMOVE mapping_context" in update_expression
        # mapping_context value placeholder should NOT be in expr_values
        assert ":mc" not in update_call[1]["ExpressionAttributeValues"]

    def test_mapping_context_length_validation(self):
        """mapping_context exceeding 2000 chars returns 400."""
        body = json.dumps({"mapping_context": "x" * 2001})

        result = edit_pipeline(PIPELINE_ID, body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "2000 character limit" in body_resp["error"]
        mock_pipelines_table.update_item.assert_not_called()

    def test_mapping_context_at_limit_accepted(self):
        """mapping_context at exactly 2000 chars is accepted."""
        body = json.dumps({"mapping_context": "x" * 2000})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        update_call = mock_pipelines_table.update_item.call_args
        assert update_call[1]["ExpressionAttributeValues"][":mc"] == "x" * 2000


# ---------------------------------------------------------------------------
# ExpressionAttributeNames bugfix tests
# ---------------------------------------------------------------------------


class TestEditPipelineExpressionAttributeNames:
    """Validates: ExpressionAttributeNames bugfix — empty dict instead of None."""

    def test_no_expression_attribute_names_omitted_when_empty(self):
        """When no reserved-word fields are used, ExpressionAttributeNames is omitted entirely."""
        body = json.dumps({"pipeline_name": "new-name"})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        update_call = mock_pipelines_table.update_item.call_args
        # Must be omitted, not passed as empty dict (DynamoDB rejects empty ExpressionAttributeNames)
        assert "ExpressionAttributeNames" not in update_call[1]

    def test_with_expression_attribute_names_passes_dict(self):
        """When reserved-word fields are used, ExpressionAttributeNames has entries."""
        body = json.dumps({"status": "active"})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        update_call = mock_pipelines_table.update_item.call_args
        assert update_call[1]["ExpressionAttributeNames"] == {"#status": "status"}


# ---------------------------------------------------------------------------
# Nested map attribute if_not_exists tests
# ---------------------------------------------------------------------------


class TestEditPipelineNestedMapAttributes:
    """Validates: target_config/approval_config partial update with separate if_not_exists call."""

    def test_target_config_dict_uses_path_based_set(self):
        """target_config dict uses path-based SET with aliased keys (reserved-word safe)."""
        body = json.dumps({"target_config": {"database": "my_db"}})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        # First call: if_not_exists initialization, second call: the main update
        assert mock_pipelines_table.update_item.call_count == 2
        main_call = mock_pipelines_table.update_item.call_args_list[1]
        update_expression = main_call[1]["UpdateExpression"]
        # Nested key is aliased via ExpressionAttributeNames so DynamoDB reserved
        # words (e.g. "type", "database") are legal as document-path segments.
        assert "target_config.#tc_database = :tc_database" in update_expression
        assert main_call[1]["ExpressionAttributeNames"]["#tc_database"] == "database"
        assert main_call[1]["ExpressionAttributeValues"][":tc_database"] == "my_db"

    def test_approval_config_dict_uses_path_based_set(self):
        """approval_config dict uses path-based SET with aliased keys (reserved-word safe)."""
        body = json.dumps({"approval_config": {"enabled": True}})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        assert mock_pipelines_table.update_item.call_count == 2
        main_call = mock_pipelines_table.update_item.call_args_list[1]
        update_expression = main_call[1]["UpdateExpression"]
        assert "approval_config.#ac_enabled = :ac_enabled" in update_expression
        assert main_call[1]["ExpressionAttributeNames"]["#ac_enabled"] == "enabled"
        assert main_call[1]["ExpressionAttributeValues"][":ac_enabled"] is True
