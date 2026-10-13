# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for MappingMemoryClient integration in schema mapping Lambda (Tasks 11.1–11.5).

Validates: AC-14, AC-15, AC-12
- AC-14: Before calling Bedrock, the schema mapping Lambda queries Memory for similar past mappings
- AC-15: Historical mapping context is included in the Bedrock prompt
- AC-12: Each approved mapping decision is stored in Memory
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# Add lambda directory to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)

# Stub out Lambda-layer dependencies
sys.modules.setdefault(
    "eventbridge_utils", MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock())

# Create a mock MappingMemoryClient class that we can control
_mock_memory_class = MagicMock()
_mock_memory_module = MagicMock()
_mock_memory_module.MappingMemoryClient = _mock_memory_class
sys.modules.setdefault("mapping_memory", _mock_memory_module)

sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _schema_mapping_module
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_correct_lambda_module():
    """Ensure sys.modules['lambda_function'] points to schema_mapping's module."""
    sys.modules["lambda_function"] = _schema_mapping_module
    yield


# --- Helpers ---


def _make_event(run_id="test-run-1", pipeline_id="test-pipeline", additional_context=""):
    return {
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "source_schema_hash": "abc123",
        "additional_context": additional_context,
    }


def _source_schema():
    return {
        "properties": {
            "cust_id": {"type": "string"},
            "fname": {"type": "string"},
            "email_addr": {"type": "string"},
        }
    }


def _target_schema():
    return {
        "properties": {
            "customer_id": {"type": "integer"},
            "first_name": {"type": "string"},
            "email": {"type": "string"},
        }
    }


def _mapping_result():
    return {
        "mappings": [
            {
                "source_field": "cust_id",
                "target_field": "customer_id",
                "confidence": 0.95,
                "transformation": "cast to integer",
            },
            {"source_field": "fname", "target_field": "first_name", "confidence": 0.92, "transformation": "rename"},
            {"source_field": "email_addr", "target_field": "email", "confidence": 0.99, "transformation": "rename"},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "All fields mapped successfully",
    }


def _similar_mappings():
    return [
        {
            "text": "Field 'cust_id' maps to 'customer_id' with 95% confidence. Status: approved. Pipeline: old-pipeline.",
            "score": 0.95,
            "record_id": "r1",
        },
        {
            "text": "Field 'fname' maps to 'first_name' with 92% confidence. Status: approved. Pipeline: old-pipeline.",
            "score": 0.90,
            "record_id": "r2",
        },
    ]


# --- Tests: Memory query before Bedrock call (AC-14) ---


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_memory_queried_before_bedrock(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """Memory is queried for similar mappings before calling Bedrock (AC-14)."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s  # pass-through
    mock_bedrock.return_value = _mapping_result()

    mock_mem = MagicMock()
    mock_mem.get_similar_mappings.return_value = _similar_mappings()
    mock_mem.format_historical_context.return_value = "## Historical Mapping Patterns\n..."
    mock_mem.store_mapping_decision.return_value = True

    with patch.object(_schema_mapping_module, "memory_client", mock_mem):
        result = _schema_mapping_module.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    mock_mem.get_similar_mappings.assert_called_once_with(["cust_id", "fname", "email_addr"])
    mock_mem.format_historical_context.assert_called_once_with(_similar_mappings())


# --- Tests: Historical context passed to Bedrock (AC-15) ---


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_historical_context_passed_to_bedrock(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """Historical context from Memory is included in the Bedrock prompt (AC-15)."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s
    mock_bedrock.return_value = _mapping_result()

    historical = "## Historical Mapping Patterns\n| cust_id | customer_id | 95% | 5 | 0 |"
    mock_mem = MagicMock()
    mock_mem.get_similar_mappings.return_value = _similar_mappings()
    mock_mem.format_historical_context.return_value = historical
    mock_mem.store_mapping_decision.return_value = True

    with patch.object(_schema_mapping_module, "memory_client", mock_mem):
        _schema_mapping_module.lambda_handler(_make_event(), None)

    # Verify Bedrock was called with the historical context as additional_context
    bedrock_call_args = mock_bedrock.call_args
    actual_context = (
        bedrock_call_args[0][2] if len(bedrock_call_args[0]) > 2 else bedrock_call_args[1].get("additional_context", "")
    )
    assert "## Historical Mapping Patterns" in actual_context


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_user_context_concatenated_with_historical(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """When both user-supplied and historical context exist, they are concatenated."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s
    mock_bedrock.return_value = _mapping_result()

    historical = "## Historical Mapping Patterns\nsome patterns"
    mock_mem = MagicMock()
    mock_mem.get_similar_mappings.return_value = _similar_mappings()
    mock_mem.format_historical_context.return_value = historical
    mock_mem.store_mapping_decision.return_value = True

    user_context = "Map cust_id to customer_id always"
    with patch.object(_schema_mapping_module, "memory_client", mock_mem):
        _schema_mapping_module.lambda_handler(_make_event(additional_context=user_context), None)

    bedrock_call_args = mock_bedrock.call_args
    actual_context = (
        bedrock_call_args[0][2] if len(bedrock_call_args[0]) > 2 else bedrock_call_args[1].get("additional_context", "")
    )
    assert "## Historical Mapping Patterns" in actual_context
    assert "Map cust_id to customer_id always" in actual_context


# --- Tests: Mapping decisions stored in Memory (AC-12) ---


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_mapping_decisions_not_stored_at_generation_time(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """Mapping decisions are NOT stored in Memory at generation time — only after approval (AC-12)."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s
    mock_bedrock.return_value = _mapping_result()

    mock_mem = MagicMock()
    mock_mem.get_similar_mappings.return_value = []
    mock_mem.format_historical_context.return_value = ""
    mock_mem.store_mapping_decision.return_value = True

    with patch.object(_schema_mapping_module, "memory_client", mock_mem):
        result = _schema_mapping_module.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    # Memory storage now happens only at approval time, not generation time
    mock_mem.store_mapping_decision.assert_not_called()


# --- Tests: Graceful degradation when Memory is unavailable ---


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_handler_succeeds_when_memory_client_is_none(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """Lambda succeeds even when memory_client is None (MappingMemoryClient not available)."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s
    mock_bedrock.return_value = _mapping_result()

    with patch.object(_schema_mapping_module, "memory_client", None):
        result = _schema_mapping_module.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    assert result["mapping_count"] == 3


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.determine_approval_requirement", return_value=False)
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.generate_mapping_id", return_value="mapping-123")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_ai_model_for_pipeline", return_value="us.anthropic.claude-3-haiku-20240307-v1:0")
@patch("lambda_function.normalize_schema_format")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
@patch("lambda_function.get_source_schema")
def test_handler_succeeds_when_memory_query_fails(
    mock_get_source,
    mock_table,
    mock_get_meta,
    mock_get_target,
    mock_normalize,
    mock_get_model,
    mock_bedrock,
    mock_gen_id,
    mock_store_sep,
    mock_approval,
    mock_store_result,
    mock_publish,
):
    """Lambda succeeds even when Memory query raises an exception."""
    mock_get_source.return_value = _source_schema()
    mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "test-pipeline"}}
    mock_get_meta.return_value = {"pipeline_id": "test-pipeline"}
    mock_get_target.return_value = (_target_schema(), "target-hash")
    mock_normalize.side_effect = lambda s: s
    mock_bedrock.return_value = _mapping_result()

    mock_mem = MagicMock()
    mock_mem.get_similar_mappings.side_effect = Exception("Memory service unavailable")
    mock_mem.store_mapping_decision.return_value = True

    with patch.object(_schema_mapping_module, "memory_client", mock_mem):
        result = _schema_mapping_module.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    # Bedrock should still be called (without historical context)
    mock_bedrock.assert_called_once()
