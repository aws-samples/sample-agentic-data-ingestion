# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Integration test: full pipeline run → /artifacts returns all artifact types.

Simulates a complete pipeline run by populating the state table mock with
all 7 step items (each with standardized output_data), then calling
get_run_artifacts() and verifying every artifact type is present, correctly
typed, and passes validate_output_data().
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup — dashboard API Lambda + shared modules
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# ---------------------------------------------------------------------------
# Environment variables required at import time
# ---------------------------------------------------------------------------
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

# ---------------------------------------------------------------------------
# Import shared schema validation
# ---------------------------------------------------------------------------
from artifact_schema import validate_output_data

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PIPELINE_ID = "pipe-integ-001"
RUN_ID = "run-integ-full-abc"

CODE_CONTENT = "import sys\nfrom awsglue.utils import getResolvedOptions\ndf_transformed = df.select('id', 'name')\n"

# All 7 pipeline steps with standardized output_data
ALL_STEP_ITEMS = [
    {
        "step_name": "file_validation",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "file_validation",
            "primary_id": "fv-integ-1",
            "status": "success",
            "summary": {
                "file_name": "sales_data.json",
                "file_size_bytes": 204800,
                "record_count_estimate": 1500,
                "file_format": "json",
            },
            "s3_uris": {
                "source_file": "s3://data-bucket/uploads/sales_data.json",
            },
        },
    },
    {
        "step_name": "schema_extract",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "schema_extraction",
            "primary_id": "se-integ-1",
            "status": "success",
            "summary": {
                "field_count": 12,
                "schema_hash": "abc123def456",
                "is_new_schema": True,
            },
            "s3_uris": {},
        },
    },
    {
        "step_name": "schema_compare",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "schema_comparison",
            "primary_id": "sc-integ-1",
            "status": "success",
            "summary": {
                "fields_added": 2,
                "fields_removed": 0,
                "fields_changed": 1,
            },
            "s3_uris": {},
        },
    },
    {
        "step_name": "schema_mapping",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "schema_mapping",
            "primary_id": "sm-integ-1",
            "status": "success",
            "summary": {
                "mapped_fields": 10,
                "unmapped_fields": 2,
                "avg_confidence": 0.87,
                "approval_required": True,
            },
            "s3_uris": {},
        },
    },
    {
        "step_name": "code_generation",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "code_generation",
            "primary_id": "cv-integ-1",
            "status": "success",
            "summary": {
                "line_count": 85,
                "template_used": "glue_job_s3_tables.py.j2",
                "target_type": "s3_tables",
            },
            "s3_uris": {
                "code_file": "s3://code-bucket/generated/pipe-integ-001/job.py",
            },
        },
    },
    {
        "step_name": "CodeTesting",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "test_results",
            "primary_id": "tr-integ-1",
            "status": "success",
            "summary": {
                "tests_passed": 5,
                "tests_failed": 0,
                "tests_total": 5,
                "test_report_markdown": "## Test Report\n- All 5 tests passed",
            },
            "s3_uris": {
                "full_report_json": "s3://code-bucket/reports/test-report.json",
            },
        },
    },
    {
        "step_name": "CreateInfrastructure",
        "run_id": RUN_ID,
        "output_data": {
            "artifact_type": "infrastructure",
            "primary_id": "inf-integ-1",
            "status": "success",
            "summary": {
                "overall_status": "created",
                "resources_created": 3,
            },
            "s3_uris": {},
        },
    },
]

# Map step_name → expected artifact_type for verification
EXPECTED_ARTIFACT_TYPES = {
    "file_validation": "file_validation",
    "schema_extract": "schema_extraction",
    "schema_compare": "schema_comparison",
    "schema_mapping": "schema_mapping",
    "code_generation": "code_generation",
    "CodeTesting": "test_results",
    "CreateInfrastructure": "infrastructure",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Fixture — patch boto resources
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    from handlers import common as _common

    # P21 Task 7.2: get_run_artifacts now lives in handlers.pipelines_runs and
    # reads S3 via handlers.pipelines_artifacts._read_s3_object. Patch the
    # owning modules' state_table / s3_client so the relocated functions see
    # the mocks (behaviour-preserving — module references only).
    from handlers import pipelines_artifacts as _artifacts
    from handlers import pipelines_runs as _runs

    mock_table = MagicMock()
    mock_s3 = MagicMock()

    monkeypatch.setattr(_common, "state_table", mock_table)
    monkeypatch.setattr(_common, "s3_client", mock_s3)
    monkeypatch.setattr(_runs, "state_table", mock_table)
    monkeypatch.setattr(_artifacts, "s3_client", mock_s3)

    # Configure state_table to return all 7 steps
    mock_table.query.return_value = {"Items": ALL_STEP_ITEMS}

    # Configure S3 to return code content for code_file URI
    s3_body = MagicMock()
    s3_body.read.return_value = CODE_CONTENT.encode("utf-8")
    mock_s3.get_object.return_value = {"Body": s3_body}

    _patch_boto.state_table = mock_table
    _patch_boto.s3_client = mock_s3
    yield


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.pipelines import get_run_artifacts

# ===========================================================================
# Integration tests — full pipeline run
# ===========================================================================


class TestFullPipelineArtifacts:
    """All 7 pipeline steps present → /artifacts returns complete artifact set."""

    def test_response_status_200(self):
        status, _ = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        assert status == 200

    def test_all_step_names_present(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        artifacts = body["artifacts"]

        for step_name in EXPECTED_ARTIFACT_TYPES:
            assert step_name in artifacts, f"Step '{step_name}' missing from artifacts response"

    def test_artifact_count_matches(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        assert len(body["artifacts"]) == 7

    def test_each_artifact_has_correct_type(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        artifacts = body["artifacts"]

        for step_name, expected_type in EXPECTED_ARTIFACT_TYPES.items():
            actual_type = artifacts[step_name]["artifact_type"]
            assert actual_type == expected_type, (
                f"Step '{step_name}': expected artifact_type='{expected_type}', got '{actual_type}'"
            )

    def test_code_content_fetched_from_s3(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        assert body["code_content"] is not None
        assert body["code_content"] == CODE_CONTENT

    def test_s3_called_with_correct_bucket_and_key(self):
        _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        _patch_boto.s3_client.get_object.assert_called_once_with(
            Bucket="code-bucket",
            Key="generated/pipe-integ-001/job.py",
        )

    def test_each_artifact_passes_schema_validation(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for step_name, artifact in body["artifacts"].items():
            try:
                validate_output_data(artifact)
            except ValueError as exc:
                pytest.fail(f"Step '{step_name}' output_data failed validation: {exc}")

    def test_pipeline_id_and_run_id_in_response(self):
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))
        assert body["pipeline_id"] == PIPELINE_ID
        assert body["run_id"] == RUN_ID
