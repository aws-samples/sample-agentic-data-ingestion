# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: file validation dual-path routing (Property 38).

**Validates: Requirements 24.2**

Property 38: For any file format, file_validation routes Parquet/Avro/ORC
to passthrough and CSV/JSON to the same HEAD-only path.

In the current implementation, ALL recognized formats use the HEAD-only path
(no get_object, no put_object). This property verifies that invariant holds
for every supported format.

Uses @settings(max_examples=10).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_s3_event(bucket: str, key: str) -> dict:
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


def _load_handler():
    """Load file_validation Lambda via importlib with mocked AWS clients.

    Returns (module, mock_s3_client, mock_table).
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

    if lambda_dir not in sys.path:
        sys.path.insert(0, lambda_dir)
    if shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)

    os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
    os.environ.setdefault("DYNAMODB_TABLE", "test-table")

    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    mock_s3.head_object.return_value = {"ContentLength": 10_240}
    mock_table.put_item.return_value = {}

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    mock_boto3.resource.return_value = mock_dynamodb

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

        lf.s3_client = mock_s3
        lf.table = mock_table

    return lf, mock_s3, mock_table


# ── Strategies ────────────────────────────────────────────────────────────

NATIVE_FORMATS = {"parquet", "pq", "avro", "orc"}
TEXT_FORMATS = {"csv", "tsv", "json", "jsonl", "ndjson"}
ALL_EXTENSIONS = list(NATIVE_FORMATS | TEXT_FORMATS)

EXPECTED_FORMAT = {
    "csv": "csv",
    "tsv": "csv",
    "json": "json",
    "jsonl": "json",
    "ndjson": "json",
    "parquet": "parquet",
    "pq": "parquet",
    "avro": "avro",
    "orc": "orc",
}

extensions = st.sampled_from(ALL_EXTENSIONS)
file_sizes = st.integers(min_value=100, max_value=500 * 1024 * 1024)  # 100B – 500MB


# ── Property tests ────────────────────────────────────────────────────────


@given(ext=extensions, size=file_sizes)
@settings(max_examples=10)
def test_all_formats_use_head_only_path(ext, size):
    """**Validates: Requirements 24.2**

    Property 38: For any file format, file_validation uses HEAD only.
    Parquet/Avro/ORC go through passthrough, CSV/JSON use the same
    HEAD-only path. No format triggers get_object or put_object.
    """
    lf, mock_s3, mock_table = _load_handler()

    mock_s3.reset_mock()
    mock_table.reset_mock()
    mock_s3.head_object.return_value = {"ContentLength": size}
    mock_table.put_item.return_value = {}

    event = _make_s3_event("test-bucket", f"pipeline-1/data.{ext}")
    ctx = MagicMock()

    result = lf.lambda_handler(event, ctx)

    # All formats: HEAD is called, get_object and put_object are NOT
    mock_s3.head_object.assert_called_once()
    mock_s3.get_object.assert_not_called()
    mock_s3.put_object.assert_not_called()

    assert result["statusCode"] == 200
    assert result["source_format"] == EXPECTED_FORMAT[ext]
