# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/artifact_schema.py — schema constants and validation."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from artifact_schema import (
    ARTIFACT_TYPES,
    REQUIRED_KEYS,
    VALID_STATUSES,
    create_output_data,
    validate_output_data,
)

# --- Constants ---


def test_artifact_types_contains_all_expected():
    expected = {
        "file_validation",
        "schema_extraction",
        "schema_comparison",
        "schema_mapping",
        "code_generation",
        "test_results",
        "infrastructure",
        "glue_pipeline",
        "quality_checks",
        "run_pipeline",
    }
    assert expected == ARTIFACT_TYPES


def test_valid_statuses():
    assert {"success", "failed", "skipped"} == VALID_STATUSES


def test_required_keys():
    assert {"artifact_type", "primary_id", "status", "summary", "s3_uris"} == REQUIRED_KEYS


def test_constants_are_immutable():
    assert isinstance(ARTIFACT_TYPES, frozenset)
    assert isinstance(VALID_STATUSES, frozenset)
    assert isinstance(REQUIRED_KEYS, frozenset)


# --- validate_output_data ---


def _valid_output():
    return {
        "artifact_type": "code_generation",
        "primary_id": "cv-123",
        "status": "success",
        "summary": {"line_count": 85},
        "s3_uris": {"code_file": "s3://bucket/key"},
    }


def test_validate_accepts_valid_data():
    validate_output_data(_valid_output())


def test_validate_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a dict"):
        validate_output_data("not a dict")


def test_validate_rejects_missing_keys():
    data = _valid_output()
    del data["status"]
    with pytest.raises(ValueError, match="missing required keys"):
        validate_output_data(data)


def test_validate_rejects_invalid_artifact_type():
    data = _valid_output()
    data["artifact_type"] = "unknown_type"
    with pytest.raises(ValueError, match="not valid"):
        validate_output_data(data)


def test_validate_rejects_non_string_artifact_type():
    data = _valid_output()
    data["artifact_type"] = 123
    with pytest.raises(ValueError, match="artifact_type must be str"):
        validate_output_data(data)


def test_validate_rejects_invalid_status():
    data = _valid_output()
    data["status"] = "pending"
    with pytest.raises(ValueError, match="not valid"):
        validate_output_data(data)


def test_validate_rejects_non_dict_summary():
    data = _valid_output()
    data["summary"] = "not a dict"
    with pytest.raises(ValueError, match="summary must be a dict"):
        validate_output_data(data)


def test_validate_rejects_non_dict_s3_uris():
    data = _valid_output()
    data["s3_uris"] = ["s3://bucket/key"]
    with pytest.raises(ValueError, match="s3_uris must be a dict"):
        validate_output_data(data)


def test_validate_rejects_non_string_primary_id():
    data = _valid_output()
    data["primary_id"] = 42
    with pytest.raises(ValueError, match="primary_id must be str"):
        validate_output_data(data)


def test_validate_accepts_empty_summary_and_s3_uris():
    data = _valid_output()
    data["summary"] = {}
    data["s3_uris"] = {}
    validate_output_data(data)


def test_validate_accepts_all_artifact_types():
    for art_type in ARTIFACT_TYPES:
        data = _valid_output()
        data["artifact_type"] = art_type
        validate_output_data(data)


def test_validate_accepts_all_statuses():
    for status in VALID_STATUSES:
        data = _valid_output()
        data["status"] = status
        validate_output_data(data)


# --- create_output_data ---


def test_create_returns_valid_dict():
    result = create_output_data(
        artifact_type="test_results",
        primary_id="tr-456",
        status="failed",
        summary={"tests_passed": 3, "tests_failed": 1},
        s3_uris={"full_report_json": "s3://bucket/report.json"},
    )
    assert result["artifact_type"] == "test_results"
    assert result["primary_id"] == "tr-456"
    assert result["status"] == "failed"
    assert result["summary"] == {"tests_passed": 3, "tests_failed": 1}
    assert result["s3_uris"] == {"full_report_json": "s3://bucket/report.json"}


def test_create_rejects_invalid_artifact_type():
    with pytest.raises(ValueError):
        create_output_data(
            artifact_type="bogus",
            primary_id="id-1",
            status="success",
            summary={},
            s3_uris={},
        )


def test_create_rejects_invalid_status():
    with pytest.raises(ValueError):
        create_output_data(
            artifact_type="code_generation",
            primary_id="id-1",
            status="unknown",
            summary={},
            s3_uris={},
        )
