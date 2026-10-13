# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for scheduled/recurring pipeline triggers (Req 3).

Validates: Requirements 3.1–3.12
Tests schedule CRUD in create/edit, archive/restore rule management,
invalid schedule expression rejection, and schedule visibility in
pipeline details response.
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
    "PIPELINE_TRIGGER_ARN": "arn:aws:lambda:us-east-1:123456789012:function:agentic_data_ingestion_pipeline_trigger",
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

# P21 Task 7.2 — the CRUD handlers (create/edit/archive/restore/details +
# _manage_schedule_rule) now live in handlers.pipelines_crud. Bind the module
# alias and patch targets there (module references only — behaviour-preserving).
try:
    from handlers import pipelines_crud as pipelines
    from handlers.pipelines import (
        _manage_schedule_rule,
        archive_pipeline,
        create_pipeline,
        edit_pipeline,
        get_pipeline_details,
        restore_pipeline,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-scheduled-pipeline"
TRIGGER_ARN = "arn:aws:lambda:us-east-1:123456789012:function:agentic_data_ingestion_pipeline_trigger"
SCHEDULE_EXPR = "rate(1 hour)"


# ---------------------------------------------------------------------------
# Mock tables and clients
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

    # Ensure require_env returns PIPELINE_TRIGGER_ARN correctly even when
    # the real env_utils module is loaded by other test files first
    original_require_env = pipelines.require_env

    def _patched_require_env(name, default=None):
        overrides = {
            "UPLOAD_BUCKET": "agentic-data-ingestion-source",
            "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "PIPELINE_TRIGGER_ARN": TRIGGER_ARN,
        }
        if name in overrides:
            return overrides[name]
        return original_require_env(name, default)

    pipelines.require_env = _patched_require_env
    yield
    pipelines.require_env = original_require_env


# ---------------------------------------------------------------------------
# _manage_schedule_rule tests
# ---------------------------------------------------------------------------


class TestManageScheduleRule:
    """Validates: Requirements 3.3, 3.6, 3.7, 3.10"""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_creates_rule_with_valid_expression(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        result = _manage_schedule_rule(PIPELINE_ID, SCHEDULE_EXPR, TRIGGER_ARN, PIPELINE_ID, source_type="S3_UPLOAD")

        assert result["action"] == "created"
        mock_events.put_rule.assert_called_once_with(
            Name=f"agentic-data-ingestion-schedule-{PIPELINE_ID}",
            ScheduleExpression=SCHEDULE_EXPR,
            State="ENABLED",
            Description=f"Scheduled trigger for pipeline {PIPELINE_ID}",
        )
        mock_events.put_targets.assert_called_once()
        target_call = mock_events.put_targets.call_args
        targets = target_call[1]["Targets"] if "Targets" in target_call[1] else target_call[0][0]
        target_input = json.loads(targets[0]["Input"])
        assert target_input["trigger"] == "scheduled"
        assert target_input["pipeline_id"] == PIPELINE_ID

    @patch("handlers.pipelines_crud.boto3.client")
    def test_deletes_rule_when_expression_is_none(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}

        result = _manage_schedule_rule(PIPELINE_ID, None, TRIGGER_ARN, PIPELINE_ID)

        assert result["action"] == "deleted"
        mock_events.remove_targets.assert_called_once()
        mock_events.delete_rule.assert_called_once_with(Name=f"agentic-data-ingestion-schedule-{PIPELINE_ID}")

    @patch("handlers.pipelines_crud.boto3.client")
    def test_delete_ignores_nonexistent_rule(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.side_effect = Exception("ResourceNotFoundException")

        result = _manage_schedule_rule(PIPELINE_ID, None, TRIGGER_ARN, PIPELINE_ID)

        assert result["action"] == "deleted"

    @patch("handlers.pipelines_crud.boto3.client")
    def test_raises_value_error_for_invalid_expression(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        # Simulate EventBridge ValidationException
        mock_events.exceptions.ValidationException = type("ValidationException", (Exception,), {})
        mock_events.put_rule.side_effect = mock_events.exceptions.ValidationException("Invalid expression")

        with pytest.raises(ValueError, match="Invalid schedule expression"):
            _manage_schedule_rule(PIPELINE_ID, "bad-expression", TRIGGER_ARN, PIPELINE_ID, source_type="S3_UPLOAD")

    # -- 2026-10-10: the payload's source_type, and PutTargets' own failures --

    @pytest.mark.parametrize("source_type", ["S3_UPLOAD", "JDBC"])
    @patch("handlers.pipelines_crud.boto3.client")
    def test_the_target_input_carries_the_source_type(self, mock_boto_client, source_type):
        """The trigger branches on it; without it every JDBC schedule ran the S3 branch."""
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        _manage_schedule_rule(PIPELINE_ID, SCHEDULE_EXPR, TRIGGER_ARN, PIPELINE_ID, source_type=source_type)

        targets = mock_events.put_targets.call_args.kwargs["Targets"]
        assert json.loads(targets[0]["Input"])["source_type"] == source_type

    @pytest.mark.parametrize("source_type", [None, "", "s3_upload", "KAFKA"])
    @patch("handlers.pipelines_crud.boto3.client")
    def test_creating_without_a_known_source_type_is_refused_before_any_call(self, mock_boto_client, source_type):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        with pytest.raises(RuntimeError, match="source_type"):
            _manage_schedule_rule(PIPELINE_ID, SCHEDULE_EXPR, TRIGGER_ARN, PIPELINE_ID, source_type=source_type)
        mock_events.put_rule.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_failed_target_entry_raises_instead_of_reporting_created(self, mock_boto_client):
        """PutTargets returns HTTP 200 with FailedEntryCount > 0 when it could
        not attach a target, so the SDK raises nothing."""
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.put_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [
                {
                    "TargetId": "pipeline-trigger",
                    "ErrorCode": "ConcurrentModificationException",
                    "ErrorMessage": "rule is being modified",
                }
            ],
        }

        with pytest.raises(RuntimeError) as raised:
            _manage_schedule_rule(PIPELINE_ID, SCHEDULE_EXPR, TRIGGER_ARN, PIPELINE_ID, source_type="JDBC")
        message = str(raised.value)
        assert "pipeline-trigger" in message
        assert "ConcurrentModificationException" in message

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_failed_remove_targets_entry_raises_and_keeps_the_rule(self, mock_boto_client):
        """RemoveTargets also answers HTTP 200 with FailedEntryCount > 0. The
        rule must not be deleted with its target still on it, and the caller
        must hear about it."""
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}
        mock_events.remove_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [{"TargetId": "pipeline-trigger", "ErrorCode": "InternalException", "ErrorMessage": "x"}],
        }

        with pytest.raises(RuntimeError, match="InternalException"):
            _manage_schedule_rule(PIPELINE_ID, None, TRIGGER_ARN, PIPELINE_ID)
        mock_events.delete_rule.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_remove_targets_error_that_is_not_not_found_raises(self, mock_boto_client):
        from botocore.exceptions import ClientError

        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}
        mock_events.remove_targets.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "RemoveTargets"
        )

        with pytest.raises(ClientError):
            _manage_schedule_rule(PIPELINE_ID, None, TRIGGER_ARN, PIPELINE_ID)
        mock_events.delete_rule.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_zero_failed_entry_count_is_success(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.put_targets.return_value = {"FailedEntryCount": 0, "FailedEntries": []}

        result = _manage_schedule_rule(PIPELINE_ID, SCHEDULE_EXPR, TRIGGER_ARN, PIPELINE_ID, source_type="JDBC")
        assert result["action"] == "created"


# ---------------------------------------------------------------------------
# create_pipeline with schedule tests
# ---------------------------------------------------------------------------


class TestCreatePipelineWithSchedule:
    """Validates: Requirements 3.1, 3.3"""

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_creates_pipeline_with_schedule(self, mock_provision, mock_manage):
        body = json.dumps(
            {
                "pipeline_name": "Scheduled Test",
                "target_schema": "uploaded_test",
                "ai_model": "claude-3",
                "schedule": SCHEDULE_EXPR,
            }
        )

        result = create_pipeline(body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 201
        # Verify schedule was stored in pipeline item
        put_call = mock_pipelines_table.put_item.call_args
        item = put_call[1]["Item"]
        assert item["schedule"] == SCHEDULE_EXPR
        # Verify schedule rule was created
        mock_manage.assert_called_once()

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_creates_pipeline_without_schedule(self, mock_provision, mock_manage):
        body = json.dumps(
            {
                "pipeline_name": "No Schedule",
                "target_schema": "uploaded_test",
                "ai_model": "claude-3",
            }
        )

        result = create_pipeline(body, event={})

        assert result["statusCode"] == 201
        put_call = mock_pipelines_table.put_item.call_args
        item = put_call[1]["Item"]
        assert "schedule" not in item
        mock_manage.assert_not_called()

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_returns_400_for_invalid_schedule(self, mock_provision, mock_manage):
        mock_manage.side_effect = ValueError("Invalid schedule expression: bad")

        body = json.dumps(
            {
                "pipeline_name": "Bad Schedule",
                "target_schema": "uploaded_test",
                "ai_model": "claude-3",
                "schedule": "bad",
            }
        )

        result = create_pipeline(body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "Invalid schedule expression" in body_resp["error"]
        # Pipeline record should be rolled back
        mock_pipelines_table.delete_item.assert_called_once()
        # `put_rule` refused the expression, so no rule exists to delete.
        mock_manage.assert_called_once()

    # -- Required when requested (2026-10-10) ------------------------------
    # A schedule failure other than an invalid expression used to log a warning
    # and return 201, leaving a record that shows a schedule EventBridge does
    # not have. It now fails the create and removes what the request made.

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_a_schedule_service_failure_fails_the_create_and_removes_the_rule(self, mock_provision, mock_manage):
        from botocore.exceptions import ClientError

        # put_rule succeeded and put_targets failed: an ENABLED rule may exist.
        mock_manage.side_effect = [
            ClientError({"Error": {"Code": "LimitExceededException", "Message": "too many rules"}}, "PutTargets"),
            {"action": "deleted"},
        ]
        body = json.dumps(
            {
                "pipeline_name": "Rule Limit",
                "target_schema": "uploaded_test",
                "ai_model": "claude-3",
                "schedule": SCHEDULE_EXPR,
            }
        )
        result = create_pipeline(body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 500
        assert body_resp["stage"] == "schedule"
        assert body_resp["reason"] == "provisioning_failed"
        assert body_resp["error"].startswith("Pipeline was not created")
        delete_call = mock_manage.call_args_list[1]
        assert delete_call.args[1] is None, "the rollback deletes the rule through the same helper"
        mock_pipelines_table.delete_item.assert_called_once()

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_a_missing_trigger_arn_is_a_failure_not_a_silent_skip(self, mock_provision, mock_manage):
        original = pipelines.require_env

        def without_trigger_arn(name, default=None):
            return "" if name == "PIPELINE_TRIGGER_ARN" else original(name, default)

        pipelines.require_env = without_trigger_arn
        try:
            body = json.dumps(
                {
                    "pipeline_name": "No Trigger",
                    "target_schema": "uploaded_test",
                    "ai_model": "claude-3",
                    "schedule": SCHEDULE_EXPR,
                }
            )
            result = create_pipeline(body, event={})
        finally:
            pipelines.require_env = original

        assert result["statusCode"] == 500, "a missing deployment setting is not the caller's fault"
        assert "PIPELINE_TRIGGER_ARN" in json.loads(result["body"])["error"]
        mock_manage.assert_not_called()
        mock_pipelines_table.delete_item.assert_called_once()

    @patch("handlers.pipelines_crud.boto3.client")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_a_target_that_did_not_attach_fails_the_create_and_removes_the_rule(self, mock_provision, mock_boto_client):
        """End to end through the real helper: PutTargets answers 200 with one
        failed entry, and the create fails and deletes the targetless rule."""
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.put_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [{"TargetId": "pipeline-trigger", "ErrorCode": "InternalException", "ErrorMessage": "x"}],
        }
        mock_events.list_targets_by_rule.return_value = {"Targets": []}
        body = json.dumps(
            {
                "pipeline_name": "Target Not Attached",
                "target_schema": "uploaded_test",
                "ai_model": "claude-3",
                "schedule": SCHEDULE_EXPR,
            }
        )
        result = create_pipeline(body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 500
        assert body_resp["stage"] == "schedule"
        assert "InternalException" in body_resp["error"]
        pipeline_id = mock_pipelines_table.put_item.call_args.kwargs["Item"]["pipeline_id"]
        mock_events.delete_rule.assert_called_once_with(Name=f"agentic-data-ingestion-schedule-{pipeline_id}")
        mock_pipelines_table.delete_item.assert_called_once()

    @patch("handlers.pipelines_crud.boto3.client")
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_a_jdbc_pipelines_rule_carries_jdbc(self, mock_provision, mock_boto_client):
        """The create path threads the request's source_type into the rule."""
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        with (
            patch("handlers.pipelines_crud._validate_jdbc_source_config"),
            patch("handlers.pipelines_crud._provision_jdbc_source_connection", return_value="conn"),
            patch("handlers.pipelines_crud._provision_jdbc_runtime_crawler", return_value="crawler"),
        ):
            body = json.dumps(
                {
                    "pipeline_name": "Jdbc Scheduled",
                    "target_schema": "uploaded_test",
                    "ai_model": "claude-3",
                    "source_type": "JDBC",
                    "source_config": {"glue_connection_name": "customer-conn", "engine": "postgres"},
                    "schedule": SCHEDULE_EXPR,
                }
            )
            result = create_pipeline(body, event={})

        assert result["statusCode"] == 201, result["body"]
        target_input = json.loads(mock_events.put_targets.call_args.kwargs["Targets"][0]["Input"])
        assert target_input["source_type"] == "JDBC"


# ---------------------------------------------------------------------------
# edit_pipeline with schedule tests
# ---------------------------------------------------------------------------


class TestEditPipelineWithSchedule:
    """Validates: Requirements 3.1, 3.6, 3.7"""

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    def test_adds_schedule_to_existing_pipeline(self, mock_manage):
        body = json.dumps({"schedule": SCHEDULE_EXPR})

        result = edit_pipeline(PIPELINE_ID, body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 200
        # Verify schedule was stored
        update_call = mock_pipelines_table.update_item.call_args
        assert ":schedule" in update_call[1]["ExpressionAttributeValues"]
        assert update_call[1]["ExpressionAttributeValues"][":schedule"] == SCHEDULE_EXPR
        # Verify rule was created
        mock_manage.assert_called_once()

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    def test_removes_schedule_from_pipeline(self, mock_manage):
        body = json.dumps({"schedule": ""})

        result = edit_pipeline(PIPELINE_ID, body, event={})

        assert result["statusCode"] == 200
        # Verify schedule was cleared
        update_call = mock_pipelines_table.update_item.call_args
        assert update_call[1]["ExpressionAttributeValues"][":schedule"] == ""
        # Verify rule was deleted (called with None)
        mock_manage.assert_called_once_with(PIPELINE_ID, None, TRIGGER_ARN, PIPELINE_ID)

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    def test_returns_400_for_invalid_schedule_on_edit(self, mock_manage):
        mock_manage.side_effect = ValueError("Invalid schedule expression: bad-cron")

        body = json.dumps({"schedule": "bad-cron"})

        result = edit_pipeline(PIPELINE_ID, body, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 400
        assert "Invalid schedule expression" in body_resp["error"]
        # DynamoDB update should NOT have been called
        mock_pipelines_table.update_item.assert_not_called()

    # -- 2026-10-10: the stored source_type, and a target that did not attach --

    @pytest.fixture
    def stored_record(self):
        """Set what `pipelines_table.get_item` returns; reset it afterwards,
        because `reset_mock()` in the module fixture keeps return values."""

        def _store(item):
            mock_pipelines_table.get_item.return_value = {"Item": item} if item is not None else {}

        yield _store
        mock_pipelines_table.get_item.reset_mock(return_value=True)

    @pytest.mark.parametrize(
        ("stored", "expected"), [("JDBC", "JDBC"), ("S3_UPLOAD", "S3_UPLOAD"), (None, "S3_UPLOAD")]
    )
    @patch("handlers.pipelines_crud._manage_schedule_rule")
    def test_the_rule_carries_the_stored_source_type(self, mock_manage, stored_record, stored, expected):
        """An edit cannot change source_type, so it comes from the record. A
        record with none predates P17 and is an S3 upload pipeline."""
        stored_record({"pipeline_id": PIPELINE_ID, "source_type": stored} if stored else {"pipeline_id": PIPELINE_ID})

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": SCHEDULE_EXPR}), event={})

        assert result["statusCode"] == 200
        assert mock_manage.call_args.kwargs["source_type"] == expected

    # -- Clearing a schedule: the rule goes first, the record only after --
    # It used to write the empty schedule, then try the delete and log any
    # failure, returning 200 while the rule kept running.

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_failed_remove_targets_entry_fails_the_clear_without_writing(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}
        mock_events.remove_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [{"TargetId": "pipeline-trigger", "ErrorCode": "InternalException", "ErrorMessage": "x"}],
        }

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": ""}), event={})

        assert result["statusCode"] == 500
        body_resp = json.loads(result["body"])
        assert body_resp["stage"] == "schedule"
        assert "Schedule was not cleared" in body_resp["error"]
        assert "InternalException" in body_resp["error"]
        mock_pipelines_table.update_item.assert_not_called()
        mock_events.delete_rule.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_raised_removal_error_fails_the_clear_without_writing(self, mock_boto_client):
        from botocore.exceptions import ClientError

        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}
        mock_events.remove_targets.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "RemoveTargets"
        )

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": ""}), event={})

        assert result["statusCode"] == 500
        mock_pipelines_table.update_item.assert_not_called()

    @patch("handlers.pipelines_crud.boto3.client")
    def test_the_rule_is_removed_before_the_record_is_cleared(self, mock_boto_client):
        order = MagicMock()
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.return_value = {"Targets": [{"Id": "pipeline-trigger"}]}
        mock_events.remove_targets.return_value = {"FailedEntryCount": 0, "FailedEntries": []}
        order.attach_mock(mock_events.delete_rule, "delete_rule")
        order.attach_mock(mock_pipelines_table.update_item, "update_item")

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": ""}), event={})

        assert result["statusCode"] == 200
        assert [c[0] for c in order.mock_calls] == ["delete_rule", "update_item"]
        assert mock_pipelines_table.update_item.call_args.kwargs["ExpressionAttributeValues"][":schedule"] == ""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_clearing_a_schedule_whose_rule_is_already_gone_succeeds(self, mock_boto_client):
        from botocore.exceptions import ClientError

        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.list_targets_by_rule.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no rule"}}, "ListTargetsByRule"
        )

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": ""}), event={})

        assert result["statusCode"] == 200
        mock_pipelines_table.update_item.assert_called_once()

    @patch("handlers.pipelines_crud._manage_schedule_rule")
    def test_clearing_does_not_need_the_trigger_arn(self, mock_manage):
        """Deleting a rule never uses the trigger ARN, and the delete used to be
        skipped outright when it was unset, leaving the rule running."""
        original = pipelines.require_env

        def without_trigger_arn(name, default=None):
            return "" if name == "PIPELINE_TRIGGER_ARN" else original(name, default)

        pipelines.require_env = without_trigger_arn
        try:
            result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": ""}), event={})
        finally:
            pipelines.require_env = original

        assert result["statusCode"] == 200
        mock_manage.assert_called_once()
        assert mock_manage.call_args.args[1] is None

    @patch("handlers.pipelines_crud.boto3.client")
    def test_a_target_that_did_not_attach_fails_the_edit_without_writing(self, mock_boto_client, stored_record):
        stored_record({"pipeline_id": PIPELINE_ID, "source_type": "JDBC"})
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.put_targets.return_value = {
            "FailedEntryCount": 1,
            "FailedEntries": [{"TargetId": "pipeline-trigger", "ErrorCode": "InternalException", "ErrorMessage": "x"}],
        }

        result = edit_pipeline(PIPELINE_ID, json.dumps({"schedule": SCHEDULE_EXPR}), event={})

        assert result["statusCode"] == 500
        body_resp = json.loads(result["body"])
        assert body_resp["stage"] == "schedule"
        assert "Schedule was not saved" in body_resp["error"]
        mock_pipelines_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# archive_pipeline schedule rule tests
# ---------------------------------------------------------------------------


class TestArchivePipelineSchedule:
    """Validates: Requirement 3.8"""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_disables_schedule_rule_on_archive(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        result = archive_pipeline(PIPELINE_ID, event={})
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 200
        # Should attempt to disable both S3 trigger and schedule rules
        disable_calls = mock_events.disable_rule.call_args_list
        rule_names = [c[1]["Name"] for c in disable_calls]
        assert f"agentic-data-ingestion-schedule-{PIPELINE_ID}" in rule_names

    @patch("handlers.pipelines_crud.boto3.client")
    def test_archive_succeeds_when_no_schedule_rule(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events
        mock_events.disable_rule.side_effect = Exception("ResourceNotFoundException")

        result = archive_pipeline(PIPELINE_ID, event={})

        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# restore_pipeline schedule rule tests
# ---------------------------------------------------------------------------


class TestRestorePipelineSchedule:
    """Validates: Requirement 3.9"""

    @patch("handlers.pipelines_crud.boto3.client")
    def test_enables_schedule_rule_on_restore(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "status": "archived",
                "schedule": SCHEDULE_EXPR,
            }
        }

        result = restore_pipeline(PIPELINE_ID)
        body_resp = json.loads(result["body"])

        assert result["statusCode"] == 200
        # Should enable both S3 trigger and schedule rules
        enable_calls = mock_events.enable_rule.call_args_list
        rule_names = [c[1]["Name"] for c in enable_calls]
        assert f"agentic-data-ingestion-schedule-{PIPELINE_ID}" in rule_names

    @patch("handlers.pipelines_crud.boto3.client")
    def test_restore_skips_schedule_enable_when_no_schedule(self, mock_boto_client):
        mock_events = MagicMock()
        mock_boto_client.return_value = mock_events

        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "status": "archived",
            }
        }

        result = restore_pipeline(PIPELINE_ID)

        assert result["statusCode"] == 200
        # Should only enable S3 trigger rule, not schedule rule
        enable_calls = mock_events.enable_rule.call_args_list
        rule_names = [c[1]["Name"] for c in enable_calls]
        assert f"agentic-data-ingestion-schedule-{PIPELINE_ID}" not in rule_names


# ---------------------------------------------------------------------------
# get_pipeline_details schedule visibility tests
# ---------------------------------------------------------------------------


class TestPipelineDetailsScheduleVisibility:
    """Validates: Requirement 3.11"""

    def test_schedule_visible_in_details_basic_response(self):
        """When pipeline has no state data, schedule appears in basic response."""
        mock_state_table.query.return_value = {"Items": []}
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "pipeline_name": "Scheduled Pipeline",
                "status": "active",
                "schedule": SCHEDULE_EXPR,
                "target_schema_name": "test",
                "ai_model": "claude-3",
            }
        }

        result = get_pipeline_details(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert body["schedule"] == SCHEDULE_EXPR

    def test_no_schedule_field_when_not_configured(self):
        """When pipeline has no schedule, the field is absent."""
        mock_state_table.query.return_value = {"Items": []}
        mock_pipelines_table.get_item.return_value = {
            "Item": {
                "pipeline_id": PIPELINE_ID,
                "pipeline_name": "No Schedule Pipeline",
                "status": "active",
                "target_schema_name": "test",
                "ai_model": "claude-3",
            }
        }

        result = get_pipeline_details(PIPELINE_ID)
        body = json.loads(result["body"])

        assert result["statusCode"] == 200
        assert "schedule" not in body
