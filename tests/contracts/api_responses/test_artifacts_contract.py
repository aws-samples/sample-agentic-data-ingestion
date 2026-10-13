# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Contract test: GET /artifacts response shape matches RunArtifacts TypeScript interface.

Validates that get_run_artifacts() returns a body whose shape is:

    interface RunArtifacts {
        pipeline_id: string;
        run_id: string;
        artifacts: Record<string, ArtifactData>;
        code_content: string | null;
    }

    interface ArtifactData {
        artifact_type: string;
        primary_id: string;
        status: 'success' | 'failed' | 'skipped';
        summary: Record<string, unknown>;
        s3_uris: Record<string, string>;
    }
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
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
# Constants
# ---------------------------------------------------------------------------
PIPELINE_ID = "pipe-contract-001"
RUN_ID = "run-contract-abc"
VALID_STATUSES = {"success", "failed", "skipped"}
RUN_ARTIFACTS_KEYS = {"pipeline_id", "run_id", "artifacts", "code_content"}
ARTIFACT_DATA_KEYS = {"artifact_type", "primary_id", "status", "summary", "s3_uris"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _make_step(step_name: str, run_id: str, output_data: dict = None) -> dict:
    item = {"step_name": step_name, "run_id": run_id}
    if output_data is not None:
        item["output_data"] = output_data
    return item


# ---------------------------------------------------------------------------
# Sample artifacts covering all three valid statuses
# ---------------------------------------------------------------------------
SAMPLE_STEPS = [
    _make_step(
        "file_validation",
        RUN_ID,
        {
            "artifact_type": "file_validation",
            "primary_id": "fv-1",
            "status": "success",
            "summary": {"file_name": "data.json", "file_size_bytes": 1024},
            "s3_uris": {"source_file": "s3://bucket/data.json"},
        },
    ),
    _make_step(
        "schema_extraction",
        RUN_ID,
        {
            "artifact_type": "schema_extraction",
            "primary_id": "se-1",
            "status": "success",
            "summary": {"field_count": 5},
            "s3_uris": {},
        },
    ),
    _make_step(
        "test_results",
        RUN_ID,
        {
            "artifact_type": "test_results",
            "primary_id": "tr-1",
            "status": "failed",
            "summary": {"tests_passed": 3, "tests_failed": 1},
            "s3_uris": {"full_report_json": "s3://bucket/report.json"},
        },
    ),
    _make_step(
        "infrastructure",
        RUN_ID,
        {
            "artifact_type": "infrastructure",
            "primary_id": "inf-1",
            "status": "skipped",
            "summary": {},
            "s3_uris": {},
        },
    ),
]


# ---------------------------------------------------------------------------
# Fixture — patch boto resources
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_boto(monkeypatch):
    from handlers import common as _common

    # P21 Task 7.2: get_run_artifacts now lives in handlers.pipelines_runs and
    # reads S3 via handlers.pipelines_artifacts._read_s3_object. Patch the
    # owning modules so the relocated functions see the mocks (module
    # references only — behaviour-preserving).
    from handlers import pipelines_artifacts as _artifacts
    from handlers import pipelines_runs as _runs

    mock_table = MagicMock()
    mock_s3 = MagicMock()

    monkeypatch.setattr(_common, "state_table", mock_table)
    monkeypatch.setattr(_common, "s3_client", mock_s3)
    monkeypatch.setattr(_runs, "state_table", mock_table)
    monkeypatch.setattr(_artifacts, "s3_client", mock_s3)

    _patch_boto.state_table = mock_table
    _patch_boto.s3_client = mock_s3
    yield


def _set_query_items(items: list):
    _patch_boto.state_table.query.return_value = {"Items": items}


# ---------------------------------------------------------------------------
# Import handler under test (after env + path setup)
# ---------------------------------------------------------------------------
from handlers.pipelines import get_run_artifacts

# ===========================================================================
# Contract tests
# ===========================================================================


class TestRunArtifactsTopLevelShape:
    """Response body has exactly the RunArtifacts keys with correct types."""

    def test_response_has_exactly_run_artifacts_keys(self):
        _set_query_items(SAMPLE_STEPS)
        status, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert status == 200
        assert set(body.keys()) == RUN_ARTIFACTS_KEYS

    def test_pipeline_id_is_string(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert isinstance(body["pipeline_id"], str)

    def test_run_id_is_string(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert isinstance(body["run_id"], str)

    def test_code_content_is_string_or_null(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert body["code_content"] is None or isinstance(body["code_content"], str)

    def test_code_content_string_when_code_step_present(self):
        steps = SAMPLE_STEPS + [
            _make_step(
                "code_generation",
                RUN_ID,
                {
                    "artifact_type": "code_generation",
                    "primary_id": "cg-1",
                    "status": "success",
                    "summary": {"line_count": 42},
                    "s3_uris": {"code_file": "s3://bucket/scripts/job.py"},
                },
            ),
        ]
        _set_query_items(steps)

        s3_body = MagicMock()
        s3_body.read.return_value = b"# generated code"
        _patch_boto.s3_client.get_object.return_value = {"Body": s3_body}

        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert isinstance(body["code_content"], str)

    def test_artifacts_is_dict(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        assert isinstance(body["artifacts"], dict)


class TestArtifactDataShape:
    """Each value in artifacts dict matches the ArtifactData interface."""

    def test_each_artifact_has_required_keys(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            assert set(artifact.keys()) >= ARTIFACT_DATA_KEYS, (
                f"Artifact '{name}' missing keys: {ARTIFACT_DATA_KEYS - set(artifact.keys())}"
            )

    def test_artifact_type_is_string(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            assert isinstance(artifact["artifact_type"], str), f"Artifact '{name}' artifact_type is not a string"

    def test_primary_id_is_string(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            assert isinstance(artifact["primary_id"], str), f"Artifact '{name}' primary_id is not a string"

    def test_status_is_valid_enum(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            assert artifact["status"] in VALID_STATUSES, (
                f"Artifact '{name}' status '{artifact['status']}' not in {VALID_STATUSES}"
            )

    def test_summary_is_dict(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            assert isinstance(artifact["summary"], dict), f"Artifact '{name}' summary is not a dict"

    def test_s3_uris_is_dict_with_string_values(self):
        _set_query_items(SAMPLE_STEPS)
        _, body = _parse(get_run_artifacts(PIPELINE_ID, RUN_ID))

        for name, artifact in body["artifacts"].items():
            s3_uris = artifact["s3_uris"]
            assert isinstance(s3_uris, dict), f"Artifact '{name}' s3_uris is not a dict"
            for key, val in s3_uris.items():
                assert isinstance(val, str), f"Artifact '{name}' s3_uris['{key}'] is not a string"
