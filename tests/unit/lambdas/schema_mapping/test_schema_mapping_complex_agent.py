# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for complex mapping agent invocation from schema_mapping Lambda (Task 3).

Validates: Requirements 3.1, 3.2, 5.1, 5.2, 11.1, 11.2, 12.1, 12.4
- 3.1: Low confidence → agent invoked
- 3.2: At/above threshold → agent NOT invoked
- 5.1: Empty endpoint → skip invocation
- 5.2: Agent error/exception → step still returns valid result
- 11.1: Pipeline proceeds normally when agent is skipped
- 11.2: Pipeline proceeds normally when agent fails
"""

import json
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
# Also add shared directory
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, _shared_dir)

# Stub out Lambda-layer dependencies that are not in shared/
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(
    require_env=lambda name, default=None: default or name,
    get_aws_account_id=lambda: "123456789012",
)
sys.modules.setdefault("artifact_schema", MagicMock(create_output_data=lambda **kwargs: {}))
sys.modules.setdefault("metrics_facade", MagicMock())
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))
sys.modules.setdefault("agent_trace_plugin", MagicMock())
sys.modules.setdefault("audit_utils", MagicMock())
sys.modules.setdefault(
    "dynamo_typing",
    MagicMock(
        item_str=lambda item, key: item.get(key, ""),
        item_int=lambda item, key: item.get(key, 0),
    ),
)
sys.modules.setdefault(
    "model_config", MagicMock(resolve_model_id=lambda: "us.anthropic.claude-3-5-sonnet-20241022-v2:0")
)

# Stub aws_lambda_powertools
_mock_logger = MagicMock()
_mock_logger.return_value = MagicMock()
sys.modules.setdefault("aws_lambda_powertools", MagicMock(Logger=_mock_logger))

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

lambda_handler = _schema_mapping_module.lambda_handler


@pytest.fixture(autouse=True)
def _ensure_correct_lambda_module():
    """Ensure sys.modules['lambda_function'] points to schema_mapping's module."""
    sys.modules["lambda_function"] = _schema_mapping_module
    yield


# --- Helpers ---


def _make_event(run_id="test-run-1"):
    return {
        "run_id": run_id,
        "pipeline_id": "test-pipeline",
        "source_schema_hash": "abc123",
        "additional_context": "",
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
            "customer_id": {"type": "string"},
            "first_name": {"type": "string"},
            "email": {"type": "string"},
        }
    }


def _low_confidence_mapping():
    """Mapping result with confidence below 0.7 threshold."""
    return {
        "mappings": [
            {
                "source_field": "cust_id",
                "target_field": "customer_id",
                "confidence": 0.55,
                "transformation": "direct",
                "notes": "",
            },
            {
                "source_field": "fname",
                "target_field": "first_name",
                "confidence": 0.60,
                "transformation": "direct",
                "notes": "",
            },
            {
                "source_field": "email_addr",
                "target_field": "email",
                "confidence": 0.50,
                "transformation": "direct",
                "notes": "",
            },
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "Low confidence mapping",
    }


def _high_confidence_mapping():
    """Mapping result with confidence at or above 0.7 threshold."""
    return {
        "mappings": [
            {
                "source_field": "cust_id",
                "target_field": "customer_id",
                "confidence": 0.95,
                "transformation": "direct",
                "notes": "",
            },
            {
                "source_field": "fname",
                "target_field": "first_name",
                "confidence": 0.92,
                "transformation": "direct",
                "notes": "",
            },
            {
                "source_field": "email_addr",
                "target_field": "email",
                "confidence": 0.98,
                "transformation": "direct",
                "notes": "",
            },
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "High confidence mapping",
    }


def _agent_success_response():
    """Successful agent response with evidence and questions."""
    return {
        "evidence": {
            "cust_id": {
                "sample_values": ["C001", "C002"],
                "reasoning": "Direct ID mapping",
                "memory_matches": 3,
                "confidence_before": 0.55,
                "confidence_after": 0.90,
            },
            "fname": {
                "sample_values": ["Alice", "Bob"],
                "reasoning": "First name abbreviation",
                "memory_matches": 2,
                "confidence_before": 0.60,
                "confidence_after": 0.85,
            },
        },
        "questions": [
            {
                "field": "email_addr",
                "question": "Should email_addr map to email or contact_email?",
                "options": ["email", "contact_email"],
                "sample_values": ["a@b.com", "c@d.com"],
                "current_confidence": 0.50,
            }
        ],
        "mappings": [
            {"source": "cust_id", "target": "customer_id", "confidence": 0.90},
        ],
    }


def _agent_error_response():
    """Agent returns an error dict."""
    return {
        "statusCode": 500,
        "error": {"message": "Model invocation failed", "type": "RuntimeError"},
    }


@pytest.fixture
def mock_env_with_endpoint(monkeypatch):
    """Set up environment with complex mapping endpoint configured."""
    monkeypatch.setenv(
        "COMPLEX_MAPPING_AGENT_ENDPOINT",
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/complex-mapping-123/invocations?qualifier=DEFAULT",
    )
    monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.7")


@pytest.fixture
def mock_env_no_endpoint(monkeypatch):
    """Set up environment without complex mapping endpoint (empty)."""
    monkeypatch.setenv("COMPLEX_MAPPING_AGENT_ENDPOINT", "")
    monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.7")


def _mock_get_item_schema_extract(run_id="test-run-1", sample_s3_uri=None):
    """Mock DynamoDB get_item for schema_extract step with json_s3_uri.

    ``sample_s3_uri`` is p53's additional attribute. Omitting it reproduces the
    pre-p53 Item, which is also the shape ``store_schema_in_dynamodb`` writes
    when no Sample_Artifact exists — it omits the key rather than storing ``""``
    (``schema_extract:1364-1368``).
    """
    item = {
        "run_id": run_id,
        "step_name": "schema_extract",
        "original_pipeline_id": "test-pipeline-001",
        "json_s3_uri": "s3://agentic-data-ingestion-data/extracts/test-run-1/sample.json",
        "extracted_schema": json.dumps(_source_schema()),
    }
    if sample_s3_uri is not None:
        item["sample_s3_uri"] = sample_s3_uri
    return {"Item": item}


# --- Tests ---


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.publish_approval_status_change")
@patch("lambda_function.AgentTracePlugin")
@patch("lambda_function.write_agent_audit_entry")
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
def test_low_confidence_agent_invoked_and_evidence_persisted(
    mock_table,
    mock_get_pipeline_meta,
    mock_get_target_schema,
    mock_generate_mapping,
    mock_store_result,
    mock_store_separately,
    mock_audit,
    mock_trace_plugin,
    mock_publish_approval,
    mock_publish_state,
    mock_env_with_endpoint,
):
    """When confidence < threshold and endpoint configured, agent is invoked and evidence persisted."""
    # Setup mocks
    mock_table.get_item.return_value = _mock_get_item_schema_extract()
    mock_get_pipeline_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
    mock_get_target_schema.return_value = (_target_schema(), "target-hash-123")
    mock_generate_mapping.return_value = _low_confidence_mapping()
    mock_trace_plugin.return_value = MagicMock()

    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=_agent_success_response()) as mock_invoke:
        result = lambda_handler(_make_event(), None)

    # Agent should have been invoked
    assert mock_invoke.called
    call_kwargs = mock_invoke.call_args
    # The function is called positionally: invoke_agentcore_runtime(endpoint=..., payload=...)
    payload = (
        call_kwargs.kwargs.get("payload") or call_kwargs[1].get("payload") if call_kwargs[1] else call_kwargs[0][1]
    )
    assert payload["source_schema"] == _source_schema()
    assert payload["target_schema"] == _target_schema()
    assert payload["sample_data_uri"] == "s3://agentic-data-ingestion-data/extracts/test-run-1/sample.json"
    assert payload["pipeline_id"] == "test-pipeline-001"
    assert payload["run_id"] == "test-run-1"
    assert payload["confidence_threshold"] == 0.7

    # Evidence should be persisted via update_item
    mock_table.update_item.assert_called_once()
    update_call = mock_table.update_item.call_args
    assert update_call[1]["Key"] == {"run_id": "test-run-1", "step_name": "schema_mapping"}
    assert ":ev" in update_call[1]["ExpressionAttributeValues"]
    assert ":qs" in update_call[1]["ExpressionAttributeValues"]

    # Step should complete successfully
    assert result["statusCode"] == 200
    assert result["complex_mapping_eligible"] is True


# --- p53 R2.4/R2.5: consumer 4's sample_data_uri is a preference ------------
#
# This is the consumer the commissioning brief did not name. The test above
# stays unmodified and is the absent case (its Item carries no `sample_s3_uri`).
# The two below cover present and present-but-empty.
#
# The agent's own reader — `agentcore/complex_mapping_agent/main.py`
# `read_sample_data` — is NOT touched, including its WARNING log level on a
# declined URI. p53 C4 records why that warning is expected on run 1 of a JDBC
# pipeline and why changing the level is a separate judgement.


def _agent_payload_sample_uri(sample_s3_uri):
    """The `sample_data_uri` the agent payload carries, for one Item shape."""
    patches = [
        patch("lambda_function.publish_pipeline_state_change"),
        patch("lambda_function.publish_approval_status_change"),
        patch("lambda_function.write_agent_audit_entry"),
        patch("lambda_function.store_mapping_separately"),
        patch("lambda_function.store_mapping_result"),
    ]
    for patcher in patches:
        patcher.start()
    try:
        with (
            patch("lambda_function.AgentTracePlugin", return_value=MagicMock()),
            patch("lambda_function.table") as mock_table,
            patch("lambda_function.get_pipeline_metadata") as mock_meta,
            patch("lambda_function.get_target_schema") as mock_target,
            patch("lambda_function.generate_mapping_with_bedrock") as mock_generate,
            patch("agentcore_proxy.invoke_agentcore_runtime", return_value=_agent_success_response()) as mock_invoke,
        ):
            mock_table.get_item.return_value = _mock_get_item_schema_extract(sample_s3_uri=sample_s3_uri)
            mock_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
            mock_target.return_value = (_target_schema(), "target-hash-123")
            mock_generate.return_value = _low_confidence_mapping()

            result = lambda_handler(_make_event(), None)
    finally:
        for patcher in patches:
            patcher.stop()

    assert result["statusCode"] == 200
    assert mock_invoke.called
    return mock_invoke.call_args.kwargs["payload"]["sample_data_uri"]


def test_agent_payload_prefers_the_sample_uri_when_the_row_records_one(mock_env_with_endpoint):
    """p53 R2.4: consumer 4 gets the Sample_Artifact when one exists.

    Validates: Requirements 2.4
    """
    given = _agent_payload_sample_uri("s3://adi-processed-files-1-r/samples/test-pipeline-001/source-sample.json")

    assert given == "s3://adi-processed-files-1-r/samples/test-pipeline-001/source-sample.json"


def test_agent_payload_falls_back_when_the_sample_uri_is_empty(mock_env_with_endpoint):
    """p53 R2.5: present-but-empty is treated as absent.

    Validates: Requirements 2.5
    """
    given = _agent_payload_sample_uri("")

    assert given == "s3://agentic-data-ingestion-data/extracts/test-run-1/sample.json"


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.publish_approval_status_change")
@patch("lambda_function.AgentTracePlugin")
@patch("lambda_function.write_agent_audit_entry")
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
def test_high_confidence_agent_not_invoked(
    mock_table,
    mock_get_pipeline_meta,
    mock_get_target_schema,
    mock_generate_mapping,
    mock_store_result,
    mock_store_separately,
    mock_audit,
    mock_trace_plugin,
    mock_publish_approval,
    mock_publish_state,
    mock_env_with_endpoint,
):
    """When confidence >= threshold, agent is NOT invoked."""
    mock_table.get_item.return_value = _mock_get_item_schema_extract()
    mock_get_pipeline_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
    mock_get_target_schema.return_value = (_target_schema(), "target-hash-123")
    mock_generate_mapping.return_value = _high_confidence_mapping()
    mock_trace_plugin.return_value = MagicMock()

    with patch("agentcore_proxy.invoke_agentcore_runtime") as mock_invoke:
        result = lambda_handler(_make_event(), None)

    # Agent should NOT be invoked (confidence is above threshold)
    mock_invoke.assert_not_called()
    assert result["statusCode"] == 200
    assert result["complex_mapping_eligible"] is False


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.publish_approval_status_change")
@patch("lambda_function.AgentTracePlugin")
@patch("lambda_function.write_agent_audit_entry")
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
def test_empty_endpoint_agent_skipped(
    mock_table,
    mock_get_pipeline_meta,
    mock_get_target_schema,
    mock_generate_mapping,
    mock_store_result,
    mock_store_separately,
    mock_audit,
    mock_trace_plugin,
    mock_publish_approval,
    mock_publish_state,
    mock_env_no_endpoint,
):
    """When COMPLEX_MAPPING_AGENT_ENDPOINT is empty, agent invocation is skipped."""
    mock_table.get_item.return_value = _mock_get_item_schema_extract()
    mock_get_pipeline_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
    mock_get_target_schema.return_value = (_target_schema(), "target-hash-123")
    mock_generate_mapping.return_value = _low_confidence_mapping()
    mock_trace_plugin.return_value = MagicMock()

    with patch("agentcore_proxy.invoke_agentcore_runtime") as mock_invoke:
        result = lambda_handler(_make_event(), None)

    # Agent should NOT be invoked (endpoint empty)
    mock_invoke.assert_not_called()
    # Step should still succeed
    assert result["statusCode"] == 200
    assert result["complex_mapping_eligible"] is True


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.publish_approval_status_change")
@patch("lambda_function.AgentTracePlugin")
@patch("lambda_function.write_agent_audit_entry")
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
def test_agent_error_response_step_still_succeeds(
    mock_table,
    mock_get_pipeline_meta,
    mock_get_target_schema,
    mock_generate_mapping,
    mock_store_result,
    mock_store_separately,
    mock_audit,
    mock_trace_plugin,
    mock_publish_approval,
    mock_publish_state,
    mock_env_with_endpoint,
):
    """When agent returns an error dict, the step completes normally (failure-safe)."""
    mock_table.get_item.return_value = _mock_get_item_schema_extract()
    mock_get_pipeline_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
    mock_get_target_schema.return_value = (_target_schema(), "target-hash-123")
    mock_generate_mapping.return_value = _low_confidence_mapping()
    mock_trace_plugin.return_value = MagicMock()

    with patch("agentcore_proxy.invoke_agentcore_runtime", return_value=_agent_error_response()):
        result = lambda_handler(_make_event(), None)

    # Step should still succeed despite agent error
    assert result["statusCode"] == 200
    assert result["complex_mapping_eligible"] is True
    # No update_item for evidence (agent failed)
    mock_table.update_item.assert_not_called()


@patch("lambda_function.publish_pipeline_state_change")
@patch("lambda_function.publish_approval_status_change")
@patch("lambda_function.AgentTracePlugin")
@patch("lambda_function.write_agent_audit_entry")
@patch("lambda_function.store_mapping_separately")
@patch("lambda_function.store_mapping_result")
@patch("lambda_function.generate_mapping_with_bedrock")
@patch("lambda_function.get_target_schema")
@patch("lambda_function.get_pipeline_metadata")
@patch("lambda_function.table")
def test_agent_exception_step_still_succeeds(
    mock_table,
    mock_get_pipeline_meta,
    mock_get_target_schema,
    mock_generate_mapping,
    mock_store_result,
    mock_store_separately,
    mock_audit,
    mock_trace_plugin,
    mock_publish_approval,
    mock_publish_state,
    mock_env_with_endpoint,
):
    """When agent invocation raises an exception, the step completes normally (failure-safe)."""
    mock_table.get_item.return_value = _mock_get_item_schema_extract()
    mock_get_pipeline_meta.return_value = {"pipeline_id": "test-pipeline-001", "target_schema_name": "test"}
    mock_get_target_schema.return_value = (_target_schema(), "target-hash-123")
    mock_generate_mapping.return_value = _low_confidence_mapping()
    mock_trace_plugin.return_value = MagicMock()

    with patch("agentcore_proxy.invoke_agentcore_runtime", side_effect=RuntimeError("Connection timeout")):
        result = lambda_handler(_make_event(), None)

    # Step should still succeed despite exception
    assert result["statusCode"] == 200
    assert result["complex_mapping_eligible"] is True
    # No update_item for evidence (agent raised)
    mock_table.update_item.assert_not_called()
