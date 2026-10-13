# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for P28 R2 — approval task-token integrity (read → send → delete).

Validates Requirements 2.1–2.5:
- 2.1: send_task_success/send_task_failure is called BEFORE the token record is
  deleted and BEFORE the approval is marked processed
- 2.2: transient send failure → 5xx returned, token record retained, approval
  NOT marked processed (user can retry)
- 2.3: already-consumed token (TaskTimedOut, TaskDoesNotExist) → token record
  deleted, approval marked expired, honest 409 returned
- 2.4: the swallow-except around the send is gone — a failed send fails the request
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

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
os.environ.setdefault("DYNAMODB_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

# ---------------------------------------------------------------------------
# Mock setup
# ---------------------------------------------------------------------------
mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()
mock_sfn_client = MagicMock()

_table_map = {
    "test-approvals": mock_approvals_table,
    "test-task-tokens": mock_task_tokens_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    mock_client.return_value = MagicMock()
    from handlers import approvals as approvals_mod


APPROVAL_ID = "approval-ordering-001"
TASK_TOKEN = "ordering-test-token-value"


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_sfn_client.reset_mock()
    mock_sfn_client.send_task_success.side_effect = None
    mock_sfn_client.send_task_failure.side_effect = None
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.approvals_table = mock_approvals_table
    yield


def _approval_record():
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": True,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "created_at": "2025-01-15T12:00:00Z",
    }


def _wire_happy_tables():
    mock_approvals_table.get_item.return_value = {"Item": _approval_record()}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "SendTask")


def _install_send_probe(send_mock) -> dict:
    """Record, at the moment the send is invoked, whether the token record had
    already been deleted or the approval already updated (R2.1 ordering)."""
    observed = {}

    def _probe(*args, **kwargs):
        observed["token_deleted_at_send_time"] = mock_task_tokens_table.delete_item.called
        observed["approval_updated_at_send_time"] = mock_approvals_table.update_item.called
        return {}

    send_mock.side_effect = _probe
    return observed


# ---------------------------------------------------------------------------
# Happy path: read token → send → delete → mark approval (R2.1, R2.5)
# ---------------------------------------------------------------------------


class TestApproveSendBeforeDelete:
    def test_approve_sends_before_deleting_token_and_updating_status(self):
        _wire_happy_tables()
        observed = _install_send_probe(mock_sfn_client.send_task_success)

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["success"] is True
        assert body["resumed_execution"] is True

        # At send time nothing was deleted or marked processed yet
        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False

        # After a successful send the token record is deleted and status marked
        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        approved_updates = [
            c
            for c in mock_approvals_table.update_item.call_args_list
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":status") == "approved"
        ]
        assert len(approved_updates) == 1


class TestRejectSendBeforeDelete:
    def test_reject_sends_before_deleting_token_and_updating_status(self):
        _wire_happy_tables()
        observed = _install_send_probe(mock_sfn_client.send_task_failure)

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "bad data"}))

        assert result["statusCode"] == 200

        assert observed["token_deleted_at_send_time"] is False
        assert observed["approval_updated_at_send_time"] is False

        mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
        rejected_updates = [
            c
            for c in mock_approvals_table.update_item.call_args_list
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":status") == "rejected"
        ]
        assert len(rejected_updates) == 1


# ---------------------------------------------------------------------------
# Transient send failure: 5xx, token kept, approval untouched (R2.2, R2.4)
# ---------------------------------------------------------------------------


class TestTransientSendFailure:
    def test_approve_throttling_returns_5xx_keeps_token_and_pending_status(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error("ThrottlingException")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        # 5xx so the caller knows to retry — no swallowed send failure
        assert 500 <= result["statusCode"] < 600
        # Token record retained for the retry
        mock_task_tokens_table.delete_item.assert_not_called()
        # Approval NOT marked processed
        mock_approvals_table.update_item.assert_not_called()

    def test_approve_service_error_returns_5xx_keeps_token_and_pending_status(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error("ServiceUnavailable")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        assert 500 <= result["statusCode"] < 600
        mock_task_tokens_table.delete_item.assert_not_called()
        mock_approvals_table.update_item.assert_not_called()

    def test_reject_throttling_returns_5xx_keeps_token_and_pending_status(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_failure.side_effect = _client_error("ThrottlingException")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "nope"}))

        assert 500 <= result["statusCode"] < 600
        mock_task_tokens_table.delete_item.assert_not_called()
        mock_approvals_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Already-consumed token: cleanup + honest 409 (R2.3)
# ---------------------------------------------------------------------------


def _assert_consumed_response(result):
    assert result["statusCode"] == 409
    body = json.loads(result["body"])
    assert body["approval_id"] == APPROVAL_ID
    assert body["status"] == "expired"
    assert "already processed or expired" in body["error"].lower()
    # Token record cleaned up
    mock_task_tokens_table.delete_item.assert_called_once_with(Key={"approval_id": APPROVAL_ID})
    # Approval marked expired — never approved/rejected
    statuses = [
        c.kwargs.get("ExpressionAttributeValues", {}).get(":status")
        for c in mock_approvals_table.update_item.call_args_list
    ]
    assert statuses == ["expired"]


class TestConsumedTokenOnApprove:
    def test_task_timed_out_returns_409_and_cleans_up(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error("TaskTimedOut")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        _assert_consumed_response(result)

    def test_task_does_not_exist_returns_409_and_cleans_up(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_success.side_effect = _client_error("TaskDoesNotExist")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))

        _assert_consumed_response(result)


class TestConsumedTokenOnReject:
    def test_task_timed_out_returns_409_and_cleans_up(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_failure.side_effect = _client_error("TaskTimedOut")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "late"}))

        _assert_consumed_response(result)

    def test_task_does_not_exist_returns_409_and_cleans_up(self):
        _wire_happy_tables()
        mock_sfn_client.send_task_failure.side_effect = _client_error("TaskDoesNotExist")

        with patch("boto3.client") as patched_client:
            patched_client.return_value = mock_sfn_client
            result = approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "late"}))

        _assert_consumed_response(result)


# ---------------------------------------------------------------------------
# Where the executions-row clear sits in the sequence of writes
#
# The clear (`_annotate_execution_row`) is the ONLY write that removes
# `pending_approval_id`, and `pipelines_runs.get_run_details` derives
# `status = "WAITING_APPROVAL"` from that attribute's mere presence — nothing
# stores the literal. So the clear's position in this function IS the length of
# time the dashboard renders "Awaiting Approval" for a run that has already
# resumed.
#
# Two orderings are asserted here and they pull in opposite directions, which is
# why both need pinning:
#
#   send  <  clear  <  first store_mapping_decision
#
# The right-hand side is the fix: the Memory block is 20+ serial AgentCore round
# trips for a 20-field schema, and it used to run before the clear. The left-hand
# side is the constraint that stops the fix from going too far — P28 R2.1 and the
# comment at the send require a failed send to leave the approval actionable, so
# no row may be marked RUNNING for a run that never resumed.
#
# Ordering is asserted through ONE recorder mock (relative order in
# `recorder.mock_calls`), not by "both were called" — an ordering test that
# cannot fail when the order reverts is worthless.
# ---------------------------------------------------------------------------

SCHEMA_MAPPING_PROPOSED = [
    {"source_field": "cust_id", "target_field": "customer_id", "confidence": 95},
    {"source_field": "fname", "target_field": "first_name", "confidence": 88},
]


def _schema_mapping_record():
    """An approval whose decision path runs the Memory block AND the state echo.

    `schema_mapping` is deliberately not in `_APPROVAL_TYPES_WITHOUT_STATE_ECHO`,
    so this record exercises both — which is what made the shared `try` a defect
    rather than a stylistic wrinkle.
    """
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": True,
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": "schema_mapping",
        "proposed_mappings": SCHEMA_MAPPING_PROPOSED,
        "created_at": "2025-01-15T12:00:00Z",
    }


@pytest.fixture
def write_recorder():
    """One parent mock recording the three writes whose ORDER is under test.

    The children are bound onto the real collaborators so the production code
    reaches them by its normal route:

    - ``clear_executions_row``      ← ``handlers.common.executions_table.update_item``
      (``_annotate_execution_row`` imports it at call time, so the module
      attribute is what to patch)
    - ``store_mapping_decision``   ← the ``MappingMemoryClient`` instance's method
    - ``send_task_success`` / ``send_task_failure`` ← the Step Functions client

    ``state_table`` is bound too, but NOT onto the recorder: its calls do not
    participate in the ordering assertions, only in the failure injection below.
    """
    from handlers import common as common_mod

    recorder = MagicMock()

    exec_table = MagicMock()
    exec_table.update_item = recorder.clear_executions_row

    memory_client = MagicMock()
    memory_client.store_mapping_decision = recorder.store_mapping_decision

    sfn = MagicMock()
    sfn.send_task_success = recorder.send_task_success
    sfn.send_task_failure = recorder.send_task_failure

    state_table = MagicMock()
    # `"Item" in <MagicMock>` is False, so the mapping_id lookup finds nothing —
    # which is the ordinary case and keeps this fixture out of the Memory block's
    # branch logic.
    state_table.get_item.return_value = {}

    original_exec = common_mod.executions_table
    original_state = approvals_mod.state_table
    common_mod.executions_table = exec_table
    approvals_mod.state_table = state_table

    recorder.state_table = state_table
    recorder.memory_client = memory_client
    recorder.sfn = sfn
    try:
        yield recorder
    finally:
        common_mod.executions_table = original_exec
        approvals_mod.state_table = original_state


def _ordered_write_names(recorder) -> list[str]:
    """The recorded write names in call order (`state_table`/helpers excluded)."""
    tracked = {"clear_executions_row", "store_mapping_decision", "send_task_success", "send_task_failure"}
    return [name for name, _args, _kwargs in recorder.mock_calls if name in tracked]


def _decide(recorder, *, approve: bool):
    mock_approvals_table.get_item.return_value = {"Item": _schema_mapping_record()}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}

    with (
        patch("handlers.approvals.MappingMemoryClient", return_value=recorder.memory_client),
        patch("boto3.client", return_value=recorder.sfn),
    ):
        if approve:
            return approvals_mod.approve_request(APPROVAL_ID, json.dumps({"user": "tester"}))
        return approvals_mod.reject_request(APPROVAL_ID, json.dumps({"user": "tester", "reason": "bad mapping"}))


class TestClearPrecedesTheMemoryBlock:
    """The clear must land BEFORE the first Memory round trip, on both paths.

    Reverting the hoist puts `store_mapping_decision` first and fails these.
    """

    def test_approve_clears_the_executions_row_before_the_first_memory_write(self, write_recorder):
        assert _decide(write_recorder, approve=True)["statusCode"] == 200

        names = _ordered_write_names(write_recorder)
        assert "clear_executions_row" in names
        assert "store_mapping_decision" in names
        assert names.index("clear_executions_row") < names.index("store_mapping_decision")

    def test_reject_clears_the_executions_row_before_the_first_memory_write(self, write_recorder):
        assert _decide(write_recorder, approve=False)["statusCode"] == 200

        names = _ordered_write_names(write_recorder)
        assert "clear_executions_row" in names
        assert "store_mapping_decision" in names
        assert names.index("clear_executions_row") < names.index("store_mapping_decision")


class TestSendStillPrecedesTheClear:
    """P28 R2.1, from the other side: the clear may move earlier, but never above
    the send. A row marked RUNNING for a run whose resume failed would claim work
    that never happened, and the approval must stay actionable."""

    def test_approve_sends_before_clearing_the_executions_row(self, write_recorder):
        assert _decide(write_recorder, approve=True)["statusCode"] == 200

        names = _ordered_write_names(write_recorder)
        assert names.index("send_task_success") < names.index("clear_executions_row")

    def test_reject_sends_before_clearing_the_executions_row(self, write_recorder):
        assert _decide(write_recorder, approve=False)["statusCode"] == 200

        names = _ordered_write_names(write_recorder)
        assert names.index("send_task_failure") < names.index("clear_executions_row")


class TestStateEchoFailureCannotSkipTheClear:
    """A failing state echo must not take the executions clear down with it.

    The echo and the clear used to share one `try`. The echo carries
    `ConditionExpression="attribute_exists(run_id)"`, which raises
    `ConditionalCheckFailedException` whenever no `{run_id, "schema_mapping"}`
    state row exists — an ordinary outcome — and that raise unwound past the
    clear, so the executions row kept `pending_approval_id` and the run showed
    "Awaiting Approval" permanently, logged only as a state-table failure.

    This had no coverage because `test_approval_state_echo_canonical.py`'s
    `mock_state_table` is a bare `MagicMock` that never raises. These tests force
    the raise.
    """

    def test_approve_still_clears_the_executions_row(self, write_recorder):
        write_recorder.state_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")

        result = _decide(write_recorder, approve=True)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "approved"
        write_recorder.clear_executions_row.assert_called_once()
        assert write_recorder.clear_executions_row.call_args.kwargs["ExpressionAttributeValues"][":status"] == "RUNNING"

    def test_approve_still_clears_the_executions_row_on_an_ordinary_echo_failure(self, write_recorder):
        """The non-conditional branch must be just as survivable — the two
        branches differ in log level, not in control flow."""
        write_recorder.state_table.update_item.side_effect = RuntimeError("dynamo is unwell")

        assert _decide(write_recorder, approve=True)["statusCode"] == 200
        write_recorder.clear_executions_row.assert_called_once()

    def test_reject_still_clears_the_executions_row(self, write_recorder):
        """The reject path has NO state echo (`LGR-5d68eeb-2` keeps it that way),
        so this pins the complementary property: its clear is not downstream of
        any state-table write and cannot be skipped by one."""
        write_recorder.state_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")

        result = _decide(write_recorder, approve=False)

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "rejected"
        write_recorder.clear_executions_row.assert_called_once()
        assert write_recorder.clear_executions_row.call_args.kwargs["ExpressionAttributeValues"][":status"] == "FAILED"
