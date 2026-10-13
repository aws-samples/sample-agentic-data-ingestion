# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: dashboard API response time budget (P29).

For any set of 20 execution items, get_pipeline_executions returns
within 500ms (moto) or 2000ms (real AWS).

**Validates: Requirements 19.2**
"""

import time

import boto3
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

_execution_count_strategy = st.integers(min_value=10, max_value=25)


def _create_tables(dynamodb_resource):
    """Create minimal tables for the response time test."""
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_executions",
        KeySchema=[
            {"AttributeName": "pipeline_id", "KeyType": "HASH"},
            {"AttributeName": "run_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "run_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@given(count=_execution_count_strategy)
@settings(
    max_examples=10,
    # This test has its OWN explicit 500ms budget assertion below. Hypothesis's
    # 200ms default per-example deadline (which includes moto setup overhead) is
    # both redundant and environment-sensitive, so disable it to avoid flakes.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_query_response_time_within_budget(count):
    """For any execution count, DynamoDB query returns within time budget.

    **Validates: Requirements 19.2**

    Tests the raw DynamoDB query performance (without handler overhead)
    to establish a baseline. The full handler test is in test_n_plus_one.py.
    """
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(dynamodb)

        pipeline_id = f"pipeline-time-{count}"
        table = dynamodb.Table("agentic_data_ingestion_executions")

        for i in range(count):
            table.put_item(
                Item={
                    "pipeline_id": pipeline_id,
                    "run_id": f"run-{i:04d}",
                    "status": "SUCCEEDED",
                    "started_at": f"2024-01-{15 + (i % 15):02d}T10:00:00Z",
                }
            )

        start = time.monotonic()
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("pipeline_id").eq(pipeline_id),
            ScanIndexForward=False,
            Limit=20,
        )
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 500, (
            f"DynamoDB query took {elapsed_ms:.0f}ms for {count} items (budget: 500ms in moto mode)"
        )
        assert len(response.get("Items", [])) > 0
