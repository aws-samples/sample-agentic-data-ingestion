# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the new state table key structure (PK=run_id, SK=step_name).

Validates AC-DB.1: Query by run_id returns all steps for that run;
GetItem by run_id + step_name returns the specific step.

Uses moto to mock DynamoDB — no real AWS calls.
"""

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "agentic_data_ingestion_state"


def _create_state_table(dynamodb_resource):
    """Create the state table with PK=run_id, SK=step_name."""
    dynamodb_resource.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "step_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "step_name", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def state_table():
    """Set up mocked DynamoDB with the new state table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_state_table(ddb)
        table = ddb.Table(TABLE_NAME)
        yield table


# ── Seed data ───────────────────────────────────────────────────────

RUN_A = "run-2025-01-15-12-00-00-aaaa1111"
RUN_B = "run-2025-01-15-13-00-00-bbbb2222"

STEPS_RUN_A = [
    {"run_id": RUN_A, "step_name": "file_validation", "status": "completed", "pipeline_id": "pipe-abc"},
    {"run_id": RUN_A, "step_name": "schema_extract", "status": "completed", "pipeline_id": "pipe-abc"},
    {"run_id": RUN_A, "step_name": "code_generation", "status": "running", "pipeline_id": "pipe-abc"},
]

STEPS_RUN_B = [
    {"run_id": RUN_B, "step_name": "file_validation", "status": "completed", "pipeline_id": "pipe-xyz"},
    {"run_id": RUN_B, "step_name": "schema_extract", "status": "failed", "pipeline_id": "pipe-xyz"},
]


def _seed(table):
    """Write all seed items to the table."""
    with table.batch_writer() as batch:
        for item in STEPS_RUN_A + STEPS_RUN_B:
            batch.put_item(Item=item)


# ── Tests ───────────────────────────────────────────────────────────


class TestQueryByRunIdReturnsAllSteps:
    """Query by run_id returns all steps for that run."""

    def test_returns_all_steps_for_run(self, state_table):
        _seed(state_table)

        from boto3.dynamodb.conditions import Key

        response = state_table.query(
            KeyConditionExpression=Key("run_id").eq(RUN_A),
        )
        items = response["Items"]

        assert len(items) == 3
        step_names = {item["step_name"] for item in items}
        assert step_names == {"file_validation", "schema_extract", "code_generation"}


class TestGetItemByRunIdAndStepName:
    """GetItem by run_id + step_name returns the specific step."""

    def test_returns_specific_step(self, state_table):
        _seed(state_table)

        response = state_table.get_item(
            Key={"run_id": RUN_A, "step_name": "schema_extract"},
        )
        item = response["Item"]

        assert item["run_id"] == RUN_A
        assert item["step_name"] == "schema_extract"
        assert item["status"] == "completed"


class TestQueryByRunIdIsolatesRuns:
    """Query by run_id with different runs returns only the correct run's steps."""

    def test_different_runs_isolated(self, state_table):
        _seed(state_table)

        from boto3.dynamodb.conditions import Key

        response_a = state_table.query(
            KeyConditionExpression=Key("run_id").eq(RUN_A),
        )
        response_b = state_table.query(
            KeyConditionExpression=Key("run_id").eq(RUN_B),
        )

        assert len(response_a["Items"]) == 3
        assert len(response_b["Items"]) == 2

        # No cross-contamination
        run_ids_a = {item["run_id"] for item in response_a["Items"]}
        run_ids_b = {item["run_id"] for item in response_b["Items"]}
        assert run_ids_a == {RUN_A}
        assert run_ids_b == {RUN_B}


class TestPipelineIdIsRegularAttribute:
    """Items have pipeline_id as a regular attribute (not the PK)."""

    def test_pipeline_id_stored_as_attribute(self, state_table):
        _seed(state_table)

        response = state_table.get_item(
            Key={"run_id": RUN_A, "step_name": "file_validation"},
        )
        item = response["Item"]

        assert item["pipeline_id"] == "pipe-abc"
        # pipeline_id is NOT part of the key — run_id is the PK
        assert item["run_id"] == RUN_A
