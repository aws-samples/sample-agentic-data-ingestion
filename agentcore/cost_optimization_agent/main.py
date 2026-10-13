# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""AgentCore Runtime entrypoint for the cost optimization agent.

Two modes, selected by the ``action`` field of the invocation payload:

* **Fleet sweep** (no ``action``, e.g. the weekly schedule's
  ``{"trigger": "weekly_schedule"}``). Scans all AgenticDataIngestion Glue jobs,
  queries CloudWatch for DPU utilization and execution time metrics, detects
  over-provisioned / under-provisioned / idle pipelines, calculates projected
  savings, and optionally auto-applies low-risk changes. Writes a
  ``step_name="cost_optimization"`` state row plus an audit entry, which is what
  the dashboard's Cost Recommendations panel reads.
* **Single apply** (``{"action": "apply_recommendation", "recommendation_id":
  ...}``, sent by ``POST /api/cost-recommendations/{id}/apply`` via the agent
  dispatch Lambda). Loads that one recommendation out of the latest
  ``cost_optimization`` state row and applies only it — p30a R1.4. Before that
  change, Apply on a single recommendation rescanned the entire fleet under
  whatever auto-apply posture was configured, which is a blast-radius problem
  rather than a cosmetic one.

An unrecognised ``action`` is refused rather than falling through to the sweep,
and so is a ``recommendation_id`` that is not in the latest row. Both would
otherwise turn a one-job request into a fleet-wide operation, which is exactly
what R1.4 removes.
"""

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", force=True)
for name in list(logging.Logger.manager.loggerDict):
    logging.getLogger(name).propagate = True

sys.path.insert(0, "/app/shared")

from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Imported at module scope rather than lazily inside the entrypoint: every path
# through this agent writes an audit entry, so a missing dependency should fail
# at container start, not part-way through a run that has already mutated a job.
from audit_utils import write_agent_audit_entry

# p30a R1.4: the dashboard's per-recommendation id is synthesised, not stored.
# Both the read path (handlers/agents.py::get_cost_recommendations) and this
# agent's apply branch derive it from the same shared module so they cannot
# disagree about which recommendation an id names.
from cost_recommendation_ids import derive_recommendation_ids, find_recommendation_by_id

# P28 R19.2: single shared float→Decimal converter (replaces the local
# ``_to_decimal`` copy). Resolves from /app/shared in the container and from
# the repo's shared/ dir in tests.
from decimal_utils import convert_floats_to_decimal

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()


# ---------------------------------------------------------------------------
# Logging filter — injects context into every log record
# ---------------------------------------------------------------------------


class _RunIdFilter(logging.Filter):
    """Injects run_id into every log record so container logs are searchable."""

    run_id = ""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        return True


_run_filter = _RunIdFilter()

# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------

import boto3

_cloudwatch = boto3.client("cloudwatch")
_glue_client = boto3.client("glue")
_dynamodb = boto3.resource("dynamodb")


def _require_env(name: str, default: str | None = None) -> str:
    """Thin wrapper — import shared env_utils at call time."""
    from env_utils import require_env
    return require_env(name, default)


# ---------------------------------------------------------------------------
# Pricing constants (on-demand $/DPU-hour by worker type)
# ---------------------------------------------------------------------------

_DPU_HOUR_PRICE: dict[str, float] = {
    "G.1X": 0.44,
    "G.2X": 0.44,
    "G.4X": 0.44,
    "G.8X": 0.44,
    "Standard": 0.44,
}

# DPU multiplier per worker type
_DPU_PER_WORKER: dict[str, int] = {
    "G.1X": 1,
    "G.2X": 2,
    "G.4X": 4,
    "G.8X": 8,
    "Standard": 1,
}

# Worker type downgrade path
_WORKER_DOWNGRADE: dict[str, str] = {
    "G.8X": "G.4X",
    "G.4X": "G.2X",
    "G.2X": "G.1X",
}

# Worker type upgrade path
_WORKER_UPGRADE: dict[str, str] = {
    "G.1X": "G.2X",
    "G.2X": "G.4X",
    "G.4X": "G.8X",
}


# ---------------------------------------------------------------------------
# Task 4.2 — CloudWatch metrics query + Glue job listing
# ---------------------------------------------------------------------------

#: Glue job-name prefixes that mean "this project created this job".
#:
#: The authority for this list is the code that CREATES Glue jobs, not the set of
#: jobs that happen to exist in some account:
#:
#: * ``create_glue_pipeline/job_builders.py:442`` — ``agentic-data-ingestion-job-<pipeline_id>``
#: * ``create_glue_pipeline/schema_evolution.py:299`` — the ``-v<N>`` versioned
#:   form of that same name
#: * ``deploy-cdk/lib/orchestration-stack.ts:32`` — declares the identical string
#:   as ``GLUE_JOB_NAME_PREFIX`` and scopes this project's EventBridge job-state
#:   rules to it (P28 R5.4)
#:
#: The snake_case form is retained because the AgentCore runtime role grants Glue
#: on BOTH ``job/agentic_data_ingestion*`` and ``job/agentic-data-ingestion-*``
#: (``foundation-stack.ts:188``, added by 2b45b72 after a real ``glue:GetJob``
#: AccessDenied). A deployment naming jobs snake_case is therefore inside the
#: permission boundary, and silently skipping it would be the worse failure. No
#: current code path emits that form.
#:
#: DELIBERATELY EXCLUDED — both families exist in the dev account today:
#:
#: * ``agentglue-job-*`` (28 jobs, created 2025-11-07 … 2026-07-22). The SAME
#:   creation code, from before commit 2b45b72 renamed the project from AgentGlue
#:   to Agentic Data Ingestion: their ``Description`` is still "AgentGlue ETL job
#:   for pipeline <id>", the pre-rename form of the template at
#:   ``job_builders.py:495``. So they are legacy-owned rather than foreign — but
#:   they sit OUTSIDE the runtime role's Glue grant, so ``GetJobRuns`` and
#:   ``UpdateJob`` against them are AccessDenied. Sweeping them would yield 28
#:   permission warnings and no usable metrics, and because a recommendation is
#:   what a later Apply acts on, it would widen the mutation surface to jobs this
#:   project no longer creates. Re-including them is an IAM decision first and a
#:   filter change second — do not do it here alone.
#: * ``strands-transform-*`` (14 jobs, all 2025-08-22). Not created by anything in
#:   this repository: no source file emits their ``Description`` template
#:   ("Strands-generated transformation job - <ts>"), and their names carry a
#:   timestamp where every job this project creates carries a pipeline_id.
#:   Ownership is unattributable, so they stay out — the safe direction.
_PROJECT_JOB_NAME_PREFIXES: tuple[str, ...] = (
    "agentic-data-ingestion-job-",
    "agentic_data_ingestion",
)


def is_project_glue_job(job_name: str) -> bool:
    """True when *job_name* is a Glue job this project owns.

    A PREFIX match against ``_PROJECT_JOB_NAME_PREFIXES``, deliberately not a
    substring test: ``"agentic-data-ingestion-job-" in job_name`` would also
    accept somebody else's job that merely mentions this project, and whatever
    this sweep returns is what a later Apply is allowed to mutate.
    """
    return job_name.startswith(_PROJECT_JOB_NAME_PREFIXES)


def get_all_glue_jobs() -> list[dict[str, Any]]:
    """List this project's Glue jobs and their configurations.

    Ownership is decided by :func:`is_project_glue_job` — see
    ``_PROJECT_JOB_NAME_PREFIXES`` for which naming families are in scope and why
    the two others present in the dev account are not.

    Returns a list of dicts with job_name, worker_type, number_of_workers,
    max_capacity, and the pipeline_id extracted from default arguments.
    """
    jobs: list[dict[str, Any]] = []
    jobs_seen = 0
    paginator = _glue_client.get_paginator("get_jobs")

    for page in paginator.paginate():
        for job in page.get("Jobs", []):
            jobs_seen += 1
            job_name = job.get("Name", "")
            if not is_project_glue_job(job_name):
                continue

            default_args = job.get("DefaultArguments", {})
            pipeline_id = default_args.get("--pipeline_id", job_name)

            jobs.append({
                "job_name": job_name,
                "pipeline_id": pipeline_id,
                "worker_type": job.get("WorkerType", "Standard"),
                "number_of_workers": job.get("NumberOfWorkers", 2),
                "max_capacity": job.get("MaxCapacity"),
                "glue_version": job.get("GlueVersion", "4.0"),
                "default_arguments": default_args,
            })

    # Both counts, not just the match count: the original log line reported only
    # "Found 0 jobs", which reads as an empty account rather than as a filter that
    # matches nothing. 0-of-44 is the shape of a prefix bug and should say so.
    logger.info(
        "Found %d project Glue jobs (%d jobs examined; ownership by prefix %s)",
        len(jobs),
        jobs_seen,
        ", ".join(_PROJECT_JOB_NAME_PREFIXES),
    )
    return jobs


def query_glue_job_metrics(
    job_names: list[str],
    days: int = 7,
) -> dict[str, dict[str, Any]]:
    """Query CloudWatch for DPU-hours and execution time per Glue job.

    Uses GetMetricData to batch-query ``glue.driver.aggregate.elapsedTime``
    and ``glue.driver.aggregate.numCompletedTasks`` for each job over the
    last *days* days.

    Returns a dict keyed by job_name with metrics:
        - avg_execution_time_sec: average execution time in seconds
        - total_dpu_hours: total DPU-hours consumed
        - run_count: number of runs in the period
        - max_execution_time_sec: longest run
    """
    if not job_names:
        return {}

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    # Build metric queries — one per job for execution time
    metric_queries: list[dict[str, Any]] = []
    for idx, job_name in enumerate(job_names):
        # Elapsed time metric (milliseconds)
        metric_queries.append({
            "Id": f"exec_{idx}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "Glue",
                    "MetricName": "glue.driver.aggregate.elapsedTime",
                    "Dimensions": [
                        {"Name": "JobName", "Value": job_name},
                        {"Name": "JobRunId", "Value": "ALL"},
                        {"Name": "Type", "Value": "gauge"},
                    ],
                },
                "Period": 86400,  # 1 day
                "Stat": "Average",
            },
        })

    results: dict[str, dict[str, Any]] = {
        name: {
            "avg_execution_time_sec": 0.0,
            "total_dpu_hours": 0.0,
            "run_count": 0,
            "max_execution_time_sec": 0.0,
        }
        for name in job_names
    }

    # CloudWatch GetMetricData supports max 500 queries per call
    batch_size = 500
    for batch_start in range(0, len(metric_queries), batch_size):
        batch = metric_queries[batch_start : batch_start + batch_size]
        try:
            response = _cloudwatch.get_metric_data(
                MetricDataQueries=batch,
                StartTime=start_time,
                EndTime=end_time,
            )
            for metric_result in response.get("MetricDataResults", []):
                metric_id = metric_result["Id"]
                values = metric_result.get("Values", [])
                if not values:
                    continue

                # Parse index from metric id (exec_0, exec_1, ...)
                idx = int(metric_id.split("_")[1])
                job_name = job_names[idx]

                avg_ms = sum(values) / len(values)
                results[job_name]["avg_execution_time_sec"] = round(avg_ms / 1000, 2)
                results[job_name]["max_execution_time_sec"] = round(max(values) / 1000, 2)
                results[job_name]["run_count"] = len(values)

        except Exception as e:
            logger.error("CloudWatch GetMetricData failed: %s", e)

    # Supplement with Glue GetJobRuns for accurate DPU-hours
    for job_name in job_names:
        try:
            runs_resp = _glue_client.get_job_runs(
                JobName=job_name,
                MaxResults=100,
            )
            runs = runs_resp.get("JobRuns", [])

            # Filter to runs within the time window
            recent_runs = [
                r for r in runs
                if r.get("StartedOn") and r["StartedOn"].replace(tzinfo=timezone.utc) >= start_time
            ]

            if recent_runs:
                total_dpu_seconds = sum(
                    r.get("DPUSeconds", 0) for r in recent_runs
                )
                results[job_name]["total_dpu_hours"] = round(total_dpu_seconds / 3600, 2)
                results[job_name]["run_count"] = max(
                    results[job_name]["run_count"], len(recent_runs)
                )

                # Check for OOM failures
                oom_runs = [
                    r for r in recent_runs
                    if r.get("JobRunState") == "FAILED"
                    and r.get("ErrorMessage")
                    and ("OutOfMemoryError" in r["ErrorMessage"]
                         or "memory exceeded" in r["ErrorMessage"].lower())
                ]
                results[job_name]["oom_failure_count"] = len(oom_runs)

                # Track last run time for idle detection
                latest_run = max(recent_runs, key=lambda r: r.get("StartedOn", start_time))
                results[job_name]["last_run_time"] = latest_run["StartedOn"].isoformat()

        except Exception as e:
            logger.warning("Failed to get job runs for %s: %s", job_name, e)

    logger.info("Queried metrics for %d jobs over %d days", len(job_names), days)
    return results


# ---------------------------------------------------------------------------
# Task 4.3 — Over-provisioning detection
# ---------------------------------------------------------------------------


def detect_over_provisioned(
    jobs: list[dict[str, Any]],
    job_metrics: dict[str, dict[str, Any]],
    threshold: float = 0.4,
) -> list[dict[str, Any]]:
    """Detect over-provisioned Glue jobs where DPU utilization < threshold.

    Compares actual DPU-hours consumed against provisioned capacity to
    compute utilization. Jobs below the threshold get a downsize
    recommendation.

    Returns a list of recommendation dicts.
    """
    recommendations: list[dict[str, Any]] = []

    for job in jobs:
        job_name = job["job_name"]
        metrics = job_metrics.get(job_name, {})

        run_count = metrics.get("run_count", 0)
        if run_count == 0:
            continue  # No runs — handled by idle detection

        total_dpu_hours = metrics.get("total_dpu_hours", 0.0)
        worker_type = job["worker_type"]
        num_workers = job["number_of_workers"]
        avg_exec_sec = metrics.get("avg_execution_time_sec", 0.0)

        # Calculate provisioned DPU-hours
        dpu_per_worker = _DPU_PER_WORKER.get(worker_type, 1)
        provisioned_dpu = num_workers * dpu_per_worker
        provisioned_dpu_hours = provisioned_dpu * (avg_exec_sec / 3600) * run_count

        if provisioned_dpu_hours <= 0:
            continue

        utilization = total_dpu_hours / provisioned_dpu_hours

        if utilization < threshold:
            # Recommend downgrade
            recommended_type = _WORKER_DOWNGRADE.get(worker_type, worker_type)
            recommended_workers = max(2, num_workers // 2)

            # If already at smallest type, just reduce workers
            if recommended_type == worker_type:
                recommended_workers = max(2, num_workers // 2)

            recommendations.append({
                "pipeline_id": job["pipeline_id"],
                "job_name": job_name,
                "type": "downsize",
                "current": {
                    "worker_type": worker_type,
                    "workers": num_workers,
                },
                "recommended": {
                    "worker_type": recommended_type,
                    "workers": recommended_workers,
                },
                "avg_dpu_utilization": round(utilization, 2),
                "run_count": run_count,
                "risk": "low",
                "auto_apply_eligible": True,
            })

            logger.info(
                "Over-provisioned: %s utilization=%.0f%% → recommend %s x%d",
                job_name,
                utilization * 100,
                recommended_type,
                recommended_workers,
            )

    return recommendations


# ---------------------------------------------------------------------------
# Task 4.4 — Under-provisioning detection
# ---------------------------------------------------------------------------

# Expected execution time per DPU-hour (baseline heuristic)
_EXPECTED_EXEC_TIME_SEC = 600  # 10 minutes baseline


def detect_under_provisioned(
    jobs: list[dict[str, Any]],
    job_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect under-provisioned Glue jobs.

    Flags jobs where:
    - Execution time > 2x the expected baseline, OR
    - OOM failures occurred in the last 7 days

    Returns a list of recommendation dicts suggesting upgrades.
    """
    recommendations: list[dict[str, Any]] = []

    for job in jobs:
        job_name = job["job_name"]
        metrics = job_metrics.get(job_name, {})

        run_count = metrics.get("run_count", 0)
        if run_count == 0:
            continue

        avg_exec_sec = metrics.get("avg_execution_time_sec", 0.0)
        oom_count = metrics.get("oom_failure_count", 0)
        worker_type = job["worker_type"]
        num_workers = job["number_of_workers"]

        reason = ""
        if oom_count > 0:
            reason = f"{oom_count} OOM failure(s) in last 7 days"
        elif avg_exec_sec > _EXPECTED_EXEC_TIME_SEC * 2:
            reason = (
                f"Avg execution time {avg_exec_sec:.0f}s exceeds "
                f"2x baseline ({_EXPECTED_EXEC_TIME_SEC * 2}s)"
            )
        else:
            continue

        # Recommend upgrade
        recommended_type = _WORKER_UPGRADE.get(worker_type, worker_type)
        recommended_workers = num_workers

        # If OOM, also increase worker count
        if oom_count > 0:
            recommended_workers = min(num_workers * 2, 100)

        # If already at max type, just increase workers
        if recommended_type == worker_type:
            recommended_workers = min(num_workers + 5, 100)

        recommendations.append({
            "pipeline_id": job["pipeline_id"],
            "job_name": job_name,
            "type": "upsize",
            "current": {
                "worker_type": worker_type,
                "workers": num_workers,
            },
            "recommended": {
                "worker_type": recommended_type,
                "workers": recommended_workers,
            },
            "reason": reason,
            "avg_execution_time_sec": avg_exec_sec,
            "oom_failure_count": oom_count,
            "risk": "medium",
            "auto_apply_eligible": False,
        })

        logger.info(
            "Under-provisioned: %s reason=%s → recommend %s x%d",
            job_name,
            reason,
            recommended_type,
            recommended_workers,
        )

    return recommendations


# ---------------------------------------------------------------------------
# Task 4.5 — Idle pipeline detection
# ---------------------------------------------------------------------------


def detect_idle_pipelines(
    jobs: list[dict[str, Any]],
    days: int = 30,
) -> list[dict[str, Any]]:
    """Detect pipelines with no runs in the last *days* days.

    Queries Glue GetJobRuns for each job and flags those with no recent
    executions. Returns a list of recommendation dicts.
    """
    recommendations: list[dict[str, Any]] = []

    for job in jobs:
        job_name = job["job_name"]

        try:
            runs_resp = _glue_client.get_job_runs(
                JobName=job_name,
                MaxResults=1,
            )
            runs = runs_resp.get("JobRuns", [])

            if not runs:
                # Never run
                last_run_str = "never"
                idle_days = days + 1
            else:
                last_run_time = runs[0].get("StartedOn")
                if last_run_time is None:
                    last_run_str = "unknown"
                    idle_days = days + 1
                else:
                    if last_run_time.tzinfo is None:
                        last_run_time = last_run_time.replace(tzinfo=timezone.utc)
                    last_run_str = last_run_time.isoformat()
                    idle_days = (datetime.now(timezone.utc) - last_run_time).days

            if idle_days >= days:
                recommendations.append({
                    "pipeline_id": job["pipeline_id"],
                    "job_name": job_name,
                    "type": "idle",
                    "last_run": last_run_str,
                    "idle_days": idle_days,
                    "recommendation": (
                        f"Archive pipeline — no runs in {idle_days} days"
                    ),
                    "risk": "low",
                    "auto_apply_eligible": False,
                })

                logger.info(
                    "Idle pipeline: %s last_run=%s idle_days=%d",
                    job_name,
                    last_run_str,
                    idle_days,
                )

        except Exception as e:
            logger.warning("Failed to check idle status for %s: %s", job_name, e)

    return recommendations


# ---------------------------------------------------------------------------
# Task 4.6 — Savings calculation and prioritized report
# ---------------------------------------------------------------------------


def calculate_savings(
    recommendations: list[dict[str, Any]],
    jobs_by_name: dict[str, dict[str, Any]],
    job_metrics: dict[str, dict[str, Any]],
) -> float:
    """Calculate projected monthly savings for each recommendation.

    Mutates each recommendation dict in-place to add
    ``projected_savings_monthly``. Returns the total projected savings.
    """
    total_savings = 0.0

    for rec in recommendations:
        rec_type = rec.get("type", "")
        job_name = rec.get("job_name", "")
        job = jobs_by_name.get(job_name, {})
        metrics = job_metrics.get(job_name, {})

        if rec_type == "downsize":
            # Current cost
            current_type = rec["current"]["worker_type"]
            current_workers = rec["current"]["workers"]
            current_dpu = current_workers * _DPU_PER_WORKER.get(current_type, 1)

            # Recommended cost
            rec_type_name = rec["recommended"]["worker_type"]
            rec_workers = rec["recommended"]["workers"]
            rec_dpu = rec_workers * _DPU_PER_WORKER.get(rec_type_name, 1)

            # Estimate monthly runs (extrapolate from 7-day data)
            run_count_7d = metrics.get("run_count", 0)
            monthly_runs = run_count_7d * (30 / 7)
            avg_exec_hours = metrics.get("avg_execution_time_sec", 0) / 3600

            current_monthly_cost = (
                current_dpu * avg_exec_hours * monthly_runs
                * _DPU_HOUR_PRICE.get(current_type, 0.44)
            )
            recommended_monthly_cost = (
                rec_dpu * avg_exec_hours * monthly_runs
                * _DPU_HOUR_PRICE.get(rec_type_name, 0.44)
            )

            savings = max(0.0, current_monthly_cost - recommended_monthly_cost)
            rec["projected_savings_monthly"] = round(savings, 2)
            total_savings += savings

        elif rec_type == "idle":
            # Idle pipelines: savings = current provisioned cost if disabled
            # Estimate based on last known run pattern (minimal)
            rec["projected_savings_monthly"] = 0.0

        elif rec_type == "upsize":
            # Upsizing costs more, so negative savings
            rec["projected_savings_monthly"] = 0.0

    return round(total_savings, 2)


def build_report(
    recommendations: list[dict[str, Any]],
    total_savings: float,
) -> dict[str, Any]:
    """Build the final prioritized recommendation report.

    Sorts recommendations by projected savings (descending) and returns
    the output shape expected by the dashboard.
    """
    # Sort: highest savings first, then by risk (low before medium/high)
    risk_order = {"low": 0, "medium": 1, "high": 2}
    sorted_recs = sorted(
        recommendations,
        key=lambda r: (
            -r.get("projected_savings_monthly", 0),
            risk_order.get(r.get("risk", "high"), 3),
        ),
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recommendations": sorted_recs,
        "total_projected_savings_monthly": total_savings,
    }


# ---------------------------------------------------------------------------
# Task 4.7 — Optional auto-apply for low-risk changes
# ---------------------------------------------------------------------------

#: Fields copied verbatim from the existing job definition into ``JobUpdate``.
#:
#: Glue's UpdateJob REPLACES the job definition with the JobUpdate it is given —
#: anything omitted is dropped, not merged. Carrying only Role/Command through
#: silently stripped a job's Glue ``Connections``, timeout, retry count and
#: bookmark/execution settings as a side effect of changing worker size. For a
#: JDBC pipeline that is fatal: the attached Glue connection is where Glue reads
#: ``SECRET_ID`` from at run time (LGR-819980f-2), and job wiring merges rather
#: than replaces connections precisely so they survive updates (LGR-2474614-1).
#:
#: A downsize must change worker type and count and nothing else, so everything
#: else the API will accept back is passed straight through. WorkerType and
#: NumberOfWorkers are deliberately absent — those are what we are changing.
#: MaxCapacity is deliberately absent too: Glue rejects it alongside WorkerType.
_JOB_UPDATE_PASSTHROUGH_FIELDS: tuple[str, ...] = (
    "Description",
    "LogUri",
    "ExecutionProperty",
    "DefaultArguments",
    "NonOverridableArguments",
    "Connections",
    "MaxRetries",
    "Timeout",
    "SecurityConfiguration",
    "NotificationProperty",
    "GlueVersion",
    "ExecutionClass",
)


def _auto_apply_enabled() -> bool:
    """True only when ``AUTO_APPLY_ENABLED`` is exactly ``"true"``.

    Comparison is case-insensitive and NOT trimmed, matching
    ``handlers/agents.py::apply_cost_recommendation`` exactly so the handler's
    response and this agent's behaviour cannot disagree. ``"1"``, ``"yes"`` and
    ``"TRUE "`` (trailing space) therefore all mean off, and an unset variable
    means off — which is the deployed state of the cost-optimization runtime
    (p30a design §5 O4), so the agent recommends and applies nothing.

    The handler checks the same flag from its OWN environment to keep the HTTP
    response honest; this check is the enforcement, because this process is the
    one holding permission to mutate a Glue job.
    """
    return os.environ.get("AUTO_APPLY_ENABLED", "false").lower() == "true"


def auto_apply_recommendation(recommendation: dict[str, Any]) -> dict[str, Any]:
    """Apply a low-risk recommendation automatically.

    Only applies if:
    - AUTO_APPLY_ENABLED env var is "true"
    - recommendation risk is "low"
    - recommendation auto_apply_eligible is True
    - recommendation type is "downsize"

    The single mutation is one Glue ``UpdateJob`` changing ``WorkerType`` and
    ``NumberOfWorkers`` on one job. No other AWS resource is touched.

    Returns a result dict with applied status and details.
    """
    if not _auto_apply_enabled():
        return {"applied": False, "reason": "Auto-apply is disabled"}

    if recommendation.get("risk") != "low":
        return {"applied": False, "reason": f"Risk level '{recommendation.get('risk')}' is not low"}

    if not recommendation.get("auto_apply_eligible"):
        return {"applied": False, "reason": "Recommendation is not auto-apply eligible"}

    rec_type = recommendation.get("type")
    if rec_type != "downsize":
        return {"applied": False, "reason": f"Auto-apply not supported for type '{rec_type}'"}

    job_name = recommendation.get("job_name", "")
    recommended = recommendation.get("recommended", {})
    new_worker_type = recommended.get("worker_type", "")
    # A recommendation read back out of DynamoDB carries Decimal, not int, and
    # boto3 rejects Decimal for NumberOfWorkers. The single-apply path (R1.4)
    # always arrives that way; the in-process sweep does not.
    new_workers = int(recommended.get("workers", 2))

    try:
        # Read current job config
        job_resp = _glue_client.get_job(JobName=job_name)
        job_def = job_resp["Job"]

        # Build update. Role and Command are required by UpdateJob; everything in
        # _JOB_UPDATE_PASSTHROUGH_FIELDS is carried over so this update changes
        # only worker size (see the constant's comment — UpdateJob replaces
        # rather than merges).
        job_update: dict[str, Any] = {
            "WorkerType": new_worker_type,
            "NumberOfWorkers": new_workers,
            "Role": job_def["Role"],
            "Command": job_def["Command"],
        }
        for field in _JOB_UPDATE_PASSTHROUGH_FIELDS:
            if field in job_def:
                job_update[field] = job_def[field]

        update_kwargs: dict[str, Any] = {"JobName": job_name, "JobUpdate": job_update}

        _glue_client.update_job(**update_kwargs)

        logger.info(
            "Auto-applied downsize for %s: %s x%d",
            job_name,
            new_worker_type,
            new_workers,
        )
        return {
            "applied": True,
            "job_name": job_name,
            "new_worker_type": new_worker_type,
            "new_workers": new_workers,
        }

    except Exception as e:
        logger.error("Auto-apply failed for %s: %s", job_name, e)
        return {"applied": False, "reason": str(e), "job_name": job_name}


# ---------------------------------------------------------------------------
# p30a R1.4 — id-scoped apply
# ---------------------------------------------------------------------------

#: Page guard on the state-table scan. See ``load_latest_cost_recommendations``.
_MAX_STATE_SCAN_PAGES = 100


def _state_table() -> Any:
    """Resolve the state table at call time, not import time."""
    return _dynamodb.Table(_require_env("STATE_TABLE", "agentic_data_ingestion_state"))


def load_latest_cost_recommendations() -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Load the recommendations list from the most recent cost_optimization row.

    Mirrors the read path in ``handlers/agents.py::get_cost_recommendations``
    deliberately, including the reason it looks the way it does:
    ``cost_optimization`` rows are a tiny fraction of the state table and
    DynamoDB's ``Limit`` caps items *evaluated*, not items *matched* by a filter,
    so a single Limit-capped scan almost never returns one. The scan is paginated
    with a bounded page guard instead.

    The ``step_name-timestamp-index`` GSI is not used on purpose: it is sparse by
    design (rows with no ``timestamp`` attribute never enter it), so querying it
    would look like an optimisation and quietly miss rows.

    Returns:
        tuple: ``(recommendations, row)``. ``row`` is ``None`` when no
        cost_optimization row exists at all, which is a different condition from
        a row that exists but holds an empty list.
    """
    table = _state_table()
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "step_name = :sn",
        "ExpressionAttributeValues": {":sn": "cost_optimization"},
    }
    for _ in range(_MAX_STATE_SCAN_PAGES):
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    items.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    if not items:
        return [], None

    latest = items[0]
    recommendations = list(latest.get("recommendations") or [])
    logger.info(
        "Loaded latest cost_optimization row: timestamp=%s recommendations=%d",
        latest.get("timestamp", ""),
        len(recommendations),
    )
    return recommendations, latest


def apply_single_recommendation(recommendation_id: str, run_id: str) -> dict[str, Any]:
    """Apply exactly the recommendation named by *recommendation_id* (R1.4).

    Every outcome — including every refusal — returns a dict and writes an audit
    entry. Nothing here falls back to a fleet sweep: an unknown id is an error,
    not a licence to rescan and change every job.

    No ``step_name="cost_optimization"`` state row is written. The dashboard read
    path takes the LATEST such row, so writing one here would replace a real
    recommendation report with a single-apply result and blank the panel. The
    audit log is the record of an apply.
    """
    auto_apply_enabled = _auto_apply_enabled()
    result: dict[str, Any] = {
        "action": "apply_recommendation",
        "recommendation_id": recommendation_id,
        "run_id": run_id,
        "applied": False,
        "auto_apply_enabled": auto_apply_enabled,
        "jobs_scanned": 0,
    }

    def _finish(details: dict[str, Any], pipeline_id: str = "system") -> dict[str, Any]:
        write_agent_audit_entry(
            agent_type="cost_optimization",
            action="apply_recommendation",
            resource_id=recommendation_id or "unknown",
            details=details,
            run_id=run_id,
            pipeline_id=pipeline_id,
        )
        return result

    # Enforcement gate first: a runtime that cannot change anything should not
    # even read the state table. AUTO_APPLY_ENABLED is unset on the deployed
    # runtime, so this is the live path.
    if not auto_apply_enabled:
        result["reason"] = (
            "AUTO_APPLY_ENABLED is not 'true' on the cost optimization agent runtime, "
            "so this recommendation was NOT applied and no Glue job was changed."
        )
        logger.info(
            "Apply refused for %s: AUTO_APPLY_ENABLED is not 'true' — nothing applied",
            recommendation_id or "<missing id>",
        )
        return _finish({"outcome": "auto_apply_disabled", "applied": False})

    if not recommendation_id:
        result["error"] = "missing_recommendation_id"
        result["reason"] = "No recommendation_id was supplied, so there is nothing to apply."
        logger.error("Apply refused: payload carried no recommendation_id")
        return _finish({"outcome": "missing_recommendation_id", "applied": False})

    recommendations, row = load_latest_cost_recommendations()
    if row is None:
        result["error"] = "no_recommendations_available"
        result["reason"] = (
            "No cost_optimization state row exists, so recommendation "
            f"'{recommendation_id}' cannot be resolved. Run a fleet sweep first."
        )
        logger.error("Apply refused for %s: no cost_optimization state row", recommendation_id)
        return _finish({"outcome": "no_recommendations_available", "applied": False})

    recommendation = find_recommendation_by_id(recommendations, recommendation_id)
    if recommendation is None:
        available = derive_recommendation_ids(recommendations)
        result["error"] = "recommendation_not_found"
        result["reason"] = (
            f"Recommendation '{recommendation_id}' is not in the latest cost_optimization "
            "report. Nothing was applied and the fleet was not rescanned."
        )
        result["available_recommendation_ids"] = available
        logger.error(
            "Apply refused for %s: not in latest report (%d available)",
            recommendation_id,
            len(available),
        )
        return _finish(
            {
                "outcome": "recommendation_not_found",
                "applied": False,
                "available_recommendation_count": len(available),
            }
        )

    target = dict(recommendation)
    apply_result = auto_apply_recommendation(target)
    result["applied"] = bool(apply_result.get("applied"))
    result["job_name"] = target.get("job_name", "")
    result["recommendation_type"] = target.get("type", "")
    result["apply_result"] = apply_result
    if not result["applied"]:
        result["reason"] = apply_result.get("reason", "Recommendation was not applied.")

    logger.info(
        "Apply for %s (job=%s type=%s): applied=%s",
        recommendation_id,
        result["job_name"],
        result["recommendation_type"],
        result["applied"],
    )
    return _finish(
        {
            "outcome": "applied" if result["applied"] else "not_applied",
            "applied": result["applied"],
            "job_name": result["job_name"],
            "recommendation_type": result["recommendation_type"],
            "reason": apply_result.get("reason", ""),
        },
        pipeline_id=str(target.get("pipeline_id") or "system"),
    )


# ---------------------------------------------------------------------------
# Entrypoint (Task 4.1)
# ---------------------------------------------------------------------------


#: ``action`` values that mean "do the full fleet sweep". The weekly schedule
#: sends ``{"trigger": "weekly_schedule"}`` with no ``action`` at all, and a bare
#: ``{}`` has always meant sweep, so the empty string is the sweep default.
_SWEEP_ACTIONS: frozenset[str] = frozenset({"", "weekly_scan", "weekly_schedule"})

#: The one action that applies a single recommendation instead (R1.4).
_APPLY_ACTION = "apply_recommendation"


@app.entrypoint
def handler(payload: dict) -> dict:
    """Cost optimization agent entrypoint.

    Dispatches on ``payload["action"]``:

    * absent / empty → full fleet sweep. This is the weekly schedule's path
      (``{"trigger": "weekly_schedule"}``), and it must stay a full sweep.
    * ``"apply_recommendation"`` → apply only the recommendation named by
      ``payload["recommendation_id"]`` (R1.4).
    * anything else → refused. Falling through to the sweep would let a typo in a
      caller's payload turn a targeted request into a fleet-wide operation.
    """
    start_time = time.time()
    run_id = f"cost-{uuid.uuid4().hex[:12]}"

    # Configure logging with run context
    _run_filter.run_id = run_id
    root = logging.getLogger()
    root.addFilter(_run_filter)
    for h in root.handlers:
        h.setFormatter(
            logging.Formatter(f"%(levelname)s [run_id={run_id}] %(name)s %(message)s")
        )

    logger.info("Cost optimization agent invoked: run_id=%s", run_id)

    # Set up trace plugin for observability
    from agent_trace_plugin import AgentTracePlugin

    trace_plugin = AgentTracePlugin(
        pipeline_id="system",
        run_id=run_id,
        step_name="cost_optimization",
    )

    # ── 0. Route on the payload (R1.4) ──────────────────────────
    # The payload used to be ignored entirely, which is why clicking Apply on one
    # recommendation rescanned every Glue job in the account.
    request = payload if isinstance(payload, dict) else {}
    action = str(request.get("action") or "").strip()

    if action == _APPLY_ACTION:
        apply_result = apply_single_recommendation(
            recommendation_id=str(request.get("recommendation_id") or "").strip(),
            run_id=run_id,
        )
        apply_result["total_duration_ms"] = round((time.time() - start_time) * 1000)
        trace_plugin.flush()
        return apply_result

    if action not in _SWEEP_ACTIONS:
        logger.error("Unsupported action %r — refusing rather than sweeping the fleet", action)
        trace_plugin.flush()
        return {
            "action": action,
            "run_id": run_id,
            "error": "unsupported_action",
            "reason": (
                f"Unsupported action '{action}'. Nothing was done — in particular the "
                "fleet was not rescanned."
            ),
            "applied": False,
            "jobs_scanned": 0,
            "total_duration_ms": round((time.time() - start_time) * 1000),
        }

    # ── 1. Discover all Glue jobs ───────────────────────────────
    #
    # An empty fleet is NOT an early return. It used to be — ``return
    # build_report([], 0.0)`` here skipped step 7 — so a sweep that found no jobs
    # wrote no ``cost_optimization`` state row at all, and the dashboard panel was
    # indistinguishable from an agent that had never run. R1.3 wants a sweep to
    # leave a row behind either way; "no recommendations" is a result, not a
    # non-event. Falling through costs nothing: ``query_glue_job_metrics`` returns
    # early on an empty name list, and every detector loops over an empty list, so
    # no CloudWatch or Glue call is made below on this path.
    jobs = get_all_glue_jobs()
    if not jobs:
        logger.info(
            "No project Glue jobs found — writing an empty cost_optimization report"
        )

    jobs_by_name = {j["job_name"]: j for j in jobs}
    job_names = list(jobs_by_name.keys())

    # ── 2. Query CloudWatch metrics ─────────────────────────────
    job_metrics = query_glue_job_metrics(job_names, days=7)

    # ── 3. Detect issues ────────────────────────────────────────
    all_recommendations: list[dict[str, Any]] = []

    over_provisioned = detect_over_provisioned(jobs, job_metrics)
    all_recommendations.extend(over_provisioned)
    logger.info("Over-provisioned recommendations: %d", len(over_provisioned))

    under_provisioned = detect_under_provisioned(jobs, job_metrics)
    all_recommendations.extend(under_provisioned)
    logger.info("Under-provisioned recommendations: %d", len(under_provisioned))

    idle = detect_idle_pipelines(jobs)
    all_recommendations.extend(idle)
    logger.info("Idle pipeline recommendations: %d", len(idle))

    # ── 4. Calculate savings ────────────────────────────────────
    total_savings = calculate_savings(
        all_recommendations, jobs_by_name, job_metrics,
    )
    logger.info("Total projected monthly savings: $%.2f", total_savings)

    # ── 5. Auto-apply low-risk changes ──────────────────────────
    auto_applied: list[dict[str, Any]] = []
    for rec in all_recommendations:
        result = auto_apply_recommendation(rec)
        if result.get("applied"):
            rec["auto_applied"] = True
            auto_applied.append(result)
        else:
            rec["auto_applied"] = False

    if auto_applied:
        logger.info("Auto-applied %d low-risk changes", len(auto_applied))

    # ── 6. Build report ─────────────────────────────────────────
    report = build_report(all_recommendations, total_savings)
    total_duration_ms = round((time.time() - start_time) * 1000)
    report["run_id"] = run_id
    report["total_duration_ms"] = total_duration_ms
    report["jobs_scanned"] = len(jobs)
    report["auto_applied_count"] = len(auto_applied)

    # ── 7. Store result in state table ──────────────────────────
    state_table = _dynamodb.Table(
        _require_env("STATE_TABLE", "agentic_data_ingestion_state")
    )
    state_item: dict[str, Any] = {
        "run_id": run_id,
        "step_name": "cost_optimization",
        "agent_type": "cost_optimization",
        # ISO-8601: ``timestamp`` is the RANGE key of the agentic_data_ingestion_state
        # PipelineIndex GSI (typed S); an int is rejected with
        # "Type mismatch for Index Key timestamp Expected: S Actual: N".
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **report,
    }
    # report carries float savings amounts → convert floats to Decimal.
    state_table.put_item(Item=convert_floats_to_decimal(state_item))

    # Write audit trail
    write_agent_audit_entry(
        agent_type="cost_optimization",
        action="weekly_scan",
        resource_id="system",
        details={
            "jobs_scanned": len(jobs),
            "recommendations": len(all_recommendations),
            "total_savings": total_savings,
            "auto_applied": len(auto_applied),
        },
        run_id=run_id,
        pipeline_id="system",
    )

    # Flush trace data
    trace_plugin.flush()

    logger.info(
        "Cost optimization complete: recommendations=%d savings=$%.2f total_ms=%d",
        len(all_recommendations),
        total_savings,
        total_duration_ms,
    )

    return report


if __name__ == "__main__":
    app.run()
