# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for data_preview format-specific readers — CSV S3 Select,
CSV fallback, JSON Range GET, Parquet PyArrow, size guard, unsupported
format, and S3 access errors.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(response: dict) -> tuple[int, dict]:
    return response["statusCode"], json.loads(response["body"])


PIPELINE_ID = "pipe-fmt-001"
RUN_ID = "run-fmt-abc"


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    """Patch module-level boto resources."""
    from handlers import common as _common
    from handlers import data_preview as _dp

    mock_state = MagicMock()
    mock_exec = MagicMock()
    mock_s3 = MagicMock()

    monkeypatch.setattr(_common, "state_table", mock_state)
    monkeypatch.setattr(_common, "executions_table", mock_exec)
    monkeypatch.setattr(_common, "s3_client", mock_s3)
    monkeypatch.setattr(_dp, "state_table", mock_state)
    monkeypatch.setattr(_dp, "executions_table", mock_exec)
    monkeypatch.setattr(_dp, "s3_client", mock_s3)

    _patch_boto.state_table = mock_state
    _patch_boto.executions_table = mock_exec
    _patch_boto.s3_client = mock_s3
    yield


def _set_state_items(items: list):
    _patch_boto.state_table.query.return_value = {"Items": items}


def _make_s3_body(content: str):
    body = MagicMock()
    body.read.return_value = content.encode("utf-8")
    return body


# ---------------------------------------------------------------------------
# Import handlers under test
# ---------------------------------------------------------------------------
from handlers.data_preview import (
    _read_csv_preview,
    _read_json_preview,
    get_data_preview,
)

# ===========================================================================
# CSV via bounded Range GET
# ===========================================================================


class TestCsvPreview:
    """AMENDED when the S3 Select arm was removed from ``_read_csv_preview``.

    Stated here rather than silently rewritten, per this repo's convention that a
    deliberate removal stays legible as a removal (``LGR-60c4985-6`` /
    ``LGR-9e76249-10``). Three tests stood here before:

    * ``TestCsvS3Select::test_csv_s3_select_returns_rows`` — mocked
      ``select_object_content`` into succeeding and asserted the rows it produced.
      **Deleted**: it exercised a path that cannot execute in any AWS account
      created after 2024-07-25, when S3 Select closed to new customers, and its
      passing was the reason nobody noticed. This is the test whose green tick
      hid the defect.
    * ``TestCsvFallback::test_csv_fallback_parses_csv`` — called the private
      ``_read_csv_fallback`` directly. **Kept as behaviour, retargeted** onto
      ``_read_csv_preview``, which is now that same reader under the name the
      dispatcher calls. ``_read_csv_fallback`` no longer exists.
    * ``TestCsvFallback::test_csv_s3_select_falls_back_on_error`` — asserted the
      fallback fired when S3 Select raised. **Deleted**: there is no longer a
      fallback to fall back to, and a bare ``except Exception`` returning a second
      read is precisely what was removed.
    """

    def test_csv_preview_parses_rows_from_a_bounded_range_get(self):
        csv_content = "name,age\nAlice,30\nBob,25\n"
        _patch_boto.s3_client.get_object.return_value = {"Body": _make_s3_body(csv_content)}
        result = _read_csv_preview("bucket", "data.csv", 10)
        assert result["columns"] == ["name", "age"]
        assert len(result["rows"]) == 2
        assert result["rows"][0]["name"] == "Alice"

    def test_the_read_is_bounded_and_never_whole_object(self):
        """The Range header is the bound. Asserted because it is the whole reason
        a preview of a multi-GB object is safe in a Lambda."""
        _patch_boto.s3_client.get_object.return_value = {"Body": _make_s3_body("a,b\n1,2\n")}
        _read_csv_preview("bucket", "data.csv", 10)
        kwargs = _patch_boto.s3_client.get_object.call_args.kwargs
        assert kwargs["Range"] == "bytes=0-262143"
        assert kwargs["Bucket"] == "bucket"
        assert kwargs["Key"] == "data.csv"

    def test_limit_caps_the_rows_returned(self):
        _patch_boto.s3_client.get_object.return_value = {
            "Body": _make_s3_body("id\n1\n2\n3\n4\n5\n"),
        }
        result = _read_csv_preview("bucket", "data.csv", 3)
        assert len(result["rows"]) == 3

    def test_a_read_failure_propagates_instead_of_being_swallowed(self):
        """The removed arm caught every exception in a bare ``except`` and retried,
        so a missing object or a permission error was reported as whatever the
        retry then failed on. Now the real cause reaches the caller, which maps it
        to a 404 with the object named."""
        _patch_boto.s3_client.get_object.side_effect = Exception("NoSuchKey")
        with pytest.raises(Exception, match="NoSuchKey"):
            _read_csv_preview("bucket", "missing.csv", 10)


# ===========================================================================
# JSON Range GET
# ===========================================================================


class TestJsonRangeGet:
    def test_json_array_parsed(self):
        json_content = '[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]'
        _patch_boto.s3_client.get_object.return_value = {"Body": _make_s3_body(json_content)}
        result = _read_json_preview("bucket", "data.json", 10)
        assert result["columns"] == ["id", "name"]
        assert len(result["rows"]) == 2

    def test_jsonl_parsed(self):
        jsonl_content = '{"id": 1}\n{"id": 2}\n{"id": 3}\n'
        _patch_boto.s3_client.get_object.return_value = {"Body": _make_s3_body(jsonl_content)}
        result = _read_json_preview("bucket", "data.json", 2)
        assert len(result["rows"]) == 2


# ===========================================================================
# Parquet PyArrow
# ===========================================================================


class TestParquetPyArrow:
    def test_parquet_reads_first_row_group(self):
        """Parquet reader downloads, reads first row group, returns rows."""
        from unittest.mock import MagicMock, patch

        _patch_boto.s3_client.head_object.return_value = {"ContentLength": 1024}
        _patch_boto.s3_client.download_file.side_effect = None

        # Build a mock pyarrow chain
        mock_table = MagicMock()
        mock_table.to_pydict.return_value = {"x": [1, 2, 3], "y": ["a", "b", "c"]}
        mock_table.slice.return_value = mock_table

        mock_pf = MagicMock()
        mock_pf.read_row_group.return_value = mock_table

        mock_pq_module = MagicMock()
        mock_pq_module.ParquetFile.return_value = mock_pf

        # The parent mock's .parquet attr must BE the same object as sys.modules entry
        mock_pyarrow = MagicMock()
        mock_pyarrow.parquet = mock_pq_module

        with patch.dict(
            "sys.modules",
            {
                "pyarrow": mock_pyarrow,
                "pyarrow.parquet": mock_pq_module,
            },
        ):
            from handlers import data_preview as _dp

            _dp.s3_client = _patch_boto.s3_client
            result = _dp._read_parquet_preview("bucket", "data.parquet", 10)

        assert set(result["columns"]) == {"x", "y"}
        assert len(result["rows"]) == 3
        assert result["rows"][0]["x"] == 1


# ===========================================================================
# Parquet size guard (>500MB returns 400)
# ===========================================================================


class TestParquetSizeGuard:
    def test_large_file_returns_400(self):
        """Files > 500MB are rejected."""
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/huge.parquet"}]
        )
        _patch_boto.s3_client.head_object.return_value = {
            "ContentLength": 600 * 1024 * 1024  # 600MB
        }
        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {}))
        assert status == 400
        assert "too large" in body["error"].lower()


# ===========================================================================
# Unsupported format → 400
# ===========================================================================


class TestUnsupportedFormat:
    def test_unsupported_extension_returns_400(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.xlsx"}]
        )
        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {}))
        assert status == 400
        assert "unsupported" in body["error"].lower()


# ===========================================================================
# S3 access error → 404
# ===========================================================================


class TestS3AccessError:
    def test_s3_error_returns_404(self):
        _set_state_items(
            [{"step_name": "file_validation", "run_id": RUN_ID, "original_s3_uri": "s3://bucket/data.json"}]
        )
        _patch_boto.s3_client.get_object.side_effect = Exception("AccessDenied")
        status, body = _resp(get_data_preview(PIPELINE_ID, RUN_ID, {}))
        assert status == 404
        assert "not accessible" in body["error"].lower()
