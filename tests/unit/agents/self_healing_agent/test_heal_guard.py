# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Heal_Guard tests (P28 task 8.3, Requirement 9.4).

The guard rule: heal reruns carry ``triggered_by="self_healing"``, the agent
never acts on a run already so marked (max one heal per original run), and
the heal rerun id is the stable ``f"{run_id}-heal"`` — the GUARD, not string
surgery, is what prevents ``-heal-heal`` growth.

Trigger-resolution reality these tests encode: the failure payload carries
the run_id file_validation GENERATED for the run (its prefix/JDBC handlers
never adopt the SFN input's run_id), so the guard resolves the marker from
the run's file_validation state row, where file_validation persists the SFN
input's ``triggered_by``. The payload's own ``triggered_by`` is honoured as
a fast path.

**Validates: Requirements 9.4**
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
#
# test_remediation.py imports the module with BedrockAgentCoreApp fully
# mocked, which turns `handler` into a MagicMock. Guard placement is a
# handler-level property, so this file loads its own copy of the module
# (unique sys.modules name — no "main" collision with the other agent test
# files) with `entrypoint` behaving as the identity decorator.

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
    _spec = importlib.util.spec_from_file_location("sha_guard_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_guard_main"] = sha
    _spec.loader.exec_module(sha)

    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn


def _wire_state_row(file_validation_item: dict | None):
    """Point sha._dynamodb at a mock whose state table returns *file_validation_item*.

    Returns the state-table mock so tests can assert on put_item calls.
    """
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": file_validation_item} if file_validation_item is not None else {}
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    sha._dynamodb = mock_dynamo
    sha._require_env = MagicMock(return_value="agentic_data_ingestion_state")
    return mock_table


_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
    "strands": MagicMock(),
    "strands.models.bedrock": MagicMock(),
    "model_config": MagicMock(),
    "rerun_contract": MagicMock(),
}


def _call_handler(payload: dict) -> dict:
    """Invoke the real handler with in-handler imports mocked."""
    with patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)):
        return sha.handler(payload)


# ── was_heal_triggered ───────────────────────────────────────────────


class TestWasHealTriggered:
    """Trigger resolution: payload fast path, state-row lookup, fail-closed."""

    def test_payload_marker_short_circuits(self):
        """Payload triggered_by=self_healing → guarded without any lookup."""
        mock_table = _wire_state_row(None)
        assert sha.was_heal_triggered({"run_id": "run-1", "triggered_by": "self_healing"}) is True
        mock_table.get_item.assert_not_called()

    def test_state_row_marker_detected(self):
        """file_validation row carrying triggered_by=self_healing → guarded."""
        mock_table = _wire_state_row(
            {"run_id": "run-y", "step_name": "file_validation", "triggered_by": "self_healing"}
        )
        assert sha.was_heal_triggered({"run_id": "run-y"}) is True
        mock_table.get_item.assert_called_once_with(Key={"run_id": "run-y", "step_name": "file_validation"})

    def test_normal_trigger_not_guarded(self):
        """A normally triggered run (s3_upload) is heal-eligible."""
        _wire_state_row({"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"})
        assert sha.was_heal_triggered({"run_id": "run-1"}) is False

    def test_legacy_row_without_marker_not_guarded(self):
        """Pre-deploy rows have no triggered_by attribute → heal-eligible."""
        _wire_state_row({"run_id": "run-old", "step_name": "file_validation"})
        assert sha.was_heal_triggered({"run_id": "run-old"}) is False

    def test_missing_row_not_guarded(self):
        _wire_state_row(None)
        assert sha.was_heal_triggered({"run_id": "run-gone"}) is False

    def test_lookup_error_fails_closed(self):
        """Unreadable state row → treat as guarded (no heal-loop risk)."""
        mock_table = _wire_state_row(None)
        mock_table.get_item.side_effect = RuntimeError("dynamo down")
        assert sha.was_heal_triggered({"run_id": "run-1"}) is True

    def test_degenerate_run_id_falls_through_to_normal_flow(self):
        """No usable run_id → not guarded (normal flow flags it for review)."""
        assert sha.was_heal_triggered({}) is False
        assert sha.was_heal_triggered({"run_id": "unknown"}) is False


# ── Guard placement in the handler ───────────────────────────────────


class TestHandlerGuard:
    """The guard fires FIRST: no remediation, no retrigger, honest skip."""

    def _failure_payload(self, run_id: str) -> dict:
        return {
            "run_id": run_id,
            "pipeline_id": "pipe-1",
            "step_name": "glue_etl",
            "error_message": "OutOfMemoryError: Java heap space",
            "job_name": "agentic-data-ingestion-pipe-1-etl",
        }

    def test_marked_run_skips_all_remediation(self):
        """State row says self_healing → no glue update, no retrigger, skip record."""
        mock_table = _wire_state_row(
            {"run_id": "run-y", "step_name": "file_validation", "triggered_by": "self_healing"}
        )
        _mock_sfn.start_execution.reset_mock()
        _mock_glue.update_job.reset_mock()

        result = _call_handler(self._failure_payload("run-y"))

        assert result["status"] == "skipped"
        assert result["skip_reason"] == "heal_guard"
        assert result["rerun_status"] == "skipped_heal_guard"
        assert result["rerun_id"] == ""
        _mock_sfn.start_execution.assert_not_called()
        _mock_glue.update_job.assert_not_called()

        # Honest skip record written to the state table
        put_items = [c.kwargs["Item"] for c in mock_table.put_item.call_args_list]
        assert any(
            i.get("step_name") == "self_healing" and i.get("rerun_status") == "skipped_heal_guard" for i in put_items
        )

    def test_payload_marker_skips_without_lookup(self):
        """Design shape: the agent first checks event.triggered_by."""
        mock_table = _wire_state_row(None)
        _mock_sfn.start_execution.reset_mock()

        payload = self._failure_payload("run-z")
        payload["triggered_by"] = "self_healing"
        result = _call_handler(payload)

        assert result["rerun_status"] == "skipped_heal_guard"
        mock_table.get_item.assert_not_called()
        _mock_sfn.start_execution.assert_not_called()

    def test_skip_record_write_failure_still_skips(self):
        """A failed skip-record write must not un-skip the guard."""
        mock_table = _wire_state_row(
            {"run_id": "run-y", "step_name": "file_validation", "triggered_by": "self_healing"}
        )
        mock_table.put_item.side_effect = RuntimeError("dynamo down")
        _mock_sfn.start_execution.reset_mock()

        result = _call_handler(self._failure_payload("run-y"))

        assert result["rerun_status"] == "skipped_heal_guard"
        _mock_sfn.start_execution.assert_not_called()

    def test_unmarked_run_proceeds_past_guard(self):
        """A normal failed run is NOT blocked by the guard.

        Uses an unclassifiable error so the flow stops at flag_for_review —
        proving the handler moved past the guard into remediation without
        needing the full retrigger machinery.
        """
        _wire_state_row({"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"})

        payload = self._failure_payload("run-1")
        payload["error_message"] = "some completely novel failure"
        result = _call_handler(payload)

        assert result.get("skip_reason") != "heal_guard"
        assert result["error_classification"] == "unknown"
        assert "manual intervention" in result["remediation_applied"]


# ── Stable rerun id ──────────────────────────────────────────────────


def _wire_pipeline_record(record):
    """Point sha._dynamodb at a mock pipelines table returning *record*."""
    mock_table = MagicMock()
    mock_table.get_item.return_value = {"Item": record} if record is not None else {}
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_table
    sha._dynamodb = mock_dynamo
    sha._require_env = MagicMock(return_value="agentic_data_ingestion_pipelines")
    return mock_table


class TestStableHealRerunId:
    """R9.4: rerun_id = f"{run_id}-heal", stable — the guard prevents growth."""

    def test_heal_of_run_x_yields_run_x_heal(self):
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD", "source_bucket": "b"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:x"}

        result = sha.retrigger_pipeline("pipe-1", "run-x", "fix")

        assert result["rerun_id"] == "run-x-heal"
        input_data = json.loads(_mock_sfn.start_execution.call_args[1]["input"])
        assert input_data["run_id"] == "run-x-heal"
        assert input_data["original_run_id"] == "run-x"

    def test_heal_of_p16_retry_run_gets_single_heal_suffix(self):
        """Retried runs keep their original run_id in state rows — healing one
        derives {that_id}-heal (one suffix, no compounding with '-retry')."""
        _wire_pipeline_record({"pipeline_id": "pipe-1", "source_type": "S3_UPLOAD", "source_bucket": "b"})
        _mock_sfn.start_execution.reset_mock()
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:x"}

        result = sha.retrigger_pipeline("pipe-1", "run-2026-01-01-abc", "fix")
        assert result["rerun_id"] == "run-2026-01-01-abc-heal"

    def test_heal_rerun_failure_never_produces_heal_heal(self):
        """The guard — not string surgery — prevents id growth.

        Simulate the heal rerun's own failure: its file_validation row is
        marked triggered_by=self_healing (whatever its run_id looks like,
        including a literal '-heal' id). The guard fires before any rerun-id
        derivation, so no 'run-x-heal-heal' execution can ever be started.
        """
        _wire_state_row({"run_id": "run-x-heal", "step_name": "file_validation", "triggered_by": "self_healing"})
        _mock_sfn.start_execution.reset_mock()

        result = _call_handler(
            {
                "run_id": "run-x-heal",
                "pipeline_id": "pipe-1",
                "error_message": "OutOfMemoryError: Java heap space",
                "job_name": "agentic-data-ingestion-pipe-1-etl",
            }
        )

        assert result["rerun_status"] == "skipped_heal_guard"
        assert result["rerun_id"] == ""
        _mock_sfn.start_execution.assert_not_called()

    def test_guard_fires_for_fresh_id_heal_rerun(self):
        """The realistic shape: file_validation gave the heal rerun a FRESH
        run_id (the SFN input's -heal id is lineage only) — the guard still
        fires because the state row carries the marker."""
        _wire_state_row({"run_id": "run-fresh-123", "step_name": "file_validation", "triggered_by": "self_healing"})
        _mock_sfn.start_execution.reset_mock()

        result = _call_handler(
            {
                "run_id": "run-fresh-123",
                "pipeline_id": "pipe-1",
                "error_message": "OutOfMemoryError: Java heap space",
                "job_name": "agentic-data-ingestion-pipe-1-etl",
            }
        )

        assert result["rerun_status"] == "skipped_heal_guard"
        _mock_sfn.start_execution.assert_not_called()
