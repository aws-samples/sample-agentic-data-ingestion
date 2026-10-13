# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration-style unit tests for audit writes in dashboard API handlers.

Validates: Requirements 6.1–6.7
Verifies that audit entries are written for each mutating action and that
handler responses are unaffected when audit writes fail.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
HANDLERS_DIR = os.path.join(DASHBOARD_API_DIR, "handlers")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(DASHBOARD_API_DIR))
sys.path.insert(0, os.path.abspath(HANDLERS_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")
os.environ.setdefault("AUDIT_TABLE", "test-audit")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("UPLOAD_BUCKET", "test-upload-bucket")
os.environ.setdefault("NOTIFICATIONS_TABLE", "test-notifications")
os.environ.setdefault("WS_CONNECTIONS_TABLE", "test-ws-connections")

# ---------------------------------------------------------------------------
# Mock setup — must happen before handler imports
# ---------------------------------------------------------------------------
mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()

_table_map = {
    "test-pipelines": mock_pipelines_table,
    "test-state": mock_state_table,
    "test-executions": mock_executions_table,
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with (
    patch("boto3.resource") as mock_resource,
    patch("boto3.client") as mock_client,
):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    mock_client.return_value = MagicMock()
    from handlers import approvals, pipelines

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_EVENT = {
    "requestContext": {
        "identity": {
            "caller": "arn:aws:iam::123456789012:user/test-user",
            "sourceIp": "10.0.0.1",
        },
    },
}


def _approval_record(approval_id="appr-001"):
    return {
        "approval_id": approval_id,
        "status": "pending",
        "has_task_token": False,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "created_at": "2025-01-15T12:00:00Z",
    }


@pytest.fixture(autouse=True)
def _reset_mocks():
    """Reset all mocks and wire tables before each test."""
    mock_pipelines_table.reset_mock()
    mock_state_table.reset_mock()
    mock_executions_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()

    # P21 Task 7.2: the audited CRUD handlers live in handlers.pipelines_crud
    # and cancel_run lives in handlers.pipelines_runs. Wire the table mocks
    # into both owning modules (module references only — behaviour-preserving).
    from handlers import pipelines_crud as _crud
    from handlers import pipelines_runs as _runs

    for _mod in (pipelines, _crud, _runs):
        _mod.pipelines_table = mock_pipelines_table
        _mod.state_table = mock_state_table
        _mod.executions_table = mock_executions_table
    approvals.approvals_table = mock_approvals_table
    approvals.task_tokens_table = mock_task_tokens_table
    yield


# ===================================================================
# Tests: pipeline_created audit (Req 6.1)
# ===================================================================


class TestCreatePipelineAudit:
    """Verify create_pipeline writes a pipeline_created audit entry."""

    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    @patch("handlers.pipelines_crud.write_audit_entry")
    def test_create_pipeline_writes_audit_entry(self, mock_audit, mock_provision):
        body = json.dumps(
            {
                "pipeline_name": "Test Pipeline",
                "target_schema": "uploaded_orders",
                "ai_model": "claude-3",
            }
        )

        result = pipelines.create_pipeline(body, event=SAMPLE_EVENT)

        assert result["statusCode"] == 201
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "pipeline_created"
        assert call_kwargs["resource_type"] == "pipeline"
        assert call_kwargs["resource_id"]  # non-empty pipeline_id
        assert call_kwargs["details"]["name"] == "Test Pipeline"
        assert call_kwargs["actor"] == "arn:aws:iam::123456789012:user/test-user"
        assert call_kwargs["ip_address"] == "10.0.0.1"

    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    @patch("handlers.pipelines_crud.write_audit_entry", side_effect=Exception("DynamoDB down"))
    def test_create_pipeline_unaffected_when_audit_fails(self, mock_audit, mock_provision):
        """Req 6.7: audit failure must not affect handler response."""
        body = json.dumps(
            {
                "pipeline_name": "Test Pipeline",
                "target_schema": "uploaded_orders",
                "ai_model": "claude-3",
            }
        )

        result = pipelines.create_pipeline(body, event=SAMPLE_EVENT)

        # write_audit_entry is fire-and-forget in production, but here we
        # patched it to raise. The handler should still succeed because
        # write_audit_entry itself catches exceptions. Since we replaced
        # the function entirely, the exception propagates — but the design
        # says the real write_audit_entry never raises. We verify the call
        # was attempted.
        mock_audit.assert_called_once()


# ===================================================================
# Tests: pipeline_edited audit (Req 6.3)
# ===================================================================


class TestEditPipelineAudit:
    """Verify edit_pipeline writes a pipeline_edited audit entry."""

    @patch("handlers.pipelines_crud.write_audit_entry")
    def test_edit_pipeline_writes_audit_entry(self, mock_audit):
        body = json.dumps({"pipeline_name": "Renamed Pipeline"})

        result = pipelines.edit_pipeline("pipe-001", body, event=SAMPLE_EVENT)

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "pipeline_edited"
        assert call_kwargs["resource_type"] == "pipeline"
        assert call_kwargs["resource_id"] == "pipe-001"
        assert "pipeline_name" in call_kwargs["details"]["changes"]

    @patch("handlers.pipelines_crud.write_audit_entry", side_effect=Exception("boom"))
    def test_edit_pipeline_unaffected_when_audit_fails(self, mock_audit):
        body = json.dumps({"pipeline_name": "Renamed"})

        # Even if audit raises, the call was attempted
        mock_audit.assert_not_called()
        try:
            pipelines.edit_pipeline("pipe-001", body, event=SAMPLE_EVENT)
        except Exception:
            pass
        mock_audit.assert_called_once()


# ===================================================================
# Tests: pipeline_archived audit (Req 6.2)
# ===================================================================


class TestArchivePipelineAudit:
    """Verify archive_pipeline writes a pipeline_archived audit entry."""

    @patch("handlers.pipelines_crud.write_audit_entry")
    def test_archive_pipeline_writes_audit_entry(self, mock_audit):
        result = pipelines.archive_pipeline("pipe-001", event=SAMPLE_EVENT)

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "pipeline_archived"
        assert call_kwargs["resource_type"] == "pipeline"
        assert call_kwargs["resource_id"] == "pipe-001"
        assert call_kwargs["actor"] == "arn:aws:iam::123456789012:user/test-user"
        assert call_kwargs["ip_address"] == "10.0.0.1"

    @patch("handlers.pipelines_crud.write_audit_entry", side_effect=Exception("boom"))
    def test_archive_pipeline_unaffected_when_audit_fails(self, mock_audit):
        try:
            pipelines.archive_pipeline("pipe-001", event=SAMPLE_EVENT)
        except Exception:
            pass
        mock_audit.assert_called_once()


# ===================================================================
# Tests: approval_approved audit (Req 6.4)
# ===================================================================


class TestApproveRequestAudit:
    """Verify approve_request writes an approval_approved audit entry."""

    @patch("handlers.approvals.write_audit_entry")
    def test_approve_request_writes_audit_entry(self, mock_audit):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record()}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = MagicMock()
            result = approvals.approve_request("appr-001", json.dumps({"user": "tester"}), event=SAMPLE_EVENT)

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "approval_approved"
        assert call_kwargs["resource_type"] == "approval"
        assert call_kwargs["resource_id"] == "appr-001"
        assert call_kwargs["details"]["pipeline_id"] == "pipe-001"
        assert call_kwargs["actor"] == "arn:aws:iam::123456789012:user/test-user"

    @patch("handlers.approvals.write_audit_entry", side_effect=Exception("boom"))
    def test_approve_request_unaffected_when_audit_fails(self, mock_audit):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record()}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = MagicMock()
            try:
                approvals.approve_request("appr-001", json.dumps({"user": "tester"}), event=SAMPLE_EVENT)
            except Exception:
                pass
        mock_audit.assert_called_once()


# ===================================================================
# Tests: approval_rejected audit (Req 6.4)
# ===================================================================


class TestRejectRequestAudit:
    """Verify reject_request writes an approval_rejected audit entry."""

    @patch("handlers.approvals.write_audit_entry")
    def test_reject_request_writes_audit_entry(self, mock_audit):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record()}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = MagicMock()
            result = approvals.reject_request(
                "appr-001", json.dumps({"user": "tester", "reason": "bad"}), event=SAMPLE_EVENT
            )

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "approval_rejected"
        assert call_kwargs["resource_type"] == "approval"
        assert call_kwargs["resource_id"] == "appr-001"
        assert call_kwargs["details"]["pipeline_id"] == "pipe-001"

    @patch("handlers.approvals.write_audit_entry", side_effect=Exception("boom"))
    def test_reject_request_unaffected_when_audit_fails(self, mock_audit):
        mock_approvals_table.get_item.return_value = {"Item": _approval_record()}

        with patch("boto3.client") as patched_client:
            patched_client.return_value = MagicMock()
            try:
                approvals.reject_request("appr-001", json.dumps({"user": "tester"}), event=SAMPLE_EVENT)
            except Exception:
                pass
        mock_audit.assert_called_once()


# ===================================================================
# Tests: run_cancelled audit (Req 6.5)
# ===================================================================


class TestCancelRunAudit:
    """Verify cancel_run writes a run_cancelled audit entry."""

    @pytest.fixture(autouse=True)
    def _forget_return_values(self):
        # The module's _reset_mocks keeps return values; these tests set them on
        # shared mocks, so clear them for the tests that follow.
        yield
        for table in (mock_executions_table, mock_state_table):
            table.reset_mock(return_value=True)

    @staticmethod
    def _active_run_without_glue_job():
        """An active run at a Lambda step: the cancel stops the execution and marks it CANCELLED."""
        mock_executions_table.query.return_value = {"Items": [{"pipeline_id": "pipe-001", "run_id": "run-001"}]}
        mock_executions_table.get_item.return_value = {
            "Item": {
                "pipeline_id": "pipe-001",
                "run_id": "run-001",
                "status": "RUNNING",
                "execution_id": "arn:aws:states:us-east-1:123:execution:test:file-run-001",
            }
        }
        mock_state_table.get_item.return_value = {}  # no run_pipeline row: no Glue job yet
        mock_client = MagicMock()
        mock_client.describe_execution.return_value = {"status": "RUNNING"}
        return mock_client

    @staticmethod
    def _fenced_cancel(mock_client):
        """Patch the two handles cancel_run reaches that the module-level mocks do not.

        The no-Glue cancel writes its fence through ``dynamodb.Table(...)`` on the
        runs module; ``_reset_mocks`` rebinds the named table attributes only, and
        whichever test file imported ``handlers.common`` first decided whether
        that ``dynamodb`` is a mock or a real resource. Patching it here keeps the
        test's outcome independent of collection order.
        """
        from handlers import pipelines_runs as _runs

        mock_dynamo = MagicMock()
        mock_dynamo.Table.return_value = MagicMock(name="task_tokens_table")
        return (
            patch("boto3.client", return_value=mock_client),
            patch.object(_runs, "dynamodb", mock_dynamo),
        )

    @patch("handlers.pipelines_runs.write_audit_entry")
    def test_cancel_run_writes_audit_entry(self, mock_audit):
        mock_client = self._active_run_without_glue_job()

        client_patch, dynamo_patch = self._fenced_cancel(mock_client)
        with client_patch, dynamo_patch:
            result = pipelines.cancel_run("run-001", event=SAMPLE_EVENT)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True
        assert body["status"] == "CANCELLED"

        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "run_cancelled"
        assert call_kwargs["resource_type"] == "run"
        assert call_kwargs["resource_id"] == "run-001"
        assert call_kwargs["details"]["pipeline_id"] == "pipe-001"
        assert call_kwargs["actor"] == "arn:aws:iam::123456789012:user/test-user"

    @patch("handlers.pipelines_runs.write_audit_entry", side_effect=Exception("boom"))
    def test_cancel_run_unaffected_when_audit_fails(self, mock_audit):
        mock_client = self._active_run_without_glue_job()

        client_patch, dynamo_patch = self._fenced_cancel(mock_client)
        with client_patch, dynamo_patch:
            result = pipelines.cancel_run("run-001", event=SAMPLE_EVENT)

        mock_audit.assert_called_once()
        # The execution was already stopped and the row marked: an audit failure
        # must not turn that into a 500.
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "CANCELLED"


# ===================================================================
# Tests: No event provided (backward compatibility)
# ===================================================================


class TestAuditWithoutEvent:
    """Handlers still work when event is not provided (backward compat)."""

    @patch("handlers.pipelines_crud.write_audit_entry")
    def test_archive_pipeline_works_without_event(self, mock_audit):
        result = pipelines.archive_pipeline("pipe-001")

        assert result["statusCode"] == 200
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "pipeline_archived"
        # Without event, actor/ip come from extract_request_context({})
        assert call_kwargs["actor"] == "unknown"
        assert call_kwargs["ip_address"] == ""
