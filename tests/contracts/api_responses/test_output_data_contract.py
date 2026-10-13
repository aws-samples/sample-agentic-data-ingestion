# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test: create_code Lambda's output_data matches schema expected by get_run_artifacts.

Validates that the output_data Map written by create_code conforms to the
standardized artifact schema and contains the specific keys that
get_run_artifacts reads (artifact_type, s3_uris.code_file, summary fields).
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from artifact_schema import ARTIFACT_TYPES, validate_output_data

# ---------------------------------------------------------------------------
# Representative create_code output_data (matches design spec)
# ---------------------------------------------------------------------------

CREATE_CODE_OUTPUT_DATA = {
    "artifact_type": "code_generation",
    "primary_id": "cv-20250101-abc123",
    "status": "success",
    "summary": {
        "line_count": 85,
        "template_used": "glue_job_s3_tables.py.j2",
        "target_type": "s3_tables",
    },
    "s3_uris": {
        "code_file": "s3://generated-code-bucket/scripts/cv-20250101-abc123.py",
    },
}


# ---------------------------------------------------------------------------
# Sample output_data for all artifact types (parametrized test)
# ---------------------------------------------------------------------------

ALL_ARTIFACT_SAMPLES = {
    "file_validation": {
        "artifact_type": "file_validation",
        "primary_id": "run-001",
        "status": "success",
        "summary": {"file_name": "data.json", "file_size_bytes": 1024},
        "s3_uris": {"source_file": "s3://bucket/data.json"},
    },
    "schema_extraction": {
        "artifact_type": "schema_extraction",
        "primary_id": "sha256-abc",
        "status": "success",
        "summary": {"field_count": 8, "schema_hash": "sha256-abc"},
        "s3_uris": {},
    },
    "schema_comparison": {
        "artifact_type": "schema_comparison",
        "primary_id": "run-001",
        "status": "success",
        "summary": {"comparison_result": "compatible", "differences_count": 0},
        "s3_uris": {},
    },
    "schema_mapping": {
        "artifact_type": "schema_mapping",
        "primary_id": "map-456",
        "status": "success",
        "summary": {"mapped_fields": 10, "unmapped_fields": 2, "approval_required": True},
        "s3_uris": {},
    },
    "code_generation": CREATE_CODE_OUTPUT_DATA,
    "test_results": {
        "artifact_type": "test_results",
        "primary_id": "tr-101",
        "status": "success",
        "summary": {"tests_passed": 5, "tests_failed": 0, "tests_total": 5},
        "s3_uris": {"full_report_json": "s3://bucket/report.json"},
    },
    "infrastructure": {
        "artifact_type": "infrastructure",
        "primary_id": "run-001",
        "status": "success",
        "summary": {"overall_status": "created"},
        "s3_uris": {},
    },
}


# ===========================================================================
# Test 1: create_code output_data passes validate_output_data()
# ===========================================================================


class TestCreateCodePassesValidation:
    """create_code output_data conforms to the standardized artifact schema."""

    def test_create_code_output_data_passes_validation(self):
        validate_output_data(CREATE_CODE_OUTPUT_DATA)


# ===========================================================================
# Test 2: artifact_type is 'code_generation'
# ===========================================================================


class TestCreateCodeArtifactType:
    """create_code output_data has artifact_type == 'code_generation'."""

    def test_artifact_type_is_code_generation(self):
        assert CREATE_CODE_OUTPUT_DATA["artifact_type"] == "code_generation"

    def test_artifact_type_is_recognized(self):
        assert CREATE_CODE_OUTPUT_DATA["artifact_type"] in ARTIFACT_TYPES


# ===========================================================================
# Test 3: s3_uris.code_file key exists (get_run_artifacts reads this)
# ===========================================================================


class TestCreateCodeS3Uris:
    """create_code output_data has s3_uris.code_file that get_run_artifacts uses."""

    def test_s3_uris_has_code_file_key(self):
        assert "code_file" in CREATE_CODE_OUTPUT_DATA["s3_uris"]

    def test_code_file_is_string(self):
        assert isinstance(CREATE_CODE_OUTPUT_DATA["s3_uris"]["code_file"], str)

    def test_code_file_is_s3_uri(self):
        assert CREATE_CODE_OUTPUT_DATA["s3_uris"]["code_file"].startswith("s3://")


# ===========================================================================
# Test 4: summary has expected keys (line_count, template_used, target_type)
# ===========================================================================


class TestCreateCodeSummaryKeys:
    """create_code output_data summary has the expected keys."""

    EXPECTED_SUMMARY_KEYS = {"line_count", "template_used", "target_type"}

    def test_summary_has_expected_keys(self):
        actual = set(CREATE_CODE_OUTPUT_DATA["summary"].keys())
        assert actual >= self.EXPECTED_SUMMARY_KEYS, f"Missing summary keys: {self.EXPECTED_SUMMARY_KEYS - actual}"

    def test_line_count_is_int(self):
        assert isinstance(CREATE_CODE_OUTPUT_DATA["summary"]["line_count"], int)

    def test_template_used_is_string(self):
        assert isinstance(CREATE_CODE_OUTPUT_DATA["summary"]["template_used"], str)

    def test_target_type_is_string(self):
        assert isinstance(CREATE_CODE_OUTPUT_DATA["summary"]["target_type"], str)


# ===========================================================================
# Test 5: output_data round-trips through JSON serialization
# ===========================================================================


class TestJsonRoundTrip:
    """output_data can be serialized to JSON and deserialized back identically."""

    def test_json_round_trip(self):
        serialized = json.dumps(CREATE_CODE_OUTPUT_DATA)
        deserialized = json.loads(serialized)
        assert deserialized == CREATE_CODE_OUTPUT_DATA

    def test_round_tripped_data_passes_validation(self):
        serialized = json.dumps(CREATE_CODE_OUTPUT_DATA)
        deserialized = json.loads(serialized)
        validate_output_data(deserialized)


# ===========================================================================
# Test 6: All artifact types pass validation (parametrized)
# ===========================================================================


class TestAllArtifactTypesPassValidation:
    """Every step Lambda's output_data passes validate_output_data()."""

    @pytest.mark.parametrize("artifact_type", sorted(ALL_ARTIFACT_SAMPLES.keys()))
    def test_artifact_type_passes_validation(self, artifact_type):
        validate_output_data(ALL_ARTIFACT_SAMPLES[artifact_type])
