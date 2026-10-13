# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Job discovery scope and the empty-fleet state write (p30a R1.3).

Two defects found by task 2.2's live run, which is the first time this agent had
ever been invoked:

* **Discovery matched nothing.** ``get_all_glue_jobs`` filtered on the prefix
  ``"agentic_data_ingestion"`` (underscores) while every job the project creates
  is kebab-case ``agentic-data-ingestion-job-<pipeline_id>``. 0 of the 44 jobs in
  the dev account matched, so the sweep had nothing to look at.
* **An empty fleet wrote no state row.** The handler returned ``build_report([],
  0.0)`` before the ``put_item``, so a sweep that found no jobs was
  indistinguishable from an agent that had never run — which is exactly the
  condition R1.3 is supposed to be able to observe.

The fleet fixtures below are the REAL job names deployed in 444455556666 /
us-west-2 as of 2026-08-01, not invented ones. That is deliberate: the previous
tests used names like ``agentic-data-ingestion-etl-1`` that no creation path
produces, which is why a filter matching zero real jobs passed 21 tests.

Expectations come from the requirement and from the creating code
(``job_builders.py:442``, ``schema_evolution.py:299``), not from the agent's prior
output — its prior output was the bug.

**Validates: Requirements 1.3**
"""

import importlib.util
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "cost_optimization_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

STATE_TABLE_NAME = "agentic_data_ingestion_state"

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
#
# Same posture as test_apply_single_recommendation.py: the agent builds its boto3
# clients at import time, and the empty-fleet behaviour is a property of
# ``handler`` itself, so this file loads its own copy under a unique sys.modules
# name with ``entrypoint`` acting as the identity decorator.

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
    _spec = importlib.util.spec_from_file_location("coa_discovery_main", MAIN_PATH)
    coa = importlib.util.module_from_spec(_spec)
    sys.modules["coa_discovery_main"] = coa
    _spec.loader.exec_module(coa)

    coa._glue_client = _mock_glue
    coa._cloudwatch = _mock_cw


#: ``handler`` imports ``agent_trace_plugin`` lazily, so the mock must be back in
#: ``sys.modules`` at CALL time, not only at import time.
_HANDLER_MODULE_MOCKS = {"agent_trace_plugin": MagicMock()}


def _call_handler(payload):
    with patch.dict("sys.modules", _HANDLER_MODULE_MOCKS):
        return coa.handler(payload)


# ---------------------------------------------------------------------------
# The real deployed fleet — 444455556666 / us-west-2, `glue get-jobs`, 2026-08-01
# ---------------------------------------------------------------------------

#: Created by the CURRENT pipeline-creation code (``agentic-data-ingestion-job-``).
IN_SCOPE_JOBS: tuple[str, ...] = (
    "agentic-data-ingestion-job-testubf-93fb4dcb",
    "agentic-data-ingestion-job-kjljlkj-ba061ab9",
)

#: Same creation code, pre-rename prefix (commit 2b45b72 renamed AgentGlue →
#: Agentic Data Ingestion). Legacy-owned, but outside the AgentCore runtime role's
#: Glue grant, so out of scope by decision — see ``_PROJECT_JOB_NAME_PREFIXES``.
LEGACY_AGENTGLUE_JOBS: tuple[str, ...] = (
    "agentglue-job-1113mike-05c71661",
    "agentglue-job-1144-b4e3c41f",
    "agentglue-job-anagenttestpipeline-72b555e8",
    "agentglue-job-anewpipeline-124b76fe",
    "agentglue-job-anewpipeline-b918bc98",
    "agentglue-job-anewpipeline-d635f2d1",
    "agentglue-job-anewpipeline-df1fb860",
    "agentglue-job-anewpipeline1-8e0c6ff8",
    "agentglue-job-csvtojdbc-df0efed6",
    "agentglue-job-dssdds-8cd18496",
    "agentglue-job-fsidemo-2f176620",
    "agentglue-job-jdbcfinaltest-b0547e74",
    "agentglue-job-mydemopipeline-ec79ed6e",
    "agentglue-job-myfirstpipeline-af06778d",
    "agentglue-job-myharshpipeline-ee1fe74c",
    "agentglue-job-mylatestpipeline-78394671",
    "agentglue-job-mypipeline-3ede6909",
    "agentglue-job-mypipeline-b7e28b30",
    "agentglue-job-newawspipeline-2141bcbc",
    "agentglue-job-newpipelinerunnext-d59e2216",
    "agentglue-job-oteltesting-a3c5c318",
    "agentglue-job-p112pipeline-701d89da",
    "agentglue-job-p11pipeline-4012c459",
    "agentglue-job-pipelinedataingesttest-70791884",
    "agentglue-job-sddsasdaa-d1fa6d29",
    "agentglue-job-smoketestingpipeline-846fdcb9",
    "agentglue-job-sreejidemo-ee2eb0f5",
    "agentglue-job-storylanedemo-46a52e03",
)

#: Not created by anything in this repository — no source file emits their
#: Description template, and they are named by timestamp rather than pipeline_id.
FOREIGN_STRANDS_JOBS: tuple[str, ...] = (
    "strands-transform-20250822-124535",
    "strands-transform-20250822-130243",
    "strands-transform-20250822-132057",
    "strands-transform-20250822-135500",
    "strands-transform-20250822-140521",
    "strands-transform-20250822-141008",
    "strands-transform-20250822-141947",
    "strands-transform-20250822-143348",
    "strands-transform-20250822-144014",
    "strands-transform-20250822-145935",
    "strands-transform-20250822-150348",
    "strands-transform-20250822-153230",
    "strands-transform-20250822-153653",
    "strands-transform-20250822-162252",
)

DEPLOYED_FLEET: tuple[str, ...] = IN_SCOPE_JOBS + LEGACY_AGENTGLUE_JOBS + FOREIGN_STRANDS_JOBS


def _glue_job(name: str, pipeline_id: str | None = None) -> dict:
    return {
        "Name": name,
        "WorkerType": "G.2X",
        "NumberOfWorkers": 8,
        "GlueVersion": "4.0",
        "DefaultArguments": {"--pipeline_id": pipeline_id} if pipeline_id else {},
    }


@pytest.fixture
def fleet():
    """Install a paginated ``get_jobs`` response over a given list of job names."""

    def _install(names, page_size=25):
        pages = [
            {"Jobs": [_glue_job(n) for n in names[i : i + page_size]]}
            for i in range(0, len(names), page_size)
        ] or [{"Jobs": []}]
        paginator = MagicMock()
        paginator.paginate.return_value = pages
        _mock_glue.get_paginator.return_value = paginator
        return paginator

    _mock_glue.reset_mock()
    yield _install
    _mock_glue.reset_mock()


# ---------------------------------------------------------------------------
# Defect A — discovery scope
# ---------------------------------------------------------------------------


class TestOwnershipPredicate:
    @pytest.mark.parametrize("name", IN_SCOPE_JOBS)
    def test_current_naming_is_owned(self, name):
        assert coa.is_project_glue_job(name) is True

    def test_versioned_form_is_owned(self):
        # schema_evolution.py:299 — agentic-data-ingestion-job-<id>-v<N>
        assert coa.is_project_glue_job("agentic-data-ingestion-job-testubf-93fb4dcb-v2") is True

    def test_snake_form_is_owned(self):
        # No current path emits this, but the runtime role grants it
        # (foundation-stack.ts:188), so it must not be silently skipped.
        assert coa.is_project_glue_job("agentic_data_ingestion_job_testubf") is True

    @pytest.mark.parametrize("name", LEGACY_AGENTGLUE_JOBS)
    def test_pre_rename_agentglue_jobs_are_out_of_scope(self, name):
        assert coa.is_project_glue_job(name) is False

    @pytest.mark.parametrize("name", FOREIGN_STRANDS_JOBS)
    def test_foreign_strands_jobs_are_out_of_scope(self, name):
        assert coa.is_project_glue_job(name) is False

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "copy-of-agentic-data-ingestion-job-testubf",
            "backup_agentic_data_ingestion_state",
            "AGENTIC-DATA-INGESTION-JOB-TESTUBF",
            "agentic-data-ingestion-crawler-testubf",
        ],
    )
    def test_prefix_not_substring_and_not_other_resource_kinds(self, name):
        """A mention of the project is not ownership.

        Whatever discovery returns is what a later Apply may mutate, so the match
        is anchored at the start of the name and is case-sensitive. The crawler
        case matters because ``agentic-data-ingestion-`` alone would accept it.
        """
        assert coa.is_project_glue_job(name) is False

    @settings(max_examples=100, deadline=None)
    @given(st.text(min_size=0, max_size=60))
    def test_only_the_declared_prefixes_are_ever_owned(self, name):
        """No name outside the declared prefix set is ever treated as owned."""
        expected = any(name.startswith(p) for p in coa._PROJECT_JOB_NAME_PREFIXES)
        assert coa.is_project_glue_job(name) is expected


class TestGetAllGlueJobs:
    def test_the_real_deployed_fleet_yields_exactly_the_owned_jobs(self, fleet):
        """The regression test for defect A.

        Against the actual 44-job fleet the old filter returned 0. This asserts on
        the real names so the same mistake cannot pass again.
        """
        fleet(list(DEPLOYED_FLEET))

        jobs = coa.get_all_glue_jobs()

        assert [j["job_name"] for j in jobs] == list(IN_SCOPE_JOBS)
        assert len(DEPLOYED_FLEET) == 44

    def test_pagination_is_followed(self, fleet):
        names = list(DEPLOYED_FLEET)
        paginator = fleet(names, page_size=10)

        jobs = coa.get_all_glue_jobs()

        assert len(paginator.paginate.return_value) == 5
        assert len(jobs) == len(IN_SCOPE_JOBS)

    def test_config_is_carried_through(self, fleet):
        fleet([IN_SCOPE_JOBS[0]])

        job = coa.get_all_glue_jobs()[0]

        assert job["worker_type"] == "G.2X"
        assert job["number_of_workers"] == 8
        assert job["glue_version"] == "4.0"

    def test_pipeline_id_falls_back_to_job_name(self, fleet):
        fleet([IN_SCOPE_JOBS[0]])
        assert coa.get_all_glue_jobs()[0]["pipeline_id"] == IN_SCOPE_JOBS[0]

    def test_pipeline_id_comes_from_default_arguments_when_present(self):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Jobs": [_glue_job(IN_SCOPE_JOBS[0], pipeline_id="testubf-93fb4dcb")]}
        ]
        _mock_glue.get_paginator.return_value = paginator

        assert coa.get_all_glue_jobs()[0]["pipeline_id"] == "testubf-93fb4dcb"

    def test_a_fleet_of_only_out_of_scope_jobs_is_empty(self, fleet):
        fleet(list(LEGACY_AGENTGLUE_JOBS + FOREIGN_STRANDS_JOBS))
        assert coa.get_all_glue_jobs() == []


# ---------------------------------------------------------------------------
# Defect B — the empty fleet still writes a state row
# ---------------------------------------------------------------------------


class TestEmptyFleetWritesStateRow:
    """R1.3: a sweep leaves a ``cost_optimization`` row behind either way.

    "No recommendations" is a result. Returning early on an empty fleet made it
    look identical to an agent that had never been invoked, which is the exact
    condition the live verification is trying to distinguish.
    """

    @pytest.fixture
    def state_table(self, mock_dynamodb_tables, monkeypatch):
        table = mock_dynamodb_tables[STATE_TABLE_NAME]
        # Real boto3 inside the fixture's mock_aws() scope, so the resource is
        # intercepted by moto rather than being the import-time MagicMock.
        monkeypatch.setattr(coa, "_dynamodb", boto3.resource("dynamodb", region_name="us-east-1"))
        monkeypatch.setattr(coa, "_require_env", MagicMock(return_value=STATE_TABLE_NAME))
        return table

    @pytest.fixture(autouse=True)
    def _no_audit_and_no_jobs(self, monkeypatch):
        monkeypatch.setattr(coa, "write_agent_audit_entry", MagicMock())
        monkeypatch.setattr(coa, "get_all_glue_jobs", MagicMock(return_value=[]))
        monkeypatch.delenv("AUTO_APPLY_ENABLED", raising=False)
        _mock_glue.reset_mock()
        yield
        _mock_glue.reset_mock()

    def _row(self, table):
        resp = table.scan(
            FilterExpression="step_name = :sn",
            ExpressionAttributeValues={":sn": "cost_optimization"},
        )
        return resp["Items"]

    def test_row_is_written_with_an_empty_recommendations_list(self, state_table):
        report = _call_handler({"trigger": "weekly_schedule"})

        rows = self._row(state_table)
        assert len(rows) == 1
        row = rows[0]
        assert row["step_name"] == "cost_optimization"
        assert row["agent_type"] == "cost_optimization"
        assert row["recommendations"] == []
        assert row["total_projected_savings_monthly"] == Decimal("0")
        assert row["jobs_scanned"] == 0
        assert row["auto_applied_count"] == 0
        assert row["run_id"] == report["run_id"]

    def test_timestamp_is_an_iso_string_not_a_number(self, state_table):
        """``timestamp`` is the PipelineIndex GSI range key, typed S.

        An int is rejected with "Type mismatch for Index Key timestamp Expected:
        S Actual: N", so this pins the type rather than just the presence.
        """
        _call_handler({"trigger": "weekly_schedule"})

        ts = self._row(state_table)[0]["timestamp"]
        assert isinstance(ts, str)
        assert ts.startswith("20")
        assert "T" in ts

    def test_returned_report_matches_the_row(self, state_table):
        report = _call_handler({"trigger": "weekly_schedule"})

        assert report["recommendations"] == []
        assert report["total_projected_savings_monthly"] == 0.0
        assert report["jobs_scanned"] == 0
        assert report["auto_applied_count"] == 0
        assert "generated_at" in report

    def test_empty_fleet_mutates_no_glue_job(self, state_table):
        _call_handler({"trigger": "weekly_schedule"})

        _mock_glue.update_job.assert_not_called()

    def test_bare_and_none_payloads_also_write_a_row(self, state_table):
        _call_handler({})
        _call_handler(None)

        assert len(self._row(state_table)) == 2

    def test_apply_action_still_writes_no_state_row(self, state_table, monkeypatch):
        """The id-scoped path must not write one — it would blank the panel.

        The dashboard reads the LATEST ``cost_optimization`` row, so an apply
        result written there would replace the recommendation report (task 1.6).
        """
        _call_handler({"action": "apply_recommendation", "recommendation_id": "nope"})

        assert self._row(state_table) == []
