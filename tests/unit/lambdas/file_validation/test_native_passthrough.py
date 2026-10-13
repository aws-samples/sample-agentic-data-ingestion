# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the native format passthrough (HEAD-only path) in file_validation Lambda.

Verifies that:
- All recognized formats use the HEAD-only path (no file read, no conversion)
- The return payload includes source_format
- Unknown formats are marked as unsupported
- DynamoDB metadata is stored correctly
"""

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


def _make_s3_event(bucket, key):
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
def lambda_env():
    """Import file_validation lambda_function with fully mocked AWS clients.

    Uses importlib to load the exact file so that other lambda_function.py
    modules on sys.path (e.g. schema_extract) don't shadow it.
    """
    import importlib.util

    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    # Default: HEAD returns 1 KB file, put_item succeeds
    mock_s3.head_object.return_value = {"ContentLength": 1024}
    mock_table.put_item.return_value = {}

    # store_metadata_in_dynamodb returns (run_id, pipeline_id)
    _call_count = {"n": 0}

    def _fake_store(**kwargs):
        _call_count["n"] += 1
        mock_table.put_item(Item=kwargs)
        return (f"run-{_call_count['n']}", kwargs.get("original_s3_uri", "pipeline-1").split("/")[0])

    # Build the absolute path to the file_validation lambda_function.py
    _lambda_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_file_validation",
    )
    _lambda_path = os.path.join(_lambda_dir, "lambda_function.py")

    # Ensure the file_validation dir is first on sys.path so its imports resolve
    sys.path.insert(0, os.path.abspath(_lambda_dir))

    # Mock boto3 at module level so the import succeeds
    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_s3
    mock_boto3.resource.return_value = mock_dynamodb

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        # Remove any cached lambda_function to force a fresh import
        sys.modules.pop("lambda_function", None)

        spec = importlib.util.spec_from_file_location("lambda_function", _lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

        # Patch the module-level AWS clients
        lf.s3_client = mock_s3
        lf.table = mock_table
        lf.store_metadata_in_dynamodb = _fake_store

        yield lf, mock_s3, mock_table

    # Cleanup
    sys.modules.pop("lambda_function", None)
    sys.path.remove(os.path.abspath(_lambda_dir))


# ── Lightweight path tests ────────────────────────────────────────────────


class TestLightweightPath:
    """All recognized formats should use HEAD only — no get_object."""

    @pytest.mark.parametrize(
        "ext,expected_format",
        [
            ("parquet", "parquet"),
            ("pq", "parquet"),
            ("avro", "avro"),
            ("orc", "orc"),
        ],
    )
    def test_native_format_no_file_read(self, ext, expected_format, lambda_env):
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 50_000_000}  # 50MB — under MAX_FILE_SIZE

        event = _make_s3_event("src-bucket", f"pipeline-1/data.{ext}")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        # Should NOT call get_object (no file read)
        mock_s3.get_object.assert_not_called()
        # Should NOT call put_object (no JSON conversion)
        mock_s3.put_object.assert_not_called()
        # Return payload includes source_format
        assert result["source_format"] == expected_format
        assert result["statusCode"] == 200

    def test_large_csv_uses_lightweight_path(self, lambda_env):
        """Large CSV files use the same HEAD-only path."""
        lf, mock_s3, mock_table = lambda_env
        # 100MB — above the 50MB default threshold
        mock_s3.head_object.return_value = {"ContentLength": 100 * 1024 * 1024}

        event = _make_s3_event("src-bucket", "pipeline-1/big.csv")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["source_format"] == "csv"
        assert result["statusCode"] == 200

    def test_large_json_uses_lightweight_path(self, lambda_env):
        """Large JSON files use the same HEAD-only path."""
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 60 * 1024 * 1024}

        event = _make_s3_event("src-bucket", "pipeline-1/events.json")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.get_object.assert_not_called()
        assert result["source_format"] == "json"


# ── Legacy path tests ─────────────────────────────────────────────────────


class TestSmallFiles:
    """Small CSV/JSON files use the same HEAD-only path — no conversion."""

    def test_small_csv_no_conversion(self, lambda_env):
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 1024}

        event = _make_s3_event("src-bucket", "pipeline-1/data.csv")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        # No file read, no JSON conversion — HEAD only
        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["source_format"] == "csv"
        assert result["statusCode"] == 200

    def test_small_json_no_conversion(self, lambda_env):
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 512}

        event = _make_s3_event("src-bucket", "pipeline-1/data.json")
        ctx = MagicMock()

        result = lf.lambda_handler(event, ctx)

        mock_s3.get_object.assert_not_called()
        mock_s3.put_object.assert_not_called()
        assert result["source_format"] == "json"


# ── Unknown format tests ─────────────────────────────────────────────────


class TestUnknownFormat:
    """Unrecognized extensions fail the step, naming the unsupported format."""

    def test_unknown_extension_raises_step_failure(self, lambda_env):
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 1024}

        # p55 task 4.2: this was `pipeline-1/data.xlsx`. `xlsx` is now a
        # FORMAT_MAP key mapped to the Canonical_Format `excel` (R6.1), so
        # keeping it here would have inverted the test — it would assert that a
        # SUPPORTED format fails. `.pdf` is outside FORMAT_MAP, so the branch
        # under test is unchanged.
        event = _make_s3_event("src-bucket", "pipeline-1/data.pdf")
        ctx = MagicMock()

        # An unsupported extension is a permanent step failure (P28 R1.1:
        # failure paths raise instead of returning 500 payloads).
        #
        # This test predates the fix and used to pass for the WRONG reason: the
        # unsupported branch fell through to the success return, which reads
        # `file_size_bytes` — a name only the sibling `else` assigns — so the
        # StepFailure it observed was a wrapped `UnboundLocalError`. The
        # assertions below are unchanged and now hold for the right reason; the
        # message check is added so the unbound-variable route cannot come back
        # unnoticed. Full coverage of the path lives in
        # `test_unsupported_format_path.py`.
        with pytest.raises(lf.StepFailure) as exc_info:
            lf.lambda_handler(event, ctx)
        assert exc_info.value.step_name == "file_validation"
        assert "pdf" in exc_info.value.message
        assert "file_size_bytes" not in exc_info.value.message


# ── DynamoDB metadata tests ──────────────────────────────────────────────


class TestMetadataStorage:
    """Verify DynamoDB metadata is stored correctly for both paths."""

    def test_lightweight_path_stores_source_format(self, lambda_env):
        lf, mock_s3, mock_table = lambda_env
        mock_s3.head_object.return_value = {"ContentLength": 1_000_000}

        event = _make_s3_event("src-bucket", "pipeline-1/data.parquet")
        ctx = MagicMock()

        lf.lambda_handler(event, ctx)

        # Verify put_item was called with correct file_type
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["file_type"] == "parquet"
        assert item["status"] == "success"
        # No JSON conversion for native formats — json_s3_uri is absent or None
        assert item.get("json_s3_uri") is None
