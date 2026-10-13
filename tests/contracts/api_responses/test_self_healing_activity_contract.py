# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: self-healing outcome record shape (P28 task 8.5, R9.7).

The dashboard's ``use-self-healing.ts`` (``SelfHealingActivity`` interface)
renders the record that ``GET /api/pipelines/{id}/runs/{runId}/self-healing``
returns verbatim from the state table — the record the self-healing agent
writes. This contract pins that shape so agent-side changes cannot silently
break the dashboard:

* dashboard-consumed fields: agent_type, error_classification,
  remediation_applied, original_run_id, rerun_id, rerun_status,
  diagnosis_duration_ms, total_duration_ms, timestamp
* design fields (P28 R9.7): healing_id, root_cause, action, rerun_started

``rerun_status`` lifecycle: ``running`` (rerun started; job_completion later
links the result and flips it to ``success``/``failed``), ``start_failed``,
``skipped``, or ``skipped_heal_guard``. The linker additionally sets
``rerun_result``/``linked_run_id`` — additive, so the base shape is stable.

**Validates: Requirements 9.7**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import jsonschema
import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

# Import the agent module with runtime dependencies mocked; the record
# builder under contract is a pure function.
_mock_boto3 = MagicMock()
_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("sha_contract_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_contract_main"] = sha
    _spec.loader.exec_module(sha)


# JSON Schema for the outcome record — the SelfHealingActivity interface
# fields plus the P28 R9.7 design fields.
SELF_HEALING_ACTIVITY_SCHEMA = {
    "type": "object",
    "properties": {
        # Table identity
        "run_id": {"type": "string", "minLength": 1},
        "step_name": {"const": "self_healing"},
        # Dashboard-consumed fields (use-self-healing.ts)
        "agent_type": {"const": "self_healing"},
        "error_classification": {"type": "string"},
        "remediation_applied": {"type": "string"},
        "original_run_id": {"type": "string", "minLength": 1},
        "rerun_id": {"type": "string"},
        "rerun_status": {"enum": ["running", "start_failed", "skipped", "success", "failed"]},
        "diagnosis_duration_ms": {"type": "number"},
        "total_duration_ms": {"type": "number"},
        "timestamp": {"type": "string", "minLength": 1},
        # Design fields (P28 R9.7)
        "healing_id": {"type": "string", "pattern": "^heal-"},
        "root_cause": {"type": "string"},
        "action": {"type": "string"},
        "rerun_started": {"type": "boolean"},
        # Optional extras
        "original_pipeline_id": {"type": "string"},
        "rerun_execution_arn": {"type": "string"},
        "rerun_start_error": {"type": "string"},
    },
    "required": [
        "run_id",
        "step_name",
        "agent_type",
        "error_classification",
        "remediation_applied",
        "original_run_id",
        "rerun_id",
        "rerun_status",
        "diagnosis_duration_ms",
        "total_duration_ms",
        "timestamp",
        "healing_id",
        "root_cause",
        "action",
        "rerun_started",
    ],
    "additionalProperties": True,
}


def _build(**overrides):
    kwargs = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "healing_id": "heal-abc123def456",
        "error_classification": "schema_mismatch",
        "remediation_action": "rerun_pipeline",
        "remediation_description": "Rerun through the standard pipeline.",
        "rerun_id": "run-1-heal",
        "rerun_started": True,
        "rerun_status": "running",
        "diagnosis_duration_ms": 3,
        "total_duration_ms": 120,
    }
    kwargs.update(overrides)
    return sha.build_outcome_record(**kwargs)


class TestSelfHealingOutcomeRecordContract:
    def test_started_rerun_record_matches_schema(self):
        record = _build(rerun_execution_arn="arn:aws:states:us-west-2:123:execution:sm:x")
        jsonschema.validate(record, SELF_HEALING_ACTIVITY_SCHEMA)
        assert record["rerun_started"] is True
        assert record["rerun_status"] == "running"

    def test_failed_start_record_matches_schema(self):
        record = _build(
            rerun_id="",
            rerun_started=False,
            rerun_status="start_failed",
            rerun_start_error="States throttled",
        )
        jsonschema.validate(record, SELF_HEALING_ACTIVITY_SCHEMA)
        assert record["rerun_started"] is False
        assert record["rerun_start_error"] == "States throttled"

    def test_skipped_record_matches_schema(self):
        record = _build(
            error_classification="unknown",
            remediation_action="flag_for_review",
            remediation_description="Manual intervention required.",
            rerun_id="",
            rerun_started=False,
            rerun_status="skipped",
        )
        jsonschema.validate(record, SELF_HEALING_ACTIVITY_SCHEMA)

    def test_design_names_track_dashboard_aliases(self):
        """root_cause/error_classification are the same value under design
        and dashboard names — one truth, two consumers."""
        record = _build()
        assert record["root_cause"] == record["error_classification"]

    def test_empty_optional_fields_are_omitted_not_blank(self):
        """DynamoDB rejects empty-string writes in some positions and the
        dashboard treats absence as 'not applicable' — empty ARN/error must
        be omitted."""
        record = _build()
        assert "rerun_execution_arn" not in record
        assert "rerun_start_error" not in record

    def test_healing_records_never_carry_the_gsi_partition_key(self):
        """``pipeline_id`` is the PipelineIndex GSI partition key; healing
        records deliberately use ``original_pipeline_id`` so they never
        surface in pipeline-step GSI queries (mapping lookups)."""
        record = _build()
        assert "pipeline_id" not in record
        assert record["original_pipeline_id"] == "pipe-1"

    def test_invalid_rerun_status_rejected_by_schema(self):
        """Guard the schema itself: the pre-P28 'timeout' status (from the
        deleted 10-minute verify_rerun poll) is no longer a legal value."""
        record = _build(rerun_status="timeout")
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(record, SELF_HEALING_ACTIVITY_SCHEMA)
