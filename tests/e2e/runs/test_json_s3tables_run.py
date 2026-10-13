# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: nested JSON → S3 Tables (NEW_S3_TABLE_BUCKET) full pipeline run.

Exercises the same end-to-end path as the keystone CSV test but with a
JSON Lines source containing nested fields. Validates that schema extraction
handles nested structures and the pipeline drives through to target write.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import pytest
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

# ── JSON test data — 4 rows with nested fields ────────────────────────

JSON_TEST_ROWS = [
    {
        "id": "e2e-j01",
        "name": "Echo",
        "value": 150,
        "metadata": {"source": "api", "priority": "high"},
    },
    {
        "id": "e2e-j02",
        "name": "Foxtrot",
        "value": 250,
        "metadata": {"source": "batch", "priority": "medium"},
    },
    {
        "id": "e2e-j03",
        "name": "Golf",
        "value": 350,
        "metadata": {"source": "stream", "priority": "low"},
    },
    {
        "id": "e2e-j04",
        "name": "Hotel",
        "value": 450,
        "metadata": {"source": "api", "priority": "high"},
    },
]

# Expected rows for verification — values stringified as the pipeline may
# coerce types; nested metadata may be flattened or serialized depending on
# the generated Glue job.
EXPECTED_ROWS = [
    {"id": "e2e-j01", "name": "Echo", "value": "150"},
    {"id": "e2e-j02", "name": "Foxtrot", "value": "250"},
    {"id": "e2e-j03", "name": "Golf", "value": "350"},
    {"id": "e2e-j04", "name": "Hotel", "value": "450"},
]


def _build_jsonl_bytes() -> bytes:
    """Create a JSON Lines (newline-delimited JSON) payload."""
    lines = [json.dumps(row) for row in JSON_TEST_ROWS]
    return "\n".join(lines).encode("utf-8")


@pytest.mark.e2e
@pytest.mark.slow
class TestJsonS3TablesRun:
    """Full pipeline run: nested JSON source → NEW_S3_TABLE_BUCKET target."""

    def test_json_to_s3_tables_succeeds(
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
        """Provision → upload JSON → approve gates → SUCCEEDED → verify rows.

        Validates that the pipeline correctly handles nested JSON source files,
        extracts the schema (including nested fields), and writes target data.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        logger.info("Provisioned pipeline: %s", pipeline.pipeline_id)

        # ── Step 2: Upload a uniquely-keyed JSON Lines file ───────────
        unique_key = f"e2e-{uuid.uuid4().hex[:12]}.json"
        source_bucket = resource_names["source_bucket"]
        s3_key = f"{pipeline.pipeline_id}/{unique_key}"

        jsonl_bytes = _build_jsonl_bytes()

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=jsonl_bytes,
            ContentType="application/json",
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
