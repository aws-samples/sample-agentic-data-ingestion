# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for schema mapping Lambda context integration (Task 4).

Tests mapping_context from pipeline metadata, model override from event,
and mapping_hints integration in the AI prompt.
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
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock())

_mock_memory_module = MagicMock()
_mock_memory_module.MappingMemoryClient = MagicMock()
sys.modules.setdefault("mapping_memory", _mock_memory_module)

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_module():
    sys.modules["lambda_function"] = _mod
    yield


# --- Helpers ---


def _make_event(**overrides):
    base = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "source_schema_hash": "abc",
        "additional_context": "",
    }
    base.update(overrides)
    return base


def _source_schema():
    return {"properties": {"fname": {"type": "string"}, "lname": {"type": "string"}}}


def _target_schema():
    return {"properties": {"first_name": {"type": "string"}, "last_name": {"type": "string"}}}


def _mapping_result():
    """A mapping covering both fields `_source_schema()` declares.

    AMENDED for p55 task 3.1. This fixture previously mapped `fname` only, of the
    two fields the source schema declares — a Source_Field_Mappability_Ratio of
    0.5, which `enforce_source_field_mappability`
    (`schema_mapping/lambda_function.py:1815`) now refuses at or below
    `MAPPABILITY_HARD_FAIL_THRESHOLD` (p55 R14.1). Every test in this file is
    about the *context* and the *model id* handed to the generator, so the
    refusal fired before the seam any of them observes. The `lname → last_name`
    entry is the amendment; nothing else about the fixture changed, and no
    assertion in this file reads the mapping's contents.
    """
    return {
        "mappings": [
            {"source_field": "fname", "target_field": "first_name", "confidence": 0.9, "transformation": "rename"},
            {"source_field": "lname", "target_field": "last_name", "confidence": 0.9, "transformation": "rename"},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "Mapped",
    }


# Shared decorator stack for all handler tests
_PATCHES = [
    "lambda_function.get_source_schema",
    "lambda_function.table",
    "lambda_function.get_pipeline_metadata",
    "lambda_function.get_target_schema",
    "lambda_function.normalize_schema_format",
    "lambda_function.get_ai_model_for_pipeline",
    "lambda_function.generate_mapping_with_bedrock",
    "lambda_function.generate_mapping_id",
    "lambda_function.store_mapping_separately",
    "lambda_function.determine_approval_requirement",
    "lambda_function.store_mapping_result",
    "lambda_function.publish_pipeline_state_change",
]


def _setup_mocks(mocks, pipeline_metadata=None):
    """Configure the standard mock chain. mocks is the list of mock args in _PATCHES order."""
    (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    ) = mocks
    m_get_source.return_value = _source_schema()
    m_table.get_item.return_value = {"Item": {"original_pipeline_id": "pipe-1"}}
    m_get_meta.return_value = pipeline_metadata or {"pipeline_id": "pipe-1"}
    m_get_target.return_value = (_target_schema(), "tgt-hash")
    m_normalize.side_effect = lambda s: s
    m_get_model.return_value = "us.anthropic.claude-3-haiku-20240307-v1:0"
    m_bedrock.return_value = _mapping_result()
    m_gen_id.return_value = "map-1"
    m_approval.return_value = False
    return m_bedrock, m_get_model


# --- 4.1: mapping_context from pipeline metadata ---


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_stored_and_event_context_merged(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """Stored mapping_context + event additional_context are merged (stored first)."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, _ = _setup_mocks(
        mocks,
        pipeline_metadata={"pipeline_id": "pipe-1", "mapping_context": "Map fname to first_name"},
    )

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(additional_context="Also ignore internal_notes"), None)

    assert result["statusCode"] == 200
    ctx_arg = m_bedrock_ref.call_args[0][2]
    assert ctx_arg.startswith("Map fname to first_name")
    assert "Also ignore internal_notes" in ctx_arg


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_stored_context_only(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """When only stored mapping_context exists, it becomes the additional_context."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, _ = _setup_mocks(
        mocks,
        pipeline_metadata={"pipeline_id": "pipe-1", "mapping_context": "Stored only context"},
    )

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(additional_context=""), None)

    assert result["statusCode"] == 200
    ctx_arg = m_bedrock_ref.call_args[0][2]
    assert "Stored only context" in ctx_arg


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_event_context_only(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """When only event additional_context exists, it is used as-is."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, _ = _setup_mocks(mocks, pipeline_metadata={"pipeline_id": "pipe-1"})

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(additional_context="Event only"), None)

    assert result["statusCode"] == 200
    ctx_arg = m_bedrock_ref.call_args[0][2]
    assert "Event only" in ctx_arg


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_neither_context(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """When neither stored nor event context exists, additional_context is empty."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, _ = _setup_mocks(mocks, pipeline_metadata={"pipeline_id": "pipe-1"})

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(additional_context=""), None)

    assert result["statusCode"] == 200
    ctx_arg = m_bedrock_ref.call_args[0][2]
    assert ctx_arg == ""


# --- 4.2: Model override from event ---


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_model_override_from_event(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """When bedrock_model_id is in the event, it overrides the pipeline's ai_model."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, m_get_model_ref = _setup_mocks(mocks, pipeline_metadata={"pipeline_id": "pipe-1"})

    override_model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(bedrock_model_id=override_model), None)

    assert result["statusCode"] == 200
    # get_ai_model_for_pipeline should NOT be called when event override is present
    m_get_model_ref.assert_not_called()
    # The model passed to generate_mapping_with_bedrock should be the override
    model_arg = m_bedrock_ref.call_args[0][3]
    assert model_arg == override_model


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_model_fallback_to_pipeline(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """When bedrock_model_id is absent from event, falls back to pipeline's ai_model."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, m_get_model_ref = _setup_mocks(mocks, pipeline_metadata={"pipeline_id": "pipe-1"})
    m_get_model_ref.return_value = "us.anthropic.claude-3-haiku-20240307-v1:0"

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    m_get_model_ref.assert_called_once_with("pipe-1")
    model_arg = m_bedrock_ref.call_args[0][3]
    assert model_arg == "us.anthropic.claude-3-haiku-20240307-v1:0"


# --- 4.4: mapping_hints integration ---


@patch(_PATCHES[11])
@patch(_PATCHES[10])
@patch(_PATCHES[9])
@patch(_PATCHES[8])
@patch(_PATCHES[7])
@patch(_PATCHES[6])
@patch(_PATCHES[5])
@patch(_PATCHES[4])
@patch(_PATCHES[3])
@patch(_PATCHES[2])
@patch(_PATCHES[1])
@patch(_PATCHES[0])
def test_mapping_hints_included_in_prompt(
    m_get_source,
    m_table,
    m_get_meta,
    m_get_target,
    m_normalize,
    m_get_model,
    m_bedrock,
    m_gen_id,
    m_store_sep,
    m_approval,
    m_store_result,
    m_publish,
):
    """mapping_hints from pipeline metadata are formatted as field override instructions."""
    mocks = (
        m_get_source,
        m_table,
        m_get_meta,
        m_get_target,
        m_normalize,
        m_get_model,
        m_bedrock,
        m_gen_id,
        m_store_sep,
        m_approval,
        m_store_result,
        m_publish,
    )
    m_bedrock_ref, _ = _setup_mocks(
        mocks,
        pipeline_metadata={
            "pipeline_id": "pipe-1",
            "mapping_hints": {"fname": "first_name", "lname": "last_name"},
        },
    )

    with patch.object(_mod, "memory_client", None):
        result = _mod.lambda_handler(_make_event(), None)

    assert result["statusCode"] == 200
    ctx_arg = m_bedrock_ref.call_args[0][2]
    assert "The user has specified these field mappings:" in ctx_arg
    assert "fname" in ctx_arg
    assert "first_name" in ctx_arg
    assert "lname" in ctx_arg
    assert "last_name" in ctx_arg
