# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: GET /api/files/{pipeline_key}/{file_key}/details never returns a task token.

``get_file_details`` returns each matching state row's remaining attributes as
the step's ``details``. A ``task_token`` on a state row is a Step Functions
callback token: whoever holds it can report a result for that step. run_pipeline
no longer writes one there (it lives in the task-tokens table), but rows written
before that change still carry it and are never rewritten, so the handler drops
it — and the other spelling, ``taskToken`` — on the way out. Everything else in
``details`` is unchanged: the deny set is deliberately not an allowlist.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from handlers import files

PIPELINE_KEY = "orders-pipe-1a2b3c4d"
FILE_KEY = "orders.csv"
SOURCE_URI = f"s3://adi-source-123456789012-us-east-1/uploads/{FILE_KEY}"


def _legacy_run_pipeline_row(**extra):
    """A run_pipeline state row as the pre-change run_pipeline wrote it."""
    row = {
        "run_id": "run-legacy-1",
        "step_name": "run_pipeline",
        "status": "success",
        "timestamp": "2026-09-01T10:00:00+00:00",
        "pipeline_id": PIPELINE_KEY,
        "original_pipeline_id": PIPELINE_KEY,
        "original_s3_uri": SOURCE_URI,
        "glue_job_name": "agentic-data-ingestion-orders",
        "glue_job_run_id": "jr_legacy",
        "task_token": "AAAAKgAAAAIAAAAAAAAAAQ-live-callback-token",
        "output_data": {"glue_job_name": "agentic-data-ingestion-orders", "status": "RUNNING"},
    }
    row.update(extra)
    return row


def _details(rows):
    state_table = MagicMock(name="state_table")
    state_table.scan.return_value = {"Items": rows}
    with patch.object(files, "state_table", state_table):
        response = files.get_file_details(PIPELINE_KEY, FILE_KEY)
    assert response["statusCode"] == 200, response
    return response, json.loads(response["body"])


def test_task_token_is_absent_from_the_response():
    response, body = _details([_legacy_run_pipeline_row()])

    (step,) = body["steps"]
    assert "task_token" not in step["details"]
    assert "task_token" not in step
    # Not anywhere in the serialized body either — not nested, not renamed.
    assert "live-callback-token" not in response["body"]


def test_camel_case_spelling_is_dropped_too():
    response, body = _details([_legacy_run_pipeline_row(taskToken="AAAA-camel-live-callback-token")])

    (step,) = body["steps"]
    assert "taskToken" not in step["details"]
    assert "live-callback-token" not in response["body"]


def test_every_other_attribute_still_reaches_details():
    """The deny set removes the token and nothing else."""
    _, body = _details([_legacy_run_pipeline_row(added_by_a_later_writer="kept")])

    (step,) = body["steps"]
    assert step["step_name"] == "run_pipeline"
    assert step["status"] == "success"
    assert step["run_id"] == "run-legacy-1"
    assert set(step["details"]) == {
        "pipeline_id",
        "original_pipeline_id",
        "original_s3_uri",
        "glue_job_name",
        "glue_job_run_id",
        "output_data",
        "added_by_a_later_writer",
    }


def test_against_a_real_state_table_row(mock_dynamodb_tables):
    """The same through moto: a legacy row stored in the state table, scanned
    by the handler's own FilterExpression, never yields its token."""
    state_table = mock_dynamodb_tables["agentic_data_ingestion_state"]
    state_table.put_item(Item=_legacy_run_pipeline_row())
    state_table.put_item(
        Item={
            "run_id": "run-legacy-1",
            "step_name": "file_validation",
            "status": "success",
            "timestamp": "2026-09-01T09:59:00+00:00",
            "pipeline_id": PIPELINE_KEY,
            "original_pipeline_id": PIPELINE_KEY,
            "original_s3_uri": SOURCE_URI,
        }
    )

    with patch.object(files, "state_table", state_table):
        response = files.get_file_details(PIPELINE_KEY, FILE_KEY)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert [s["step_name"] for s in body["steps"]] == ["file_validation", "run_pipeline"]
    assert "live-callback-token" not in response["body"]
