# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Target_Verifier — reads actual target stores to verify pipeline output.

Dispatches by target type to read rows from the real target data store and
assert that the expected rows are present. Supports:

- **RDS / JDBC**: Reads rows via the RDS Data API (HTTP, no VPC access needed).
- **S3 Parquet**: Lists objects under the target path, reads with pyarrow.
- **S3 Tables (Iceberg)**: Capability-detects Athena + S3 Tables federation;
  falls back to State-table ``verify_target_data`` with a logged warning.

The existing ``verify_target_data`` (State-table evidence check) is the
documented fallback for targets where direct reads are unavailable.

Usage (via the ``target_verifier`` fixture):

    def test_run(target_verifier, provisioner):
        pipeline = provisioner.create(target_type="S3_PARQUET", ...)
        # ... run pipeline to SUCCEEDED ...
        result = target_verifier.verify(
            pipeline,
            expected_rows=[{"id": "1", "name": "Alice", "value": "100"}],
            key_field="id",
        )
        assert result.success
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Result of a target verification attempt."""

    success: bool
    row_count: int = 0
    key_values: set = field(default_factory=set)
    method: str = ""  # e.g. "rds_data_api", "s3_parquet", "athena", "state_table_fallback"
    message: str = ""


class TargetVerifier:
    """Verifies pipeline target data by reading the actual target store.

    Args:
        boto3_session: An active boto3 Session (provides credentials + region).
        verify_target_data_fn: The existing State-table fallback function
            (from ``tests/e2e/conftest.py``).
    """

    def __init__(self, boto3_session: Any, verify_target_data_fn: Any = None) -> None:
        self._session = boto3_session
        self._verify_target_data_fn = verify_target_data_fn

    def verify(
        self,
        provisioned: Any,
        *,
        expected_rows: list[dict],
        key_field: str,
    ) -> VerifyResult:
        """Verify target data matches expected source rows.

        Dispatches to the appropriate backend based on ``provisioned.target_type``.

        Args:
            provisioned: A ``ProvisionedPipeline`` instance.
            expected_rows: The source rows that should have been written.
            key_field: The field name used to identify unique rows.

        Returns:
            A ``VerifyResult`` with success status and details.
        """
        expected_keys = {str(row[key_field]) for row in expected_rows}
        expected_count = len(expected_rows)

        target_type = provisioned.target_type
        target_config = provisioned.target_config

        if target_type == "RDS_DATABASE":
            result = self._verify_rds(target_config, key_field)
        elif target_type == "S3_PARQUET":
            result = self._verify_s3_parquet(target_config, key_field)
        elif target_type in ("NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET", "GLUE_CATALOG"):
            result = self._verify_s3_tables_or_fallback(provisioned, target_config, key_field)
        else:
            # Unknown target type — use the State-table fallback
            result = self._fallback_verify(provisioned)

        # Assert row count and key values match expected
        if result.method == "state_table_fallback":
            # Fallback does not read rows — success is based on evidence only
            return result

        # If the backend already set an error message (e.g. missing config),
        # do not override it with a comparison mismatch
        if result.message and not result.success:
            return result

        result.success = result.row_count == expected_count and result.key_values == expected_keys
        if not result.success:
            result.message = (
                f"Mismatch: expected {expected_count} rows with keys {expected_keys}, "
                f"got {result.row_count} rows with keys {result.key_values}"
            )

        return result

    # ── RDS Data API backend ──────────────────────────────────────────

    def _verify_rds(self, target_config: dict, key_field: str) -> VerifyResult:
        """Read rows from Aurora Postgres via the RDS Data API.

        Uses ``cluster_arn`` and ``secret_arn`` from target_config to execute
        a SELECT query over the HTTP endpoint (no VPC access needed).
        """
        cluster_arn = target_config["cluster_arn"]
        secret_arn = target_config["secret_arn"]
        database = target_config.get("database_name", "postgres")
        table = target_config.get("jdbc_table") or target_config.get("table_name", "target_table")

        rds_data = self._session.client("rds-data")

        # Count rows
        count_sql = f"SELECT COUNT(*) as cnt FROM {table}"
        count_resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=count_sql,
        )
        row_count = count_resp["records"][0][0].get("longValue", 0)

        # Get key values
        key_sql = f"SELECT CAST({key_field} AS VARCHAR) as k FROM {table}"
        key_resp = rds_data.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=key_sql,
        )
        key_values = {record[0].get("stringValue", "") for record in key_resp.get("records", [])}

        logger.info("RDS Data API verification: %d rows, keys=%s", row_count, key_values)

        return VerifyResult(
            success=False,  # set by caller after comparison
            row_count=row_count,
            key_values=key_values,
            method="rds_data_api",
        )

    # ── S3 Parquet backend ────────────────────────────────────────────

    def _verify_s3_parquet(self, target_config: dict, key_field: str) -> VerifyResult:
        """Read rows from S3 Parquet objects using pyarrow.

        Lists objects under ``target_config["output_path"]`` (or ``target_path``),
        reads each Parquet file, counts rows, and collects key values.
        """
        output_path = (
            target_config.get("s3_path") or target_config.get("output_path") or target_config.get("target_path", "")
        )
        if not output_path:
            return VerifyResult(
                success=False,
                method="s3_parquet",
                message="No s3_path, output_path, or target_path in target_config",
            )

        import pyarrow.parquet as pq

        # Parse s3://bucket/prefix from output_path
        bucket, prefix = self._parse_s3_path(output_path)

        s3 = self._session.client("s3")

        # List all parquet objects under the prefix
        parquet_keys = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".parquet") or key.endswith(".snappy.parquet"):
                    parquet_keys.append(key)

        if not parquet_keys:
            return VerifyResult(
                success=False,
                row_count=0,
                key_values=set(),
                method="s3_parquet",
                message=f"No Parquet files found under s3://{bucket}/{prefix}",
            )

        # Read each parquet file and collect rows
        total_rows = 0
        all_keys: set = set()

        for key in parquet_keys:
            resp = s3.get_object(Bucket=bucket, Key=key)
            body = resp["Body"].read()
            table = pq.read_table(io.BytesIO(body))
            total_rows += table.num_rows

            if key_field in table.column_names:
                column = table.column(key_field)
                all_keys.update(str(v) for v in column.to_pylist() if v is not None)

        logger.info(
            "S3 Parquet verification: %d rows across %d files, keys=%s",
            total_rows,
            len(parquet_keys),
            all_keys,
        )

        return VerifyResult(
            success=False,  # set by caller after comparison
            row_count=total_rows,
            key_values=all_keys,
            method="s3_parquet",
        )

    # ── S3 Tables / Athena backend (with capability detection) ────────

    def _verify_s3_tables_or_fallback(self, provisioned: Any, target_config: dict, key_field: str) -> VerifyResult:
        """Attempt Athena query against S3 Tables; fall back on failure.

        Capability-detects Athena + S3 Tables federation at runtime by
        attempting a probe query. If the catalog/workgroup is absent or
        a federation error occurs, falls back to the State-table verifier
        and logs that the fallback does not read target rows.
        """
        if self._athena_s3_tables_available(target_config):
            return self._verify_via_athena(target_config, key_field)
        else:
            logger.warning(
                "Athena/S3-Tables federation not available for target %s. "
                "Falling back to State-table verification. "
                "NOTE: The fallback does NOT read actual target rows — "
                "it verifies evidence of a successful write only.",
                provisioned.target_type,
            )
            return self._fallback_verify(provisioned)

    def _athena_s3_tables_available(self, target_config: dict) -> bool:
        """Probe whether Athena can query S3 Tables in this account.

        Attempts a lightweight probe query. Returns True if the query
        starts successfully (even if it returns zero rows), False if the
        catalog/workgroup is missing or a federation error occurs.
        """
        try:
            athena = self._session.client("athena")

            # Use S3 Tables catalog if available; default workgroup otherwise
            catalog = target_config.get("catalog_name", "s3tablescatalog")
            workgroup = target_config.get("workgroup", "primary")

            # Probe with a simple query that should succeed if federation is available
            response = athena.start_query_execution(
                QueryString="SELECT 1",
                QueryExecutionContext={"Catalog": catalog},
                WorkGroup=workgroup,
            )

            # Wait briefly for the probe to complete or fail
            import time

            query_id = response["QueryExecutionId"]
            for _ in range(10):
                status = athena.get_query_execution(QueryExecutionId=query_id)
                state = status["QueryExecution"]["Status"]["State"]
                if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                    break
                time.sleep(1)

            # If it succeeded, federation is available
            if state == "SUCCEEDED":
                logger.info("Athena S3-Tables probe succeeded — federation available")
                return True
            else:
                reason = status["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
                logger.info(
                    "Athena S3-Tables probe failed (state=%s, reason=%s) — federation not available",
                    state,
                    reason,
                )
                return False

        except Exception as exc:
            logger.info(
                "Athena S3-Tables capability detection failed: %s — falling back to State-table verification",
                exc,
            )
            return False

    def _verify_via_athena(self, target_config: dict, key_field: str) -> VerifyResult:
        """Query S3 Tables via Athena and verify rows.

        Executes a SELECT COUNT(*) and a key-value query against the
        S3 Tables catalog using the configured database/table names.
        """

        athena = self._session.client("athena")

        catalog = target_config.get("catalog_name", "s3tablescatalog")
        workgroup = target_config.get("workgroup", "primary")
        database = target_config.get("database_name", "default")
        table = target_config.get("table_name", "target_table")

        # Query row count
        count_sql = f'SELECT COUNT(*) FROM "{database}"."{table}"'
        row_count = self._run_athena_query_scalar(athena, count_sql, catalog, workgroup)

        # Query key values
        key_sql = f'SELECT CAST("{key_field}" AS VARCHAR) FROM "{database}"."{table}"'
        key_values = self._run_athena_query_column(athena, key_sql, catalog, workgroup)

        logger.info("Athena S3-Tables verification: %d rows, keys=%s", row_count, key_values)

        return VerifyResult(
            success=False,  # set by caller after comparison
            row_count=row_count,
            key_values=key_values,
            method="athena",
        )

    def _run_athena_query_scalar(self, athena: Any, sql: str, catalog: str, workgroup: str) -> int:
        """Execute an Athena query and return the first scalar int result."""
        import time

        response = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Catalog": catalog},
            WorkGroup=workgroup,
        )
        query_id = response["QueryExecutionId"]

        # Poll for completion
        for _ in range(60):
            status = athena.get_query_execution(QueryExecutionId=query_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(2)

        if state != "SUCCEEDED":
            return 0

        results = athena.get_query_results(QueryExecutionId=query_id)
        rows = results.get("ResultSet", {}).get("Rows", [])
        if len(rows) > 1:
            # First row is header, second is the value
            val = rows[1]["Data"][0].get("VarCharValue", "0")
            return int(val)
        return 0

    def _run_athena_query_column(self, athena: Any, sql: str, catalog: str, workgroup: str) -> set:
        """Execute an Athena query and return a set of string values from the first column."""
        import time

        response = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Catalog": catalog},
            WorkGroup=workgroup,
        )
        query_id = response["QueryExecutionId"]

        # Poll for completion
        for _ in range(60):
            status = athena.get_query_execution(QueryExecutionId=query_id)
            state = status["QueryExecution"]["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(2)

        if state != "SUCCEEDED":
            return set()

        results = athena.get_query_results(QueryExecutionId=query_id)
        rows = results.get("ResultSet", {}).get("Rows", [])
        # Skip header row
        values = set()
        for row in rows[1:]:
            val = row["Data"][0].get("VarCharValue")
            if val is not None:
                values.add(val)
        return values

    # ── State-table fallback ──────────────────────────────────────────

    def _fallback_verify(self, provisioned: Any) -> VerifyResult:
        """Use the existing verify_target_data State-table check.

        This does NOT read actual target rows — it verifies evidence of
        a successful write (job_run_id, completed_at, no error_message).
        """
        if self._verify_target_data_fn is None:
            return VerifyResult(
                success=False,
                method="state_table_fallback",
                message="No verify_target_data function provided",
            )

        try:
            output = self._verify_target_data_fn(provisioned.pipeline_id)
            return VerifyResult(
                success=True,
                method="state_table_fallback",
                message=(
                    "State-table fallback: verified write evidence "
                    f"(job_run_id={output.get('job_run_id')}). "
                    "NOTE: This fallback does NOT read actual target rows."
                ),
            )
        except AssertionError as exc:
            return VerifyResult(
                success=False,
                method="state_table_fallback",
                message=f"State-table fallback failed: {exc}",
            )

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_s3_path(s3_path: str) -> tuple[str, str]:
        """Parse 's3://bucket/prefix' into (bucket, prefix)."""
        path = s3_path
        if path.startswith("s3://"):
            path = path[5:]
        parts = path.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        return bucket, prefix
