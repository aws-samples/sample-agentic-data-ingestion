# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: checkpoint / state-persistence verification against real AWS.

Conformance: Requirement 23 — brought under the shared E2E_Harness contracts.

Checkpoint-resume is backed by (a) the dedicated checkpoints bucket and
(b) per-step state persistence in the State table — a resumed run reads the
previously-completed steps instead of re-running them. This test verifies both
the deployed checkpoint store exists and that a real multi-step execution
persisted intermediate step state.

**Coverage scope (substrate only):**
- TC-3.6 substrate: checkpoints bucket exists + multi-step state persistence
- Does NOT perform a live resume — see test_resume_behavior.py (Req 27)

**Harness conformance:**
- Gated by Real_AWS_Gate (session autouse via ``require_real_aws``)
- Read-only: does NOT create artifacts, does NOT need ``requires_trigger``
- Reuses E2E_Harness fixtures (``boto3_session``, ``find_pipelines_fn``,
  ``account_id``, ``aws_region``)
- No Cleanup_Contract obligations (no artifacts created)

**State table fields relied on (``agentic_data_ingestion_state``):**
- ``pipeline_id`` (GSI partition key on ``PipelineIndex``)
- ``step_name``: identifies the pipeline step (e.g. ``SchemaExtract``,
  ``SchemaCompare``, ``CreateCode``, ``RunPipeline``)
- ``status``: step completion status (``success`` / ``failed``)
- ``output_data``: step-specific output dict; for ``RunPipeline`` steps this
  contains ``job_run_id``, ``completed_at``, ``actual_cost``, ``error_message``
- ``timestamp``: ISO timestamp of step completion

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest
from boto3.dynamodb.conditions import Key


def test_checkpoints_bucket_exists(boto3_session, account_id, aws_region):
    """The dedicated checkpoints bucket is deployed and reachable."""
    s3 = boto3_session.client("s3")
    bucket = f"adi-checkpoints-{account_id}-{aws_region}"
    s3.head_bucket(Bucket=bucket)


def test_run_persists_multistep_state(boto3_session, find_pipelines_fn):
    """A real successful pipeline run persisted multiple distinct step records
    in the State table — the substrate for checkpoint-based resume."""
    candidates = find_pipelines_fn(target_type="NEW_S3_TABLE_BUCKET", glue_status="SUCCEEDED")
    if not candidates:
        pytest.skip("No SUCCEEDED pipeline available to inspect step state")

    state = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_state")
    for p in candidates:
        items = state.query(
            IndexName="PipelineIndex",
            KeyConditionExpression=Key("pipeline_id").eq(p["pipeline_id"]),
        ).get("Items", [])
        step_names = {i.get("step_name") for i in items if i.get("step_name")}
        # A full run touches several steps (schema extract/compare/mapping,
        # code gen, infra, run). Require at least 3 distinct persisted steps.
        if len(step_names) >= 3:
            return
    pytest.fail("No pipeline shows >=3 persisted step records in the State table")
