# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.2 — an approval or rejection of a manifest-bearing approval is
RECORDED BY THE EXISTING MECHANISM (R10.6, R10.7). Nothing new is built.

The task's own wording is the constraint: *"Approve and reject are recorded, not
invented … Both ride the existing approval mechanism."* So this module's job is to
**hold the existing mechanism to R10.6 and R10.7 for a manifest-bearing
approval**, not to add a recording path. Every assertion below is about code that
was already there before p55; what is new is that a Rename_Manifest is now on the
payload those paths decide about, and R10.6/R10.7 are stated in terms of it.

Where the approved manifest actually lives, and why that answers R10.7
---------------------------------------------------------------------
``create_approval_payload`` puts the manifest on the payload
``store_mapping_result`` stores whole at ``output_data.approval`` on the run's own
``(run_id, "schema_mapping")`` state row. So the manifest is recorded **against
the run** at the moment it is proposed. What an approval adds is the decision:
``approve_request`` flips that row's ``status`` to ``success`` and marks the
approvals row approved, and it **writes nothing to ``output_data``**. That is the
whole of R10.7 — the approved manifest is the manifest on the approved run's row —
and it holds only while the echo does not rewrite ``output_data``, which is what
``test_the_approval_does_not_rewrite_the_recorded_manifest`` pins.

*Rejected: copying the manifest onto the approvals row on approval.* It would be a
second authority for a value the run's row already holds, and task 6.3's reuse
lookup selects **mapping records** — so a copy could disagree with the row the
lookup reads. ``LGR-28b4d75-1`` and ``LGR-9e76249-2`` (both **template /
ratified**) reject a second derivation of one shape in a second place, and design
risk R5.7 is the same point for this value specifically: the stored manifest is
the authority at code-generation time and is used verbatim or not at all.

Why the rejection arm is asserted separately, in full
----------------------------------------------------
R10.6 has two halves — *terminate the run* and *record the rejection* — and the
rejection arm of this handler has a history of one-sided landings.
``LGR-c284fbb-7`` is cited in the handler's own source for exactly that failure
mode: a fix applied to the approve path only, "inert on this arm while every test
on the other one passed". So both halves are asserted here rather than inferred
from the approve path's behaviour.

**Not asserted here:** the ``rejected`` status the approvals row rests at is the
dashboard path's. ``approval_handler`` has its own reject path and
``LGR-5d68eeb-2`` records that a **dashboard**-rejected run's state row rests at
``waiting_approval`` indefinitely because this branch performs no state echo. That
is a recorded finding with its own decision pending, not this task's to change, and
task 6.3's reuse conjunct is written as *is approved* rather than *is not rejected*
precisely because of it.

**Validates: Requirements 10.6, 10.7**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_approval_state_echo_canonical.py in this directory.
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

APPROVAL_ID = "approval-p55-manifest-001"
RUN_ID = "run-p55-manifest"
PIPELINE_ID = "pipe-p55-manifest"
MAPPING_ID = "mapping-hash-p55manifest"
TASK_TOKEN = "task-token-p55-manifest"

mock_state_table = MagicMock()
mock_executions_table = MagicMock()

#: The manifest as `create_approval_payload` writes it — two proposed renames, so
#: an assertion that the manifest survived cannot pass on an empty list.
RENAME_MANIFEST: dict = {
    "manifest_id": MAPPING_ID,
    "source_schema_hash": "sha256:p55manifest",
    "approval_id": None,
    "renames": [
        {"original_name": "first name", "target_name": "first_name"},
        {"original_name": "2024-total", "target_name": "_2024_total"},
    ],
}


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    mock_state_table.reset_mock()
    approvals_mod.approvals_table = mock_approvals_table
    approvals_mod.task_tokens_table = mock_task_tokens_table
    approvals_mod.state_table = mock_state_table
    mock_state_table.get_item.return_value = {"Item": _state_row()}
    yield


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


@pytest.fixture(autouse=True)
def _no_memory_round_trips():
    """The schema_mapping arms both call into AgentCore Memory.

    Patched out rather than left to the handler's own ``except`` so a failure in
    this test reads as a failure of the decision recording rather than of a
    Memory call this task does not touch.
    """
    with patch.object(approvals_mod, "MappingMemoryClient", MagicMock()):
        yield


def _state_row() -> dict:
    """The run's ``(run_id, "schema_mapping")`` row, manifest included."""
    return {
        "run_id": RUN_ID,
        "step_name": "schema_mapping",
        "status": "waiting_approval",
        "mapping_id": MAPPING_ID,
        "output_data": {
            "artifact_type": "schema_mapping",
            "primary_id": MAPPING_ID,
            "approval": {
                "approval_type": "schema_mapping",
                "source_schema_hash": "sha256:p55manifest",
                "rename_manifest": RENAME_MANIFEST,
            },
        },
    }


def _approval_record(*, has_task_token: bool = True) -> dict:
    return {
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "has_task_token": has_task_token,
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "approval_type": "schema_mapping",
        "mapping_id": MAPPING_ID,
        "created_at": "2026-08-30T12:00:00Z",
        "approval_data": json.dumps({"rename_manifest": RENAME_MANIFEST}),
    }


def _sfn_and_audit(has_task_token: bool = True):
    """Patch the two side effects both arms have: Step Functions, and the audit write."""
    mock_approvals_table.get_item.return_value = {"Item": _approval_record(has_task_token=has_task_token)}
    mock_task_tokens_table.get_item.return_value = {"Item": {"approval_id": APPROVAL_ID, "task_token": TASK_TOKEN}}
    sfn = MagicMock()
    boto3_stub = MagicMock()
    boto3_stub.client.return_value = sfn
    audit = MagicMock()
    return (
        sfn,
        patch.object(approvals_mod, "boto3", boto3_stub),
        patch.object(approvals_mod, "write_audit_entry", audit),
        audit,
    )


def _state_update_calls() -> list[dict]:
    return [c.kwargs for c in mock_state_table.update_item.call_args_list]


def _approvals_update_values() -> dict:
    assert mock_approvals_table.update_item.called, "the decision was not recorded on the approvals row"
    return mock_approvals_table.update_item.call_args.kwargs["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# R10.7 — approving records the approved manifest against the run
# ---------------------------------------------------------------------------


def test_the_approval_does_not_rewrite_the_recorded_manifest():
    """R10.7, and this is the assertion the requirement actually reduces to.

    The manifest is already recorded against the run — ``store_mapping_result``
    put it at ``output_data.approval.rename_manifest`` on the
    ``(run_id, "schema_mapping")`` row when it was proposed. An approval records
    the *decision* on top of that, and R10.7 holds exactly as long as the
    decision does not disturb the record. So what is checked is that no write on
    this path touches ``output_data``: the echo sets ``status`` and ``updated_at``
    and nothing else.

    Reading it this way rather than adding a write is the point. A second copy of
    the manifest, written at decision time, would be a second authority for a
    value task 6.3's lookup reads from the record — ``LGR-9e76249-2`` and
    ``LGR-28b4d75-1`` both reject that, and risk R5.7 says why it matters here
    specifically.
    """
    _sfn, boto3_patch, audit_patch, _ = _sfn_and_audit()
    with boto3_patch, audit_patch:
        result = approvals_mod.approve_request(APPROVAL_ID, json.dumps({"approved_by": "approver@example.com"}))

    assert result["statusCode"] == 200
    assert json.loads(result["body"])["status"] == "approved"

    echoes = [c for c in _state_update_calls() if c["ExpressionAttributeValues"].get(":status") == "success"]
    assert len(echoes) == 1, f"expected exactly one state echo, got {_state_update_calls()}"
    assert echoes[0]["Key"] == {"run_id": RUN_ID, "step_name": "schema_mapping"}

    for call in _state_update_calls():
        assert "output_data" not in call["UpdateExpression"], (
            "the approval must not rewrite output_data — the manifest recorded there when it was "
            f"proposed IS the approved manifest (R10.7). Got: {call['UpdateExpression']}"
        )
    mock_state_table.put_item.assert_not_called()


def test_the_approval_is_recorded_on_the_approvals_row_and_in_the_audit_log():
    """The decision half of R10.7 — recorded, by the mechanism that already records it.

    Two rows, deliberately: the approvals row carries the status and the actor,
    and the audit log carries the same decision under ``approval_approved``.
    ``LGR-c58f276-1`` (**template / ratified**) measured 107 ``approval_approved``
    audit rows with ``actor == "unknown"``, which is why the audit write is
    asserted to have happened at all rather than assumed from the 200.
    """
    sfn, boto3_patch, audit_patch, audit = _sfn_and_audit()
    with boto3_patch, audit_patch:
        assert approvals_mod.approve_request(APPROVAL_ID, "{}")["statusCode"] == 200

    values = _approvals_update_values()
    assert values[":status"] == "approved"
    assert ":approved_at" in values

    assert audit.called
    assert audit.call_args.kwargs["action"] == "approval_approved"
    assert audit.call_args.kwargs["resource_id"] == APPROVAL_ID

    # And the run resumes rather than terminating — the contrast that makes the
    # rejection assertions below meaningful.
    sfn.send_task_success.assert_called_once()
    sfn.send_task_failure.assert_not_called()


# ---------------------------------------------------------------------------
# R10.6 — rejecting terminates the run and records the rejection
# ---------------------------------------------------------------------------


def test_the_rejection_terminates_the_run():
    """R10.6's first half. ``send_task_failure``, so the execution ends rather than retries.

    Asserted on the arm with a task token, because that is the arm that can
    terminate anything: a rejection of a token-less approval has no execution to
    fail. The error string is asserted too — the Step Functions ``Cause`` is what
    an operator reads on a terminated execution, and ``ApprovalRejected`` is what
    says the termination was a decision rather than a fault.
    """
    sfn, boto3_patch, audit_patch, _ = _sfn_and_audit()
    with boto3_patch, audit_patch:
        result = approvals_mod.reject_request(
            APPROVAL_ID, json.dumps({"rejected_by": "approver@example.com", "reason": "renames wrong"})
        )

    assert result["statusCode"] == 200
    sfn.send_task_failure.assert_called_once()
    kwargs = sfn.send_task_failure.call_args.kwargs
    assert kwargs["taskToken"] == TASK_TOKEN
    assert kwargs["error"] == "ApprovalRejected"
    assert "renames wrong" in kwargs["cause"]
    sfn.send_task_success.assert_not_called()

    # The run is marked FAILED on the executions row, so the dashboard stops
    # reporting it as awaiting a decision that has already been made.
    exec_calls = [c.kwargs for c in mock_executions_table.update_item.call_args_list]
    assert len(exec_calls) == 1
    assert exec_calls[0]["ExpressionAttributeValues"][":status"] == "FAILED"


def test_the_rejection_is_recorded_with_its_reason_and_in_the_audit_log():
    """R10.6's second half, and the reason is part of the record.

    An approver who rejects a proposed renaming is saying something about the
    renaming, and the manifest is still on the run's row afterwards — so the
    reason is the only place their objection is written down. It is persisted
    verbatim, not summarised.
    """
    _sfn, boto3_patch, audit_patch, audit = _sfn_and_audit()
    with boto3_patch, audit_patch:
        assert (
            approvals_mod.reject_request(
                APPROVAL_ID, json.dumps({"rejected_by": "approver@example.com", "reason": "2024-total is not that"})
            )["statusCode"]
            == 200
        )

    values = _approvals_update_values()
    assert values[":status"] == "rejected"
    assert values[":reason"] == "2024-total is not that"
    assert ":rejected_at" in values

    assert audit.called
    assert audit.call_args.kwargs["action"] == "approval_rejected"
    assert audit.call_args.kwargs["resource_id"] == APPROVAL_ID


def test_the_rejection_leaves_the_recorded_manifest_where_it_was():
    """The rejected manifest is not deleted, and that is deliberate rather than lazy.

    R10.6 says terminate and record the rejection; it does not say erase the
    proposal. Keeping it is what lets the rejection reason above be read against
    the thing it objected to. Task 6.3's reuse conjunct is *is approved*, so a
    manifest on a rejected run is not reusable — the record staying put costs
    nothing and removing it would destroy the evidence.
    """
    _sfn, boto3_patch, audit_patch, _ = _sfn_and_audit()
    with boto3_patch, audit_patch:
        assert approvals_mod.reject_request(APPROVAL_ID, json.dumps({"reason": "no"}))["statusCode"] == 200

    for call in _state_update_calls():
        assert "output_data" not in call["UpdateExpression"], (
            f"the rejection must not rewrite output_data: {call['UpdateExpression']}"
        )
    mock_state_table.put_item.assert_not_called()
    mock_state_table.delete_item.assert_not_called()
