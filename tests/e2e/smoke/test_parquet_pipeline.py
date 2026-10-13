# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E smoke test: S3 Parquet / Glue Catalog pipeline produced target data.

Non-mutating verification. Skips gracefully if no Parquet/Glue-Catalog
pipeline is deployed in the target environment.

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import pytest


def test_parquet_or_catalog_pipeline_produced_data(find_pipelines_fn, verify_target_data_fn):
    candidates = []
    for tt in ("S3_PARQUET", "s3_parquet", "GLUE_CATALOG", "glue_catalog"):
        candidates += find_pipelines_fn(target_type=tt, glue_status="SUCCEEDED")
    if not candidates:
        pytest.skip("No SUCCEEDED Parquet/Glue-Catalog pipeline in this environment")

    errors = []
    for p in candidates:
        try:
            verify_target_data_fn(p["pipeline_id"])
            return
        except AssertionError as e:
            errors.append(f"{p['pipeline_id']}: {e}")
    pytest.fail("No pipeline evidenced a successful target write:\n" + "\n".join(errors))
