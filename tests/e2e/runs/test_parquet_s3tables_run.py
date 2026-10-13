# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: Parquet → S3 Tables (NEW_S3_TABLE_BUCKET) full pipeline run.

Exercises the same end-to-end path as the keystone CSV test but with a
Parquet source file generated in-memory via pyarrow. Validates that schema
extraction, mapping, code generation, and target write work correctly for
columnar Parquet input.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

from __future__ import annotations

import io
import logging
import time
import uuid

import pytest
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

# ── Parquet test data — 4 rows matching the keystone schema ───────────

PARQUET_ROWS = [
    {"id": "e2e-p01", "name": "Alpha", "value": 110},
    {"id": "e2e-p02", "name": "Bravo", "value": 220},
    {"id": "e2e-p03", "name": "Charlie", "value": 330},
    {"id": "e2e-p04", "name": "Delta", "value": 440},
]

EXPECTED_ROWS = [
    {"id": "e2e-p01", "name": "Alpha", "value": "110"},
    {"id": "e2e-p02", "name": "Bravo", "value": "220"},
    {"id": "e2e-p03", "name": "Charlie", "value": "330"},
    {"id": "e2e-p04", "name": "Delta", "value": "440"},
]


def _build_parquet_bytes() -> bytes:
    """Create a Parquet file in-memory with the test rows."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.table(
        {
            "id": [r["id"] for r in PARQUET_ROWS],
            "name": [r["name"] for r in PARQUET_ROWS],
            "value": [r["value"] for r in PARQUET_ROWS],
        }
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


@pytest.mark.e2e
@pytest.mark.slow
class TestParquetS3TablesRun:
    """Full pipeline run: Parquet source → NEW_S3_TABLE_BUCKET target."""

    def test_parquet_to_s3_tables_succeeds(
        self,
        requires_trigger,
        e2e_ledger,
        provisioner,
        approval_driver,
        target_verifier,
        cost_budget,
        boto3_session,
        resource_names,
    ):
        """Provision → upload Parquet → approve gates → SUCCEEDED → verify rows.

        Validates that the pipeline correctly handles Parquet source files,
        extracts the schema from columnar metadata, and writes target data.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        logger.info("Provisioned pipeline: %s", pipeline.pipeline_id)

        # ── Step 2: Upload a uniquely-keyed Parquet file ──────────────
        unique_key = f"e2e-{uuid.uuid4().hex[:12]}.parquet"
        source_bucket = resource_names["source_bucket"]
        s3_key = f"{pipeline.pipeline_id}/{unique_key}"

        parquet_bytes = _build_parquet_bytes()

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=parquet_bytes,
            ContentType="application/octet-stream",
        )
        logger.info("Uploaded source file: s3://%s/%s", source_bucket, s3_key)

        # Register the S3 object for cleanup
        e2e_ledger.add_s3_object(source_bucket, s3_key)

        # Record the upload timestamp for execution polling
        started_after = time.time() - 5  # small lookback buffer

        # ── Step 3: Find the triggered execution (returns immediately) ─
        sfn_client = boto3_session.client("stepfunctions")
        state_machine_arn = resource_names["state_machine_arn"]

        logger.info(
            "Finding execution: pipeline_id=%s, state_machine=%s",
            pipeline.pipeline_id,
            state_machine_arn,
        )

        status, execution_arn = find_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=120,
        )

        logger.info("Found execution: arn=%s, status=%s", execution_arn, status)

        # Register the execution for cleanup (stop if still RUNNING)
        e2e_ledger.add_execution(execution_arn)

        # ── Step 4: Release approval gates (while execution is RUNNING) ─
        if status == "RUNNING":
            logger.info("Releasing schema_mapping gate...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="approve",
                timeout=cost_budget,
            )

            logger.info("Releasing code_generation gate (optional)...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="code_generation",
                decision="approve",
                timeout=min(cost_budget, 120),
                optional=True,
            )

        # ── Step 5: Poll for terminal state after gates released ──────
        logger.info("Polling for terminal state after gate release...")
        status, execution_arn = poll_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=cost_budget,
        )
        logger.info("Final execution status: %s", status)

        # Assert the pipeline succeeded
        assert status == "SUCCEEDED", (
            f"Pipeline execution did not succeed: status={status}, execution_arn={execution_arn}"
        )

        # ── Step 6: Verify target rows ────────────────────────────────
        logger.info("Verifying target data for pipeline %s", pipeline.pipeline_id)
        result = target_verifier.verify(
            pipeline,
            expected_rows=EXPECTED_ROWS,
            key_field="id",
        )

        assert result.success, f"Target verification failed (method={result.method}): {result.message}"
        logger.info(
            "Target verification passed: method=%s, rows=%d",
            result.method,
            result.row_count,
        )
