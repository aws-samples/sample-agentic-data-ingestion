# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the PipelineIndex GSI on the state table.

Validates AC-DB.2: Query PipelineIndex by pipeline_id returns all runs
for that pipeline, sorted by started_at.

Uses moto to mock DynamoDB — no real AWS calls.
"""

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

TABLE_NAME = "agentic_data_ingestion_state"
GSI_NAME = "PipelineIndex"


def _create_state_table_with_gsi(dynamodb_resource):
    """Create the state table with PipelineIndex GSI."""
    dynamodb_resource.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "step_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "step_name", "AttributeType": "S"},
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": GSI_NAME,
                "KeySchema": [
                    {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def state_table():
    """Set up mocked DynamoDB with the state table + PipelineIndex GSI."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_state_table_with_gsi(ddb)
        table = ddb.Table(TABLE_NAME)
        yield table


# ── Seed data ───────────────────────────────────────────────────────

PIPE_A = "pipe-abc"
PIPE_B = "pipe-xyz"

ITEMS = [
    {"run_id": "run-001", "step_name": "file_validation", "pipeline_id": PIPE_A, "started_at": "2025-01-15T12:00:00Z"},
    {"run_id": "run-002", "step_name": "file_validation", "pipeline_id": PIPE_A, "started_at": "2025-01-15T14:00:00Z"},
    {"run_id": "run-003", "step_name": "file_validation", "pipeline_id": PIPE_A, "started_at": "2025-01-15T10:00:00Z"},
    {"run_id": "run-004", "step_name": "file_validation", "pipeline_id": PIPE_B, "started_at": "2025-01-15T11:00:00Z"},
    {"run_id": "run-005", "step_name": "file_validation", "pipeline_id": PIPE_B, "started_at": "2025-01-15T09:00:00Z"},
]


def _seed(table):
    with table.batch_writer() as batch:
        for item in ITEMS:
            batch.put_item(Item=item)


# ── Tests ───────────────────────────────────────────────────────────


class TestQueryPipelineIndexReturnsAllRuns:
    """Query PipelineIndex by pipeline_id returns all runs for that pipeline."""

    def test_returns_all_runs_for_pipeline(self, state_table):
        _seed(state_table)

        response = state_table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("pipeline_id").eq(PIPE_A),
        )
        items = response["Items"]

        assert len(items) == 3
        run_ids = {item["run_id"] for item in items}
        assert run_ids == {"run-001", "run-002", "run-003"}


class TestPipelineIndexSortedByStartedAt:
    """Results are sorted by started_at ascending by default."""

    def test_ascending_sort_order(self, state_table):
        _seed(state_table)

        response = state_table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("pipeline_id").eq(PIPE_A),
            ScanIndexForward=True,
        )
        timestamps = [item["started_at"] for item in response["Items"]]

        assert timestamps == [
            "2025-01-15T10:00:00Z",
            "2025-01-15T12:00:00Z",
            "2025-01-15T14:00:00Z",
        ]

    def test_descending_sort_order(self, state_table):
        _seed(state_table)

        response = state_table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("pipeline_id").eq(PIPE_A),
            ScanIndexForward=False,
        )
        timestamps = [item["started_at"] for item in response["Items"]]

        assert timestamps == [
            "2025-01-15T14:00:00Z",
            "2025-01-15T12:00:00Z",
            "2025-01-15T10:00:00Z",
        ]


class TestPipelineIndexIsolatesPipelines:
    """Multiple pipelines don't interfere with each other."""

    def test_pipelines_isolated(self, state_table):
        _seed(state_table)

        response_a = state_table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("pipeline_id").eq(PIPE_A),
        )
        response_b = state_table.query(
            IndexName=GSI_NAME,
            KeyConditionExpression=Key("pipeline_id").eq(PIPE_B),
        )

        assert len(response_a["Items"]) == 3
        assert len(response_b["Items"]) == 2

        pipeline_ids_a = {item["pipeline_id"] for item in response_a["Items"]}
        pipeline_ids_b = {item["pipeline_id"] for item in response_b["Items"]}
        assert pipeline_ids_a == {PIPE_A}
        assert pipeline_ids_b == {PIPE_B}
