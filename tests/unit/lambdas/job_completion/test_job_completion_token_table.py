# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion reads the Glue-callback token from the task-tokens table.

run_pipeline stores the RunPipeline task token in the task-tokens table under
``approval_id = "glue_run#<run_id>"``, not on the state row: AgentCore agents
read the state table and the dashboard returns state rows to the browser,
while the AgentCore runtime role is denied every action on the task-tokens
table (``DenyApprovalTaskTokens``, LGR-8f59603-3, whose recorded residual was
exactly this token). job_completion therefore:

1. finds the run through the GlueJobRunIndex GSI (unchanged — it gives run_id);
2. reads the token row by ``glue_run#<run_id>``;
3. falls back to a ``task_token`` on the state row only when no token row
   exists — runs in flight at deploy time, written by the old run_pipeline;
4. deletes the token row after a SendTask* call succeeds (best effort), and
   leaves the consumed-token handling exactly as it was;
5. answers a duplicate delivery that arrives after that delete — no token
   left, state row already terminal — with the same clean return the
   consumed-token path gives, before any metrics, writes or publish.

Error paths live here too (this directory has no ``test_error_paths.py``).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

# Add Lambda dir and shared dir to path
_LAMBDA_ROOT = Path(__file__).resolve().parents[4] / "lambda"
_job_completion_dir = str(_LAMBDA_ROOT / "agentic_data_ingestion_job_completion")
sys.path.insert(0, _job_completion_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


RUN_ID = "run-tok-1"
PIPELINE_ID = "pipe-tok"
JOB_NAME = "agentic-data-ingestion-pipe-tok"
JOB_RUN_ID = "jr-tok-1"
TOKEN = "token-from-table"
LEGACY_TOKEN = "token-on-state-row"
TOKEN_KEY = {"approval_id": f"glue_run#{RUN_ID}"}

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


def _event(state="SUCCEEDED"):
    return {"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": state}}


def _state_row(**overrides):
    """The run_pipeline state row the GSI lookup finds (new shape: no token)."""
    row = {
        "run_id": RUN_ID,
        "step_name": "run_pipeline",
        "original_pipeline_id": PIPELINE_ID,
        "glue_job_run_id": JOB_RUN_ID,
        "status": "RUNNING",
    }
    row.update(overrides)
    return row


def _tokens_table(task_token=TOKEN):
    """A task-tokens table holding the run's row (``None``: no row)."""
    table = MagicMock(name="task_tokens_table")
    table.get_item.return_value = (
        {"Item": {**TOKEN_KEY, "task_token": task_token, "run_id": RUN_ID}} if task_token else {}
    )
    return table


class _Harness:
    """The handler's collaborators, all mocks, for one invocation."""

    def __init__(self, state_row=None, tokens=None):
        self.runs = MagicMock(name="pipeline_runs_table")
        self.runs.query.return_value = {"Items": [state_row if state_row is not None else _state_row()]}
        self.runs.get_item.return_value = {}
        self.tokens = tokens if tokens is not None else _tokens_table()
        self.glue = MagicMock(name="glue")
        self.glue.get_job_run.return_value = _GLUE_JOB_RUN
        self.pipelines = MagicMock(name="pipelines_table")
        self.pipelines.get_item.return_value = {}
        self.sfn = MagicMock(name="sfn")
        self.lambda_client = MagicMock(name="lambda")
        self.publish = MagicMock(name="publish_glue_run_cost")

    def run(self, event):
        with (
            patch.object(_mod, "pipeline_runs_table", self.runs),
            patch.object(_mod, "task_tokens_table", self.tokens),
            patch.object(_mod, "glue_client", self.glue),
            patch.object(_mod, "pipelines_table", self.pipelines),
            patch.object(_mod, "sfn_client", self.sfn),
            patch.object(_mod, "lambda_client", self.lambda_client),
            patch.object(_mod, "publish_glue_run_cost", self.publish),
            patch.dict(os.environ, {"SELF_HEALING_FN": ""}, clear=False),
        ):
            return _mod.lambda_handler(event, None)


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


# ---------------------------------------------------------------------------
# The key — one literal in two Lambdas
# ---------------------------------------------------------------------------
def test_token_key_matches_run_pipelines():
    """run_pipeline writes and job_completion reads the same key.

    The two Lambdas share no module for it (each defines the prefix), so the
    agreement is asserted by loading both — a drift here means every run's
    callback misses its token and strands for the full task timeout.
    """
    spec = importlib.util.spec_from_file_location(
        "run_pipeline_for_token_key_pin",
        str(_LAMBDA_ROOT / "agentic_data_ingestion_run_pipeline" / "lambda_function.py"),
    )
    run_pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_pipeline)

    assert run_pipeline.GLUE_RUN_TOKEN_KEY_PREFIX == _mod.GLUE_RUN_TOKEN_KEY_PREFIX == "glue_run#"
    assert run_pipeline.glue_run_token_key(RUN_ID) == _mod.glue_run_token_key(RUN_ID) == f"glue_run#{RUN_ID}"


# ---------------------------------------------------------------------------
# The token row is the source
# ---------------------------------------------------------------------------
def test_success_reads_the_token_row_and_sends_its_token():
    h = _Harness()

    result = h.run(_event("SUCCEEDED"))

    assert result["statusCode"] == 200
    h.tokens.get_item.assert_called_once_with(Key=TOKEN_KEY, ConsistentRead=True)
    h.sfn.send_task_success.assert_called_once()
    assert h.sfn.send_task_success.call_args.kwargs["taskToken"] == TOKEN


def test_token_row_is_deleted_after_a_successful_task_success():
    h = _Harness()

    h.run(_event("SUCCEEDED"))

    h.tokens.delete_item.assert_called_once_with(Key=TOKEN_KEY)


def test_token_row_is_deleted_after_a_successful_task_failure():
    h = _Harness()

    result = h.run(_event("FAILED"))

    assert result["statusCode"] == 200
    assert h.sfn.send_task_failure.call_args.kwargs["taskToken"] == TOKEN
    h.tokens.delete_item.assert_called_once_with(Key=TOKEN_KEY)


def test_the_delete_follows_the_send():
    """Deleting first would lose the token if the send then failed."""
    order = []
    h = _Harness()
    h.sfn.send_task_success.side_effect = lambda **kw: order.append("send")
    h.tokens.delete_item.side_effect = lambda **kw: order.append("delete")

    h.run(_event("SUCCEEDED"))

    assert order == ["send", "delete"]


def test_token_row_wins_over_a_legacy_state_row_token():
    h = _Harness(state_row=_state_row(task_token=LEGACY_TOKEN))

    h.run(_event("SUCCEEDED"))

    assert h.sfn.send_task_success.call_args.kwargs["taskToken"] == TOKEN


# ---------------------------------------------------------------------------
# The fallback for runs in flight at deploy time
# ---------------------------------------------------------------------------
def test_no_token_row_falls_back_to_the_state_row_token():
    """A run started by the old run_pipeline has its token on the state row only."""
    h = _Harness(state_row=_state_row(task_token=LEGACY_TOKEN), tokens=_tokens_table(task_token=None))

    result = h.run(_event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert h.sfn.send_task_success.call_args.kwargs["taskToken"] == LEGACY_TOKEN
    # There is no token row to delete.
    h.tokens.delete_item.assert_not_called()


def test_no_token_anywhere_on_a_running_row_raises():
    """Not a duplicate (the row is still RUNNING), so it must fail the
    invocation: Lambda retries it and then sends it to the DLQ, which pages. The
    duplicate-delivery case below, on a terminal row, still returns cleanly."""
    h = _Harness(tokens=_tokens_table(task_token=None))

    with pytest.raises(_mod.TaskTokenMissingError):
        h.run(_event("SUCCEEDED"))

    h.sfn.send_task_success.assert_not_called()
    h.runs.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Duplicate deliveries
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("terminal_status", ["success", "failed"])
def test_duplicate_after_the_delete_terminates_cleanly_before_any_side_effect(terminal_status):
    """The first delivery sent the callback, set the row terminal and deleted
    the token row. The duplicate finds no token: it returns the consumed-token
    shape and does nothing else — no metrics, no state write, no send, no
    self-healing dispatch, no cost datapoint."""
    h = _Harness(state_row=_state_row(status=terminal_status), tokens=_tokens_table(task_token=None))

    result = h.run(_event("SUCCEEDED" if terminal_status == "success" else "FAILED"))

    assert result["statusCode"] == 200
    assert "already consumed" in result["message"]
    assert result["job_run_id"] == JOB_RUN_ID
    assert result["pipeline_id"] == PIPELINE_ID
    h.glue.get_job_run.assert_not_called()
    h.runs.update_item.assert_not_called()
    h.sfn.send_task_success.assert_not_called()
    h.sfn.send_task_failure.assert_not_called()
    h.lambda_client.invoke.assert_not_called()
    h.publish.assert_not_called()


@pytest.mark.parametrize("error_code", ["TaskTimedOut", "TaskDoesNotExist", "InvalidToken"])
def test_consumed_token_behaviour_is_unchanged_and_keeps_the_row(error_code):
    """A send rejected as consumed still returns cleanly (P28 R5.2). The row is
    not deleted on that path — it is left to its TTL, as before this change."""
    h = _Harness()
    h.sfn.send_task_success.side_effect = _client_error(error_code, "SendTaskSuccess")

    result = h.run(_event("SUCCEEDED"))

    assert result["statusCode"] == 200
    assert "already consumed" in result["message"]
    h.tokens.delete_item.assert_not_called()
    h.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------
def test_other_send_errors_raise_and_keep_the_row():
    """A send that failed for any other reason did not deliver the callback, so
    the token must survive it, and the invocation must FAIL so Lambda's async
    retry sends it again. A returned 500-shaped dict is a successful
    asynchronous invocation, which is how a throttled callback could strand a
    run until its task timeout."""
    h = _Harness()
    h.sfn.send_task_success.side_effect = _client_error("ThrottlingException", "SendTaskSuccess")

    with pytest.raises(ClientError, match="ThrottlingException"):
        h.run(_event("SUCCEEDED"))

    h.tokens.delete_item.assert_not_called()


def test_a_failed_delete_does_not_change_the_outcome():
    """The callback was delivered; a leftover spent token is housekeeping (TTL)."""
    h = _Harness()
    h.tokens.delete_item.side_effect = _client_error("ProvisionedThroughputExceededException", "DeleteItem")

    result = h.run(_event("SUCCEEDED"))

    assert result["statusCode"] == 200
    h.sfn.send_task_success.assert_called_once()
    h.publish.assert_called_once()


def test_a_token_table_read_error_is_not_answered_by_the_fallback():
    """A broken token-table read must surface, not be papered over by a legacy
    state-row token: nothing is sent, and the invocation fails so it is retried."""
    tokens = MagicMock(name="task_tokens_table")
    tokens.get_item.side_effect = _client_error("AccessDeniedException", "GetItem")
    h = _Harness(state_row=_state_row(task_token=LEGACY_TOKEN), tokens=tokens)

    with pytest.raises(ClientError, match="AccessDeniedException"):
        h.run(_event("SUCCEEDED"))

    h.sfn.send_task_success.assert_not_called()
    h.runs.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Against moto: the real tables, the real GSI, several deliveries
# ---------------------------------------------------------------------------
def test_end_to_end_one_callback_one_publish_and_no_token_left(mock_dynamodb_tables):
    """Seeded with the shapes run_pipeline writes, three deliveries of the same
    SUCCEEDED event produce one callback and one cost datapoint, and leave no
    token row behind. The second and third deliveries take the
    duplicate-after-delete return."""
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    tokens_table = mock_dynamodb_tables["agentic_data_ingestion_task_tokens"]
    pipelines_table = mock_dynamodb_tables["agentic_data_ingestion_pipelines"]
    state_table.put_item(Item=_state_row())
    tokens_table.put_item(Item={**TOKEN_KEY, "task_token": TOKEN, "run_id": RUN_ID, "pipeline_id": PIPELINE_ID})

    glue = MagicMock(name="glue")
    glue.get_job_run.return_value = _GLUE_JOB_RUN
    sfn = MagicMock(name="sfn")
    publish = MagicMock(name="publish_glue_run_cost")
    results = []

    with (
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(_mod, "task_tokens_table", tokens_table),
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock(name="lambda")),
        patch.object(_mod, "publish_glue_run_cost", publish),
        # The three data-outcome writers read tables this test does not seed;
        # they are not its subject.
        patch.object(_mod, "_link_heal_rerun_outcome", MagicMock()),
        patch.object(_mod, "_commit_target_schema_version", MagicMock()),
        patch.object(_mod, "_record_processed_file", MagicMock()),
        patch.object(_mod, "_populate_mirror_metadata_location", MagicMock()),
        patch.dict(os.environ, {"SELF_HEALING_FN": ""}, clear=False),
    ):
        for _ in range(3):
            results.append(_mod.lambda_handler(_event("SUCCEEDED"), None))

    assert [r["statusCode"] for r in results] == [200, 200, 200]
    assert all("already consumed" in r["message"] for r in results[1:])
    sfn.send_task_success.assert_called_once()
    assert sfn.send_task_success.call_args.kwargs["taskToken"] == TOKEN
    publish.assert_called_once()

    assert tokens_table.get_item(Key=TOKEN_KEY).get("Item") is None
    row = state_table.get_item(Key={"run_id": RUN_ID, "step_name": "run_pipeline"})["Item"]
    assert row["status"] == "success"
    assert "task_token" not in row
