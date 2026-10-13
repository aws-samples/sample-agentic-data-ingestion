# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E smoke test: JDBC pipeline produced target data on real AWS (TC-7.7).

Non-mutating verification that a deployed JDBC pipeline has a SUCCEEDED Glue
run with a recorded successful target write.

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest


def test_jdbc_pipeline_produced_data(find_pipelines_fn, verify_target_data_fn):
    candidates = find_pipelines_fn(target_type="JDBC", glue_status="SUCCEEDED")
    if not candidates:
        pytest.skip("No SUCCEEDED JDBC pipeline available in this environment")

    errors = []
    for p in candidates:
        try:
            verify_target_data_fn(p["pipeline_id"])
            return
        except AssertionError as e:
            errors.append(f"{p['pipeline_id']}: {e}")
    pytest.fail("No JDBC pipeline evidenced a successful target write:\n" + "\n".join(errors))
