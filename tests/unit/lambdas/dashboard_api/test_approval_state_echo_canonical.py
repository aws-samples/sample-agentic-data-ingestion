# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: dashboard approve path writes NO phantom state rows (P28 R8.3).

The dashboard approvals handler used to flip a state row keyed
``step_name = approval_type`` after a successful approval — a phantom row for
every type except schema_mapping. The echo now targets the CANONICAL step
row via ``from_any``, only updates an existing row (``attribute_exists``),
and is skipped for ``test_failure`` (the test_code row's honest failed
status survives an approve-anyway decision).

The reject path has the same echo, writing ``rejected``, and a schema-mapping
rejection also withdraws the refused mapping from reuse by later runs
(``shared/mapping_reuse.py``).

Validates: P28 Requirements 8.3, 8.5
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirrors test_approval_send_ordering.py)
# ---------------------------------------------------------------------------
DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
HANDLERS_DIR = os.path.join(DASHBOARD_API_DIR, "handlers")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(DASHBOARD_API_DIR))
sys.path.insert(0, os.path.abspath(HANDLERS_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")

mock_approvals_table = MagicMock()
mock_task_tokens_table = MagicMock()

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

APPROVAL_ID = "approval-echo-001"

mock_state_table = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    # The reject-echo tests set return values and side effects on this mock;
    # a bare reset_mock() keeps both, so they would leak into later tests.
    mock_state_table.reset_mock(return_value=True, side_effect=True)
    approvals_mod.approvals_table = mock_approvals_table
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.state_table = mock_state_table
    yield


def _approval_record(approval_type: str) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": False,  # no SFN interaction needed for the echo test
        "pipeline_id": "pipe-001",
        "run_id": "run-001",
        "approval_type": approval_type,
        "created_at": "2025-01-15T12:00:00Z",
    }


def _approve(approval_type: str) -> dict:
    mock_approvals_table.get_item.return_value = {"Item": _approval_record(approval_type)}
    return approvals_mod.approve_request(APPROVAL_ID, json.dumps({"approved_by": "tester"}))


def _state_update_calls():
    return [c.kwargs for c in mock_state_table.update_item.call_args_list]


class TestStateEchoStepNamePolicy:
    def test_maps_approval_types_to_canonical_steps(self):
        assert approvals_mod._state_echo_step_name("schema_mapping") == "schema_mapping"
        assert approvals_mod._state_echo_step_name("code_generation") == "create_code"
        assert approvals_mod._state_echo_step_name("first_run") == "create_quality_checks"

    def test_test_failure_and_unknown_get_no_echo(self):
        assert approvals_mod._state_echo_step_name("test_failure") is None
        assert approvals_mod._state_echo_step_name("not_a_step") is None
        assert approvals_mod._state_echo_step_name("") is None


class TestApproveRequestEcho:
    def test_first_run_approval_targets_create_quality_checks_row(self):
        result = _approve("first_run")
        assert result["statusCode"] == 200

        # No phantom row: exactly one state update, keyed by the canonical
        # step, guarded so it can never create a row.
        echo_calls = [c for c in _state_update_calls() if c["ExpressionAttributeValues"].get(":status") == "success"]
        assert len(echo_calls) == 1
        call = echo_calls[0]
        assert call["Key"] == {"run_id": "run-001", "step_name": "create_quality_checks"}
        assert call["ConditionExpression"] == "attribute_exists(run_id)"
        for c in _state_update_calls():
            assert c["Key"]["step_name"] != "first_run"

    def test_test_failure_approval_leaves_state_rows_untouched(self):
        """Approve-anyway keeps the honest failed test_code status (R4.3)."""
        result = _approve("test_failure")
        assert result["statusCode"] == 200

        assert _state_update_calls() == []
        mock_state_table.put_item.assert_not_called()
        # The approval record itself is still marked approved
        assert mock_approvals_table.update_item.called


# ── executions-row annotation: guarded, and never fails the request ────
#
# p48 R9.2. The state echo above has carried ``attribute_exists(run_id)`` since
# P28 R8.3. Its executions-table sibling three lines away did not, so a decision
# on a run with no execution row CREATED one — ``{pipeline_id, run_id, status,
# updated_at}`` and nothing else — which ``RunsByTimeIndex`` cannot index because
# it has no ``started_at``. Two such rows exist live (`pipe-001/run-001`,
# `pipe-c/run-c`), both with `status=FAILED`, both with zero state rows, which is
# the signature of a row CREATED by an update rather than updated.
#
# Both branches now go through ``_annotate_execution_row``, so there is one write
# path and one guard rather than two copies.

mock_executions_table = MagicMock()


@pytest.fixture(autouse=True)
def _bind_executions_table():
    """``_annotate_execution_row`` imports ``executions_table`` from
    ``handlers.common`` at call time, so the module attribute is what to patch."""
    from handlers import common as common_mod

    original = common_mod.executions_table
    common_mod.executions_table = mock_executions_table
    mock_executions_table.reset_mock()
    mock_executions_table.update_item.side_effect = None
    yield
    common_mod.executions_table = original


def _reject(approval_type: str) -> dict:
    mock_approvals_table.get_item.return_value = {"Item": _approval_record(approval_type)}
    return approvals_mod.reject_request(APPROVAL_ID, json.dumps({"rejected_by": "tester", "reason": "no"}))


def _exec_update_calls():
    return [c.kwargs for c in mock_executions_table.update_item.call_args_list]


def _conditional_check_failed():
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "The conditional request failed"}},
        "UpdateItem",
    )


class TestExecutionsRowAnnotationIsGuarded:
    def test_approve_annotation_cannot_create_a_row(self):
        assert _approve("first_run")["statusCode"] == 200

        calls = _exec_update_calls()
        assert len(calls) == 1
        assert calls[0]["Key"] == {"pipeline_id": "pipe-001", "run_id": "run-001"}
        assert calls[0]["ConditionExpression"] == "attribute_exists(run_id)"
        assert calls[0]["ExpressionAttributeValues"][":status"] == "RUNNING"
        # R1.4: no fabricated timestamp on the index's sort key.
        assert "started_at" not in calls[0]["UpdateExpression"]

    def test_reject_annotation_cannot_create_a_row(self):
        assert _reject("first_run")["statusCode"] == 200

        calls = _exec_update_calls()
        assert len(calls) == 1
        assert calls[0]["ConditionExpression"] == "attribute_exists(run_id)"
        assert calls[0]["ExpressionAttributeValues"][":status"] == "FAILED"
        assert "started_at" not in calls[0]["UpdateExpression"]

    def test_the_annotation_writes_no_state_row(self):
        """The executions annotation stays separate from the state-row echo.

        Each decision branch echoes onto the state row in its own step, so a
        failure of one write cannot skip the other.
        """
        approvals_mod._annotate_execution_row("pipe-001", "run-001", "FAILED", context="rejection")

        assert _state_update_calls() == []
        mock_state_table.put_item.assert_not_called()


class TestRejectRequestEcho:
    """The rejection arm of the approve echo.

    Observed on 2026-10-09: a mapping rejected through the dashboard kept the
    ``success`` its row last held, so the next run reused it. The reject path
    had no state-row echo; ``LGR-5d68eeb-2`` recorded that as a finding.
    """

    def test_a_schema_mapping_rejection_rests_the_row_at_rejected(self):
        assert _reject("schema_mapping")["statusCode"] == 200

        echo_calls = [c for c in _state_update_calls() if c["ExpressionAttributeValues"].get(":status") == "rejected"]
        assert len(echo_calls) == 1
        call = echo_calls[0]
        assert call["Key"] == {"run_id": "run-001", "step_name": "schema_mapping"}
        assert call["ConditionExpression"] == "attribute_exists(run_id)"
        assert ":now" in call["ExpressionAttributeValues"]

    def test_the_echo_targets_the_same_canonical_row_as_the_approve_echo(self):
        _reject("first_run")

        keys = [c["Key"] for c in _state_update_calls()]
        assert keys == [{"run_id": "run-001", "step_name": "create_quality_checks"}]

    def test_a_test_failure_rejection_leaves_state_rows_untouched(self):
        """Same policy as approve: the test_code row's honest status survives."""
        assert _reject("test_failure")["statusCode"] == 200

        assert _state_update_calls() == []
        mock_state_table.put_item.assert_not_called()

    def test_an_absent_row_never_fails_the_request(self):
        """A reuse run has no mapping row of its own; the guard working is not a 500."""
        mock_state_table.update_item.side_effect = _conditional_check_failed()

        result = _reject("schema_mapping")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "rejected"

    def test_an_ordinary_echo_failure_never_fails_the_request(self):
        mock_state_table.update_item.side_effect = RuntimeError("dynamo is unwell")

        assert _reject("schema_mapping")["statusCode"] == 200


class TestRejectRevokesTheMappingFromReuse:
    """Every mapping a rejection refused is withdrawn from reuse by later runs."""

    @pytest.fixture(autouse=True)
    def _revoke(self):
        with patch.object(approvals_mod, "revoke_mapping_reuse", return_value=1) as revoke:
            yield revoke

    def _reject_with(self, approval_type: str = "schema_mapping", **record_extra) -> dict:
        record = {**_approval_record(approval_type), **record_extra}
        mock_approvals_table.get_item.return_value = {"Item": record}
        return approvals_mod.reject_request(APPROVAL_ID, json.dumps({"reason": "no"}))

    def test_the_run_own_mapping_is_revoked(self, _revoke):
        mock_state_table.update_item.return_value = {"Attributes": {"mapping_id": "mapping-hash-own"}}

        self._reject_with()

        _revoke.assert_called_once_with(mock_state_table, "pipe-001", "mapping-hash-own", approval_id=APPROVAL_ID)

    def test_a_reuse_run_revokes_the_mapping_it_resolved_to(self, _revoke):
        """The observed case: the reuse run had no mapping row at all."""
        mock_state_table.update_item.side_effect = _conditional_check_failed()
        approval_data = json.dumps({"reused_mapping": True, "reused_mapping_id": "mapping-hash-3688"})

        self._reject_with(approval_data=approval_data)

        _revoke.assert_called_once_with(mock_state_table, "pipe-001", "mapping-hash-3688", approval_id=APPROVAL_ID)

    def test_truncated_approval_data_still_names_the_reused_mapping(self, _revoke):
        """approval_handler truncates approval_data past 50,000 chars, breaking the JSON."""
        mock_state_table.update_item.side_effect = _conditional_check_failed()
        truncated = (
            '{"proposed_mappings": [], "reused_mapping_id": "mapping-hash-cut", "source_schema": {"pro...(truncated)'
        )

        self._reject_with(approval_data=truncated)

        _revoke.assert_called_once_with(mock_state_table, "pipe-001", "mapping-hash-cut", approval_id=APPROVAL_ID)

    def test_both_sources_are_revoked_once_each(self, _revoke):
        mock_state_table.update_item.return_value = {"Attributes": {"mapping_id": "mapping-hash-own"}}
        approval_data = json.dumps({"reused_mapping_id": "mapping-hash-own"})

        self._reject_with(approval_data=approval_data)

        assert _revoke.call_count == 1

    def test_other_approval_types_revoke_nothing(self, _revoke):
        mock_state_table.update_item.return_value = {"Attributes": {"mapping_id": "mapping-hash-own"}}

        self._reject_with("code_generation")

        _revoke.assert_not_called()

    def test_a_revocation_failure_never_fails_the_request(self, _revoke):
        _revoke.side_effect = RuntimeError("throttled")
        mock_state_table.update_item.return_value = {"Attributes": {"mapping_id": "mapping-hash-own"}}

        assert self._reject_with()["statusCode"] == 200


class TestRowAbsentNeverFailsTheRequest:
    """``LGR-eee8ac8-4``: a ``ConditionalCheckFailedException`` means the row is
    absent — the guard working — and must not become a 500. The decision has
    already landed on the approvals row and the task token has already been sent,
    so failing here would report a failure for work that succeeded."""

    def test_approve_still_returns_200(self):
        mock_executions_table.update_item.side_effect = _conditional_check_failed()

        result = _approve("first_run")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "approved"

    def test_reject_still_returns_200(self):
        mock_executions_table.update_item.side_effect = _conditional_check_failed()

        result = _reject("first_run")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "rejected"

    def test_an_ordinary_failure_is_also_swallowed(self):
        """A non-conditional failure takes the other branch and must not raise
        either — the branches differ in log level, not in control flow."""
        mock_executions_table.update_item.side_effect = RuntimeError("dynamo is unwell")

        assert _approve("first_run")["statusCode"] == 200
        assert _reject("first_run")["statusCode"] == 200
