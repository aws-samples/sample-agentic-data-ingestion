# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A cancelled run stays cancelled; a cancelling run can still end any way.

Requirements these derive from (the run-cancellation decision, 2026-10-11):

* A run the dashboard cancelled ends CANCELLED, not FAILED. Stopping a Step
  Functions execution does not stop a step Lambda that is already running, and
  that Lambda's state-row write still reaches this handler afterwards. It must
  not move a CANCELLED row.
* While a Glue stop is pending (CANCELLING) the run is not over: if the job
  finishes before the stop lands, the run is a success, and that must reach the
  row. When the job has stopped, job_completion writes the run_pipeline row
  ``cancelled`` and the run becomes CANCELLED.
* The upsert still creates a row that does not exist yet (p48 R9.2,
  LGR-7d19eca-3): the guard must not turn it into an update-only write.

Backed by moto (``mock_dynamodb_tables``) rather than a MagicMock, because the
claims are about what DynamoDB does with the condition, which a mock cannot
answer. The handler is loaded by path (LGR-b7d5490-1) and its tables are
swapped for the moto ones.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_execution_updater"
    )
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

EXECUTIONS_TABLE = "agentic_data_ingestion_executions"
PIPELINE_ID = "orders-cancel-1a2b"
RUN_ID = "run-2026-10-11-09-00-00-cafe0001"


@pytest.fixture
def updater(mock_dynamodb_tables):
    with (
        patch.dict(os.environ, {"AGENTIC_DATA_INGESTION_ENV": "local", "AWS_DEFAULT_REGION": "us-east-1"}),
        patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}),
        patch("boto3.resource", return_value=MagicMock()),
    ):
        sys.modules.pop("lambda_function", None)
        if SHARED_DIR not in sys.path:
            sys.path.insert(0, SHARED_DIR)
        spec = importlib.util.spec_from_file_location("lambda_function", os.path.join(LAMBDA_DIR, "lambda_function.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.executions_table = mock_dynamodb_tables[EXECUTIONS_TABLE]
    module.pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    module.errors_table = MagicMock(name="errors_table")
    return module


@pytest.fixture
def executions(mock_dynamodb_tables):
    return mock_dynamodb_tables[EXECUTIONS_TABLE]


def _record(step_name: str, status: str, event_name: str = "MODIFY") -> dict:
    image = {
        "run_id": RUN_ID,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "step_name": step_name,
        "status": status,
        "timestamp": "2026-10-11T09:00:00+00:00",
    }
    return {
        "eventID": f"evt-{step_name}-{status}",
        "eventName": event_name,
        "dynamodb": {"NewImage": {k: {"S": v} for k, v in image.items()}},
    }


def _seed(executions, status: str) -> None:
    executions.put_item(
        Item={
            "pipeline_id": PIPELINE_ID,
            "run_id": RUN_ID,
            "status": status,
            "current_step": "create_code",
            "started_at": "2026-10-11T09:00:00+00:00",
            "steps_status": {"file_validation": "SUCCEEDED"},
        }
    )


def _row(executions) -> dict:
    return executions.get_item(Key={"pipeline_id": PIPELINE_ID, "run_id": RUN_ID})["Item"]


@pytest.mark.parametrize(
    ("step_name", "status"),
    [
        ("create_code", "success"),  # a non-final step finishing: would derive RUNNING
        ("create_code", "failed"),  # a step failing after the stop: would derive FAILED
        ("run_pipeline", "success"),  # a final step: would derive SUCCEEDED
    ],
)
def test_a_late_step_write_does_not_move_a_cancelled_run(updater, executions, step_name, status):
    _seed(executions, "CANCELLED")

    updater.lambda_handler({"Records": [_record(step_name, status)]}, None)

    row = _row(executions)
    assert row["status"] == "CANCELLED"
    assert row["current_step"] == "create_code"
    assert row["steps_status"] == {"file_validation": "SUCCEEDED"}, "no step history is written for a cancelled run"
    updater.errors_table.put_item.assert_not_called()


def test_a_stopped_glue_job_turns_a_cancelling_run_cancelled(updater, executions):
    _seed(executions, "CANCELLING")

    updater.lambda_handler({"Records": [_record("run_pipeline", "cancelled")]}, None)

    row = _row(executions)
    assert row["status"] == "CANCELLED"
    assert row["steps_status"]["run_pipeline"] == "CANCELLED"
    assert "completed_at" in row


def test_a_job_that_finished_before_the_stop_landed_closes_the_run_as_a_success(updater, executions):
    _seed(executions, "CANCELLING")

    updater.lambda_handler({"Records": [_record("run_pipeline", "success")]}, None)

    assert _row(executions)["status"] == "SUCCEEDED"


def test_the_upsert_still_creates_a_row_that_does_not_exist(updater, executions):
    updater.lambda_handler({"Records": [_record("file_validation", "success", event_name="INSERT")]}, None)

    row = _row(executions)
    assert row["status"] == "RUNNING"
    assert row["started_at"] == "2026-10-11T09:00:00+00:00"


def test_cancelled_is_its_own_execution_status(updater):
    assert updater._derive_execution_status("run_pipeline", "cancelled") == "CANCELLED"
    assert updater._derive_execution_status("run_pipeline", "CANCELLED") == "CANCELLED"
