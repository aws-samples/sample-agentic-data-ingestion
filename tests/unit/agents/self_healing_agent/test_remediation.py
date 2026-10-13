# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for self-healing agent remediation paths and idempotency."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

# Mock heavy dependencies before importing the module
_mock_glue = MagicMock()
_mock_sfn = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: {
    "glue": _mock_glue,
    "stepfunctions": _mock_sfn,
    "logs": MagicMock(),
}.get(svc, MagicMock())
_mock_boto3.resource.return_value = MagicMock()

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    import main as sha_main

    # Patch the module-level clients directly
    sha_main._glue_client = _mock_glue
    sha_main._sfn_client = _mock_sfn


# ── OOM remediation ─────────────────────────────────────────────────


class TestRemediateOom:
    """Test worker type upgrade path for OOM remediation."""

    def _mock_job(self, worker_type="G.1X"):
        _mock_glue.get_job.return_value = {
            "Job": {
                "Name": "agentic-data-ingestion-test-etl",
                "WorkerType": worker_type,
                "NumberOfWorkers": 5,
                "Role": "arn:aws:iam::role/GlueRole",
                "Command": {"Name": "glueetl", "ScriptLocation": "s3://scripts/etl.py"},
                "GlueVersion": "4.0",
            }
        }
        _mock_glue.update_job.return_value = {}

    def test_upgrade_g1x_to_g2x(self):
        self._mock_job("G.1X")
        result = sha_main.remediate_oom("pipe-1", "agentic-data-ingestion-test-etl")
        assert result["action"] == "upgrade_worker_type"
        assert result["previous_worker_type"] == "G.1X"
        assert result["new_worker_type"] == "G.2X"
        _mock_glue.update_job.assert_called_once()

    def test_upgrade_g2x_to_g4x(self):
        self._mock_job("G.2X")
        result = sha_main.remediate_oom("pipe-1", "agentic-data-ingestion-test-etl")
        assert result["action"] == "upgrade_worker_type"
        assert result["new_worker_type"] == "G.4X"

    def test_max_worker_type_returns_no_upgrade(self):
        self._mock_job("G.8X")
        _mock_glue.update_job.reset_mock()
        result = sha_main.remediate_oom("pipe-1", "agentic-data-ingestion-test-etl")
        assert result["action"] == "no_upgrade_available"
        _mock_glue.update_job.assert_not_called()

    def test_idempotent_upgrade(self):
        """Calling remediate_oom twice with same state produces same result."""
        self._mock_job("G.1X")
        r1 = sha_main.remediate_oom("pipe-1", "agentic-data-ingestion-test-etl")
        self._mock_job("G.1X")
        r2 = sha_main.remediate_oom("pipe-1", "agentic-data-ingestion-test-etl")
        assert r1["new_worker_type"] == r2["new_worker_type"]


# ── Retrigger pipeline ──────────────────────────────────────────────


def _wire_pipeline_record(record):
    """Point sha_main._dynamodb at a mock pipelines table returning *record*."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": record} if record is not None else {}
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    sha_main._dynamodb = mock_dynamo
    return mock_table


class TestRetriggerPipeline:
    """P28 R9.1/R9.2: the heal rerun input is the full shared trigger contract."""

    def test_starts_execution(self):
        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD", "source_bucket": "src-bkt"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:run-1"}
        result = sha_main.retrigger_pipeline("pipe-1", "run-1", "Fixed schema")
        assert result["execution_arn"] == "arn:aws:states:us-west-2:123:execution:test:run-1"
        assert result["rerun_id"] == "run-1-heal"
        _mock_sfn.start_execution.assert_called_once()

    def test_execution_input_carries_full_trigger_contract(self):
        """R9.1: source_bucket + source_prefix from the pipeline record; the
        canonical triggered_by='self_healing' Heal_Guard key (R9.4); no
        pre-contract fix_description side channel."""
        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD", "source_bucket": "src-bkt"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:run-2"}
        sha_main.retrigger_pipeline("pipe-1", "run-2", "Upgraded workers")
        call_kwargs = _mock_sfn.start_execution.call_args[1]
        input_data = json.loads(call_kwargs["input"])

        assert input_data["pipeline_id"] == "pipe-1"
        assert input_data["source_type"] == "S3_UPLOAD"
        assert input_data["source_bucket"] == "src-bkt"
        assert input_data["source_prefix"] == "pipe-1"
        assert input_data["trigger_mode"] == "prefix"
        assert input_data["triggered_by"] == "self_healing"
        assert input_data["trigger"] == "self_healing"
        assert input_data["run_id"] == "run-2-heal"
        assert input_data["original_run_id"] == "run-2"
        assert "fix_description" not in input_data

    def test_execution_input_carries_job_config_overrides(self):
        """R9.5 groundwork: remediation config rides the rerun input."""
        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD", "source_bucket": "src-bkt"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:run-3"}
        sha_main.retrigger_pipeline("pipe-1", "run-3", "OOM fix", job_config_overrides={"WorkerType": "G.2X"})
        input_data = json.loads(_mock_sfn.start_execution.call_args[1]["input"])
        assert input_data["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_jdbc_pipeline_gets_jdbc_shape(self):
        """JDBC pipeline records produce the JDBC contract (no S3 source keys)."""
        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record({"pipeline_id": "pipe-jdbc", "source_type": "JDBC"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:run-4"}
        sha_main.retrigger_pipeline("pipe-jdbc", "run-4", "Fixed cast")
        input_data = json.loads(_mock_sfn.start_execution.call_args[1]["input"])
        assert input_data["source_type"] == "JDBC"
        assert "source_bucket" not in input_data
        assert "source_prefix" not in input_data
        assert input_data["triggered_by"] == "self_healing"

    def test_missing_pipeline_record_fails_loudly(self):
        """No pipeline record → ValueError, no execution started (R9.1)."""
        import pytest

        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record(None)
        _mock_sfn.start_execution.reset_mock()
        with pytest.raises(ValueError, match="Pipeline record not found"):
            sha_main.retrigger_pipeline("pipe-gone", "run-5", "whatever")
        _mock_sfn.start_execution.assert_not_called()

    def test_bucketless_record_uses_fallback_chain(self):
        """S3 record without a bucket → SOURCE_BUCKET env fallback."""
        import os as _os
        from unittest.mock import patch as _patch

        sha_main._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:test:run-6"}
        with _patch.dict(_os.environ, {"SOURCE_BUCKET": "env-fallback-bkt"}):
            sha_main.retrigger_pipeline("pipe-1", "run-6", "fix")
        input_data = json.loads(_mock_sfn.start_execution.call_args[1]["input"])
        assert input_data["source_bucket"] == "env-fallback-bkt"


# ── Rerun-based remediation (P28 R9.6) ─────────────────────────────


class TestRemediateByRerun:
    """Code-level failures remediate through the standard pipeline.

    The old remediate_schema_mismatch / remediate_type_cast paths — LLM
    regeneration stored as raw code in DynamoDB, never read, bypassing
    test_code — are deleted (R9.6). The remediation IS the rerun.

    **Validates: Requirements 9.6**
    """

    def test_schema_mismatch_maps_to_rerun_action(self):
        result = sha_main.remediate_by_rerun("schema_mismatch")
        assert result["action"] == "rerun_pipeline"
        assert "standard pipeline" in result["description"]

    def test_type_cast_maps_to_rerun_action(self):
        result = sha_main.remediate_by_rerun("type_cast_error")
        assert result["action"] == "rerun_pipeline"
        assert "standard pipeline" in result["description"]

    def test_no_code_version_side_channel_in_result(self):
        """The pre-R9.6 remediations returned code_version_id pointing at the
        DynamoDB side channel — the rerun remediation carries no such ref."""
        for error_class in ("schema_mismatch", "type_cast_error"):
            result = sha_main.remediate_by_rerun(error_class)
            assert "code_version_id" not in result


class TestSideChannelRemoved:
    """P28 R9.6: the DynamoDB regenerated-code side channel is deleted from
    the agent source — no generated-code table usage, no raw-code writes
    with generated_by="self_healing_agent", no LLM regeneration branch, and
    no synchronous verify_rerun polling loop (replaced by job_completion
    linking, R9.7).

    **Validates: Requirements 9.6**
    """

    @staticmethod
    def _agent_source() -> str:
        source_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent", "main.py"
        )
        with open(source_path) as f:
            return f.read()

    def test_generated_code_table_usage_removed(self):
        source = self._agent_source()
        assert "GENERATED_CODE_TABLE" not in source
        assert "agentic_data_ingestion_generated_code" not in source

    def test_self_healing_agent_code_writes_removed(self):
        """No code path writes generated_by="self_healing_agent" (the marker
        of the side channel). Docstring mentions describing the DELETED path
        are allowed; executable string literals are not."""
        source = self._agent_source()
        assert '"generated_by": "self_healing_agent"' not in source
        assert "'generated_by': 'self_healing_agent'" not in source

    def test_llm_regeneration_branch_removed(self):
        source = self._agent_source()
        assert "remediate_schema_mismatch" not in source
        assert "remediate_type_cast" not in source
        assert "from strands import" not in source

    def test_synchronous_verify_polling_removed(self):
        source = self._agent_source()
        assert "verify_rerun" not in source
        assert "_MAX_POLL_ATTEMPTS" not in source
