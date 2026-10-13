# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Partial_Write_Guard at the HANDLER level (p30 task 6.5).

R15.6's three clauses, and only those. The siblings own everything else:

* ``test_rerun_safety.py`` (6.2) — the pure predicate: allow-list,
  target-type normalisation, case sensitivity, fail-closed readings.
* ``test_rerun_exemption_reasons.py`` (6.4) — the recorded rationale.
* ``test_rerun_declined_notification.py`` (6.3) + ``tests/unit/shared/
  test_self_healing_declined_notification.py`` — the notification, wiring
  and content.
* ``test_heal_applied_notification.py`` (2.5) — one notification per heal.

None of those is re-covered here. What IS here:

1. **Unsafe target → no re-run, classified, reason surfaced.** No
   ``start_execution``; ``rerun_started=False`` with ``rerun_id=""``;
   ``rerun_status="skipped_partial_write_guard"`` and
   ``remediation_action="flag_for_review"``; and the verdict's reason on the
   outcome record rather than a generic "flagged for review".
2. **Safe target → the re-run is unchanged.** The regression half, and the more
   important one: a guard that declined everything would satisfy clause 1. The
   permitted path must still call ``start_execution``, still record
   ``rerun_started=True`` / ``rerun_status="running"``, and still carry
   ``job_config_overrides`` on the OOM path.
3. **Heal_Guard still one heal per run.** The guard ADDED a condition (R15.4);
   asserted by precedence rather than by absence — a heal-triggered run on an
   *unsafe* target must skip via the Heal_Guard, and ``skip_reason``
   (``heal_guard`` vs ``partial_write_guard``) is what distinguishes them.
   Getting that precedence backwards would be invisible otherwise.

**The task text's "with a recorded partial write" is not testable, and is not
faked here.** Task 0.5 established that no partial-write signal exists anywhere
at this decision point: the dispatch payload carries only ``{pipeline_id,
run_id, job_name, job_run_id, error_message, state}``, ``collect_job_metrics``
returns no row counts, and no state row records rows-written. The guard is
matrix-gated, not detection-gated. So the testable condition is "an unsafe
(target, mode) plus any non-``SUCCEEDED`` terminal state", and
``TestTheGuardIsMatrixGatedNotDetectionGated`` pins that the decline is
independent of the terminal state and of any absent signal — which is the
substance of the premise that CAN be established.

**Validates: Requirements 15.6**
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "self_healing_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
# The directory's established pattern (test_rerun_declined_notification.py is
# the closest model): a unique sys.modules name so the agent test files do not
# fight over one module identity, boto3 clients mocked at module scope, and
# ``entrypoint`` as an identity decorator so ``handler`` stays callable.

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
    _spec = importlib.util.spec_from_file_location("sha_guard_handler_main", MAIN_PATH)
    sha = importlib.util.module_from_spec(_spec)
    sys.modules["sha_guard_handler_main"] = sha
    _spec.loader.exec_module(sha)

    sha._glue_client = _mock_glue
    sha._sfn_client = _mock_sfn

# ``rerun_contract``/``step_names`` are left REAL (SHARED_DIR is on sys.path):
# the permitted-path tests read the execution input the contract actually built.
_HANDLER_MODULE_MOCKS = {
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(),
    "env_utils": MagicMock(),
}

_PIPELINES_TABLE = "agentic_data_ingestion_pipelines"

_NORMAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "s3_upload"}
_HEAL_FV_ROW = {"run_id": "run-1", "step_name": "file_validation", "triggered_by": "self_healing"}

_OOM_MESSAGE = "java.lang.OutOfMemoryError: Java heap space"
_SCHEMA_MESSAGE = "AnalysisException: cannot resolve 'customer_id'"
_PERMISSION_MESSAGE = "AccessDeniedException: not authorized to perform s3:GetObject"


def _record(target_type: str, write_mode: str) -> dict:
    return {
        "pipeline_id": "pipe-1",
        "source_type": "S3_UPLOAD",
        "source_bucket": "src-bkt",
        "target_config": {"type": target_type, "write_mode": write_mode},
    }


# The two unsafe records the task names, for different reasons:
#   * glue_catalog + overwrite is the strongest case because it LOOKS safe —
#     the template ignores write_mode and appends (design §5 O13's trap).
#   * s3_tables + append is the combination R15.6 itself names.
_GLUE_CATALOG_OVERWRITE = _record("GLUE_CATALOG", "overwrite")
_S3_TABLES_APPEND = _record("NEW_S3_TABLE_BUCKET", "append")
_UNSAFE_RECORDS = [_GLUE_CATALOG_OVERWRITE, _S3_TABLES_APPEND]

# The wizard's default target shape, inside the safe set.
_SAFE_RECORD = _record("NEW_S3_TABLE_BUCKET", "overwrite")

# Every persisted spelling that normalises into the allow-list. A guard that
# declined everything would pass clause 1 and break self-healing outright, so
# each of these must still re-run through the handler.
_SAFE_SPELLINGS = [
    ("NEW_S3_TABLE_BUCKET", "overwrite"),
    ("EXISTING_S3_TABLE_BUCKET", "merge"),
    ("s3_tables", "merge"),
    ("JDBC", "overwrite"),
    ("RDS_DATABASE", "overwrite"),
    ("S3_PARQUET", "overwrite"),
]


def _wire_tables(pipeline_record: dict, fv_row: dict = _NORMAL_FV_ROW) -> MagicMock:
    """Wire ``sha._dynamodb``; returns the state-table mock."""
    state_table = MagicMock()
    state_table.get_item.return_value = {"Item": fv_row}
    pipelines_table = MagicMock()
    pipelines_table.get_item.return_value = {"Item": pipeline_record}

    dynamo = MagicMock()
    dynamo.Table.side_effect = lambda name: pipelines_table if name == _PIPELINES_TABLE else state_table
    sha._dynamodb = dynamo
    sha._require_env = MagicMock(side_effect=lambda name, default=None: default)
    return state_table


def _wire_glue_job(worker_type: str = "G.1X") -> None:
    """A ``worker_type`` job so ``remediate_oom`` can grade the upgrade."""
    _mock_glue.reset_mock()
    _mock_glue.get_job.return_value = {
        "Job": {
            "WorkerType": worker_type,
            "NumberOfWorkers": 2,
            "Role": "arn:aws:iam::123456789012:role/GlueRole",
            "Command": {"Name": "glueetl", "ScriptLocation": "s3://b/s.py"},
        }
    }


def _payload(error_message: str, **extra) -> dict:
    """The dispatch payload shape, verbatim — see the module docstring.

    ``self_healing_dispatch`` relays ``{pipeline_id, run_id, job_name,
    job_run_id, error_message, state}`` and nothing else. No field here
    reports rows written.
    """
    payload = {
        "run_id": "run-1",
        "pipeline_id": "pipe-1",
        "step_name": "glue_etl",
        "error_message": error_message,
        "job_name": "agentic-data-ingestion-pipe-1-etl",
    }
    payload.update(extra)
    return payload


def _run(
    error_message: str,
    pipeline_record: dict,
    *,
    fv_row: dict = _NORMAL_FV_ROW,
    worker_type: str = "G.1X",
    **payload_extra,
) -> tuple[dict, MagicMock, MagicMock]:
    """Drive the full handler with the guard predicate spied on.

    Both notification helpers are stubbed out — 6.3 and 2.5 own them, and
    letting them run would only add noise. Returns ``(outcome record,
    state table mock, evaluate_rerun_safety spy)``.
    """
    state_table = _wire_tables(pipeline_record, fv_row)
    _wire_glue_job(worker_type)
    _mock_sfn.reset_mock(side_effect=True)
    _mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:::execution/x"}

    with (
        patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
        patch.object(sha, "_notify_rerun_declined"),
        patch.object(sha, "_notify_heal_applied"),
        patch.object(sha, "evaluate_rerun_safety", wraps=sha.evaluate_rerun_safety) as safety,
    ):
        result = sha.handler(_payload(error_message, **payload_extra))
    return result, state_table, safety


def _outcome_item(state_table: MagicMock) -> dict:
    """The single ``self_healing`` record written to the state table."""
    items = [c.kwargs["Item"] for c in state_table.put_item.call_args_list]
    outcome = [i for i in items if i.get("step_name") == "self_healing"]
    assert len(outcome) == 1, f"expected exactly one self_healing record, got {len(outcome)}"
    return outcome[0]


# ── Clause 1: unsafe target → no re-run, classified, reason surfaced ──


class TestUnsafeTargetDeclinesTheRerun:
    """R15.6 clause 1. Both remediations that reach the gate are covered:
    ``rerun_pipeline`` (schema/type failures) and ``upgrade_worker_type``
    (OOM), because the gate is on the re-run and not on the classification."""

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    @pytest.mark.parametrize("error_message", [_SCHEMA_MESSAGE, _OOM_MESSAGE], ids=["rerun_pipeline", "oom_upgrade"])
    def test_no_execution_is_started(self, record, error_message):
        _run(error_message, record)
        _mock_sfn.start_execution.assert_not_called()

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_the_record_says_nothing_was_started(self, record):
        """``rerun_id`` must stay empty: a lineage id on a record with
        ``rerun_started=False`` reads as a re-run that exists somewhere."""
        result, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        item = _outcome_item(state_table)
        assert item["rerun_started"] is False
        assert item["rerun_id"] == ""
        assert "rerun_execution_arn" not in item
        assert result["rerun_started"] is False
        assert result["rerun_id"] == ""

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_the_failure_is_classified_for_human_review(self, record):
        result, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        item = _outcome_item(state_table)
        assert item["rerun_status"] == "skipped_partial_write_guard"
        assert item["action"] == "flag_for_review"
        assert result["rerun_status"] == "skipped_partial_write_guard"
        assert result["action"] == "flag_for_review"

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_the_reason_reaches_the_outcome_record(self, record):
        """R15.6 "with the reason surfaced": the verdict travels to the record,
        not just to the log line."""
        _, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        item = _outcome_item(state_table)
        expected = sha.evaluate_rerun_safety(record)["reason"]
        assert item["skip_reason"] == "partial_write_guard"
        assert item["skip_detail"] == expected
        assert item["rerun_safety"]["safe"] is False
        assert item["rerun_safety"]["reason"] == expected
        # …and in the field the dashboard renders as the remediation.
        assert expected in item["remediation_applied"]

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_the_record_names_the_action_it_declined(self, record):
        """``action`` is rewritten to ``flag_for_review``, so without
        ``declined_action`` nothing on the record says what would have run."""
        result, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        assert _outcome_item(state_table)["declined_action"] == "rerun_pipeline"
        assert result["declined_action"] == "rerun_pipeline"

    def test_an_oom_decline_names_the_worker_upgrade_as_the_declined_action(self):
        result, state_table, _ = _run(_OOM_MESSAGE, _GLUE_CATALOG_OVERWRITE)
        assert _outcome_item(state_table)["declined_action"] == "upgrade_worker_type"
        assert result["action"] == "flag_for_review"

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_it_does_not_read_as_a_generic_flagged_for_review(self, record):
        """R15.3's wording requirement, seen from the record: the action is the
        same ``flag_for_review`` an unclassifiable error produces, so the
        distinguishing detail has to be present and the generic text absent."""
        _, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        item = _outcome_item(state_table)
        assert "requires manual intervention" not in item["remediation_applied"]
        assert "Re-run declined" in item["remediation_applied"]

        # For contrast: the unclassifiable path is the generic one, and it
        # carries no guard verdict at all.
        _, generic_table, _ = _run("some completely novel failure", record)
        generic = _outcome_item(generic_table)
        assert generic["action"] == "flag_for_review"
        assert "requires manual intervention" in generic["remediation_applied"]
        assert "skip_reason" not in generic


class TestTheGuardIsMatrixGatedNotDetectionGated:
    """The premise R15.6 states — "with a recorded partial write" — has no
    signal behind it (task 0.5). What can be pinned is the consequence: the
    decline is decided from (target, write_mode) alone, so it holds for every
    non-``SUCCEEDED`` terminal state and does not weaken when nothing about the
    write is reported."""

    @pytest.mark.parametrize("state", ["FAILED", "TIMEOUT", "STOPPED", "ERROR"])
    def test_the_decline_is_identical_for_every_terminal_state(self, state):
        result, _, _ = _run(_SCHEMA_MESSAGE, _S3_TABLES_APPEND, state=state)
        assert result["rerun_status"] == "skipped_partial_write_guard"
        assert result["skip_reason"] == "partial_write_guard"

    def test_a_payload_with_no_write_information_still_declines(self):
        """The dispatch payload reports nothing about rows written, and the
        guard does not need it to."""
        payload_keys = set(_payload(_SCHEMA_MESSAGE, job_run_id="jr-1", state="FAILED"))
        assert not {k for k in payload_keys if "row" in k or "written" in k}
        result, _, _ = _run(_SCHEMA_MESSAGE, _S3_TABLES_APPEND, job_run_id="jr-1", state="FAILED")
        assert result["rerun_status"] == "skipped_partial_write_guard"

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_the_recorded_reason_does_not_claim_a_partial_write_was_detected(self, record):
        """A record that claimed detection would be lying about its evidence."""
        _, state_table, _ = _run(_SCHEMA_MESSAGE, record)
        detail = _outcome_item(state_table)["skip_detail"].lower()
        assert "detected" not in detail
        assert "may have written rows" in detail


class TestAnAlreadyAppliedFixSurvivesTheDecline:
    """``remediate_oom`` calls ``update_job`` BEFORE the gate, so a decline
    cannot pretend nothing happened. 6.3 pins the ``applied_fix`` that reaches
    the notification; this is the record half."""

    def test_the_worker_upgrade_stays_visible_in_remediation_applied(self):
        _, state_table, _ = _run(_OOM_MESSAGE, _GLUE_CATALOG_OVERWRITE)
        applied = _outcome_item(state_table)["remediation_applied"]
        assert "Upgraded worker type from G.1X to G.2X" in applied
        # The decline reason is appended, not substituted.
        assert "Re-run declined" in applied

    def test_the_glue_job_was_really_mutated_before_the_gate(self):
        _run(_OOM_MESSAGE, _GLUE_CATALOG_OVERWRITE)
        assert _mock_glue.update_job.called
        assert _mock_glue.update_job.call_args.kwargs["JobUpdate"]["WorkerType"] == "G.2X"


# ── Clause 2: safe target → the re-run is unchanged ──────────────────


class TestSafeTargetRerunsUnchanged:
    """R15.6 clause 2, and the regression half of the task: the permitted path
    must be behaviourally identical to the pre-guard handler."""

    def test_the_execution_is_started_exactly_once(self):
        _run(_SCHEMA_MESSAGE, _SAFE_RECORD)
        assert _mock_sfn.start_execution.call_count == 1

    def test_the_record_keeps_its_pre_guard_shape(self):
        result, state_table, _ = _run(_SCHEMA_MESSAGE, _SAFE_RECORD)
        item = _outcome_item(state_table)
        assert item["rerun_started"] is True
        assert item["rerun_status"] == "running"
        assert item["rerun_id"] == "run-1-heal"
        assert item["action"] == "rerun_pipeline"
        assert item["rerun_execution_arn"] == "arn:aws:states:::execution/x"
        # None of the decline fields may appear on a permitted re-run.
        assert "skip_reason" not in item
        assert "skip_detail" not in item
        assert "declined_action" not in item
        assert result["rerun_started"] is True

    def test_the_permitted_verdict_is_still_recorded(self):
        """The guard runs on the safe path too; R15.5 wants the exemption
        readable per run, so the verdict is on the record even when nothing
        was declined."""
        _, state_table, safety = _run(_SCHEMA_MESSAGE, _SAFE_RECORD)
        assert safety.call_count == 1
        item = _outcome_item(state_table)
        assert item["rerun_safety"]["safe"] is True
        assert item["rerun_safety"]["target_type"] == "s3_tables"
        assert item["rerun_safety"]["write_mode"] == "overwrite"
        assert item["rerun_safety"]["reason"]

    def test_the_oom_path_still_carries_the_worker_override(self):
        """R9.5: the WorkerType override rides the re-run input. The gate sits
        between building it and starting the execution, so this is exactly
        where it could have been dropped."""
        _, state_table, _ = _run(_OOM_MESSAGE, _SAFE_RECORD)
        item = _outcome_item(state_table)
        assert item["action"] == "upgrade_worker_type"
        assert item["rerun_started"] is True
        execution_input = json.loads(_mock_sfn.start_execution.call_args.kwargs["input"])
        assert execution_input["job_config_overrides"] == {"WorkerType": "G.2X"}

    def test_the_rerun_input_still_carries_the_full_trigger_contract(self):
        """R9.1: the guard hoisted the pipeline-record read above the gate and
        passes it down, so the built input must be unchanged."""
        _run(_SCHEMA_MESSAGE, _SAFE_RECORD)
        execution_input = json.loads(_mock_sfn.start_execution.call_args.kwargs["input"])
        assert execution_input["triggered_by"] == "self_healing"
        assert execution_input["run_id"] == "run-1-heal"
        assert execution_input["original_run_id"] == "run-1"
        assert execution_input["source_bucket"] == "src-bkt"
        assert execution_input["source_type"] == "S3_UPLOAD"

    @pytest.mark.parametrize("target_type,write_mode", _SAFE_SPELLINGS)
    def test_every_safe_spelling_still_reruns(self, target_type, write_mode):
        """A guard that declined everything would satisfy clause 1 while
        switching self-healing off. Each persisted spelling in the allow-list
        must survive the handler, not just the predicate."""
        result, _, _ = _run(_SCHEMA_MESSAGE, _record(target_type, write_mode))
        assert result["rerun_started"] is True, f"({target_type}, {write_mode}) was declined"
        assert result["rerun_status"] == "running"
        assert _mock_sfn.start_execution.call_count == 1


# ── Clause 3: Heal_Guard still enforces one heal per run ─────────────


class TestHealGuardStillEnforcesOneHealPerRun:
    """R15.6 clause 3 / R15.4. Asserted by PRECEDENCE, not by absence: the new
    condition is added ahead of nothing and behind the Heal_Guard, and
    ``skip_reason`` is the only thing that distinguishes which guard fired."""

    @pytest.mark.parametrize("record", _UNSAFE_RECORDS, ids=["glue_catalog_overwrite", "s3_tables_append"])
    def test_a_heal_triggered_run_on_an_unsafe_target_skips_via_the_heal_guard(self, record):
        """The case that would be invisible if the precedence were reversed:
        both guards would decline, both would produce a skip, and only
        ``skip_reason`` says which one — and only the Heal_Guard's path skips
        remediation entirely."""
        result, _, safety = _run(_OOM_MESSAGE, record, fv_row=_HEAL_FV_ROW)
        assert result["skip_reason"] == "heal_guard"
        assert result["skip_reason"] != "partial_write_guard"
        assert result["rerun_status"] == "skipped_heal_guard"
        assert result["status"] == "skipped"
        # The Partial_Write_Guard is never consulted, because no remediation
        # was attempted at all.
        assert safety.call_count == 0
        _mock_glue.update_job.assert_not_called()
        _mock_sfn.start_execution.assert_not_called()

    def test_a_heal_triggered_run_on_a_safe_target_still_skips(self):
        """The permit branch must not have opened a second way through."""
        result, _, safety = _run(_SCHEMA_MESSAGE, _SAFE_RECORD, fv_row=_HEAL_FV_ROW)
        assert result["rerun_status"] == "skipped_heal_guard"
        assert result["rerun_id"] == ""
        assert safety.call_count == 0
        _mock_sfn.start_execution.assert_not_called()

    def test_the_payload_marker_short_circuits_ahead_of_the_gate(self):
        result, _, safety = _run(_SCHEMA_MESSAGE, _SAFE_RECORD, fv_row=_NORMAL_FV_ROW, triggered_by="self_healing")
        assert result["rerun_status"] == "skipped_heal_guard"
        assert safety.call_count == 0
        _mock_sfn.start_execution.assert_not_called()

    @pytest.mark.parametrize("record", [_SAFE_RECORD, _GLUE_CATALOG_OVERWRITE], ids=["safe", "unsafe"])
    def test_a_failed_trigger_lookup_still_fails_closed(self, record):
        """R15.4's "fails closed on lookup error", preserved on both sides of
        the new condition."""
        state_table = _wire_tables(record)
        state_table.get_item.side_effect = RuntimeError("dynamo down")
        _wire_glue_job()
        _mock_sfn.reset_mock(side_effect=True)

        with (
            patch.dict("sys.modules", dict(_HANDLER_MODULE_MOCKS)),
            patch.object(sha, "logger"),
            patch.object(sha, "evaluate_rerun_safety", wraps=sha.evaluate_rerun_safety) as safety,
        ):
            result = sha.handler(_payload(_OOM_MESSAGE))

        assert result["skip_reason"] == "heal_guard"
        assert safety.call_count == 0
        _mock_sfn.start_execution.assert_not_called()

    def test_a_normally_triggered_run_reaches_the_gate(self):
        """The other half of precedence: the Heal_Guard must not have widened
        into blocking runs the guard was supposed to grade."""
        _, _, safety = _run(_SCHEMA_MESSAGE, _GLUE_CATALOG_OVERWRITE)
        assert safety.call_count == 1


class TestActionsThatNeverReachTheGate:
    """Neither ``flag_for_review`` nor ``no_upgrade_available`` constitutes a
    corrected input, so the pre-existing honest gating (R9.7) keeps them out of
    the re-run branch entirely — and therefore out of the guard. A verdict on
    a record for a run that was never going to re-run would misattribute the
    skip."""

    def test_an_unclassifiable_failure_is_not_graded(self):
        result, state_table, safety = _run("some completely novel failure", _GLUE_CATALOG_OVERWRITE)
        assert safety.call_count == 0
        assert result["rerun_status"] == "skipped"
        assert "rerun_safety" not in _outcome_item(state_table)

    def test_a_permission_failure_is_not_graded(self):
        result, _, safety = _run(_PERMISSION_MESSAGE, _GLUE_CATALOG_OVERWRITE)
        assert safety.call_count == 0
        assert result["rerun_status"] == "skipped"

    def test_an_oom_already_at_the_top_worker_tier_is_not_graded(self):
        """``no_upgrade_available`` applies no fix, so there is nothing whose
        re-run needs grading."""
        result, state_table, safety = _run(_OOM_MESSAGE, _GLUE_CATALOG_OVERWRITE, worker_type="G.8X")
        assert result["action"] == "no_upgrade_available"
        assert result["rerun_status"] == "skipped"
        assert safety.call_count == 0
        assert "skip_reason" not in _outcome_item(state_table)
        _mock_glue.update_job.assert_not_called()
