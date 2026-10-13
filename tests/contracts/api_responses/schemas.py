# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""JSON Schema definitions for all dashboard API endpoints.

Derived from the frontend TypeScript interfaces in dashboard-react/src/types.ts
(analysis-0.5-frontend-types.md). Used by contract tests to validate that
backend API responses match the shapes expected by the React dashboard.

Task 7.1 — Requirement 4.8
"""

# ---------------------------------------------------------------------------
# Pipeline (single item in GET /pipelines response)
# ---------------------------------------------------------------------------

PIPELINE_SCHEMA = {
    "type": "object",
    "required": [
        "pipeline_key",
        "pipeline_name",
        "status",
        "created_at",
        "updated_at",
    ],
    "properties": {
        "pipeline_key": {"type": "string"},
        "pipeline_id": {"type": "string"},
        "pipeline_name": {"type": "string"},
        "status": {"type": "string"},
        "source_schema": {"type": "string"},
        "target_schema": {"type": "string"},
        "target_schema_name": {"type": "string"},
        "created_at": {"type": "string"},
        "updated_at": {"type": "string"},
        "model_id": {"type": "string"},
        "ai_model": {"type": "string"},
        "target_type": {"type": "string"},
        "schema_version": {"type": ["number", "integer"]},
        "mapping_count": {"type": ["number", "integer"]},
        "has_mapping": {"type": "boolean"},
        "file_count": {"type": ["number", "integer"]},
        "pipeline_description": {"type": "string"},
        "all_files": {
            "type": "array",
            "items": {"$ref": "#/$defs/PipelineFile"},
        },
        "versions": {
            "type": "array",
            "items": {"$ref": "#/$defs/PipelineVersion"},
        },
        "schema_history": {
            "type": "array",
            "items": {"type": "object"},
        },
        "target_config": {"$ref": "#/$defs/TargetConfig"},
        "approval_config": {
            "type": "object",
            "properties": {
                "schema_mapping_enabled": {"type": "boolean"},
                "code_generation_enabled": {"type": "boolean"},
            },
        },
    },
    "$defs": {
        "PipelineFile": {
            "type": "object",
            "required": ["file_key"],
            "properties": {
                "file_key": {"type": "string"},
                "pipeline_key": {"type": "string"},
                "processed_at": {"type": "string"},
                "execution_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        "PipelineVersion": {
            "type": "object",
            "required": ["version_index", "status", "created_at"],
            "properties": {
                "version_index": {"type": ["number", "integer"]},
                "status": {"type": "string"},
                "created_at": {"type": "string"},
                "code_version_id": {"type": "string"},
            },
        },
        "TargetConfig": {
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "bucket_name": {"type": "string"},
                "table_name": {"type": "string"},
                "database": {"type": "string"},
                "table": {"type": "string"},
                "format": {"type": "string"},
                "write_mode": {"type": "string"},
                "merge_key": {"type": "string"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# PaginatedPipelines — GET /pipelines response wrapper
# ---------------------------------------------------------------------------

PAGINATED_PIPELINES_SCHEMA = {
    "type": "object",
    "required": ["pipelines", "total_count", "page", "page_size", "total_pages", "has_more"],
    "properties": {
        "pipelines": {
            "type": "array",
            "items": PIPELINE_SCHEMA,
        },
        "total_count": {"type": ["number", "integer"]},
        "page": {"type": ["number", "integer"]},
        "page_size": {"type": ["number", "integer"]},
        "total_pages": {"type": ["number", "integer"]},
        "has_more": {"type": "boolean"},
    },
}

# ---------------------------------------------------------------------------
# PipelineExecution — individual execution in GET /pipeline/{id}/executions
# ---------------------------------------------------------------------------

PIPELINE_EXECUTION_SCHEMA = {
    "type": "object",
    "required": ["pipeline_id", "run_id", "status", "started_at"],
    "properties": {
        "pipeline_id": {"type": "string"},
        "run_id": {"type": "string"},
        "execution_id": {"type": "string"},
        "status": {"type": "string"},
        "started_at": {"type": "string"},
        "completed_at": {"type": ["string", "null"]},
        "current_step": {"type": "string"},
        "file_name": {"type": "string"},
        "file_key": {"type": "string"},
        "ai_model": {"type": "string"},
        "ai_model_used": {"type": "string"},
        "schema_version": {"type": ["number", "integer"]},
        "steps_status": {"type": "object"},
        "pending_approval_id": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_name": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# PaginatedExecutions — GET /pipeline/{id}/executions response wrapper
# ---------------------------------------------------------------------------

PAGINATED_EXECUTIONS_SCHEMA = {
    "type": "object",
    "required": ["executions", "pagination"],
    "properties": {
        "executions": {
            "type": "array",
            "items": PIPELINE_EXECUTION_SCHEMA,
        },
        "pagination": {
            "type": "object",
            "required": ["page", "page_size", "total_count", "total_pages", "has_next", "has_prev"],
            "properties": {
                "page": {"type": ["number", "integer"]},
                "page_size": {"type": ["number", "integer"]},
                "total_count": {"type": ["number", "integer"]},
                "total_pages": {"type": ["number", "integer"]},
                "has_next": {"type": "boolean"},
                "has_prev": {"type": "boolean"},
            },
        },
    },
}

# ---------------------------------------------------------------------------
# QueuedFilesResponse — GET /pipeline/{id}/queued-files
# ---------------------------------------------------------------------------

QUEUED_FILE_SCHEMA = {
    "type": "object",
    "required": ["file_name", "source_key", "queued_at", "status"],
    "properties": {
        "file_name": {"type": "string"},
        "source_key": {"type": "string"},
        "queued_at": {"type": "string"},
        "status": {"type": "string"},
    },
}

QUEUED_FILES_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["queued_files", "count"],
    "properties": {
        "queued_files": {
            "type": "array",
            "items": QUEUED_FILE_SCHEMA,
        },
        "count": {"type": ["number", "integer"]},
    },
}

# ---------------------------------------------------------------------------
# Approval — approval record
# ---------------------------------------------------------------------------

APPROVAL_SCHEMA = {
    "type": "object",
    "required": ["approval_id", "approval_type", "status", "created_at"],
    "properties": {
        "approval_id": {"type": "string"},
        "pipeline_key": {"type": "string"},
        "pipeline_id": {"type": "string"},
        "run_id": {"type": "string"},
        "approval_type": {"type": "string"},
        "status": {"type": "string"},
        "created_at": {"type": "string"},
        "decided_at": {"type": ["string", "null"]},
        "decided_by": {"type": ["string", "null"]},
        "comment": {"type": "string"},
        "gate_name": {"type": "string"},
        "has_task_token": {"type": "boolean"},
        "response_time_ms": {"type": ["number", "integer"]},
        "details": {"type": "object"},
    },
}

# ---------------------------------------------------------------------------
# GlueDatabase — item in GET /resources/glue/databases
# Backend currently uses 'name' field; frontend expects 'database_name'.
# Contract test must flag this inconsistency.
# ---------------------------------------------------------------------------

GLUE_DATABASE_SCHEMA = {
    "type": "object",
    "required": ["database_name"],
    "properties": {
        "database_name": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "locationUri": {"type": ["string", "null"]},
        "createTime": {"type": ["string", "null"]},
    },
}

GLUE_DATABASES_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["databases", "count"],
    "properties": {
        "databases": {
            "type": "array",
            "items": GLUE_DATABASE_SCHEMA,
        },
        "count": {"type": ["number", "integer"]},
    },
}

# ---------------------------------------------------------------------------
# ComprehensiveMetrics — GET /metrics/comprehensive
# ---------------------------------------------------------------------------

# R4.11 — FOUR values, not three. `_reported_trend` answers `not_established`
# whenever either side of the comparison had no measurement, and has done since
# the backend's absent cases landed (tasks 3.2 / 3.4). This enum declared three
# until then, so a real payload carrying `trends.ingestion = "not_established"`
# was REJECTED by `COMPREHENSIVE_METRICS_SCHEMA` — measured, not inferred. It
# never went red because nothing imports these schemas at runtime; the frontend
# union in `dashboard-react/src/types.ts` was narrow by the same amount, and both
# widen in one change so neither side can assert the old shape and pass.
_TREND_DIRECTION_ENUM = {"type": "string", "enum": ["up", "down", "stable", "not_established"]}

TREND_INDICATORS_SCHEMA = {
    "type": "object",
    "required": ["ingestion", "success", "agent_latency", "cost"],
    "properties": {
        "ingestion": _TREND_DIRECTION_ENUM,
        "success": _TREND_DIRECTION_ENUM,
        "agent_latency": _TREND_DIRECTION_ENUM,
        "cost": _TREND_DIRECTION_ENUM,
    },
}

# R2.6 / R2.7 — what the cost figure was summed from, and the statement that
# nothing here measures spend. Emitted by `_get_estimated_monthly_cost` as one
# dict alongside the figure, so it is `null` exactly when `monthly_cost` is:
# there is no basis for a sum that was never taken. `required` applies only when
# the instance is an object, so the null arm is unconstrained by it.
MONTHLY_COST_BASIS_SCHEMA = {
    "type": ["object", "null"],
    "required": [
        "metric",
        "window_start",
        "window_end",
        "runs",
        "rate_usd_per_dpu_hour",
        "dpu_minimum_minutes",
        "note",
    ],
    "properties": {
        "metric": {"type": "string"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "runs": {"type": ["number", "integer"]},
        "rate_usd_per_dpu_hour": {"type": "number"},
        "dpu_minimum_minutes": {"type": ["number", "integer"]},
        "note": {"type": "string"},
    },
}

# R5.4 — the per-status counts `overall_success_rate`'s denominator was built
# from, so a reader can reconstruct the rate on screen. `null`, not a dict of
# zeros, when the States read did not complete: zeros there would assert an
# empty window nobody observed. The four keys are the handler's
# `_TERMINAL_EXECUTION_METRICS` (`R5.1`, `R5.3`); `ExecutionsStarted` is not
# terminal and is deliberately absent (`R5.2`).
TERMINAL_COUNTS_SCHEMA = {
    "type": ["object", "null"],
    "required": ["succeeded", "failed", "aborted", "timed_out"],
    "properties": {
        "succeeded": {"type": ["number", "integer"]},
        "failed": {"type": ["number", "integer"]},
        "aborted": {"type": ["number", "integer"]},
        "timed_out": {"type": ["number", "integer"]},
    },
}

# p59 — the reconciliation between `performance.failed_pipelines` and the Stage
# Metrics panel below it. `failed_pipelines` counts every failed execution;
# stage `error_count`s could only ever see failures a stage row recorded, and
# `LGR-9e76249-6` (template / ratified) is why that is most of them missing: the
# RecordFailure sentinel is the only carrier of a run's terminal FAILED status
# when the failing step never wrote a row of its own.
#
# `null`, not a dict of zeros, when the `pipeline_failure` partition could not be
# read — zeros there would read as "no run failed outside a stage row", the
# collapse `LGR-4854b3d-1` (template / ratified) forbids. `performance.missing`
# names it on that condition and `determined_from` carries
# `pipeline_failure_records_incomplete`.
#
# `type` is deliberately `object`/`null` and NOT numeric: Property 7 in
# `test_metrics_stage_measurement_prop.py` derives the performance TILE key set
# from the properties here that declare a scalar-numeric type, and this is a
# structured fact rather than a tile. Declaring it numeric would put it in the
# `measurement` map's asserted key set.
#
# `failed_executions_without_a_record` is SIGNED and unclamped on purpose — it is
# the field whose job is to admit the arithmetic does not close, and both
# directions occur (see `_published_attribution`'s docstring).
STAGE_FAILURE_ATTRIBUTION_SCHEMA = {
    "type": ["object", "null"],
    "required": [
        "failure_records",
        "attributed_to_stage",
        "counted_into_stage_errors",
        "already_counted_by_stage_row",
        "unattributed",
        "unattributed_by_step",
        "rejected_at_approval",
        "rejected_by_step",
        "failed_executions",
        "failed_executions_without_a_record",
    ],
    "properties": {
        "failure_records": {"type": ["number", "integer"]},
        "attributed_to_stage": {"type": ["number", "integer"]},
        "counted_into_stage_errors": {"type": ["number", "integer"]},
        "already_counted_by_stage_row": {"type": ["number", "integer"]},
        "unattributed": {"type": ["number", "integer"]},
        # step name -> count. The breakdown, not a bare total: "5 timed out at
        # the code approval gate" and "5 failed somewhere" are different facts.
        "unattributed_by_step": {"type": "object", "additionalProperties": {"type": ["number", "integer"]}},
        # 2026-10-10 — rejections are set aside before anything else, so they
        # are in none of the fields above. Mirrors `performance.rejected_at_approval`
        # whenever this object is not null; the breakdown is by gate.
        "rejected_at_approval": {"type": ["number", "integer"]},
        "rejected_by_step": {"type": "object", "additionalProperties": {"type": ["number", "integer"]}},
        # Mirrors `performance.failed_pipelines` (rejections already taken out);
        # `null` when the AWS/States read did not complete, where that field's
        # own 0 is the read's failure.
        "failed_executions": {"type": ["number", "integer", "null"]},
        "failed_executions_without_a_record": {"type": ["number", "integer", "null"]},
    },
}

# p39 R4.2 / R4.3 / R4.11 — five performance figures are three-state, and the
# union here is the TRUE wire shape rather than a tolerance: each is `null`
# exactly when its source established no value, and `performance.missing` names
# it. Do NOT narrow one of these back to `{"type": "number"}` to quiet a
# validator — `LGR-99050e6-5` (template / ratified) rejected exactly that move
# on the ground that the union is what the producer emits, and the guard in
# `test_metrics_contract.py` asserts the TypeScript side still declares it
# nullable so a narrowing fails loudly on both halves.
#
# `failed_pipelines` is deliberately NOT widened: it is an `ExecutionsFailed`
# sum (less approval rejections since 2026-10-10), and a sum with no datapoints
# is a genuine zero — nothing failed. A failed READ is not lost by that, because
# its two siblings from the same read (`overall_success_rate`,
# `terminal_counts`) both carry the `_incomplete` token, and a failed rejection
# read is carried by `rejected_at_approval`.
PERFORMANCE_METRICS_SCHEMA = {
    "type": "object",
    "required": [
        "lines_ingested_24h",
        "processing_rate",
        "overall_success_rate",
        "failed_pipelines",
        "rejected_at_approval",
        "agent_latency_ms",
        "monthly_cost",
        "monthly_cost_basis",
        "terminal_counts",
        "stage_failure_attribution",
        "measurement",
        "determined_from",
        "missing",
        "trends",
    ],
    "properties": {
        # R4.9 — rows loaded: `rows_written` on the Glue job's `load_metrics`
        # rows in the window, counted where the write was confirmed; null when
        # no such row carries a usable count, never a count of rows. (Until
        # 2026-10-10 this was the `file_validation` `record_count_estimate`.)
        "lines_ingested_24h": {"type": ["number", "null"]},
        # Inherits R4.9's nullability: the rate is the rows-loaded figure divided
        # by the window's wall clock, and there is no rate without a numerator.
        "processing_rate": {"type": ["number", "null"]},
        # R4.6 — null when no execution reached a terminal outcome, rather than
        # the 100.0 a window with no runs used to report.
        # Since 2026-10-10 the denominator has approval rejections taken out of
        # Failed (see `rejected_at_approval`); `terminal_counts` stays raw.
        "overall_success_rate": {"type": ["number", "null"]},
        # `ExecutionsFailed` less approval rejections, capped so it never goes
        # below 0. Still not nullable: when the rejections cannot be read it is
        # the plain sum, and `rejected_at_approval` carries the failed read.
        "failed_pipelines": {"type": ["number", "integer"]},
        # 2026-10-10 — runs a reviewer rejected at an approval gate
        # (`pipeline_failure` rows with `error_name` ApprovalRejected). A tile.
        # `null` when that partition could not be read, NOT 0: zero would say
        # nobody rejected anything, and the two figures above would then be
        # read as adjusted when they were not.
        "rejected_at_approval": {"type": ["number", "integer", "null"]},
        # R4.1 / D1 — null when no agent stage published a duration in the
        # window. `infrastructure.agentcore.response_time_ms` below MIRRORS this
        # field: one fact under two names, so it carries the same union.
        "agent_latency_ms": {"type": ["number", "null"]},
        # R2.4 — null when no Glue run reached a terminal state in the window.
        # The flat-rate `$0.50`-per-execution fallback that made this always a
        # number is gone.
        "monthly_cost": {"type": ["number", "null"]},
        "monthly_cost_basis": MONTHLY_COST_BASIS_SCHEMA,
        "terminal_counts": TERMINAL_COUNTS_SCHEMA,
        "stage_failure_attribution": STAGE_FAILURE_ATTRIBUTION_SCHEMA,
        # R7.6 — tile key -> the measurement behind it. Derived from the
        # handler's one fact registry, never written out beside the values.
        "measurement": {"type": "object", "additionalProperties": {"type": "string"}},
        # R4.2 — field name -> provenance token, three-valued: the source name
        # for a measurement, `<source>_absent` for an established nothing,
        # `<source>_incomplete` for a read that could not be made.
        "determined_from": {"type": "object", "additionalProperties": {"type": "string"}},
        # R4.3 — every fact that was unavailable, derived from the assembled
        # payload's nulls so it cannot disagree with them.
        "missing": {"type": "array", "items": {"type": "string"}},
        "trends": TREND_INDICATORS_SCHEMA,
    },
}

STAGE_METRICS_SCHEMA = {
    "type": "object",
    "required": ["success_count", "error_count", "avg_time_ms"],
    "properties": {
        "success_count": {"type": ["number", "integer"]},
        "error_count": {"type": ["number", "integer"]},
        # null when the stage records no duration (rendered "N/A" on the frontend)
        "avg_time_ms": {"type": ["number", "null"]},
    },
}

HEALTH_STATUS_ENUM = {"type": "string", "enum": ["healthy", "warning", "critical", "unknown"]}

INFRASTRUCTURE_STATUS_SCHEMA = {
    "type": "object",
    "required": ["agentcore", "dynamodb", "s3", "lambda"],
    "properties": {
        "agentcore": {
            "type": "object",
            "required": ["availability", "response_time_ms", "status"],
            "properties": {
                # R4.8 — null when the agent error-rate query established no
                # rate: a raising query, or a window with no agent invocations.
                # Both used to yield an error rate of 0.0, hence an availability
                # of 100.0 and a `healthy` status — a perfect record claimed on
                # the one event that proves nothing was observed. `status` then
                # reads `unknown`, already in HEALTH_STATUS_ENUM.
                "availability": {"type": ["number", "null"]},
                # R4.1 / D1 — MIRRORS `performance.agent_latency_ms`: one fact
                # under two names, read from the same rounded local, so it
                # inherits that field's Not_Established case. Widening the
                # performance block alone would leave this half of the same fact
                # declaring a non-null number.
                "response_time_ms": {"type": ["number", "null"]},
                "status": HEALTH_STATUS_ENUM,
            },
        },
        "dynamodb": {
            "type": "object",
            "required": ["read_capacity_per_hour", "write_capacity_per_hour", "status"],
            "properties": {
                "read_capacity_per_hour": {"type": "number"},
                "write_capacity_per_hour": {"type": "number"},
                "status": HEALTH_STATUS_ENUM,
            },
        },
        "s3": {
            "type": "object",
            "required": ["storage_used_bytes", "requests_per_minute", "status"],
            "properties": {
                # R1.3 — null when the deployed buckets report no
                # `BucketSizeBytes` datapoint. It is a daily metric, so "no
                # series" and "measured empty" are different facts and must not
                # collapse (`LGR-4854b3d-1`).
                "storage_used_bytes": {"type": ["number", "null"]},
                # R4.7 — always null: S3 request metrics were never enabled on
                # the deployed buckets, so the `0` this used to declare was a
                # hand-written constant dressed as a measurement. The union is
                # kept rather than pinned to `null` so enabling the metrics later
                # is a handler change and not also a contract change.
                "requests_per_minute": {"type": ["number", "null"]},
                "status": HEALTH_STATUS_ENUM,
            },
        },
        "lambda": {
            "type": "object",
            "required": ["invocations_per_minute", "error_rate", "status"],
            "properties": {
                "invocations_per_minute": {"type": "number"},
                "error_rate": {"type": "number"},
                "status": HEALTH_STATUS_ENUM,
            },
        },
    },
}

COMPREHENSIVE_METRICS_SCHEMA = {
    "type": "object",
    "required": ["performance", "stages", "infrastructure"],
    "properties": {
        "performance": PERFORMANCE_METRICS_SCHEMA,
        "stages": {
            "type": "object",
            "additionalProperties": STAGE_METRICS_SCHEMA,
        },
        "infrastructure": INFRASTRUCTURE_STATUS_SCHEMA,
        "period": {"type": "string"},
        "generated_at": {"type": "string"},
    },
}


# ---------------------------------------------------------------------------
# Data quality report (GET /pipelines/{id}/runs/{runId}/quality-report)
# ---------------------------------------------------------------------------
# Added by p64 task 2d.3 because R8.4 makes a new `checks[]` entry a
# response-shape change: the non-printable character check (R8.1) appends an
# entry to this list, so the list needed a declared shape before it grew one.
# There was none here at all — the endpoint returns the `(run_id,
# "data_quality")` state row VERBATIM (`handlers/agents.py::get_quality_report`,
# line 121-125), so until now the wire format was whatever two unrelated
# producers happened to write.
#
# `additionalProperties: True` throughout, deliberately. Two producers write
# this key — the generated Glue job's inline evaluation (`source:
# "glue_inline_dq"`) and the on-demand agent (`agent_type: "data_quality"`) —
# and the agent's fields are not enumerated here. Pinning them closed would
# make this schema refuse a valid row from the producer it does not model.
QUALITY_CHECK_SCHEMA = {
    "type": "object",
    "required": ["name", "status"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        # THREE VALUES, not four. p64 2d.1 decided the non-printable check
        # FLAGS rather than quarantines, and its entry reports `warning` —
        # a value both this contract and `QualityReportCard.checkStatusType`
        # already admitted. Do not add a fourth: `fail` is reserved for an
        # outcome that removed rows from the frame, and a flag did not.
        #
        # TWO non-rule entries now use `warning`, not one. p64 2e.1 added
        # `TypeCastFailures` (R9.1/R9.3): a count of cells that a type
        # conversion turned into NULL, which the `Completeness` rule cannot
        # tell from cells whose source was empty. It reuses this shape
        # exactly — `name`, `status`, a numeric `actual`, a string
        # `failure_reason` — so it needed NO widening here, which is why the
        # schema is unchanged and only this comment moved. `name` is
        # deliberately not an enum: pinning it would make this schema refuse
        # the next such entry, and the on-demand agent's names are not
        # enumerated here either.
        #
        # NEITHER entry is counted in `rules_passed`/`rules_failed`/
        # `rules_total` below, and `TypeCastFailures` is ABSENT rather than
        # zero on a run that did not measure — the count is only computable
        # where the generated job has an auto-cast block, so a written zero
        # would be a claim the run cannot support. Do not add it to
        # `required`.
        "status": {"enum": ["pass", "warning", "fail"]},
        # Null on the DQDL arm: the rule loop writes
        # `float(_mv) if _mv is not None else None` when a rule reports no
        # evaluated metric (`_partials/_data_quality.py.j2`). The R8 entry
        # always writes a float. The union is the true wire shape, so it is
        # declared rather than narrowed to make the frontend type simpler
        # (`LGR-99050e6-5` refused exactly that narrowing).
        "actual": {"type": ["number", "null"]},
        # Present on a DQDL failure, and on the R8 entry only when it found
        # something (`_adi_np_rows > 0`). It carries the sentence that says the
        # rows were "flagged, not quarantined" — the only place the wire format
        # states that the rows reached the target, so it is a required part of
        # the shape being pinned, not an incidental extra.
        "failure_reason": {"type": "string", "minLength": 1},
        "expected": {"type": ["number", "null"]},
        "rate": {"type": ["number", "null"]},
        "threshold": {"type": ["number", "null"]},
        "duplicates": {"type": ["number", "null"]},
        "latest_record": {"type": "string"},
        "sample_nulls": {"type": "array", "items": {"type": "string"}},
        "sample_duplicates": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

QUALITY_REPORT_SCHEMA = {
    "type": "object",
    "required": ["run_id", "overall_status", "checks"],
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "step_name": {"const": "data_quality"},
        "pipeline_id": {"type": "string"},
        "overall_status": {"enum": ["pass", "warning", "fail"]},
        "checks": {"type": "array", "items": QUALITY_CHECK_SCHEMA},
        "root_cause": {"type": ["string", "null"]},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        # Inline producer only. These three describe the DQDL ruleset and
        # nothing else: the R8 entry is counted in none of them (its own
        # comment in the template says so), so `len(checks)` and `rules_total`
        # are NOT expected to agree and no assertion should require it.
        "rules_passed": {"type": "number"},
        "rules_failed": {"type": "number"},
        "rules_total": {"type": "number"},
        "rows_passed": {"type": "number"},
        # Rows removed from the frame before the target write. The R8 entry
        # contributes nothing here — it flags and diverts nothing.
        "rows_quarantined": {"type": "number"},
        "quarantine_path": {"type": ["string", "null"]},
        "source": {"type": "string"},
        "agent_type": {"type": "string"},
        "timestamp": {"type": "string"},
    },
    "additionalProperties": True,
}

#: The endpoint's envelope. `report` is null for a run with no row — the
#: handler returns 200 with `{"report": None}` rather than a 404, and the hook
#: (`use-quality-report.ts::fetchQualityReport`) coalesces both to null.
QUALITY_REPORT_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["report"],
    "properties": {"report": {"oneOf": [QUALITY_REPORT_SCHEMA, {"type": "null"}]}},
}


# ---------------------------------------------------------------------------
# Run cancellation (POST /runs/{runId}/cancel)
# ---------------------------------------------------------------------------
# Derived from `CancelRunResponse` in dashboard-react/src/hooks/use-cancel-run.ts,
# which test_cancel_run_response_contract.py parses and holds these against.

#: The two statuses an accepted cancel answers with: CANCELLED (HTTP 200, the run
#: was stopped or already was) and CANCELLING (HTTP 202, its Glue job is stopping).
CANCEL_RUN_STATUS_ENUM = ["CANCELLED", "CANCELLING"]

CANCEL_RUN_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["success", "run_id", "status", "message"],
    "properties": {
        "success": {"type": "boolean", "const": True},
        "run_id": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": CANCEL_RUN_STATUS_ENUM},
        "message": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

#: A refused cancel: 404 (no such run) or 409 (finished, or not cancellable yet).
#: The dashboard's ApiError surfaces `error` verbatim as the toast's reason.
CANCEL_RUN_REFUSAL_SCHEMA = {
    "type": "object",
    "required": ["error", "run_id"],
    "properties": {
        "error": {"type": "string", "minLength": 1},
        "run_id": {"type": "string"},
        "status": {"type": "string"},
    },
    "additionalProperties": False,
}
