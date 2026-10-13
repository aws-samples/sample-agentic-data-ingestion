# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code silent-fallback removal (P28 R3).

- AI transform failure raises StepFailure — the silent ``df_transformed = df``
  passthrough is gone (R3.3).
- A missing/unresolvable field mapping raises StepFailure naming the mapping —
  the fabricated ``default_passthrough`` mapping is gone (R3.4).
- Happy path with a valid mapping is unaffected.

**Validates: Requirements 3.3, 3.4, 3.6**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy dependencies
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(
        extract_code_only=MagicMock(return_value="# code"),
        validate_generated_code=MagicMock(return_value={"is_valid": True, "errors": [], "summary": {}}),
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(
        load_prompt_template=MagicMock(return_value="mock prompt"),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "artifact_schema": MagicMock(
        create_output_data=MagicMock(return_value={}),
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


SAMPLE_MAPPINGS = [
    {"source_field": "id", "source_type": "integer", "target_field": "id", "target_type": "integer"},
    {"source_field": "name", "source_type": "string", "target_field": "full_name", "target_type": "string"},
]


# ── R3.3: AI transform failure fails the step ──────────────────────────


def test_ai_transform_failure_raises_step_failure():
    """When the AI call fails, generate_transform_with_ai raises StepFailure
    carrying the AI error — it never returns passthrough code.

    **Validates: Requirements 3.3, 3.6**
    """
    ai_error = RuntimeError("bedrock exploded")
    with (
        patch.object(_mod, "Agent", MagicMock(side_effect=ai_error)),
        pytest.raises(_mod.StepFailure) as exc_info,
    ):
        _mod.generate_transform_with_ai(mappings=SAMPLE_MAPPINGS)

    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "AI transform generation failed" in exc.message
    assert "bedrock exploded" in exc.message
    assert exc.__cause__ is ai_error
    assert exc.details["original_error_type"] == "RuntimeError"
    assert exc.details["mapping_count"] == len(SAMPLE_MAPPINGS)


def test_ai_transform_failure_produces_no_artifact():
    """The failure propagates out of generate_code — no passthrough script is
    assembled or returned when the AI call fails.

    **Validates: Requirements 3.3, 3.6**
    """
    pipeline_metadata = {
        "target_config": {
            "type": "s3_tables",
            "bucket_name": "b",
            "namespace_name": "n",
            "table_name": "t",
        },
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "source_schema": {"properties": {"id": {"type": "integer"}}},
    }
    mapping_item = {"field_mappings": SAMPLE_MAPPINGS}

    with (
        patch.object(_mod, "Agent", MagicMock(side_effect=RuntimeError("bedrock exploded"))),
        pytest.raises(_mod.StepFailure),
    ):
        _mod.generate_code(pipeline_metadata, mapping_item)


def test_empty_mappings_passthrough_is_designed_flow():
    """A real mapping record with ZERO field mappings (nothing to transform)
    still yields the passthrough transform — this is the designed no-op flow,
    not failure masking, and must survive the R3.3 removal.
    """
    assert _mod.generate_transform_with_ai(mappings=[]) == "df_transformed = df"


# ── R3.4: missing mapping fails the step, naming the mapping ───────────


def _handler_patches(mappings_get_item_response):
    """Common patch set to drive lambda_handler to the mapping-resolution code."""
    return (
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": {
                    "type": "s3_tables",
                    "bucket_name": "b",
                    "namespace_name": "n",
                    "table_name": "t",
                }
            },
        ),
        patch.object(
            _mod,
            "pipeline_runs_table",
            MagicMock(
                get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
                query=MagicMock(return_value={"Items": []}),
                put_item=MagicMock(),
            ),
        ),
        patch.object(
            _mod,
            "mappings_table",
            MagicMock(get_item=MagicMock(return_value=mappings_get_item_response)),
        ),
        patch.object(_mod, "publish_pipeline_state_change"),
    )


def test_upstream_mapping_failure_raises_step_failure():
    """A `mapping_failed` status in the event fails the step instead of
    fabricating a default_passthrough mapping.

    **Validates: Requirements 3.4, 3.6**
    """
    event = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "mapping_id": "m-1",
        "mapping_failed": 500,
    }
    p1, p2, p3, p4 = _handler_patches({})
    with p1, p2, p3, p4, pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(event, None)

    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "schema mapping failed" in exc.message.lower()
    assert exc.details["mapping_failed_status"] == 500
    assert exc.details["mapping_id"] == "m-1"


def test_missing_mapping_id_raises_step_failure():
    """An empty mapping_id fails the step instead of fabricating a 'default'
    passthrough mapping.

    **Validates: Requirements 3.4, 3.6**
    """
    event = {"run_id": "run-2", "pipeline_id": "pipe-2", "mapping_id": ""}
    p1, p2, p3, p4 = _handler_patches({})
    with p1, p2, p3, p4, pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(event, None)

    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "no mapping_id" in exc.message.lower()
    assert exc.details["mapping_id"] in (None, "")


def test_mapping_not_found_raises_step_failure_naming_mapping():
    """A mapping_id that resolves to no record fails the step with a message
    NAMING the missing mapping — no fabricated default_passthrough.

    **Validates: Requirements 3.4, 3.6**
    """
    event = {"run_id": "run-3", "pipeline_id": "pipe-3", "mapping_id": "missing-mapping-123"}
    p1, p2, p3, p4 = _handler_patches({})  # mappings_table.get_item → no Item
    with p1, p2, p3, p4, pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(event, None)

    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "missing-mapping-123" in exc.message
    assert "not found" in exc.message
    assert exc.details["mapping_id"] == "missing-mapping-123"


def test_specific_step_failure_survives_outer_handler_wrap():
    """The handler's outer except must re-raise the specific StepFailure as-is
    (message + details intact), not double-wrap its JSON serialization.

    **Validates: Requirements 3.4, 3.6**
    """
    event = {"run_id": "run-4", "pipeline_id": "pipe-4", "mapping_id": "gone-42"}
    p1, p2, p3, p4 = _handler_patches({})
    with p1, p2, p3, p4, pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(event, None)

    # A double-wrapped failure would carry the serialized JSON as message.
    assert not exc_info.value.message.startswith("{")
    assert exc_info.value.details["mapping_id"] == "gone-42"


# ── Happy path: valid mapping unaffected ────────────────────────────────


def test_happy_path_with_valid_mapping_unaffected():
    """A resolvable, approved mapping still generates code and returns 200.

    **Validates: Requirements 3.6**
    """
    event = {"run_id": "run-ok", "pipeline_id": "pipe-ok", "mapping_id": "m-ok"}
    # p33 task 4.6 harness amendment, NOT an assertion change. The two schema
    # hashes are added to the fixture: they were absent and the handler
    # substituted `str(uuid.uuid4())` for each, while requirement 2.9 makes their
    # absence a StepFailure because a generated identifier matches no schema.
    # `store_mapping_separately` writes both on every real record
    # (`schema_mapping/lambda_function.py:1166-1169`), so a resolvable, approved
    # mapping — which is exactly what this test is about — carries them.
    p1, p2, p3, p4 = _handler_patches(
        {"Item": {"mapping_id": "m-ok", "source_schema_hash": "src-hash-m-ok", "target_schema_hash": "tgt-hash-m-ok"}}
    )
    with (
        p1,
        p2,
        p3,
        p4,
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "generate_code", return_value="# code\ndf = spark.read.json('s3://b/k')\n"),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    ):
        result = _mod.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert result["next_step"] == "code_testing"
