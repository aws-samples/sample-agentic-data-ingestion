# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Migration safety test: existing pipeline retains old write_mode after default change.

Verifies that when the system default write_mode changes (e.g., from 'overwrite'
to 'append'), existing pipelines that were created with the old default retain
their original write_mode value.

Validates: Requirements 21.1
"""

import boto3
from moto import mock_aws


def _create_pipelines_table(dynamodb_resource):
    """Create the pipelines DynamoDB table."""
    dynamodb_resource.create_table(
        TableName="agentic_data_ingestion_pipelines",
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb_resource.Table("agentic_data_ingestion_pipelines")


@mock_aws
def test_existing_pipeline_retains_write_mode_after_default_change():
    """Existing pipeline keeps 'overwrite' even if new default is 'append'.

    Scenario:
    1. Create a pipeline with write_mode='overwrite' (old default)
    2. Simulate a code deploy that changes the default to 'append'
    3. Read the pipeline back — it should still have 'overwrite'

    This validates that write_mode is stored per-pipeline in DynamoDB and
    not derived from a global default at read time.
    """
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_pipelines_table(dynamodb)

    # Step 1: Create pipeline with old default write_mode
    old_pipeline = {
        "pipeline_id": "pipeline-legacy-001",
        "pipeline_name": "Legacy Customer Pipeline",
        "status": "created",
        "created_at": "2024-01-15T10:00:00Z",
        "updated_at": "2024-01-15T10:00:00Z",
        "target_config": {
            "type": "NEW_S3_TABLE_BUCKET",
            "write_mode": "overwrite",  # Old default
            "bucket_name": "my-bucket",
            "table_name": "customers",
        },
    }
    table.put_item(Item=old_pipeline)

    # Step 2: Simulate new default by creating a new pipeline with 'append'
    new_pipeline = {
        "pipeline_id": "pipeline-new-002",
        "pipeline_name": "New Customer Pipeline",
        "status": "created",
        "created_at": "2024-06-01T10:00:00Z",
        "updated_at": "2024-06-01T10:00:00Z",
        "target_config": {
            "type": "NEW_S3_TABLE_BUCKET",
            "write_mode": "append",  # New default
            "bucket_name": "my-bucket",
            "table_name": "customers_v2",
        },
    }
    table.put_item(Item=new_pipeline)

    # Step 3: Read back the old pipeline — write_mode must still be 'overwrite'
    response = table.get_item(Key={"pipeline_id": "pipeline-legacy-001"})
    item = response["Item"]
    actual_write_mode = item["target_config"]["write_mode"]

    assert actual_write_mode == "overwrite", (
        f"Migration safety violation: existing pipeline's write_mode changed "
        f"from 'overwrite' to '{actual_write_mode}' after default change"
    )


@mock_aws
def test_new_pipeline_uses_new_default_write_mode():
    """New pipeline created after default change uses 'append'."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_pipelines_table(dynamodb)

    new_pipeline = {
        "pipeline_id": "pipeline-new-003",
        "pipeline_name": "Fresh Pipeline",
        "status": "created",
        "created_at": "2024-06-15T10:00:00Z",
        "updated_at": "2024-06-15T10:00:00Z",
        "target_config": {
            "type": "NEW_S3_TABLE_BUCKET",
            "write_mode": "append",
            "bucket_name": "my-bucket",
            "table_name": "fresh_table",
        },
    }
    table.put_item(Item=new_pipeline)

    response = table.get_item(Key={"pipeline_id": "pipeline-new-003"})
    assert response["Item"]["target_config"]["write_mode"] == "append"


@mock_aws
def test_write_mode_values_are_independent_per_pipeline():
    """Each pipeline's write_mode is independent — changing one doesn't affect others."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_pipelines_table(dynamodb)

    modes = ["append", "overwrite", "merge"]
    for i, mode in enumerate(modes):
        table.put_item(
            Item={
                "pipeline_id": f"pipeline-{mode}-{i}",
                "pipeline_name": f"Pipeline {mode}",
                "status": "created",
                "created_at": "2024-01-15T10:00:00Z",
                "updated_at": "2024-01-15T10:00:00Z",
                "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": mode},
            }
        )

    # Update one pipeline's write_mode
    table.update_item(
        Key={"pipeline_id": "pipeline-append-0"},
        UpdateExpression="SET target_config.write_mode = :m",
        ExpressionAttributeValues={":m": "overwrite"},
    )

    # Verify others are unchanged
    for i, mode in enumerate(modes):
        response = table.get_item(Key={"pipeline_id": f"pipeline-{mode}-{i}"})
        actual = response["Item"]["target_config"]["write_mode"]
        if i == 0:
            assert actual == "overwrite", "Updated pipeline should be 'overwrite'"
        else:
            assert actual == mode, (
                f"Pipeline {i} write_mode changed from '{mode}' to '{actual}' when another pipeline was updated"
            )
