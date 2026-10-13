# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Id-scoped apply for the cost optimization agent (p30a task 1.6, R1.4).

``POST /api/cost-recommendations/{id}/apply`` used to reach an agent that ignored
its payload entirely and rescanned every Glue job in the account. These tests pin
the four behaviours R1.4 asks for:

* an id resolves to exactly one recommendation from the latest
  ``cost_optimization`` state row, and only that job is changed;
* the weekly schedule payload still performs the full fleet sweep;
* an id that is not in that row is a handled error — not a silent no-op and not a
  fallback to sweeping;
* ``AUTO_APPLY_ENABLED`` that is not exactly ``"true"`` applies nothing and says so.

Expectations here come from the requirement, not from the previous behaviour of
this code: ``handler`` and ``auto_apply_recommendation`` had no covering tests
before this file, and their previous output contained the bug.

The agent creates its boto3 clients at import time, so it cannot take the shared
DynamoDB fixture through the front door. It is wired to it instead: the fixture
owns the moto tables and the real scan/pagination semantics, and only the Glue
client — for which the fixture set has no equivalent — is a mock, because the
whole point is to assert on what the mutation call looks like without making it.

**Validates: Requirements 1.4**
"""

import importlib.util
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "cost_optimization_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

STATE_TABLE_NAME = "agentic_data_ingestion_state"

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
#
# test_cost_optimization.py imports the module with BedrockAgentCoreApp fully
# mocked, which turns ``handler`` into a MagicMock. Payload routing is a
# handler-level property, so this file loads its own copy under a unique
# sys.modules name (no "main" collision with the other agent test files) with
# ``entrypoint`` behaving as the identity decorator.

_mock_glue = MagicMock()
_mock_cw = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: {
    "glue": _mock_glue,
    "cloudwatch": _mock_cw,
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
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("coa_apply_main", MAIN_PATH)
    coa = importlib.util.module_from_spec(_spec)
    sys.modules["coa_apply_main"] = coa
    _spec.loader.exec_module(coa)

    coa._glue_client = _mock_glue
    coa._cloudwatch = _mock_cw


#: ``handler`` imports ``agent_trace_plugin`` lazily, so the mock has to be back
#: in ``sys.modules`` at CALL time, not only at import time.
_HANDLER_MODULE_MOCKS = {"agent_trace_plugin": MagicMock()}


def _call_handler(payload):
    with patch.dict("sys.modules", _HANDLER_MODULE_MOCKS):
        return coa.handler(payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _downsize_rec(job_name, workers=Decimal("4"), worker_type="G.1X"):
    """A downsize recommendation shaped as it comes back out of DynamoDB."""
    return {
        "pipeline_id": f"pipe-{job_name}",
        "job_name": job_name,
        "type": "downsize",
        "current": {"worker_type": "G.2X", "workers": Decimal("8")},
        "recommended": {"worker_type": worker_type, "workers": workers},
        "risk": "low",
        "auto_apply_eligible": True,
        "projected_savings_monthly": Decimal("12.50"),
    }


@pytest.fixture
def state_table(mock_dynamodb_tables, monkeypatch):
    """Point the agent at the moto-backed state table from the shared fixture."""
    table = mock_dynamodb_tables[STATE_TABLE_NAME]
    # Real boto3 here, inside the fixture's mock_aws() scope, so the resource is
    # intercepted by moto rather than being the import-time MagicMock.
    monkeypatch.setattr(coa, "_dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))
    monkeypatch.setattr(coa, "_require_env", MagicMock(return_value=STATE_TABLE_NAME))
    return table


@pytest.fixture
def audit_calls(monkeypatch):
    """Capture audit writes so refusals can be shown to be auditable."""
    spy = MagicMock()
    monkeypatch.setattr(coa, "write_agent_audit_entry", spy)
    return spy


@pytest.fixture
def no_sweep(monkeypatch):
    """Spy on fleet discovery — the thing an id-scoped apply must never do."""
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(coa, "get_all_glue_jobs", spy)
    return spy


@pytest.fixture(autouse=True)
def _reset_glue_mock():
    _mock_glue.reset_mock()
    _mock_glue.get_job.return_value = {
        "Job": {
            "Name": "job-a",
            "Role": "arn:aws:iam::123456789012:role/glue-role",
            "Command": {"Name": "glueetl", "ScriptLocation": "s3://bucket/script.py"},
            "GlueVersion": "4.0",
            "DefaultArguments": {"--pipeline_id": "pipe-job-a"},
            "Connections": {"Connections": ["adi-jdbc-target"]},
            "Timeout": 60,
            "MaxRetries": 1,
            "ExecutionProperty": {"MaxConcurrentRuns": 1},
            "WorkerType": "G.2X",
            "NumberOfWorkers": 8,
        }
    }
    yield
    _mock_glue.reset_mock()


def _write_report(table, recommendations, timestamp="2026-07-30T06:00:00+00:00", run_id="cost-1"):
    table.put_item(
        Item={
            "run_id": run_id,
            "step_name": "cost_optimization",
            "timestamp": timestamp,
            "generated_at": timestamp,
            "recommendations": recommendations,
            "total_projected_savings_monthly": Decimal("25.00"),
        }
    )


# ---------------------------------------------------------------------------
# AUTO_APPLY_ENABLED enforcement
# ---------------------------------------------------------------------------


class TestAutoApplyGate:
    """R1.4: not exactly ``"true"`` → nothing applied, said honestly."""

    @pytest.mark.parametrize("flag", [None, "", "false", "1", "yes", "TRUE ", " true", "True!"])
    def test_near_miss_values_apply_nothing(self, flag, monkeypatch, audit_calls, no_sweep):
        if flag is None:
            monkeypatch.delenv("AUTO_APPLY_ENABLED", raising=False)
        else:
            monkeypatch.setenv("AUTO_APPLY_ENABLED", flag)

        result = coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        assert result["applied"] is False
        assert result["auto_apply_enabled"] is False
        assert "AUTO_APPLY_ENABLED" in result["reason"]
        _mock_glue.update_job.assert_not_called()
        no_sweep.assert_not_called()

    def test_refusal_is_audited(self, monkeypatch, audit_calls):
        monkeypatch.delenv("AUTO_APPLY_ENABLED", raising=False)

        coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        audit_calls.assert_called_once()
        assert audit_calls.call_args.kwargs["details"]["outcome"] == "auto_apply_disabled"
        assert audit_calls.call_args.kwargs["details"]["applied"] is False

    def test_disabled_gate_does_not_read_the_state_table(self, monkeypatch, audit_calls):
        monkeypatch.delenv("AUTO_APPLY_ENABLED", raising=False)
        loader = MagicMock(return_value=([], None))
        monkeypatch.setattr(coa, "load_latest_cost_recommendations", loader)

        coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        loader.assert_not_called()

    @pytest.mark.parametrize("flag", ["true", "TRUE", "True"])
    def test_exact_true_is_accepted_case_insensitively(self, flag, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_ENABLED", flag)
        assert coa._auto_apply_enabled() is True


# ---------------------------------------------------------------------------
# Id-scoped apply
# ---------------------------------------------------------------------------


class TestIdScopedApply:
    @pytest.fixture(autouse=True)
    def _enable_auto_apply(self, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")

    def test_applies_only_the_identified_recommendation(self, state_table, audit_calls, no_sweep):
        _write_report(
            state_table,
            [
                _downsize_rec("job-a"),
                _downsize_rec("job-b", workers=Decimal("2")),
                _downsize_rec("job-c"),
            ],
        )

        result = coa.apply_single_recommendation("job-b:downsize", run_id="cost-test")

        assert result["applied"] is True
        assert result["job_name"] == "job-b"
        # Exactly one job mutated, and it is the one named by the id.
        _mock_glue.update_job.assert_called_once()
        assert _mock_glue.update_job.call_args.kwargs["JobName"] == "job-b"
        # And no fleet rescan happened on the way.
        no_sweep.assert_not_called()

    def test_applies_the_recommended_worker_size(self, state_table, audit_calls):
        _write_report(state_table, [_downsize_rec("job-a", workers=Decimal("2"), worker_type="G.1X")])

        coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        job_update = _mock_glue.update_job.call_args.kwargs["JobUpdate"]
        assert job_update["WorkerType"] == "G.1X"
        assert job_update["NumberOfWorkers"] == 2

    def test_worker_count_is_an_int_not_a_decimal(self, state_table, audit_calls):
        # DynamoDB hands back Decimal and boto3 rejects it for NumberOfWorkers.
        _write_report(state_table, [_downsize_rec("job-a", workers=Decimal("2"))])

        coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        workers = _mock_glue.update_job.call_args.kwargs["JobUpdate"]["NumberOfWorkers"]
        assert isinstance(workers, int)
        assert not isinstance(workers, Decimal)

    def test_reads_the_most_recent_report(self, state_table, audit_calls):
        _write_report(state_table, [_downsize_rec("old-job")], timestamp="2026-07-01T06:00:00+00:00", run_id="cost-old")
        _write_report(state_table, [_downsize_rec("new-job")], timestamp="2026-07-30T06:00:00+00:00", run_id="cost-new")

        result = coa.apply_single_recommendation("new-job:downsize", run_id="cost-test")
        assert result["applied"] is True

        stale = coa.apply_single_recommendation("old-job:downsize", run_id="cost-test")
        assert stale["applied"] is False
        assert stale["error"] == "recommendation_not_found"

    def test_ignores_rows_from_other_steps(self, state_table, audit_calls):
        state_table.put_item(
            Item={
                "run_id": "dq-1",
                "step_name": "data_quality",
                "timestamp": "2026-07-31T06:00:00+00:00",
                "recommendations": [_downsize_rec("not-a-cost-job")],
            }
        )
        _write_report(state_table, [_downsize_rec("job-a")])

        result = coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")
        assert result["applied"] is True

        wrong_step = coa.apply_single_recommendation("not-a-cost-job:downsize", run_id="cost-test")
        assert wrong_step["error"] == "recommendation_not_found"

    def test_non_downsize_recommendation_is_not_applied(self, state_table, audit_calls):
        _write_report(
            state_table,
            [
                {
                    "pipeline_id": "pipe-1",
                    "job_name": "job-a",
                    "type": "idle",
                    "risk": "low",
                    "auto_apply_eligible": False,
                }
            ],
        )

        result = coa.apply_single_recommendation("job-a:idle", run_id="cost-test")

        assert result["applied"] is False
        _mock_glue.update_job.assert_not_called()


class TestUnknownRecommendationId:
    """Safety: a bad id must not become a fleet-wide operation."""

    @pytest.fixture(autouse=True)
    def _enable_auto_apply(self, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")

    def test_unknown_id_is_a_handled_error_with_no_sweep(self, state_table, audit_calls, no_sweep):
        _write_report(state_table, [_downsize_rec("job-a")])

        result = coa.apply_single_recommendation("job-does-not-exist:downsize", run_id="cost-test")

        assert result["error"] == "recommendation_not_found"
        assert result["applied"] is False
        assert result["available_recommendation_ids"] == ["job-a:downsize"]
        _mock_glue.update_job.assert_not_called()
        no_sweep.assert_not_called()

    def test_unknown_id_is_audited(self, state_table, audit_calls):
        _write_report(state_table, [_downsize_rec("job-a")])

        coa.apply_single_recommendation("nope:downsize", run_id="cost-test")

        assert audit_calls.call_args.kwargs["details"]["outcome"] == "recommendation_not_found"

    def test_missing_id_is_a_handled_error(self, state_table, audit_calls, no_sweep):
        result = coa.apply_single_recommendation("", run_id="cost-test")

        assert result["error"] == "missing_recommendation_id"
        assert result["applied"] is False
        _mock_glue.update_job.assert_not_called()
        no_sweep.assert_not_called()

    def test_no_report_at_all_is_a_handled_error(self, state_table, audit_calls, no_sweep):
        result = coa.apply_single_recommendation("job-a:downsize", run_id="cost-test")

        assert result["error"] == "no_recommendations_available"
        assert result["applied"] is False
        _mock_glue.update_job.assert_not_called()
        no_sweep.assert_not_called()


# ---------------------------------------------------------------------------
# Payload routing — the weekly sweep must survive
# ---------------------------------------------------------------------------


class TestHandlerRouting:
    """The sweep paths take ``state_table`` because every sweep now writes a row.

    They did not need it while an empty fleet returned before step 7. That early
    return was defect B (p30a R1.3): a sweep finding no jobs left no
    ``cost_optimization`` row, so it was indistinguishable from an agent that had
    never run. The row is now written on every sweep path, empty or not, which is
    why these three need the table wired up.
    """

    def test_weekly_schedule_payload_still_sweeps_the_fleet(self, state_table, no_sweep, audit_calls):
        result = _call_handler({"trigger": "weekly_schedule"})

        no_sweep.assert_called_once()
        # Empty fleet → the report shape, not an apply result.
        assert "recommendations" in result
        assert "recommendation_id" not in result

    def test_empty_payload_still_sweeps_the_fleet(self, state_table, no_sweep, audit_calls):
        _call_handler({})
        no_sweep.assert_called_once()

    def test_non_dict_payload_still_sweeps_the_fleet(self, state_table, no_sweep, audit_calls):
        _call_handler(None)
        no_sweep.assert_called_once()

    def test_apply_action_routes_to_the_single_recommendation_path(self, no_sweep, monkeypatch, audit_calls):
        applier = MagicMock(return_value={"applied": True, "recommendation_id": "job-a:downsize"})
        monkeypatch.setattr(coa, "apply_single_recommendation", applier)

        result = _call_handler({"action": "apply_recommendation", "recommendation_id": "job-a:downsize"})

        no_sweep.assert_not_called()
        assert applier.call_args.kwargs["recommendation_id"] == "job-a:downsize"
        assert result["applied"] is True
        assert "total_duration_ms" in result

    def test_unsupported_action_is_refused_rather_than_swept(self, no_sweep, audit_calls):
        result = _call_handler({"action": "delete_everything"})

        assert result["error"] == "unsupported_action"
        assert result["jobs_scanned"] == 0
        no_sweep.assert_not_called()


# ---------------------------------------------------------------------------
# The mutation itself
# ---------------------------------------------------------------------------


class TestMutationIsNarrow:
    """A downsize changes worker size and nothing else.

    Glue's UpdateJob replaces the job definition rather than merging into it, so
    a field left out of JobUpdate is dropped. Dropping ``Connections`` would
    detach a JDBC job from the Glue connection Glue reads ``SECRET_ID`` from at
    run time (LGR-819980f-2, LGR-2474614-1).
    """

    @pytest.fixture(autouse=True)
    def _enable_auto_apply(self, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_ENABLED", "true")

    def test_preserves_glue_connections(self):
        coa.auto_apply_recommendation(_downsize_rec("job-a", workers=2))

        job_update = _mock_glue.update_job.call_args.kwargs["JobUpdate"]
        assert job_update["Connections"] == {"Connections": ["adi-jdbc-target"]}

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("Timeout", 60),
            ("MaxRetries", 1),
            ("ExecutionProperty", {"MaxConcurrentRuns": 1}),
            ("GlueVersion", "4.0"),
            ("DefaultArguments", {"--pipeline_id": "pipe-job-a"}),
        ],
    )
    def test_preserves_operational_fields(self, field, expected):
        coa.auto_apply_recommendation(_downsize_rec("job-a", workers=2))

        assert _mock_glue.update_job.call_args.kwargs["JobUpdate"][field] == expected

    def test_does_not_echo_back_max_capacity_or_the_old_worker_size(self):
        coa.auto_apply_recommendation(_downsize_rec("job-a", workers=2, worker_type="G.1X"))

        job_update = _mock_glue.update_job.call_args.kwargs["JobUpdate"]
        # MaxCapacity is rejected by Glue alongside WorkerType.
        assert "MaxCapacity" not in job_update
        assert job_update["WorkerType"] == "G.1X"
        assert job_update["NumberOfWorkers"] == 2

    def test_one_update_job_call_and_nothing_else_mutating(self):
        coa.auto_apply_recommendation(_downsize_rec("job-a", workers=2))

        assert _mock_glue.update_job.call_count == 1
        _mock_glue.delete_job.assert_not_called()
        _mock_glue.start_job_run.assert_not_called()
