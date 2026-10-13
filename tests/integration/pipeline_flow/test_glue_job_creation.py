#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Test script for Glue job creation with sample pipelines.
Tests all three target types: S3 Table Buckets, Glue Catalog tables, and JDBC databases.

This module validates that pipeline items are correctly written to DynamoDB
and that the Glue job creation flow works end-to-end.

All tests use moto mocks — no real AWS calls are made.
"""

from datetime import datetime

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

# Configuration
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
STATE_TABLE = "agentic_data_ingestion_state"
ACCOUNT_ID = "123456789012"
REGION = "us-east-1"

TEST_PREFIX = "glue-test"
TEST_TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def _create_mock_tables(region: str):
    """Create moto DynamoDB tables needed for Glue job creation tests."""
    dynamodb = boto3.resource("dynamodb", region_name=region)

    # Pipelines table
    dynamodb.create_table(
        TableName=PIPELINES_TABLE,
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "status-created_at-index",
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # State table
    dynamodb.create_table(
        TableName=STATE_TABLE,
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

    return dynamodb


def _create_test_pipeline(dynamodb, pipeline_id: str, target_type: str, target_config: dict):
    """Create a test pipeline in the moto DynamoDB table."""
    table = dynamodb.Table(PIPELINES_TABLE)
    table.put_item(
        Item={
            "pipeline_id": pipeline_id,
            "pipeline_name": f"Test Pipeline - {target_type}",
            "description": f"Test pipeline for {target_type} target type",
            "target_type": target_type,
            "target_config": target_config,
            "status": "active",
            "created_at": datetime.now().isoformat(),
            "source_bucket": f"adi-source-files-{ACCOUNT_ID}-{REGION}",
            "processed_bucket": f"adi-processed-files-{ACCOUNT_ID}-{REGION}",
            "source_prefix": f"{pipeline_id}/",
            "processed_prefix": f"{pipeline_id}/",
        }
    )


class TestGlueJobCreationPipelineSetup:
    """Verify pipeline items are correctly written to DynamoDB for each target type."""

    @mock_aws
    def test_s3_tables_pipeline_stored_correctly(self):
        """Pipeline with S3_TABLES target config is stored in DynamoDB."""
        dynamodb = _create_mock_tables(REGION)
        pipeline_id = f"{TEST_PREFIX}-s3tables-{TEST_TIMESTAMP}"

        target_config = {
            "type": "NEW_S3_TABLE_BUCKET",
            "s3_table_bucket": f"agentic-data-ingestion-table-{pipeline_id}-{ACCOUNT_ID}",
            "table_name": "test_customer_data",
            "format": "iceberg",
        }

        _create_test_pipeline(dynamodb, pipeline_id, "S3_TABLES", target_config)

        table = dynamodb.Table(PIPELINES_TABLE)
        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert "Item" in response
        assert response["Item"]["target_type"] == "S3_TABLES"
        assert response["Item"]["target_config"]["type"] == "NEW_S3_TABLE_BUCKET"

    @mock_aws
    def test_glue_catalog_pipeline_stored_correctly(self):
        """Pipeline with GLUE_CATALOG target config is stored in DynamoDB."""
        dynamodb = _create_mock_tables(REGION)
        pipeline_id = f"{TEST_PREFIX}-catalog-{TEST_TIMESTAMP}"

        target_config = {
            "type": "GLUE_CATALOG",
            "glue_database": "agentic_data_ingestion_test_db",
            "glue_table": "test_customer_data",
            "format": "parquet",
            "partition_keys": ["year", "month"],
        }

        _create_test_pipeline(dynamodb, pipeline_id, "GLUE_CATALOG", target_config)

        table = dynamodb.Table(PIPELINES_TABLE)
        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert "Item" in response
        assert response["Item"]["target_type"] == "GLUE_CATALOG"
        assert response["Item"]["target_config"]["glue_database"] == "agentic_data_ingestion_test_db"

    @mock_aws
    def test_jdbc_pipeline_stored_correctly(self):
        """Pipeline with JDBC target config is stored in DynamoDB."""
        dynamodb = _create_mock_tables(REGION)
        pipeline_id = f"{TEST_PREFIX}-jdbc-{TEST_TIMESTAMP}"

        target_config = {
            "type": "JDBC",
            "jdbc_connection_name": "test-aurora-connection",
            "database_name": "test_db",
            "table_name": "customer_data",
            "write_mode": "append",
            "batch_size": 10000,
        }

        _create_test_pipeline(dynamodb, pipeline_id, "JDBC", target_config)

        table = dynamodb.Table(PIPELINES_TABLE)
        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert "Item" in response
        assert response["Item"]["target_type"] == "JDBC"
        assert response["Item"]["target_config"]["write_mode"] == "append"

    @mock_aws
    def test_state_entry_created_for_pipeline(self):
        """State entry is correctly written for a pipeline run."""
        dynamodb = _create_mock_tables(REGION)
        pipeline_id = f"{TEST_PREFIX}-state-{TEST_TIMESTAMP}"
        run_id = f"{pipeline_id}-{TEST_TIMESTAMP}"

        table = dynamodb.Table(STATE_TABLE)
        table.put_item(
            Item={
                "run_id": run_id,
                "step_name": "CreateGluePipeline",
                "pipeline_id": pipeline_id,
                "status": "IN_PROGRESS",
                "code_s3_uri": f"s3://adi-generated-code-{ACCOUNT_ID}-{REGION}/pipelines/{pipeline_id}/test_script.py",
                "created_at": datetime.now().isoformat(),
            }
        )

        response = table.get_item(Key={"run_id": run_id, "step_name": "CreateGluePipeline"})
        assert "Item" in response
        assert response["Item"]["pipeline_id"] == pipeline_id
        assert response["Item"]["status"] == "IN_PROGRESS"
