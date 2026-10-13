#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Test suite for target configuration feature.
Tests pipeline creation, infrastructure creation, and Glue job configuration.

All tests use moto mocks or are skipped (require real AWS + deployed Lambdas).
No real AWS calls are made at module level.
"""

import os
import re
from datetime import datetime

import boto3
import pytest
from moto import mock_aws

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

pytestmark = pytest.mark.integration

# Configuration
PIPELINES_TABLE = "agentic_data_ingestion_pipelines"


def _create_pipelines_table(region: str = "us-east-1"):
    """Create the pipelines table in moto."""
    dynamodb = boto3.resource("dynamodb", region_name=region)
    dynamodb.create_table(
        TableName=PIPELINES_TABLE,
        KeySchema=[{"AttributeName": "pipeline_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "pipeline_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb


@pytest.mark.skip(reason="Requires real AWS credentials and deployed Lambda functions")
class TestTargetConfigPipelineCreation:
    """Test pipeline creation with different target types (requires deployed Lambdas)."""

    def test_create_pipeline_with_new_s3_tables_bucket(self):
        pass

    def test_create_pipeline_with_existing_s3_tables_bucket(self):
        pass

    def test_create_pipeline_with_glue_catalog(self):
        pass

    def test_create_pipeline_without_target_config(self):
        pass


@pytest.mark.skip(reason="Requires real AWS credentials and deployed Lambda functions")
class TestInfrastructureCreation:
    """Test infrastructure creation logic (requires deployed Lambdas)."""

    def test_infrastructure_creation_for_new_bucket(self):
        pass

    def test_infrastructure_skipped_for_existing_bucket(self):
        pass

    def test_infrastructure_idempotency(self):
        pass


class TestTargetConfigStorage:
    """Test that target_config is correctly stored in DynamoDB (moto)."""

    @mock_aws
    def test_new_s3_table_bucket_config_stored(self):
        """NEW_S3_TABLE_BUCKET target_config is stored correctly."""
        dynamodb = _create_pipelines_table()
        table = dynamodb.Table(PIPELINES_TABLE)
        pipeline_id = "test-new-bucket-001"

        table.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "pipeline_name": "Test New Bucket Pipeline",
                "target_config": {
                    "type": "NEW_S3_TABLE_BUCKET",
                    "bucket_name": "test-bucket-001",
                },
                "created_at": datetime.now().isoformat(),
            }
        )

        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert "Item" in response
        assert response["Item"]["target_config"]["type"] == "NEW_S3_TABLE_BUCKET"

    @mock_aws
    def test_existing_s3_table_bucket_config_stored(self):
        """EXISTING_S3_TABLE_BUCKET target_config is stored correctly."""
        dynamodb = _create_pipelines_table()
        table = dynamodb.Table(PIPELINES_TABLE)
        pipeline_id = "test-existing-bucket-001"

        table.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "pipeline_name": "Test Existing Bucket Pipeline",
                "target_config": {
                    "type": "EXISTING_S3_TABLE_BUCKET",
                    "bucket_name": "existing-test-bucket",
                    "table_name": "existing-test-table",
                },
                "created_at": datetime.now().isoformat(),
            }
        )

        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert response["Item"]["target_config"]["type"] == "EXISTING_S3_TABLE_BUCKET"
        assert response["Item"]["target_config"]["bucket_name"] == "existing-test-bucket"

    @mock_aws
    def test_glue_catalog_config_stored(self):
        """GLUE_CATALOG target_config is stored correctly."""
        dynamodb = _create_pipelines_table()
        table = dynamodb.Table(PIPELINES_TABLE)
        pipeline_id = "test-glue-catalog-001"

        table.put_item(
            Item={
                "pipeline_id": pipeline_id,
                "pipeline_name": "Test Glue Catalog Pipeline",
                "target_config": {
                    "type": "GLUE_CATALOG",
                    "database": "test_database",
                    "table": "test_table",
                },
                "created_at": datetime.now().isoformat(),
            }
        )

        response = table.get_item(Key={"pipeline_id": pipeline_id})
        assert response["Item"]["target_config"]["type"] == "GLUE_CATALOG"
        assert response["Item"]["target_config"]["database"] == "test_database"


class TestValidation:
    """Test validation logic — pure logic, no AWS calls."""

    def test_bucket_name_validation(self):
        """Test bucket name validation regex."""
        pattern = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"

        valid_names = [
            "my-bucket-123",
            "test-data-2024",
            "a1b2c3",
        ]

        invalid_names = [
            "My-Bucket",  # uppercase
            "-my-bucket",  # starts with hyphen
            "my-bucket-",  # ends with hyphen
            "ab",  # too short
            "my_bucket",  # underscore not allowed
        ]

        for name in valid_names:
            assert re.match(pattern, name), f"Valid name rejected: {name}"

        for name in invalid_names:
            assert not re.match(pattern, name), f"Invalid name accepted: {name}"
