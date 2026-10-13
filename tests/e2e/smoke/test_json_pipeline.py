# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E smoke test: JSON-source pipeline produced target data on real AWS.

Non-mutating verification: asserts that a deployed pipeline (any S3-Tables
target — JSON and CSV sources share the same downstream flow) has a SUCCEEDED
Glue run with a recorded successful target write.

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest


def test_existing_s3_table_pipeline_produced_data(find_pipelines_fn, verify_target_data_fn):
    """An EXISTING_S3_TABLE_BUCKET pipeline has SUCCEEDED and wrote data."""
    candidates = find_pipelines_fn(target_type="EXISTING_S3_TABLE_BUCKET", glue_status="SUCCEEDED")
    if not candidates:
        pytest.skip("No SUCCEEDED EXISTING_S3_TABLE_BUCKET pipeline available")

    errors = []
    for p in candidates:
        try:
            verify_target_data_fn(p["pipeline_id"])
            return
        except AssertionError as e:
            errors.append(f"{p['pipeline_id']}: {e}")
    pytest.fail("No pipeline evidenced a successful target write:\n" + "\n".join(errors))
