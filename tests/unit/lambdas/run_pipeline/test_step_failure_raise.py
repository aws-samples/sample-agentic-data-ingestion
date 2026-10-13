# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: run_pipeline failure path raises StepFailure (P28 R1.1).

The handler must raise (so the Step Functions task fails and Retry/Catch
fire) instead of swallowing failures. Genuinely retryable Glue start_job_run
errors (concurrency limits, throttling) raise TransientStepFailure so the
state machine's Retry policy can back off and retry.

Validates: P28 Requirements 1.1
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


def test_missing_pipeline_raises_permanent_step_failure():
    """A failing handler raises StepFailure — never returns an error payload."""
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}  # pipeline not found

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", MagicMock()),
        patch.object(_mod, "task_tokens_table", MagicMock()),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    exc = exc_info.value
    assert exc.step_name == "run_pipeline"
    assert exc.error_type == "permanent"
    assert "not found" in exc.message
    assert exc.details["run_id"] == "run-1"


def test_concurrent_runs_exceeded_raises_transient_step_failure():
    """Glue concurrency-limit errors are retryable → TransientStepFailure."""
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    mock_glue = MagicMock()
    mock_glue.start_job_run.side_effect = ClientError(
        {"Error": {"Code": "ConcurrentRunsExceededException", "Message": "Too many concurrent runs"}},
        "StartJobRun",
    )

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", MagicMock()),
        patch.object(_mod, "task_tokens_table", MagicMock()),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.TransientStepFailure) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    assert exc_info.value.error_type == "transient"
    assert exc_info.value.step_name == "run_pipeline"


def test_non_retryable_glue_error_raises_permanent_step_failure():
    """Non-retryable Glue errors (e.g. EntityNotFound) stay permanent."""
    mock_pipelines_table = MagicMock()
    _seed_pipeline(mock_pipelines_table)

    mock_glue = MagicMock()
    mock_glue.start_job_run.side_effect = ClientError(
        {"Error": {"Code": "EntityNotFoundException", "Message": "Job not found"}},
        "StartJobRun",
    )

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", MagicMock()),
        patch.object(_mod, "task_tokens_table", MagicMock()),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "sfn_client", MagicMock()),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.lambda_handler(_event(), None)

    assert exc_info.value.error_type == "permanent"
    assert not isinstance(exc_info.value, _mod.TransientStepFailure)


def test_failure_still_sends_task_failure_callback():
    """The failure path keeps its side effect: send_task_failure on the token."""
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}  # pipeline not found
    mock_sfn = MagicMock()

    with (
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "pipeline_runs_table", MagicMock()),
        patch.object(_mod, "task_tokens_table", MagicMock()),
        patch.object(_mod, "sfn_client", mock_sfn),
        pytest.raises(_mod.StepFailure),
    ):
        _mod.lambda_handler(_event(), None)

    mock_sfn.send_task_failure.assert_called_once()
    assert mock_sfn.send_task_failure.call_args.kwargs["taskToken"] == "token-abc"
