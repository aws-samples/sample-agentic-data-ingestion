# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: CSV → S3 Parquet (S3_PARQUET) full pipeline run.

Provisions an S3_PARQUET pipeline targeting a unique prefix under the
processed bucket, runs to SUCCEEDED, verifies the expected Parquet
objects/rows via pyarrow, then tears down the provisioned pipeline.

Requirements: 21.1, 21.2, 21.3, 21.4
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

# ── CSV test data — 4 rows with unique IDs for verification ───────────

CSV_TEST_DATA = """\
id,name,value
e2e-pq-001,Echo,50
e2e-pq-002,Foxtrot,60
e2e-pq-003,Golf,70
e2e-pq-004,Hotel,80
"""

EXPECTED_ROWS = [
    {"id": "e2e-pq-001", "name": "Echo", "value": "50"},
    {"id": "e2e-pq-002", "name": "Foxtrot", "value": "60"},
    {"id": "e2e-pq-003", "name": "Golf", "value": "70"},
    {"id": "e2e-pq-004", "name": "Hotel", "value": "80"},
]


@pytest.mark.e2e
@pytest.mark.slow
class TestS3ParquetTarget:
    """Full pipeline run: CSV source → S3_PARQUET target."""

    def test_s3_parquet_run_succeeds(
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
        """Provision → upload → approve gates → SUCCEEDED → verify Parquet → teardown.

        This test exercises a pipeline targeting S3 Parquet output, which is
        a target type not currently deployed in the shared dev environment.
        """
        # ── Step 1: Provision an S3 Parquet pipeline ──────────────────
        unique_suffix = uuid.uuid4().hex[:8]
        processed_bucket = resource_names["processed_bucket"]
        output_path = f"s3://{processed_bucket}/e2e-parquet-target/{unique_suffix}/"

        target_config = {
            "type": "S3_PARQUET",
            "s3_path": output_path,
        }

        pipeline = provisioner.create(
            target_type="S3_PARQUET",
            target_config=target_config,
        )
        logger.info(
            "Provisioned S3_PARQUET pipeline: %s (output_path=%s)",
            pipeline.pipeline_id,
            output_path,
        )

        # Register S3 target prefix for cleanup (delete all objects under it)
        def _cleanup_s3_parquet_target():
            s3 = boto3_session.client("s3")
            prefix = f"e2e-parquet-target/{unique_suffix}/"
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=processed_bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    s3.delete_object(Bucket=processed_bucket, Key=obj["Key"])
                    logger.debug("Deleted target object: s3://%s/%s", processed_bucket, obj["Key"])
            logger.info("Cleaned up S3 Parquet target prefix: %s", prefix)

        e2e_ledger.add_callable(_cleanup_s3_parquet_target)

        # ── Step 2: Upload a uniquely-keyed CSV to the source prefix ──
        unique_key = f"e2e-{uuid.uuid4().hex[:12]}.csv"
        source_bucket = resource_names["source_bucket"]
        s3_key = f"{pipeline.pipeline_id}/{unique_key}"

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=CSV_TEST_DATA.encode("utf-8"),
            ContentType="text/csv",
        )
        logger.info("Uploaded source file: s3://%s/%s", source_bucket, s3_key)

        # Register the S3 object for cleanup
        e2e_ledger.add_s3_object(source_bucket, s3_key)

        # Record upload timestamp for execution polling
        started_after = time.time() - 5

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
            f"S3_PARQUET pipeline execution did not succeed: status={status}, execution_arn={execution_arn}"
        )

        # ── Step 6: Verify Parquet target rows ────────────────────────
        logger.info("Verifying S3 Parquet target data for pipeline %s", pipeline.pipeline_id)
        result = target_verifier.verify(
            pipeline,
            expected_rows=EXPECTED_ROWS,
            key_field="id",
        )

        assert result.success, f"S3_PARQUET target verification failed (method={result.method}): {result.message}"
        logger.info(
            "Target verification passed: method=%s, rows=%d, files verified via pyarrow",
            result.method,
            result.row_count,
        )
