# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Honest outcome record tests (P28 task 8.5, Requirements 9.6, 9.7).

R9.7: the agent records "fix applied" (``rerun_started=True``) ONLY when
``start_execution`` actually succeeded with the corrected input. A failed
start is recorded as ``rerun_started=False`` with the error — the old
pattern (audit says "fix applied" while the rerun died at step 1) is gone.
The rerun RESULT is linked later by the heal rerun's own job_completion
(covered in tests/unit/lambdas/job_completion/test_heal_rerun_linking.py);
the agent itself no longer polls Step Functions for 10 minutes.

R9.6 (behavioral): remediating a schema-mismatch failure writes exactly one
state record — the outcome record — and never touches a generated-code
table or stores raw code.

**Validates: Requirements 9.6, 9.7**
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
# Same pattern as test_heal_guard.py: unique sys.modules name, entrypoint
# as identity decorator, boto3 clients mocked at module scope.

_mock_glue = MagicMock()
_mock_sfn = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: {
    "glue": _mock_glue,
    "stepfunctions": _mock_sfn,
    "logs": MagicMock(),
}.get(svc, MagicMock())
_mock_boto3.resource.return_value = MagicMock()

_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn

_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

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
    _spec = importlib.util.spec_from_file_location("sha_outcome_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_outcome_main"] = sha
    _spec.loader.exec_module(sha)

    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn


# rerun_contract/step_names are REAL shared modules here (SHARED_DIR is on
# sys.path): the success-path test json.dumps the built execution input.
_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
}

_STATE_TABLE = "agentic_data_ingestion_state"
_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"


def _wire_tables(
    fv_item: dict | None,
    pipeline_record: dict | None,
) -> tuple[MagicMock, MagicMock]:
    """Wire sha._dynamodb with a state table (guard lookup + outcome write)
    and a pipelines table (rerun-contract record source), routed by name.

    Returns ``(state_table, pipelines_table)`` mocks.
    """
    state_table = MagicMock()
    state_table.get_item.return_value = {"Item": fv_item} if fv_item is not None else {}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": pipeline_record} if pipeline_record is not None else {}

    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda name: pipelines_table if name == _PIPELINES_TABLE else state_table
    sha._dynamodb = dynamo
    # Resolve every table name to its default (the second argument).
    sha._require_env = MagicMock(side_effect=lambda name, default=None: default)
    return state_table, pipelines_table


def _call_handler(payload: dict) -> dict:
    with patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)):
        return sha.handler(payload)


def _failure_payload(error_message: str, run_id: str = "run-1") -> dict:
    return {
        "run_id": run_id,
        "pipeline_id": "pipe-1",
        "step_name": "glue_etl",
        "error_message": error_message,
        "job_name": "agentic-data-ingestion-pipe-1-etl",
    }


_NORMAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"}
# P30 R15.2: the Partial_Write_Guard now grades (target type, write_mode) before
# a re-run, so this record needs a target_config to be a valid re-run subject at
# all. NEW_S3_TABLE_BUCKET + overwrite is the wizard's own default shape
# (CreatePipelinePage.tsx:361) and is in the re-run safe set, which keeps these
# tests testing what they were written for — rerun_started truthfulness (R9.7) —
# rather than incidentally exercising the guard. The guard's own cases are p30
# task 6.5.
_PIPELINE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "S3_UPLOAD",
    "source_bucket": "src-bkt",
    "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "overwrite"},
}


def _outcome_item(state_table: MagicMock) -> dict:
    """The self_healing outcome record written to the state table."""
    items = [c.kwargs["Item"] for c in state_table.put_item.call_args_list]
    outcome = [i for i in items if i.get("step_name") == "self_healing"]
    assert len(outcome) == 1, f"expected exactly one self_healing record, got {len(outcome)}"
    return outcome[0]


# ── R9.7: rerun_started truthfulness ─────────────────────────────────


class TestHonestOutcomeRecord:
    """rerun_started=True ONLY on a successful start; failures recorded."""

    def test_successful_start_records_rerun_started_true(self):
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-west-2:123:execution:sm:x"}

        result = _call_handler(_failure_payload("AnalysisException: cannot resolve 'col_x'"))

        item = _outcome_item(state_table)
        assert item["rerun_started"] is True
        assert item["rerun_status"] == "running"
        assert item["rerun_id"] == "run-1-heal"
        assert item["rerun_execution_arn"] == "arn:aws:states:us-west-2:123:execution:sm:x"
        assert "rerun_start_error" not in item
        assert result["rerun_started"] is True

    def test_failed_start_records_rerun_started_false_with_error(self):
        """start_execution raising is recorded honestly — never as success."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.side_effect = RuntimeError("States throttled")

        result = _call_handler(_failure_payload("AnalysisException: cannot resolve 'col_x'"))

        item = _outcome_item(state_table)
        assert item["rerun_started"] is False
        assert item["rerun_status"] == "start_failed"
        assert item["rerun_id"] == ""
        assert "States throttled" in item["rerun_start_error"]
        assert result["rerun_started"] is False
        _mock_sfn.start_execution.side_effect = None

    def test_missing_pipeline_record_records_start_failure(self):
        """A rerun input that cannot be built (no pipeline record) is a
        start FAILURE on the record — the old code died before writing any
        record at all."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, None)
        _mock_sfn.start_execution.reset_mock(side_effect=True)

        _call_handler(_failure_payload("AnalysisException: cannot resolve 'col_x'"))

        item = _outcome_item(state_table)
        assert item["rerun_started"] is False
        assert item["rerun_status"] == "start_failed"
        assert "Pipeline record not found" in item["rerun_start_error"]
        _mock_sfn.start_execution.assert_not_called()

    def test_flag_for_review_never_starts_rerun(self):
        """Unhealable classes apply no fix → no rerun, honest 'skipped'."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)

        _call_handler(_failure_payload("some completely novel failure"))

        item = _outcome_item(state_table)
        assert item["action"] == "flag_for_review"
        assert item["rerun_started"] is False
        assert item["rerun_status"] == "skipped"
        _mock_sfn.start_execution.assert_not_called()

    def test_max_worker_tier_oom_never_starts_rerun(self):
        """no_upgrade_available applies NO corrected input — rerunning the
        same config would fail the same way, so no rerun is started and the
        record says so."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_glue.get_job.return_value = {
            "Job": {
                "Name": "agentic-data-ingestion-pipe-1-etl",
                "WorkerType": "G.8X",
                "Role": "arn:aws:iam::role/GlueRole",
                "Command": {"Name": "glueetl"},
            }
        }
        _mock_glue.update_job.reset_mock()

        _call_handler(_failure_payload("OutOfMemoryError: Java heap space"))

        item = _outcome_item(state_table)
        assert item["action"] == "no_upgrade_available"
        assert item["rerun_started"] is False
        assert item["rerun_status"] == "skipped"
        _mock_sfn.start_execution.assert_not_called()
        _mock_glue.update_job.assert_not_called()

    def test_oom_upgrade_starts_rerun_with_overrides(self):
        """upgrade_worker_type IS a corrected input: the rerun starts and
        carries the WorkerType override (R9.5) in the execution input."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:oom-heal"}
        _mock_glue.get_job.return_value = {
            "Job": {
                "Name": "agentic-data-ingestion-pipe-1-etl",
                "WorkerType": "G.1X",
                "Role": "arn:aws:iam::role/GlueRole",
                "Command": {"Name": "glueetl"},
            }
        }

        _call_handler(_failure_payload("OutOfMemoryError: Java heap space"))

        item = _outcome_item(state_table)
        assert item["action"] == "upgrade_worker_type"
        assert item["rerun_started"] is True
        input_data = json.loads(_mock_sfn.start_execution.call_args[1]["input"])
        assert input_data["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_record_carries_design_shape_and_identity(self):
        """{healing_id, root_cause, action, rerun_id, rerun_started} plus
        run/pipeline ids and timestamp (design R9)."""
        state_table, _ = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:x"}

        _call_handler(_failure_payload("NumberFormatException: for input string"))

        item = _outcome_item(state_table)
        assert item["healing_id"].startswith("heal-")
        assert item["root_cause"] == "type_cast_error"
        assert item["action"] == "rerun_pipeline"
        assert item["run_id"] == "run-1"
        assert item["original_run_id"] == "run-1"
        assert item["original_pipeline_id"] == "pipe-1"
        assert item["timestamp"]
        # Dashboard-consumed aliases stay in lockstep with the design names.
        assert item["error_classification"] == item["root_cause"]


# ── R9.6 behavioral: no side-channel writes from the agent ──────────


class TestNoSideChannelWrites:
    """Remediating a code-level failure writes ONLY the outcome record —
    no raw code, no generated-code table, no generated_by marker."""

    def test_schema_mismatch_heal_writes_only_the_outcome_record(self):
        state_table, pipelines_table = _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:x"}

        _call_handler(_failure_payload("AnalysisException: column not found: order_id"))

        # Exactly one state write (the outcome record), nothing on pipelines.
        items = [c.kwargs["Item"] for c in state_table.put_item.call_args_list]
        assert [i["step_name"] for i in items] == ["self_healing"]
        pipelines_table.put_item.assert_not_called()

        # No item anywhere carries raw code or the side-channel marker.
        for item in items:
            assert "code" not in item
            assert item.get("generated_by") != "self_healing_agent"

    def test_agent_never_resolves_a_generated_code_table(self):
        _wire_tables(_NORMAL_FV_ROW, _PIPELINE_RECORD)
        _mock_sfn.start_execution.reset_mock(side_effect=True)
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:x"}

        _call_handler(_failure_payload("cannot cast STRING to DATE"))

        resolved = [c.args[0] for c in sha._require_env.call_args_list]
        assert "GENERATED_CODE_TABLE" not in resolved
