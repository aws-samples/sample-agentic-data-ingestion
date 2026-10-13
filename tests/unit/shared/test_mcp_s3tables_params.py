# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression tests for the S3 Tables MCP server's boto3 parameter names.

These tests guard against the class of bug where an MCP tool calls a boto3
operation with the wrong keyword (e.g. ``get_table(tableARN=...)`` when the API
parameter is ``tableArn``, or ``delete_table(tableARN=...)`` when the API has no
ARN parameter at all and requires ``tableBucketARN``/``namespace``/``name``).

Each handler is driven through a real ``botocore.stub.Stubber``, which validates
the kwargs against the actual ``s3tables`` service model. A wrong parameter name
raises ``ParamValidationError`` at call time — so these tests fail loudly if the
bug is reintroduced, without making any real AWS calls.
"""

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import Stubber

# Add the s3_tables MCP server directory to the path so the handler imports.
_S3TABLES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mcp", "s3_tables")
sys.path.insert(0, _S3TABLES_DIR)

from agentcore_s3tables_mcp import S3TablesHandler

_REGION = "us-east-1"
_BUCKET_ARN = "arn:aws:s3tables:us-east-1:123456789012:bucket/my-bucket"
_TABLE_ARN = f"{_BUCKET_ARN}/table/abc123"


@pytest.fixture
def handler():
    """An S3TablesHandler whose MCP tool registration is mocked out."""
    return S3TablesHandler(mcp=MagicMock())


@pytest.fixture
def stubber(handler):
    """A Stubber over a real s3tables client, injected into the handler.

    The Stubber enforces the real service model: any wrong kwarg name surfaces
    as ParamValidationError when the handler issues the call.
    """
    client = boto3.client("s3tables", region_name=_REGION)
    stub = Stubber(client)
    handler.get_aws_client = lambda service_name: client  # type: ignore[method-assign]
    return stub


def _run(coro):
    return asyncio.run(coro)


def test_get_s3_table_uses_valid_params(handler, stubber):
    """get_s3_table must call get_table with the ``tableArn`` parameter."""
    stubber.add_response(
        "get_table",
        {
            "name": "t",
            "namespace": ["ns"],
            "tableARN": _TABLE_ARN,
            "type": "customer",
            "versionToken": "v1",
            "metadataLocation": "s3://x/meta",
            "warehouseLocation": "s3://x/wh",
            "createdAt": datetime(2024, 1, 1),
            "createdBy": "123456789012",
            "modifiedAt": datetime(2024, 1, 1),
            "modifiedBy": "123456789012",
            "ownerAccountId": "123456789012",
            "format": "ICEBERG",
        },
        {"tableArn": _TABLE_ARN},  # expected request params — wrong name → ParamValidationError
    )
    with stubber:
        result = _run(handler.get_s3_table(MagicMock(), table_arn=_TABLE_ARN))
    assert result["success"] is True
    assert result["table"]["table_arn"] == _TABLE_ARN
    stubber.assert_no_pending_responses()


def test_delete_s3_table_uses_valid_params(handler, stubber):
    """delete_s3_table must call delete_table with bucket ARN + namespace + name."""
    stubber.add_response(
        "delete_table",
        {},
        {"tableBucketARN": _BUCKET_ARN, "namespace": "ns", "name": "orders"},
    )
    with stubber:
        result = _run(handler.delete_s3_table(MagicMock(), table_bucket_arn=_BUCKET_ARN, namespace="ns", name="orders"))
    assert result["success"] is True
    stubber.assert_no_pending_responses()


def test_delete_s3_table_passes_version_token_when_given(handler, stubber):
    """version_token is forwarded only when provided."""
    stubber.add_response(
        "delete_table",
        {},
        {"tableBucketARN": _BUCKET_ARN, "namespace": "ns", "name": "orders", "versionToken": "v9"},
    )
    with stubber:
        result = _run(
            handler.delete_s3_table(
                MagicMock(), table_bucket_arn=_BUCKET_ARN, namespace="ns", name="orders", version_token="v9"
            )
        )
    assert result["success"] is True
    stubber.assert_no_pending_responses()


def test_rename_s3_table_uses_valid_params(handler, stubber):
    """rename_s3_table must call rename_table with bucket ARN + namespace + name + newName."""
    stubber.add_response(
        "rename_table",
        {},
        {"tableBucketARN": _BUCKET_ARN, "namespace": "ns", "name": "orders", "newName": "orders_v2"},
    )
    with stubber:
        result = _run(
            handler.rename_s3_table(
                MagicMock(),
                table_bucket_arn=_BUCKET_ARN,
                namespace="ns",
                name="orders",
                new_name="orders_v2",
            )
        )
    assert result["success"] is True
    stubber.assert_no_pending_responses()


def test_rename_s3_table_includes_new_namespace_when_moving(handler, stubber):
    """A namespace move forwards newNamespaceName."""
    stubber.add_response(
        "rename_table",
        {},
        {
            "tableBucketARN": _BUCKET_ARN,
            "namespace": "ns",
            "name": "orders",
            "newName": "orders",
            "newNamespaceName": "archive",
        },
    )
    with stubber:
        result = _run(
            handler.rename_s3_table(
                MagicMock(),
                table_bucket_arn=_BUCKET_ARN,
                namespace="ns",
                name="orders",
                new_name="orders",
                new_namespace="archive",
            )
        )
    assert result["success"] is True
    stubber.assert_no_pending_responses()
