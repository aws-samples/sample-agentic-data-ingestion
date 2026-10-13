# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test for create_s3_table_bucket against real AWS (Task 2.16).

Validates: AC-19.3, AC-19.4
- Creates a real test bucket/namespace/table via boto3 s3tables
- Verifies real ARNs are returned
- Cleans up all resources after test

Requires:
- AWS credentials configured (AWS_PROFILE or IAM role)
- AWS_REGION set (defaults to us-west-2)

Run with: pytest tests/test_create_infrastructure_integration.py -v -s
Skip in CI with: pytest -m "not integration"
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import boto3
import pytest

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))

# Stub out Lambda-layer dependencies
sys.modules["env_utils"] = MagicMock()
sys.modules["env_utils"].require_env = lambda name, default=None: os.environ.get(name, default or name)
sys.modules["env_utils"].get_aws_account_id = lambda: "123456789012"

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_infrastructure"),
)

# Import with patched DynamoDB (we don't need real tables for this test)
with patch("boto3.resource"):
    # Remove cached lambda_function from other test files to force fresh import
    sys.modules.pop("lambda_function", None)
    from lambda_function import create_s3_table_bucket


# Generate unique test names to avoid collisions
TEST_ID = uuid.uuid4().hex[:8]
TEST_BUCKET_NAME = f"agentic-data-ingestion-inttest-{TEST_ID}"
TEST_NAMESPACE = f"inttest_{TEST_ID}"
TEST_TABLE_NAME = f"test_table_{TEST_ID}"

SAMPLE_SCHEMA = {
    "name": TEST_TABLE_NAME,
    "fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
    ],
}


def _cleanup_s3_tables_resources():
    """Best-effort cleanup of test S3 Tables resources."""
    s3tables = boto3.client("s3tables", region_name=AWS_REGION)

    # Find the bucket ARN
    bucket_arn = None
    try:
        response = s3tables.list_table_buckets()
        for bucket in response.get("tableBuckets", []):
            if bucket["name"] == TEST_BUCKET_NAME:
                bucket_arn = bucket["arn"]
                break
    except Exception:
        return

    if not bucket_arn:
        return

    # Delete table first
    try:
        s3tables.delete_table(tableBucketARN=bucket_arn, namespace=TEST_NAMESPACE, name=TEST_TABLE_NAME)
    except Exception:
        pass

    # Delete namespace
    try:
        s3tables.delete_namespace(tableBucketARN=bucket_arn, namespace=TEST_NAMESPACE)
    except Exception:
        pass

    # Delete bucket
    try:
        s3tables.delete_table_bucket(tableBucketARN=bucket_arn)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure cleanup runs after each test, even on failure."""
    yield
    _cleanup_s3_tables_resources()


@pytest.mark.skip(reason="Requires real AWS credentials and deployed infrastructure")
class TestCreateS3TableBucketIntegration:
    """Integration tests against real AWS S3 Tables service."""

    @patch("lambda_function.get_pipeline_metadata")
    def test_creates_real_resources_and_returns_real_arns(self, mock_metadata):
        """Create real bucket/namespace/table and verify ARNs are real."""
        mock_metadata.return_value = {
            "pipeline_id": "inttest-pipeline",
            "target_config": {
                "bucket_name": TEST_BUCKET_NAME,
                "namespace_name": TEST_NAMESPACE,
                "table_name": TEST_TABLE_NAME,
            },
        }

        result = create_s3_table_bucket("inttest-pipeline", SAMPLE_SCHEMA)

        # Verify success
        assert result["status"] in ("created", "already_exists"), f"Unexpected status: {result}"
        assert result["bucket_name"] == TEST_BUCKET_NAME
        assert result["table_name"] == TEST_TABLE_NAME
        assert result["namespace"] == TEST_NAMESPACE

        # Verify real ARNs (not mocked)
        assert result["table_bucket_arn"].startswith("arn:aws:s3tables:")
        assert AWS_REGION in result["table_bucket_arn"]
        assert result["table_arn"].startswith("arn:aws:s3tables:")

        # Verify warehouse_location is a real S3 path
        assert result["warehouse_location"].startswith("s3://") or result["warehouse_location"] == ""

    @patch("lambda_function.get_pipeline_metadata")
    def test_idempotent_second_call_returns_already_exists(self, mock_metadata):
        """Calling twice returns already_exists on second call."""
        mock_metadata.return_value = {
            "pipeline_id": "inttest-pipeline",
            "target_config": {
                "bucket_name": TEST_BUCKET_NAME,
                "namespace_name": TEST_NAMESPACE,
                "table_name": TEST_TABLE_NAME,
            },
        }

        # First call creates
        result1 = create_s3_table_bucket("inttest-pipeline", SAMPLE_SCHEMA)
        assert result1["status"] in ("created", "already_exists")

        # Second call should find everything existing
        result2 = create_s3_table_bucket("inttest-pipeline", SAMPLE_SCHEMA)
        assert result2["status"] == "already_exists"
        assert result2["table_bucket_arn"] == result1["table_bucket_arn"]
        assert result2["table_arn"] == result1["table_arn"]
