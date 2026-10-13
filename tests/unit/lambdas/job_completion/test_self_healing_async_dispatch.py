# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion dispatches self-healing asynchronously (P28 R9.3).

The old pattern — a daemon thread doing a SYNCHRONOUS AgentCore invoke from
inside job_completion — was broken twice over: Lambda freezes the execution
environment the moment the handler returns (killing the thread mid-call), and
boto3's default 60s read timeout could never survive the agent's ~10-minute
workload. The fixed behavior:

    FAILED completion → lambda.invoke(FunctionName=SELF_HEALING_FN,
                                      InvocationType="Event", Payload=...)
      → job_completion returns immediately (fire-and-forget)
        → the dispatch Lambda owns the long synchronous AgentCore call

Dispatch is best-effort: the Step Functions callback has already succeeded,
so a failed DISPATCH logs and continues — it never fails job_completion.

Validates: P28 Requirements 9.3
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

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

_DISPATCH_FN = "agentic_data_ingestion_self_healing_dispatch"


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


@pytest.fixture(autouse=True)
def _self_healing_fn_env(monkeypatch):
    monkeypatch.setenv("SELF_HEALING_FN", _DISPATCH_FN)
    yield


def _event(state="FAILED"):
    return {"detail": {"jobName": "agentic-data-ingestion-pipe-1", "jobRunId": "jr-1", "state": state}}


def _token_record(**overrides):
    record = {
        "run_id": "run-1",
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
    table.get_item.return_value = {"Item": {"approval_id": "glue_run#run-1", "task_token": "token-abc"}}
    return table


# Real-dict Glue response so collect_job_metrics computes real numbers.
def _glue_job_run(state="FAILED", error_message="OutOfMemoryError: container killed"):
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


def _standard_mocks(items, glue_response=None):
    mock_runs_table = MagicMock()
    mock_runs_table.query.return_value = {"Items": items}
    mock_glue = MagicMock()
    mock_glue.get_job_run.return_value = glue_response or _glue_job_run()
    mock_pipelines_table = MagicMock()
    mock_pipelines_table.get_item.return_value = {}
    mock_sfn = MagicMock()
    return mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn


def _run_handler(mock_runs_table, mock_glue, mock_pipelines_table, mock_sfn, mock_lambda, event):
    with (
        patch.object(_mod, "pipeline_runs_table", mock_runs_table),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "glue_client", mock_glue),
        patch.object(_mod, "pipelines_table", mock_pipelines_table),
        patch.object(_mod, "sfn_client", mock_sfn),
        patch.object(_mod, "lambda_client", mock_lambda),
    ):
        return _mod.lambda_handler(event, None)


def test_failed_state_dispatches_async_event_invoke_with_payload():
    """FAILED completion → async Lambda invoke (InvocationType=Event) of
    SELF_HEALING_FN carrying the full self-healing payload."""
    mocks = _standard_mocks(items=[_token_record()])
    mock_lambda = MagicMock()

    result = _run_handler(*mocks, mock_lambda, _event("FAILED"))

    assert result["statusCode"] == 200
    mock_lambda.invoke.assert_called_once()
    kwargs = mock_lambda.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == _DISPATCH_FN
    assert kwargs["InvocationType"] == "Event"
    payload = json.loads(kwargs["Payload"])
    assert payload == {
        "pipeline_id": "pipe-1",
        "run_id": "run-1",
        "job_name": "agentic-data-ingestion-pipe-1",
        "job_run_id": "jr-1",
        "error_message": "OutOfMemoryError: container killed",
        "state": "FAILED",
    }


def test_failed_state_dispatch_happens_after_task_failure_callback():
    """The SFN callback (the run's source of truth) is sent before the
    best-effort healing dispatch."""
    mocks = _standard_mocks(items=[_token_record()])
    mock_sfn = mocks[3]
    mock_lambda = MagicMock()
    call_order = []
    mock_sfn.send_task_failure.side_effect = lambda **kw: call_order.append("send_task_failure")
    mock_lambda.invoke.side_effect = lambda **kw: call_order.append("dispatch")

    _run_handler(*mocks, mock_lambda, _event("FAILED"))

    assert call_order == ["send_task_failure", "dispatch"]


def test_succeeded_state_does_not_dispatch():
    """SUCCEEDED completion → no self-healing dispatch."""
    mocks = _standard_mocks(items=[_token_record()], glue_response=_glue_job_run("SUCCEEDED", ""))
    mock_lambda = MagicMock()

    result = _run_handler(*mocks, mock_lambda, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    mock_lambda.invoke.assert_not_called()


def test_dispatch_failure_does_not_fail_job_completion():
    """A failed DISPATCH logs and continues — the completion callback already
    succeeded, so job_completion still returns 200 (healing is best-effort)."""
    mocks = _standard_mocks(items=[_token_record()])
    mock_sfn = mocks[3]
    mock_lambda = MagicMock()
    mock_lambda.invoke.side_effect = Exception("Lambda service unavailable")

    result = _run_handler(*mocks, mock_lambda, _event("FAILED"))

    assert result["statusCode"] == 200
    assert "FAILED" in result["message"]
    mock_sfn.send_task_failure.assert_called_once()


def test_no_dispatch_when_fn_not_configured(monkeypatch):
    """Empty SELF_HEALING_FN (stack deployed without the dispatcher) → no
    dispatch attempt, normal completion processing."""
    monkeypatch.setenv("SELF_HEALING_FN", "")
    mocks = _standard_mocks(items=[_token_record()])
    mock_lambda = MagicMock()

    result = _run_handler(*mocks, mock_lambda, _event("FAILED"))

    assert result["statusCode"] == 200
    mock_lambda.invoke.assert_not_called()


def test_daemon_thread_pattern_removed_from_source():
    """The daemon-thread + synchronous-invoke pattern is deleted: no
    threading import/usage and no in-handler AgentCore invoke remain (P28
    R9.3 — 'the daemon-thread + synchronous-invoke pattern SHALL be
    removed')."""
    source_path = os.path.join(_job_completion_dir, "lambda_function.py")
    with open(source_path) as f:
        source = f.read()

    assert "threading" not in source
    assert "invoke_agentcore_runtime" not in source
    assert "SELF_HEALING_AGENT_ENDPOINT" not in source  # replaced by SELF_HEALING_FN
    assert 'InvocationType="Event"' in source
