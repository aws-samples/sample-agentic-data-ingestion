# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""job_completion publishes the success half of the pipeline error rate.

PipelineErrorRateAlarm averages one datapoint per finished run: 100 from
record_failure for a failure, 0 from here for a success. Only SUCCEEDED
publishes here. A FAILED, STOPPED or TIMEOUT Glue run also reaches
record_failure through the RunPipeline Catch, so publishing it here too would
count it twice. The publish sits on the post-callback anchor (LGR-ba3dfab-3),
so a duplicate delivery, which returns before the callback, publishes nothing.
The publisher itself is covered in
tests/unit/shared/test_metrics_facade_run_outcome.py.
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

RUN_ID = "run-a4-outcome-1"
PIPELINE_ID = "pipe-a4"
JOB_NAME = "agentic-data-ingestion-job-pipe-a4"
JOB_RUN_ID = "jr-a4"


@pytest.fixture(autouse=True)
def _ensure_correct_module(monkeypatch):
    sys.modules["lambda_function"] = _mod
    monkeypatch.setenv("ENVIRONMENT", "staging")
    yield


def _mocks(state: str):
    runs = MagicMock(name="pipeline_runs_table")
    runs.query.return_value = {
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
    runs.get_item.return_value = {}
    tokens = MagicMock(name="task_tokens_table")
    tokens.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-a4"}}
    glue = MagicMock(name="glue")
    glue.get_job_run.return_value = {
        "JobRun": {"JobRunState": state, "ExecutionTime": 60, "NumberOfWorkers": 2, "WorkerType": "G.1X"}
    }
    pipelines = MagicMock(name="pipelines_table")
    pipelines.get_item.return_value = {}
    return runs, tokens, glue, pipelines, MagicMock(name="sfn")


def _run(state: str, sfn_error: Exception | None = None) -> MagicMock:
    runs, tokens, glue, pipelines, sfn = _mocks(state)
    if sfn_error is not None:
        sfn.send_task_success.side_effect = sfn_error
        sfn.send_task_failure.side_effect = sfn_error
    outcome = MagicMock(name="publish_run_outcome")
    with ExitStack() as stack:
        for target, value in (
            ("pipeline_runs_table", runs),
            ("task_tokens_table", tokens),
            ("glue_client", glue),
            ("pipelines_table", pipelines),
            ("sfn_client", sfn),
            ("lambda_client", MagicMock()),
            ("publish_glue_run_cost", MagicMock()),
            ("publish_run_outcome", outcome),
        ):
            stack.enter_context(patch.object(_mod, target, value))
        _mod.lambda_handler({"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": state}}, None)
    return outcome


def test_a_succeeded_run_publishes_one_success_datapoint():
    outcome = _run("SUCCEEDED")

    outcome.assert_called_once_with(failed=False, environment="staging")


@pytest.mark.parametrize("state", ["FAILED", "STOPPED", "TIMEOUT"])
def test_a_failed_glue_run_is_left_to_record_failure(state):
    assert _run(state).call_count == 0


def test_a_duplicate_delivery_publishes_nothing():
    """The first delivery consumed the token; the duplicate's send is rejected
    and the handler returns before the post-callback anchor."""
    consumed = ClientError({"Error": {"Code": "TaskTimedOut", "Message": "consumed"}}, "SendTaskSuccess")

    assert _run("SUCCEEDED", sfn_error=consumed).call_count == 0
