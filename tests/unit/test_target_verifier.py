# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the TargetVerifier capability-detection and fallback.

Validates:
- Athena capability detection falls back when probe fails
- Athena capability detection succeeds when probe succeeds
- RDS Data API verification dispatches correctly
- S3 Parquet verification dispatches correctly
- Fallback to verify_target_data is used for unknown targets

These tests use NO real AWS resources — all boto3 calls are mocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from tests.e2e._harness.verify import TargetVerifier


@dataclass
class FakeProvisionedPipeline:
    """Minimal stand-in for ProvisionedPipeline in unit tests."""

    pipeline_id: str
    target_type: str
    target_config: dict


class TestAthenaCapabilityDetection:
    """Verify the Athena S3-Tables capability detection probe."""

    def test_fallback_when_athena_probe_fails_with_exception(self):
        """When start_query_execution raises, capability detection returns False
        and the verifier falls back to State-table verification."""
        mock_session = MagicMock()
        mock_athena = MagicMock()
        mock_session.client.return_value = mock_athena
        mock_athena.start_query_execution.side_effect = Exception("Catalog s3tablescatalog not found")

        fallback_called = []

        def fake_verify_target_data(pipeline_id, *, run_id=None):
            fallback_called.append(pipeline_id)
            return {"job_run_id": "jr-123", "completed_at": "2024-01-01T00:00:00Z"}

        verifier = TargetVerifier(
            boto3_session=mock_session,
            verify_target_data_fn=fake_verify_target_data,
        )

        pipeline = FakeProvisionedPipeline(
            pipeline_id="test-pipe-1",
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1", "name": "Alice", "value": "100"}],
            key_field="id",
        )

        assert result.method == "state_table_fallback"
        assert result.success is True
        assert "test-pipe-1" in fallback_called

    def test_fallback_when_athena_probe_state_is_failed(self):
        """When the probe query execution reaches FAILED state, capability
        detection returns False and the verifier uses the State-table fallback."""
        mock_session = MagicMock()
        mock_athena = MagicMock()
        mock_session.client.return_value = mock_athena

        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qe-probe-1"}
        mock_athena.get_query_execution.return_value = {
            "QueryExecution": {
                "Status": {
                    "State": "FAILED",
                    "StateChangeReason": "INVALID_CATALOG: Catalog not found",
                }
            }
        }

        fallback_called = []

        def fake_verify_target_data(pipeline_id, *, run_id=None):
            fallback_called.append(pipeline_id)
            return {"job_run_id": "jr-456", "completed_at": "2024-01-01T00:00:00Z"}

        verifier = TargetVerifier(
            boto3_session=mock_session,
            verify_target_data_fn=fake_verify_target_data,
        )

        pipeline = FakeProvisionedPipeline(
            pipeline_id="test-pipe-2",
            target_type="GLUE_CATALOG",
            target_config={"type": "GLUE_CATALOG", "database_name": "mydb", "table_name": "t"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1", "name": "Bob", "value": "200"}],
            key_field="id",
        )

        assert result.method == "state_table_fallback"
        assert result.success is True
        assert "test-pipe-2" in fallback_called

    def test_athena_probe_succeeds_selects_athena_backend(self):
        """When the probe query succeeds, the Athena backend is selected
        for the actual verification queries."""
        mock_session = MagicMock()
        mock_athena = MagicMock()
        mock_session.client.return_value = mock_athena

        # Probe succeeds
        mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qe-probe-ok"}
        mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        # Count query returns 2 rows
        mock_athena.get_query_results.side_effect = [
            # Count query result
            {
                "ResultSet": {
                    "Rows": [
                        {"Data": [{"VarCharValue": "cnt"}]},  # header
                        {"Data": [{"VarCharValue": "2"}]},  # value
                    ]
                }
            },
            # Key query result
            {
                "ResultSet": {
                    "Rows": [
                        {"Data": [{"VarCharValue": "id"}]},  # header
                        {"Data": [{"VarCharValue": "1"}]},
                        {"Data": [{"VarCharValue": "2"}]},
                    ]
                }
            },
        ]

        verifier = TargetVerifier(boto3_session=mock_session)

        pipeline = FakeProvisionedPipeline(
            pipeline_id="test-pipe-3",
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={
                "type": "NEW_S3_TABLE_BUCKET",
                "database_name": "testdb",
                "table_name": "output",
            },
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[
                {"id": "1", "name": "Alice", "value": "100"},
                {"id": "2", "name": "Bob", "value": "200"},
            ],
            key_field="id",
        )

        assert result.method == "athena"
        assert result.row_count == 2
        assert result.key_values == {"1", "2"}
        assert result.success is True


class TestRDSDataAPIVerification:
    """Verify the RDS Data API verification backend."""

    def test_rds_verify_dispatches_correctly(self):
        """RDS_DATABASE target uses the rds-data client."""
        mock_session = MagicMock()
        mock_rds_data = MagicMock()
        mock_session.client.return_value = mock_rds_data

        # Count query
        mock_rds_data.execute_statement.side_effect = [
            {"records": [[{"longValue": 2}]]},
            {
                "records": [
                    [{"stringValue": "k1"}],
                    [{"stringValue": "k2"}],
                ]
            },
        ]

        verifier = TargetVerifier(boto3_session=mock_session)

        pipeline = FakeProvisionedPipeline(
            pipeline_id="rds-pipe-1",
            target_type="RDS_DATABASE",
            target_config={
                "type": "RDS_DATABASE",
                "cluster_arn": "arn:aws:rds:us-west-2:123:cluster:test-cluster",
                "secret_arn": "arn:aws:secretsmanager:us-west-2:123:secret:test-secret",
                "database_name": "mydb",
                "table_name": "output_table",
            },
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[
                {"id": "k1", "name": "A", "value": "1"},
                {"id": "k2", "name": "B", "value": "2"},
            ],
            key_field="id",
        )

        assert result.method == "rds_data_api"
        assert result.row_count == 2
        assert result.key_values == {"k1", "k2"}
        assert result.success is True

        # Verify correct client was created
        mock_session.client.assert_called_with("rds-data")


class TestFallbackVerification:
    """Verify the State-table fallback path."""

    def test_unknown_target_type_uses_fallback(self):
        """An unrecognized target type falls back to State-table verification."""
        mock_session = MagicMock()

        def fake_verify(pipeline_id, *, run_id=None):
            return {"job_run_id": "jr-999", "completed_at": "2024-01-01"}

        verifier = TargetVerifier(
            boto3_session=mock_session,
            verify_target_data_fn=fake_verify,
        )

        pipeline = FakeProvisionedPipeline(
            pipeline_id="unknown-pipe",
            target_type="UNKNOWN_TYPE",
            target_config={"type": "UNKNOWN_TYPE"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1"}],
            key_field="id",
        )

        assert result.method == "state_table_fallback"
        assert result.success is True
        assert "does NOT read actual target rows" in result.message

    def test_fallback_fails_when_no_fn_provided(self):
        """If no verify_target_data function is available, fallback returns failure."""
        mock_session = MagicMock()

        verifier = TargetVerifier(boto3_session=mock_session, verify_target_data_fn=None)

        pipeline = FakeProvisionedPipeline(
            pipeline_id="no-fn-pipe",
            target_type="UNKNOWN_TYPE",
            target_config={"type": "UNKNOWN_TYPE"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1"}],
            key_field="id",
        )

        assert result.method == "state_table_fallback"
        assert result.success is False
        assert "No verify_target_data function" in result.message

    def test_fallback_fails_when_verify_raises(self):
        """If verify_target_data raises AssertionError, fallback reports failure."""
        mock_session = MagicMock()

        def failing_verify(pipeline_id, *, run_id=None):
            raise AssertionError(f"No RunPipeline step for {pipeline_id}")

        verifier = TargetVerifier(
            boto3_session=mock_session,
            verify_target_data_fn=failing_verify,
        )

        pipeline = FakeProvisionedPipeline(
            pipeline_id="fail-pipe",
            target_type="UNKNOWN_TYPE",
            target_config={"type": "UNKNOWN_TYPE"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1"}],
            key_field="id",
        )

        assert result.method == "state_table_fallback"
        assert result.success is False
        assert "failed" in result.message


class TestS3ParquetVerification:
    """Verify the S3 Parquet verification backend dispatches correctly."""

    def test_s3_parquet_no_output_path_returns_failure(self):
        """If no output_path is configured, returns a failure result."""
        mock_session = MagicMock()

        verifier = TargetVerifier(boto3_session=mock_session)

        pipeline = FakeProvisionedPipeline(
            pipeline_id="parquet-pipe",
            target_type="S3_PARQUET",
            target_config={"type": "S3_PARQUET"},
        )

        result = verifier.verify(
            pipeline,
            expected_rows=[{"id": "1"}],
            key_field="id",
        )

        assert result.method == "s3_parquet"
        assert result.success is False
        assert "No s3_path, output_path, or target_path" in result.message


class TestParseS3Path:
    """Verify the S3 path parsing helper."""

    def test_parse_s3_uri(self):
        bucket, prefix = TargetVerifier._parse_s3_path("s3://my-bucket/path/to/data")
        assert bucket == "my-bucket"
        assert prefix == "path/to/data"

    def test_parse_bare_path(self):
        bucket, prefix = TargetVerifier._parse_s3_path("my-bucket/prefix")
        assert bucket == "my-bucket"
        assert prefix == "prefix"

    def test_parse_bucket_only(self):
        bucket, prefix = TargetVerifier._parse_s3_path("s3://my-bucket")
        assert bucket == "my-bucket"
        assert prefix == ""
