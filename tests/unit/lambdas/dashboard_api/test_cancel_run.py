# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""POST /runs/{runId}/cancel: what a cancel does, and what it refuses to do.

Requirements these derive from (the run-cancellation decision, 2026-10-11):

* No Glue job running: stop the execution and mark the run CANCELLED. 200.
* Glue job running: stop the job and mark the run CANCELLING. 202. The execution
  is NOT stopped here; job_completion stops it once Glue reports the job stopped,
  so the pipeline lock (released on ABORTED) outlives the Glue job.
* Unknown run: 404. A finished run: 409. Already CANCELLED or CANCELLING:
  answered idempotently with the same status code as the first cancel.
* A failed AWS call is reported, never swallowed, and leaves the run as it was.
* The execution is addressed by ARN. Rows store the execution NAME; the old
  cancel passed that to StopExecution, got InvalidArn and reported success.

DynamoDB is moto (``mock_dynamodb_tables``); Step Functions and Glue are
MagicMocks, because no moto fixture for them exists in this suite.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

PIPELINE_ID = "orders-1a2b3c4d"
RUN_ID = "run-2026-10-11-09-00-00-cafe0001"
EXECUTION_NAME = "file-orders_csv-20261011T090000-0123456789ab"
# mock_lambda_env's STATE_MACHINE_ARN is arn:aws:states:us-east-1:123456789012:stateMachine:test
EXECUTION_ARN = f"arn:aws:states:us-east-1:123456789012:execution:test:{EXECUTION_NAME}"
JOB_NAME = f"agentic-data-ingestion-job-{PIPELINE_ID}"
JOB_RUN_ID = "jr_0123abcd"


class _World:
    def __init__(self, tables):
        self.executions = tables["agentic_data_ingestion_executions"]
        self.state = tables["agentic_data_ingestion_state"]
        self.tokens = tables["agentic_data_ingestion_task_tokens"]
        self.sfn = MagicMock(name="stepfunctions")
        self.sfn.describe_execution.return_value = {"status": "RUNNING"}
        self.glue = MagicMock(name="glue")
        self.glue.get_job_run.return_value = {"JobRun": {"JobRunState": "RUNNING"}}
        self.glue.batch_stop_job_run.return_value = {
            "SuccessfulSubmissions": [{"JobName": JOB_NAME, "JobRunId": JOB_RUN_ID}],
            "Errors": [],
        }
        self.audit = MagicMock(name="write_audit_entry")

    def seed_run(self, status: str = "RUNNING", **extra) -> None:
        self.executions.put_item(
            Item={
                "pipeline_id": PIPELINE_ID,
                "run_id": RUN_ID,
                "status": status,
                "started_at": "2026-10-11T09:00:00+00:00",
                "current_step": "create_code",
                "execution_id": EXECUTION_NAME,
                **extra,
            }
        )

    def seed_glue_job(self, *, with_run_id: bool = True) -> None:
        item = {
            "run_id": RUN_ID,
            "step_name": "run_pipeline",
            "status": "RUNNING",
            "pipeline_id": PIPELINE_ID,
            "original_pipeline_id": PIPELINE_ID,
            "glue_job_name": JOB_NAME,
        }
        if with_run_id:
            item["glue_job_run_id"] = JOB_RUN_ID
        self.state.put_item(Item=item)
        self.tokens.put_item(Item={"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-1", "run_id": RUN_ID})

    def cancel(self, run_id: str = RUN_ID) -> tuple[int, dict]:
        import boto3
        from handlers import pipelines_runs as runs

        clients = {"stepfunctions": self.sfn, "glue": self.glue}
        with (
            patch.object(runs, "executions_table", self.executions),
            patch.object(runs, "state_table", self.state),
            patch.object(runs, "dynamodb", boto3.resource("dynamodb", region_name="us-east-1")),
            patch.object(runs, "write_audit_entry", self.audit),
            patch.object(runs.boto3, "client", side_effect=lambda service, *a, **k: clients[service]),
        ):
            response = runs.cancel_run(run_id, event={})
        return response["statusCode"], json.loads(response["body"])

    def row(self) -> dict:
        return self.executions.get_item(Key={"pipeline_id": PIPELINE_ID, "run_id": RUN_ID})["Item"]

    def token_row(self) -> dict:
        return self.tokens.get_item(Key={"approval_id": f"glue_run#{RUN_ID}"})["Item"]


@pytest.fixture
def world(mock_lambda_env, mock_dynamodb_tables):
    return _World(mock_dynamodb_tables)


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


# ── No Glue job running: stop now ─────────────────────────────────────────


def test_a_run_at_a_lambda_step_is_stopped_and_cancelled(world):
    world.seed_run()

    code, body = world.cancel()

    assert (code, body["status"]) == (200, "CANCELLED")
    world.sfn.stop_execution.assert_called_once()
    assert world.sfn.stop_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
    assert world.sfn.stop_execution.call_args.kwargs["error"] == "UserCancelled"
    row = world.row()
    assert row["status"] == "CANCELLED"
    assert "completed_at" in row
    world.glue.batch_stop_job_run.assert_not_called()
    assert world.audit.call_args.kwargs["details"]["status"] == "CANCELLED"


def test_a_cancel_with_no_glue_job_leaves_a_fence_for_a_launcher_already_running(world):
    """Assessment F1. The RunPipeline Lambda may be mid-invocation with nothing
    written yet; stopping the execution does not stop it. The fence on its token
    row is what makes it refuse to start the job (run_pipeline's two conditional
    token-row writes), so the fence must exist before the execution is stopped."""
    world.seed_run()
    order: list[str] = []
    world.sfn.stop_execution.side_effect = lambda **kw: order.append("stop_execution")

    code, _ = world.cancel()

    assert code == 200
    fence = world.tokens.get_item(Key={"approval_id": f"glue_run#{RUN_ID}"})["Item"]
    assert fence["cancel_requested_at"]
    assert fence["cancel_execution_arn"] == EXECUTION_ARN
    assert "cancel_glue_job_run_id" not in fence, "no job to name: the launcher stamps its own id if it starts one"
    assert fence["run_id"] == RUN_ID
    assert "ttl" in fence, "a fence row the cancel created must expire on its own"
    assert order == ["stop_execution"]


def test_the_fence_does_not_disturb_a_token_row_the_launcher_already_wrote(world):
    """The launcher wrote its row but has no job yet (the starting window is a
    409, so this is the row with no glue_job_run_id after the job already ended)."""
    world.seed_run()
    world.tokens.put_item(
        Item={"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-1", "run_id": RUN_ID, "ttl": 1_900_000_000}
    )

    code, _ = world.cancel()

    assert code == 200
    fence = world.tokens.get_item(Key={"approval_id": f"glue_run#{RUN_ID}"})["Item"]
    assert fence["task_token"] == "token-1", "the stored callback token must survive"
    assert fence["ttl"] == 1_900_000_000, "if_not_exists: the launcher's TTL wins"
    assert fence["cancel_requested_at"]


def test_a_run_waiting_for_approval_loses_its_pending_approval(world):
    """The run list rewrites a row carrying pending_approval_id to WAITING_APPROVAL."""
    world.seed_run("RUNNING", pending_approval_id="appr-1", pending_approval_type="schema_mapping")

    code, _ = world.cancel()

    assert code == 200
    row = world.row()
    assert row["status"] == "CANCELLED"
    assert "pending_approval_id" not in row
    assert "pending_approval_type" not in row


def test_a_retry_stops_its_own_execution_not_the_first_one(world):
    world.seed_run(execution_name="retry-run-2026-10-11-abcd1234")

    world.cancel()

    stopped = world.sfn.stop_execution.call_args.kwargs["executionArn"]
    assert stopped == "arn:aws:states:us-east-1:123456789012:execution:test:retry-run-2026-10-11-abcd1234"


def test_an_execution_already_aborted_is_marked_without_a_second_stop(world):
    world.seed_run()
    world.sfn.describe_execution.return_value = {"status": "ABORTED"}

    code, body = world.cancel()

    assert (code, body["status"]) == (200, "CANCELLED")
    world.sfn.stop_execution.assert_not_called()


def test_a_glue_job_that_already_failed_does_not_block_the_cancel(world):
    world.seed_run()
    world.seed_glue_job()
    world.glue.get_job_run.return_value = {"JobRun": {"JobRunState": "FAILED"}}

    code, body = world.cancel()

    assert (code, body["status"]) == (200, "CANCELLED")
    world.sfn.stop_execution.assert_called_once()
    world.glue.batch_stop_job_run.assert_not_called()


# ── Glue job running: stop the job, defer the execution ───────────────────


def test_a_running_glue_job_is_stopped_and_the_run_is_cancelling(world):
    world.seed_run()
    world.seed_glue_job()

    code, body = world.cancel()

    assert (code, body["status"]) == (202, "CANCELLING")
    world.glue.batch_stop_job_run.assert_called_once_with(JobName=JOB_NAME, JobRunIds=[JOB_RUN_ID])
    world.sfn.stop_execution.assert_not_called()  # job_completion stops it once Glue has stopped
    assert world.row()["status"] == "CANCELLING"
    token = world.token_row()
    assert token["cancel_execution_arn"] == EXECUTION_ARN
    assert token["cancel_glue_job_run_id"] == JOB_RUN_ID
    assert token["task_token"] == "token-1", "the marker must not disturb the callback token"
    assert world.audit.call_args.kwargs["details"]["status"] == "CANCELLING"


def test_a_job_already_stopping_is_not_asked_to_stop_again(world):
    world.seed_run()
    world.seed_glue_job()
    world.glue.get_job_run.return_value = {"JobRun": {"JobRunState": "STOPPING"}}

    code, _ = world.cancel()

    assert code == 202
    world.glue.batch_stop_job_run.assert_not_called()
    assert world.token_row()["cancel_glue_job_run_id"] == JOB_RUN_ID


def test_a_glue_job_still_starting_is_refused_for_now(world):
    world.seed_run()
    world.seed_glue_job(with_run_id=False)

    code, body = world.cancel()

    assert code == 409
    assert "starting" in body["error"]
    world.sfn.stop_execution.assert_not_called()
    assert world.row()["status"] == "RUNNING"


def test_a_job_that_succeeded_is_left_to_finish(world):
    world.seed_run()
    world.seed_glue_job()
    world.glue.get_job_run.return_value = {"JobRun": {"JobRunState": "SUCCEEDED"}}

    code, _ = world.cancel()

    assert code == 409
    world.sfn.stop_execution.assert_not_called()
    assert world.row()["status"] == "RUNNING"


def test_a_stop_glue_rejects_restores_the_run(world):
    world.seed_run()
    world.seed_glue_job()
    world.glue.batch_stop_job_run.return_value = {
        "SuccessfulSubmissions": [],
        "Errors": [
            {"JobName": JOB_NAME, "JobRunId": JOB_RUN_ID, "ErrorDetail": {"ErrorCode": "InvalidInputException"}}
        ],
    }

    code, body = world.cancel()

    assert code == 409
    assert "InvalidInputException" in body["error"]
    assert world.row()["status"] == "RUNNING"
    assert "cancel_requested_at" not in world.row()
    assert "cancel_glue_job_run_id" not in world.token_row()


def test_a_glue_stop_that_raises_is_reported_and_the_run_restored(world):
    world.seed_run()
    world.seed_glue_job()
    world.glue.batch_stop_job_run.side_effect = _client_error("AccessDeniedException", "BatchStopJobRun")

    code, _ = world.cancel()

    assert code == 500
    assert world.row()["status"] == "RUNNING"
    assert "cancel_glue_job_run_id" not in world.token_row()


def test_a_missing_callback_row_means_the_job_already_finished(world):
    world.seed_run()
    world.seed_glue_job()
    world.tokens.delete_item(Key={"approval_id": f"glue_run#{RUN_ID}"})

    code, _ = world.cancel()

    assert code == 409
    world.glue.batch_stop_job_run.assert_not_called()
    assert world.row()["status"] == "RUNNING"


# ── Refusals and idempotency ──────────────────────────────────────────────


def test_an_unknown_run_is_404(world):
    code, body = world.cancel("run-that-does-not-exist")

    assert code == 404
    assert body["run_id"] == "run-that-does-not-exist"
    world.sfn.describe_execution.assert_not_called()


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "SKIPPED"])
def test_a_finished_run_is_409_and_untouched(world, status):
    world.seed_run(status)

    code, body = world.cancel()

    assert (code, body["status"]) == (409, status)
    world.sfn.stop_execution.assert_not_called()
    assert world.row()["status"] == status


@pytest.mark.parametrize(("status", "expected_code"), [("CANCELLED", 200), ("CANCELLING", 202)])
def test_a_second_cancel_gets_the_first_answer_again(world, status, expected_code):
    world.seed_run(status)

    code, body = world.cancel()

    assert (code, body["status"]) == (expected_code, status)
    world.sfn.describe_execution.assert_not_called()
    world.audit.assert_not_called()


def test_a_row_lagging_a_finished_execution_is_409(world):
    world.seed_run()
    world.sfn.describe_execution.return_value = {"status": "SUCCEEDED"}

    code, body = world.cancel()

    assert code == 409
    assert "SUCCEEDED" in body["error"]
    assert world.row()["status"] == "RUNNING"


def test_a_stop_that_fails_is_reported_and_marks_nothing(world):
    """The old cancel swallowed this and returned 200."""
    world.seed_run()
    world.sfn.stop_execution.side_effect = _client_error("AccessDeniedException", "StopExecution")

    code, _ = world.cancel()

    assert code == 500
    assert world.row()["status"] == "RUNNING"


def test_a_run_with_no_recorded_execution_is_409(world):
    world.executions.put_item(
        Item={"pipeline_id": PIPELINE_ID, "run_id": RUN_ID, "status": "RUNNING", "started_at": "2026-10-11T09:00:00Z"}
    )

    code, _ = world.cancel()

    assert code == 409
    world.sfn.describe_execution.assert_not_called()


def test_the_execution_name_falls_back_to_the_file_validation_row(world):
    """A row the stream has not yet given an execution_id."""
    world.executions.put_item(
        Item={"pipeline_id": PIPELINE_ID, "run_id": RUN_ID, "status": "RUNNING", "started_at": "2026-10-11T09:00:00Z"}
    )
    world.state.put_item(Item={"run_id": RUN_ID, "step_name": "file_validation", "execution_id": EXECUTION_NAME})

    code, _ = world.cancel()

    assert code == 200
    assert world.sfn.stop_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
