# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""job_completion closes a run whose Glue job a dashboard cancel stopped.

Requirements these derive from (the run-cancellation decision, 2026-10-11):

* A cancelled run ends CANCELLED, not FAILED, and dispatches no self-healing.
* The pipeline lock is released only once Glue reports the job stopped. The
  dashboard leaves the execution waiting on the Glue callback; this handler
  stops it, and the execution's ABORTED event is what releases the lock.
* A job that finished before the stop took effect has loaded its data, so a
  SUCCEEDED completion closes the run as a success through the ordinary path.
* The marker names one Glue job run; a completion for any other job run is
  handled as if no cancel had been asked for.
* A redelivery publishes the run's cost once.

The dashboard's ``cancel_run`` writes the marker on the run's Glue-callback
token row: ``cancel_requested_at``, ``cancel_execution_arn`` and
``cancel_glue_job_run_id``.
"""

import os
import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_job_completion_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
sys.path.insert(0, _job_completion_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.modules.pop("lambda_function", None)
import lambda_function as _mod

RUN_ID = "run-cancel-1"
PIPELINE_ID = "pipe-cancel"
JOB_NAME = "agentic-data-ingestion-job-pipe-cancel"
JOB_RUN_ID = "jr-cancel-1"
EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:agentic-data-ingestion-pipeline:file-orders-1"


@pytest.fixture(autouse=True)
def _ensure_correct_module(monkeypatch):
    sys.modules["lambda_function"] = _mod
    monkeypatch.setenv("SELF_HEALING_FN", "agentic_data_ingestion_self_healing_dispatch")
    yield


class _World:
    """The handler's collaborators, as mocks, for one completion event."""

    def __init__(self, *, marker_job_run_id: str | None = JOB_RUN_ID, execution_status: str = "RUNNING"):
        self.runs = MagicMock(name="pipeline_runs_table")
        self.runs.query.return_value = {
            "Items": [
                {
                    "run_id": RUN_ID,
                    "step_name": "run_pipeline",
                    "original_pipeline_id": PIPELINE_ID,
                    "glue_job_run_id": JOB_RUN_ID,
                    "status": "RUNNING",
                }
            ]
        }
        self.runs.get_item.return_value = {}
        token_row = {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-cancel"}
        if marker_job_run_id is not None:
            token_row.update(
                {
                    "cancel_requested_at": "2026-10-11T09:05:00+00:00",
                    "cancel_execution_arn": EXECUTION_ARN,
                    "cancel_glue_job_run_id": marker_job_run_id,
                }
            )
        self.tokens = MagicMock(name="task_tokens_table")
        self.tokens.get_item.return_value = {"Item": token_row}
        self.glue = MagicMock(name="glue")
        self.glue.get_job_run.return_value = {
            "JobRun": {"JobRunState": "STOPPED", "ExecutionTime": 120, "NumberOfWorkers": 2, "WorkerType": "G.1X"}
        }
        self.sfn = MagicMock(name="sfn")
        self.sfn.describe_execution.return_value = {"status": execution_status}
        self.lambda_client = MagicMock(name="lambda_client")
        self.cost = MagicMock(name="publish_glue_run_cost")
        self.outcome = MagicMock(name="publish_run_outcome")

    def run(self, state: str) -> dict:
        with ExitStack() as stack:
            for target, value in (
                ("pipeline_runs_table", self.runs),
                ("task_tokens_table", self.tokens),
                ("glue_client", self.glue),
                ("pipelines_table", MagicMock(name="pipelines_table")),
                ("sfn_client", self.sfn),
                ("lambda_client", self.lambda_client),
                ("publish_glue_run_cost", self.cost),
                ("publish_run_outcome", self.outcome),
            ):
                stack.enter_context(patch.object(_mod, target, value))
            return _mod.lambda_handler({"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": state}}, None)

    def state_row_statuses(self) -> list[str]:
        return [
            c.kwargs["ExpressionAttributeValues"][":status"]
            for c in self.runs.update_item.call_args_list
            if ":status" in c.kwargs.get("ExpressionAttributeValues", {})
        ]


@pytest.mark.parametrize("state", ["STOPPED", "FAILED", "TIMEOUT"])
def test_a_stopped_job_closes_the_run_as_cancelled(state):
    world = _World()

    result = world.run(state)

    assert result["statusCode"] == 200
    world.sfn.stop_execution.assert_called_once()
    assert world.sfn.stop_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
    assert world.sfn.stop_execution.call_args.kwargs["error"] == "UserCancelled"
    assert world.state_row_statuses() == ["cancelled"]
    world.sfn.send_task_failure.assert_not_called()
    world.sfn.send_task_success.assert_not_called()
    world.lambda_client.invoke.assert_not_called()  # no self-healing for a run stopped on purpose
    world.outcome.assert_not_called()  # the error rate counts failures and successes, not cancels
    world.tokens.delete_item.assert_called_once()


def test_the_cancel_releases_nothing_itself_the_aborted_execution_does():
    """The lock is released by SfnTerminalLockRelease on ABORTED. Stopping the
    execution here is the release, so it must happen; no locks table is touched."""
    world = _World()

    world.run("STOPPED")

    assert world.sfn.stop_execution.call_count == 1
    assert not hasattr(_mod, "locks_table")


def test_an_execution_that_is_no_longer_running_is_not_stopped_again():
    world = _World(execution_status="ABORTED")

    world.run("STOPPED")

    world.sfn.stop_execution.assert_not_called()
    assert world.state_row_statuses() == ["cancelled"]


def test_the_stopped_run_publishes_its_cost_once():
    world = _World()
    world.run("STOPPED")
    assert world.cost.call_count == 1

    duplicate = _World()
    duplicate.runs.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already cancelled"}}, "UpdateItem"
    )
    result = duplicate.run("STOPPED")

    assert result["statusCode"] == 200
    duplicate.cost.assert_not_called()


def test_the_state_row_write_is_conditional_on_not_already_cancelled():
    world = _World()
    world.run("STOPPED")

    call = world.runs.update_item.call_args
    assert call.kwargs["Key"] == {"run_id": RUN_ID, "step_name": "run_pipeline"}
    assert "<> :status" in call.kwargs["ConditionExpression"]


def test_a_job_that_succeeded_before_the_stop_landed_closes_as_a_success():
    world = _World()

    world.run("SUCCEEDED")

    world.sfn.send_task_success.assert_called_once()
    world.sfn.stop_execution.assert_not_called()
    assert world.state_row_statuses() == ["success"]


def test_a_fence_the_launcher_started_a_job_under_closes_the_run_as_cancelled():
    """Assessment F1, the after-start arm. cancel_run found no job and wrote the
    fence with no job id; run_pipeline then started a job anyway, stopped it, and
    stamped its id onto the token row as glue_job_run_id. That identity is what
    ties the STOPPED event to the cancel."""
    world = _World(marker_job_run_id=None)
    token_row = world.tokens.get_item.return_value["Item"]
    token_row.update(
        {
            "cancel_requested_at": "2026-10-11T10:00:01+00:00",
            "cancel_execution_arn": EXECUTION_ARN,
            "glue_job_run_id": JOB_RUN_ID,
            "glue_job_name": JOB_NAME,
        }
    )

    result = world.run("STOPPED")

    assert result["statusCode"] == 200
    world.sfn.stop_execution.assert_called_once()
    assert world.state_row_statuses() == ["cancelled"]
    world.sfn.send_task_failure.assert_not_called()
    world.lambda_client.invoke.assert_not_called()


def test_a_fence_with_no_job_named_does_not_claim_a_different_job_run():
    """A fence the launcher stamped with one job id must not close a completion for another."""
    world = _World(marker_job_run_id=None)
    world.tokens.get_item.return_value["Item"].update(
        {
            "cancel_requested_at": "2026-10-11T10:00:01+00:00",
            "cancel_execution_arn": EXECUTION_ARN,
            "glue_job_run_id": "jr-a-different-run",
        }
    )

    world.run("STOPPED")

    world.sfn.stop_execution.assert_not_called()
    world.sfn.send_task_failure.assert_called_once()


def test_a_marker_for_another_job_run_is_ignored():
    world = _World(marker_job_run_id="jr-some-other-run")

    world.run("STOPPED")

    world.sfn.stop_execution.assert_not_called()
    world.sfn.send_task_failure.assert_called_once()
    assert world.state_row_statuses() == ["failed"]


def test_without_a_marker_a_stopped_job_still_fails_the_run():
    world = _World(marker_job_run_id=None)

    world.run("STOPPED")

    world.sfn.stop_execution.assert_not_called()
    world.sfn.send_task_failure.assert_called_once()
    world.lambda_client.invoke.assert_called_once()  # self-healing, as before


def test_a_redelivery_after_the_token_row_is_gone_is_a_clean_duplicate():
    world = _World()
    world.tokens.get_item.return_value = {}
    world.runs.query.return_value["Items"][0]["status"] = "cancelled"

    result = world.run("STOPPED")

    assert result["statusCode"] == 200
    world.sfn.stop_execution.assert_not_called()
    world.runs.update_item.assert_not_called()


def test_a_stop_that_fails_is_retried_not_swallowed():
    world = _World()
    world.sfn.stop_execution.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "StopExecution"
    )

    with pytest.raises(ClientError):
        world.run("STOPPED")

    world.runs.update_item.assert_not_called()  # the run is not closed before its execution is stopped
