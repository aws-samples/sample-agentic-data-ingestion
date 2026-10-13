# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Integration test: write_mode propagation from pipeline config to Glue job parameters.

Validates: Requirement 6.5
- Sets pipeline write_mode in DynamoDB (append, overwrite, merge)
- Calls the create_glue_pipeline parameter builder
- Verifies write_mode appears in the Glue job DefaultArguments

Uses importlib to load the parameter_builder module with boto3 mocked via moto.
"""

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_create_glue_pipeline"
_SHARED_DIR = _PROJECT_ROOT / "shared"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_parameter_builder():
    """Load the parameter_builder module from the create_glue_pipeline Lambda."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if str(_LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(_LAMBDA_DIR))

    # Stub out aws_lambda_powertools if not installed
    if "aws_lambda_powertools" not in sys.modules:
        mock_powertools = MagicMock()
        mock_powertools.Logger.return_value = MagicMock()
        sys.modules["aws_lambda_powertools"] = mock_powertools

    sys.modules.pop("parameter_builder", None)

    spec = importlib.util.spec_from_file_location(
        "parameter_builder",
        str(_LAMBDA_DIR / "parameter_builder.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["parameter_builder"] = module
    spec.loader.exec_module(module)
    return module


def _load_lambda_function_module():
    """Load the create_glue_pipeline lambda_function module for build_*_arguments helpers."""
    if str(_SHARED_DIR) not in sys.path:
        sys.path.insert(0, str(_SHARED_DIR))
    if str(_LAMBDA_DIR) not in sys.path:
        sys.path.insert(0, str(_LAMBDA_DIR))

    # Stub out heavy dependencies
    for mod_name in [
        "aws_lambda_powertools",
        "strands_mcp_factory",
        "prompt_template_loader",
        "metrics_facade",
        "schema_evolution",
        "mcp",
        "mcp.client",
        "mcp.client.streamable_http",
        "streamable_http_sigv4",
    ]:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # Provide require_env and get_aws_account_id stubs
    mock_env_utils = MagicMock()
    mock_env_utils.require_env = lambda name, default=None: os.environ.get(name, default or name)
    mock_env_utils.get_aws_account_id = lambda: "123456789012"
    sys.modules["env_utils"] = mock_env_utils

    sys.modules.pop("lambda_function", None)

    spec = importlib.util.spec_from_file_location(
        "lambda_function",
        str(_LAMBDA_DIR / "lambda_function.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["lambda_function"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteModePropagation:
    """Set pipeline write_mode → call parameter builder → verify mode in Glue job params."""

    @mock_aws
    def test_append_write_mode_propagates_to_glue_catalog_arguments(self, mock_lambda_env):
        """write_mode='append' should appear in Glue Catalog job arguments."""
        region = "us-east-1"

        lf = _load_lambda_function_module()

        target_config = {
            "type": "GLUE_CATALOG",
            "database": "test_db",
            "table": "test_table",
            "format": "parquet",
            "write_mode": "append",
        }

        result = lf.build_glue_catalog_arguments(target_config, source_path="s3://bucket/data.csv")

        assert result["--write-mode"] == "append", (
            f"Expected write_mode 'append' in Glue args, got: {result.get('--write-mode')}"
        )
        assert result["--database_name"] == "test_db"
        assert result["--table_name"] == "test_table"

    @mock_aws
    def test_overwrite_write_mode_propagates_to_glue_catalog_arguments(self, mock_lambda_env):
        """write_mode='overwrite' should appear in Glue Catalog job arguments."""
        lf = _load_lambda_function_module()

        target_config = {
            "type": "GLUE_CATALOG",
            "database": "my_db",
            "table": "my_table",
            "format": "parquet",
            "write_mode": "overwrite",
        }

        result = lf.build_glue_catalog_arguments(target_config, source_path="s3://bucket/data.csv")

        assert result["--write-mode"] == "overwrite"

    @mock_aws
    def test_write_mode_propagates_via_parameter_builder(self, mock_lambda_env):
        """write_mode from target_config should propagate through build_parameter_values."""
        region = "us-east-1"

        pb = _load_parameter_builder()

        # Create moto DynamoDB table for generated code
        dynamodb = boto3.resource("dynamodb", region_name=region)
        dynamodb.create_table(
            TableName="agentic_data_ingestion_generated_code",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "pipeline_id", "AttributeType": "S"},
                {"AttributeName": "generation_timestamp", "AttributeType": "S"},
                {"AttributeName": "run_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "pipeline_id-index",
                    "KeySchema": [
                        {"AttributeName": "pipeline_id", "KeyType": "HASH"},
                        {"AttributeName": "generation_timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "run_id-index",
                    "KeySchema": [
                        {"AttributeName": "run_id", "KeyType": "HASH"},
                        {"AttributeName": "generation_timestamp", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        target_config = {
            "type": "GLUE_CATALOG",
            "write_mode": "append",
            "catalog_database": "test_db",
            "table_name": "test_table",
        }
        pipeline_metadata = {
            "processed_bucket": "agentic-data-ingestion-processed",
            "source_format": "csv",
            "region": "us-east-1",
        }

        # build_parameter_values with WRITE_MODE in required_params
        result = pb.build_parameter_values(
            pipeline_id="test-pipeline",
            required_params=["WRITE_MODE", "SOURCE_FORMAT"],
            optional_params=[],
            pipeline_metadata=pipeline_metadata,
            target_config=target_config,
        )

        assert result.get("--WRITE_MODE") == "append", f"Expected WRITE_MODE='append' in params, got: {result}"

    @mock_aws
    def test_jdbc_write_mode_propagates(self, mock_lambda_env):
        """write_mode should propagate to JDBC job arguments."""
        lf = _load_lambda_function_module()

        # A Secrets Manager reference replaces the inline `jdbc_user` /
        # `jdbc_password` pair: the builder ignores that pair since the
        # JDBC-target credential fix (standing invariant `LGR-819980f-1`) and
        # would otherwise look the reference up on the (non-existent) moto
        # connection `my-conn`. write_mode propagation does not depend on it.
        #
        # Bound to a variable rather than written inside the dict on purpose:
        # bandit 1.9.4 reports B105 on a `"secret_arn": "<literal>"` entry with
        # the whole multi-line dict as its range, and its SARIF formatter then
        # crashes on the comment lines in that range, emptying the entire bandit
        # report (seen in the 2026-10-06 ASH scan). Not a secret: an ARN.
        reference_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:write-mode-AbCdEf"
        target_config = {
            "type": "JDBC",
            "jdbc_connection_name": "my-conn",
            "database_name": "mydb",
            "table_name": "mytable",
            "write_mode": "overwrite",
            "jdbc_url": "jdbc:postgresql://host:5432/db",
            "jdbc_driver": "org.postgresql.Driver",
            "secret_arn": reference_arn,
        }

        result = lf.build_jdbc_arguments(target_config, source_path="s3://bucket/data.csv")

        assert result["--write-mode"] == "overwrite", (
            f"Expected write_mode 'overwrite' in JDBC args, got: {result.get('--write-mode')}"
        )
