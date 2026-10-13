# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: run_pipeline persists the task token BEFORE start_job_run (P28 R5.1).

Fast-fail race: job_completion matches completion events to runs via the
GlueJobRunIndex GSI, then reads the run's token row. The old order
(start_job_run → put_item) let a fast-failing Glue job emit its completion
event before the token existed, stranding the run. The fixed order is:

    task-tokens put_item (approval_id = "glue_run#<run_id>")
      → state put_item (RUNNING row, no task_token, no glue_job_run_id)
        → start_job_run
          → state update_item (attach glue_job_run_id → GSI-findable)

The token is stored in the task-tokens table and NEVER on the state row:
AgentCore agents read the state table and the dashboard returns state rows to
the browser, while the AgentCore runtime role is denied the task-tokens table
(LGR-8f59603-3 named this token as that Deny's known residual).

On start failure both pre-start rows are cleaned up (delete_item) and the
step raises per R1 (TransientStepFailure for retryable Glue codes). Both rows
are written unconditionally, so a retried task overwrites its own rows — no
duplicate-key failure.

Validates: P28 Requirements 5.1
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_run_pipeline_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_run_pipeline")
)
sys.path.insert(0, _run_pipeline_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def _seed_pipeline(mock_pipelines_table):
    mock_pipelines_table.get_item.return_value = {
        "Item": {
            "pipeline_id": "pipe-1",
            "glue_job_metadata": {"glue_job_name": "agentic-data-ingestion-pipe-1"},
        }
    }


def _event():
    return {"pipeline_id": "pipe-1", "run_id": "run-1", "taskToken": "token-abc"}


TOKEN_KEY = {"approval_id": "glue_run#run-1"}


def _tracking_mocks(call_order, start_side_effect=None):
    """Build runs-table, task-tokens-table and glue mocks that record their call sequence."""
    mock_runs_table = MagicMock()
    mock_runs_table.put_item.side_effect = lambda **kw: call_order.append("put_item")
    mock_runs_table.update_item.side_effect = lambda **kw: call_order.append("update_item")
    mock_runs_table.delete_item.side_effect = lambda **kw: call_order.append("delete_item")

    mock_tokens_table = MagicMock()
    mock_tokens_table.put_item.side_effect = lambda **kw: call_order.append("token_put")
    mock_tokens_table.update_item.side_effect = lambda **kw: call_order.append("token_update")
    mock_tokens_table.delete_item.side_effect = lambda **kw: call_order.append("token_delete")

    mock_glue = MagicMock()

    def _start(**kw):
        call_order.append("start_job_run")
        if start_side_effect is not None:
            raise start_side_effect
        return {"JobRunId": "jr-123"}

    mock_glue.start_job_run.side_effect = _start
    return mock_runs_table, mock_tokens_table, mock_glue


def test_token_record_persisted_before_start_job_run():
    """Ordering: token put → state put → start_job_run → update_item (job run id)."""
    call_order = []
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order)
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
    ):
        _mod.lambda_handler(_event(), None)

    # The token is stored first, so whenever the state row is findable its
    # token already exists.
    # token_update is the post-start cancel-fence check that also stamps the
    # job's id onto the token row (assessment F1); it precedes the state-row
    # attach so a fenced run stops its job before anything makes it findable.
    assert call_order == ["token_put", "put_item", "start_job_run", "token_update", "update_item"]
    # Both token-row writes refuse a cancel fence.
    assert (
        "attribute_not_exists(cancel_requested_at)"
        in mock_tokens_table.put_item.call_args.kwargs["ConditionExpression"]
    )
    assert (
        "attribute_not_exists(cancel_requested_at)"
        in mock_tokens_table.update_item.call_args.kwargs["ConditionExpression"]
    )

    # Pre-start state row carries no glue_job_run_id (it doesn't exist yet —
    # it stays out of the sparse GSI until update) and never the task token.
    item = mock_runs_table.put_item.call_args.kwargs["Item"]
    assert item["run_id"] == "run-1"
    assert item["step_name"] == "run_pipeline"
    assert item["status"] == "RUNNING"
    assert "glue_job_run_id" not in item
    assert "task_token" not in item
    assert "token-abc" not in repr(item)

    token_item = mock_tokens_table.put_item.call_args.kwargs["Item"]
    assert token_item["approval_id"] == "glue_run#run-1"
    assert token_item["task_token"] == "token-abc"

    # Post-start update attaches the job run id to key + output_data,
    # making the record findable via the GlueJobRunIndex GSI.
    update_kwargs = mock_runs_table.update_item.call_args.kwargs
    assert update_kwargs["Key"] == {"run_id": "run-1", "step_name": "run_pipeline"}
    assert update_kwargs["ExpressionAttributeValues"] == {":jid": "jr-123"}
    assert "glue_job_run_id" in update_kwargs["UpdateExpression"]


def test_start_failure_cleans_up_token_record_and_raises_transient():
    """Retryable Glue start failure → token record deleted + TransientStepFailure."""
    call_order = []
    start_error = ClientError(
        {"Error": {"Code": "ConcurrentRunsExceededException", "Message": "Too many concurrent runs"}},
        "StartJobRun",
    )
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order, start_side_effect=start_error)
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)
    mock_sfn = MagicMock()

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", mock_sfn),
        pytest.raises(_mod.TransientStepFailure) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    assert exc_info.value.error_type == "transient"
    mock_runs_table.delete_item.assert_called_once_with(Key={"run_id": "run-1", "step_name": "run_pipeline"})
    mock_tokens_table.delete_item.assert_called_once_with(Key=TOKEN_KEY)
    # Existing failure-path side effects are preserved: task failure sent
    # with the event's token, error-state record written AFTER both cleanup
    # deletes.
    mock_sfn.send_task_failure.assert_called_once()
    assert mock_sfn.send_task_failure.call_args.kwargs["taskToken"] == "token-abc"
    assert call_order == ["token_put", "put_item", "start_job_run", "delete_item", "token_delete", "put_item"]
    error_item = mock_runs_table.put_item.call_args.kwargs["Item"]
    assert error_item["status"] == "failed"
    assert "task_token" not in error_item


def test_start_failure_cleans_up_token_record_and_raises_permanent():
    """Non-retryable Glue start failure → token record deleted + permanent StepFailure."""
    call_order = []
    start_error = ClientError(
        {"Error": {"Code": "EntityNotFoundException", "Message": "Job not found"}},
        "StartJobRun",
    )
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order, start_side_effect=start_error)
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    assert exc_info.value.error_type == "permanent"
    assert not isinstance(exc_info.value, _mod.TransientStepFailure)
    mock_runs_table.delete_item.assert_called_once_with(Key={"run_id": "run-1", "step_name": "run_pipeline"})
    mock_tokens_table.delete_item.assert_called_once_with(Key=TOKEN_KEY)


def test_pre_start_failure_does_not_delete_record():
    """Failures before the token row is written never delete anything.

    The cleanup contract is scoped to the rows THIS invocation wrote —
    an early validation failure (pipeline not found) must not touch state.
    """
    mock_runs_table = MagicMock()
    mock_tokens_table = MagicMock()
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}  # pipeline not found

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure),
    ):
        _mod.lambda_handler(_event(), None)

    mock_runs_table.delete_item.assert_not_called()
    mock_tokens_table.put_item.assert_not_called()
    mock_tokens_table.delete_item.assert_not_called()


def test_token_row_carries_the_approval_ttl_backstop():
    """The token row's TTL is the approval path's TASK_TOKEN_TTL_SECONDS
    (7 days), stamped from the wall clock at write time, plus the run's ids."""
    call_order = []
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order)
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    before = int(time.time())
    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
    ):
        _mod.lambda_handler(_event(), None)
    after = int(time.time())

    ttl_seconds = _mod.TASK_TOKEN_TTL_SECONDS
    assert ttl_seconds == 7 * 24 * 3600
    token_item = mock_tokens_table.put_item.call_args.kwargs["Item"]
    assert before + ttl_seconds <= token_item["ttl"] <= after + ttl_seconds
    assert token_item["run_id"] == "run-1"
    assert token_item["pipeline_id"] == "pipe-1"


def test_failed_token_write_never_starts_the_job():
    """If the token cannot be stored, the job must not start: a running job
    whose token is nowhere is the stranded run R5.1 exists to prevent. The
    failure path sends the task failure with the event's token and deletes
    nothing it did not write."""
    mock_runs_table = MagicMock()
    mock_tokens_table = MagicMock()
    mock_tokens_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "PutItem"
    )
    mock_glue = MagicMock()
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)
    mock_sfn = MagicMock()

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", mock_sfn),
        pytest.raises(_mod.StepFailure),
    ):
        _mod.lambda_handler(_event(), None)

    mock_glue.start_job_run.assert_not_called()
    mock_tokens_table.delete_item.assert_not_called()
    mock_runs_table.delete_item.assert_not_called()
    mock_sfn.send_task_failure.assert_called_once()
    assert mock_sfn.send_task_failure.call_args.kwargs["taskToken"] == "token-abc"


def test_update_failure_after_start_stops_the_job_then_cleans_up():
    """start_job_run succeeded but attaching the job run id failed (assessment F2).

    Until 2026-10-11 this path was treated as a failed start: rows deleted, task
    failed, and the job left running with no row that named it. Now the job is
    stopped first, the cleanup still runs, and the error row carries the job's
    identity so what ran can be found."""
    call_order = []
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order)

    def _update(**kw):
        call_order.append("update_item")
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem")

    mock_runs_table.update_item.side_effect = _update
    mock_glue.batch_stop_job_run.side_effect = lambda **kw: call_order.append("stop_job_run") or {"Errors": []}
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure) as raised,
    ):
        _mod.lambda_handler(_event(), None)

    assert call_order == [
        "token_put",
        "put_item",
        "start_job_run",
        "token_update",  # the post-start fence check stamps the job id on the token row
        "update_item",  # the state-row attach, which fails
        "stop_job_run",  # the job is stopped before anything is deleted
        "delete_item",
        "token_delete",
        "put_item",
    ]
    mock_glue.batch_stop_job_run.assert_called_once_with(JobName="agentic-data-ingestion-pipe-1", JobRunIds=["jr-123"])
    mock_tokens_table.delete_item.assert_called_once_with(Key=TOKEN_KEY)
    error_item = mock_runs_table.put_item.call_args.kwargs["Item"]
    assert error_item["status"] == "failed"
    assert error_item["glue_job_run_id"] == "jr-123", "the job that ran must be findable from the error row"
    assert error_item["glue_job_name"] == "agentic-data-ingestion-pipe-1"
    assert error_item["glue_job_stop_requested"] is True
    assert error_item["error_type"] == "GlueJobPostStartError"
    assert raised.value.details["glue_job_run_id"] == "jr-123"


def test_a_failed_stop_after_a_post_start_failure_does_not_mask_the_cause():
    """BatchStopJobRun itself failing is logged; the step still fails on the real error."""
    call_order = []
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks(call_order)
    mock_runs_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}}, "UpdateItem"
    )
    mock_glue.batch_stop_job_run.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "BatchStopJobRun"
    )
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure) as raised,
    ):
        _mod.lambda_handler(_event(), None)

    assert "boom" in str(raised.value)
    # The error row still names the job, so an operator can stop it by hand.
    assert mock_runs_table.put_item.call_args.kwargs["Item"]["glue_job_run_id"] == "jr-123"


def test_token_cleanup_failure_does_not_mask_the_start_error():
    """A failed token-row delete is logged, not raised: the step still fails
    with the start error's classification, and the error row is still written."""
    start_error = ClientError(
        {"Error": {"Code": "ConcurrentRunsExceededException", "Message": "Too many concurrent runs"}},
        "StartJobRun",
    )
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks([], start_side_effect=start_error)
    mock_tokens_table.delete_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "DeleteItem"
    )
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)
    mock_sfn = MagicMock()

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", mock_sfn),
        pytest.raises(_mod.TransientStepFailure),
    ):
        _mod.lambda_handler(_event(), None)

    mock_sfn.send_task_failure.assert_called_once()
    assert mock_runs_table.put_item.call_args.kwargs["Item"]["status"] == "failed"


def test_task_token_is_not_logged():
    """The event is logged without its taskToken: the dashboard reads this
    function's log group back to the browser."""
    mock_runs_table, mock_tokens_table, mock_glue = _tracking_mocks([])
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)
    mock_logger = MagicMock()

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", mock_tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        patch.object(_mod, "logger", mock_logger),
    ):
        _mod.lambda_handler(_event(), None)

    logged = repr(mock_logger.mock_calls)
    assert "pipe-1" in logged  # the event IS logged …
    assert "token-abc" not in logged  # … without its token


def test_happy_path_record_findable_via_gsi_after_start(mock_dynamodb_tables):
    """The token row exists the moment the job starts, and the state row is
    GSI-findable by glue_job_run_id right after — closing the fast-fail race.
    The state row never carries the token."""
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    tokens_table = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
    pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    pipelines_table.put_item(
        Item={"pipeline_id": "pipe-1", "glue_job_metadata": {"glue_job_name": "agentic-data-ingestion-pipe-1"}}
    )

    mock_glue = MagicMock()

    def _start(**kw):
        # Fast-fail race closure: at the instant Glue starts the job, the
        # token row and the RUNNING state row must both already be persisted.
        token_row = tokens_table.get_item(Key=TOKEN_KEY).get("Item")
        assert token_row is not None, "token row must exist before start_job_run"
        assert token_row["task_token"] == "token-abc"
        record = state_table.get_item(Key={"run_id": "run-1", "step_name": "run_pipeline"}).get("Item")
        assert record is not None, "state row must exist before start_job_run"
        assert "task_token" not in record
        return {"JobRunId": "jr-1"}

    mock_glue.start_job_run.side_effect = _start

    with (
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(_mod, "task_tokens_table", tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
    ):
        _mod.lambda_handler(_event(), None)

    # After start, the record is findable exactly the way job_completion
    # looks it up: GlueJobRunIndex GSI keyed on glue_job_run_id.
    result = state_table.query(
        IndexName="GlueJobRunIndex",
        KeyConditionExpression=Key("glue_job_run_id").eq("jr-1"),
        Limit=1,
    )
    items = result.get("Items", [])
    assert len(items) == 1
    assert items[0]["run_id"] == "run-1"
    assert "task_token" not in items[0]
    assert items[0]["output_data"]["glue_job_run_id"] == "jr-1"

    # No state row anywhere carries the token; exactly one token row does.
    assert all("task_token" not in row for row in state_table.scan()["Items"])
    token_rows = tokens_table.scan()["Items"]
    assert [row["approval_id"] for row in token_rows] == ["glue_run#run-1"]
    assert token_rows[0]["task_token"] == "token-abc"


def test_retried_invocation_overwrites_own_record(mock_dynamodb_tables):
    """A retried task overwrites its own rows — no duplicate-key failure, and
    the retry's fresh token replaces the spent one."""
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    tokens_table = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
    pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    pipelines_table.put_item(
        Item={"pipeline_id": "pipe-1", "glue_job_metadata": {"glue_job_name": "agentic-data-ingestion-pipe-1"}}
    )

    mock_glue = MagicMock()
    mock_glue.start_job_run.side_effect = [{"JobRunId": "jr-1"}, {"JobRunId": "jr-2"}]

    with (
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(_mod, "task_tokens_table", tokens_table),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
    ):
        _mod.lambda_handler(_event(), None)  # first attempt
        _mod.lambda_handler({**_event(), "taskToken": "token-retry"}, None)  # SFN retry — same run_id

    # Exactly one record per (run_id, step_name); it carries the newest job run id.
    records = state_table.query(KeyConditionExpression=Key("run_id").eq("run-1"))["Items"]
    run_pipeline_records = [r for r in records if r["step_name"] == "run_pipeline"]
    assert len(run_pipeline_records) == 1
    assert run_pipeline_records[0]["glue_job_run_id"] == "jr-2"
    assert "task_token" not in run_pipeline_records[0]

    # Exactly one token row for the run, carrying the retry's token.
    token_rows = tokens_table.scan()["Items"]
    assert len(token_rows) == 1
    assert token_rows[0]["task_token"] == "token-retry"
