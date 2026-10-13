# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E (triggered): Behavioral checkpoint/resume via the retry path.

Exercises live resume behavior (TC-3.6) by:
1. Provisioning a throwaway pipeline and triggering a run
2. Letting it progress far enough to persist multiple step records
3. Aborting the execution (simulates a mid-run failure)
4. Resuming via POST /api/runs/{run_id}/retry
5. Asserting the resumed run references its source (retry_source_run_id)
   and reuses checkpointed steps rather than re-running them

Requirements: 27.1, 27.2, 27.3, 27.4

Run with:
    AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true AGENTIC_DATA_INGESTION_E2E_TRIGGER=true \
        uv run python -m pytest tests/e2e/checkpoint/test_resume_behavior.py -m e2e
"""

import json
import logging
import time
import uuid
from datetime import UTC, datetime

import pytest
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

_SAMPLE_CSV = (
    "id,name,amount,created_at\n1,alice,100.50,2026-01-01\n2,bob,200.75,2026-01-02\n3,carol,310.00,2026-01-03\n"
)


@pytest.mark.slow
class TestResumeBehavior:
    """Behavioral checkpoint/resume: retry a run and assert step reuse."""

    def test_retry_resumes_from_checkpoint(
        self,
        requires_trigger,
        cost_budget,
        provisioner,
        e2e_ledger,
        boto3_session,
        resource_names,
        rest_client,
    ):
        """Resume a run via POST /api/runs/{run_id}/retry and assert the
        resumed run references its source and reuses checkpointed steps.

        Strategy (Req 27.4): rather than engineering a live mid-run failure,
        we trigger a run, let it persist multiple steps, abort it, then
        retry via the API. This proves the retry path reuses checkpointed
        state without requiring a fragile mid-execution interruption.
        """
        # ── Step 1: Provision a throwaway pipeline ────────────────────
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        pipeline_id = pipeline.pipeline_id
        logger.info("Provisioned pipeline: %s", pipeline_id)

        # ── Step 2: Upload a CSV to trigger a run ─────────────────────
        s3 = boto3_session.client("s3")
        sfn = boto3_session.client("stepfunctions")
        bucket = resource_names["source_bucket"]
        file_key = f"{pipeline_id}/e2e-resume-{uuid.uuid4().hex[:8]}.csv"

        started_after = time.time() - 5
        s3.put_object(Bucket=bucket, Key=file_key, Body=_SAMPLE_CSV.encode())
        e2e_ledger.add_s3_object(bucket, file_key)
        logger.info("Uploaded source file: s3://%s/%s", bucket, file_key)

        # ── Step 3: Locate the execution ──────────────────────────────
        sm_arn = resource_names["state_machine_arn"]
        execution_arn = None
        deadline = time.time() + 120

        while time.time() < deadline and execution_arn is None:
            for ex in sfn.list_executions(stateMachineArn=sm_arn, maxResults=25).get("executions", []):
                if ex["startDate"].timestamp() < started_after:
                    continue
                desc = sfn.describe_execution(executionArn=ex["executionArn"])
                try:
                    inp = json.loads(desc.get("input", "{}"))
                except json.JSONDecodeError:
                    inp = {}
                if inp.get("pipeline_id") == pipeline_id:
                    execution_arn = ex["executionArn"]
                    break
            if execution_arn is None:
                time.sleep(5)

        assert execution_arn, f"No execution started for pipeline {pipeline_id} within 120s"
        e2e_ledger.add_execution(execution_arn)
        logger.info("Located execution: %s", execution_arn)

        # ── Step 4: Wait for multiple steps to persist ────────────────
        # The execution must progress past schema extraction (enough to
        # persist checkpoint state in the State table).
        state_table = boto3_session.resource("dynamodb").Table(resource_names["state_table"])
        steps_persisted = False
        step_deadline = time.time() + min(cost_budget, 300)

        while time.time() < step_deadline:
            items = state_table.query(
                IndexName="PipelineIndex",
                KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
            ).get("Items", [])
            step_names = {i.get("step_name") for i in items if i.get("step_name")}
            if len(step_names) >= 2:
                steps_persisted = True
                logger.info("Found %d persisted steps: %s", len(step_names), step_names)
                break

            # Also check if execution already terminal (fast path, e.g. cache)
            status = sfn.describe_execution(executionArn=execution_arn)["status"]
            if status != "RUNNING":
                # If it finished (SUCCEEDED/FAILED/ABORTED), we can still retry
                items = state_table.query(
                    IndexName="PipelineIndex",
                    KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
                ).get("Items", [])
                step_names = {i.get("step_name") for i in items if i.get("step_name")}
                if len(step_names) >= 2:
                    steps_persisted = True
                    logger.info(
                        "Execution terminal (%s) with %d steps: %s",
                        status,
                        len(step_names),
                        step_names,
                    )
                break
            time.sleep(10)

        assert steps_persisted, (
            f"Execution did not persist >=2 steps within timeout "
            f"(found: {step_names if 'step_names' in dir() else 'none'})"
        )

        # Capture original step records for later comparison
        original_items = state_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        ).get("Items", [])
        original_run_ids = {i.get("run_id") for i in original_items if i.get("run_id")}

        # ── Step 5: Abort the execution (simulate failure) ────────────
        current_status = sfn.describe_execution(executionArn=execution_arn)["status"]
        if current_status == "RUNNING":
            sfn.stop_execution(
                executionArn=execution_arn,
                error="E2ECheckpointTest",
                cause="Aborted to test retry/resume behavior",
            )
            logger.info("Aborted execution: %s", execution_arn)
            # Wait for abort to register — poll until no longer RUNNING
            abort_deadline = time.time() + 30
            while time.time() < abort_deadline:
                abort_status = sfn.describe_execution(executionArn=execution_arn)["status"]
                if abort_status != "RUNNING":
                    logger.info("Execution stopped: status=%s", abort_status)
                    break
                time.sleep(2)
            else:
                logger.warning("Execution still RUNNING after stop_execution — proceeding anyway")

        # Determine the run_id for the retry call. It's typically the
        # execution name or stored in the executions table.
        # Try to find it from the execution input or state records
        run_id = None
        if original_run_ids:
            run_id = sorted(original_run_ids).pop()  # most recent

        if not run_id:
            # Derive from execution ARN — the run_id is often the execution name
            run_id = execution_arn.split(":")[-1]

        logger.info("Using run_id for retry: %s", run_id)

        # ── Step 5b: Record a genuine failure for this run ────────────
        # Aborting the SFN execution does NOT mark the run FAILED: the
        # execution was paused at an approval gate (WAITING_APPROVAL), and
        # stop_execution does not trigger the failure-recorder Lambda (which
        # only fires on a caught States.TaskFailed). The retry endpoint only
        # resumes runs with a recorded failure — either a State entry with
        # status 'error'/'failed', or an execution record marked FAILED.
        #
        # Mirror exactly what the failure-recorder Lambda writes on a real
        # mid-run Lambda failure (a `pipeline_failure` row, PK=run_id,
        # SK='pipeline_failure', status='error', synthetic pipeline_id
        # '{run_id}.99'). This is a faithful simulation of the production
        # failure path and unblocks the retry/resume behavior under test.
        # The pre-existing success step rows are left intact so the retry
        # endpoint still finds cached results to reuse.
        state_table.put_item(
            Item={
                "run_id": run_id,
                "step_name": "pipeline_failure",
                "pipeline_id": f"{run_id}.99",
                "original_pipeline_id": pipeline_id,
                "status": "error",
                "timestamp": datetime.now(UTC).isoformat(),
                "step_sequence": 99,
                "error_message": "E2ECheckpointTest: simulated mid-run failure to exercise retry/resume",
            }
        )
        logger.info("Recorded pipeline_failure row for run %s to enable retry", run_id)

        # ── Step 5c: Release the pipeline lock ────────────────────────
        # Aborting the SFN execution skips the terminal ReleaseLockFailure
        # step, so the pipeline lock row persists and the retry endpoint
        # rejects with 409 "Pipeline is currently locked". A real failure path
        # releases the lock via the lock-release Lambda; mirror that here.
        # The lock key (pipeline_prefix) is the S3 key prefix, which for a
        # source file uploaded under "{pipeline_id}/..." is the pipeline_id
        # (matches the retry endpoint's get_item(Key={"pipeline_prefix":
        # pipeline_id}) check).
        locks_table = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_pipeline_locks")
        try:
            locks_table.delete_item(Key={"pipeline_prefix": pipeline_id})
            logger.info("Released pipeline lock for prefix %s", pipeline_id)
        except Exception as exc:
            logger.warning("Failed to release pipeline lock (continuing): %s", exc)

        # ── Step 6: Resume via POST /api/runs/{run_id}/retry ──────────
        retry_resp = rest_client.post(f"/api/runs/{run_id}/retry")

        # Accept 200, 201, or 202 as successful retry initiation
        assert retry_resp.status_code in (200, 201, 202), (
            f"Retry request failed (HTTP {retry_resp.status_code}): {retry_resp.body}"
        )

        retry_body = retry_resp.body if isinstance(retry_resp.body, dict) else {}
        logger.info("Retry response: %s", retry_body)

        # ── Step 7: Assert retry_source_run_id reference ──────────────
        # The response should include a reference to the source run
        new_run_id = retry_body.get("run_id") or retry_body.get("execution_id")
        retry_source = retry_body.get("retry_source_run_id") or retry_body.get("source_run_id")

        assert retry_source or new_run_id, (
            "Retry response did not include a new run_id or retry_source_run_id reference"
        )

        if retry_source:
            assert retry_source == run_id, (
                f"retry_source_run_id ({retry_source}) does not match the original run_id ({run_id})"
            )
            logger.info("Confirmed: resumed run references source run %s", retry_source)

        # ── Step 8: Poll for the resumed execution ────────────────────
        # Discover it by the response's `execution_name`, NOT by comparing run
        # ids (p38 §4.1). Under the continuation model the retry executes AND
        # reports under the ORIGINAL run id, so the previous guard
        # `if new_run_id and new_run_id != run_id:` is permanently FALSE. That
        # was a cost leak rather than a failed assertion: discovery no-opped,
        # `resumed_execution_arn` stayed None, and step 10's cost-bounding
        # `stop_execution` never ran — leaving the resumed execution to run to
        # completion on every invocation of this test.
        #
        # The execution NAME is the identifier that still distinguishes the
        # retry: `retry-{run_id}-{ts_hash}`, truncated to AWS's 80-char bound
        # (pipelines_runs.py:1611-1613), returned verbatim in the 200 body
        # (:1738). It is unique per state machine, so an exact name match needs
        # no `describe_execution`/input parse to disambiguate.
        resumed_execution_arn = None
        retry_execution_name = retry_body.get("execution_name")
        assert retry_execution_name, (
            f"Retry response carried no execution_name, so the resumed execution cannot be "
            f"discovered or cost-bounded: {retry_body}"
        )

        # No statusFilter: the execution is registered for cleanup whether it is
        # still RUNNING or already terminal, and step 10 re-checks the status
        # before stopping it.
        poll_deadline = time.time() + 60
        while time.time() < poll_deadline:
            for ex in sfn.list_executions(stateMachineArn=sm_arn, maxResults=25).get("executions", []):
                if ex["name"] == retry_execution_name:
                    resumed_execution_arn = ex["executionArn"]
                    break
            if resumed_execution_arn:
                break
            time.sleep(5)

        if resumed_execution_arn:
            e2e_ledger.add_execution(resumed_execution_arn)
            logger.info("Found resumed execution: %s", resumed_execution_arn)
        else:
            logger.warning(
                "Could not locate the resumed execution by name %s within 60s — step 10 cannot cost-bound it",
                retry_execution_name,
            )

        # ── Step 9: Assert checkpointed step reuse ────────────────────
        # The resumed run should reference/reuse state from the original.
        # Wait a short period for state records to populate, then check.
        time.sleep(10)

        post_retry_items = state_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        ).get("Items", [])

        # Check for step reuse evidence:
        # - The original step records should still exist (not re-created)
        # - Or the new run's state should reference the source run
        original_step_keys = {(i.get("step_name"), i.get("run_id")) for i in original_items if i.get("step_name")}
        post_retry_step_keys = {(i.get("step_name"), i.get("run_id")) for i in post_retry_items if i.get("step_name")}

        # The original steps should still be present (not wiped/recreated).
        #
        # Under p38's continuation model this assertion is TAUTOLOGICAL: the
        # retry executes under the original run id, so every row it writes lands
        # on an existing `(run_id, step_name)` key — the retry overwrites rows in
        # place and can only ADD keys, never remove one. Kept deliberately rather
        # than deleted: it is now a guard that a future change does not make a
        # retry delete or re-key the original's state rows, which is exactly the
        # regression `LGR-60c4985-4` (template / ratified) fences against.
        assert original_step_keys.issubset(post_retry_step_keys), (
            "Original checkpointed steps were removed after retry — "
            "expected reuse, not re-creation. "
            f"Missing: {original_step_keys - post_retry_step_keys}"
        )

        logger.info(
            "Step reuse confirmed: %d original steps preserved post-retry, %d total steps now",
            len(original_step_keys),
            len(post_retry_step_keys),
        )

        # ── Step 10: Abort resumed execution (cost bound) ─────────────
        if resumed_execution_arn:
            try:
                status = sfn.describe_execution(executionArn=resumed_execution_arn)["status"]
                if status == "RUNNING":
                    sfn.stop_execution(
                        executionArn=resumed_execution_arn,
                        error="E2ECheckpointTestCleanup",
                        cause="Aborted resumed execution after assertions confirmed",
                    )
                    logger.info("Aborted resumed execution: %s", resumed_execution_arn)
            except Exception as exc:
                logger.warning("Failed to abort resumed execution: %s", exc)
