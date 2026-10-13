# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Metrics implementation for AgenticDataIngestion Pipeline Monitoring.

This module IS the metrics implementation for AgenticDataIngestion. It is built on
aws-lambda-powertools Metrics for EMF, plus custom batched PutMetricData and
probabilistic sampling, all exposed through the ``MetricsFacade`` class and the
shared ``metrics_facade`` instance.

The wrapper functions and classes defined below (``PipelineMetrics``,
``ApprovalMetrics``, ``DashboardMetrics``, ``CostMetrics``, ``time_function``,
``record_error``, the ``glue_metrics``-named ``publish_*`` helpers, etc.) are
backward-compatibility SHIMS retained for stable call sites. They simply
delegate to this module's ``metrics_facade`` implementation and are NOT
separate modules — the historical ``cloudwatch_metrics``, ``mcp_logger``,
``performance_tracker``, and ``glue_metrics`` modules no longer exist.

Requirements: P21 Req 3 (consolidation cleanup); historical: 9.1, 9.2, 9.3, 9.4, 9.5
"""

import logging
import os
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature-flag controlled import of aws-lambda-powertools Metrics
# ---------------------------------------------------------------------------
POWERTOOLS_ENABLED = os.environ.get("POWERTOOLS_ENABLED", "false").lower() == "true"

if POWERTOOLS_ENABLED:
    from aws_lambda_powertools.metrics import MetricUnit, single_metric

    POWERTOOLS_AVAILABLE = True
else:
    POWERTOOLS_AVAILABLE = False
    single_metric = None
    MetricUnit = None


# ---------------------------------------------------------------------------
# Unit mapping helper
# ---------------------------------------------------------------------------
_UNIT_MAP = {
    "Count": "Count",
    "Milliseconds": "Milliseconds",
    "Seconds": "Seconds",
    "Bytes": "Bytes",
    "Percent": "Percent",
    "None": "None",
    "Count/Second": "Count/Second",
}


# ---------------------------------------------------------------------------
# The ``service`` dimension — what keeps the two producers of one metric
# readable by one consumer (p39 task 13.2, R6.1/R6.3)
# ---------------------------------------------------------------------------
# Powertools stamps a ``service`` dimension onto every EMF metric it emits, so
# ``put_metric`` on a ``POWERTOOLS_ENABLED`` Lambda publishes
# ``{PipelineId, StageName, Status, service}`` while the same call in an
# AgentCore container — where ``agentContainerEnv`` sets no
# ``POWERTOOLS_ENABLED``, so ``put_metric`` falls through to
# ``put_direct_metric`` — published ``{PipelineId, StageName, Status}``.
#
# A CloudWatch SEARCH brace schema is an EXACT match on the dimension-NAME set,
# so one brace form cannot match both shapes. ``_stage_durations_cw``
# (``dashboard_api/handlers/metrics.py:1108``) names all four, and a verified
# read-only ``get-metric-data`` probe returned ZERO series for ``CodeGeneration``
# against 26 for ``SchemaExtract`` — the container's datapoints existed and were
# unreadable. Only the dimension NAME matters for matching; the value is not
# required to agree between the two paths, which is why the fallback below is a
# constant rather than a lookup.
_DEFAULT_SERVICE_NAME = "agentic_data_ingestion"


def _service_dimension_value() -> str:
    """The value Powertools would stamp on ``service`` in this environment.

    Read from the environment at CALL time, not at import time. ``POWERTOOLS_ENABLED``
    is resolved at import (``:38-46``) because it selects an import; this needs no
    import and reading it late keeps it testable without a module reload.

    ``POWERTOOLS_SERVICE_NAME`` is ``agentic_data_ingestion`` on every Lambda that
    sets it (``compute-stack.ts:888``, ``:895``, ``:916``;
    ``orchestration-stack.ts:1472``) and unset in every AgentCore container, so the
    fallback is that same string rather than Powertools' ``service_undefined``.
    """
    return os.environ.get("POWERTOOLS_SERVICE_NAME") or _DEFAULT_SERVICE_NAME


def _with_service_dimension(dimensions: dict[str, str] | None) -> dict[str, str]:
    """*dimensions* plus ``service``, as a new dict — the caller's is not mutated.

    An explicit ``service`` in *dimensions* wins: a caller naming it means it.
    """
    dims: dict[str, str] = dict(dimensions or {})
    dims.setdefault("service", _service_dimension_value())
    return dims


def _resolve_powertools_unit(unit: str):
    """Resolve a string unit name to a Powertools MetricUnit enum value."""
    if MetricUnit is None:
        return None
    mapping = {
        "Count": MetricUnit.Count,
        "Milliseconds": MetricUnit.Milliseconds,
        "Seconds": MetricUnit.Seconds,
        "Bytes": MetricUnit.Bytes,
        "Percent": MetricUnit.Percent,
        "None": MetricUnit.NoUnit,
        "Count/Second": MetricUnit.CountPerSecond,
    }
    return mapping.get(unit, MetricUnit.Count)


# ---------------------------------------------------------------------------
# Buffered metric data class
# ---------------------------------------------------------------------------
@dataclass
class BufferedMetric:
    """A metric buffered for direct PutMetricData submission."""

    metric_name: str
    value: float
    unit: str
    dimensions: dict[str, str] = field(default_factory=dict)
    timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# MetricsFacade
# ---------------------------------------------------------------------------
class MetricsFacade:
    """
    Unified metrics facade for AgenticDataIngestion.

    * EMF metrics via ``put_metric()`` — delegates to aws-lambda-powertools
      ``single_metric()`` context manager so each metric gets its own
      dimension set (avoids dimension bleed).
    * Direct PutMetricData via ``put_direct_metric()`` — buffers and flushes
      in batches of ``batch_size`` (default 20).
    * Probabilistic sampling via ``set_sample_rate()`` — metrics whose name
      starts with a registered prefix are randomly dropped according to the
      configured rate.
    """

    def __init__(
        self,
        namespace: str = "AgenticDataIngestion/Pipeline",
        batch_size: int = 20,
    ):
        self.namespace = namespace
        self._batch_size = batch_size
        self._sample_rates: dict[str, float] = {}
        self._batch_buffer: list[BufferedMetric] = []
        self._cloudwatch = None  # lazy

    # -- lazy CloudWatch client ------------------------------------------------

    def _get_cloudwatch(self):
        if self._cloudwatch is None:
            self._cloudwatch = boto3.client("cloudwatch")
        return self._cloudwatch

    # -- sampling --------------------------------------------------------------

    def set_sample_rate(self, metric_prefix: str, rate: float) -> None:
        """
        Set sampling rate (0.0–1.0) for metrics whose name starts with
        *metric_prefix*.  A rate of 1.0 means "publish every metric";
        0.1 means "publish ~10 %".
        """
        self._sample_rates[metric_prefix] = max(0.0, min(1.0, rate))

    def _should_sample(self, metric_name: str) -> bool:
        """Return True if this metric should be published (passes sampling)."""
        for prefix, rate in self._sample_rates.items():
            if metric_name.startswith(prefix):
                # Telemetry volume control, not a security decision: the draw
                # decides whether a CloudWatch datapoint is published, and
                # nothing downstream treats it as a secret, token or identifier.
                # Both suppressions are the same known-false finding under two
                # tools — ruff spells it S311, bandit B311, and bandit does not
                # honour `noqa` — so one without the other leaves the finding
                # reported by the other tool.
                return random.random() < rate  # noqa: S311 — sampling, not secrecy  # nosec B311
        return True  # no sampling configured → always publish

    # -- EMF metrics (Powertools) ----------------------------------------------

    def put_metric(
        self,
        name: str,
        value: float,
        unit: str = "Count",
        dimensions: dict[str, str] | None = None,
    ) -> None:
        """
        Publish a metric via Powertools EMF using ``single_metric()`` so
        each metric gets isolated dimensions (no dimension bleed).

        Where Powertools is unavailable this falls through to
        ``put_direct_metric`` with a ``service`` dimension added, so ONE metric
        published through ``put_metric`` carries the same dimension-NAME set
        whatever the value of ``POWERTOOLS_ENABLED``. Without it the direct
        fallback published a shape no consumer's brace schema matched — see
        ``_with_service_dimension`` (p39 R6.1/R6.3).
        """
        if not self._should_sample(name):
            return

        if not POWERTOOLS_AVAILABLE or single_metric is None:
            self.put_direct_metric(name, value, unit, _with_service_dimension(dimensions))
            return

        try:
            pt_unit = _resolve_powertools_unit(unit)
            with single_metric(
                name=name,
                unit=pt_unit,
                value=value,
                namespace=self.namespace,
                default_dimensions={},
            ) as metric:
                if dimensions:
                    for k, v in dimensions.items():
                        metric.add_dimension(name=k, value=str(v))
        except Exception as e:
            logger.error(f"EMF put_metric failed for {name}: {e}")

    # -- direct PutMetricData (batched) ----------------------------------------

    def put_direct_metric(
        self,
        name: str,
        value: float,
        unit: str = "Count",
        dimensions: dict[str, str] | None = None,
    ) -> None:
        """
        Buffer a metric for direct CloudWatch PutMetricData.
        Auto-flushes when the buffer reaches ``batch_size``.

        DO NOT add the ``service`` dimension here. It is added one level up, in
        ``put_metric``'s Powertools fallback, precisely so that it reaches only
        the metrics whose OTHER producer is the EMF path. The publishers that
        call this method directly — ``publish_glue_run_cost`` and its siblings —
        have no EMF twin, and their consumers' brace schemas already name their
        exact dimension sets: ``_COST_METRIC_SERIES``
        (``dashboard_api/handlers/metrics.py:1315``) matches
        ``{PipelineId, JobName, WorkerType, Environment}`` on
        ``GlueJob.RunCostEstimate``. Adding a fifth name here would empty the
        monthly-cost tile's read for every future emit — the failure that
        comment at ``metrics.py:1312-1313`` warns about, and the one
        ``test_metrics_facade_run_cost.py`` pins (R2.1).
        """
        if not self._should_sample(name):
            return

        self._batch_buffer.append(
            BufferedMetric(
                metric_name=name,
                value=value,
                unit=_UNIT_MAP.get(unit, unit),
                dimensions=dimensions or {},
                timestamp=datetime.now(UTC),
            )
        )

        if len(self._batch_buffer) >= self._batch_size:
            self.flush_direct()

        # Safety valve: auto-flush if buffer grows to 10× batch_size
        if len(self._batch_buffer) >= self._batch_size * 10:
            logger.warning("Metrics buffer overflow — auto-flushing")
            self.flush_direct()

    def flush_direct(self) -> None:
        """Flush buffered direct PutMetricData calls in batches of ``batch_size``."""
        if not self._batch_buffer:
            return

        cw = self._get_cloudwatch()
        buffer = self._batch_buffer
        self._batch_buffer = []

        for i in range(0, len(buffer), self._batch_size):
            batch = buffer[i : i + self._batch_size]
            metric_data = []
            for m in batch:
                entry: dict[str, Any] = {
                    "MetricName": m.metric_name,
                    "Value": m.value,
                    "Unit": m.unit,
                }
                if m.dimensions:
                    entry["Dimensions"] = [{"Name": k, "Value": str(v)} for k, v in m.dimensions.items()]
                if m.timestamp:
                    entry["Timestamp"] = m.timestamp
                metric_data.append(entry)

            try:
                cw.put_metric_data(Namespace=self.namespace, MetricData=metric_data)
                logger.debug(f"Flushed {len(metric_data)} direct metrics")
            except Exception as e:
                logger.error(f"Failed to flush direct metrics batch: {e}")

    # -- flush all -------------------------------------------------------------

    def flush(self) -> None:
        """
        Flush all pending metrics — the "flush everything" entry point.

        Only the direct PutMetricData buffer needs flushing, so this delegates
        to ``flush_direct()``. EMF metrics never accumulate here: this class
        holds no Powertools ``Metrics`` aggregator (it imports only
        ``single_metric`` and ``MetricUnit``), and ``put_metric()`` publishes
        through ``single_metric()``, a context manager that serialises and
        emits each metric when its ``with`` block exits. There is nothing on
        the EMF side left to flush.

        Fixed under P30 R16.1. This method previously called
        ``self._powertools_metrics.flush_metrics()`` — an attribute ``__init__``
        never assigns, left over from an earlier design — behind a guard that
        read the same attribute. The guard sat *outside* the ``try``, so every
        call raised ``AttributeError`` on both ``POWERTOOLS_ENABLED`` paths. It
        stayed dormant only because every production flush path calls
        ``flush_direct()`` directly. Do not reintroduce a Powertools branch here
        without also constructing the aggregator it flushes.
        """
        self.flush_direct()

    # -- convenience: timer context manager ------------------------------------

    @contextmanager
    def timer(self, metric_name: str, dimensions: dict[str, str] | None = None):
        """Context manager that publishes duration in milliseconds."""
        start = time.time()
        try:
            yield
            duration_ms = (time.time() - start) * 1000
            self.put_metric(metric_name, duration_ms, "Milliseconds", dimensions)
        except Exception:
            duration_ms = (time.time() - start) * 1000
            self.put_metric(f"{metric_name}.Error", duration_ms, "Milliseconds", dimensions)
            raise

    def timing_decorator(self, metric_name: str, dimensions: dict[str, str] | None = None):
        """Decorator that publishes function execution duration."""

        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                with self.timer(metric_name, dimensions):
                    return func(*args, **kwargs)

            return wrapper

        return decorator

    # -- buffer inspection (for testing) ---------------------------------------

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def buffer_length(self) -> int:
        return len(self._batch_buffer)


# ============================================================================
# Global facade instance
# ============================================================================
metrics_facade = MetricsFacade()


# ============================================================================
# Backward-compatibility shims (Requirement 9.5)
#
# These functions/classes are compatibility shims, NOT separate modules. They
# preserve the call signatures from the historical (now-deleted) metrics modules
# so Lambda functions can switch their import path without changing call sites.
# Every shim delegates to the ``metrics_facade`` implementation defined above.
# ============================================================================

# --- cloudwatch_metrics compatibility shims (delegating to this module) -----


def time_function(metric_name: str, dimensions: dict[str, str] | None = None):
    """Decorator to time function execution (was in cloudwatch_metrics)."""
    return metrics_facade.timing_decorator(metric_name, dimensions)


def record_error(error_type: str, function_name: str, error_message: str = None):
    """Record error occurrence (was in cloudwatch_metrics)."""
    dims = {"ErrorType": error_type, "FunctionName": function_name}
    metrics_facade.put_metric("Errors.Occurrence", 1, "Count", dims)
    if error_message:
        logger.error(f"Error in {function_name}: {error_type} - {error_message}")


def record_resource_utilization(resource_type: str, utilization_percent: float, resource_id: str = None):
    """Record resource utilization (was in cloudwatch_metrics)."""
    dims: dict[str, str] = {"ResourceType": resource_type}
    if resource_id:
        dims["ResourceId"] = resource_id
    metrics_facade.put_metric("ResourceUtilization", utilization_percent, "Percent", dims)


class ApprovalMetrics:
    """Backward-compatible approval metrics (was in cloudwatch_metrics)."""

    @staticmethod
    def record_approval_created(approval_type: str, pipeline_id: str):
        metrics_facade.put_metric(
            "ApprovalRequests.Created",
            1,
            "Count",
            {"ApprovalType": approval_type, "PipelineId": pipeline_id},
        )

    @staticmethod
    def record_approval_processed(approval_type: str, decision: str, response_time_ms: float, pipeline_id: str):
        dims = {
            "ApprovalType": approval_type,
            "Decision": decision,
            "PipelineId": pipeline_id,
        }
        metrics_facade.put_metric("ApprovalRequests.Processed", 1, "Count", dims)
        metrics_facade.put_metric("ApprovalRequests.ResponseTime", response_time_ms, "Milliseconds", dims)
        success_value = 1.0 if decision == "approved" else 0.0
        metrics_facade.put_metric("ApprovalRequests.SuccessRate", success_value, "None", dims)

    @staticmethod
    def record_approval_timeout(approval_type: str, pipeline_id: str, timeout_behavior: str):
        metrics_facade.put_metric(
            "ApprovalRequests.Timeout",
            1,
            "Count",
            {
                "ApprovalType": approval_type,
                "PipelineId": pipeline_id,
                "TimeoutBehavior": timeout_behavior,
            },
        )

    @staticmethod
    def record_notification_sent(approval_type: str, channel: str, success: bool):
        status = "Success" if success else "Failure"
        metrics_facade.put_metric(
            f"ApprovalNotifications.{channel}.{status}",
            1,
            "Count",
            {"ApprovalType": approval_type},
        )


class PipelineMetrics:
    """Backward-compatible pipeline metrics (was in cloudwatch_metrics)."""

    @staticmethod
    def record_stage_execution(
        stage_name: str,
        pipeline_id: str,
        status: str,
        duration_ms: float,
        lines_processed: int = 0,
        mcp_operations: int = 0,
    ):
        dims = {"StageName": stage_name, "PipelineId": pipeline_id, "Status": status}
        metrics_facade.put_metric("Pipeline.StageExecution", 1, "Count", dims)
        metrics_facade.put_metric("Pipeline.StageDuration", duration_ms, "Milliseconds", dims)
        if lines_processed > 0:
            metrics_facade.put_metric("Pipeline.LinesProcessed", lines_processed, "Count", dims)
        if mcp_operations > 0:
            metrics_facade.put_metric("Pipeline.MCPOperations", mcp_operations, "Count", dims)
        success_value = 1.0 if status == "completed" else 0.0
        metrics_facade.put_metric("Pipeline.StageSuccessRate", success_value, "None", dims)

        # Publish now rather than at buffer capacity (p39 C5, R6.1/R6.5/R6.6).
        # Where ``POWERTOOLS_ENABLED`` is unset — every AgentCore container, since
        # ``agentContainerEnv`` does not set it — ``put_metric`` falls through to
        # ``put_direct_metric``, which buffers and auto-flushes only at
        # ``batch_size`` (20). This method buffers three metrics per call, so the
        # threshold is never reached and the microVM is reclaimed with the buffer
        # still full: nothing published, nothing logged. Flushing here fixes every
        # caller at once instead of at each call site.
        #
        # Correct under either flag value: on the EMF path the direct buffer is
        # always empty and ``flush()`` issues no API call, pinned by
        # ``test_metrics_facade_flush.py::test_flush_with_empty_buffer_makes_no_api_call``.
        # ``flush()`` delegates to ``flush_direct()`` and gains no Powertools
        # branch — LGR-eee8ac8-7.
        try:
            metrics_facade.flush()
        except Exception:
            # Log, never raise: a metric emission must not fail the pipeline step
            # (R6.5, requirements constraint 8). The precedent this replaces is a
            # swallowing ``except`` that dropped the metric with no trace, so the
            # failure is recorded at WARNING with the traceback attached.
            logger.warning(
                "Failed to flush stage metrics for stage=%s pipeline=%s status=%s",
                stage_name,
                pipeline_id,
                status,
                exc_info=True,
            )

    @staticmethod
    def record_pipeline_execution(
        pipeline_id: str,
        status: str,
        total_duration_ms: float,
        total_lines: int,
        total_stages: int,
        failed_stages: int = 0,
    ):
        dims = {"PipelineId": pipeline_id, "Status": status}
        metrics_facade.put_metric("Pipeline.Execution", 1, "Count", dims)
        metrics_facade.put_metric("Pipeline.TotalDuration", total_duration_ms, "Milliseconds", dims)
        if total_duration_ms > 0:
            throughput = (total_lines * 1000) / total_duration_ms
            metrics_facade.put_metric("Pipeline.Throughput", throughput, "Count/Second", dims)
        error_rate = (failed_stages / total_stages) * 100 if total_stages > 0 else 0
        metrics_facade.put_metric("Pipeline.ErrorRate", error_rate, "Percent", dims)


class DashboardMetrics:
    """Backward-compatible dashboard metrics (was in cloudwatch_metrics)."""

    @staticmethod
    def record_api_request(endpoint: str, method: str, status_code: int, response_time_ms: float):
        dims = {"Endpoint": endpoint, "Method": method, "StatusCode": str(status_code)}
        metrics_facade.put_metric("Dashboard.APIRequests", 1, "Count", dims)
        metrics_facade.put_metric("Dashboard.APIResponseTime", response_time_ms, "Milliseconds", dims)
        success_value = 1.0 if 200 <= status_code < 300 else 0.0
        metrics_facade.put_metric("Dashboard.APISuccessRate", success_value, "None", dims)

    @staticmethod
    def record_dashboard_load(load_time_ms: float, user_agent: str = None):
        dims: dict[str, str] = {}
        if user_agent:
            if "Chrome" in user_agent:
                dims["Browser"] = "Chrome"
            elif "Firefox" in user_agent:
                dims["Browser"] = "Firefox"
            elif "Safari" in user_agent:
                dims["Browser"] = "Safari"
            else:
                dims["Browser"] = "Other"
        metrics_facade.put_metric("Dashboard.PageLoadTime", load_time_ms, "Milliseconds", dims)

    @staticmethod
    def record_real_time_update(update_type: str, latency_ms: float, success: bool):
        dims = {"UpdateType": update_type, "Status": "Success" if success else "Failure"}
        metrics_facade.put_metric("Dashboard.RealTimeUpdates", 1, "Count", dims)
        metrics_facade.put_metric("Dashboard.UpdateLatency", latency_ms, "Milliseconds", dims)


class CostMetrics:
    """Backward-compatible cost metrics (was in cloudwatch_metrics)."""

    @staticmethod
    def record_lambda_cost(function_name: str, duration_ms: float, memory_mb: int):
        gb_seconds = (memory_mb / 1024) * (duration_ms / 1000)
        cost_usd = gb_seconds * 0.0000166667
        dims = {"FunctionName": function_name, "MemoryMB": str(memory_mb)}
        metrics_facade.put_metric("Cost.Lambda.Duration", duration_ms, "Milliseconds", dims)
        metrics_facade.put_metric("Cost.Lambda.GBSeconds", gb_seconds, "None", dims)
        metrics_facade.put_metric("Cost.Lambda.EstimatedCost", cost_usd, "None", dims)

    @staticmethod
    def record_dynamodb_cost(table_name: str, read_units: int, write_units: int):
        read_cost = read_units * 0.00000025
        write_cost = write_units * 0.00000125
        total_cost = read_cost + write_cost
        dims = {"TableName": table_name}
        metrics_facade.put_metric("Cost.DynamoDB.ReadUnits", read_units, "Count", dims)
        metrics_facade.put_metric("Cost.DynamoDB.WriteUnits", write_units, "Count", dims)
        metrics_facade.put_metric("Cost.DynamoDB.EstimatedCost", total_cost, "None", dims)

    @staticmethod
    def record_mcp_operation_cost(operation_type: str, cost_usd: float, success: bool):
        dims = {"OperationType": operation_type, "Status": "Success" if success else "Failure"}
        metrics_facade.put_metric("Cost.MCP.Operations", 1, "Count", dims)
        metrics_facade.put_metric("Cost.MCP.EstimatedCost", cost_usd, "None", dims)


# --- glue_metrics compatibility shims (delegating to this module) -----------


def calculate_dpu_hours(worker_type: str, num_workers: int, duration_seconds: float) -> float:
    """Calculate DPU-hours for a Glue job execution (was in glue_metrics)."""
    dpu_map = {
        "G.025X": 0.25,
        "G.1X": 1,
        "G.2X": 2,
        "G.4X": 4,
        "G.8X": 8,
        "Z.2X": 2,
    }
    dpu_per_worker = dpu_map.get(worker_type, 1)
    duration_hours = duration_seconds / 3600
    return dpu_per_worker * num_workers * duration_hours


def publish_glue_job_metrics(
    pipeline_id: str,
    job_name: str,
    records_processed: int,
    execution_duration_seconds: float,
    estimated_cost: float,
    worker_type: str,
    num_workers: int,
    environment: str = "dev",
) -> None:
    """Publish Glue job execution metrics (was in glue_metrics)."""
    try:
        dims_base = {
            "PipelineId": pipeline_id,
            "JobName": job_name,
            "Environment": environment,
        }
        dims_worker = {**dims_base, "WorkerType": worker_type}

        rps = records_processed / execution_duration_seconds if execution_duration_seconds > 0 else 0
        metrics_facade.put_direct_metric("GlueJob.RecordsProcessedPerSecond", rps, "Count/Second", dims_worker)

        cpm = (estimated_cost / records_processed) * 1_000_000 if records_processed > 0 else 0
        metrics_facade.put_direct_metric("GlueJob.CostPerMillionRecords", cpm, "None", dims_worker)
        metrics_facade.put_direct_metric("GlueJob.ExecutionDuration", execution_duration_seconds, "Seconds", dims_base)
        metrics_facade.put_direct_metric("GlueJob.EstimatedCost", estimated_cost, "None", dims_base)

        dpu_hours = calculate_dpu_hours(worker_type, num_workers, execution_duration_seconds)
        metrics_facade.put_direct_metric("GlueJob.DPUHours", dpu_hours, "None", dims_worker)

        metrics_facade.flush_direct()
        logger.info(f"Published Glue job metrics for {job_name}")
    except Exception as e:
        logger.error(f"Error publishing Glue job metrics: {e}")


def publish_mcp_recommendation_accuracy(
    pipeline_id: str,
    job_name: str,
    recommended_workers: int,
    actual_workers_needed: int,
    recommended_duration: float,
    actual_duration: float,
    mcp_tools_used: list[str],
    environment: str = "dev",
) -> None:
    """Publish MCP recommendation accuracy metrics (was in glue_metrics)."""
    try:
        worker_accuracy = 1.0 - abs(recommended_workers - actual_workers_needed) / max(
            recommended_workers, actual_workers_needed
        )
        worker_accuracy = max(0.0, min(1.0, worker_accuracy))
        metrics_facade.put_direct_metric(
            "GlueJob.MCPRecommendationAccuracy",
            worker_accuracy,
            "None",
            {
                "PipelineId": pipeline_id,
                "JobName": job_name,
                "MetricType": "WorkerCount",
                "Environment": environment,
            },
        )

        if recommended_duration > 0 and actual_duration > 0:
            dur_acc = 1.0 - abs(recommended_duration - actual_duration) / max(recommended_duration, actual_duration)
            dur_acc = max(0.0, min(1.0, dur_acc))
            metrics_facade.put_direct_metric(
                "GlueJob.MCPRecommendationAccuracy",
                dur_acc,
                "None",
                {
                    "PipelineId": pipeline_id,
                    "JobName": job_name,
                    "MetricType": "Duration",
                    "Environment": environment,
                },
            )

        for tool in mcp_tools_used:
            metrics_facade.put_direct_metric(
                "MCP.ToolUsage",
                1,
                "Count",
                {"ToolName": tool, "Context": "GlueJobCreation", "Environment": environment},
            )

        metrics_facade.flush_direct()
        logger.info(f"Published MCP recommendation accuracy metrics for {job_name}")
    except Exception as e:
        logger.error(f"Error publishing MCP recommendation metrics: {e}")


def publish_glue_job_creation_metrics(
    pipeline_id: str,
    job_name: str,
    creation_success: bool,
    worker_type: str,
    num_workers: int,
    estimated_cost_per_run: float,
    mcp_enabled: bool,
    fallback_used: bool,
    environment: str = "dev",
) -> None:
    """Publish Glue job creation metrics (was in glue_metrics)."""
    try:
        metric_name = "GlueJob.Created" if creation_success else "GlueJob.CreationFailed"
        metrics_facade.put_direct_metric(
            metric_name,
            1,
            "Count",
            {"PipelineId": pipeline_id, "Environment": environment},
        )

        if mcp_enabled:
            metrics_facade.put_direct_metric(
                "MCP.Enabled",
                1,
                "Count",
                {"Context": "GlueJobCreation", "FallbackUsed": str(fallback_used), "Environment": environment},
            )

        if creation_success:
            metrics_facade.put_direct_metric(
                "GlueJob.WorkerConfiguration",
                num_workers,
                "Count",
                {"WorkerType": worker_type, "Environment": environment},
            )
            metrics_facade.put_direct_metric(
                "GlueJob.EstimatedCostPerRun",
                estimated_cost_per_run,
                "None",
                {"PipelineId": pipeline_id, "WorkerType": worker_type, "Environment": environment},
            )

        metrics_facade.flush_direct()
        logger.info(f"Published Glue job creation metrics for {job_name}")
    except Exception as e:
        logger.error(f"Error publishing Glue job creation metrics: {e}")


def publish_glue_job_state_change(
    pipeline_id: str,
    job_name: str,
    run_id: str,
    state: str,
    error_message: str | None = None,
    environment: str = "dev",
) -> None:
    """Publish Glue job state change metrics (was in glue_metrics)."""
    try:
        state_metric_map = {
            "SUCCEEDED": "GlueJob.Succeeded",
            "FAILED": "GlueJob.Failed",
            "TIMEOUT": "GlueJob.Timeout",
            "RUNNING": "GlueJob.Executed",
        }
        metric_name = state_metric_map.get(state, "GlueJob.StateChange")
        metrics_facade.put_direct_metric(
            metric_name,
            1,
            "Count",
            {"PipelineId": pipeline_id, "JobName": job_name, "State": state, "Environment": environment},
        )
        metrics_facade.flush_direct()
        logger.info(f"Published Glue job state change metric: {job_name} - {state}")
    except Exception as e:
        logger.error(f"Error publishing Glue job state change metrics: {e}")


def publish_glue_run_cost(
    pipeline_id: str,
    job_name: str,
    estimated_cost: float,
    worker_type: str,
    environment: str = "dev",
) -> None:
    """Publish the per-run Glue cost estimate for one completed run.

    One ``GlueJob.RunCostEstimate`` datapoint per Glue run that reached a
    terminal state, published from the completion callback. This is the metric
    the dashboard's monthly-cost tile sums (p39 R2.1).

    Distinct from ``GlueJob.EstimatedCostPerRun``, which
    ``publish_glue_job_creation_metrics`` emits once at **job creation** — one
    datapoint per job created, none per run. Summing that metric counted
    creations and priced them at the estimate; do not source a run-cost figure
    from it.

    *estimated_cost* is the DPU-minutes-derived estimate the caller already
    computed, including the 10-minute per-run minimum. Nothing in this system
    measures spend, so the unit is ``"None"`` rather than a currency claim.

    Best-effort: a publish failure is logged and never raised, so it cannot
    change the outcome of the run whose cost it records.
    """
    try:
        metrics_facade.put_direct_metric(
            "GlueJob.RunCostEstimate",
            estimated_cost,
            "None",
            {
                "PipelineId": pipeline_id,
                "JobName": job_name,
                "WorkerType": worker_type,
                "Environment": environment,
            },
        )
        metrics_facade.flush_direct()
        logger.info(f"Published Glue run cost estimate for {job_name}: {estimated_cost}")
    except Exception as e:
        logger.error(f"Error publishing Glue run cost estimate: {e}")


# ── Pipeline error rate (one datapoint per finished run) ─────────────────────
#
# The series PipelineErrorRateAlarm reads (deploy-cdk/lib/orchestration-stack.ts).
# Every finished run contributes one datapoint: 100 when it failed, 0 when it
# succeeded. The Average over a window is therefore the percentage of finished
# runs that failed, and a window with no finished run has no data.
#
# Two producers, one per outcome, so no run is counted twice:
#   * success: job_completion, after the SUCCEEDED callback (publish_run_outcome
#     below), on the post-callback anchor LGR-ba3dfab-3 requires;
#   * failure: record_failure, the RecordFailure state every caught failure
#     reaches, including GlueJobFailed. It has no shared layer, so it vendors
#     the namespace, name and dimension. Approval rejections are not failures
#     there, the same rule the metrics dashboard applies.
# tests/contracts/test_pipeline_error_rate_cross_language.py pins the producers
# and the alarm to these three values.
#
# Not to be confused with PipelineMetrics.record_pipeline_execution above, which
# has no caller and publishes a per-run share of failed STAGES under the same
# name with {PipelineId, Status}: a different series the alarm does not read.
RUN_ERROR_RATE_NAMESPACE = "AgenticDataIngestion/Pipeline"
RUN_ERROR_RATE_METRIC = "Pipeline.ErrorRate"
RUN_ERROR_RATE_DIMENSION = "Environment"


def publish_run_outcome(failed: bool, environment: str = "dev") -> None:
    """Publish one finished run's datapoint on the pipeline error-rate series.

    ``100`` for a failed run, ``0`` for a successful one, dimension
    ``{Environment}`` only. Published through the direct path, which adds no
    ``service`` dimension, because the alarm matches the dimension set exactly
    (LGR-ba3dfab-5). Best-effort: a failure is logged and never raised.
    """
    try:
        metrics_facade.put_direct_metric(
            RUN_ERROR_RATE_METRIC,
            100.0 if failed else 0.0,
            "Percent",
            {RUN_ERROR_RATE_DIMENSION: environment},
        )
        metrics_facade.flush_direct()
    except Exception as e:
        logger.error(f"Error publishing the pipeline error-rate datapoint: {e}")
