# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test: Approval approve path — approve at schema-mapping gate → SUCCEEDED.

Drives a pipeline run to the schema-mapping approval gate, submits an
approval via the Approval_Driver, and asserts the execution resumes and
reaches SUCCEEDED within the Cost_Budget.

Requirements: 12.1, 12.2
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
approve-001,Alice,100
approve-002,Bob,200
"""


@pytest.mark.e2e
@pytest.mark.slow
class TestApprovePath:
    """Verify that approving at the schema-mapping gate resumes the execution."""

    def test_approve_resumes_and_succeeds(
        self,
        requires_trigger,
        e2e_ledger,
        provisioner,
        approval_driver,
        cost_budget,
        boto3_session,
        resource_names,
    ):
        """Provision → upload → approve schema-mapping gate → SUCCEEDED.

        Req 12.1: Approval at the gate resumes the execution.
        Req 12.2: The approved execution finishes with status SUCCEEDED.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        logger.info("Provisioned pipeline: %s", pipeline.pipeline_id)

        # ── Step 2: Upload a uniquely-keyed CSV ───────────────────────
        unique_key = f"e2e-approve-{uuid.uuid4().hex[:12]}.csv"
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

        # ── Step 4: Approve the schema-mapping gate (while RUNNING) ───
        if status == "RUNNING":
            logger.info("Releasing schema_mapping gate with 'approve'...")
            result = approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="schema_mapping",
                decision="approve",
                timeout=cost_budget,
            )
            assert result is not None, "Schema-mapping gate release returned None"
            logger.info(
                "Schema-mapping gate approved (method=%s, approval_id=%s)",
                result.method,
                result.approval_id,
            )

            # Release optional code-generation gate
            logger.info("Releasing code_generation gate (optional)...")
            approval_driver.release_gate(
                pipeline_id=pipeline.pipeline_id,
                approval_type="code_generation",
                decision="approve",
                timeout=min(cost_budget, 120),
                optional=True,
            )

        # ── Step 5: Poll for terminal state after approval ────────────
        logger.info("Polling for terminal state after approval...")
        status, execution_arn = poll_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=cost_budget,
        )

        # ── Assert SUCCEEDED ──────────────────────────────────────────
        assert status == "SUCCEEDED", (
            f"Approved execution did not succeed: status={status}, execution_arn={execution_arn}"
        )
        logger.info("Approve path verified: execution reached SUCCEEDED")
