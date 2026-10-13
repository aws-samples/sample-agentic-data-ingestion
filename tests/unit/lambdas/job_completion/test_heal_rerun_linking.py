# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion links heal rerun results to healing records (P28 R9.7).

The self-healing agent's outcome record says ``rerun_status="running"`` the
moment a heal rerun starts — this handler, processing that rerun's own Glue
completion, is the LINKER. It resolves the rerun's file_validation identity
row (``triggered_by="self_healing"`` + ``original_run_id``, persisted from
the rerun-contract SFN input) and updates the ORIGINAL run's ``self_healing``
record with the terminal outcome:

    rerun_result: SUCCEEDED|FAILED   linked_run_id: <rerun's fresh run_id>
    rerun_status: success|failed     rerun_completed_at: <iso timestamp>

Best-effort by design: the Step Functions callback is the run's source of
truth and has already been sent — linking failures (unreadable row, missing
healing record) log and never fail job_completion. The update is
conditional (attribute_exists) so a missing healing record cannot spawn a
phantom row.

Validates: P28 Requirements 9.7
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_job_completion_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
sys.path.insert(0, _job_completion_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _event(state="SUCCEEDED"):
    return {"detail": {"jobName": "agentic-data-ingestion-pipe-1", "jobRunId": "jr-1", "state": state}}


def _token_record(**overrides):
    record = {
        "run_id": "run-fresh-77",
        "step_name": "RunPipeline",
        "original_pipeline_id": "pipe-1",
        "glue_job_run_id": "jr-1",
        "status": "RUNNING",
    }
    record.update(overrides)
    return record


def _task_tokens_table():
    """The run's Glue-callback token row — run_pipeline stores it in the
    task-tokens table, never on the state row."""
    table = MagicMock(name="task_tokens_table")
    table.get_item.return_value = {"Item": {"approval_id": "glue_run#run-fresh-77", "task_token": "token-abc"}}
    return table


def _heal_fv_row(**overrides):
    """The heal rerun's file_validation identity row (fresh run_id, marker +
    link persisted by file_validation from the rerun-contract SFN input)."""
    row = {
        "run_id": "run-fresh-77",
        "step_name": "file_validation",
        "triggered_by": "self_healing",
        "original_run_id": "run-orig-1",
    }
    row.update(overrides)
    return row


def _glue_job_run(state="SUCCEEDED", error_message=""):
    return {
        "JobRun": {
            "JobRunState": state,
            "ExecutionTime": 120,
            "NumberOfWorkers": 2,
            "WorkerType": "G.1X",
            "MaxCapacity": 2.0,
            "GlueVersion": "5.0",
            "ErrorMessage": error_message,
        }
    }


def _mocks(fv_row, state="SUCCEEDED"):
    mock_runs_table = MagicMock()
    mock_runs_table.query.return_value = {"Items": [_token_record()]}
    mock_runs_table.get_item.return_value = {"Item": fv_row} if fv_row is not None else {}
    mock_glue = MagicMock()
    mock_glue.get_job_run.return_value = _glue_job_run(state)
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}
    mock_sfn = MagicMock()
    return mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn


def _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, event):
    with (
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "sfn_client", mock_sfn),
        patch.object(_mod, "lambda_client", MagicMock()),
    ):
        return _mod.lambda_handler(event, None)


def _healing_updates(mock_runs_table):
    """update_item calls targeting a self_healing record (the handler also
    updates the run's own RunPipeline row with metrics)."""
    return [
        c.kwargs
        for c in mock_runs_table.update_item.call_args_list
        if c.kwargs.get("Key", {}).get("step_name") == "self_healing"
    ]


# ── Linking on terminal states ───────────────────────────────────────


def test_succeeded_heal_rerun_links_result_to_original_healing_record():
    mocks = _mocks(_heal_fv_row(), state="SUCCEEDED")
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    updates = _healing_updates(mocks[0])
    assert len(updates) == 1
    update = updates[0]
    assert update["Key"] == {"run_id": "run-orig-1", "step_name": "self_healing"}
    values = update["ExpressionAttributeValues"]
    assert values[":res"] == "SUCCEEDED"
    assert values[":lrid"] == "run-fresh-77"
    assert values[":rst"] == "success"
    assert values[":ts"]


def test_failed_heal_rerun_links_failed_result():
    mocks = _mocks(_heal_fv_row(), state="FAILED")
    result = _run_handler(*mocks, _event("FAILED"))

    assert result["statusCode"] == 200
    updates = _healing_updates(mocks[0])
    assert len(updates) == 1
    values = updates[0]["ExpressionAttributeValues"]
    assert values[":res"] == "FAILED"
    assert values[":rst"] == "failed"


def test_link_is_update_only_no_phantom_healing_rows():
    """The update carries attribute_exists so a missing healing record is
    never CREATED by the linker (phantom-row protection)."""
    mocks = _mocks(_heal_fv_row())
    _run_handler(*mocks, _event("SUCCEEDED"))

    update = _healing_updates(mocks[0])[0]
    assert "attribute_exists" in update["ConditionExpression"]


# ── Non-heal runs never link ─────────────────────────────────────────


def test_normal_run_does_not_touch_healing_records():
    mocks = _mocks({"run_id": "run-fresh-77", "step_name": "file_validation", "triggered_by": "s3_upload"})
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert _healing_updates(mocks[0]) == []


def test_run_without_fv_row_does_not_link():
    mocks = _mocks(None)
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert _healing_updates(mocks[0]) == []


def test_heal_row_without_original_run_id_skips_link():
    """Pre-deploy heal reruns have the marker but no original_run_id — the
    linker logs and skips instead of guessing."""
    fv_row = _heal_fv_row()
    del fv_row["original_run_id"]
    mocks = _mocks(fv_row)

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert _healing_updates(mocks[0]) == []


# ── Best-effort: linking failures never fail job_completion ─────────


def test_fv_lookup_failure_does_not_fail_job_completion():
    mocks = _mocks(_heal_fv_row())
    mocks[0].get_item.side_effect = RuntimeError("dynamo down")

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def _raise_on_healing_update(error_code: str):
    """side_effect raising only for the self_healing link update — the
    handler's own RunPipeline metrics update must keep working."""

    def _side_effect(**kwargs):
        if kwargs.get("Key", {}).get("step_name") == "self_healing":
            raise ClientError({"Error": {"Code": error_code, "Message": error_code}}, "UpdateItem")
        return {}

    return _side_effect


def test_missing_healing_record_conditional_failure_is_swallowed():
    """The agent crashed before writing its record → conditional update
    fails → logged, job_completion still succeeds."""
    mocks = _mocks(_heal_fv_row())
    mocks[0].update_item.side_effect = _raise_on_healing_update("ConditionalCheckFailedException")

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_link_update_error_is_swallowed():
    mocks = _mocks(_heal_fv_row())
    mocks[0].update_item.side_effect = _raise_on_healing_update("ProvisionedThroughputExceededException")

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_callback_sent_before_link():
    """The SFN callback (the run's source of truth) precedes the best-effort
    link."""
    mocks = _mocks(_heal_fv_row())
    mock_sfn = mocks[3]
    call_order = []
    mock_sfn.send_task_success.side_effect = lambda **kw: call_order.append("send_task_success")
    mocks[0].update_item.side_effect = lambda **kw: call_order.append("update") or {}

    _run_handler(*mocks, _event("SUCCEEDED"))

    # First update is the RunPipeline metrics write (pre-callback); the
    # healing link comes after the callback.
    assert call_order == ["update", "send_task_success", "update"]
