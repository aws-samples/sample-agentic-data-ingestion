# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_mapping Converse call routed through invoke_model (P28 R15, Task 12.2).

The single direct ``bedrock.converse(`` call site in the codebase (schema
mapping's AI generation) must:

- carry explicit ``maxTokens`` AND ``temperature`` from shared configuration
  on every Converse call (R15.1),
- thread ``run_id`` from the handler into the call so the per-run model-call
  budget is consulted (R15.2),
- raise the budget-exceeded StepFailure out of the handler (never degrade
  into the substring-match fallback) and never reach Bedrock once the
  budget is exhausted (R15.2 → honest failure per R1).

**Validates: Requirements 15.1, 15.2, 15.3**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add Lambda dir and shared dir to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub out Lambda-layer dependencies (mirrors test_step_failure_raise.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod

    import model_config
    from model_config import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
    from step_errors import StepFailure
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _clean_budget_state(monkeypatch):
    """Isolate every test from env config and the container budget cache."""
    sys.modules["lambda_function"] = _mod
    monkeypatch.delenv("MODEL_MAX_TOKENS", raising=False)
    monkeypatch.delenv("MODEL_TEMPERATURE", raising=False)
    monkeypatch.delenv("MODEL_CALL_BUDGET", raising=False)
    monkeypatch.delenv(model_config.BUDGET_TABLE_ENV, raising=False)
    model_config._BUDGETS.clear()
    yield
    model_config._BUDGETS.clear()


def _source_schema():
    return {"properties": {"id": {"type": "string"}, "email": {"type": "string"}}}


def _target_schema():
    return {"properties": {"user_id": {"type": "string"}, "email": {"type": "string"}}}


def _ai_converse_response():
    body = {
        "mappings": [{"target_field": "email", "source_field": "email", "confidence": 0.97}],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "ai mapping",
    }
    return {
        "output": {"message": {"content": [{"text": json.dumps(body)}]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }


# --- R15.1: explicit maxTokens AND temperature on every Converse call ---


def test_converse_call_carries_explicit_max_tokens_and_temperature():
    """The mapping-generation Converse call sets both params from shared config."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = _ai_converse_response()
        _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    inference_config = mock_bedrock.converse.call_args.kwargs["inferenceConfig"]
    assert inference_config["maxTokens"] == DEFAULT_MAX_TOKENS  # documented default 4000
    assert inference_config["temperature"] == DEFAULT_TEMPERATURE  # documented default 0.0


def test_shared_env_config_governs_the_call(monkeypatch):
    """MODEL_MAX_TOKENS/MODEL_TEMPERATURE env config reaches the Converse call (R15.3)."""
    monkeypatch.setenv("MODEL_MAX_TOKENS", "1234")
    monkeypatch.setenv("MODEL_TEMPERATURE", "0.5")
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = _ai_converse_response()
        _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    inference_config = mock_bedrock.converse.call_args.kwargs["inferenceConfig"]
    assert inference_config == {"maxTokens": 1234, "temperature": 0.5}


def test_result_contract_preserved_through_wrapper():
    """Provenance/_token_usage/_prompt_text/_response_text survive the routing (R3.1 contract)."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = _ai_converse_response()
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "us.test-model"
    assert result["_token_usage"] == {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 0}
    assert "SOURCE SCHEMA" in result["_prompt_text"]
    assert result["_response_text"]


# --- R15.2: run_id threading — the per-run budget is consulted ---


def test_run_id_threads_into_budget_enforcement():
    """Passing run_id consults (increments) that run's call budget."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = _ai_converse_response()
        _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model", run_id="run-t1")

    assert "run-t1" in model_config._BUDGETS
    assert model_config._BUDGETS["run-t1"].local_count == 1


def test_budget_exceeded_raises_step_failure_and_never_calls_converse(monkeypatch):
    """An exhausted budget raises the permanent StepFailure before reaching Bedrock,
    and never degrades into the substring-match fallback."""
    monkeypatch.setenv("MODEL_CALL_BUDGET", "1")
    model_config.get_call_budget("run-t2").check_and_increment()  # exhaust the budget

    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        with pytest.raises(StepFailure) as exc_info:
            _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model", run_id="run-t2")
        mock_bedrock.converse.assert_not_called()

    exc = exc_info.value
    assert exc.error_type == "permanent"
    assert "model call budget exceeded" in exc.message
    assert exc.step_name == "schema_mapping"


def test_no_run_id_keeps_call_working_without_budget():
    """Without run_id (defensive default) the call still carries explicit params."""
    with patch.object(_mod, "bedrock_runtime") as mock_bedrock:
        mock_bedrock.converse.return_value = _ai_converse_response()
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model")

    assert result["generated_by"] == "us.test-model"
    assert model_config._BUDGETS == {}  # no budget consulted


# --- Handler threading and propagation (R15.2 → R1) ---


def _handler_patches():
    """Patches for everything the handler touches before/around mapping generation."""
    return (
        patch.object(_mod, "get_source_schema", return_value=_source_schema()),
        patch.object(_mod, "table"),
        patch.object(_mod, "get_pipeline_metadata", return_value={"pipeline_id": "pipe-1"}),
        patch.object(_mod, "get_target_schema", return_value=(_target_schema(), "tgt-hash")),
        patch.object(_mod, "get_ai_model_for_pipeline", return_value="us.test-model"),
        patch.object(_mod, "AgentTracePlugin"),
        patch.object(_mod, "publish_pipeline_state_change"),
    )


def test_handler_threads_run_id_into_mapping_generation():
    """lambda_handler passes the event's run_id to generate_mapping_with_bedrock."""
    patches = _handler_patches()
    with (
        patches[0],
        patches[1] as mock_table,
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch.object(_mod, "generate_mapping_with_bedrock") as mock_generate,
    ):
        mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "pipe-1"}}
        # Raising here ends the handler right after the call we care about —
        # no need to mock the entire downstream persistence chain.
        mock_generate.side_effect = StepFailure(
            "schema_mapping", "model call budget exceeded: 51 calls > limit 50 for run run-h1"
        )
        with pytest.raises(StepFailure):
            _mod.lambda_handler({"run_id": "run-h1", "source_schema_hash": "h1"}, None)

    assert mock_generate.call_args.kwargs["run_id"] == "run-h1"


def test_budget_step_failure_propagates_out_of_handler():
    """Budget exhaustion fails the step honestly — StepFailure escapes lambda_handler
    with the budget message intact (never a 200 with a fallback mapping)."""
    patches = _handler_patches()
    with (
        patches[0],
        patches[1] as mock_table,
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch.object(_mod, "generate_mapping_with_bedrock") as mock_generate,
    ):
        mock_table.get_item.return_value = {"Item": {"original_pipeline_id": "pipe-1"}}
        mock_generate.side_effect = StepFailure(
            "schema_mapping",
            "model call budget exceeded: 51 calls > limit 50 for run run-h2",
            error_type="permanent",
        )
        with pytest.raises(StepFailure) as exc_info:
            _mod.lambda_handler({"run_id": "run-h2", "source_schema_hash": "h1"}, None)

    exc = exc_info.value
    assert exc.step_name == "schema_mapping"
    assert exc.error_type == "permanent"
    assert "model call budget exceeded" in exc.message
