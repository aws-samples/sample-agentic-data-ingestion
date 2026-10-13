# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration tests: run-lifecycle races — fast-fail completion + missing-record retry (P28 R5, task 6.4).

Cross-Lambda contract under test (tasks 6.1 + 6.2 together). Both handlers are
the REAL Lambdas (run_pipeline, job_completion) loaded by path and run against
shared moto fixtures — moto DynamoDB tables (with the real sparse
GlueJobRunIndex GSI) and a real moto Glue job. The ONLY mocked boundary is
job_completion's Step Functions client: moto's send_task_success/failure
cannot target an execution actually parked on a waitForTaskToken task, so the
send attempt and its token are asserted instead (same boundary choice as the
6.1/6.2 unit tests).

    run_pipeline (REAL handler)
        put_item   (task-tokens table: approval_id "glue_run#<run_id>" carries
                    the task token — never the state row, which agents read)
        put_item   (RUNNING state row — NO task_token, NO glue_job_run_id;
                    absent from the sparse GlueJobRunIndex GSI)
      → start_job_run   (REAL moto Glue — produces the real JobRunId)
      → update_item (attach glue_job_run_id → record becomes GSI-findable)

    job_completion (REAL handler, same moto tables) receives the Glue
    EventBridge event ({detail: {jobName, jobRunId, state}}):
      - record findable (post-update): metrics collected via moto get_job_run,
        run state row updated, task callback sent with the token run_pipeline
        persisted in the task-tokens table, then that token row deleted
      - record NOT findable (pre-update window, or no record at all): raise
        RecordNotFoundError — a FUNCTION ERROR, so EventBridge redelivers with
        backoff; the redelivery lands after the update and succeeds. Events
        that never match dead-letter via the rule's DLQ (CDK-asserted in
        task 6.3, not re-tested here).

Validates: Requirements 5.6
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest

pytestmark = pytest.mark.integration

# run_pipeline resolves the account id at import time (env var → STS); pin the
# env var so the import never needs a (moto) STS round-trip.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_ROOT = _PROJECT_ROOT / "lambda"
_SHARED_DIR = _PROJECT_ROOT / "shared"

RUN_ID = "run-2026-01-01-00-00-00-itest6x4"
PIPELINE_ID = "customer-pipeline"
GLUE_JOB_NAME = "agentic-data-ingestion-customer-pipeline"
EXECUTION_NAME = "exec-itest-run-lifecycle-races"
# Realistic Step Functions task-token length (~600-700 chars)
TASK_TOKEN = "AAAAKgAAAAIAAAAAAAAAA" + "x" * 640

# Canonical step name (P28 R8.2, task 14.2): run_pipeline keys its state
# row with StepName.RUN_PIPELINE — the legacy "RunPipeline" variant is retired.
STATE_KEY = {"run_id": RUN_ID, "step_name": "run_pipeline"}
# run_pipeline's Glue-callback token row in the task-tokens table.
TOKEN_KEY = {"approval_id": f"glue_run#{RUN_ID}"}
TOKENS_TABLE = "agentic_data_ingestion_task_tokens"


def _load_lambda_module(lambda_dir_name: str, alias: str):
    """Load a Lambda's lambda_function.py under a unique module alias.

    Always called INSIDE a test (after mock_lambda_env set the canonical
    table-name env vars and the moto context from the shared fixtures is
    active) so import-time env reads and boto3 client creation resolve
    against the mocked environment.
    """
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))

    sys.modules.pop(alias, None)
    spec = importlib.util.spec_from_file_location(
        alias,
        str(_LAMBDA_ROOT / lambda_dir_name / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _seed_pipeline_and_glue_job(mock_dynamodb_tables) -> None:
    """Seed the pipeline record (glue_job_metadata) and create the moto Glue job."""
    mock_dynamodb_tables["agentic_data_ingestion_pipelines"].put_item(
        Item={
            "pipeline_id": PIPELINE_ID,
            "glue_job_metadata": {"glue_job_name": GLUE_JOB_NAME},
        }
    )
    boto3.client("glue", region_name="us-east-1").create_job(
        Name=GLUE_JOB_NAME,
        Role="arn:aws:iam::123456789012:role/itest-glue-role",
        Command={"Name": "glueetl", "ScriptLocation": "s3://agentic-data-ingestion-generated-code/script.py"},
    )


def _run_pipeline_event() -> dict:
    """The input the RunPipeline task state passes (waitForTaskToken pattern)."""
    return {
        "pipeline_id": PIPELINE_ID,
        "run_id": RUN_ID,
        "taskToken": TASK_TOKEN,
        "execution_arn": EXECUTION_NAME,
    }


def _completion_event(job_run_id: str, state: str) -> dict:
    """The Glue EventBridge event shape the GlueJobMonitorRule delivers."""
    return {
        "source": "aws.glue",
        "detail-type": "Glue Job State Change",
        "detail": {"jobName": GLUE_JOB_NAME, "jobRunId": job_run_id, "state": state},
    }


class TestFastFailRace:
    """Completion events arriving immediately after start are never lost.

    Validates: Requirements 5.6
    """

    def test_completion_immediately_after_start_matches_and_sends_callback(self, mock_dynamodb_tables, mock_lambda_env):
        """Fast-fail race, post-update side: the 6.1 ordering makes an
        immediately-arriving completion event matchable.

        Real run_pipeline persists the token record and attaches the real
        moto JobRunId; the completion event (simulated as arriving the moment
        the handler returns) is matched via the GlueJobRunIndex GSI and the
        task callback is sent with the token run_pipeline persisted.
        """
        _seed_pipeline_and_glue_job(mock_dynamodb_tables)
        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        tokens_table = mock_dynamodb_tables[TOKENS_TABLE]

        rp_module = _load_lambda_module("agentic_data_ingestion_run_pipeline", "itest6x4_run_pipeline_fast")
        rp_module.lambda_handler(_run_pipeline_event(), MagicMock())

        # What run_pipeline left behind: the REAL job run id on the state row,
        # and the token in the task-tokens table — never on the state row.
        record = state_table.get_item(Key=STATE_KEY)["Item"]
        assert "task_token" not in record
        assert tokens_table.get_item(Key=TOKEN_KEY)["Item"]["task_token"] == TASK_TOKEN
        job_run_id = record["glue_job_run_id"]
        assert job_run_id, "run_pipeline must attach the Glue job run id"

        # The completion event arrives IMMEDIATELY — same jobRunId Glue produced
        jc_module = _load_lambda_module("agentic_data_ingestion_job_completion", "itest6x4_job_completion_fast")
        mock_sfn = MagicMock()  # the only mocked boundary (see module docstring)
        with patch.object(jc_module, "sfn_client", mock_sfn):
            result = jc_module.lambda_handler(_completion_event(job_run_id, "SUCCEEDED"), MagicMock())

        assert result["statusCode"] == 200
        assert result["job_run_id"] == job_run_id
        assert result["pipeline_id"] == PIPELINE_ID

        # The callback carries exactly the token run_pipeline persisted (the
        # end-to-end 6.1+6.2 contract: token can never be lost post-start).
        mock_sfn.send_task_success.assert_called_once()
        assert mock_sfn.send_task_success.call_args.kwargs["taskToken"] == TASK_TOKEN
        mock_sfn.send_task_failure.assert_not_called()

        # Run state row updated with the completion outcome + metrics
        row = state_table.get_item(Key=STATE_KEY)["Item"]
        assert row["status"] == "success"
        assert row["completed_at"]
        assert row["output_data"]["job_run_id"] == job_run_id

        # The spent token is gone once the callback is delivered.
        assert tokens_table.get_item(Key=TOKEN_KEY).get("Item") is None

    def test_completion_beating_the_update_raises_then_redelivery_succeeds(self, mock_dynamodb_tables, mock_lambda_env):
        """Fast-fail race, pre-update window — 'EventBridge retry closes the race'.

        The completion event is delivered at the WORST possible instant:
        after start_job_run returns but before run_pipeline's update_item
        attaches glue_job_run_id. The state at that instant is produced by
        the real handler itself (token present, no glue_job_run_id → absent
        from the sparse GSI): job_completion raises RecordNotFoundError (a
        function error → EventBridge redelivery). Once run_pipeline's update
        lands, the redelivery of the SAME event matches and completes.

        The Glue module attribute on run_pipeline is wrapped by a delegate
        that calls the REAL moto start_job_run and then interleaves the
        completion delivery — a scheduling hook, not a stubbed client.
        """
        _seed_pipeline_and_glue_job(mock_dynamodb_tables)
        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
        tokens_table = mock_dynamodb_tables[TOKENS_TABLE]

        rp_module = _load_lambda_module("agentic_data_ingestion_run_pipeline", "itest6x4_run_pipeline_race")
        jc_module = _load_lambda_module("agentic_data_ingestion_job_completion", "itest6x4_job_completion_race")

        mock_sfn = MagicMock()
        first_delivery: dict = {}
        real_glue = rp_module.glue_client

        def start_then_deliver_completion(**kwargs):
            response = real_glue.start_job_run(**kwargs)  # REAL moto start
            job_run_id = response["JobRunId"]
            event = _completion_event(job_run_id, "FAILED")  # a fast-FAILING job

            # Evidence of the mid-flight state as the real handler left it:
            # the token row and the state row exist, the state row not yet
            # GSI-findable.
            first_delivery["mid_flight_record"] = state_table.get_item(Key=STATE_KEY).get("Item")
            first_delivery["mid_flight_token_row"] = tokens_table.get_item(Key=TOKEN_KEY).get("Item")

            # The completion event arrives BEFORE run_pipeline's update_item.
            try:
                with patch.object(jc_module, "sfn_client", mock_sfn):
                    outcome = ("returned", jc_module.lambda_handler(event, MagicMock()))
            except Exception as exc:  # captured for type assertion below
                outcome = ("raised", exc)
            first_delivery["outcome"] = outcome
            first_delivery["event"] = event
            first_delivery["job_run_id"] = job_run_id
            return response

        racing_glue = MagicMock()
        racing_glue.start_job_run.side_effect = start_then_deliver_completion
        with patch.object(rp_module, "glue_client", racing_glue):
            rp_module.lambda_handler(_run_pipeline_event(), MagicMock())

        # Mid-flight state: exactly what 6.1's ordering guarantees — token
        # persisted (in the task-tokens table), glue_job_run_id not yet
        # attached (sparse-GSI-invisible).
        mid_flight = first_delivery["mid_flight_record"]
        assert mid_flight is not None, "state row must exist before start_job_run returns"
        assert "task_token" not in mid_flight
        assert "glue_job_run_id" not in mid_flight
        mid_flight_token_row = first_delivery["mid_flight_token_row"]
        assert mid_flight_token_row is not None, "token row must exist before start_job_run returns"
        assert mid_flight_token_row["task_token"] == TASK_TOKEN

        # First delivery raised RecordNotFoundError → EventBridge will redeliver
        kind, value = first_delivery["outcome"]
        assert kind == "raised", f"first delivery must raise, got: {value!r}"
        assert isinstance(value, jc_module.RecordNotFoundError)

        # ... and left no side effects behind (no sends; row untouched)
        mock_sfn.send_task_success.assert_not_called()
        mock_sfn.send_task_failure.assert_not_called()

        # run_pipeline finished: the update landed, record is now GSI-findable
        record = state_table.get_item(Key=STATE_KEY)["Item"]
        assert record["glue_job_run_id"] == first_delivery["job_run_id"]
        assert record["status"] == "RUNNING"

        # EventBridge redelivery: the SAME event now matches and completes
        with patch.object(jc_module, "sfn_client", mock_sfn):
            result = jc_module.lambda_handler(first_delivery["event"], MagicMock())

        assert result["statusCode"] == 200
        mock_sfn.send_task_failure.assert_called_once()
        send_kwargs = mock_sfn.send_task_failure.call_args.kwargs
        assert send_kwargs["taskToken"] == TASK_TOKEN
        assert send_kwargs["error"] == "GlueJobFailed"

        row = state_table.get_item(Key=STATE_KEY)["Item"]
        assert row["status"] == "failed"
        assert row["completed_at"]

        # The raised first delivery left the token row in place for the
        # redelivery; the redelivery's confirmed callback removed it.
        assert tokens_table.get_item(Key=TOKEN_KEY).get("Item") is None


class TestMissingRecordRetryPath:
    """Unmatched completion events raise (redelivery) and never half-process.

    Validates: Requirements 5.6
    """

    def test_unmatched_event_raises_with_no_side_effects(self, mock_dynamodb_tables, mock_lambda_env):
        """A completion event with NO matching record raises RecordNotFoundError.

        The raise is a function error (EventBridge redelivers with backoff;
        exhaustion dead-letters to the 6.3 DLQ). No state writes and no task
        callbacks happen on this path.
        """
        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]

        jc_module = _load_lambda_module("agentic_data_ingestion_job_completion", "itest6x4_job_completion_missing")
        mock_sfn = MagicMock()

        with (
            patch.object(jc_module, "sfn_client", mock_sfn),
            pytest.raises(jc_module.RecordNotFoundError) as exc_info,
        ):
            jc_module.lambda_handler(_completion_event("jr-never-started", "SUCCEEDED"), MagicMock())

        # The message names the unmatched job for the retry/DLQ ops trail
        message = str(exc_info.value)
        assert GLUE_JOB_NAME in message
        assert "jr-never-started" in message

        # No side effects: no state writes, no sends
        assert state_table.scan()["Items"] == []
        mock_sfn.send_task_success.assert_not_called()
        mock_sfn.send_task_failure.assert_not_called()

    def test_redelivery_after_record_appears_processes_successfully(self, mock_dynamodb_tables, mock_lambda_env):
        """Redelivery: first delivery raises; the record appears; second succeeds.

        The record's shape is authentic — produced by the REAL run_pipeline
        handler (captured verbatim), removed to recreate the no-record-yet
        state for the first delivery, then restored to simulate the 6.1
        update landing before the redelivery.
        """
        _seed_pipeline_and_glue_job(mock_dynamodb_tables)
        state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]

        # Produce the authentic post-update record via the real handler
        rp_module = _load_lambda_module("agentic_data_ingestion_run_pipeline", "itest6x4_run_pipeline_redeliver")
        rp_module.lambda_handler(_run_pipeline_event(), MagicMock())
        authentic_record = state_table.get_item(Key=STATE_KEY)["Item"]
        job_run_id = authentic_record["glue_job_run_id"]

        # Recreate the no-record-yet state (event racing ahead of the writes)
        state_table.delete_item(Key=STATE_KEY)

        event = _completion_event(job_run_id, "SUCCEEDED")
        jc_module = _load_lambda_module("agentic_data_ingestion_job_completion", "itest6x4_job_completion_redeliver")
        mock_sfn = MagicMock()

        # First delivery: nothing to match → raise (EventBridge will redeliver)
        with (
            patch.object(jc_module, "sfn_client", mock_sfn),
            pytest.raises(jc_module.RecordNotFoundError),
        ):
            jc_module.lambda_handler(event, MagicMock())
        mock_sfn.send_task_success.assert_not_called()

        # The record appears — the 6.1 writes landing (restored verbatim)
        state_table.put_item(Item=authentic_record)

        # Second delivery, SAME event: processes successfully
        with patch.object(jc_module, "sfn_client", mock_sfn):
            result = jc_module.lambda_handler(event, MagicMock())

        assert result["statusCode"] == 200
        assert result["job_run_id"] == job_run_id
        mock_sfn.send_task_success.assert_called_once()
        assert mock_sfn.send_task_success.call_args.kwargs["taskToken"] == TASK_TOKEN

        # Run state update landed on the redelivery, and the spent token is gone
        row = state_table.get_item(Key=STATE_KEY)["Item"]
        assert row["status"] == "success"
        assert row["completed_at"]
        assert mock_dynamodb_tables[TOKENS_TABLE].get_item(Key=TOKEN_KEY).get("Item") is None

    def test_exception_type_name_is_stable(self, mock_lambda_env):
        """The exception type name is a stable ops contract.

        EventBridge retry/DLQ trails and ops docs/alarms (task 6.3) reference
        the function errorType by name — renaming it would orphan them.
        """
        jc_module = _load_lambda_module("agentic_data_ingestion_job_completion", "itest6x4_job_completion_name")

        assert jc_module.RecordNotFoundError.__name__ == "RecordNotFoundError"
        # A plain Exception subclass: raising it is what makes the invocation
        # a FUNCTION ERROR (EventBridge retries those, not error-shaped returns).
        assert issubclass(jc_module.RecordNotFoundError, Exception)
