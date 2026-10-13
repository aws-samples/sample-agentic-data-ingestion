# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for cost optimization agent: over/under provisioning, idle detection, savings."""

import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "cost_optimization_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

_mock_glue = MagicMock()
_mock_cw = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.side_effect = lambda svc, **kw: {
    "glue": _mock_glue,
    "cloudwatch": _mock_cw,
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
    import main as coa_main

    coa_main._glue_client = _mock_glue
    coa_main._cloudwatch = _mock_cw


# ── detect_over_provisioned ─────────────────────────────────────────


class TestDetectOverProvisioned:
    def _make_job(self, name="agentic-data-ingestion-etl-1", worker_type="G.2X", workers=10):
        return {
            "job_name": name,
            "pipeline_id": "pipe-1",
            "worker_type": worker_type,
            "number_of_workers": workers,
        }

    def test_low_utilization_recommends_downsize(self):
        jobs = [self._make_job()]
        metrics = {
            "agentic-data-ingestion-etl-1": {
                "run_count": 10,
                "total_dpu_hours": 5.0,
                "avg_execution_time_sec": 300,
            }
        }
        recs = coa_main.detect_over_provisioned(jobs, metrics, threshold=0.4)
        assert len(recs) == 1
        assert recs[0]["type"] == "downsize"
        assert recs[0]["recommended"]["worker_type"] == "G.1X"

    def test_high_utilization_no_recommendation(self):
        jobs = [self._make_job()]
        metrics = {
            "agentic-data-ingestion-etl-1": {
                "run_count": 10,
                "total_dpu_hours": 100.0,
                "avg_execution_time_sec": 600,
            }
        }
        recs = coa_main.detect_over_provisioned(jobs, metrics, threshold=0.4)
        assert len(recs) == 0

    def test_no_runs_skipped(self):
        jobs = [self._make_job()]
        metrics = {"agentic-data-ingestion-etl-1": {"run_count": 0, "total_dpu_hours": 0, "avg_execution_time_sec": 0}}
        recs = coa_main.detect_over_provisioned(jobs, metrics)
        assert len(recs) == 0


# ── detect_under_provisioned ────────────────────────────────────────


class TestDetectUnderProvisioned:
    def _make_job(self, name="agentic-data-ingestion-etl-2", worker_type="G.1X", workers=5):
        return {
            "job_name": name,
            "pipeline_id": "pipe-2",
            "worker_type": worker_type,
            "number_of_workers": workers,
        }

    def test_oom_failures_recommend_upgrade(self):
        jobs = [self._make_job()]
        metrics = {
            "agentic-data-ingestion-etl-2": {
                "run_count": 5,
                "avg_execution_time_sec": 300,
                "oom_failure_count": 2,
            }
        }
        recs = coa_main.detect_under_provisioned(jobs, metrics)
        assert len(recs) == 1
        assert recs[0]["type"] == "upsize"
        assert recs[0]["recommended"]["worker_type"] == "G.2X"

    def test_long_execution_recommends_upgrade(self):
        jobs = [self._make_job()]
        metrics = {
            "agentic-data-ingestion-etl-2": {
                "run_count": 5,
                "avg_execution_time_sec": 1500,  # > 2x baseline of 600
                "oom_failure_count": 0,
            }
        }
        recs = coa_main.detect_under_provisioned(jobs, metrics)
        assert len(recs) == 1
        assert recs[0]["type"] == "upsize"

    def test_normal_execution_no_recommendation(self):
        jobs = [self._make_job()]
        metrics = {
            "agentic-data-ingestion-etl-2": {
                "run_count": 5,
                "avg_execution_time_sec": 300,
                "oom_failure_count": 0,
            }
        }
        recs = coa_main.detect_under_provisioned(jobs, metrics)
        assert len(recs) == 0


# ── detect_idle_pipelines ───────────────────────────────────────────


class TestDetectIdlePipelines:
    def _make_job(self, name="agentic-data-ingestion-etl-3"):
        return {"job_name": name, "pipeline_id": "pipe-3"}

    def test_no_runs_detected_as_idle(self):
        _mock_glue.get_job_runs.return_value = {"JobRuns": []}
        jobs = [self._make_job()]
        recs = coa_main.detect_idle_pipelines(jobs, days=30)
        assert len(recs) == 1
        assert recs[0]["type"] == "idle"

    def test_recent_run_not_idle(self):
        recent = datetime.now(UTC) - timedelta(days=5)
        _mock_glue.get_job_runs.return_value = {"JobRuns": [{"StartedOn": recent}]}
        jobs = [self._make_job()]
        recs = coa_main.detect_idle_pipelines(jobs, days=30)
        assert len(recs) == 0

    def test_old_run_detected_as_idle(self):
        old = datetime.now(UTC) - timedelta(days=60)
        _mock_glue.get_job_runs.return_value = {"JobRuns": [{"StartedOn": old}]}
        jobs = [self._make_job()]
        recs = coa_main.detect_idle_pipelines(jobs, days=30)
        assert len(recs) == 1
        assert recs[0]["idle_days"] >= 30


# ── calculate_savings ───────────────────────────────────────────────


class TestCalculateSavings:
    def test_downsize_calculates_positive_savings(self):
        recs = [
            {
                "type": "downsize",
                "job_name": "agentic-data-ingestion-etl-1",
                "current": {"worker_type": "G.2X", "workers": 10},
                "recommended": {"worker_type": "G.1X", "workers": 5},
            }
        ]
        jobs_by_name = {"agentic-data-ingestion-etl-1": {"worker_type": "G.2X", "number_of_workers": 10}}
        metrics = {"agentic-data-ingestion-etl-1": {"run_count": 10, "avg_execution_time_sec": 600}}
        total = coa_main.calculate_savings(recs, jobs_by_name, metrics)
        assert total >= 0
        assert "projected_savings_monthly" in recs[0]

    def test_idle_has_zero_savings(self):
        recs = [{"type": "idle", "job_name": "agentic-data-ingestion-etl-3"}]
        total = coa_main.calculate_savings(recs, {}, {})
        assert total == 0.0

    def test_idempotent_savings(self):
        recs = [
            {
                "type": "downsize",
                "job_name": "j1",
                "current": {"worker_type": "G.2X", "workers": 10},
                "recommended": {"worker_type": "G.1X", "workers": 5},
            }
        ]
        jobs = {"j1": {"worker_type": "G.2X", "number_of_workers": 10}}
        metrics = {"j1": {"run_count": 7, "avg_execution_time_sec": 300}}
        t1 = coa_main.calculate_savings(recs, jobs, metrics)
        # Reset projected_savings_monthly for second call
        del recs[0]["projected_savings_monthly"]
        t2 = coa_main.calculate_savings(recs, jobs, metrics)
        assert t1 == t2
