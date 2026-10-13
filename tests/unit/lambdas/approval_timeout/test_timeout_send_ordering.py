# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: Send_Before_Status in the approval_timeout_handler (P28b R3, task 4.2).

`auto_approve_timeout` / `auto_reject_timeout` used to write the timeout
outcome status BEFORE sending the task-token callback — and read the token
from the approval record, which never carries one (tokens live only in the
dedicated task-tokens table; the record carries has_task_token), so the
callback never actually sent. The resequenced flow mirrors the
approval_handler (p28b task 4.1): read token from the task-tokens table →
send → delete token record → write status.

Mirrors tests/unit/lambdas/approval_handler/test_approval_send_ordering.py —
`_install_send_probe` records, at send time, whether the status write / token
delete already happened. Pins:

- (a) ordering: the callback send is observed BEFORE the approval-record
  status write (auto-approve and auto-reject)
- (b) transient send failure: status remains "pending" (no approved/rejected
  write), token record retained, AND the item is reported failed in the
  processing result so the next scheduled sweep retries
- (c) consumed-set error (TaskTimedOut, TaskDoesNotExist, InvalidToken):
  status becomes "expired" — never approved/rejected — already-processed
- plus: token record absent while has_task_token → "expired",
  already-processed; no-token approvals write status directly; the manual
  path consumes no token

Validates: Requirements 3.2, 3.4, 3.5, 3.6
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_timeout_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Deterministic sys.modules stubs — installed with assignment (not setdefault)
# so this file is immune to stub pollution from sibling test modules (see
# approval_handler/test_state_row_echo_canonical.py for the rationale).
# ---------------------------------------------------------------------------
_metrics_stub = MagicMock()
_metrics_stub.time_function = lambda *_a, **_k: lambda f: f  # pass-through decorator factory
_metrics_stub.ApprovalMetrics = MagicMock()
_metrics_stub.record_error = MagicMock()
sys.modules["metrics_facade"] = _metrics_stub

_env_stub = MagicMock()
_env_stub.require_env = lambda name, default=None: os.environ.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _env_stub

sys.modules["eventbridge_utils"] = MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
sys.modules["notification_service"] = MagicMock(
    notification_service=MagicMock(send_notification=MagicMock(return_value={"success": True})),
    send_approval_timeout_alert=MagicMock(return_value={"success": True}),
)

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_sfn_client = MagicMock()
mock_cloudwatch_client = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
}

try:
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_dynamo = MagicMock()
        mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
        mock_resource.return_value = mock_dynamo

        sys.modules.pop("lambda_function", None)
        _spec = importlib.util.spec_from_file_location(
            "approval_timeout_send_ordering_lf",
            os.path.join(LAMBDA_DIR, "lambda_function.py"),
        )
        handler = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(handler)
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


APPROVAL_ID = "pipe-001_code_generation_1700000000"
TASK_TOKEN = "timeout-ordering-test-token-value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    for m in (mock_task_tokens_table, mock_approvals_table, mock_dynamo, mock_cloudwatch_client):
        m.reset_mock(return_value=True, side_effect=True)
    mock_sfn_client.reset_mock(return_value=True, side_effect=True)
    mock_sfn_client.send_task_success.side_effect = None
    mock_sfn_client.send_task_failure.side_effect = None
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    handler.task_tokens_table = mock_task_tokens_table
    handler.approvals_table = mock_approvals_table
    handler.stepfunctions = mock_sfn_client
    handler.cloudwatch = mock_cloudwatch_client
    handler.dynamodb = mock_dynamo
    yield


def _approval_record(timeout_behavior: str, has_task_token: bool = True) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": "code_generation",
        "status": "pending",
        "timeout_at": "2020-01-01T00:00:00+00:00",
        "timeout_behavior": timeout_behavior,
        "has_task_token": has_task_token,
    }


def _wire_happy_tables(record: dict):
    mock_approvals_table.scan.return_value = {"Items": [record]}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "SendTask")


def _install_send_probe(send_mock) -> dict:
    """Record, at the moment the send is invoked, whether any write had
    already happened (R3.2 Send_Before_Status ordering)."""
    observed = {}

    def _probe(*args, **kwargs):
        observed["token_deleted_at_send_time"] = mock_task_tokens_table.delete_item.called
        observed["approval_updated_at_send_time"] = mock_approvals_table.update_item.called
        return {}

    send_mock.side_effect = _probe
    return observed


def _approval_status_writes() -> list:
    """The :status values written to the approvals table, in call order."""
    return [
        c.kwargs.get("ExpressionAttributeValues", {}).get(":status")
        for c in mock_approvals_table.update_item.call_args_list
    ]


def _run_sweep() -> dict:
    """Drive the scheduled sweep through lambda_handler; return the report."""
    result = handler.lambda_handler({}, None)
    assert result["statusCode"] == 200
    return json.loads(result["body"])["report"]


# ---------------------------------------------------------------------------
# (a) Ordering: send BEFORE the status write (R3.2)
# ---------------------------------------------------------------------------


class TestSendBeforeStatusWrite:
    def test_auto_approve_sends_before_status_write_and_token_delete(self):
        _wire_happy_tables(_approval_record("auto_approve"))
        observed = _install_send_probe(mock_sfn_client.send_task_success)

        report = _run_sweep()

        # At send time NOTHING had been written yet
        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False

        # The send actually happened — with the token read from the dedicated
        # task-tokens table (the approval record never carries the token)
        mock_sfn_client.send_task_success.assert_called_once()
        assert mock_sfn_client.send_task_success.call_args.kwargs["taskToken"] == TASK_TOKEN

        # After the send: token record deleted, timeout outcome written with
        # the existing timeout_reason provenance
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["approved"]
        update_kwargs = mock_approvals_table.update_item.call_args.kwargs
        assert update_kwargs["ExpressionAttributeValues"][":reason"] == "auto_approved_on_timeout"
        assert update_kwargs["ExpressionAttributeValues"][":processed_by"] == "system_timeout"

        assert report["timeout_details"][0]["success"] is True

    def test_auto_reject_sends_failure_before_status_write_and_token_delete(self):
        _wire_happy_tables(_approval_record("auto_reject"))
        observed = _install_send_probe(mock_sfn_client.send_task_failure)

        report = _run_sweep()

        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False

        mock_sfn_client.send_task_failure.assert_called_once()
        assert mock_sfn_client.send_task_failure.call_args.kwargs["taskToken"] == TASK_TOKEN
        assert mock_sfn_client.send_task_failure.call_args.kwargs["error"] == "ApprovalTimeout"

        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["rejected"]
        update_kwargs = mock_approvals_table.update_item.call_args.kwargs
        assert update_kwargs["ExpressionAttributeValues"][":reason"] == "auto_rejected_on_timeout"

        assert report["timeout_details"][0]["success"] is True

    def test_no_token_approval_writes_status_directly_without_send(self):
        _wire_happy_tables(_approval_record("auto_approve", has_task_token=False))

        report = _run_sweep()

        mock_sfn_client.send_task_success.assert_not_called()
        mock_sfn_client.send_task_failure.assert_not_called()
        mock_task_tokens_table.get_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()
        assert _approval_status_writes() == ["approved"]
        assert report["timeout_details"][0]["success"] is True


# ---------------------------------------------------------------------------
# (b) Transient send failure: pending stays pending, token retained, item
#     reported failed in the processing result (R3.4)
# ---------------------------------------------------------------------------


class TestTransientSendFailure:
    def test_auto_approve_throttling_keeps_pending_and_reports_item_failed(self):
        _wire_happy_tables(_approval_record("auto_approve"))
        mock_sfn_client.send_task_success.side_effect = _client_error("ThrottlingException")

        report = _run_sweep()

        # Approval NOT marked processed — status stays "pending" (no write at all)
        mock_approvals_table.update_item.assert_not_called()
        # Token record retained for the next scheduled sweep's retry
        mock_task_tokens_table.delete_item.assert_not_called()
        # The item is reported FAILED in the processing result
        assert report["timeout_details"][0]["success"] is False
        assert report["processing_errors"] == 1

    def test_auto_reject_service_error_keeps_pending_and_reports_item_failed(self):
        _wire_happy_tables(_approval_record("auto_reject"))
        mock_sfn_client.send_task_failure.side_effect = _client_error("ServiceUnavailable")

        report = _run_sweep()

        mock_approvals_table.update_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()
        assert report["timeout_details"][0]["success"] is False
        assert report["processing_errors"] == 1


# ---------------------------------------------------------------------------
# (c) Consumed/expired token: status "expired", never approved/rejected (R3.5)
# ---------------------------------------------------------------------------


class TestConsumedToken:
    @pytest.mark.parametrize("error_code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
    def test_consumed_set_on_auto_approve_marks_expired_already_processed(self, error_code):
        _wire_happy_tables(_approval_record("auto_approve"))
        mock_sfn_client.send_task_success.side_effect = _client_error(error_code)

        report = _run_sweep()

        # Token record cleaned up; approval marked expired — never approved
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["expired"]
        # Already-processed, NOT a retry
        assert report["timeout_details"][0]["success"] is True
        assert report["processing_errors"] == 0

    def test_consumed_set_on_auto_reject_marks_expired_never_rejected(self):
        _wire_happy_tables(_approval_record("auto_reject"))
        mock_sfn_client.send_task_failure.side_effect = _client_error("TaskTimedOut")

        report = _run_sweep()

        assert _approval_status_writes() == ["expired"]
        assert report["timeout_details"][0]["success"] is True


# ---------------------------------------------------------------------------
# Token record absent while has_task_token: "expired", already-processed
# ---------------------------------------------------------------------------


class TestTokenRecordAbsent:
    @pytest.mark.parametrize("timeout_behavior", ["auto_approve", "auto_reject"])
    def test_missing_token_record_marks_expired_already_processed(self, timeout_behavior):
        _wire_happy_tables(_approval_record(timeout_behavior))
        mock_task_tokens_table.get_item.return_value = {}

        report = _run_sweep()

        # Nothing sent — the callback can never be delivered
        mock_sfn_client.send_task_success.assert_not_called()
        mock_sfn_client.send_task_failure.assert_not_called()
        # Nothing to delete (record already absent); approval marked expired
        mock_task_tokens_table.delete_item.assert_not_called()
        assert _approval_status_writes() == ["expired"]
        # Already-processed, NOT a retry
        assert report["timeout_details"][0]["success"] is True


# ---------------------------------------------------------------------------
# Manual timeout handling: unchanged — no token consumption
# ---------------------------------------------------------------------------


class TestManualTimeoutUnchanged:
    def test_manual_behavior_touches_no_token_and_sends_nothing(self):
        _wire_happy_tables(_approval_record("manual"))

        report = _run_sweep()

        mock_sfn_client.send_task_success.assert_not_called()
        mock_sfn_client.send_task_failure.assert_not_called()
        mock_task_tokens_table.get_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()
        # The manual path marks timeout_occurred but never writes a status
        assert _approval_status_writes() == [None]
        update_kwargs = mock_approvals_table.update_item.call_args.kwargs
        assert update_kwargs["ExpressionAttributeValues"][":timeout_occurred"] is True
        assert report["timeout_details"][0]["success"] is True
