# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: each step Lambda's output_data has required keys with correct types.

Validates that representative output_data dicts for every artifact type conform
to the standardized schema and contain the expected summary keys per step.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from artifact_schema import REQUIRED_KEYS, validate_output_data

# ---------------------------------------------------------------------------
# Representative output_data fixtures — one per step Lambda
# ---------------------------------------------------------------------------

SAMPLE_OUTPUT_DATA = {
    "file_validation": {
        "artifact_type": "file_validation",
        "primary_id": "run-001",
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
    "schema_extraction": {
        "artifact_type": "schema_extraction",
        "primary_id": "sha256-abc123",
        "status": "success",
        "summary": {
            "field_count": 12,
            "schema_hash": "sha256-abc123",
            "is_new_schema": True,
        },
        "s3_uris": {},
    },
    "schema_comparison": {
        "artifact_type": "schema_comparison",
        "primary_id": "run-001",
        "status": "success",
        "summary": {
            "comparison_result": "compatible",
            "target_schema_changed": False,
            "differences_count": 0,
        },
        "s3_uris": {},
    },
    "schema_mapping": {
        "artifact_type": "schema_mapping",
        "primary_id": "map-456",
        "status": "success",
        "summary": {
            "mapped_fields": 10,
            "unmapped_fields": 2,
            "avg_confidence": 0.92,
            "approval_required": True,
        },
        "s3_uris": {},
    },
    "code_generation": {
        "artifact_type": "code_generation",
        "primary_id": "cv-789",
        "status": "success",
        "summary": {
            "line_count": 85,
            "template_used": "glue_job_s3_tables.py.j2",
            "target_type": "s3_tables",
        },
        "s3_uris": {
            "code_file": "s3://code-bucket/generated/cv-789.py",
        },
    },
    "test_results": {
        "artifact_type": "test_results",
        "primary_id": "tr-101",
        "status": "success",
        "summary": {
            "tests_passed": 5,
            "tests_failed": 0,
            "tests_total": 5,
            "test_report_markdown": "## Test Report\n- All 5 tests passed",
        },
        "s3_uris": {
            "full_report_json": "s3://reports-bucket/tests/tr-101.json",
        },
    },
    "infrastructure": {
        "artifact_type": "infrastructure",
        "primary_id": "run-001",
        "status": "success",
        "summary": {
            "overall_status": "created",
            "resources_created": 3,
            "bucket_name": "my-table-bucket",
            "table_name": "sales",
            "namespace": "analytics",
            "table_format": "iceberg",
        },
        "s3_uris": {},
    },
}

# Expected summary keys per artifact type (from design spec)
EXPECTED_SUMMARY_KEYS = {
    "file_validation": {"file_name", "file_size_bytes", "record_count_estimate", "file_format"},
    "schema_extraction": {"field_count", "schema_hash", "is_new_schema"},
    "schema_comparison": {"comparison_result", "target_schema_changed", "differences_count"},
    "schema_mapping": {"mapped_fields", "unmapped_fields", "avg_confidence", "approval_required"},
    "code_generation": {"line_count", "template_used", "target_type"},
    "test_results": {"tests_passed", "tests_failed", "tests_total", "test_report_markdown"},
    "infrastructure": {"overall_status", "resources_created", "bucket_name", "table_name", "namespace", "table_format"},
}


# ---------------------------------------------------------------------------
# Tests: each artifact type passes validation
# ---------------------------------------------------------------------------


class TestOutputDataValidation:
    """Each representative output_data passes validate_output_data()."""

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_sample_passes_validation(self, artifact_type):
        validate_output_data(SAMPLE_OUTPUT_DATA[artifact_type])

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_has_all_required_keys(self, artifact_type):
        data = SAMPLE_OUTPUT_DATA[artifact_type]
        assert set(data.keys()) >= REQUIRED_KEYS

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_artifact_type_matches_key(self, artifact_type):
        """The dict's artifact_type value matches the key used in SAMPLE_OUTPUT_DATA."""
        assert SAMPLE_OUTPUT_DATA[artifact_type]["artifact_type"] == artifact_type

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_primary_id_is_string(self, artifact_type):
        assert isinstance(SAMPLE_OUTPUT_DATA[artifact_type]["primary_id"], str)

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_status_is_string(self, artifact_type):
        assert isinstance(SAMPLE_OUTPUT_DATA[artifact_type]["status"], str)

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_summary_is_dict(self, artifact_type):
        assert isinstance(SAMPLE_OUTPUT_DATA[artifact_type]["summary"], dict)

    @pytest.mark.parametrize("artifact_type", list(SAMPLE_OUTPUT_DATA.keys()))
    def test_s3_uris_is_dict(self, artifact_type):
        assert isinstance(SAMPLE_OUTPUT_DATA[artifact_type]["s3_uris"], dict)


# ---------------------------------------------------------------------------
# Tests: each artifact type has the correct summary keys
# ---------------------------------------------------------------------------


class TestSummaryKeysPerStep:
    """Each artifact type's summary contains exactly the expected keys."""

    @pytest.mark.parametrize("artifact_type", list(EXPECTED_SUMMARY_KEYS.keys()))
    def test_summary_has_expected_keys(self, artifact_type):
        actual_keys = set(SAMPLE_OUTPUT_DATA[artifact_type]["summary"].keys())
        expected_keys = EXPECTED_SUMMARY_KEYS[artifact_type]
        assert actual_keys == expected_keys, (
            f"{artifact_type}: expected summary keys {sorted(expected_keys)}, got {sorted(actual_keys)}"
        )


# ---------------------------------------------------------------------------
# Tests: invalid output_data fails validation
# ---------------------------------------------------------------------------


class TestInvalidOutputData:
    """Invalid output_data is rejected by validate_output_data()."""

    def test_missing_artifact_type(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        del data["artifact_type"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data(data)

    def test_missing_primary_id(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        del data["primary_id"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data(data)

    def test_missing_status(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        del data["status"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data(data)

    def test_missing_summary(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        del data["summary"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data(data)

    def test_missing_s3_uris(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        del data["s3_uris"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data(data)

    def test_invalid_artifact_type_value(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["artifact_type"] = "nonexistent_step"
        with pytest.raises(ValueError, match="not valid"):
            validate_output_data(data)

    def test_wrong_type_artifact_type(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["artifact_type"] = 42
        with pytest.raises(ValueError, match="artifact_type must be str"):
            validate_output_data(data)

    def test_wrong_type_primary_id(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["primary_id"] = 123
        with pytest.raises(ValueError, match="primary_id must be str"):
            validate_output_data(data)

    def test_wrong_type_status(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["status"] = 1
        with pytest.raises(ValueError, match="status must be str"):
            validate_output_data(data)

    def test_invalid_status_value(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["status"] = "pending"
        with pytest.raises(ValueError, match="not valid"):
            validate_output_data(data)

    def test_wrong_type_summary(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["summary"] = "not a dict"
        with pytest.raises(ValueError, match="summary must be a dict"):
            validate_output_data(data)

    def test_wrong_type_s3_uris(self):
        data = dict(SAMPLE_OUTPUT_DATA["code_generation"])
        data["s3_uris"] = ["s3://bucket/key"]
        with pytest.raises(ValueError, match="s3_uris must be a dict"):
            validate_output_data(data)

    def test_non_dict_input(self):
        with pytest.raises(ValueError, match="must be a dict"):
            validate_output_data("just a string")

    def test_empty_dict(self):
        with pytest.raises(ValueError, match="missing required keys"):
            validate_output_data({})
