# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for the rerun_mapping handler.

Feature: p15-rerun-mapping

Uses Hypothesis to verify correctness properties across randomised inputs
for context merging, persistence round-trips, length validation,
model override passthrough, and model non-persistence.

**Validates: Requirements 1.4, 1.5, 2.1, 2.5, 2.6, 3.2, 3.3, 3.4, 5.3, 5.4, 5.6**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

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
    "PIPELINE_TRIGGER_ARN": "arn:aws:lambda:us-east-1:123456789012:function:trigger",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

try:
    from handlers import (
        pipelines,
        pipelines_mappings,
    )
    from handlers.pipelines import rerun_mapping
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
PIPELINE_KEY = "prop-test-pipeline"

mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_mappings_table = MagicMock()
mock_lambda_client = MagicMock()


def _make_event(body: dict) -> dict:
    return {"body": json.dumps(body)}


def _lambda_response(payload: dict, status: int = 200):
    resp_payload = MagicMock()
    resp_payload.read.return_value = json.dumps({**payload, "statusCode": status}).encode()
    return {"Payload": resp_payload, "StatusCode": 200}


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_mappings_table.reset_mock()
    mock_lambda_client.reset_mock(side_effect=True)

    pipelines.pipelines_table = mock_pipelines_table
    pipelines.state_table = mock_state_table
    pipelines.mappings_table = mock_mappings_table
    # P21 Task 7.2: rerun_mapping now lives in handlers.pipelines_mappings;
    # wire the same mocks into the owning module (module references only).
    pipelines_mappings.pipelines_table = mock_pipelines_table
    pipelines_mappings.state_table = mock_state_table
    pipelines_mappings.mappings_table = mock_mappings_table

    # Default: pipeline exists with no stored context
    mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_KEY}}

    # Default: one schema_extract run exists
    mock_state_table.query.return_value = {
        "Items": [
            {
                "run_id": "run-prop-001",
                "step_name": "schema_extract",
                "pipeline_id": PIPELINE_KEY,
                "schema_hash": "hash-prop",
                "timestamp": "2025-06-01T00:00:00Z",
            }
        ]
    }

    mock_mappings_table.query.return_value = {"Items": []}

    yield


# ---------------------------------------------------------------------------
# Feature: p15-rerun-mapping, Property 2: Context merge produces correct combined string
# ---------------------------------------------------------------------------


class TestContextMergeProperty:
    """Property 2: Rerun sends exactly the user-provided context to the Lambda.

    The rerun handler no longer merges stored context — it sends exactly what
    the user typed. Stored context is managed separately via the Mapping Context UI.

    **Validates: Requirements 1.4, 1.5, 3.2, 3.3, 3.4**
    """

    @given(
        stored=st.text(min_size=1, max_size=200),
        request=st.text(min_size=1, max_size=200),
    )
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_both_non_empty_sends_request_context_only(self, mock_boto, stored, request):
        """When both stored and request context exist, Lambda receives only request context."""
        mock_boto.return_value = mock_lambda_client

        mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_KEY, "mapping_context": stored}}

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": request}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == request

    @given(stored=st.text(min_size=1, max_size=200))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_empty_request_sends_empty(self, mock_boto, stored):
        """When request context is empty, Lambda receives empty — no stale stored context."""
        mock_boto.return_value = mock_lambda_client

        mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_KEY, "mapping_context": stored}}

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": ""}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == ""

    @given(request=st.text(min_size=1, max_size=200))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_request_only_when_no_stored(self, mock_boto, request):
        """When only request context is non-empty, additional_context == request."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        # No stored mapping_context on pipeline record
        mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_KEY}}

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": request}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == request

    @patch("boto3.client")
    def test_both_empty_produces_empty(self, mock_boto):
        """When both stored and request context are empty, additional_context == ''."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": PIPELINE_KEY}}

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": ""}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == ""


# ---------------------------------------------------------------------------
# Feature: p15-rerun-mapping, Property 3: Context persistence round-trip
# ---------------------------------------------------------------------------


class TestContextPersistenceRoundTrip:
    """Property 3: Context persistence round-trip.

    For any non-empty context string (≤ 2000 chars) submitted via rerun_mapping,
    the pipeline record's mapping_context attribute SHALL be updated to that string.

    **Validates: Requirements 2.1**
    """

    @given(context=st.text(min_size=1, max_size=2000))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_context_persisted_matches_input(self, mock_boto, context):
        """Non-empty context is persisted as mapping_context on the pipeline record."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": context}))

        # Verify update_item was called with SET mapping_context = :mc
        update_call = mock_pipelines_table.update_item.call_args
        assert update_call is not None, "update_item should have been called"
        assert update_call[1]["UpdateExpression"] == "SET mapping_context = :mc"
        assert update_call[1]["ExpressionAttributeValues"][":mc"] == context


# ---------------------------------------------------------------------------
# Feature: p15-rerun-mapping, Property 5: Length validation
# ---------------------------------------------------------------------------


class TestLengthValidation:
    """Property 5: Length validation.

    For any string, the context field SHALL be accepted iff length ≤ 2000.
    Strings exceeding 2000 characters SHALL result in a 400 response.

    **Validates: Requirements 2.5, 2.6**
    """

    @given(context=st.text(min_size=0, max_size=2000))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_valid_length_accepted(self, mock_boto, context):
        """Strings with length ≤ 2000 are accepted (not rejected with 400)."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": context}))

        assert result["statusCode"] != 400 or "2000" not in json.loads(result["body"]).get("error", "")

    @given(context=st.text(min_size=2001, max_size=3000))
    @settings(max_examples=100)
    def test_exceeding_length_rejected(self, context):
        """Strings with length > 2000 are rejected with 400."""
        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "context": context}))

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "2000" in body["error"]


# ---------------------------------------------------------------------------
# Feature: p15-rerun-mapping, Property 8: Model override passthrough
# ---------------------------------------------------------------------------


class TestModelOverridePassthrough:
    """Property 8: Model override passthrough.

    For any non-empty model_id string, the Lambda invocation event SHALL contain
    bedrock_model_id equal to that string. When model_id is absent or empty,
    the invocation event SHALL NOT contain bedrock_model_id.

    **Validates: Requirements 5.3, 5.4**
    """

    @given(model_id=st.text(min_size=1, max_size=200))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_non_empty_model_id_passed_through(self, mock_boto, model_id):
        """Non-empty model_id is passed as bedrock_model_id in Lambda payload."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "model_id": model_id}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["bedrock_model_id"] == model_id

    @given(model_id=st.just(""))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_empty_model_id_omitted(self, mock_boto, model_id):
        """Empty model_id means bedrock_model_id is NOT in the Lambda payload."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY, "model_id": model_id}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert "bedrock_model_id" not in payload

    @patch("boto3.client")
    def test_absent_model_id_omitted(self, mock_boto):
        """Absent model_id means bedrock_model_id is NOT in the Lambda payload."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert "bedrock_model_id" not in payload


# ---------------------------------------------------------------------------
# Feature: p15-rerun-mapping, Property 9: Model override is not persisted
# ---------------------------------------------------------------------------


class TestModelOverrideNotPersisted:
    """Property 9: Model override is not persisted.

    For any rerun request with a model_id, the Pipeline_Record SHALL NOT be
    modified to include the model_id — only mapping_context is persisted.

    **Validates: Requirements 5.6**
    """

    @given(model_id=st.text(min_size=1, max_size=200))
    @settings(max_examples=100)
    @patch("boto3.client")
    def test_model_id_not_written_to_pipeline_record(self, mock_boto, model_id):
        """model_id is never persisted on the pipeline record."""
        mock_boto.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "some context",
                    "model_id": model_id,
                }
            )
        )

        # Check every update_item call — none should contain model_id or bedrock_model_id
        for call in mock_pipelines_table.update_item.call_args_list:
            update_expr = call[1].get("UpdateExpression", "")
            expr_values = call[1].get("ExpressionAttributeValues", {})

            assert "model_id" not in update_expr.lower().replace("mapping_context", "")
            assert "bedrock_model_id" not in update_expr
            for val in expr_values.values():
                if isinstance(val, str):
                    # The only string value persisted should be the context, not the model_id
                    assert val != model_id or val == "some context"
