# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for file_validation dual-path routing (Task 11.2).

Verifies that:
- Parquet/Avro/ORC use the lightweight passthrough (HEAD only, no get_object)
- CSV/JSON also use the HEAD-only path (current code uses HEAD for ALL formats)
- No format triggers get_object or put_object (no file content read/conversion)

Requirements: 24.2
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_s3_event(bucket: str, key: str) -> dict:
    """Build a minimal S3 event for the lambda handler."""
    return {
        "Records": [
            {
                "eventSource": "aws:s3",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                },
            }
        ],
    }


@pytest.fixture()
def handler_env():
    """Load file_validation Lambda via importlib with fully mocked AWS clients.

    Uses importlib.util.spec_from_file_location to avoid collisions with
    other lambda_function.py modules on sys.path.
    """
    lambda_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "lambda",
            "agentic_data_ingestion_file_validation",
        )
    )
    lambda_path = os.path.join(lambda_dir, "lambda_function.py")
    shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

    os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
    os.environ.setdefault("DYNAMODB_TABLE", "test-table")

    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    # HEAD returns a 10 KB file by default
    mock_s3.head_object.return_value = {"ContentLength": 10_240}
    mock_table.put_item.return_value = {}

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    mock_boto3.resource.return_value = mock_dynamodb

    sys.path.insert(0, lambda_dir)
    sys.path.insert(0, shared_dir)

    with patch.dict(
        "sys.modules",
        {"boto3": mock_boto3},
    ):
        sys.modules.pop("lambda_function", None)

        spec = importlib.util.spec_from_file_location("lambda_function", lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

        # Patch module-level clients
        lf.s3_client = mock_s3
        lf.table = mock_table

        yield lf, mock_s3, mock_table

    sys.modules.pop("lambda_function", None)
    if lambda_dir in sys.path:
        sys.path.remove(lambda_dir)
    if shared_dir in sys.path:
        sys.path.remove(shared_dir)


# ── Native format passthrough (Parquet / Avro / ORC) ─────────────────────


class TestNativeFormatPassthrough:
    """Parquet, Avro, and ORC use HEAD only — no get_object or put_object."""

    @pytest.mark.parametrize(
        "ext,expected_format",
        [
            ("parquet", "parquet"),
            ("pq", "parquet"),
            ("avro", "avro"),
            ("orc", "orc"),
        ],
    )
    def test_head_only_no_get_object(self, ext, expected_format, handler_env):
        lf, mock_s3, _ = handler_env
        event = _make_s3_event("src-bucket", f"pipeline-1/data.{ext}")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.head_object.assert_called_once()
        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["statusCode"] == 200
        assert result["source_format"] == expected_format


# ── CSV / JSON also use HEAD-only path ───────────────────────────────────


class TestCsvJsonHeadOnlyPath:
    """CSV and JSON use the same HEAD-only path — no file read or conversion."""

    @pytest.mark.parametrize(
        "ext,expected_format",
        [
            ("csv", "csv"),
            ("tsv", "csv"),
            ("json", "json"),
            ("jsonl", "json"),
            ("ndjson", "json"),
        ],
    )
    def test_csv_json_head_only(self, ext, expected_format, handler_env):
        lf, mock_s3, _ = handler_env
        event = _make_s3_event("src-bucket", f"pipeline-1/data.{ext}")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.head_object.assert_called_once()
        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["statusCode"] == 200
        assert result["source_format"] == expected_format

    def test_large_csv_still_head_only(self, handler_env):
        """Even a 200 MB CSV uses HEAD only — no conversion."""
        lf, mock_s3, _ = handler_env
        mock_s3.head_object.return_value = {"ContentLength": 200 * 1024 * 1024}

        event = _make_s3_event("src-bucket", "pipeline-1/big.csv")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["source_format"] == "csv"
        assert result["statusCode"] == 200
