# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion raises on missing record (P28 R5.2).

EventBridge Lambda targets retry on FUNCTION ERROR (an unhandled exception),
not on error-shaped success payloads. The old ``{"statusCode": 404}`` return
counted as a successful invocation, permanently dropping the completion event
and stranding the run for the full task timeout. The fixed behavior:

    missing token record → raise RecordNotFoundError
      → EventBridge redelivers with backoff
        → by the retry, run_pipeline's update_item (task 6.1) has landed and
          the record is findable via the GlueJobRunIndex GSI

Idempotency on redelivery: a duplicate event finds the record but the first
delivery already consumed the token — Step Functions rejects the send with
TaskTimedOut/TaskDoesNotExist/InvalidToken. That terminates cleanly (log +
return, no raise), so a permanently unprocessable duplicate never raise-loops.
All other failure modes keep their pre-existing semantics.

Validates: P28 Requirements 5.2
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
    """The run_pipeline state row the GlueJobRunIndex lookup finds. It carries
    no task token: run_pipeline stores that in the task-tokens table."""
    record = {
        "run_id": "run-1",
        "step_name": "RunPipeline",
        "original_pipeline_id": "pipe-1",
        "glue_job_run_id": "jr-1",
        "status": "RUNNING",
    }
    record.update(overrides)
    return record


def _task_tokens_table(task_token="token-abc"):
    """The task-tokens table holding run-1's Glue-callback token row (``None``: no row)."""
    table = MagicMock(name="task_tokens_table")
    item = {"approval_id": "glue_run#run-1", "task_token": task_token} if task_token else None
    table.get_item.return_value = {"Item": item} if item else {}
    return table


# Real-dict Glue response so collect_job_metrics computes real numbers
# (a bare MagicMock would leak Mock objects into arithmetic/serialization).
_GLUE_JOB_RUN = {
    "JobRun": {
        "JobRunState": "SUCCEEDED",
        "ExecutionTime": 120,
        "NumberOfWorkers": 2,
        "WorkerType": "G.1X",
        "MaxCapacity": 2.0,
        "GlueVersion": "5.0",
    }
}


def _standard_mocks(items):
    """Runs-table / glue / pipelines / sfn mocks for a found-record scenario."""
    mock_runs_table = MagicMock()
    mock_runs_table.query.return_value = {"Items": items}
    mock_glue = MagicMock()
    mock_glue.get_job_run.return_value = _GLUE_JOB_RUN
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}  # no estimates → empty comparison
    mock_sfn = MagicMock()
    return mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn


def _consumed_error(code="TaskTimedOut", operation="SendTaskSuccess"):
    return ClientError({"Error": {"Code": code, "Message": "Task token no longer valid"}}, operation)


def _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, event, tokens_table=None):
    with (
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", tokens_table if tokens_table is not None else _task_tokens_table()),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "sfn_client", mock_sfn),
    ):
        return _mod.lambda_handler(event, None)


def test_missing_record_raises_record_not_found_error():
    """No token record for the event → raise (EventBridge retry), not 404-as-success.

    The exception message names jobName and jobRunId so the retry/DLQ trail
    identifies the unmatched job.
    """
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[])

    mock_tokens_table = _task_tokens_table()

    with (
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "sfn_client", mock_sfn),
        pytest.raises(_mod.RecordNotFoundError) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    message = str(exc_info.value)
    assert "agentic-data-ingestion-pipe-1" in message  # jobName
    assert "jr-1" in message  # jobRunId

    # The retry path leaves no partial side effects behind — and never
    # touches the token row, which the redelivery still needs.
    mock_runs_table.update_item.assert_not_called()
    mock_sfn.send_task_success.assert_not_called()
    mock_sfn.send_task_failure.assert_not_called()
    mock_tokens_table.get_item.assert_not_called()
    mock_tokens_table.delete_item.assert_not_called()


def test_race_window_record_without_job_run_id_raises(mock_dynamodb_tables):
    """The exact 6.1 race window: token record persisted (pre-start put_item)
    but glue_job_run_id not yet attached → absent from the sparse
    GlueJobRunIndex GSI → the completion event cannot match → raise so
    EventBridge redelivers after the update_item lands."""
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    pre_start_record = _token_record()
    del pre_start_record["glue_job_run_id"]  # attribute absent until update_item — sparse GSI
    state_table.put_item(Item=pre_start_record)
    # The record is real but not yet indexed.
    assert state_table.get_item(Key={"run_id": "run-1", "step_name": "RunPipeline"}).get("Item")

    mock_glue = MagicMock()
    mock_glue.get_job_run.return_value = _GLUE_JOB_RUN

    with (
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.RecordNotFoundError),
    ):
        _mod.lambda_handler(_event(), None)


def test_record_exists_success_normal_processing():
    """Record found + SUCCEEDED → metrics update + send_task_success, 200."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])

    result = _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    mock_sfn.send_task_success.assert_called_once()
    assert mock_sfn.send_task_success.call_args.kwargs["taskToken"] == "token-abc"
    update_kwargs = mock_runs_table.update_item.call_args.kwargs
    assert update_kwargs["Key"] == {"run_id": "run-1", "step_name": "RunPipeline"}
    assert update_kwargs["ExpressionAttributeValues"][":status"] == "success"


def test_record_exists_failure_normal_processing():
    """Record found + FAILED → send_task_failure with GlueJobFailed, 200."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])

    result = _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("FAILED"))

    assert result["statusCode"] == 200
    mock_sfn.send_task_failure.assert_called_once()
    assert mock_sfn.send_task_failure.call_args.kwargs["error"] == "GlueJobFailed"
    update_kwargs = mock_runs_table.update_item.call_args.kwargs
    assert update_kwargs["ExpressionAttributeValues"][":status"] == "failed"


@pytest.mark.parametrize("error_code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_duplicate_delivery_consumed_token_returns_cleanly(error_code):
    """Duplicate delivery: record found, token already consumed by the first
    delivery → clean return (no raise) so EventBridge does not redeliver a
    permanently unprocessable event."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])
    mock_sfn.send_task_success.side_effect = _consumed_error(error_code)

    result = _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert "already consumed" in result["message"]
    assert result["job_run_id"] == "jr-1"


def test_duplicate_failure_event_skips_self_healing(monkeypatch):
    """Duplicate FAILED delivery with consumed token → clean return BEFORE the
    self-healing dispatch (the first delivery already triggered it).

    P28 R9.3: dispatch is now an async Lambda invoke of SELF_HEALING_FN
    (InvocationType=Event), not a daemon thread — the duplicate-delivery
    guard must skip that invoke."""
    monkeypatch.setenv("SELF_HEALING_FN", "agentic_data_ingestion_self_healing_dispatch")
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])
    mock_sfn.send_task_failure.side_effect = _consumed_error("TaskTimedOut", "SendTaskFailure")
    mock_lambda = MagicMock()

    with patch.object(_mod, "lambda_client", mock_lambda):
        result = _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("FAILED"))

    assert result["statusCode"] == 200
    assert "already consumed" in result["message"]
    mock_lambda.invoke.assert_not_called()


def test_other_send_errors_raise_so_the_invocation_is_retried():
    """A send that failed for any reason other than a consumed token did not
    deliver the callback. It must be a function error: EventBridge invokes this
    handler asynchronously, so the 500-shaped dict it used to return was a
    successful invocation, nothing retried it, and the run waited for its task
    timeout."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])
    mock_sfn.send_task_success.side_effect = _consumed_error("ThrottlingException")

    with pytest.raises(ClientError, match="ThrottlingException"):
        _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("SUCCEEDED"))


@pytest.mark.parametrize(
    "failing_step",
    [
        pytest.param("query", id="run-lookup-throttled"),
        pytest.param("update_item", id="state-update-failed"),
    ],
)
def test_a_dynamodb_failure_before_the_callback_raises(failing_step):
    """The external review's probe: a throttled GlueJobRunIndex query made the
    handler return `{"statusCode": 500}` without raising, so the completion was
    acknowledged and never retried. Any table failure before the callback must
    fail the invocation, and nothing may be sent to Step Functions."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])
    throttled = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Rate exceeded"}}, failing_step
    )
    getattr(mock_runs_table, failing_step).side_effect = throttled

    with pytest.raises(ClientError, match="ProvisionedThroughputExceededException"):
        _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, _event("SUCCEEDED"))

    mock_sfn.send_task_success.assert_not_called()
    mock_sfn.send_task_failure.assert_not_called()


def test_record_without_token_raises_so_the_event_reaches_the_dlq():
    """A GSI-findable, still-RUNNING record with no token anywhere — no token
    row and none on the state row — is a function error. It used to return a
    400-shaped dict, which an asynchronous invocation treats as success, so the
    event was acknowledged, never retried and never dead-lettered while the
    execution waited for its task timeout."""
    mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn = _standard_mocks(items=[_token_record()])

    with pytest.raises(_mod.TaskTokenMissingError, match="No task token for active run"):
        _run_handler(
            mock_runs_table,
            mock_glue,
            mock_pipelines_table,
            mock_sfn,
            _event("SUCCEEDED"),
            tokens_table=_task_tokens_table(task_token=None),
        )

    mock_sfn.send_task_success.assert_not_called()
    mock_runs_table.update_item.assert_not_called()
