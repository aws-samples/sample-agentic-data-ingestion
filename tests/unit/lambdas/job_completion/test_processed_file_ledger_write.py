# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: job_completion records the Processed_File_Ledger row only on success (p30 R9.1).

R9.2 skips a trigger whose content "already has a **successful** run". That word
puts the write here and nowhere else: a row written at trigger time would mark a
file processed before anything had processed it, and a run that then failed would
leave that file permanently un-retriable. Same defect shape, same direction, as
the one R8.4 fixed for ``target_schema_version`` — so this write reuses that
task's anchor (the Glue SUCCEEDED callback) rather than inventing a second notion
of "successful".

``RunPipeline`` is the last substantive state in the machine (only
``ReleaseLock`` and a state update follow it,
``orchestration-stack.ts:718-719``), so SUCCEEDED means the target write landed
and there is no later step whose failure should re-open the file.

The fingerprint is carried on the run's own ``file_validation`` identity row,
written by the Lambda that HEADs the object for its size check — see
``tests/unit/lambdas/file_validation/test_content_fingerprint_capture.py`` for
the upstream half, and
``tests/unit/lambdas/pipeline_trigger/test_duplicate_content_skip.py`` for the
reader.

Validates: Requirements 9.1, 9.2
"""

import os
import sys
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


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


RUN_ID = "run-r91-1"
PIPELINE_ID = "orders"
FINGERPRINT = "a" * 64
SOURCE_URI = "s3://src-bucket/orders/data.csv"


def _event(state="SUCCEEDED"):
    return {"detail": {"jobName": "agentic-data-ingestion-orders", "jobRunId": "jr-r91", "state": state}}


def _token_record():
    return {
        "run_id": RUN_ID,
        "step_name": "run_pipeline",
        "original_pipeline_id": PIPELINE_ID,
        "glue_job_run_id": "jr-r91",
        "status": "RUNNING",
    }


def _task_tokens_table():
    """The run's Glue-callback token row — run_pipeline stores it in the
    task-tokens table, never on the state row."""
    table = MagicMock(name="task_tokens_table")
    table.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-r91"}}
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


def _mocks(state="SUCCEEDED", fv_row=..., processed_table=None):
    """Wire the handler's clients.

    ``get_item`` dispatches on ``step_name``: three best-effort helpers now read
    rows of the same table — the heal linker and this ledger writer both read
    ``file_validation``, and the schema-version commit reads ``schema_compare``.
    """
    if fv_row is ...:
        fv_row = {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "triggered_by": "s3_upload",
            "original_s3_uri": SOURCE_URI,
            "content_fingerprint": FINGERPRINT,
        }
    rows = {"file_validation": fv_row, "schema_compare": None}

    runs_table = MagicMock()
    runs_table.query.return_value = {"Items": [_token_record()]}
    runs_table.get_item.side_effect = lambda Key: (
        {"Item": rows[Key["step_name"]]} if rows.get(Key["step_name"]) is not None else {}
    )

    glue = MagicMock()
    glue.get_job_run.return_value = _glue_job_run(state)

    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {}

    processed = processed_table or MagicMock()
    return runs_table, glue, pipelines_table, MagicMock(), processed


def _run_handler(runs_table, glue, pipelines_table, sfn, processed, event):
    with (
        patch.object(_mod, "pipeline_runs_table", runs_table),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "pipelines_table", pipelines_table),
        patch.object(_mod, "processed_files_table", processed),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock()),
    ):
        return _mod.lambda_handler(event, None)


# ── Recorded on success ───────────────────────────────────────────────


def test_successful_run_records_the_fingerprint():
    """**Validates: Requirement 9.1**"""
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")

    result = _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    processed.put_item.assert_called_once()
    item = processed.put_item.call_args.kwargs["Item"]
    assert item["pipeline_id"] == PIPELINE_ID
    assert item["fingerprint"] == FINGERPRINT
    assert item["run_id"] == RUN_ID


def test_row_is_keyed_by_pipeline_and_fingerprint():
    """R9.1: keyed by pipeline PLUS fingerprint — the same content submitted to
    two pipelines is two legitimate ingestions, so the pipeline is part of the
    key, and the trigger's keyed lookup must find exactly this shape."""
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")

    _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    item = processed.put_item.call_args.kwargs["Item"]
    assert set(["pipeline_id", "fingerprint"]).issubset(item.keys())


def test_row_carries_a_ttl():
    """R9.4: the ledger has a TTL, so a ledger that grows forever is not a cost
    problem and a duplicate is not blocked indefinitely."""
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")

    _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert processed.put_item.call_args.kwargs["Item"]["ttl"] > 0


def test_row_carries_the_source_key_for_diagnosis():
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")

    _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert processed.put_item.call_args.kwargs["Item"]["source_key"] == SOURCE_URI


# ── NOT recorded on anything but success ──────────────────────────────


@pytest.mark.parametrize("state", ["FAILED", "STOPPED", "TIMEOUT"])
def test_non_success_terminal_states_record_nothing(state):
    """The clause that makes a retry possible.

    Parametrized over ALL three non-success terminal states rather than FAILED
    alone: a run that was stopped or timed out has processed no more of the file
    than one that failed, and marking its content processed would block the
    retry just as permanently.
    """
    runs, glue, pipelines, sfn, processed = _mocks(state)

    _run_handler(runs, glue, pipelines, sfn, processed, _event(state))

    processed.put_item.assert_not_called()


def test_no_fingerprint_on_the_row_records_nothing():
    """JDBC runs, unsupported-format files, ETag-less objects, and runs that
    started before the field existed. Deliberately not an error."""
    runs, glue, pipelines, sfn, processed = _mocks(
        "SUCCEEDED",
        fv_row={"run_id": RUN_ID, "step_name": "file_validation", "triggered_by": "manual"},
    )

    result = _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    processed.put_item.assert_not_called()


def test_missing_file_validation_row_records_nothing():
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED", fv_row=None)

    result = _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert result["statusCode"] == 200
    processed.put_item.assert_not_called()


# ── Best-effort: never fails the handler ──────────────────────────────


def test_ledger_write_failure_still_returns_200():
    """The Step Functions callback is the run's source of truth and has already
    been sent. A bookkeeping failure must not fail job_completion."""
    processed = MagicMock()
    processed.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "throttled"}}, "PutItem"
    )
    runs, glue, pipelines, sfn, _ = _mocks("SUCCEEDED", processed_table=processed)

    result = _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_state_row_read_failure_still_returns_200():
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")
    runs.get_item.side_effect = RuntimeError("read failed")

    result = _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert result["statusCode"] == 200


def test_callback_is_sent_before_the_ledger_row_is_written():
    """Ordering, asserted rather than assumed: if the ledger write ever moved
    ahead of the callback, a throttle on a bookkeeping table could strand the
    Step Functions execution."""
    order: list[str] = []
    runs, glue, pipelines, sfn, processed = _mocks("SUCCEEDED")
    sfn.send_task_success.side_effect = lambda **_: order.append("callback")
    processed.put_item.side_effect = lambda **_: order.append("ledger")

    _run_handler(runs, glue, pipelines, sfn, processed, _event("SUCCEEDED"))

    assert order == ["callback", "ledger"]
