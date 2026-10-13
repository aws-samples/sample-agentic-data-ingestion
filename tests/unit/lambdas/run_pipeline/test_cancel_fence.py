# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A cancel that lands while the launcher is running never leaves a Glue job unwatched.

Assessment F1 (2026-10-11): the dashboard cancel found no Glue job, aborted the
execution and marked the run CANCELLED, and an already-running RunPipeline
Lambda then wrote its rows and started a job nobody stopped. The probe that
found it paused the launcher before its first write; the window also exists
between the launcher's token put and the moment start_job_run returns.

The fix is a fence on the run's ``glue_run#<run_id>`` token row:
``cancel_requested_at``, written by the cancel before it stops the execution.
Both of the launcher's token-row writes are conditional on its absence:

* the pre-start put. Fence present: nothing started, raise, touch no Glue;
* the post-start stamp of the job's id. Fence present: the job has started, so
  the launcher stops it, keeps the id on the state row for job_completion, and
  raises.

Backed by moto (``mock_dynamodb_tables``): the claims are about what DynamoDB
does with the condition, which a MagicMock cannot answer. Only Glue and Step
Functions are mocked; the cancel's write is replayed directly, with the exact
expression ``cancel_run`` uses, because importing the dashboard handler here
would drag its whole module tree into this Lambda's test process.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_run_pipeline_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_run_pipeline")
)
sys.path.insert(0, _run_pipeline_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.modules.pop("lambda_function", None)
import lambda_function as _mod

RUN_ID = "run-2026-10-11-10-00-00-fence001"
PIPELINE_ID = "orders-1a2b3c4d"
JOB_NAME = f"agentic-data-ingestion-job-{PIPELINE_ID}"
TOKEN_KEY = {"approval_id": f"glue_run#{RUN_ID}"}
EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:agentic-data-ingestion-pipeline:file-orders-1"


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


@pytest.fixture
def world(mock_dynamodb_tables):
    tokens = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
    state = mock_dynamodb_tables["agentic_data_ingestion_state"]
    pipelines = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    pipelines.put_item(Item={"pipeline_id": PIPELINE_ID, "glue_job_metadata": {"glue_job_name": JOB_NAME}})
    glue = MagicMock(name="glue")
    glue.start_job_run.return_value = {"JobRunId": "jr-fence-1"}
    glue.batch_stop_job_run.return_value = {"SuccessfulSubmissions": [{"JobRunId": "jr-fence-1"}], "Errors": []}
    sfn = MagicMock(name="stepfunctions")
    return {"tokens": tokens, "state": state, "pipelines": pipelines, "glue": glue, "sfn": sfn}


def _cancel_writes_fence(tokens) -> None:
    """Exactly the write cancel_run makes when it finds no Glue job."""
    tokens.update_item(
        Key=TOKEN_KEY,
        UpdateExpression=(
            "SET cancel_requested_at = :now, cancel_execution_arn = :arn, "
            "run_id = if_not_exists(run_id, :run_id), #ttl = if_not_exists(#ttl, :ttl)"
        ),
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":now": "2026-10-11T10:00:01+00:00",
            ":arn": EXECUTION_ARN,
            ":run_id": RUN_ID,
            ":ttl": 9_999_999_999,
        },
    )


def _run(world) -> None:
    with (
        patch.object(_mod, "pipelines_table", world["pipelines"]),
        patch.object(_mod, "pipeline_runs_table", world["state"]),
        patch.object(_mod, "task_tokens_table", world["tokens"]),
        patch.object(_mod, "glue_client", world["glue"]),
        patch.object(_mod, "sfn_client", world["sfn"]),
    ):
        _mod.lambda_handler({"pipeline_id": PIPELINE_ID, "run_id": RUN_ID, "taskToken": "token-fence"}, None)


def _state_row(world):
    return world["state"].get_item(Key={"run_id": RUN_ID, "step_name": "run_pipeline"}).get("Item")


class TestCancelBeforeTheLauncherWrites:
    """The assessment's probe: the launcher is invoked, the cancel lands, the launcher resumes."""

    def test_the_job_is_never_started(self, world):
        _cancel_writes_fence(world["tokens"])

        with pytest.raises(_mod.RunCancelledBeforeStart):
            _run(world)

        world["glue"].start_job_run.assert_not_called()
        world["glue"].batch_stop_job_run.assert_not_called()
        assert _state_row(world) is None, "no RUNNING row for a run that never ran"

    def test_the_cancels_fence_row_is_left_alone(self, world):
        _cancel_writes_fence(world["tokens"])

        with pytest.raises(_mod.RunCancelledBeforeStart):
            _run(world)

        row = world["tokens"].get_item(Key=TOKEN_KEY)["Item"]
        assert row["cancel_requested_at"] == "2026-10-11T10:00:01+00:00"
        assert "task_token" not in row, "the launcher's put must not have replaced the fence row"

    def test_no_task_failure_is_sent_to_an_aborted_execution(self, world):
        _cancel_writes_fence(world["tokens"])

        with pytest.raises(_mod.RunCancelledBeforeStart):
            _run(world)

        world["sfn"].send_task_failure.assert_not_called()


class TestCancelBetweenTokenPutAndStart:
    """The cancel lands after the launcher's first write but before start_job_run returns."""

    def test_the_launcher_stops_the_job_it_started(self, world):
        def _start_then_cancel(**kw):
            _cancel_writes_fence(world["tokens"])
            return {"JobRunId": "jr-fence-1"}

        world["glue"].start_job_run.side_effect = _start_then_cancel

        with pytest.raises(_mod.RunCancelledAfterStart) as raised:
            _run(world)

        world["glue"].batch_stop_job_run.assert_called_once_with(JobName=JOB_NAME, JobRunIds=["jr-fence-1"])
        assert raised.value.details["glue_job_run_id"] == "jr-fence-1"
        world["sfn"].send_task_failure.assert_not_called()

    def test_the_state_row_keeps_the_job_id_for_job_completion(self, world):
        """job_completion finds the run through the GlueJobRunIndex when the STOPPED event lands."""

        def _start_then_cancel(**kw):
            _cancel_writes_fence(world["tokens"])
            return {"JobRunId": "jr-fence-1"}

        world["glue"].start_job_run.side_effect = _start_then_cancel

        with pytest.raises(_mod.RunCancelledAfterStart):
            _run(world)

        row = _state_row(world)
        assert row is not None, "the RUNNING row must survive so the STOPPED event can be matched"
        assert row["glue_job_run_id"] == "jr-fence-1"
        token = world["tokens"].get_item(Key=TOKEN_KEY)["Item"]
        assert token["cancel_requested_at"], "the fence stays for job_completion to read"
        assert token["cancel_execution_arn"] == EXECUTION_ARN
        assert token["task_token"] == "token-fence", "the launcher's token put landed before the cancel"


class TestNoCancel:
    def test_the_launcher_stamps_the_job_onto_its_own_token_row(self, world):
        _run(world)

        token = world["tokens"].get_item(Key=TOKEN_KEY)["Item"]
        assert token["glue_job_run_id"] == "jr-fence-1"
        assert token["glue_job_name"] == JOB_NAME
        assert "cancel_requested_at" not in token
        assert _state_row(world)["glue_job_run_id"] == "jr-fence-1"
        world["glue"].batch_stop_job_run.assert_not_called()

    def test_a_retry_still_overwrites_its_own_token_row(self, world):
        """The fence condition must not break the retry idempotency P28 R5.1 relies on."""
        _run(world)
        world["glue"].start_job_run.return_value = {"JobRunId": "jr-fence-2"}

        _run(world)

        token = world["tokens"].get_item(Key=TOKEN_KEY)["Item"]
        assert token["glue_job_run_id"] == "jr-fence-2"
        assert world["glue"].start_job_run.call_count == 2
