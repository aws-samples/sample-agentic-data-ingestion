# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the rerun_mapping handler.

Validates: Requirements 1.1–1.6, 2.1, 5.3, 5.4
Tests happy path, missing pipeline_key (400), no runs (404), Lambda failure (500),
context persistence, context clearing, context merging, length validation,
model_id passthrough, model_id absent, and stale mapping deletion.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub Lambda-layer dependencies before importing handlers
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
PIPELINE_KEY = "test-pipeline-001"

mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_mappings_table = MagicMock()
mock_lambda_client = MagicMock()


def _make_event(body: dict | None = None) -> dict:
    return {"body": json.dumps(body) if body else "{}"}


def _lambda_response(payload: dict, status: int = 200):
    """Build a mock Lambda invoke response."""
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
                "run_id": "run-abc123",
                "step_name": "schema_extract",
                "pipeline_id": PIPELINE_KEY,
                "schema_hash": "hash-xyz",
                "timestamp": "2025-06-01T00:00:00Z",
            }
        ]
    }

    # Default: no stale mappings
    mock_mappings_table.query.return_value = {"Items": []}

    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRerunMappingHappyPath:
    """Happy path: valid pipeline_key, Lambda returns 200."""

    @patch("boto3.client")
    def test_happy_path_returns_200(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client

        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True
        assert body["message"] == "Mapping regeneration started"
        # Verify async invocation
        call_args = mock_lambda_client.invoke.call_args
        assert call_args[1]["InvocationType"] == "Event"

    @patch("boto3.client")
    def test_lambda_invoked_with_run_id(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        call_args = mock_lambda_client.invoke.call_args
        payload = json.loads(call_args[1]["Payload"])
        assert payload["run_id"] == "run-abc123"
        assert payload["pipeline_id"] == PIPELINE_KEY
        assert payload["force_remap"] is True

    @patch("boto3.client")
    def test_lambda_invoked_with_correct_function_name(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        call_args = mock_lambda_client.invoke.call_args
        assert call_args[1]["FunctionName"] == "agentic_data_ingestion_schema_mapping"


class TestRerunMappingValidation:
    """Input validation: missing pipeline_key, context length."""

    def test_missing_pipeline_key_returns_400(self):
        result = rerun_mapping(_make_event({}))
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "pipeline_key is required" in body["error"]

    def test_context_exceeds_2000_returns_400(self):
        result = rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "x" * 2001,
                }
            )
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "2000" in body["error"]

    def test_context_exactly_2000_accepted(self):
        """Context at exactly 2000 chars should not be rejected."""
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value = mock_lambda_client
            mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

            result = rerun_mapping(
                _make_event(
                    {
                        "pipeline_key": PIPELINE_KEY,
                        "context": "x" * 2000,
                    }
                )
            )
            assert result["statusCode"] == 200

    def test_invalid_json_body_returns_400(self):
        result = rerun_mapping({"body": "not-json"})
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "Invalid JSON" in body["error"]


class TestRerunMappingNoRuns:
    """No schema_extract runs found → 404."""

    def test_no_runs_returns_404(self):
        mock_state_table.query.return_value = {"Items": []}

        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert "No source schema run found" in body["error"]
        assert PIPELINE_KEY in body["error"]


class TestRerunMappingLambdaFailure:
    """Lambda invocation failure → 500."""

    @patch("boto3.client")
    def test_lambda_exception_returns_500(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.side_effect = Exception("Connection timeout")

        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Schema mapping invocation failed" in body["error"]

    @patch("boto3.client")
    def test_lambda_invocation_failure_returns_500(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.side_effect = Exception("Lambda invoke failed")

        result = rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        assert result["statusCode"] == 500


class TestRerunMappingContextPersistence:
    """Context persistence: write mapping_context to pipeline record."""

    @patch("boto3.client")
    def test_context_persisted_on_pipeline_record(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "Map fname to first_name",
                }
            )
        )

        mock_pipelines_table.update_item.assert_called()
        call_args = mock_pipelines_table.update_item.call_args
        assert call_args[1]["UpdateExpression"] == "SET mapping_context = :mc"
        assert call_args[1]["ExpressionAttributeValues"][":mc"] == "Map fname to first_name"


class TestRerunMappingContextClearing:
    """Empty context removes mapping_context from pipeline record."""

    @patch("boto3.client")
    def test_empty_context_removes_attribute(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "",
                }
            )
        )

        mock_pipelines_table.update_item.assert_called()
        call_args = mock_pipelines_table.update_item.call_args
        assert "REMOVE mapping_context" in call_args[1]["UpdateExpression"]


class TestRerunMappingContextMerging:
    """Merge stored + request context."""

    @patch("boto3.client")
    def test_sends_only_request_context_to_lambda(self, mock_boto_client):
        """Rerun sends only the user-provided context, not merged with stored."""
        mock_boto_client.return_value = mock_lambda_client

        mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_KEY, "mapping_context": "Stored hint"}
        }

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "New hint",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == "New hint"

    @patch("boto3.client")
    def test_empty_context_sends_empty_to_lambda(self, mock_boto_client):
        """When user clears context, Lambda receives empty — no stale stored context."""
        mock_boto_client.return_value = mock_lambda_client

        mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_KEY, "mapping_context": "Stored hint"}
        }

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == ""

    @patch("boto3.client")
    def test_request_only_when_no_stored_context(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "New hint",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == "New hint"

    @patch("boto3.client")
    def test_dedupes_identical_stored_and_request(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        mock_pipelines_table.get_item.return_value = {
            "Item": {"pipeline_id": PIPELINE_KEY, "mapping_context": "Same hint"}
        }

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "context": "Same hint",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["additional_context"] == "Same hint"


class TestRerunMappingModelId:
    """Model ID passthrough and absence."""

    @patch("boto3.client")
    def test_model_id_passed_as_bedrock_model_id(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert payload["bedrock_model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    @patch("boto3.client")
    def test_no_model_id_omits_bedrock_model_id(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert "bedrock_model_id" not in payload

    @patch("boto3.client")
    def test_empty_model_id_omits_bedrock_model_id(self, mock_boto_client):
        mock_boto_client.return_value = mock_lambda_client
        mock_lambda_client.invoke.return_value = _lambda_response({"ok": True})

        rerun_mapping(
            _make_event(
                {
                    "pipeline_key": PIPELINE_KEY,
                    "model_id": "",
                }
            )
        )

        payload = json.loads(mock_lambda_client.invoke.call_args[1]["Payload"])
        assert "bedrock_model_id" not in payload


class TestRerunMappingStaleDeletion:
    """Stale mapping cleanup is skipped with async invocation to avoid race conditions."""

    @patch("boto3.client")
    def test_no_stale_mapping_deletion_with_async_invocation(self, mock_boto_client):
        """Stale mappings are NOT deleted because Lambda runs async — deletion would race."""
        mock_boto_client.return_value = mock_lambda_client

        rerun_mapping(_make_event({"pipeline_key": PIPELINE_KEY}))

        # No stale mapping deletion — async Lambda handles its own mapping creation
        mock_mappings_table.delete_item.assert_not_called()
