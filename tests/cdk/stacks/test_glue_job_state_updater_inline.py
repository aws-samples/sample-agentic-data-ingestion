# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Glue job state updater never creates a pipeline record, and maps versioned jobs.

``GlueJobStateUpdateFunction`` is an inline Lambda in
``deploy-cdk/lib/orchestration-stack.ts``. It derived the pipeline id by
stripping the job-name prefix and wrote it with an unconditional UpdateItem,
which creates a missing item. So a late event for a deleted pipeline recreated
a partial record, and a versioned job (``...-job-p1-v2``,
``schema_evolution.create_versioned_job_name``) was recorded under ``p1-v2``.
The external review reproduced both against the real handler.

This module extracts the handler from the stack source and runs it against
moto DynamoDB, Glue and CloudWatch, so the condition is evaluated by
DynamoDB's own UpdateItem semantics and the job lookup by a real job
definition. Expectations come from the requirement: an event never creates a
pipeline, and a job's events land on the pipeline that owns it.
"""

from __future__ import annotations

import types
from pathlib import Path

import boto3
import pytest

STACK_SOURCE = Path(__file__).resolve().parents[3] / "deploy-cdk" / "lib" / "orchestration-stack.ts"
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
JOB_PREFIX = "agentic-data-ingestion-job-"
PIPELINE_ID = "orders-pipeline-1a2b3c4d"
ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-orders-ExecutionRole"


def _handler_source() -> str:
    """The Python the stack deploys, taken from the template literal verbatim."""
    text = STACK_SOURCE.read_text(encoding="utf-8")
    start = text.index("new lambda.Function(this, 'GlueJobStateUpdateFunction'")
    code_start = text.index("inlineLambdaCode(`", start) + len("inlineLambdaCode(`")
    code_end = text.index("`)", code_start)
    code = text[code_start:code_end]
    # A TypeScript template literal rewrites both of these before the code is
    # deployed, so source containing either is not the code that runs.
    assert "${" not in code, "the inline handler contains a dollar-brace, which TypeScript interpolates"
    assert "\\" not in code, "the inline handler contains a backslash, which a template literal unescapes"
    return code


@pytest.fixture
def updater(mock_dynamodb_tables, monkeypatch):
    """The handler module, loaded inside the moto context the table fixture opened."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("PIPELINES_TABLE", PIPELINES_TABLE)
    monkeypatch.setenv("ENVIRONMENT", "test")
    # The rest of the function's deployed environment block.
    monkeypatch.setenv("STATE_TABLE_NAME", "agentic_data_ingestion_state")
    module = types.ModuleType("glue_job_state_updater_inline")
    exec(compile(_handler_source(), "orchestration-stack.ts:GlueJobStateUpdateFunction", "exec"), module.__dict__)
    return module


@pytest.fixture
def pipelines(mock_dynamodb_tables):
    return mock_dynamodb_tables[PIPELINES_TABLE]


def _create_job(name: str, pipeline_id: str | None) -> None:
    default_args = {"--pipeline-id": pipeline_id} if pipeline_id else {}
    boto3.client("glue", region_name="us-east-1").create_job(
        Name=name,
        Role=ROLE_ARN,
        Command={"Name": "glueetl", "ScriptLocation": "s3://code/job.py"},
        DefaultArguments=default_args,
    )


def _event(job_name: str, state: str = "SUCCEEDED") -> dict:
    return {"detail": {"jobName": job_name, "state": state, "jobRunId": "jr_0001"}}


def _published_pipeline_ids() -> set[str]:
    metrics = boto3.client("cloudwatch", region_name="us-east-1").list_metrics(
        Namespace="AgenticDataIngestion/Pipeline"
    )["Metrics"]
    return {d["Value"] for m in metrics for d in m["Dimensions"] if d["Name"] == "PipelineId"}


def test_an_event_updates_the_pipeline_that_owns_the_job(updater, pipelines):
    pipelines.put_item(Item={"pipeline_id": PIPELINE_ID, "pipeline_name": "orders", "status": "active"})
    _create_job(f"{JOB_PREFIX}{PIPELINE_ID}", PIPELINE_ID)

    updater.lambda_handler(_event(f"{JOB_PREFIX}{PIPELINE_ID}"), None)

    item = pipelines.get_item(Key={"pipeline_id": PIPELINE_ID})["Item"]
    assert item["glue_job_last_status"] == "SUCCEEDED"
    assert item["glue_job_last_run_id"] == "jr_0001"
    assert item["pipeline_name"] == "orders", "the update must not replace the record"


def test_a_versioned_job_resolves_to_its_pipeline_not_to_its_suffix(updater, pipelines):
    pipelines.put_item(Item={"pipeline_id": PIPELINE_ID, "pipeline_name": "orders"})
    versioned = f"{JOB_PREFIX}{PIPELINE_ID}-v2"
    _create_job(versioned, PIPELINE_ID)

    updater.lambda_handler(_event(versioned, "FAILED"), None)

    assert pipelines.get_item(Key={"pipeline_id": PIPELINE_ID})["Item"]["glue_job_last_status"] == "FAILED"
    assert "Item" not in pipelines.get_item(Key={"pipeline_id": f"{PIPELINE_ID}-v2"})
    assert _published_pipeline_ids() == {PIPELINE_ID}, "the metric must carry the owning pipeline too"


def test_a_late_event_for_a_deleted_pipeline_creates_nothing(updater, pipelines):
    """A versioned job survives a pipeline delete (only the base job is
    deleted), so its events keep arriving after the record is gone."""
    versioned = f"{JOB_PREFIX}{PIPELINE_ID}-v3"
    _create_job(versioned, PIPELINE_ID)

    result = updater.lambda_handler(_event(versioned), None)

    assert result == {"statusCode": 200}, "a late event is acknowledged, not retried"
    assert pipelines.scan()["Items"] == []


def test_an_unreadable_job_falls_back_to_its_name_and_still_creates_nothing(updater, pipelines):
    """No job definition to read (deleted with its pipeline): the name is the
    only clue, and a wrong guess must write nothing."""
    updater.lambda_handler(_event(f"{JOB_PREFIX}{PIPELINE_ID}-v4"), None)

    assert pipelines.scan()["Items"] == []


def test_events_for_other_jobs_are_skipped(updater, pipelines):
    assert updater.lambda_handler(_event("someone-elses-job"), None)["body"] == "Skipped"
    assert pipelines.scan()["Items"] == []
