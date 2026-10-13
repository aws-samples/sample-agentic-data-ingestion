# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for edit_pipeline mapping_context support.

Feature: p15-rerun-mapping, Property 4: Edit endpoint mapping_context support

Uses Hypothesis to verify that mapping_context values are correctly persisted
or removed via the edit_pipeline handler across randomised inputs.

**Validates: Requirements 2.3, 2.4**
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies before importing handlers
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------
PIPELINE_ID = "prop-edit-pipeline"

mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_s3_client = MagicMock()
mock_dynamodb = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
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
# Feature: p15-rerun-mapping, Property 4: Edit endpoint mapping_context support
# ---------------------------------------------------------------------------


class TestEditMappingContextProperty:
    """Property 4: Edit endpoint mapping_context support.

    For any non-empty string (≤ 2000 chars) sent as mapping_context in an
    edit_pipeline request, the pipeline record's mapping_context attribute
    SHALL be updated to that string. For an empty string, the attribute
    SHALL be removed.

    **Validates: Requirements 2.3, 2.4**
    """

    @given(context=st.text(min_size=1, max_size=2000))
    @settings(max_examples=100)
    def test_non_empty_context_persisted(self, context):
        """Non-empty mapping_context is SET on the pipeline record."""
        body = json.dumps({"mapping_context": context})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200

        update_call = mock_pipelines_table.update_item.call_args
        assert update_call is not None, "update_item should have been called"
        assert ":mc" in update_call[1]["ExpressionAttributeValues"]
        assert update_call[1]["ExpressionAttributeValues"][":mc"] == context
        assert "mapping_context = :mc" in update_call[1]["UpdateExpression"]

    @given(context=st.just(""))
    @settings(max_examples=100)
    def test_empty_context_removes_attribute(self, context):
        """Empty mapping_context triggers REMOVE on the pipeline record."""
        body = json.dumps({"mapping_context": context})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200

        update_call = mock_pipelines_table.update_item.call_args
        assert update_call is not None, "update_item should have been called"
        update_expression = update_call[1]["UpdateExpression"]
        assert "REMOVE mapping_context" in update_expression
        # :mc placeholder should NOT be in expression values
        assert ":mc" not in update_call[1]["ExpressionAttributeValues"]

    @given(context=st.text(min_size=2001, max_size=3000))
    @settings(max_examples=100)
    def test_exceeding_length_rejected(self, context):
        """mapping_context exceeding 2000 chars returns 400."""
        body = json.dumps({"mapping_context": context})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 400
        resp_body = json.loads(result["body"])
        assert "2000 character limit" in resp_body["error"]
        mock_pipelines_table.update_item.assert_not_called()
