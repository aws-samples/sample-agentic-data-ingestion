# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: Send_Before_Status in the approval_handler (P28b R3, task 4.1).

`process_approval_decision` used to write the decision status BEFORE sending
the task-token callback — and read the token from the approval record, which
never carries one (tokens live only in the dedicated task-tokens table), so
the callback never actually sent. The resequenced flow mirrors the dashboard
(p28 task 3.1): read token from the task-tokens table → send → delete token
record → write status.

Mirrors tests/unit/lambdas/dashboard_api/test_approval_send_ordering.py —
`_install_send_probe` records, at send time, whether the status write / token
delete already happened. Pins:

- (a) ordering: the callback send is observed BEFORE the approval-record
  status write (and before the token delete, state echo, executions clear)
- (b) transient send failure: status remains "pending" (no approved/rejected
  write), token record retained, caller gets a 500 so it can retry
- (c) consumed-set error (TaskTimedOut, TaskDoesNotExist, InvalidToken):
  status becomes "expired" — never approved/rejected — with a 409-style
  already-processed response

(a)-(c) are asserted for a `code_generation` record. The two classes at the
bottom of the file repeat the ordering half and the expired-on-replay half for
the `test_failure` type (p32 3.17), which reaches the same function with no
state-row echo — see the comment block there for why the ordering carries more
weight for that type than for any other.

Validates: Requirements 3.1, 3.3, 3.5, 3.6; p32 3.17
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
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")

# ---------------------------------------------------------------------------
# Deterministic sys.modules stubs — installed with assignment (not setdefault)
# so this file is immune to stub pollution from sibling test modules (see
# test_state_row_echo_canonical.py for the rationale).
# ---------------------------------------------------------------------------
_metrics_stub = MagicMock()
_metrics_stub.time_function = lambda *_a, **_k: (lambda f: f)  # pass-through decorator factory
_metrics_stub.ApprovalMetrics = MagicMock()
_metrics_stub.record_error = MagicMock()
_metrics_stub.PipelineMetrics = None
sys.modules["metrics_facade"] = _metrics_stub

_env_stub = MagicMock()
_env_stub.require_env = lambda name, default=None: os.environ.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _env_stub

sys.modules["eventbridge_utils"] = MagicMock(
    get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())
)
sys.modules["notification_service"] = MagicMock(
    send_approval_notification=MagicMock(return_value={"success": True}),
    send_approval_timeout_alert=MagicMock(return_value={"success": True}),
)

# ---------------------------------------------------------------------------
# Mock setup — patch boto3 before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()
mock_exec_table = MagicMock()
mock_sfn_client = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
    "agentic_data_ingestion_state": mock_state_table,
    "test-executions": mock_exec_table,
    "agentic_data_ingestion_executions": mock_exec_table,
}

try:
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_dynamo = MagicMock()
        mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
        mock_resource.return_value = mock_dynamo

        sys.modules.pop("lambda_function", None)
        _spec = importlib.util.spec_from_file_location(
            "approval_handler_send_ordering_lf",
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
TASK_TOKEN = "ordering-test-token-value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    for m in (mock_task_tokens_table, mock_approvals_table, mock_state_table, mock_exec_table, mock_dynamo):
        m.reset_mock(return_value=True, side_effect=True)
    mock_sfn_client.reset_mock(return_value=True, side_effect=True)
    mock_sfn_client.send_task_success.side_effect = None
    mock_sfn_client.send_task_failure.side_effect = None
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    handler.task_tokens_table = mock_task_tokens_table
    handler.approvals_table = mock_approvals_table
    handler.state_table = mock_state_table
    handler.dynamodb = mock_dynamo
    handler.stepfunctions = mock_sfn_client
    yield


def _approval_record(has_task_token: bool = True, approval_type: str = "code_generation") -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": has_task_token,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": approval_type,
        "created_at": "2025-01-15T12:00:00Z",
    }


def _wire_happy_tables(has_task_token: bool = True, approval_type: str = "code_generation"):
    mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token, approval_type)}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "SendTask")


def _install_send_probe(send_mock) -> dict:
    """Record, at the moment the send is invoked, whether any write had
    already happened (R3.1 Send_Before_Status ordering)."""
    observed = {}

    def _probe(*args, **kwargs):
        observed["token_deleted_at_send_time"] = mock_task_tokens_table.delete_item.called
        observed["approval_updated_at_send_time"] = mock_approvals_table.update_item.called
        observed["state_echoed_at_send_time"] = mock_state_table.update_item.called
        observed["executions_updated_at_send_time"] = mock_exec_table.update_item.called
        return {}

    send_mock.side_effect = _probe
    return observed


def _approval_status_writes() -> list:
    """The :status values written to the approvals table, in call order."""
    return [
        c.kwargs.get("ExpressionAttributeValues", {}).get(":status")
        for c in mock_approvals_table.update_item.call_args_list
    ]


def _decide(decision: str) -> dict:
    return handler.lambda_handler(
        {"approval_id": APPROVAL_ID, "decision": decision, "comment": "lgtm", "user": "tester"}, None
    )


# ---------------------------------------------------------------------------
# (a) Ordering: send BEFORE any write (R3.1)
# ---------------------------------------------------------------------------


class TestSendBeforeStatusWrite:
    def test_approve_sends_before_status_write_and_token_delete(self):
        _wire_happy_tables()
        observed = _install_send_probe(mock_sfn_client.send_task_success)

        result = _decide("approved")

        assert result["statusCode"] == 200

        # At send time NOTHING had been written yet
        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False
        assert observed["state_echoed_at_send_time"] is False
        assert observed["executions_updated_at_send_time"] is False

        # The send actually happened — with the token read from the dedicated
        # task-tokens table (the approval record never carries the token)
        mock_sfn_client.send_task_success.assert_called_once()
        assert mock_sfn_client.send_task_success.call_args.kwargs["taskToken"] == TASK_TOKEN

        # After the send: token record deleted, decision status written
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["approved"]

    def test_reject_sends_failure_before_status_write_and_token_delete(self):
        _wire_happy_tables()
        observed = _install_send_probe(mock_sfn_client.send_task_failure)

        result = _decide("rejected")

        assert result["statusCode"] == 200

        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False

        mock_sfn_client.send_task_failure.assert_called_once()
        assert mock_sfn_client.send_task_failure.call_args.kwargs["taskToken"] == TASK_TOKEN

        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["rejected"]

    def test_no_token_approval_writes_status_directly_without_send(self):
        _wire_happy_tables(has_task_token=False)

        result = _decide("approved")

        assert result["statusCode"] == 200
        mock_sfn_client.send_task_success.assert_not_called()
        mock_sfn_client.send_task_failure.assert_not_called()
        mock_task_tokens_table.get_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()
        assert _approval_status_writes() == ["approved"]


# ---------------------------------------------------------------------------
# (b) Transient send failure: pending stays pending, token retained (R3.3)
# ---------------------------------------------------------------------------


class TestTransientSendFailure:
    def test_approve_throttling_returns_500_keeps_token_and_pending_status(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error("ThrottlingException")

        result = _decide("approved")

        # 500 so the caller knows to retry
        assert result["statusCode"] == 500
        # Token record retained for the retry
        mock_task_tokens_table.delete_item.assert_not_called()
        # Approval NOT marked processed — status stays "pending"
        mock_approvals_table.update_item.assert_not_called()
        # No state echo, no executions clear either
        mock_state_table.update_item.assert_not_called()
        mock_exec_table.update_item.assert_not_called()

    def test_reject_service_error_returns_500_keeps_token_and_pending_status(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_failure.side_effect = _client_error("ServiceUnavailable")

        result = _decide("rejected")

        assert result["statusCode"] == 500
        mock_task_tokens_table.delete_item.assert_not_called()
        mock_approvals_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# (c) Consumed/expired token: status "expired", never approved/rejected (R3.5)
# ---------------------------------------------------------------------------


class TestConsumedToken:
    @pytest.mark.parametrize("error_code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
    def test_consumed_set_on_approve_marks_expired_and_returns_409(self, error_code):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error(error_code)

        result = _decide("approved")

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "ApprovalExpired"
        assert body["approval_id"] == APPROVAL_ID
        assert body["status"] == "expired"

        # Token record cleaned up; approval marked expired — never approved
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["expired"]

    def test_consumed_set_on_reject_marks_expired_never_rejected(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_failure.side_effect = _client_error("TaskTimedOut")

        result = _decide("rejected")

        assert result["statusCode"] == 409
        assert _approval_status_writes() == ["expired"]


# ---------------------------------------------------------------------------
# Token record absent (TTL-reaped): error response, approval untouched
# ---------------------------------------------------------------------------


class TestTokenRecordAbsent:
    def test_missing_token_record_returns_410_and_leaves_approval_untouched(self):
        _wire_happy_tables()
        mock_task_tokens_table.get_item.return_value = {}

        result = _decide("approved")

        assert result["statusCode"] == 410
        body = json.loads(result["body"])
        assert body["error"] == "TaskTokenNotFound"
        assert body["approval_id"] == APPROVAL_ID

        # Nothing sent, nothing written — the approval record stays pending
        mock_sfn_client.send_task_success.assert_not_called()
        mock_approvals_table.update_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# The same two guarantees for the `test_failure` approval type (p32 3.17)
#
# Everything above is asserted for a `code_generation` record. `test_failure` is
# the one gate where a wrongly-ordered write is worst, and it takes a different
# path through the same function:
#
#   * it is the forced test-review gate (P28 R4.2) — a human deciding to proceed
#     with code that did not pass testing, which 3.15 requires to stay auditable;
#   * and it is the only approval type in
#     `_APPROVAL_TYPES_WITHOUT_STATE_ECHO` (`lambda_function.py:81`), so it
#     writes NO state row. The test_code row keeps its honest "failed" status,
#     which means the approvals-record write is the *only* place the human
#     decision is recorded.
#
# Put those two together and the ordering is load-bearing here in a way it is
# not for `code_generation`: a status write that landed before a failed send
# would be the sole surviving record of a decision the execution never received.
# Hence both halves, for this type: send-before-write, and `expired` on a
# replayed token.
# ---------------------------------------------------------------------------

TEST_FAILURE = "test_failure"


class TestTestFailureTypedSendBeforeStatusWrite:
    """R3.1 ordering, for the forced test-review gate."""

    def test_approve_sends_before_the_only_record_of_the_decision(self):
        _wire_happy_tables(approval_type=TEST_FAILURE)
        observed = _install_send_probe(mock_sfn_client.send_task_success)

        result = _decide("approved")

        assert result["statusCode"] == 200

        # Nothing written at send time
        assert observed["approval_updated_at_send_time"] is False
        assert observed["token_deleted_at_send_time"] is False
        assert observed["state_echoed_at_send_time"] is False
        assert observed["executions_updated_at_send_time"] is False

        mock_sfn_client.send_task_success.assert_called_once()
        assert mock_sfn_client.send_task_success.call_args.kwargs["taskToken"] == TASK_TOKEN

        # ...and only then the decision, on the approvals record (3.15: an
        # approve-despite-failure stays auditable).
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        assert _approval_status_writes() == ["approved"]

    def test_reject_sends_failure_before_the_status_write(self):
        _wire_happy_tables(approval_type=TEST_FAILURE)
        observed = _install_send_probe(mock_sfn_client.send_task_failure)

        result = _decide("rejected")

        assert result["statusCode"] == 200
        assert observed["approval_updated_at_send_time"] is False
        assert observed["token_deleted_at_send_time"] is False

        mock_sfn_client.send_task_failure.assert_called_once()
        assert mock_sfn_client.send_task_failure.call_args.kwargs["error"] == "ApprovalRejected"
        assert _approval_status_writes() == ["rejected"]

    def test_transient_send_failure_leaves_no_record_of_a_decision(self):
        """R3.3 for this type: the run is still waiting, so nothing may claim otherwise."""
        _wire_happy_tables(approval_type=TEST_FAILURE)
        mock_sfn_client.send_task_success.side_effect = _client_error("ThrottlingException")

        result = _decide("approved")

        assert result["statusCode"] == 500
        mock_approvals_table.update_item.assert_not_called()
        mock_task_tokens_table.delete_item.assert_not_called()

    def test_decision_writes_no_state_row(self):
        """`test_failure` is in `_APPROVAL_TYPES_WITHOUT_STATE_ECHO`.

        Pinned here rather than assumed, because it is what makes the ordering
        above matter: with no state-row echo, the approvals record is the whole
        audit trail for the decision.
        """
        _wire_happy_tables(approval_type=TEST_FAILURE)

        assert _decide("approved")["statusCode"] == 200

        mock_state_table.update_item.assert_not_called()
        assert handler.state_echo_step_name(TEST_FAILURE) is None
        # The executions record still clears its pending approval.
        mock_exec_table.update_item.assert_called_once()


class TestTestFailureTypedConsumedToken:
    """R3.5 for this type: a replayed token is `expired`, never a decision."""

    @pytest.mark.parametrize("error_code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
    def test_replayed_token_on_approve_is_expired_never_approved(self, error_code):
        _wire_happy_tables(approval_type=TEST_FAILURE)
        mock_sfn_client.send_task_success.side_effect = _client_error(error_code)

        result = _decide("approved")

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "ApprovalExpired"
        assert body["status"] == "expired"

        writes = _approval_status_writes()
        assert writes == ["expired"]
        assert "approved" not in writes

    def test_replayed_token_on_reject_is_expired_never_rejected(self):
        _wire_happy_tables(approval_type=TEST_FAILURE)
        mock_sfn_client.send_task_failure.side_effect = _client_error("TaskTimedOut")

        result = _decide("rejected")

        assert result["statusCode"] == 409
        writes = _approval_status_writes()
        assert writes == ["expired"]
        assert "rejected" not in writes
