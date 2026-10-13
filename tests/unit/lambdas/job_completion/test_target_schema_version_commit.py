# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion commits target_schema_version only on success (p30 R8.4).

R8.4 requires that ``target_schema_version`` is not advanced before the run
completes, and is committed only after a **successful** one, so a failure leaves
the remap re-triggerable. The write therefore had to move out of
``schema_compare`` — which decides at step 3 of 9 — and into this handler, the
only place in the system that knows the target write actually landed. This file
covers that downstream half;
``tests/unit/lambdas/schema_compare/test_target_schema_version_deferred.py``
covers the upstream half (the detecting step performs no write, and reports the
version it observed on its state row).

**Why the Glue callback is the anchor.** ``RunPipeline`` is the last substantive
state in the machine: only ``ReleaseLock`` and a state update follow it
(``orchestration-stack.ts:718-719``), and ``CreateQualityChecks`` runs *before*
the job (``:712``, ``:715``). So Glue SUCCEEDED means the target write landed,
and no later failure should re-open the remap — the data is in the target, and
remapping it again would be wrong.

The commit is best-effort, matching ``_link_heal_rerun_outcome``: the Step
Functions callback is the run's source of truth and has already been sent by the
time it runs, so a failure to record the version must never fail job_completion.

Validates: p30 Requirements 8.4, 8.5 (version not advanced on failure)
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


RUN_ID = "run-r84-1"
PIPELINE_ID = "pipe-r84"
OBSERVED_VERSION = "sha256-of-target-schema"


def _event(state="SUCCEEDED"):
    return {"detail": {"jobName": "agentic-data-ingestion-pipe-r84", "jobRunId": "jr-r84", "state": state}}


def _token_record():
    return {
        "run_id": RUN_ID,
        "step_name": "run_pipeline",
        "original_pipeline_id": PIPELINE_ID,
        "glue_job_run_id": "jr-r84",
        "status": "RUNNING",
    }


def _task_tokens_table():
    """The run's Glue-callback token row — run_pipeline stores it in the
    task-tokens table, never on the state row."""
    table = MagicMock(name="task_tokens_table")
    table.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-r84"}}
    return table


def _glue_job_run(state="SUCCEEDED"):
    return {
        "JobRun": {
            "JobRunState": state,
            "ExecutionTime": 60,
            "NumberOfWorkers": 2,
            "WorkerType": "G.1X",
            "MaxCapacity": 2.0,
            "GlueVersion": "5.0",
            "ErrorMessage": "",
        }
    }


def _mocks(state="SUCCEEDED", compare_row=None, fv_row=None):
    """Wire the handler's four AWS clients.

    ``get_item`` dispatches on ``step_name`` because two best-effort helpers read
    different rows of the same table: ``_link_heal_rerun_outcome`` reads
    ``file_validation`` and ``_commit_target_schema_version`` reads
    ``schema_compare``.
    """
    if compare_row is None:
        compare_row = {
            "run_id": RUN_ID,
            "step_name": "schema_compare",
            "target_schema_new_version": OBSERVED_VERSION,
        }
    if fv_row is None:
        # A normal (non-heal) run, so the heal linker returns early.
        fv_row = {"run_id": RUN_ID, "step_name": "file_validation", "triggered_by": "s3_upload"}

    rows = {"file_validation": fv_row, "schema_compare": compare_row}

    runs_table = MagicMock()
    runs_table.query.return_value = {"Items": [_token_record()]}
    runs_table.get_item.side_effect = lambda Key: (
        {"Item": rows[Key["step_name"]]} if rows.get(Key["step_name"]) is not None else {}
    )

    glue = MagicMock()
    glue.get_job_run.return_value = _glue_job_run(state)

    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {}

    return runs_table, glue, pipelines_table, MagicMock()


def _run_handler(runs_table, glue, pipelines_table, sfn, event):
    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock()),
    ):
        return _mod.lambda_handler(event, None)


def _version_commits(pipelines_table):
    return [
        c.kwargs
        for c in pipelines_table.update_item.call_args_list
        if "target_schema_version" in c.kwargs.get("UpdateExpression", "")
    ]


# ── Committed on success ──────────────────────────────────────────────


def test_successful_run_commits_the_observed_version():
    """**Validates: Requirement 8.4**"""
    mocks = _mocks("SUCCEEDED")
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    commits = _version_commits(mocks[2])
    assert len(commits) == 1
    commit = commits[0]
    assert commit["Key"] == {"pipeline_id": PIPELINE_ID}
    assert commit["ExpressionAttributeValues"][":v"] == OBSERVED_VERSION
    assert commit["ExpressionAttributeValues"][":ts"]
    assert "target_schema_last_checked" in commit["UpdateExpression"]


def test_commit_is_update_only_no_phantom_pipeline_rows():
    """An in-flight run whose pipeline was deleted must not resurrect it as a stub.

    **Validates: Requirement 8.4**
    """
    mocks = _mocks("SUCCEEDED")
    _run_handler(*mocks, _event("SUCCEEDED"))

    assert "attribute_exists" in _version_commits(mocks[2])[0]["ConditionExpression"]


# ── NOT committed on any non-success terminal state ───────────────────


@pytest.mark.parametrize("state", ["FAILED", "STOPPED", "TIMEOUT"])
def test_unsuccessful_run_does_not_advance_the_version(state):
    """R8.5's named case. Leaving the stored version behind is exactly what
    keeps the remap re-triggerable on the next run: that run's
    ``check_target_schema_changed`` sees the same change again.

    **Validates: Requirements 8.4, 8.5**
    """
    mocks = _mocks(state)
    result = _run_handler(*mocks, _event(state))

    assert result["statusCode"] == 200
    assert _version_commits(mocks[2]) == []


# ── Nothing to commit ─────────────────────────────────────────────────


def test_run_without_a_schema_compare_row_commits_nothing():
    """A retry that started after schema_compare has no row for this run_id.

    **Validates: Requirement 8.4**
    """
    mocks = _mocks("SUCCEEDED", compare_row={})
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert _version_commits(mocks[2]) == []


def test_pipeline_with_no_target_schema_object_commits_nothing():
    """``check_target_schema_changed`` reports no version when the pipeline has
    no ``target_schema_s3_key``, so the state row carries none either.

    **Validates: Requirement 8.4**
    """
    mocks = _mocks(
        "SUCCEEDED",
        compare_row={"run_id": RUN_ID, "step_name": "schema_compare", "target_schema_changed": False},
    )
    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert _version_commits(mocks[2]) == []


# ── Best-effort: a commit failure never fails job_completion ──────────


def test_deleted_pipeline_conditional_failure_is_swallowed():
    """**Validates: Requirement 8.4**"""
    mocks = _mocks("SUCCEEDED")
    mocks[2].update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "gone"}}, "UpdateItem"
    )

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_commit_error_is_swallowed():
    """**Validates: Requirement 8.4**"""
    mocks = _mocks("SUCCEEDED")
    mocks[2].update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}}, "UpdateItem"
    )

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_state_row_read_failure_is_swallowed():
    """**Validates: Requirement 8.4**"""
    mocks = _mocks("SUCCEEDED")
    mocks[0].get_item.side_effect = RuntimeError("dynamo down")

    result = _run_handler(*mocks, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_callback_is_sent_before_the_version_commit():
    """The run's source of truth precedes the best-effort bookkeeping, so a
    commit failure can never affect the run's outcome.

    **Validates: Requirement 8.4**
    """
    mocks = _mocks("SUCCEEDED")
    order: list[str] = []
    mocks[3].send_task_success.side_effect = lambda **kw: order.append("callback")
    mocks[2].update_item.side_effect = lambda **kw: order.append("commit") or {}

    _run_handler(*mocks, _event("SUCCEEDED"))

    assert order == ["callback", "commit"]
