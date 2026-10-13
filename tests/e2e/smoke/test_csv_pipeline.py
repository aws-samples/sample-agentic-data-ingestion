# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E smoke test: CSV → S3 Tables pipeline produced target data on real AWS.

Verifies the full pipeline flow end-to-end by asserting that a deployed
S3-Tables pipeline has a SUCCEEDED Glue run and that the RunPipeline step
recorded a successful target write in the State table.

This is a *non-mutating* verification of real deployed output (it does not
trigger a new run). The opt-in upload→approve→completion variant lives in
``tests/e2e/smoke/test_pipeline_trigger.py`` (gated by AGENTIC_DATA_INGESTION_E2E_TRIGGER).

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest


def test_csv_s3_tables_pipeline_produced_data(find_pipelines_fn, verify_target_data_fn):
    """A deployed S3-Tables pipeline has SUCCEEDED and wrote target data."""
    candidates = find_pipelines_fn(target_type="NEW_S3_TABLE_BUCKET", glue_status="SUCCEEDED")
    if not candidates:
        pytest.skip("No SUCCEEDED S3-Tables pipeline available in this environment")

    # Verify at least one candidate has evidenced a successful target write.
    errors = []
    for p in candidates:
        try:
            output = verify_target_data_fn(p["pipeline_id"])
            assert output["job_run_id"]
            return  # success
        except AssertionError as e:
            errors.append(f"{p['pipeline_id']}: {e}")
    pytest.fail("No S3-Tables pipeline evidenced a successful target write:\n" + "\n".join(errors))
