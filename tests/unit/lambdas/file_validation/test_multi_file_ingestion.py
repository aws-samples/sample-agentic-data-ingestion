# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for multi-file ingestion: list_s3_files, handle_prefix_trigger, pagination."""

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
        yield lambda_function, mock_s3, mock_table


class TestListS3Files:
    """Tests for list_s3_files() — lists files in an S3 prefix."""

    def test_returns_correct_files(self, _mock_boto):
        lf, mock_s3, _ = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "data/file1.csv", "Size": 1000},
                {"Key": "data/file2.csv", "Size": 2000},
                {"Key": "data/file3.csv", "Size": 3000},
            ],
            "IsTruncated": False,
        }

        files = lf.list_s3_files("my-bucket", "data/")
        assert len(files) == 3
        assert files[0] == {"Key": "data/file1.csv", "Size": 1000}
        assert files[1] == {"Key": "data/file2.csv", "Size": 2000}
        assert files[2] == {"Key": "data/file3.csv", "Size": 3000}
        mock_s3.list_objects_v2.assert_called_once_with(Bucket="my-bucket", Prefix="data/")

    def test_filters_directory_markers(self, _mock_boto):
        lf, mock_s3, _ = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "data/", "Size": 0},
                {"Key": "data/subdir/", "Size": 0},
                {"Key": "data/file1.csv", "Size": 500},
            ],
            "IsTruncated": False,
        }

        files = lf.list_s3_files("my-bucket", "data/")
        assert len(files) == 1
        assert files[0]["Key"] == "data/file1.csv"

    def test_pagination_over_1000_files(self, _mock_boto):
        """Test pagination when S3 returns IsTruncated=True with ContinuationToken."""
        lf, mock_s3, _ = _mock_boto

        # First page: 1000 files, truncated
        page1_contents = [{"Key": f"data/file{i:04d}.csv", "Size": 100} for i in range(1000)]
        # Second page: 200 more files, not truncated
        page2_contents = [{"Key": f"data/file{i:04d}.csv", "Size": 100} for i in range(1000, 1200)]

        mock_s3.list_objects_v2.side_effect = [
            {
                "Contents": page1_contents,
                "IsTruncated": True,
                "NextContinuationToken": "token-page2",
            },
            {
                "Contents": page2_contents,
                "IsTruncated": False,
            },
        ]

        files = lf.list_s3_files("my-bucket", "data/")
        assert len(files) == 1200
        # Verify pagination was used
        assert mock_s3.list_objects_v2.call_count == 2
        second_call_kwargs = mock_s3.list_objects_v2.call_args_list[1][1]
        assert second_call_kwargs["ContinuationToken"] == "token-page2"

    def test_empty_prefix_returns_empty_list(self, _mock_boto):
        lf, mock_s3, _ = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "IsTruncated": False,
        }

        files = lf.list_s3_files("my-bucket", "empty-prefix/")
        assert files == []


class TestHandlePrefixTrigger:
    """Tests for handle_prefix_trigger() — prefix-based multi-file trigger."""

    def test_returns_correct_file_count_and_total_size(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "pipeline1/file1.csv", "Size": 1000},
                {"Key": "pipeline1/file2.csv", "Size": 2000},
                {"Key": "pipeline1/file3.csv", "Size": 3000},
            ],
            "IsTruncated": False,
        }

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "pipeline1/",
        }
        context = MagicMock()

        result = lf.handle_prefix_trigger(event, context)

        assert result["file_count"] == 3
        assert result["file_size_bytes"] == 6000
        assert result["source_s3_uri"] == "s3://my-bucket/pipeline1/"
        assert result["trigger_mode"] == "prefix"
        assert result["source_format"] == "csv"
        assert "run_id" in result

    def test_slashless_prefix_normalized_to_directory_uri(self, _mock_boto):
        """A bare pipeline-id prefix (the rerun-contract default) is
        normalized to directory form: schema_extract's prefix-mode detection
        keys on the trailing slash, and a slashless URI was treated as an
        object key (NoSuchKey — caught live in P28 task 17.2 verification on
        a self-healing rerun)."""
        lf, mock_s3, _ = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "pipeline1/file1.csv", "Size": 1000}],
            "IsTruncated": False,
        }

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "pipeline1",  # no trailing slash (rerun contract default)
        }

        result = lf.handle_prefix_trigger(event, MagicMock())

        assert result["source_s3_uri"] == "s3://my-bucket/pipeline1/"
        mock_s3.list_objects_v2.assert_called_once()
        assert mock_s3.list_objects_v2.call_args.kwargs["Prefix"] == "pipeline1/"

    def test_empty_prefix_raises_value_error(self, _mock_boto):
        lf, mock_s3, _ = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "IsTruncated": False,
        }

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "empty/",
        }
        context = MagicMock()

        with pytest.raises(ValueError, match="No files found"):
            lf.handle_prefix_trigger(event, context)

    def test_detects_format_from_first_file(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "data/records.parquet", "Size": 5000},
                {"Key": "data/records2.parquet", "Size": 8000},
            ],
            "IsTruncated": False,
        }

        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "data/",
        }
        context = MagicMock()

        result = lf.handle_prefix_trigger(event, context)
        assert result["source_format"] == "parquet"
        assert result["file_count"] == 2
        assert result["file_size_bytes"] == 13000


class TestPrefixTriggerPersistsTriggeredBy:
    """P28 R9.4 Heal_Guard: the prefix handler persists the SFN input's
    triggered_by on the run's file_validation state row. Heal reruns run in
    prefix mode and this handler generates a FRESH run_id — the marker on the
    identity row is how the self-healing agent later resolves the trigger
    from the fresh id.

    **Validates: Requirements 9.4**
    """

    def _event(self, **extras):
        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "pipeline1/",
        }
        event.update(extras)
        return event

    def _seed_files(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "pipeline1/file1.csv", "Size": 1000}],
            "IsTruncated": False,
        }

    def _stored_item(self, mock_table):
        assert mock_table.put_item.called, "put_item was never called"
        return mock_table.put_item.call_args.kwargs["Item"]

    def test_heal_rerun_marker_persisted(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(self._event(triggered_by="self_healing", trigger="self_healing"), MagicMock())

        item = self._stored_item(mock_table)
        assert item["step_name"] == "file_validation"
        assert item["triggered_by"] == "self_healing"

    def test_legacy_trigger_key_used_as_fallback(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(self._event(trigger="manual"), MagicMock())

        assert self._stored_item(mock_table)["triggered_by"] == "manual"

    def test_absent_trigger_leaves_attribute_off_row(self, _mock_boto):
        """Legacy events without any trigger marker → no attribute written."""
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(self._event(), MagicMock())

        assert "triggered_by" not in self._stored_item(mock_table)


class TestPrefixTriggerPersistsJobConfigOverrides:
    """P28 R9.5 WorkerType persistence: the prefix handler persists the SFN
    input's job_config_overrides on the run's file_validation state row —
    the delivery path create_glue_pipeline reads back by the FRESH run_id to
    merge the heal remediation over its generated job config. Heal reruns
    run in prefix mode, so this is the primary origin.

    **Validates: Requirements 9.5**
    """

    def _event(self, **extras):
        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "pipeline1/",
        }
        event.update(extras)
        return event

    def _seed_files(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "pipeline1/file1.csv", "Size": 1000}],
            "IsTruncated": False,
        }

    def _stored_item(self, mock_table):
        assert mock_table.put_item.called, "put_item was never called"
        return mock_table.put_item.call_args.kwargs["Item"]

    def test_heal_rerun_overrides_persisted(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(
            self._event(triggered_by="self_healing", job_config_overrides={"WorkerType": "G.2X"}),
            MagicMock(),
        )

        item = self._stored_item(mock_table)
        assert item["step_name"] == "file_validation"
        assert item["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_absent_overrides_leaves_attribute_off_row(self, _mock_boto):
        """Normal runs carry no overrides → no attribute written."""
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(self._event(), MagicMock())

        assert "job_config_overrides" not in self._stored_item(mock_table)


class TestPrefixTriggerPersistsOriginalRunId:
    """P28 R9.7 rerun-result linking: the prefix handler persists the SFN
    input's ``original_run_id`` on the run's file_validation state row —
    heal reruns run in prefix mode with a FRESH run_id, and this attribute
    (with ``triggered_by="self_healing"``) is how job_completion later
    resolves the ORIGINAL run whose healing record gets the rerun outcome.

    **Validates: Requirements 9.7**
    """

    def _event(self, **extras):
        event = {
            "trigger_mode": "prefix",
            "source_bucket": "my-bucket",
            "source_prefix": "pipeline1/",
        }
        event.update(extras)
        return event

    def _seed_files(self, mock_s3):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [{"Key": "pipeline1/file1.csv", "Size": 1000}],
            "IsTruncated": False,
        }

    def _stored_item(self, mock_table):
        assert mock_table.put_item.called, "put_item was never called"
        return mock_table.put_item.call_args.kwargs["Item"]

    def test_heal_rerun_original_run_id_persisted(self, _mock_boto):
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(
            self._event(triggered_by="self_healing", original_run_id="run-orig-1"),
            MagicMock(),
        )

        item = self._stored_item(mock_table)
        assert item["step_name"] == "file_validation"
        assert item["original_run_id"] == "run-orig-1"

    def test_absent_original_run_id_leaves_attribute_off_row(self, _mock_boto):
        """Normal runs carry no original_run_id → no attribute written."""
        lf, mock_s3, mock_table = _mock_boto
        self._seed_files(mock_s3)

        lf.handle_prefix_trigger(self._event(), MagicMock())

        assert "original_run_id" not in self._stored_item(mock_table)
