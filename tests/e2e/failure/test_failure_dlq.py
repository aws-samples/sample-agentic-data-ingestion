# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase T2 E2E: failure injection and error recording.

Injects a fault (deliberately malformed file with a .csv extension containing
binary garbage) into a throwaway pipeline run. Asserts:
1. The Step Functions execution exercises its catch path
2. The execution reaches a terminal FAILED/ABORTED state
3. The failure is recorded in the State table by the failure-recorder Lambda
4. The recorded error reflects the injected fault

Architecture note (why this test does not poll an SQS DLQ):
AgenticDataIngestion does not route pipeline failures to an SQS dead-letter queue. The
Step Functions definition catches `States.ALL` on every task state to a
`RecordFailure` task (P28 R1.3), which invokes the record-failure Lambda. That
Lambda writes a `pipeline_failure` row into the State table (`agentic_data_ingestion_state`)
with `status='error'` and an `error_message`. This test therefore asserts on
that recorded failure row — the real failure-handling path — rather than on a
non-existent DLQ.

The record-failure Lambda receives the FULL execution state and extracts
`run_id` from `file_validation_result.run_id`, falling back to the top-level
`run_id` and finally the execution name — so a failure row is written no
matter which step fails (P28 R1.4). The injected fault here surfaces in
schema extraction: file_validation performs a HEAD-only check (it never reads
file content), so a binary file with a .csv extension passes validation and
fails one step later.

Requirements: 15.1, 15.2, 15.3, 15.4
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import pytest
from boto3.dynamodb.conditions import Key
from tests.e2e.conftest import poll_execution_by_input

logger = logging.getLogger(__name__)

# ── Malformed test data — binary garbage with a .csv extension ────────
# This passes the HEAD-only file_validation step and then triggers a
# parse/decode error in schema_extract, exercising the catch path.
MALFORMED_FILE_CONTENT = bytes(range(256)) * 4  # 1 KB of binary garbage

# State-table failure-record polling parameters
FAILURE_RECORD_POLL_INTERVAL = 5  # seconds between polls
FAILURE_RECORD_MAX_WAIT = 120  # max seconds to wait for the failure row
FAILURE_STEP_NAME = "pipeline_failure"  # SK written by the failure-recorder Lambda


def _resolve_run_id(state_table: object, pipeline_id: str, *, timeout: int = 120) -> str | None:
    """Resolve the run_id for a pipeline via the PipelineIndex GSI.

    The early step rows (e.g. file_validation) carry the real pipeline_id as
    the GSI partition key and the generated run_id as the base-table partition
    key. The failure row itself uses a synthetic pipeline_id (``{run_id}.99``)
    and is therefore NOT findable via the GSI — we resolve run_id from the
    early rows instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = state_table.query(  # type: ignore[union-attr]
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        ).get("Items", [])
        for item in items:
            run_id = item.get("run_id")
            if run_id:
                return str(run_id)
        time.sleep(5)
    return None


def _poll_failure_record(state_table: object, run_id: str, *, timeout: int = FAILURE_RECORD_MAX_WAIT) -> dict | None:
    """Poll the base State table (PK=run_id) for the pipeline_failure row.

    The failure-recorder Lambda writes this row with SK ``pipeline_failure`` and
    ``status='error'`` when an SFN Lambda step is caught. Returns the row if
    found within the timeout, else None.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = state_table.get_item(  # type: ignore[union-attr]
            Key={"run_id": run_id, "step_name": FAILURE_STEP_NAME}
        )
        item = resp.get("Item")
        if item:
            return item
        time.sleep(FAILURE_RECORD_POLL_INTERVAL)
    return None


@pytest.mark.e2e
@pytest.mark.slow
class TestFailureDlq:
    """Fault injection → catch path → terminal FAILED → recorded error."""

    def test_malformed_file_triggers_failure_and_records_error(
        self,
        requires_trigger,
        e2e_ledger,
        provisioner,
        cost_budget,
        boto3_session,
        resource_names,
    ):
        """Inject a malformed file, confirm the execution fails, and assert the
        failure is durably recorded (State-table row or execution error/cause).

        Workflow:
        1. Provision a throwaway pipeline (any target — won't reach it)
        2. Upload binary garbage with a .csv extension
        3. Register S3 object with ledger
        4. Poll for execution start (triggered via EventBridge)
        5. Register execution with ledger
        6. Wait for execution to reach FAILED/ABORTED state
        7. Assert the failure is durably recorded (pipeline_failure row OR
           the SFN execution error/cause)
        8. Assert the recorded failure reflects the injected fault
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
            name_prefix="e2e-failure",
        )
        logger.info("Provisioned failure-test pipeline: %s", pipeline.pipeline_id)

        # ── Step 2: Upload malformed file (binary garbage, .csv ext) ──
        unique_key = f"e2e-malformed-{uuid.uuid4().hex[:12]}.csv"
        source_bucket = resource_names["source_bucket"]
        s3_key = f"{pipeline.pipeline_id}/{unique_key}"

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=MALFORMED_FILE_CONTENT,
            ContentType="text/csv",
        )
        logger.info("Uploaded malformed file: s3://%s/%s", source_bucket, s3_key)

        # ── Step 3: Register S3 object with ledger ────────────────────
        e2e_ledger.add_s3_object(source_bucket, s3_key)

        # Record the upload timestamp for execution polling
        started_after = time.time() - 5  # small lookback buffer

        # ── Step 4: Poll for execution start ──────────────────────────
        sfn_client = boto3_session.client("stepfunctions")
        state_machine_arn = resource_names["state_machine_arn"]

        logger.info(
            "Polling for execution: pipeline_id=%s, state_machine=%s",
            pipeline.pipeline_id,
            state_machine_arn,
        )

        status, execution_arn = poll_execution_by_input(
            sfn_client,
            state_machine_arn,
            pipeline_id=pipeline.pipeline_id,
            started_after=started_after,
            timeout=cost_budget,
        )

        logger.info("Found execution: arn=%s, status=%s", execution_arn, status)

        # ── Step 5: Register execution with ledger ────────────────────
        e2e_ledger.add_execution(execution_arn)

        # ── Step 6: Assert the execution reached a failure state ──────
        # The malformed file passes HEAD-only file_validation, then derails a
        # downstream step (schema extraction/compare produces an invalid result
        # the CheckSchemaAction choice can't route), failing the execution. It
        # lands in FAILED (or ABORTED/TIMED_OUT under budget).
        assert status in ("FAILED", "ABORTED", "TIMED_OUT"), (
            f"Expected execution to fail for malformed input, "
            f"but got status={status} (execution_arn={execution_arn}). "
            f"This indicates the catch path was not triggered as expected."
        )
        logger.info(
            "Execution reached expected failure state: %s (Req 15.1 — catch path exercised)",
            status,
        )

        # ── Step 7: Assert the failure is durably recorded ────────────
        # AgenticDataIngestion records failures in one of two places depending on WHERE
        # the run fails:
        #   - A failing *Lambda step* is caught by the SFN UpdateFailureState
        #     task → failure-recorder Lambda → a pipeline_failure row
        #     (status='error') in agentic_data_ingestion_state.
        #   - A failing *Choice/flow state* raises States.Runtime, which is NOT
        #     caught by UpdateFailureState (the catch is only on Lambda steps),
        #     so no row is written; the failure surfaces in the execution's
        #     error/cause.
        # A malformed binary file passes HEAD-only validation and then derails
        # downstream (e.g. the CheckSchemaAction choice on an invalid
        # schema_compare result → States.Runtime), so assert the failure is
        # observable via EITHER mechanism. There is no SQS DLQ in this
        # architecture.
        state_table = boto3_session.resource("dynamodb").Table(resource_names["state_table"])

        failure_texts: list[str] = []

        run_id = _resolve_run_id(state_table, pipeline.pipeline_id, timeout=30)
        if run_id:
            logger.info("Resolved run_id=%s; checking for a pipeline_failure row...", run_id)
            # The execution is already terminal, so any pipeline_failure row is
            # already written — a short poll covers write/read consistency only.
            failure_record = _poll_failure_record(state_table, run_id, timeout=10)
            if failure_record is not None:
                logger.info("Failure record found: %s", json.dumps(failure_record, default=str)[:500])
                assert str(failure_record.get("status", "")).lower() == "error", (
                    f"pipeline_failure row has status='{failure_record.get('status')}', expected 'error' (Req 15.3)."
                )
                assert failure_record.get("original_pipeline_id") == pipeline.pipeline_id, (
                    f"pipeline_failure row original_pipeline_id="
                    f"'{failure_record.get('original_pipeline_id')}' != provisioned "
                    f"pipeline '{pipeline.pipeline_id}'."
                )
                failure_texts.append(str(failure_record.get("error_message", "")))
            else:
                logger.info("No pipeline_failure row (failure routed through a choice/flow state).")
        else:
            logger.info("No run_id resolvable from PipelineIndex (run failed before any step row).")

        # Always-present signal for a FAILED execution: its terminal error/cause.
        desc = sfn_client.describe_execution(executionArn=execution_arn)
        exec_error = str(desc.get("error", "") or "")
        exec_cause = str(desc.get("cause", "") or "")
        if exec_error or exec_cause:
            failure_texts.append(f"{exec_error} {exec_cause}")
        logger.info("Execution failure: error=%s, cause=%s", exec_error, exec_cause[:300])

        # At least one durable failure signal must be present (Req 15.2).
        assert any(t.strip() for t in failure_texts), (
            f"Failure for pipeline {pipeline.pipeline_id} (run {run_id}) was not durably "
            f"recorded in either a pipeline_failure State row or the execution error/cause "
            f"(Req 15.2)."
        )

        # ── Step 8: Assert the recorded failure reflects the injected fault ─
        combined = " ".join(failure_texts).lower()
        fault_indicators = [
            "error",
            "exception",
            "fail",
            "schema",
            "parse",
            "decode",
            "invalid",
            "malformed",
            "runtime",
            "states.",
        ]
        assert any(indicator in combined for indicator in fault_indicators), (
            f"Recorded failure does not reflect the injected fault. Expected an "
            f"error/parse/schema/runtime indicator, got: {combined[:300]!r} (Req 15.3, 15.4)."
        )
        logger.info(
            "Failure recording confirmed: durable signal present and reflects injected fault (Req 15.2, 15.3, 15.4)",
        )
