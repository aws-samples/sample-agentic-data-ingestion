# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: approval flow writes NO phantom state rows (P28 R8.3, task 14.2).

The approval_handler used to write state rows keyed ``step_name =
approval_type`` ("code_generation", "infrastructure", "first_run", ...) —
phantom rows no pipeline step owns. The status echo now:

- targets the CANONICAL step row (``from_any(approval_type)``),
- only UPDATES an existing row (``attribute_exists`` condition — an echo can
  never create a row), and
- is skipped entirely for ``test_failure`` so the test_code row's honest
  "failed" status (R4.3) survives an approve-anyway decision.

Validates: P28 Requirements 8.3, 8.5
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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
# so this file is immune to stub pollution from sibling test modules. Other
# test files stub metrics_facade with a bare MagicMock, which would turn the
# handler's @time_function decorator into a call-swallowing mock.
# ---------------------------------------------------------------------------
_metrics_stub = MagicMock()
_metrics_stub.time_function = lambda *_a, **_k: lambda f: f  # pass-through decorator factory
_metrics_stub.ApprovalMetrics = MagicMock()
_metrics_stub.record_error = MagicMock()
# Sibling test modules stub metrics_facade with PipelineMetrics=None and rely
# on setdefault seeing that shape — keep it so this stub is a superset.
_metrics_stub.PipelineMetrics = None
sys.modules["metrics_facade"] = _metrics_stub

_env_stub = MagicMock()
_env_stub.require_env = lambda name, default=None: os.environ.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _env_stub

sys.modules["eventbridge_utils"] = MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
sys.modules["notification_service"] = MagicMock(
    send_approval_notification=MagicMock(return_value={"success": True}),
    send_approval_timeout_alert=MagicMock(return_value={"success": True}),
)

# ---------------------------------------------------------------------------
# Mock setup — patch boto3.resource before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()
mock_exec_table = MagicMock()

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
            "approval_handler_echo_lf",
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


@pytest.fixture(autouse=True)
def _reset_mocks():
    for m in (mock_task_tokens_table, mock_approvals_table, mock_state_table, mock_exec_table, mock_dynamo):
        m.reset_mock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    # get_pipeline_config queries the state table — make it return no config
    mock_state_table.query.return_value = {"Items": []}
    mock_state_table.get_item.return_value = {}
    handler.task_tokens_table = mock_task_tokens_table
    handler.approvals_table = mock_approvals_table
    handler.state_table = mock_state_table
    handler.dynamodb = mock_dynamo
    yield


def _state_update_calls():
    """All update_item calls made against the state table mock."""
    return [c.kwargs for c in mock_state_table.update_item.call_args_list]


# ── state_echo_step_name policy ──────────────────────────────────────


class TestStateEchoStepName:
    def test_maps_approval_types_to_canonical_steps(self):
        assert handler.state_echo_step_name("schema_mapping") == "schema_mapping"
        assert handler.state_echo_step_name("code_generation") == "create_code"
        assert handler.state_echo_step_name("first_run") == "create_quality_checks"
        assert handler.state_echo_step_name("infrastructure") == "create_infrastructure"

    def test_test_failure_gets_no_echo(self):
        """R4.3 honesty: the test_code row's failed status must survive."""
        assert handler.state_echo_step_name("test_failure") is None

    def test_unknown_and_empty_types_get_no_echo(self):
        assert handler.state_echo_step_name("definitely_not_a_step") is None
        assert handler.state_echo_step_name("") is None


# ── create_approval_request: waiting_approval echo ───────────────────


class TestCreateRequestEcho:
    def _create(self, approval_type: str) -> None:
        event = {
            "pipeline_id": "pipe-1",
            "run_id": "run-1",
            "approval_type": approval_type,
            "execution_arn": "arn:aws:states:us-east-1:1:execution:x:y",
        }
        handler.create_approval_request(event)

    def test_code_generation_echo_targets_create_code_row(self):
        self._create("code_generation")

        state_updates = _state_update_calls()
        assert len(state_updates) == 1
        call = state_updates[0]
        assert call["Key"] == {"run_id": "run-1", "step_name": "create_code"}
        # The echo can never CREATE a row (no more phantom rows)
        assert call["ConditionExpression"] == "attribute_exists(run_id)"
        assert call["ExpressionAttributeValues"][":status"] == "waiting_approval"

    def test_no_state_row_keyed_by_approval_type(self):
        self._create("code_generation")

        for call in _state_update_calls():
            assert call["Key"]["step_name"] != "code_generation"
        mock_state_table.put_item.assert_not_called()

    def test_test_failure_writes_no_state_row_at_all(self):
        self._create("test_failure")

        assert _state_update_calls() == []
        mock_state_table.put_item.assert_not_called()
        # The approval record itself is still created
        mock_approvals_table.put_item.assert_called_once()


# ── process_approval_decision: decision echo ─────────────────────────


class TestDecisionEcho:
    def _decide(self, approval_type: str, decision: str = "approved") -> None:
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": approval_type,
                "status": "pending",
            }
        }
        handler.process_approval_decision("appr-1", decision, comment="lgtm", user="tester")

    def test_first_run_approval_echo_targets_create_quality_checks_row(self):
        self._decide("first_run")

        state_updates = _state_update_calls()
        assert len(state_updates) == 1
        call = state_updates[0]
        assert call["Key"] == {"run_id": "run-1", "step_name": "create_quality_checks"}
        assert call["ConditionExpression"] == "attribute_exists(run_id)"
        assert call["ExpressionAttributeValues"][":status"] == "success"

    def test_rejection_echo_writes_rejected_on_canonical_row(self):
        self._decide("code_generation", decision="rejected")

        state_updates = _state_update_calls()
        assert len(state_updates) == 1
        call = state_updates[0]
        assert call["Key"] == {"run_id": "run-1", "step_name": "create_code"}
        assert call["ExpressionAttributeValues"][":status"] == "rejected"

    def test_test_failure_decision_leaves_state_rows_untouched(self):
        """Approve-anyway must not flip the honest failed test_code status."""
        self._decide("test_failure")

        assert _state_update_calls() == []
        mock_state_table.put_item.assert_not_called()


# ── executions-row annotation: guarded, and never fails the decision ──
#
# p48 R9.2. The state echo above has carried ``attribute_exists(run_id)`` since
# P28 R8.3; its executions-table sibling three lines away did not, so an approval
# for a run with no execution row CREATED one — ``{pipeline_id, run_id,
# pending_*, status}`` with no ``started_at``, which ``RunsByTimeIndex`` cannot
# index. The row was therefore invisible in run history with no error.
#
# ``mock_exec_table`` was already bound by this module (see ``_table_map``) and
# asserted on by nothing. These tests use it.


@pytest.fixture(autouse=True)
def _clear_injected_side_effects():
    """``reset_mock()`` does not clear ``side_effect`` unless asked, and the
    module fixture above does not ask — so a failure injected by one test would
    leak into every test after it."""
    yield
    mock_exec_table.update_item.side_effect = None
    mock_state_table.update_item.side_effect = None


def _exec_update_calls():
    """All update_item calls made against the executions table mock."""
    return [c.kwargs for c in mock_exec_table.update_item.call_args_list]


def _conditional_check_failed():
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "UpdateItem",
    )


class TestExecutionsRowAnnotationIsGuarded:
    """R9.2: neither approval path may create a ``started_at``-less row."""

    def test_create_request_annotation_cannot_create_a_row(self):
        handler.create_approval_request(
            {
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "code_generation",
                "execution_arn": "arn:aws:states:us-east-1:1:execution:x:y",
            }
        )

        exec_updates = _exec_update_calls()
        assert len(exec_updates) == 1
        call = exec_updates[0]
        assert call["Key"] == {"pipeline_id": "pipe-1", "run_id": "run-1"}
        assert call["ConditionExpression"] == "attribute_exists(run_id)"
        # And it must not invent a start time onto the index's sort key (R1.4).
        assert "started_at" not in call["UpdateExpression"]

    def test_decision_annotation_cannot_create_a_row(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "first_run",
                "status": "pending",
            }
        }
        handler.process_approval_decision("appr-1", "approved", comment="lgtm", user="tester")

        exec_updates = _exec_update_calls()
        assert len(exec_updates) == 1
        assert exec_updates[0]["ConditionExpression"] == "attribute_exists(run_id)"
        assert exec_updates[0]["ExpressionAttributeValues"][":status"] == "RUNNING"
        assert "started_at" not in exec_updates[0]["UpdateExpression"]

    def test_rejection_annotation_is_guarded_too(self):
        """The reject branch is the one that produced the live `status=FAILED`
        phantoms (`pipe-001/run-001`, `pipe-c/run-c`), so it gets its own test
        rather than resting on the approve path's."""
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "code_generation",
                "status": "pending",
            }
        }
        handler.process_approval_decision("appr-1", "rejected", comment="no", user="tester")

        exec_updates = _exec_update_calls()
        assert len(exec_updates) == 1
        assert exec_updates[0]["ConditionExpression"] == "attribute_exists(run_id)"
        assert exec_updates[0]["ExpressionAttributeValues"][":status"] == "FAILED"


class TestRowAbsentNeverFailsTheDecision:
    """``LGR-eee8ac8-4``: bookkeeping ABOUT a run must never fail it.

    A ``ConditionalCheckFailedException`` means the row is absent, which is the
    guard doing its job — not an error. It must be swallowed, and specifically it
    must not turn an approval the approver already made into a failure.
    """

    def test_create_request_still_returns_the_approval_id(self):
        mock_exec_table.update_item.side_effect = _conditional_check_failed()

        approval_id = handler.create_approval_request(
            {
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "code_generation",
                "execution_arn": "arn:aws:states:us-east-1:1:execution:x:y",
            }
        )

        assert approval_id
        # The approval record itself is written regardless.
        mock_approvals_table.put_item.assert_called_once()

    def test_decision_still_returns_its_result(self):
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "first_run",
                "status": "pending",
            }
        }
        mock_exec_table.update_item.side_effect = _conditional_check_failed()

        result = handler.process_approval_decision("appr-1", "approved", comment="lgtm", user="tester")

        assert result["decision"] == "approved"
        # The state echo ran BEFORE the annotation and is unaffected by it.
        assert len(_state_update_calls()) == 1

    def test_an_ordinary_failure_is_also_swallowed_but_still_warned(self):
        """A non-conditional failure is a different branch and must not raise
        either — the distinction is the log level, not the control flow."""
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "first_run",
                "status": "pending",
            }
        }
        mock_exec_table.update_item.side_effect = RuntimeError("dynamo is unwell")

        result = handler.process_approval_decision("appr-1", "approved", comment="lgtm", user="tester")

        assert result["decision"] == "approved"

    def test_a_state_echo_failure_does_not_skip_the_annotation(self):
        """The two writes are in separate ``try`` blocks on purpose.

        They used to share one, so a state-echo failure skipped the executions
        annotation entirely and left the row claiming WAITING_APPROVAL after the
        decision had been made.
        """
        mock_approvals_table.get_item.return_value = {
            "Item": {
                "approval_id": "appr-1",
                "pipeline_id": "pipe-1",
                "run_id": "run-1",
                "approval_type": "first_run",
                "status": "pending",
            }
        }
        mock_state_table.update_item.side_effect = RuntimeError("state table is unwell")

        result = handler.process_approval_decision("appr-1", "approved", comment="lgtm", user="tester")

        assert result["decision"] == "approved"
        assert len(_exec_update_calls()) == 1, "the executions annotation must still run"
