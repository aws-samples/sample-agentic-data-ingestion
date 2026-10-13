# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The handler notifies on a declined re-run — wiring only (P30 task 6.3).

R15.3 has two halves. The outcome-record half is task 6.2's
(``skip_reason``/``skip_detail``/``rerun_safety``, covered by
test_outcome_record.py and test_rerun_safety.py). This file covers the
notification half's WIRING, and deliberately nothing else:

* the notification fires exactly on a Partial_Write_Guard decline, and not on
  a permitted re-run;
* it is sent AFTER the outcome record is durable (persist-then-notify, the
  approval handler's convention) — a notification pointing at a record that
  does not exist is worse than none;
* ``applied_fix`` is populated only for the OOM path, whose ``update_job`` ran
  BEFORE the gate, and carries the fix description WITHOUT the decline reason
  glued on;
* a notification failure cannot un-decline the guard or fail the handler.

The message CONTENT is pinned in tests/unit/shared/test_self_healing_declined_notification.py.
The guard's own handler-level behaviour (append target → no re-run → classified
→ reason surfaced; overwrite target → unchanged; Heal_Guard still one heal per
run) is task 6.5 and is NOT duplicated here.

**Validates: Requirements 15.3**
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
# Same pattern as test_outcome_record.py: unique sys.modules name, entrypoint
# as an identity decorator, boto3 clients mocked at module scope.

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
    _spec = importlib.util.spec_from_file_location("sha_notify_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_notify_main"] = sha
    _spec.loader.exec_module(sha)

    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn

_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
}

_STATE_TABLE = "agentic_data_ingestion_state"
_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

_NORMAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"}

# glue_catalog + overwrite: the "looks safe but appends" trap — outside the
# re-run safe set, so the guard declines.
_UNSAFE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "S3_UPLOAD",
    "source_bucket": "src-bkt",
    "target_config": {"type": "GLUE_CATALOG", "write_mode": "overwrite"},
}
# The wizard's default shape, inside the safe set — the re-run proceeds.
_SAFE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "S3_UPLOAD",
    "source_bucket": "src-bkt",
    "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "overwrite"},
}

_OOM_MESSAGE = "java.lang.OutOfMemoryError: Java heap space"
_SCHEMA_MESSAGE = "AnalysisException: cannot resolve 'customer_id'"


def _wire_tables(pipeline_record: dict) -> MagicMock:
    """Wire sha._dynamodb; returns the state-table mock."""
    state_table = MagicMock()
    state_table.get_item.return_value = {"Item": _NORMAL_FV_ROW}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": pipeline_record}

    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda name: pipelines_table if name == _PIPELINES_TABLE else state_table
    sha._dynamodb = dynamo
    sha._require_env = MagicMock(side_effect=lambda name, default=None: default)
    return state_table


def _wire_glue_job() -> None:
    """A G.1X job so remediate_oom's update_job upgrade succeeds."""
    _mock_glue.reset_mock()
    _mock_glue.get_job.return_value = {
        "Job": {
            "WorkerType": "G.1X",
            "NumberOfWorkers": 2,
            "Role": "arn:aws:iam::123456789012:role/GlueRole",
            "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/s.py"},
        }
    }


def _payload(error_message: str) -> dict:
    return {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "step_name": "glue_etl",
        "error_message": error_message,
        "job_name": "agentic-data-ingestion-pipe-1-etl",
    }


def _run(error_message: str, pipeline_record: dict) -> tuple[dict, MagicMock, MagicMock]:
    """Run the handler with the notification helper captured.

    Returns ``(outcome record, notify mock, state table mock)``.
    """
    state_table = _wire_tables(pipeline_record)
    _wire_glue_job()
    _mock_sfn.reset_mock()
    _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}

    with (
        patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
        patch.object(sha, "_notify_rerun_declined") as notify,
    ):
        result = sha.handler(_payload(error_message))
    return result, notify, state_table


class TestNotifiesOnlyOnDecline:
    def test_declined_rerun_notifies_once(self):
        result, notify, _ = _run(_SCHEMA_MESSAGE, _UNSAFE_RECORD)
        assert notify.call_count == 1
        assert result["skip_reason"] == "partial_write_guard"

    def test_permitted_rerun_does_not_notify(self):
        result, notify, _ = _run(_SCHEMA_MESSAGE, _SAFE_RECORD)
        assert notify.call_count == 0
        assert result["rerun_started"] is True

    def test_notification_carries_the_verdict_and_the_declined_action(self):
        _, notify, _ = _run(_SCHEMA_MESSAGE, _UNSAFE_RECORD)
        kwargs = notify.call_args.kwargs
        assert kwargs["declined_action"] == "rerun_pipeline"
        assert kwargs["rerun_safety"]["safe"] is False
        assert kwargs["rerun_safety"]["target_type"] == "glue_catalog"
        assert kwargs["run_id"] == "run-1"
        assert kwargs["pipeline_id"] == "pipe-1"


class TestPersistThenNotify:
    def test_outcome_record_is_written_before_the_notification(self):
        state_table = _wire_tables(_UNSAFE_RECORD)
        _wire_glue_job()
        written_before_send: list[bool] = []

        def _probe(**kwargs):
            written_before_send.append(state_table.put_item.call_count > 0)

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(sha, "_notify_rerun_declined", side_effect=_probe),
        ):
            sha.handler(_payload(_SCHEMA_MESSAGE))

        assert written_before_send == [True]

    def test_notification_failure_does_not_change_the_decision(self):
        state_table = _wire_tables(_UNSAFE_RECORD)
        _wire_glue_job()

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(sha, "_notify_rerun_declined", side_effect=RuntimeError("SNS down")),
            patch.object(sha, "logger"),
        ):
            try:
                result = sha.handler(_payload(_SCHEMA_MESSAGE))
            except RuntimeError:
                result = None

        # The helper itself swallows failures; this pins that a raising
        # notification cannot rewrite the recorded decision. If the handler
        # did propagate, the record written before it still stands.
        written = [c.kwargs["Item"] for c in state_table.put_item.call_args_list]
        assert written, "the outcome record must already be durable"
        assert written[-1]["rerun_status"] == "skipped_partial_write_guard"
        assert written[-1]["rerun_started"] is False
        if result is not None:
            assert result["rerun_status"] == "skipped_partial_write_guard"

    def _call_helper(self, alert: MagicMock) -> MagicMock:
        """Run the helper with ``notification_service`` stubbed; returns the logger."""
        with (
            patch.dict(
                "sys.modules",
                {"notification_service": MagicMock(send_self_healing_declined_alert=alert)},
            ),
            patch.object(sha, "logger") as log,
        ):
            sha._notify_rerun_declined(
                pipeline_id="pipe-1",
                run_id="run-1",
                rerun_safety={"safe": False, "reason": "r"},
                declined_action="rerun_pipeline",
                applied_fix="",
            )
        return log

    def test_helper_swallows_a_send_failure(self):
        log = self._call_helper(MagicMock(side_effect=RuntimeError("boom")))
        assert log.error.called

    def test_helper_logs_an_undelivered_notification(self):
        log = self._call_helper(MagicMock(return_value={"success": False, "delivery_results": {}}))
        assert log.error.called

    def test_helper_logs_a_delivered_notification(self):
        log = self._call_helper(MagicMock(return_value={"success": True, "notification_id": "n-1"}))
        assert log.info.called
        assert not log.error.called


class TestAlreadyAppliedFixIsCommunicated:
    def test_oom_decline_reports_the_worker_upgrade_as_applied(self):
        """remediate_oom's update_job ran BEFORE the gate — say so."""
        _, notify, _ = _run(_OOM_MESSAGE, _UNSAFE_RECORD)
        applied = notify.call_args.kwargs["applied_fix"]
        assert applied == "Upgraded worker type from G.1X to G.2X"
        # The reason belongs in its own field, not glued onto the applied fix.
        assert "declined" not in applied.lower()
        assert _mock_glue.update_job.called

    def test_rerun_decline_reports_no_applied_fix(self):
        """For schema/type failures the remediation IS the re-run."""
        _, notify, _ = _run(_SCHEMA_MESSAGE, _UNSAFE_RECORD)
        assert notify.call_args.kwargs["applied_fix"] == ""

    def test_pre_gate_mutating_actions_is_exactly_the_worker_upgrade(self):
        assert set(sha._PRE_GATE_MUTATING_ACTIONS) == {"upgrade_worker_type"}
