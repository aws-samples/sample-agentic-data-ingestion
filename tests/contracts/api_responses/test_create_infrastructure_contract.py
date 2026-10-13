# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for create_infrastructure Lambda response shape (Task 2.15).

Validates: AC-19.4
Verifies the Lambda response shape matches what the orchestration-stack
expects at $.create_infrastructure_result.
"""

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing the module
sys.modules["env_utils"] = MagicMock()
sys.modules["env_utils"].require_env = lambda name, default=None: default or name
sys.modules["env_utils"].get_aws_account_id = lambda: "123456789012"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_INFRA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_infrastructure"
)


def _get_module():
    """Import (or re-import) the create_infrastructure lambda_function module."""
    sys.modules.pop("lambda_function", None)
    if _INFRA_DIR not in sys.path:
        sys.path.insert(0, _INFRA_DIR)
    with patch("boto3.client"), patch("boto3.resource"):
        import lambda_function

        importlib.reload(lambda_function)
    return lambda_function


# Required top-level keys in the Lambda response for the success path
SUCCESS_RESPONSE_KEYS = {
    "statusCode",
    "run_id",
    "pipeline_id",
    "infrastructure_status",
    "approval_required",
    "next_step",
}

SKIP_RESPONSE_KEYS = {
    "statusCode",
    "run_id",
    "pipeline_id",
    "status",
    "skip_reason",
    "approval_required",
}


class TestCreateInfrastructureContract:
    """Contract tests verifying response shape matches orchestration-stack expectations."""

    def test_success_response_shape(self):
        """Success response contains all required keys for orchestration-stack."""
        mod = _get_module()
        with (
            patch.object(mod, "create_s3_table_infrastructure") as mock_create_infra,
            patch.object(mod, "determine_infrastructure_approval_requirement") as mock_approval_req,
            patch.object(mod, "create_infrastructure_approval_payload") as mock_approval_payload,
            patch.object(mod, "should_create_infrastructure") as mock_should_create,
            patch.object(mod, "get_pipeline_metadata") as mock_metadata,
            patch.object(mod, "pipeline_runs_table"),
            patch.object(mod, "s3_client"),
        ):
            mock_metadata.return_value = {
                "pipeline_id": "test-pipeline",
                "target_config": {"type": "NEW_S3_TABLE_BUCKET", "bucket_name": "test-bucket"},
            }
            mock_should_create.return_value = (True, "Creating infrastructure")
            mock_approval_req.return_value = False
            mock_approval_payload.return_value = {"approval_type": "infrastructure"}
            mock_create_infra.return_value = {
                "overall_status": "success",
                "s3_table_bucket": {
                    "bucket_name": "test-bucket",
                    "table_name": "test_table",
                    "table_bucket_arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket",
                    "table_arn": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table",
                    "namespace": "test_ns",
                    "status": "created",
                    "table_format": "iceberg",
                    "data_location": "s3://test-bucket/data/test_table/",
                    "metadata_location": "s3://test-bucket/metadata/test_table",
                    "warehouse_location": "s3://test-bucket/warehouse",
                },
                "resources_created": ["S3TableBucket", "Namespace", "IcebergTable"],
            }

            event = {
                "run_id": "run-123",
                "pipeline_id": "test-pipeline",
                "target_schema": {"name": "test", "fields": [{"name": "id", "type": "integer"}]},
            }

            result = mod.lambda_handler(event, None)

            assert SUCCESS_RESPONSE_KEYS.issubset(result.keys()), (
                f"Missing keys: {SUCCESS_RESPONSE_KEYS - result.keys()}"
            )
            assert result["statusCode"] == 200
            assert result["run_id"] == "run-123"
            assert result["pipeline_id"] == "test-pipeline"

    def test_skip_response_shape(self):
        """Skip response contains all required keys when infrastructure not needed."""
        mod = _get_module()
        with (
            patch.object(mod, "should_create_infrastructure") as mock_should_create,
            patch.object(mod, "get_pipeline_metadata") as mock_metadata,
            patch.object(mod, "pipeline_runs_table"),
        ):
            mock_metadata.return_value = {
                "pipeline_id": "test-pipeline",
                "target_config": {"type": "GLUE_CATALOG"},
            }
            mock_should_create.return_value = (False, "Using existing Glue Catalog table")

            event = {
                "run_id": "run-456",
                "pipeline_id": "test-pipeline",
            }

            result = mod.lambda_handler(event, None)

            assert SKIP_RESPONSE_KEYS.issubset(result.keys()), f"Missing keys: {SKIP_RESPONSE_KEYS - result.keys()}"
            assert result["statusCode"] == 200
            assert result["status"] == "skipped"

    def test_error_response_shape(self):
        """On error the handler RAISES StepFailure instead of returning an error envelope.

        SFN's CreateInfrastructure task uses payloadResponseOnly and ignores a
        returned statusCode, so the Lambda must raise for the failure to route to
        the task's Catch. P28 R1.1: the raised exception is StepFailure whose
        str() serializes to the stable Failure_Record JSON (step_name,
        error_type, message, details) parsed by the Catch path.
        """
        mod = _get_module()
        with patch.object(mod, "pipeline_runs_table"):
            event = {}
            with pytest.raises(mod.StepFailure, match="run_id and pipeline_id are required") as exc_info:
                mod.lambda_handler(event, None)

        # The serialized exception (what SFN puts in the Cause) must be the
        # stable JSON contract consumed by the RecordFailure Lambda.
        serialized = json.loads(str(exc_info.value))
        assert serialized["step_name"] == "create_infrastructure"
        assert serialized["error_type"] == "permanent"
        assert "run_id and pipeline_id are required" in serialized["message"]
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_create_s3_table_bucket_return_is_json_serializable(self):
        """Return value from create_s3_table_bucket is fully JSON-serializable."""
        mod = _get_module()
        with patch.object(mod, "get_pipeline_metadata") as mock_metadata, patch.object(mod, "boto3") as mock_boto3:
            mock_metadata.return_value = {
                "pipeline_id": "test-pipeline",
                "target_config": {
                    "bucket_name": "test-bucket",
                    "namespace_name": "test_ns",
                    "table_name": "test_table",
                },
            }
            s3tables = MagicMock()
            s3tables.exceptions.ConflictException = type("ConflictException", (Exception,), {})
            s3tables.list_table_buckets.return_value = {"tableBuckets": []}
            s3tables.create_table_bucket.return_value = {
                "arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket"
            }
            s3tables.list_namespaces.return_value = {"namespaces": []}
            s3tables.list_tables.return_value = {"tables": []}
            s3tables.create_table.return_value = {
                "tableARN": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table"
            }
            s3tables.get_table.return_value = {"metadataLocation": "s3://meta", "warehouseLocation": "s3://wh"}
            mock_boto3.client.return_value = s3tables

            result = mod.create_s3_table_bucket("test-pipeline", {"name": "test", "fields": []})

            serialized = json.dumps(result)
            assert isinstance(serialized, str)
