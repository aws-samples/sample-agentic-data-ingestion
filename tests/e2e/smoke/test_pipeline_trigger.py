# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E (opt-in): live upload → EventBridge → Step Functions trigger flow.

This test actually mutates the environment (uploads a file, starts a real
Step Functions execution) so it is gated behind an EXTRA opt-in flag:

    AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true AGENTIC_DATA_INGESTION_E2E_TRIGGER=true \
        uv run python -m pytest tests/e2e/smoke/test_pipeline_trigger.py -m e2e

It verifies the upload-driven trigger path end-to-end: an S3 upload to a
pipeline's source prefix causes EventBridge to start a Step Functions
execution that progresses to the schema-mapping approval gate. It then aborts
the execution and removes the uploaded file so it leaves no dangling run.

(The downstream approve → code-gen → Glue → target-write stages require
acting as the approver across two waitForTaskToken gates; that is exercised by
the non-mutating output-verification smoke tests, which assert real SUCCEEDED
runs produced target data.)
"""

import time

import pytest

_SAMPLE_CSV = (
    "id,name,amount,created_at\n1,alice,100.50,2026-01-01\n2,bob,200.75,2026-01-02\n3,carol,310.00,2026-01-03\n"
)


def test_upload_triggers_sfn_to_approval_gate(requires_trigger, boto3_session, resource_names, find_pipelines_fn):
    candidates = find_pipelines_fn(target_type="NEW_S3_TABLE_BUCKET")
    if not candidates:
        pytest.skip("No active S3-Tables pipeline available to trigger")
    pipeline_id = candidates[0]["pipeline_id"]

    s3 = boto3_session.client("s3")
    sfn = boto3_session.client("stepfunctions")
    bucket = resource_names["source_bucket"]
    key = f"{pipeline_id}/e2e_trigger_probe.csv"
    sm_arn = resource_names["state_machine_arn"]

    started_after = time.time() - 5
    s3.put_object(Bucket=bucket, Key=key, Body=_SAMPLE_CSV.encode())
    execution_arn = None
    try:
        # Wait for EventBridge → trigger Lambda → SFN execution to appear.
        deadline = time.time() + 120
        while time.time() < deadline and execution_arn is None:
            for ex in sfn.list_executions(stateMachineArn=sm_arn, maxResults=25)["executions"]:
                if ex["startDate"].timestamp() >= started_after and ("e2e_trigger_probe" in ex["name"]):
                    execution_arn = ex["executionArn"]
                    break
            if execution_arn is None:
                time.sleep(5)
        assert execution_arn, "Upload did not start a Step Functions execution"

        # Progress to an approval gate (proves the trigger → extract →
        # compare → mapping wiring works on real AWS). Either gate is a valid
        # success signal: a pipeline with NO cached mapping pauses at
        # WaitForSchemaMappingApproval, while one WITH a cached mapping takes
        # the ReuseExistingMapping path and pauses at the later
        # WaitForCodeApproval gate. Reaching either proves the upstream wiring.
        approval_gate_states = ("WaitForSchemaMappingApproval", "WaitForCodeApproval")
        reached_gate = False
        deadline = time.time() + 240
        while time.time() < deadline:
            events = sfn.get_execution_history(executionArn=execution_arn, reverseOrder=True, maxResults=25)["events"]
            names = [e.get("stateEnteredEventDetails", {}).get("name", "") for e in events]
            if any(gate in n for n in names for gate in approval_gate_states):
                reached_gate = True
                break
            status = sfn.describe_execution(executionArn=execution_arn)["status"]
            if status not in ("RUNNING",):
                # Reached a terminal state without an approval gate — still a
                # valid trigger (e.g., cache-hit auto-skip path).
                reached_gate = status == "SUCCEEDED"
                break
            time.sleep(10)
        assert reached_gate, "Execution did not reach an approval gate or succeed"
    finally:
        if execution_arn:
            try:
                if sfn.describe_execution(executionArn=execution_arn)["status"] == "RUNNING":
                    sfn.stop_execution(
                        executionArn=execution_arn,
                        error="E2EProbeCleanup",
                        cause="Aborted by e2e trigger test",
                    )
            except Exception:
                pass
        s3.delete_object(Bucket=bucket, Key=key)
