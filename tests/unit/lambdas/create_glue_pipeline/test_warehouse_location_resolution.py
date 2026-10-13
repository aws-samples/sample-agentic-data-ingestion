# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for get_warehouse_location() -- mock the state table and S3 Tables
API, verify it returns the correct warehouse location for new and existing buckets.

Validates AC-4 from Phase 7 requirements.
"""

import importlib.util
import os
import sys
import unittest.mock as mock
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock"),
    ),
}


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "glue_pipeline_wh_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["glue_pipeline_wh_lf"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()
# P21 Task 7.3: get_warehouse_location moved to the sibling ``artifacts`` module.
# Patch the state table / s3tables client on the executing module's globals
# (resolved via ``__globals__``) so the fixtures reach whichever ``artifacts``
# instance the re-exported function actually runs in — robust against the
# importlib/patch.dict module-identity split.
get_warehouse_location = _lf.get_warehouse_location
_artifacts_globals = get_warehouse_location.__globals__

TABLE_NAME = "agentic_data_ingestion_state"
RUN_ID = "run-2025-01-15-12-00-00-aaaa1111"
WH = "s3://aws-s3-table-bucket-us-west-2-abc123/warehouse"


def _create_table(ddb):
    ddb.create_table(
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
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(ddb)
        table = ddb.Table(TABLE_NAME)
        orig = _artifacts_globals["pipeline_runs_table"]
        _artifacts_globals["pipeline_runs_table"] = table
        yield table
        _artifacts_globals["pipeline_runs_table"] = orig


class TestFromInfrastructureStep:
    def test_s3_table_bucket_path(self, state_table):
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "create_infrastructure",
                "status": "completed",
                "output_data": {"s3_table_bucket": {"warehouse_location": WH}},
            }
        )
        assert get_warehouse_location(RUN_ID, {"type": "NEW_S3_TABLE_BUCKET"}) == WH

    def test_infrastructure_status_path(self, state_table):
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "create_infrastructure",
                "status": "completed",
                "output_data": {"infrastructure_status": {"s3_table_bucket": {"warehouse_location": WH}}},
            }
        )
        assert get_warehouse_location(RUN_ID, {"type": "NEW_S3_TABLE_BUCKET"}) == WH

    def test_top_level_output_data(self, state_table):
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "create_infrastructure",
                "status": "completed",
                "output_data": {"warehouse_location": WH},
            }
        )
        assert get_warehouse_location(RUN_ID, {"type": "NEW_S3_TABLE_BUCKET"}) == WH


class TestFromS3TablesAPI:
    def test_queries_api(self, state_table):
        # P21 Req 5.6: s3tables client is created once at module scope, so patch
        # the module-level client rather than boto3.client (per-call creation removed).
        mock_s3t = MagicMock()
        mock_s3t.get_table.return_value = {"warehouseLocation": WH}
        cfg = {"type": "EXISTING_S3_TABLE_BUCKET", "bucket_name": "b", "namespace_name": "ns", "table_name": "t"}
        orig = _artifacts_globals["s3tables_client"]
        _artifacts_globals["s3tables_client"] = mock_s3t
        try:
            assert get_warehouse_location(RUN_ID, cfg) == WH
        finally:
            _artifacts_globals["s3tables_client"] = orig
        mock_s3t.get_table.assert_called_once()

    def test_namespace_as_list(self, state_table):
        # P21 Req 5.6: patch the module-level s3tables client (see test_queries_api).
        mock_s3t = MagicMock()
        mock_s3t.get_table.return_value = {"warehouseLocation": WH}
        cfg = {
            "type": "EXISTING_S3_TABLE_BUCKET",
            "bucket_name": "b",
            "namespace_name": ["ns", "other"],
            "table_name": "t",
        }
        orig = _artifacts_globals["s3tables_client"]
        _artifacts_globals["s3tables_client"] = mock_s3t
        try:
            assert get_warehouse_location(RUN_ID, cfg) == WH
        finally:
            _artifacts_globals["s3tables_client"] = orig
        assert mock_s3t.get_table.call_args[1]["namespace"] == "ns"


class TestFromTargetConfig:
    def test_direct_fallback(self, state_table):
        cfg = {"type": "S3_PARQUET", "warehouse_location": WH}
        assert get_warehouse_location(RUN_ID, cfg) == WH


class TestErrors:
    def test_raises_when_not_resolvable(self, state_table):
        with pytest.raises(ValueError, match="warehouse_location could not be resolved"):
            get_warehouse_location(RUN_ID, {"type": "S3_PARQUET"})

    def test_raises_when_api_fails(self, state_table):
        from botocore.exceptions import ClientError

        # P21 Req 5.6: patch the module-level s3tables client (see TestFromS3TablesAPI).
        mock_s3t = MagicMock()
        mock_s3t.get_table.side_effect = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "nope"}}, "GetTable"
        )
        cfg = {"type": "EXISTING_S3_TABLE_BUCKET", "bucket_name": "b", "namespace_name": "ns", "table_name": "t"}
        orig = _artifacts_globals["s3tables_client"]
        _artifacts_globals["s3tables_client"] = mock_s3t
        try:
            with pytest.raises(ValueError, match="warehouse_location could not be resolved"):
                get_warehouse_location(RUN_ID, cfg)
        finally:
            _artifacts_globals["s3tables_client"] = orig
