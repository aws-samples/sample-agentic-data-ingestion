# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for lineage building in the execution updater.

Verifies:
- Node creation for each step type (file_validation, schema_extract,
  schema_mapping, CodeGeneration, RunPipeline).
- Skip on missing artifact identifier (returns None, logs warning).
- Node structure validation (all required fields present).
- Lineage append is called from _process_record on success.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8
"""

import importlib
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (same pattern as sibling test files)
# ---------------------------------------------------------------------------

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_execution_updater",
    )
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

_ENV = {
    "EXECUTIONS_TABLE": "test-executions",
    "PIPELINES_TABLE": "test-pipelines",
    "ERRORS_TABLE": "test-errors",
    "STATE_TABLE": "test-state",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AGENTIC_DATA_INGESTION_ENV": "local",
}

mock_executions_table = MagicMock()


def _load_module():
    """Import lambda_function with boto3 and powertools mocked out."""
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_executions_table

    with patch.dict(os.environ, _ENV), patch.dict("sys.modules", {"aws_lambda_powertools": MagicMock()}):
        with patch("boto3.resource", return_value=mock_dynamo):
            sys.modules.pop("lambda_function", None)
            if SHARED_DIR not in sys.path:
                sys.path.insert(0, SHARED_DIR)
            spec = importlib.util.spec_from_file_location(
                "lambda_function",
                os.path.join(LAMBDA_DIR, "lambda_function.py"),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


_mod = _load_module()
_build_lineage_node = _mod._build_lineage_node

FIXED_NOW = "2025-01-15T12:05:30+00:00"
REQUIRED_NODE_FIELDS = {"step_name", "artifact_id", "artifact_type", "s3_uri", "timestamp"}


# ---------------------------------------------------------------------------
# Tests — file_validation node creation (Req 1.3)
# ---------------------------------------------------------------------------


class TestFileValidationNode:
    """_build_lineage_node creates correct node for file_validation step."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_file_validation_extracts_filename(self, _mock_now):
        new_image = {
            "original_s3_uri": {"S": "s3://bucket/uploads/customers.csv"},
            "timestamp": {"S": "2025-01-15T12:00:00Z"},
        }
        node = _build_lineage_node("file_validation", new_image)

        assert node is not None
        assert node["step_name"] == "file_validation"
        assert node["artifact_type"] == "source_file"
        assert node["artifact_id"] == "customers.csv"
        assert node["s3_uri"] == "s3://bucket/uploads/customers.csv"
        assert node["timestamp"] == "2025-01-15T12:00:00Z"

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_file_validation_uses_iso_now_when_no_timestamp(self, _mock_now):
        new_image = {
            "original_s3_uri": {"S": "s3://bucket/data.json"},
        }
        node = _build_lineage_node("file_validation", new_image)

        assert node is not None
        assert node["timestamp"] == FIXED_NOW


# ---------------------------------------------------------------------------
# Tests — schema_extract node creation (Req 1.4)
# ---------------------------------------------------------------------------


class TestSchemaExtractNode:
    """_build_lineage_node creates correct node for schema_extract step."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_schema_extract_uses_schema_hash(self, _mock_now):
        new_image = {
            "schema_hash": {"S": "abc123def456"},
            "json_s3_uri": {"S": "s3://bucket/schemas/abc123def456.json"},
            "timestamp": {"S": "2025-01-15T12:01:00Z"},
        }
        node = _build_lineage_node("schema_extract", new_image)

        assert node is not None
        assert node["step_name"] == "schema_extract"
        assert node["artifact_type"] == "schema"
        assert node["artifact_id"] == "abc123def456"
        assert node["s3_uri"] == "s3://bucket/schemas/abc123def456.json"


# ---------------------------------------------------------------------------
# Tests — schema_mapping node creation (Req 1.5)
# ---------------------------------------------------------------------------


class TestSchemaMappingNode:
    """_build_lineage_node creates correct node for schema_mapping step."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_schema_mapping_uses_mapping_id(self, _mock_now):
        new_image = {
            "mapping_id": {"S": "map-001"},
            "json_s3_uri": {"S": "s3://bucket/mappings/map-001.json"},
            "timestamp": {"S": "2025-01-15T12:02:00Z"},
        }
        node = _build_lineage_node("schema_mapping", new_image)

        assert node is not None
        assert node["step_name"] == "schema_mapping"
        assert node["artifact_type"] == "mapping"
        assert node["artifact_id"] == "map-001"
        assert node["s3_uri"] == "s3://bucket/mappings/map-001.json"


# ---------------------------------------------------------------------------
# Tests — CodeGeneration node creation (Req 1.6)
# ---------------------------------------------------------------------------


class TestCodeGenerationNode:
    """_build_lineage_node creates correct node for CodeGeneration step."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_code_generation_uses_code_version_id(self, _mock_now):
        new_image = {
            "code_version_id": {"S": "v-42"},
            "json_s3_uri": {"S": "s3://bucket/code/v-42.py"},
            "timestamp": {"S": "2025-01-15T12:03:00Z"},
        }
        node = _build_lineage_node("CodeGeneration", new_image)

        assert node is not None
        assert node["step_name"] == "CodeGeneration"
        assert node["artifact_type"] == "code_version"
        assert node["artifact_id"] == "v-42"
        assert node["s3_uri"] == "s3://bucket/code/v-42.py"


# ---------------------------------------------------------------------------
# Tests — RunPipeline node creation (Req 1.7)
# ---------------------------------------------------------------------------


class TestRunPipelineNode:
    """_build_lineage_node creates correct node for RunPipeline step."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_run_pipeline_uses_glue_job_run_id(self, _mock_now):
        new_image = {
            "glue_job_run_id": {"S": "jr_abc123"},
            "json_s3_uri": {"S": "s3://bucket/runs/jr_abc123.json"},
            "timestamp": {"S": "2025-01-15T12:04:00Z"},
        }
        node = _build_lineage_node("RunPipeline", new_image)

        assert node is not None
        assert node["step_name"] == "RunPipeline"
        assert node["artifact_type"] == "glue_job_run"
        assert node["artifact_id"] == "jr_abc123"
        assert node["s3_uri"] == "s3://bucket/runs/jr_abc123.json"

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_run_pipeline_from_output_data(self, _mock_now):
        """Falls back to output_data when top-level field is missing."""
        new_image = {
            "output_data": {
                "M": {
                    "glue_job_run_id": {"S": "jr_from_output"},
                }
            },
            "json_s3_uri": {"S": "s3://bucket/runs/output.json"},
            "timestamp": {"S": "2025-01-15T12:04:30Z"},
        }
        node = _build_lineage_node("RunPipeline", new_image)

        assert node is not None
        assert node["artifact_id"] == "jr_from_output"


# ---------------------------------------------------------------------------
# Tests — Skip on missing artifact (Req 1.8)
# ---------------------------------------------------------------------------


class TestSkipOnMissingArtifact:
    """_build_lineage_node returns None when artifact identifier is missing."""

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_skip_file_validation_no_s3_uri(self, _mock_now):
        new_image = {"timestamp": {"S": "2025-01-15T12:00:00Z"}}
        node = _build_lineage_node("file_validation", new_image)
        assert node is None

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_skip_schema_extract_no_hash(self, _mock_now):
        new_image = {"timestamp": {"S": "2025-01-15T12:01:00Z"}}
        node = _build_lineage_node("schema_extract", new_image)
        assert node is None

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_skip_unknown_step(self, _mock_now):
        new_image = {"some_field": {"S": "value"}}
        node = _build_lineage_node("unknown_step", new_image)
        assert node is None


# ---------------------------------------------------------------------------
# Tests — Node structure validation (Req 1.2)
# ---------------------------------------------------------------------------


class TestNodeStructure:
    """Every lineage node contains all required fields."""

    @pytest.mark.parametrize(
        "step_name, new_image",
        [
            ("file_validation", {"original_s3_uri": {"S": "s3://b/f.csv"}, "timestamp": {"S": "2025-01-15T00:00:00Z"}}),
            (
                "schema_extract",
                {
                    "schema_hash": {"S": "h1"},
                    "json_s3_uri": {"S": "s3://b/s.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "schema_mapping",
                {
                    "mapping_id": {"S": "m1"},
                    "json_s3_uri": {"S": "s3://b/m.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "CodeGeneration",
                {
                    "code_version_id": {"S": "c1"},
                    "json_s3_uri": {"S": "s3://b/c.py"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "RunPipeline",
                {
                    "glue_job_run_id": {"S": "r1"},
                    "json_s3_uri": {"S": "s3://b/r.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
        ],
    )
    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_node_has_all_required_fields(self, _mock_now, step_name, new_image):
        node = _build_lineage_node(step_name, new_image)
        assert node is not None
        assert set(node.keys()) == REQUIRED_NODE_FIELDS

    @pytest.mark.parametrize(
        "step_name, new_image",
        [
            ("file_validation", {"original_s3_uri": {"S": "s3://b/f.csv"}, "timestamp": {"S": "2025-01-15T00:00:00Z"}}),
            (
                "schema_extract",
                {
                    "schema_hash": {"S": "h1"},
                    "json_s3_uri": {"S": "s3://b/s.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "schema_mapping",
                {
                    "mapping_id": {"S": "m1"},
                    "json_s3_uri": {"S": "s3://b/m.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "CodeGeneration",
                {
                    "code_version_id": {"S": "c1"},
                    "json_s3_uri": {"S": "s3://b/c.py"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
            (
                "RunPipeline",
                {
                    "glue_job_run_id": {"S": "r1"},
                    "json_s3_uri": {"S": "s3://b/r.json"},
                    "timestamp": {"S": "2025-01-15T00:00:00Z"},
                },
            ),
        ],
    )
    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_node_values_are_strings(self, _mock_now, step_name, new_image):
        node = _build_lineage_node(step_name, new_image)
        assert node is not None
        for value in node.values():
            assert isinstance(value, str)


# ---------------------------------------------------------------------------
# Tests — Lineage append from _process_record (Req 1.1)
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-test-001"
RUN_ID = "run-2025-01-15-12-00-00-abcd1234"


def _stream_record(step_name, status, extra_attrs=None):
    """Build a DynamoDB Stream record for _process_record tests."""
    new_image = {
        "run_id": {"S": RUN_ID},
        "pipeline_id": {"S": PIPELINE_ID},
        "step_name": {"S": step_name},
        "status": {"S": status},
    }
    if extra_attrs:
        new_image.update(extra_attrs)
    return {
        "eventID": f"evt-{step_name}-{status}",
        "eventName": "MODIFY",
        "dynamodb": {"NewImage": new_image},
    }


class TestLineageAppendFromProcessRecord:
    """_process_record appends lineage node on successful step completion."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        mock_executions_table.reset_mock()
        yield

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_lineage_appended_on_success(self, _mock_now):
        record = _stream_record(
            "schema_extract",
            "success",
            extra_attrs={
                "schema_hash": {"S": "hash123"},
                "json_s3_uri": {"S": "s3://bucket/schema.json"},
            },
        )
        _mod._process_record(record)

        # Find the lineage update call (uses list_append)
        lineage_calls = [c for c in mock_executions_table.update_item.call_args_list if "list_append" in str(c)]
        assert len(lineage_calls) == 1
        kwargs = lineage_calls[0][1]
        assert kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        node_list = kwargs["ExpressionAttributeValues"][":node"]
        assert len(node_list) == 1
        assert node_list[0]["artifact_id"] == "hash123"

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_lineage_not_appended_on_failure(self, _mock_now):
        record = _stream_record(
            "schema_extract",
            "failed",
            extra_attrs={"schema_hash": {"S": "hash123"}},
        )
        _mod._process_record(record)

        lineage_calls = [c for c in mock_executions_table.update_item.call_args_list if "list_append" in str(c)]
        assert len(lineage_calls) == 0

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_lineage_not_appended_for_unknown_step(self, _mock_now):
        record = _stream_record("approval_check", "completed")
        _mod._process_record(record)

        lineage_calls = [c for c in mock_executions_table.update_item.call_args_list if "list_append" in str(c)]
        assert len(lineage_calls) == 0

    @patch.object(_mod, "_iso_now", return_value=FIXED_NOW)
    def test_lineage_appended_on_completed_status(self, _mock_now):
        record = _stream_record(
            "file_validation",
            "completed",
            extra_attrs={"original_s3_uri": {"S": "s3://b/data.csv"}},
        )
        _mod._process_record(record)

        lineage_calls = [c for c in mock_executions_table.update_item.call_args_list if "list_append" in str(c)]
        assert len(lineage_calls) == 1
