# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_mapping publishes NO fabricated (ghost) approval event.

P28 R19.5: the handler used to fabricate an approval_id
(``{run_id}_schema_mapping_{timestamp}``) and publish an 'Approval Status
Change' event for it when approval was required — an id that never had an
approvals-table record. The approval_handler owns the real approval record
and publishes the honest event with the real id.

Validates: Requirements 19.5
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
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, _shared_dir)

# Stub out Lambda-layer dependencies (same harness as test_schema_mapping_complex_agent.py)
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


@pytest.fixture
def _no_complex_agent(monkeypatch):
    """Disable the complex-mapping agent path (empty endpoint)."""
    monkeypatch.setenv("COMPLEX_MAPPING_AGENT_ENDPOINT", "")
    monkeypatch.setenv("COMPLEX_MAPPING_THRESHOLD", "0.7")


# --- Helpers ---


def _source_schema():
    return {
        "properties": {
            "cust_id": {"type": "string"},
            "fname": {"type": "string"},
        }
    }


def _target_schema():
    return {
        "properties": {
            "customer_id": {"type": "string"},
            "first_name": {"type": "string"},
        }
    }


def _high_confidence_mapping():
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
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "High confidence mapping",
    }


def _mock_get_item_schema_extract(run_id="test-run-ghost"):
    return {
        "Item": {
            "run_id": run_id,
            "step_name": "schema_extract",
            "original_pipeline_id": "test-pipeline-001",
            "json_s3_uri": "s3://agentic-data-ingestion-data/extracts/test-run-ghost/sample.json",
            "extracted_schema": json.dumps(_source_schema()),
        }
    }


def _run_handler(approval_required: bool):
    event = {
        "run_id": "test-run-ghost",
        "pipeline_id": "test-pipeline",
        "source_schema_hash": "abc123",
        "additional_context": "",
    }

    with (
        patch.object(_schema_mapping_module, "publish_pipeline_state_change") as mock_publish_state,
        patch.object(_schema_mapping_module, "publish_approval_status_change") as mock_publish_approval,
        patch.object(
            _schema_mapping_module,
            "table",
            MagicMock(get_item=MagicMock(return_value=_mock_get_item_schema_extract())),
        ),
        patch.object(
            _schema_mapping_module,
            "get_pipeline_metadata",
            return_value={"pipeline_id": "test-pipeline-001", "target_schema_name": "test"},
        ),
        patch.object(
            _schema_mapping_module,
            "get_target_schema",
            return_value=(_target_schema(), "target-hash-123"),
        ),
        patch.object(
            _schema_mapping_module,
            "generate_mapping_with_bedrock",
            return_value=_high_confidence_mapping(),
        ),
        patch.object(_schema_mapping_module, "store_mapping_result"),
        patch.object(_schema_mapping_module, "store_mapping_separately"),
        patch.object(_schema_mapping_module, "write_agent_audit_entry"),
        patch.object(_schema_mapping_module, "AgentTracePlugin", MagicMock()),
        patch.object(
            _schema_mapping_module,
            "determine_approval_requirement",
            return_value=approval_required,
        ),
    ):
        result = lambda_handler(event, None)
    return result, mock_publish_state, mock_publish_approval


# --- Tests ---


def test_approval_required_publishes_no_ghost_approval_event(_no_complex_agent):
    """approval_required=True → NO 'Approval Status Change' event from this step."""
    result, mock_publish_state, mock_publish_approval = _run_handler(approval_required=True)

    assert result["statusCode"] == 200
    # The ghost publication is gone — the approval_handler owns the honest event.
    mock_publish_approval.assert_not_called()
    # The regular pipeline-state event still goes out.
    mock_publish_state.assert_called_once()


def test_no_approval_required_also_publishes_no_approval_event(_no_complex_agent):
    result, _, mock_publish_approval = _run_handler(approval_required=False)

    assert result["statusCode"] == 200
    mock_publish_approval.assert_not_called()


def test_no_fabricated_approval_id_pattern_in_source():
    """The fabricated '{run_id}_schema_mapping_{timestamp}' id is gone from the handler."""
    import inspect

    source = inspect.getsource(_schema_mapping_module)
    assert "_schema_mapping_{int(datetime" not in source
