# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Keystone E2E test: CSV → S3 Tables (NEW_S3_TABLE_BUCKET) full pipeline run.

This is the canonical triggered e2e test. It:
1. Provisions a throwaway NEW_S3_TABLE_BUCKET pipeline
2. Uploads a uniquely-keyed CSV to the pipeline's source prefix
3. Locates the triggered execution via poll_execution_by_input
4. Releases both approval gates (schema_mapping + code_generation)
5. Waits for SUCCEEDED within the Cost_Budget
6. Verifies real target rows via the target_verifier

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 8.1
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
e2e-001,Alice,100
e2e-002,Bob,200
e2e-003,Charlie,300
e2e-004,Diana,400
"""

EXPECTED_ROWS = [
    {"id": "e2e-001", "name": "Alice", "value": "100"},
    {"id": "e2e-002", "name": "Bob", "value": "200"},
    {"id": "e2e-003", "name": "Charlie", "value": "300"},
    {"id": "e2e-004", "name": "Diana", "value": "400"},
]


@pytest.mark.e2e
@pytest.mark.slow
class TestCsvS3TablesRun:
    """Full pipeline run: CSV source → NEW_S3_TABLE_BUCKET target."""

    def test_csv_to_s3_tables_succeeds(
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
        """Provision → upload → approve gates → SUCCEEDED → verify rows.

        This test exercises the complete happy-path pipeline execution from
        file upload through Glue job completion and target data verification.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        logger.info("Provisioned pipeline: %s", pipeline.pipeline_id)

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
            # Release schema-mapping gate (required with default config)
            logger.info("Releasing schema_mapping gate...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="approve",
                timeout=cost_budget,
            )

            # Release code-generation gate (optional — may not trigger)
            logger.info("Releasing code_generation gate (optional)...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="code_generation",
                decision="approve",
                timeout=min(cost_budget, 120),  # short wait for optional gate
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
