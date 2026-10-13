# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: Approval reject path — reject at schema-mapping gate → FAILED/ABORTED.

Drives a pipeline run to the schema-mapping approval gate, submits a
rejection via the Approval_Driver, and asserts the execution reaches a
terminal FAILED or ABORTED state with no target data written.

Requirements: 13.1, 13.2
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from tests.e2e.conftest import find_execution_by_input, poll_execution_by_input

logger = logging.getLogger(__name__)

CSV_TEST_DATA = """\
id,name,value
reject-001,Alice,100
reject-002,Bob,200
"""


@pytest.mark.e2e
@pytest.mark.slow
class TestRejectPath:
    """Verify that rejecting at the schema-mapping gate stops the execution."""

    def test_reject_stops_execution_no_target_write(
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
        """Provision → upload → reject schema-mapping gate → FAILED/ABORTED + no target write.

        Req 13.1: Rejection at the gate stops the execution gracefully.
        Req 13.2: Terminal state is FAILED or ABORTED, and no target write occurred.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        logger.info("Provisioned pipeline: %s", pipeline.pipeline_id)

        # ── Step 2: Upload a uniquely-keyed CSV ───────────────────────
        unique_key = f"e2e-reject-{uuid.uuid4().hex[:12]}.csv"
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
        e2e_ledger.add_s3_object(source_bucket, s3_key)

        started_after = time.time() - 5

        # ── Step 3: Find the triggered execution (returns immediately) ─
        sfn_client = boto3_session.client("stepfunctions")
        state_machine_arn = resource_names["state_machine_arn"]

        status, execution_arn = find_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=120,
        )
        logger.info("Found execution: arn=%s, status=%s", execution_arn, status)
        e2e_ledger.add_execution(execution_arn)

        # ── Step 4: Reject the schema-mapping gate (while RUNNING) ────
        if status == "RUNNING":
            logger.info("Releasing schema_mapping gate with 'reject'...")
            result = approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="reject",
                timeout=cost_budget,
            )
            assert result is not None, "Schema-mapping gate release returned None"
            logger.info(
                "Schema-mapping gate rejected (method=%s, approval_id=%s)",
                result.method,
                result.approval_id,
            )

        # ── Step 5: Poll for terminal state after rejection ───────────
        logger.info("Polling for terminal state after rejection...")
        status, execution_arn = poll_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=cost_budget,
        )

        # ── Assert FAILED or ABORTED ──────────────────────────────────
        assert status in ("FAILED", "ABORTED"), (
            f"Rejected execution did not reach FAILED/ABORTED: status={status}, execution_arn={execution_arn}"
        )
        logger.info("Reject path verified: execution reached %s", status)

        # ── Assert no target data was written ─────────────────────────
        # The target_verifier should find no successful RunPipeline step
        # for this pipeline, confirming no target write occurred.
        from tests.e2e.conftest import latest_successful_runpipeline

        run_record = latest_successful_runpipeline(boto3_session, pipeline.pipeline_id)
        assert run_record is None, f"Target write detected after rejection — RunPipeline step found: {run_record}"
        logger.info(
            "No target write confirmed: no successful RunPipeline step for %s",
            pipeline.pipeline_id,
        )
