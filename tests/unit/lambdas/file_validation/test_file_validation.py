# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for file size validation in file_validation Lambda."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_file_validation"),
)

# Set required env vars before importing the module
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test_table")


@pytest.fixture(autouse=True)
def _mock_boto(monkeypatch):
    """Prevent real AWS calls during import and tests."""
    mock_s3 = MagicMock()
    mock_dynamodb = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    # Ensure our Lambda directory is at the front of sys.path
    fv_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_file_validation"
        )
    )
    sys.path.insert(0, fv_dir)

    with patch.dict(
        "sys.modules",
        {
            "boto3": MagicMock(),
        },
    ):
        import boto3 as _boto3

        _boto3.client.return_value = mock_s3
        _boto3.resource.return_value = mock_dynamodb
        # Force reimport so module picks up mocks
        if "lambda_function" in sys.modules:
            del sys.modules["lambda_function"]
        import lambda_function

        lambda_function.s3_client = mock_s3
        lambda_function.table = mock_table
        yield lambda_function, mock_s3


class TestMaxFileSizeConfig:
    """Tests for MAX_FILE_SIZE_BYTES configuration."""

    def test_default_is_5tb(self, _mock_boto):
        lf, _ = _mock_boto
        assert lf.MAX_FILE_SIZE_BYTES == 5 * 1024 * 1024 * 1024 * 1024  # 5TB (S3 limit)
