# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The handler notifies when a heal ran — wiring only (p30 task 2.5).

R14.3's notification clause. This file covers the WIRING and deliberately
nothing else:

* exactly ONE notification per heal — the R14.3 "a heal ran" alert and 6.3's
  R15.3 "the guard declined" alert are mutually exclusive, and a heal that
  applied no fix at all sends neither;
* it fires for both meanings of "a heal ran" — a re-run that started, and a
  re-run that failed to start after a fix was applied;
* it is sent AFTER the outcome record is durable (persist-then-notify);
* a notification failure cannot change the recorded outcome.

The message CONTENT is pinned in
tests/unit/shared/test_self_healing_applied_notification.py. The
Partial_Write_Guard's own handler-level behaviour is task 6.5, and 6.3's
declined-notification wiring is test_rerun_declined_notification.py — neither
is duplicated here.

**Validates: Requirements 14.3**
"""

import contextlib
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
# Same pattern as test_rerun_declined_notification.py: a unique sys.modules
# name so the four agent test files do not fight over one module identity.

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
    _spec = importlib.util.spec_from_file_location("sha_heal_notify_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_heal_notify_main"] = sha
    _spec.loader.exec_module(sha)

    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn

_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
}

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

_NORMAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"}

# The wizard's default target shape, inside the re-run safe set — the guard
# permits, so the heal proceeds and the R14.3 alert is the one that fires.
_SAFE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "S3_UPLOAD",
    "source_bucket": "src-bkt",
    "target_config": {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "overwrite"},
}
# glue_catalog + overwrite: outside the safe set, so 6.3's decline alert fires
# instead. Present only to pin the mutual exclusivity.
_UNSAFE_RECORD = {
    "pipeline_id": "pipe-1",
    "source_type": "S3_UPLOAD",
    "source_bucket": "src-bkt",
    "target_config": {"type": "GLUE_CATALOG", "write_mode": "overwrite"},
}

_OOM_MESSAGE = "java.lang.OutOfMemoryError: Java heap space"
_SCHEMA_MESSAGE = "AnalysisException: cannot resolve 'customer_id'"
# permission_error → flag_for_review: diagnosed, no fix applied, nothing re-run.
_PERMISSION_MESSAGE = "AccessDeniedException: not authorized to perform s3:GetObject"


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


def _run(
    error_message: str,
    pipeline_record: dict = _SAFE_RECORD,
    start_error: Exception | None = None,
) -> tuple[dict, MagicMock, MagicMock, MagicMock]:
    """Run the handler with BOTH notification helpers captured.

    Returns ``(outcome record, applied-alert mock, declined-alert mock,
    state table mock)``.
    """
    state_table = _wire_tables(pipeline_record)
    _wire_glue_job()
    _mock_sfn.reset_mock()
    if start_error is None:
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}
        _mock_sfn.start_execution.side_effect = None
    else:
        _mock_sfn.start_execution.side_effect = start_error

    with (
        patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
        patch.object(sha, "_notify_heal_applied") as applied,
        patch.object(sha, "_notify_rerun_declined") as declined,
    ):
        result = sha.handler(_payload(error_message))
    return result, applied, declined, state_table


class TestExactlyOneNotificationPerHeal:
    def test_a_started_rerun_sends_the_applied_alert_only(self):
        result, applied, declined, _ = _run(_SCHEMA_MESSAGE)
        assert applied.call_count == 1
        assert declined.call_count == 0
        assert result["rerun_status"] == "running"
        assert result["rerun_started"] is True

    def test_a_declined_rerun_sends_the_declined_alert_only(self):
        """6.3's R15.3 alert and this R14.3 alert are mutually exclusive."""
        result, applied, declined, _ = _run(_SCHEMA_MESSAGE, _UNSAFE_RECORD)
        assert applied.call_count == 0
        assert declined.call_count == 1
        assert result["rerun_status"] == "skipped_partial_write_guard"

    def test_a_diagnosis_with_no_fix_sends_nothing(self):
        """flag_for_review applies no remediation, so there is nothing to name."""
        result, applied, declined, _ = _run(_PERMISSION_MESSAGE)
        assert applied.call_count == 0
        assert declined.call_count == 0
        assert result["rerun_status"] == "skipped"
        assert result["action"] == "flag_for_review"


class TestBothMeaningsOfAHealThatRan:
    def test_a_started_rerun_reports_started_with_its_lineage_id(self):
        _, applied, _, _ = _run(_OOM_MESSAGE)
        kwargs = applied.call_args.kwargs
        assert kwargs["rerun_started"] is True
        assert kwargs["rerun_id"] == "run-1-heal"
        assert kwargs["rerun_start_error"] == ""

    def test_a_failed_start_reports_not_started_with_the_error(self):
        result, applied, declined, _ = _run(
            _OOM_MESSAGE, start_error=RuntimeError("ThrottlingException: Rate exceeded")
        )
        assert declined.call_count == 0
        assert applied.call_count == 1
        kwargs = applied.call_args.kwargs
        assert kwargs["rerun_started"] is False
        assert "ThrottlingException" in kwargs["rerun_start_error"]
        # Nothing started, so nothing to point at.
        assert kwargs["rerun_id"] == ""
        assert result["rerun_status"] == "start_failed"

    def test_it_names_the_remediation_the_agent_actually_applied(self):
        _, applied, _, _ = _run(_OOM_MESSAGE)
        kwargs = applied.call_args.kwargs
        assert kwargs["remediation_action"] == "upgrade_worker_type"
        assert kwargs["remediation_applied"] == "Upgraded worker type from G.1X to G.2X"
        assert kwargs["error_classification"] == "oom_error"
        assert kwargs["run_id"] == "run-1"
        assert kwargs["pipeline_id"] == "pipe-1"

    def test_the_rerun_action_is_named_for_code_level_failures(self):
        _, applied, _, _ = _run(_SCHEMA_MESSAGE)
        kwargs = applied.call_args.kwargs
        assert kwargs["remediation_action"] == "rerun_pipeline"
        assert kwargs["error_classification"] == "schema_mismatch"
        assert kwargs["remediation_applied"]


class TestPersistThenNotify:
    def test_the_outcome_record_is_written_before_the_notification(self):
        state_table = _wire_tables(_SAFE_RECORD)
        _wire_glue_job()
        _mock_sfn.start_execution.side_effect = None
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}
        written_before_send: list[bool] = []

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(
                sha,
                "_notify_heal_applied",
                side_effect=lambda **kw: written_before_send.append(state_table.put_item.call_count > 0),
            ),
        ):
            sha.handler(_payload(_SCHEMA_MESSAGE))

        assert written_before_send == [True]

    def test_a_notification_failure_does_not_change_the_recorded_outcome(self):
        state_table = _wire_tables(_SAFE_RECORD)
        _wire_glue_job()
        _mock_sfn.start_execution.side_effect = None
        _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(sha, "_notify_heal_applied", side_effect=RuntimeError("SNS down")),
            patch.object(sha, "logger"),
            contextlib.suppress(RuntimeError),
        ):
            sha.handler(_payload(_SCHEMA_MESSAGE))

        written = [c.kwargs["Item"] for c in state_table.put_item.call_args_list]
        assert written, "the outcome record must already be durable"
        assert written[-1]["rerun_status"] == "running"
        assert written[-1]["rerun_started"] is True


class TestTheHelperSwallowsEverything:
    def _call_helper(self, alert: MagicMock) -> MagicMock:
        """Run the helper with ``notification_service`` stubbed; returns the logger."""
        with (
            patch.dict(
                "sys.modules",
                {"notification_service": MagicMock(send_self_healing_applied_alert=alert)},
            ),
            patch.object(sha, "logger") as log,
        ):
            sha._notify_heal_applied(
                pipeline_id="pipe-1",
                run_id="run-1",
                error_classification="oom_error",
                remediation_action="upgrade_worker_type",
                remediation_applied="Upgraded worker type from G.1X to G.2X",
                rerun_started=True,
                rerun_id="run-1-heal",
                rerun_start_error="",
            )
        return log

    def test_a_send_failure_is_logged_not_raised(self):
        log = self._call_helper(MagicMock(side_effect=RuntimeError("boom")))
        assert log.error.called

    def test_an_undelivered_notification_is_logged(self):
        log = self._call_helper(MagicMock(return_value={"success": False, "delivery_results": {}}))
        assert log.error.called

    def test_a_delivered_notification_is_logged(self):
        log = self._call_helper(MagicMock(return_value={"success": True, "notification_id": "n-1"}))
        assert log.info.called
        assert not log.error.called


class TestHealGuardIsUntouched:
    def test_a_heal_triggered_run_notifies_nothing(self):
        """The Heal_Guard returns at step 0 — before any remediation or notification."""
        state_table = _wire_tables(_SAFE_RECORD)
        state_table.get_item.return_value = {
            "Item": {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "self_healing"}
        }

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(sha, "_notify_heal_applied") as applied,
            patch.object(sha, "_notify_rerun_declined") as declined,
        ):
            result = sha.handler(_payload(_SCHEMA_MESSAGE))

        assert applied.call_count == 0
        assert declined.call_count == 0
        assert result["rerun_status"] == "skipped_heal_guard"
