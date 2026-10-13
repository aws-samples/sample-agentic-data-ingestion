# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for permanent pipeline deletion (Req 1).

Validates: Requirements 1.1–1.15
Tests the delete_pipeline function including precondition checks,
best-effort cleanup of all resource types, partial failure handling,
and running execution stop.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

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

try:
    from handlers.pipelines import delete_pipeline
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-pipeline-abc12345"


# ---------------------------------------------------------------------------
# Mock tables and clients
# ---------------------------------------------------------------------------
mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()
mock_locks_table = MagicMock()
mock_s3_client = MagicMock()
mock_dynamodb = MagicMock()

_table_map = {
    "agentic_data_ingestion_task_tokens": mock_task_tokens_table,
    "agentic_data_ingestion_pipeline_locks": mock_locks_table,
}
mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())


@pytest.fixture(autouse=True)
def _reset_mocks(monkeypatch):
    """Reset all mocks before each test and wire them into the module."""
    import importlib

    # The reloads below re-run each module's `from env_utils import ...`, so this
    # file's stub has to be in sys.modules while they run. Module scope restores
    # it after the first import (LGR-c58f276-4); install it again per test here.
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)

    # P21 Task 7.2: delete_pipeline now lives in handlers.pipelines_crud. Reload
    # the owning module (plus common + façade) to shed mock-tainted bindings;
    # guard each reload so a missing namespace parent (a pure-isolation
    # test-ordering artefact) doesn't error the fixture.
    for _name in ("handlers.common", "handlers.pipelines_crud", "handlers.pipelines"):
        _mod = sys.modules.get(_name)
        if _mod is not None and "handlers" in sys.modules:
            try:
                importlib.reload(_mod)
            except ImportError:
                pass

    # P21 Task 7.2: delete_pipeline now lives in handlers.pipelines_crud; wire
    # mocks into the owning module (module references only — behaviour-preserving).
    from handlers import pipelines_crud as _fresh_crud

    global pipelines, delete_pipeline
    pipelines = _fresh_crud
    delete_pipeline = _fresh_crud.delete_pipeline

    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_executions_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_locks_table.reset_mock()
    mock_s3_client.reset_mock()
    mock_dynamodb.reset_mock()
    mock_dynamodb.Table.side_effect = lambda name: _table_map.get(name, MagicMock())

    # Default: no pagination
    mock_state_table.query.return_value = {"Items": []}
    mock_executions_table.query.return_value = {"Items": []}
    mock_approvals_table.scan.return_value = {"Items": []}

    # Wire mocks into the owning module
    _fresh_crud.pipelines_table = mock_pipelines_table
    _fresh_crud.state_table = mock_state_table
    _fresh_crud.executions_table = mock_executions_table
    _fresh_crud.approvals_table = mock_approvals_table
    _fresh_crud.dynamodb = mock_dynamodb
    _fresh_crud.s3_client = mock_s3_client
    yield


def _archived_pipeline_item():
    return {
        "pipeline_id": PIPELINE_ID,
        "pipeline_name": "Test Pipeline",
        "status": "archived",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _active_pipeline_item():
    item = _archived_pipeline_item()
    item["status"] = "active"
    return item


class TestDeletePipelineNotFound:
    """Validates: Requirement 1.1 — 404 for missing pipeline."""

    def test_returns_404_when_pipeline_not_found(self):
        mock_pipelines_table.get_item.return_value = {}

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 404
        assert "not found" in body["error"].lower()


class TestDeletePipelineNotArchived:
    """Validates: Requirement 1.2 — 409 for non-archived pipeline."""

    def test_returns_409_for_active_pipeline(self):
        mock_pipelines_table.get_item.return_value = {"Item": _active_pipeline_item()}

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 409
        assert "archived" in body["error"].lower()
        assert body["current_status"] == "active"

    def test_returns_409_for_running_pipeline(self):
        item = _archived_pipeline_item()
        item["status"] = "running"
        mock_pipelines_table.get_item.return_value = {"Item": item}

        result = delete_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 409


class TestDeletePipelineSuccessfulCleanup:
    """Validates: Requirements 1.3–1.14 — successful cleanup of all resource types."""

    def _setup_archived_pipeline(self):
        mock_pipelines_table.get_item.return_value = {"Item": _archived_pipeline_item()}

    @patch("handlers.pipelines_crud.boto3.client")
    def test_sets_status_to_deleting(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        # Glue delete_job succeeds
        mock_boto_client.return_value.delete_job.return_value = {}
        # Events list_targets_by_rule raises ResourceNotFoundException
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        delete_pipeline(PIPELINE_ID)

        # Verify status was set to "deleting"
        update_call = mock_pipelines_table.update_item.call_args
        assert update_call is not None
        assert update_call[1]["ExpressionAttributeValues"][":status"] == "deleting"

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_state_entries(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        # State table has entries
        mock_state_table.query.return_value = {
            "Items": [
                {"run_id": "run-1", "step_name": "file_validation"},
                {"run_id": "run-1", "step_name": "schema_extract"},
            ]
        }
        mock_batch_writer = MagicMock()
        mock_state_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_state_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["cleanup_results"]["state_entries"] == 2

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_executions(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        mock_executions_table.query.return_value = {
            "Items": [
                {"pipeline_id": PIPELINE_ID, "run_id": "run-1", "status": "SUCCEEDED"},
            ]
        }
        mock_batch_writer = MagicMock()
        mock_executions_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_executions_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["cleanup_results"]["executions"] == 1

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_approvals_and_task_tokens(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        mock_approvals_table.scan.return_value = {
            "Items": [
                {"approval_id": "appr-1"},
                {"approval_id": "appr-2"},
            ]
        }

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert body["cleanup_results"]["approvals"] == 2
        assert body["cleanup_results"]["task_tokens"] == 2
        # Verify task tokens were deleted for each approval
        assert mock_task_tokens_table.delete_item.call_count == 2

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_lock_record(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert body["cleanup_results"]["locks"] == 1
        mock_locks_table.delete_item.assert_called_once_with(Key={"pipeline_prefix": PIPELINE_ID})

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_s3_objects(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        # Mock S3 paginator
        mock_paginator = MagicMock()
        mock_s3_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": f"{PIPELINE_ID}/file1.csv"},
                    {"Key": f"{PIPELINE_ID}/file2.csv"},
                ]
            }
        ]

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert body["cleanup_results"]["s3_objects"] == 2
        mock_s3_client.delete_objects.assert_called_once()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_glue_job(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_glue = MagicMock()
        mock_events = MagicMock()
        mock_events.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        def _client_factory(service):
            if service == "glue":
                return mock_glue
            if service == "events":
                return mock_events
            return MagicMock()

        mock_boto_client.side_effect = _client_factory

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert body["cleanup_results"]["glue_job"] is True
        mock_glue.delete_job.assert_called_once_with(JobName=f"agentic-data-ingestion-job-{PIPELINE_ID}")

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_eventbridge_rules(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_glue = MagicMock()
        mock_events = MagicMock()
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "target-1"}]}

        def _client_factory(service):
            if service == "glue":
                return mock_glue
            if service == "events":
                return mock_events
            return MagicMock()

        mock_boto_client.side_effect = _client_factory

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        # Both S3 trigger and schedule rules should be attempted
        assert len(body["cleanup_results"]["eventbridge_rules"]) == 2
        assert mock_events.remove_targets.call_count == 2
        assert mock_events.delete_rule.call_count == 2

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_pipeline_record_last(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["cleanup_results"]["pipeline_record"] is True
        mock_pipelines_table.delete_item.assert_called_once_with(Key={"pipeline_id": PIPELINE_ID})

    @patch("handlers.pipelines_crud.boto3.client")
    def test_returns_200_with_summary(self, mock_boto_client):
        self._setup_archived_pipeline()
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["success"] is True
        assert body["pipeline_id"] == PIPELINE_ID
        assert "cleanup_results" in body
        assert "cleanup_failures" in body


class TestDeletePipelinePartialFailure:
    """Validates: Requirement 1.13 — partial failure handling."""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_continues_cleanup_when_state_delete_fails(self, mock_boto_client):
        mock_pipelines_table.get_item.return_value = {"Item": _archived_pipeline_item()}
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        # State table query fails
        mock_state_table.query.side_effect = Exception("DynamoDB error")

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        # Should still return 200 with failures logged
        assert result["statusCode"] == 200
        assert len(body["cleanup_failures"]) >= 1
        failure_resources = [f["resource"] for f in body["cleanup_failures"]]
        assert "state_entries" in failure_resources
        # Pipeline record should still be deleted
        assert body["cleanup_results"]["pipeline_record"] is True

    @patch("handlers.pipelines_crud.boto3.client")
    def test_continues_cleanup_when_s3_delete_fails(self, mock_boto_client):
        mock_pipelines_table.get_item.return_value = {"Item": _archived_pipeline_item()}
        mock_boto_client.return_value = MagicMock()
        mock_boto_client.return_value.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        # S3 paginator fails
        mock_s3_client.get_paginator.side_effect = Exception("S3 error")

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        failure_resources = [f["resource"] for f in body["cleanup_failures"]]
        assert "s3_objects" in failure_resources


class TestDeletePipelineRunningExecutionStop:
    """Validates: Requirement 1.15 — stop running executions before cleanup."""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_stops_running_executions(self, mock_boto_client):
        mock_pipelines_table.get_item.return_value = {"Item": _archived_pipeline_item()}

        mock_sfn = MagicMock()
        mock_glue = MagicMock()
        mock_events = MagicMock()
        mock_events.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        def _client_factory(service):
            if service == "stepfunctions":
                return mock_sfn
            if service == "glue":
                return mock_glue
            if service == "events":
                return mock_events
            return MagicMock()

        mock_boto_client.side_effect = _client_factory

        # First query call is for running execution check, second is for cleanup
        exec_arn = "arn:aws:states:us-east-1:123456789012:execution:test:run-1"
        mock_executions_table.query.side_effect = [
            {
                "Items": [
                    {
                        "pipeline_id": PIPELINE_ID,
                        "run_id": "run-1",
                        "status": "RUNNING",
                        "execution_id": exec_arn,
                    },
                ]
            },
            # Second call for cleanup batch-delete
            {
                "Items": [
                    {"pipeline_id": PIPELINE_ID, "run_id": "run-1"},
                ]
            },
        ]
        mock_batch_writer = MagicMock()
        mock_executions_table.batch_writer.return_value.__enter__ = MagicMock(return_value=mock_batch_writer)
        mock_executions_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        result = delete_pipeline(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert "run-1" in body["stopped_executions"]
        mock_sfn.stop_execution.assert_called_once_with(
            executionArn=exec_arn,
            error="PipelineDeleted",
            cause="Pipeline is being permanently deleted",
        )
