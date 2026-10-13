# DynamoDB Tables Documentation

## Overview

Agentic Data Ingestion uses DynamoDB tables to manage pipeline state, execution summaries, metadata, generated code, test results, schema mappings, approvals, task tokens, notifications, WebSocket connections, errors, pipeline locks, audit logs, agent traces, JDBC incremental-source watermarks, processed-file content fingerprints, and per-run model-call budgets. All table names follow the `agentic_data_ingestion_{name}` convention (underscores, no env suffix, no `sample_` prefix).

**This file is the per-table reference and holds one numbered section per table**, so the count is
derivable here by enumeration. The count as a *figure*, with its derivation, is stated once in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory) — read it there rather than
carrying a copy.

> **Phase 4 Changes (2026-02-22):** Table names standardized, state table PK changed to `run_id`, GSIs consolidated downward, `agentic_data_ingestion_schema_metadata` deleted (unused), two new tables added (`agentic_data_ingestion_executions`, `agentic_data_ingestion_task_tokens`).
>
> **The GSI consolidation is history, and it did not hold.** The count fell on that date and has
> risen since, as later phases added tables that brought indexes with them. The figures that
> bracketed the change on the day are deliberately not restated here, because a number pinned inside
> a dated note reads as current and goes stale in place. The live total is derived once, in the GSI
> row of [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory).
>
> **Later Phases:** Added `agentic_data_ingestion_ws_connections` (Phase 6 — WebSocket), `agentic_data_ingestion_errors` (Phase 8), `agentic_data_ingestion_pipeline_locks` (Phase 11), `agentic_data_ingestion_audit_log` (Phase 13), `agentic_data_ingestion_agent_traces` (Phase 13a), `agentic_data_ingestion_pipeline_watermarks` (P17 — JDBC source), `agentic_data_ingestion_model_call_budget` (P28 R15.2 — per-run model-call budget), `agentic_data_ingestion_processed_files` (p30 R9.1 — Processed_File_Ledger).

---

## Table of Contents

1. [agentic_data_ingestion_state](#1-agentic_data_ingestion_state)
2. [agentic_data_ingestion_pipelines](#2-agentic_data_ingestion_pipelines)
3. [agentic_data_ingestion_generated_code](#3-agentic_data_ingestion_generated_code)
4. [agentic_data_ingestion_test_results](#4-agentic_data_ingestion_test_results)
5. [agentic_data_ingestion_schema_mappings](#5-agentic_data_ingestion_schema_mappings)
6. [agentic_data_ingestion_approvals](#6-agentic_data_ingestion_approvals)
7. [agentic_data_ingestion_notifications](#7-agentic_data_ingestion_notifications)
8. [agentic_data_ingestion_executions](#8-agentic_data_ingestion_executions)
9. [agentic_data_ingestion_task_tokens](#9-agentic_data_ingestion_task_tokens)
10. [agentic_data_ingestion_ws_connections](#10-agentic_data_ingestion_ws_connections)
11. [agentic_data_ingestion_errors](#11-agentic_data_ingestion_errors)
12. [agentic_data_ingestion_pipeline_locks](#12-agentic_data_ingestion_pipeline_locks)
13. [agentic_data_ingestion_audit_log](#13-agentic_data_ingestion_audit_log)
14. [agentic_data_ingestion_agent_traces](#14-agentic_data_ingestion_agent_traces)
15. [agentic_data_ingestion_pipeline_watermarks](#15-agentic_data_ingestion_pipeline_watermarks)
16. [agentic_data_ingestion_processed_files](#16-agentic_data_ingestion_processed_files)
17. [agentic_data_ingestion_model_call_budget](#17-agentic_data_ingestion_model_call_budget)

---

## 1. agentic_data_ingestion_state

> **Renamed from `agentic_data_ingestion_state` in Phase 4. PK changed from synthetic `{run_id}.{seq}` to `run_id`. Phase 4 reduced the GSIs from 5 to 1; later phases added two back, so the table carries **3** GSIs today — `PipelineIndex`, `GlueJobRunIndex` and `step_name-timestamp-index` (`deploy-cdk/lib/data-stack.ts:45-79`).**

**Purpose**: Tracks the execution state of each step in the ETL pipeline workflow.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `run_id` (PK) | String | Unique execution identifier |
| `step_name` (SK) | String | Name of the pipeline step |
| `pipeline_id` | String | Pipeline identifier (for PipelineIndex GSI) |
| `original_pipeline_id` | String | Original pipeline identifier |
| `status` | String | Step status: `success`, `error`, `pending`, `in_progress` |
| `started_at` | String | ISO 8601 timestamp |
| `timestamp` | String | ISO 8601 timestamp |
| `step_sequence` | Number | Step order in pipeline (1-10) |
| `glue_job_run_id` | String | AWS Glue job run identifier |
| `output_data` | Map | Standardized artifact output (see artifact_schema.py) |
| `error_details` | String | Error information if failed |
| `ai_model_used` | String | Bedrock model identifier |
| `execution_time_ms` | Number | Step execution duration |
| `file_name` | String | Source file name |

### Global Secondary Indexes

Three, all `ProjectionType.ALL`. Verified against `deploy-cdk/lib/data-stack.ts:45-79`.

#### PipelineIndex
- **Keys**: `pipeline_id` (HASH), `timestamp` (RANGE) — `data-stack.ts:55-56`
- **Purpose**: Query all runs for a specific pipeline
- **Projection**: ALL
- **Use Case**: Pipeline execution history, dashboard pipeline details

> The range key is `timestamp`, **not** `started_at`. Both attributes exist on a state row, so a
> query written against `started_at` here is a key-condition error rather than an empty result.

#### GlueJobRunIndex
- **Keys**: `glue_job_run_id` (HASH) — no range key (`data-stack.ts:59-63`)
- **Purpose**: Resolve a Glue job run back to the state row that launched it
- **Projection**: ALL
- **Use Case**: Glue job-completion callback correlation

#### step_name-timestamp-index
- **Keys**: `step_name` (HASH), `timestamp` (RANGE) — `data-stack.ts:74-79`
- **Purpose**: Read step rows by step name within a time window instead of scanning the table
  (P28 R17.1/17.2 — dashboard metrics stage counts, lines ingested, `glue_jobs` schema-version
  lookups)
- **Projection**: ALL
- **Sparse, by design**: a row with no `timestamp` attribute (the `metrics_cache` row, for one)
  never enters the index. That matches the behaviour of the scan it replaced, which skipped rows
  with an unparseable timestamp client-side. It is not a defect, and it is not a filter.

> **Deploy note.** DynamoDB creates at most **one** GSI per table per stack update. This table
> already carries three, so a fourth must be deployed on its own.

### Removed GSIs (Phase 4)

The following GSIs were removed as unused:
- `StatusIndex` (PK=status, SK=timestamp) — never queried in Lambda code
- `TimestampIndex` (PK=pipeline_id, SK=timestamp) — used synthetic PK, not actual pipeline_id
- `RunIdIndex` (PK=run_id, SK=step_sequence) — no longer needed since `run_id` is now the base table PK

> Earlier revisions of this section also listed `GlueJobRunIdIndex` as removed. That is wrong on
> both counts: the index is **present**, and its name is `GlueJobRunIndex` (no `Id`). See the
> Global Secondary Indexes section above.

### DynamoDB Stream

- **Stream View Type**: `NEW_IMAGE`
- **Consumer**: `agentic_data_ingestion_execution_updater` Lambda
- **Purpose**: Populates the `agentic_data_ingestion_executions` summary table on every step INSERT/MODIFY

### Capacity

- **Billing Mode**: Pay-per-request
- **Point-in-Time Recovery**: Enabled
- **Encryption**: SSE enabled

### Example Record

```json
{
  "run_id": "run-2025-01-15-12-00-00-abcd1234",
  "step_name": "schema_mapping",
  "pipeline_id": "pipeline-customer-data",
  "original_pipeline_id": "pipeline-customer-data",
  "status": "success",
  "started_at": "2025-01-15T10:30:45.123Z",
  "step_sequence": 4,
  "output_data": {
    "artifact_type": "schema_mapping",
    "primary_id": "mapping-20250115-abc123",
    "status": "success",
    "summary": {"confidence": 0.95}
  },
  "ai_model_used": "us.anthropic.claude-sonnet-4-6",
  "execution_time_ms": 3456
}
```

---

## 2. agentic_data_ingestion_pipelines

**Purpose**: Stores pipeline metadata and configuration.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pipeline_id` (PK) | String | Unique pipeline identifier |
| `pipeline_name` | String | Human-readable pipeline name |
| `created_at` | String | ISO 8601 creation timestamp |
| `updated_at` | String | ISO 8601 last update timestamp |
| `status` | String | Pipeline status: `active`, `archived`, `failed` |
| `target_schema_name` | String | Target schema identifier |
| `target_schema` | Map | Target schema definition |
| `source_config` | Map | Source configuration (S3 location, format) |
| `target_config` | Map | Target configuration (type, location, credentials) |
| `ai_model` | String | Bedrock model for code generation |
| `pipeline_description` | String | Pipeline description. The stored attribute is `pipeline_description`, **not** `description` — `create_pipeline` writes it under that name (`dashboard_api/handlers/pipelines_crud.py:2035`) and the update path sets the same attribute (`:2297-2301`), accepting either spelling on the *request* body only |
| `created_by` | String | User who created pipeline |
| `glue_job_name` | String | Associated Glue job name |
| `glue_job_arn` | String | Glue job ARN |

### Global Secondary Indexes

#### status-created_at-index
- **Keys**: `status` (HASH), `created_at` (RANGE)
- **Purpose**: Query pipelines by status
- **Projection**: ALL
- **Use Case**: Dashboard pipeline list filtering

#### target_schema_name-index
- **Keys**: `target_schema_name` (HASH), `created_at` (RANGE)
- **Purpose**: Find pipelines using specific target schema
- **Projection**: ALL
- **Use Case**: Schema impact analysis

### Capacity

- **Billing Mode**: Pay-per-request
- **Point-in-Time Recovery**: Enabled
- **Encryption**: Default

### Example Record

```json
{
  "pipeline_id": "pipeline-customer-data",
  "pipeline_name": "Customer Data ETL",
  "created_at": "2024-11-01T08:00:00.000Z",
  "updated_at": "2024-11-12T10:30:00.000Z",
  "status": "active",
  "target_schema_name": "customer_master_v2",
  "target_schema": {
    "fields": [
      {"name": "customer_id", "type": "string"},
      {"name": "email", "type": "string"}
    ]
  },
  "source_config": {
    "type": "S3",
    "bucket": "agentic-data-ingestion-source-files",
    "prefix": "customers/",
    "format": "parquet"
  },
  "target_config": {
    "type": "S3_TABLES",
    "s3_table_bucket": "customer-data-lake",
    "table_name": "customers",
    "format": "iceberg"
  },
  "ai_model": "us.anthropic.claude-sonnet-4-6",
  "pipeline_description": "Ingests customer data from S3 to Iceberg table",
  "created_by": "user@example.com",
  "glue_job_name": "pipeline-customer-data-etl-job",
  "glue_job_arn": "arn:aws:glue:us-west-2:123456789012:job/pipeline-customer-data-etl-job"
}
```

---

## 3. agentic_data_ingestion_generated_code

> **Renamed from `adi-generated-code-dev` in Phase 4.**

**Purpose**: Stores metadata about AI-generated PySpark code.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `PK` (PK) | String | `CODE#{code_version_id}` |
| `SK` (SK) | String | `VERSION` or `METADATA` |
| `code_version_id` | String | Unique code version identifier |
| `pipeline_id` | String | Associated pipeline |
| `code_s3_uri` | String | S3 location of generated code |
| `created_at` | String | ISO 8601 timestamp |
| `ai_model` | String | Bedrock model used |
| `quality_score` | Number | Code quality score (0-100) |
| `validation_status` | String | `valid`, `invalid`, `pending` |
| `syntax_errors` | List | List of syntax errors |
| `required_params` | List | Required Glue job parameters |
| `optional_params` | List | Optional Glue job parameters |
| `template_used` | String | PySpark template identifier |

### Global Secondary Indexes

Two, both `ProjectionType.ALL`. Verified against `deploy-cdk/lib/data-stack.ts:108-109`.

#### pipeline_id-index
- **Keys**: `pipeline_id` (HASH), `generation_timestamp` (RANGE)
- **Purpose**: List a pipeline's code versions newest-first
- **Projection**: ALL

#### run_id-index
- **Keys**: `run_id` (HASH), `generation_timestamp` (RANGE)
- **Purpose**: Find the code version a given run generated
- **Projection**: ALL

> Three tables carry an index named `run_id-index` — this one, the approvals table and the
> executions table — and the three have **different key schemas**. Check which table you are
> querying before reusing a key condition.

### Capacity

- **Billing Mode**: Pay-per-request (`PAY_PER_REQUEST`)
- **Streams**: Enabled (for triggering tests)
- **Encryption**: Default

### Example Record

```json
{
  "PK": "CODE#code-20241112-xyz789",
  "SK": "VERSION",
  "code_version_id": "code-20241112-xyz789",
  "pipeline_id": "pipeline-customer-data",
  "code_s3_uri": "s3://adi-generated-code-{account}-{region}/pipeline-customer-data/code/v1/etl_script.py",
  "created_at": "2024-11-12T10:35:00.000Z",
  "ai_model": "us.anthropic.claude-sonnet-4-6",
  "quality_score": 95,
  "validation_status": "valid",
  "syntax_errors": [],
  "required_params": [
    "SOURCE_S3_PATH",
    "TARGET_S3_PATH",
    "TARGET_TABLE_NAME"
  ],
  "optional_params": [
    "PARTITION_KEYS",
    "COMPRESSION_TYPE"
  ],
  "template_used": "write_s3_tables_v1"
}
```

---

## 4. agentic_data_ingestion_test_results

> **Renamed from `agentic-data-ingestion-test-results-dev` in Phase 4.**

**Purpose**: Stores test execution results for generated code.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `PK` (PK) | String | `TEST#{code_version_id}` |
| `SK` (SK) | String | `RESULT#{timestamp}` |
| `code_version_id` | String | Code version tested |
| `test_status` | String | `passed`, `failed`, `error` |
| `tests_passed` | Number | Number of passed tests |
| `tests_failed` | Number | Number of failed tests |
| `coverage_percentage` | Number | Code coverage (0-100) |
| `output_uri` | String | S3 location of test outputs |
| `execution_time_ms` | Number | Test execution duration |
| `spark_session_id` | String | Interactive session ID |
| `error_details` | String | Error information if failed |

### Global Secondary Indexes

#### test_status-index
- **Keys**: `test_status` (HASH), `test_timestamp` (RANGE) — `data-stack.ts:119`
- **Purpose**: Query test runs by outcome
- **Projection**: ALL

### Capacity

- **Billing Mode**: Pay-per-request (`PAY_PER_REQUEST`)
- **Streams**: Enabled (`NEW_AND_OLD_IMAGES`)
- **Encryption**: Default

### Example Record

```json
{
  "PK": "TEST#code-20241112-xyz789",
  "SK": "RESULT#2024-11-12T10:40:00.000Z",
  "code_version_id": "code-20241112-xyz789",
  "test_status": "passed",
  "tests_passed": 12,
  "tests_failed": 0,
  "coverage_percentage": 87,
  "output_uri": "s3://agentic-data-ingestion-test-outputs/pipeline-customer-data/test-20241112/",
  "execution_time_ms": 45678,
  "spark_session_id": "agentcore-spark-abc123",
  "error_details": null
}
```

---

## 5. agentic_data_ingestion_schema_mappings

> **Renamed from `agentic_data_ingestion_schema_mappings_dev` in Phase 4.**

**Purpose**: Stores generated schema mapping records (per-mapping provenance and content). This is a record store, not a lookup cache — reuse happens through AgentCore mapping memory (approved mappings guide future AI generation) and through freshest-approved-mapping substitution at code-generation time.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `mapping_id` (PK) | String | Content-hash-derived mapping identifier (identical mappings dedupe) |
| `source_schema_hash` | String | Hash of source schema |
| `target_schema_hash` | String | Hash of target schema |
| `created_timestamp` | String | ISO 8601 timestamp |
| `updated_timestamp` | String | ISO 8601 timestamp of the latest write |
| `mapping_result` | String | JSON-serialized field mapping definitions |
| `mapping_version` | Number | Incremented on context remapping updates |
| `confidence_score` | Number | Overall confidence (0-1) |
| `ai_model_used` | String | Bedrock model identifier (or fallback marker) |
| `generated_by` | String | Honest provenance: model id or `fallback_substring_match` |
| `update_type` | String | `initial_mapping` or `context_remapping` |
| `run_id` | String | Run that produced this mapping (when available) |
| `pipeline_id` | String | Pipeline whose run produced this mapping (when available) |

> `pipeline_id` and `run_id` are both written **absent, not empty**, and both name the run that
> **first created** this content. `mapping_id` is a content hash, so a second pipeline producing a
> byte-identical mapping reuses this record without writing to it — neither attribute enumerates
> every pipeline that uses the mapping.
>
> Neither is indexed. **A pipeline-scoped read of this table is still a scan with a filter**; the
> attributes aid correlation, they do not make the table queryable by pipeline. Adding a GSI for
> that is a cost and capacity decision nobody has taken.

### Global Secondary Indexes

#### source-schema-index
- **Keys**: `source_schema_hash` (HASH), `created_timestamp` (RANGE)
- **Purpose**: Find mappings by source schema
- **Projection**: ALL

#### target-schema-index
- **Keys**: `target_schema_hash` (HASH), `created_timestamp` (RANGE)
- **Purpose**: Find mappings by target schema
- **Projection**: ALL

#### schema-pair-index
- **Keys**: `source_schema_hash` (HASH), `target_schema_hash` (RANGE)
- **Purpose**: Find exact schema pair matches
- **Projection**: ALL
- **Use Case**: Mapping reuse optimization

### Capacity

- **Billing Mode**: Pay-per-request
- **Point-in-Time Recovery**: Enabled

### Example Record

```json
{
  "mapping_id": "mapping-20241112-abc123",
  "source_schema_hash": "sha256:abc123...",
  "target_schema_hash": "sha256:def456...",
  "created_timestamp": "2024-11-12T10:30:00.000Z",
  "mapping_result": "{\"mappings\": {\"cust_id\": {\"target_field\": \"customer_id\", \"transformation\": \"direct\", \"confidence\": 0.98}}}",
  "confidence_score": 0.95,
  "ai_model_used": "us.anthropic.claude-sonnet-4-6",
  "generated_by": "us.anthropic.claude-sonnet-4-6",
  "mapping_version": 1,
  "update_type": "initial_mapping"
}
```

---

## 6. agentic_data_ingestion_approvals

> **Renamed from `agentic_data_ingestion_pipeline_approvals` in Phase 4. Task tokens moved to dedicated `agentic_data_ingestion_task_tokens` table.**

**Purpose**: Manages human-in-the-loop approval workflows.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `approval_id` (PK) | String | Unique approval request ID |
| `pipeline_id` | String | Associated pipeline |
| `run_id` | String | Associated run |
| `approval_type` | String | Exactly one of `schema_mapping`, `code_generation`, `first_run`, `test_failure` — the validator's `valid_types` list at `lambda/agentic_data_ingestion_approval_handler/lambda_function.py:189`, which raises on anything else. The forced test-review gate is **`test_failure`**, not `test_results`; that value is created when the TestCode step reports a failed structured result (P28 R4.2, the `ForceTestReviewApproval` state) |
| `status` | String | `pending`, `approved`, `rejected`, `timeout` |
| `has_task_token` | Boolean | Whether a task token exists in `agentic_data_ingestion_task_tokens` |
| `created_at` | String | ISO 8601 timestamp |
| `timeout_at` | String | Approval deadline (`created_at` + `timeout_hours`; default **72 hours** from `shared/config.py`'s `APPROVAL_WINDOW_HOURS`, which mirrors the CDK gate's `APPROVAL_TIMEOUT_SECONDS`) |
| `ttl` | Number | Auto-cleanup epoch, **30 days** after creation. This is row retention, not the approval window |
| `approved_by` | String | User who approved/rejected |
| `approved_at` | String | Approval timestamp |
| `rejection_reason` | String | Reason for rejection |
| `approval_data` | Map | Data requiring approval |

### Global Secondary Indexes

#### pipeline-status-index
- **Keys**: `pipeline_id` (HASH), `status` (RANGE)
- **Purpose**: Query approvals by pipeline and status
- **Projection**: ALL

#### status-created-index
- **Keys**: `status` (HASH), `created_at` (RANGE)
- **Purpose**: Query approvals by status
- **Projection**: ALL
- **Use Case**: Dashboard pending approvals view

#### approval-type-created-index
- **Keys**: `approval_type` (HASH), `created_at` (RANGE)
- **Purpose**: Query approvals by type
- **Projection**: ALL

#### run_id-index
- **Keys**: `run_id` (HASH) — no range key (`data-stack.ts:143`)
- **Purpose**: Per-run approval lookups (dashboard run details, pending-approval checks) query by
  `run_id` instead of scanning inside a per-run loop (P28 R17.2)
- **Projection**: ALL
- **PK-only on purpose**: an approval row missing `created_at` must still be visible to the
  run-details listing, and a run only ever has a handful of approvals

> **Deploy note.** This table carries four GSIs and DynamoDB creates at most one per table per
> stack update, so a fifth must be deployed on its own.

### Approval window: 72 hours, in prose and in the examples

The advertised window is **72 hours**. It mirrors the gate that enforces it — `APPROVAL_TIMEOUT_SECONDS = 259200`
in `deploy-cdk/lib/orchestration-stack.ts`, the `TimeoutSeconds` on all three `waitForTaskToken`
approval gates. When that elapses, `Catch[States.ALL]` routes to `RecordFailure` and the execution
is abandoned.

Three separate durations live near each other here, and conflating them is the failure this section
exists to prevent:

| Duration | Value | What it is |
|---|---|---|
| Approval window | **72 h** | What the approver is promised, and what the gate enforces. `timeout_at` is derived from it |
| Task-token TTL | 7 days | A **backstop** on `agentic_data_ingestion_task_tokens`, deliberately longer than the window so a token outlives the wait instead of racing it. It is **not** the advertised window |
| Row TTL (`ttl`) | 30 days | Auto-cleanup of the approval row itself, long after the decision |

The first two are deliberately no longer the same number. Advertising 168 h while enforcing 72 h
promised an approver time the gate would not give them: told 7 days, they act on day 4 and find the
run abandoned. Retargeting the shared constant to 3 days was rejected because that one value served
as both the window and the TTL, so it would have dragged the token TTL down to race the very window
it exists to survive.

**The agreement is pinned by test, not restated as a second literal.**
`tests/contracts/test_approval_window_cross_language.py` parses `APPROVAL_TIMEOUT_SECONDS` out of
the TypeScript and asserts `APPROVAL_WINDOW_HOURS * 3600` equals it, that the env-backed
`default_timeout_hours()` reader agrees, and that `TASK_TOKEN_TTL_SECONDS` is strictly greater than
the gate. Changing one side without the other fails there. Do not add a second assertion of the
same fact.

→ **Approval lifecycle and `timeout_behavior`**: see [DATABASE-ARCHITECTURE.md](DATABASE-ARCHITECTURE.md).
That document owns the lifecycle; only `"manual"` is advertised for `timeout_behavior`, because
`auto_approve` and `auto_reject` are unreachable twice over: no timeout sweep can arrive before the
gate has already consumed the token, and no code path writes the attribute that would select them.

### Capacity

- **Billing Mode**: Pay-per-request (`PAY_PER_REQUEST`)
- **TTL**: Enabled on `ttl` (auto-delete old approvals after 30 days)
- **Point-in-Time Recovery**: Enabled
- **Encryption**: SSE enabled

### Example Record

```json
{
  "approval_id": "approval-20241112-xyz789",
  "pipeline_id": "pipeline-customer-data",
  "run_id": "run-20241112-abc123",
  "approval_type": "schema_mapping",
  "status": "pending",
  "created_at": "2024-11-12T10:30:00.000Z",
  "timeout_at": "2024-11-15T10:30:00.000Z",
  "approval_data": {
    "mappings": [...],
    "confidence_score": 0.72,
    "low_confidence_fields": ["legacy_field"]
  }
}
```

`timeout_at` is `created_at` + **72 h**. An earlier revision of this example showed
`2024-11-19T10:30:00.000Z` (168 h), contradicting the 72 h stated two sections above and modelling
exactly the overpromise that separating the window from the token TTL exists to stop.

---

## 7. agentic_data_ingestion_notifications

> **Renamed from `agentic_data_ingestion_notifications_dev` in Phase 4. GSIs reduced from 3 to 1.**

**Purpose**: Stores dashboard notifications for users.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `notification_id` (PK) | String | Unique notification ID |
| `created_at` | String | ISO 8601 timestamp |
| `priority` | String | Notification priority (high, medium, low) |
| `dashboard_read` | String | Read status for dashboard filtering |
| `notification_type` | String | `approval_required`, `pipeline_complete`, `error` |
| `title` | String | Notification title |
| `message` | String | Notification message |
| `link` | String | Dashboard link |
| `ttl` | Number | Unix timestamp for auto-deletion |

### Global Secondary Indexes

#### PriorityIndex
- **Keys**: `priority` (HASH), `created_at` (RANGE)
- **Purpose**: Query notifications by priority
- **Projection**: ALL

### Removed GSIs (Phase 4)

- `CreatedAtIndex` (PK=created_at) — removed, use PriorityIndex or scan
- `DashboardReadIndex` (PK=dashboard_read, SK=created_at) — removed, feature not yet implemented

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` (auto-delete after 30 days)
- **Point-in-Time Recovery**: Enabled

### Example Record

```json
{
  "notification_id": "notif-20241112-abc123",
  "created_at": "2024-11-12T10:30:00.000Z",
  "priority": "high",
  "dashboard_read": "false",
  "notification_type": "approval_required",
  "title": "Approval Required: Customer Data Pipeline",
  "message": "Schema mapping requires your approval (confidence: 85%)",
  "link": "/pipeline/pipeline-customer-data/run/run-20241112-abc123",
  "ttl": 1734000000
}
```

---

## 8. agentic_data_ingestion_executions

> **NEW in Phase 4. Populated via DynamoDB Stream from the state table.**

**Purpose**: Denormalized execution summary table for fast dashboard queries. Replaces the old `get_pipeline_executions` full-table-scan + Step Functions API call pattern.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pipeline_id` (PK) | String | Pipeline identifier |
| `run_id` (SK) | String | Unique execution identifier |
| `status` | String | `running`, `completed`, `failed`, `waiting_approval` |
| `current_step` | String | Name of the currently executing step |
| `steps_status` | Map | Map of step_name → status |
| `approval_status` | String | `None`, `pending`, `approved`, `rejected` |
| `started_at` | String | ISO 8601 timestamp |
| `completed_at` | String | ISO 8601 timestamp (null if running) |
| `file_name` | String | Source file name |
| `schema_version` | String | Schema version identifier |
| `ai_model` | String | Bedrock model identifier |
| `updated_at` | String | ISO 8601 last update timestamp |
| `queue_outcome` | String | → attribute. Mechanism: [WORKFLOW.md § Queue admission, loss and skips](WORKFLOW.md#queue-admission-loss-and-skips) |
| `skip_reason` | String | → attribute. Mechanism: [WORKFLOW.md § Queue admission, loss and skips](WORKFLOW.md#queue-admission-loss-and-skips) |
| `skipped` | Boolean | → attribute. Not stored on this table — trigger response payload only. Mechanism: [WORKFLOW.md § Queue admission, loss and skips](WORKFLOW.md#queue-admission-loss-and-skips) |

### Global Secondary Indexes

Three, all `ProjectionType.ALL`. Verified against `deploy-cdk/lib/data-stack.ts:163-216`.

#### StatusIndex
- **Keys**: `status` (HASH), `started_at` (RANGE) — `data-stack.ts:164-166`
- **Purpose**: Query all running/failed executions across pipelines
- **Projection**: ALL

#### run_id-index
- **Keys**: `run_id` (HASH) — no range key (`data-stack.ts:175-179`)
- **Purpose**: `run_id` is this table's **sort** key, so a run-only lookup has no key-bounded
  access path without this index. It serves `cancel_run`'s run lookup, which has only the run id
  (P28 R17.2)
- **Projection**: ALL

#### RunsByTimeIndex
- **Keys**: `pipeline_id` (HASH), `started_at` (RANGE) — `data-stack.ts:210-215`
- **Purpose**: Chronological run history. `pipelines_runs.get_pipeline_executions` delegates
  ordering to DynamoDB through this index instead of ordering by `run_id` and calling it
  "newest first" (p48 R5.2)
- **Projection**: ALL — **required, not preferred.** The read consumes `status`, `file_name`,
  `source_key`, `run_id`, `schema_hash`, `pending_approval_id`, `pending_approval_type` and
  `current_step`; `KEYS_ONLY` or `INCLUDE` would force a second read per row

> **Why the base table cannot serve run history.** The sort key is `run_id`, and several id
> prefixes share it — `e2e-` < `file-` < `queued-` < `retry-` < `run-` < `skipped-` < `unknown`.
> A descending read therefore groups **by kind** and is chronological only within one group: every
> duplicate skip pins above every run permanently, and the `queued-` placeholder sorts last.

> ⚠️ **`RunsByTimeIndex` is SPARSE, and that is a standing invariant of this system.** A GSI
> indexes only items carrying **both** key attributes, so **a row written without `started_at` is
> absent from this index and invisible in run history — silently, with no error.** This is the
> accepted cost of the chosen mechanism, not a defect in it, and it is the fact most worth knowing
> before you trust a run-history answer: a reader who knows the index exists but not that it is
> sparse will trust an incomplete result. Measured when this was recorded: 10 of 61 rows in the live
> executions table carried no `started_at`, all of them test or fixture rows.
>
> The exposure is made loud rather than designed out, two ways:
> `tests/unit/lambdas/test_executions_started_at_invariant.py` is an AST sweep that discovers
> executions-table writers by key shape (`{pipeline_id, run_id}` — the only such key schema in
> `data-stack.ts`), so a new writer is found automatically; and
> `scripts/detect_unindexed_executions.py` finds an unindexed row after the fact.
>
> **Do NOT use the sparseness as a filter.** The phantom-row filter stays where it is; the two
> predicates overlap only by accident and differ in both directions, which
> `tests/unit/lambdas/test_executions_index_sparseness.py::TestSparsenessIsNotAFilter` pins. A
> missing `started_at` is something to detect, never something to rely on.

> **Deploy note.** DynamoDB creates at most one GSI per table per stack update, and this table
> carries three. A fourth must be deployed on its own, and no two of these three may be created in
> the same stack update.

### Capacity

- **Billing Mode**: Pay-per-request (`PAY_PER_REQUEST`)
- **Streams**: Enabled (`NEW_IMAGE`)
- **TTL**: Enabled on `ttl`
- **Point-in-Time Recovery**: Enabled
- **Encryption**: AWS-managed SSE
- **Removal Policy**: RETAIN

### Example Record

```json
{
  "pipeline_id": "pipeline-customer-data",
  "run_id": "run-2025-01-15-12-00-00-abcd1234",
  "status": "running",
  "current_step": "code_generation",
  "steps_status": {
    "file_validation": "completed",
    "schema_extract": "completed",
    "schema_compare": "completed",
    "schema_mapping": "completed",
    "code_generation": "running"
  },
  "approval_status": null,
  "started_at": "2025-01-15T12:00:00Z",
  "completed_at": null,
  "file_name": "customers.json",
  "schema_version": "v3",
  "ai_model": "us.anthropic.claude-sonnet-4-6",
  "updated_at": "2025-01-15T12:05:30Z"
}
```

---

## 9. agentic_data_ingestion_task_tokens

> **NEW in Phase 4. Stores Step Functions task tokens separately from approval records.**

**Purpose**: Dedicated storage for Step Functions `waitForTaskToken` tokens with automatic TTL expiry. Decouples token lifecycle from approval metadata.

> **The 7-day TTL here is a backstop and is deliberately NOT the advertised approval window.** The
> advertised window is 72 h (§6). The two were once the same constant, and separating them is
> deliberate: a TTL equal to the window would race it, and a token reaped a moment early turns a
> decision that arrived in time into a callback that can never be delivered. Do not
> quote 7 days to an approver.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `approval_id` (PK) | String | Approval ID (matches `agentic_data_ingestion_approvals` PK) |
| `task_token` | String | Full, untruncated Step Functions task token (~668 chars) |
| `ttl` | Number | Unix epoch seconds, 7 days from creation (`TASK_TOKEN_TTL_DAYS`) — deliberately longer than the 72-hour approval window so a token outlives the wait rather than racing it |
| `created_at` | String | ISO 8601 timestamp |
| `cancel_requested_at` | String | Glue-callback rows only. Set by the dashboard's cancel: with `cancel_glue_job_run_id` when it stopped a running job, without it as a **fence** when it found no job yet. run_pipeline's two token-row writes are conditional on this attribute being absent, so a launcher already mid-invocation either never starts the job or stops the one it just started |
| `cancel_execution_arn` | String | Glue-callback rows only. The execution job_completion stops once the job has stopped |
| `cancel_glue_job_run_id` | String | Glue-callback rows only. The job run the cancel stopped; a completion for any other job run ignores the marker. Absent on a fence |
| `glue_job_run_id`, `glue_job_name` | String | Glue-callback rows only. Stamped by run_pipeline after `StartJobRun` returns, under the no-fence condition. On a fence row this is how job_completion knows which job the launcher started and stopped |

The table also holds one row per run waiting on its Glue job, keyed `glue_run#<run_id>` and
written by run_pipeline before the job starts (`LGR-0ec143c-1`). Its `task_token` is the
RunPipeline callback token.

### Access Pattern

- **Write**: Approval handler writes token on approval creation
- **Read + Delete**: Approve/reject handler reads token, calls `send_task_success`/`send_task_failure`, then deletes the record
- **Glue callback**: run_pipeline writes `glue_run#<run_id>`; job_completion reads it when Glue reports the job finished, sends the callback, then deletes it. If the dashboard's cancel marked the row, job_completion stops the execution instead of sending a callback (see `cancel_run` in `handlers/pipelines_runs.py`)
- **TTL Expiry**: Tokens auto-expire after 7 days if never processed — longer than the 72-hour approval window on purpose, since the gate abandons the execution at 72 h and the token must not be reaped before the deadline it backstops

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute
- **Removal Policy**: DESTROY

---

## 10. agentic_data_ingestion_ws_connections

> **Added in Phase 6 (WebSocket). Tracks active WebSocket connections for real-time dashboard updates.**

**Purpose**: Maps WebSocket connection IDs to pipeline subscriptions so the WebSocket broadcast Lambda can push updates to connected clients.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `connection_id` (PK) | String | API Gateway WebSocket connection ID |
| `pipeline_id` (SK) | String | Pipeline the client is subscribed to |
| `ttl` | Number | Unix epoch seconds, auto-expire stale connections |

### Global Secondary Indexes

#### PipelineIndex
- **Keys**: `pipeline_id` (HASH)
- **Purpose**: Find all connections subscribed to a pipeline
- **Projection**: ALL
- **Use Case**: Broadcast pipeline state changes to all connected clients

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute
- **Removal Policy**: DESTROY

---

## 11. agentic_data_ingestion_errors

> **Added in Phase 8. Centralized error tracking with TTL-based cleanup.**

**Purpose**: Stores pipeline and Lambda errors for dashboard error monitoring and troubleshooting.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `error_date` (PK) | String | Date partition key (YYYY-MM-DD) |
| `timestamp` (SK) | String | ISO 8601 timestamp |
| `pipeline_id` | String | Associated pipeline |
| `run_id` | String | Associated run |
| `step_name` | String | Step where error occurred |
| `error_type` | String | Error classification |
| `error_message` | String | Error details |
| `ttl` | Number | Unix epoch seconds for auto-deletion |

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute
- **Removal Policy**: DESTROY

---

## 12. agentic_data_ingestion_pipeline_locks

> **Added in Phase 11. Prevents concurrent pipeline executions.**

**Purpose**: Distributed locking mechanism to ensure only one execution runs per pipeline at a time. The TTL is a backstop for a lock whose execution vanished without any release path running; it is not how a normal run gives the lock back.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pipeline_prefix` (PK) | String | Lock key: the S3 source prefix for an upload-triggered pipeline, the `pipeline_id` for a JDBC one |
| `execution_name` | String | The Step Functions execution holding the lock. Every release is conditional on it |
| `started_at` | String | ISO 8601 timestamp of acquisition |
| `ttl` | Number | Unix epoch seconds, `LOCK_TTL_SECONDS` (11 days) after acquisition |

### Access pattern

- **Acquire**: `pipeline_trigger` puts the row with `attribute_not_exists(pipeline_prefix)`; a condition failure is the "already has a running execution" rejection.
- **Release**: always conditional on `execution_name = <the releasing execution>`, so a release can never remove a lock a later execution acquired. Three releasers:
  - the trigger's own failed-start release, when `StartExecution` raises after the acquire (`LGR-21a1b5b-1`);
  - the `ReleaseLockSuccess` / `ReleaseLockFailure` tasks in the state machine, which pass `$$.Execution.Name`;
  - the `SfnTerminalLockRelease` EventBridge rule on ABORTED / FAILED / TIMED_OUT, which reads `detail.name`.

  A condition failure (lock held by someone else, or no lock) returns `released: false, reason: not_owner` and is not an error. Until 2026-10-11 the state-machine and EventBridge releases deleted by key alone, and a late FAILED event from one execution could delete the lock the next execution was holding.
- **Pipeline delete** (`pipelines_crud` step 5e) deletes unconditionally; the pipeline is going away.

### Why 11 days

A DynamoDB TTL deletes the row whether or not its execution is still running, so the TTL must exceed the longest legitimate execution. The state machine's own timeout is 10 days (`EXECUTION_TIMEOUT_SECONDS`), sized to three 72-hour approval gates plus a 6-hour Glue job plus the container steps; the lock outlives that by a day. It was 24 hours until 2026-10-11, which deleted the lock from under any run parked at an approval gate for more than a day. `tests/cdk/stacks/test_lock_ttl_exceeds_execution_timeout.py` pins the ordering.

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute
- **Point-in-Time Recovery**: Enabled

---

## 13. agentic_data_ingestion_audit_log

> **Added in Phase 13. Tracks user actions for compliance and debugging.**

**Purpose**: Audit trail of user actions (approvals, pipeline edits, mapping changes, deletions) with TTL-based retention.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `resource_id` (PK) | String | Resource being acted on (pipeline_id, mapping_id, etc.) |
| `timestamp` (SK) | String | ISO 8601 timestamp |
| `action` | String | Action performed (approve, reject, edit, delete, create) |
| `user` | String | User who performed the action |
| `details` | Map | Action-specific metadata |
| `ttl` | Number | Unix epoch seconds for auto-deletion |

### Global Secondary Indexes

#### ActionIndex
- **Keys**: `action` (HASH), `timestamp` (RANGE)
- **Purpose**: Query all actions of a specific type
- **Projection**: ALL
- **Use Case**: Audit reports, compliance queries

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute
- **Removal Policy**: RETAIN

---

## 14. agentic_data_ingestion_agent_traces

> **Added in Phase 13a. Stores structured trace data from AI agent invocations.**

**Purpose**: Captures model calls, tool calls, token usage, cost estimates, and timing data from each AI agent step (schema mapping, code generation, code testing, Glue pipeline creation). Written by `AgentTracePlugin` (Strands `HookProvider`). Displayed in the dashboard's Agent Trace Timeline.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `run_id` (PK) | String | Pipeline execution run ID |
| `step_name` (SK) | String | Pipeline step (`schema_mapping`, `code_generation`, `code_testing`, `glue_pipeline_creation`) |
| `pipeline_id` | String | Pipeline identifier |
| `trace_data` | Map | Full trace: `model_calls`, `tool_calls`, token counts, cost, timing |
| `timestamp` | String | ISO 8601 when trace was written |
| `ttl` | Number | Auto-expire after 90 days |

### Access Patterns

- `GetItem(run_id, step_name)` — single step trace for the run detail page
- `Query(run_id)` — all step traces for a run (aggregate view)

No GSIs needed — the primary access pattern is always by `run_id`.

### trace_data Structure

```json
{
  "model_calls": [
    {
      "model_id": "us.anthropic.claude-sonnet-4-6",
      "input_tokens": 1250,
      "output_tokens": 340,
      "cache_read_tokens": 800,
      "duration_ms": 2340,
      "prompt_preview": "Generate PySpark transform...",
      "response_preview": "df_transformed = df.select(..."
    }
  ],
  "tool_calls": [
    {
      "tool_name": "create_glue_job",
      "duration_ms": 1200,
      "status": "success",
      "tool_params": "{\"job_name\": \"...\"}",
      "result_preview": "{\"job_name\": \"...\"}"
    }
  ],
  "total_input_tokens": 1250,
  "total_output_tokens": 340,
  "total_cache_read_tokens": 800,
  "estimated_cost_usd": 0.0089,
  "start_time": 1710500000.0,
  "end_time": 1710500005.0,
  "error": null
}
```

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` attribute (90-day retention)
- **Removal Policy**: RETAIN

### Environment Variable

`AGENT_TRACES_TABLE` — set on all 4 agent Lambdas in `compute-stack.ts`.

---

## 15. agentic_data_ingestion_pipeline_watermarks

> **Added in P17 (JDBC data source). Stores the incremental-load watermark for each JDBC-source pipeline.**

**Purpose**: Tracks the last-processed value of the configured watermark column so incremental JDBC pipelines only pull rows newer than the previous run. Read and written per run by the generated incremental PySpark template (`pyspark_templates/glue_job_jdbc_source.py.j2`) via `get_item` / `put_item`. The Glue ETL job role (`gluePipelineRole`) already has read/write access through the shared DynamoDB `agentic_data_ingestion*` wildcard — no scoped grant is required.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pipeline_id` (PK) | String | Pipeline identifier |
| `watermark_column` | String | Source column used for the incremental high-water mark |
| `watermark_type` | String | Column type (e.g. `timestamp`, `numeric`) |
| `last_value` | String | Last-processed watermark value |
| `last_updated_at` | String | ISO 8601 when the watermark was last written |
| `last_run_id` | String | Run ID that produced the current watermark |

Non-key attributes are schemaless in DynamoDB; only `pipeline_id` is declared in CDK.

### Access Patterns

- `GetItem(pipeline_id)` — read the prior watermark at the start of a run (a missing item is the documented first-run case → read full table)
- `PutItem(pipeline_id)` — persist the new watermark at the end of a successful run

No GSIs needed — the watermark item is keyed solely by `pipeline_id`.

### Capacity

- **Billing Mode**: Pay-per-request
- **Encryption**: AWS-managed SSE
- **Point-in-time Recovery**: Enabled
- **Removal Policy**: RETAIN (created empty on first deploy)

### Environment Variable

`WATERMARKS_TABLE` — passed to the Glue job as the `watermarks_table_name` argument via `create_glue_pipeline/job_arguments.py`.

---

## 16. agentic_data_ingestion_processed_files

> **Added in p30 (R9.1) as the Processed_File_Ledger. Content-level idempotency for S3-sourced pipelines.**

**Purpose**: Records the content fingerprint of every S3 object that has completed a successful run, so a re-upload of the same content is skipped instead of re-ingested. The per-pipeline lock in `pipeline_trigger` serialises runs but cannot stop the *same content* being ingested twice (`_execution_name` embeds a timestamp on purpose, since Step Functions rejects a reused name), so against an append-mode target a re-upload would duplicate rows. Written by `job_completion` on the Glue SUCCEEDED callback — the only place terminal write success is known — and read by `pipeline_trigger` **before it acquires the pipeline lock**, so a duplicate skip leaves no lock to release (R10.3). Fingerprint composition and both access helpers live in `shared/processed_file_ledger.py` so the writing and reading Lambdas cannot drift apart.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `pipeline_id` (PK) | String | Pipeline identifier — the same content submitted to two pipelines is two legitimate ingestions, so the pipeline is part of the key, not a filter |
| `fingerprint` (SK) | String | `sha256("{size}:{normalized_etag}")`, 64 hex chars (`content_fingerprint()`) |
| `run_id` | String | Run that recorded the fingerprint |
| `recorded_at` | String | ISO 8601 when the row was written |
| `content_size` | Number | Object size — half the fingerprint's pre-image, stored so a match or miss is diagnosable without recomputing the digest |
| `content_etag` | String | Normalised S3 ETag (quotes stripped, lowercased) — the other half of the pre-image |
| `source_key` | String | S3 key of the object that produced the row |
| `ttl` | Number | Unix epoch seconds, 30 days from write (`config.PROCESSED_FILE_LEDGER_TTL_SECONDS`) |

Only `pipeline_id` and `fingerprint` are declared in CDK; the rest are schemaless. `content_size`, `content_etag` and `source_key` are written only when available.

There is deliberately **no status attribute** — rows exist only for successful runs, so existence *is* the success record. A provisional "started" row would have to be distinguished from a completed one on every read, and getting that wrong once would permanently block retries of a file whose run failed.

### Access Patterns

- `GetItem(pipeline_id, fingerprint)` — `pipeline_trigger`'s duplicate check before the lock (`find_processed_file()`). Fails **open**: a throttle, missing table or IAM gap costs the dedup, never the ingestion.
- `PutItem(pipeline_id, fingerprint)` — `job_completion` on Glue SUCCEEDED (`record_processed_file()`). Last-writer-wins with no condition, so a deliberate re-run refreshes `recorded_at` and the TTL instead of erroring.

No GSIs and no stream, both on purpose: the only access pattern is a keyed get by (`pipeline_id`, `fingerprint`), and nothing should react to a row appearing.

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `ttl` (30 days)
- **Encryption**: AWS-managed SSE
- **Point-in-Time Recovery**: Enabled
- **Removal Policy**: RETAIN

### Environment Variable

`PROCESSED_FILES_TABLE` — set on `agentic_data_ingestion_job_completion` (`compute-stack.ts`) and on `PipelineTrigger` (`orchestration-stack.ts`).

### Naming Fence

The construct ID `ProcessedFilesTable` **must never be renamed**. RETAIN plus a hardcoded physical name means a new logical ID would make CloudFormation try to create a second table with the same name in the same stack. Same fence as `PipelineWatermarksTable`, which was deliberately added under its pre-existing logical ID to avoid exactly that collision.

JDBC sources have no file, therefore no fingerprint and no ledger row — this is an S3-content mechanism only. For multipart uploads the S3 ETag depends on the uploader's part size, so a genuine duplicate uploaded a different way produces a different fingerprint: the failure mode is a missed skip, never a wrong skip.

---

## 17. agentic_data_ingestion_model_call_budget

> **Added in P28 (R15.2). Cross-Lambda per-run model-call counter backing `CallBudget` in `shared/model_config.py`.**

**Purpose**: Holds one counter item per run so the per-run model-call budget holds across processes — a Lambda container, a second Lambda, and the AgentCore agent containers all increment the same item instead of each enforcing a private in-memory count. Every counted model call performs an atomic `ADD call_count 1` and enforces against the returned per-run total; exceeding the ceiling raises a permanent `StepFailure` ("model call budget exceeded"), which fails the step. These are ephemeral counters, so unlike most tables here the removal policy is DESTROY.

### Schema

| Attribute | Type | Description |
|-----------|------|-------------|
| `run_id` (PK) | String | Pipeline execution run ID — one counter item per run |
| `call_count` | Number | Model calls counted so far this run; incremented with an atomic `ADD` |
| `expires_at` | Number | Unix epoch seconds, 8 days, set with `if_not_exists` on the first increment so later increments do not extend it |

No sort key. Only `run_id` is declared in CDK; `call_count` and `expires_at` are schemaless.

The 8-day TTL (`model_config.BUDGET_ITEM_TTL_DAYS`) is pinned rather than derived from the approval window: what the item has to outlive is a *run*, and a run can be long for reasons unrelated to approvals (a slow Glue job, retries, a resumed execution). Reaping it early would silently reset a cross-Lambda budget mid-run.

### Access Patterns

- `UpdateItem(run_id)` with `ADD call_count :inc SET expires_at = if_not_exists(expires_at, :exp)` — one per model call, `ReturnValues=UPDATED_NEW` so the caller enforces against the fresh per-run total
- No separate read path and no GSIs — the only value consumed is the total returned by the increment

The increment fails **open**: on error it is logged loudly and enforcement degrades to that process's in-memory count for that call, rather than failing an otherwise-healthy model call. The table is also optional — with `MODEL_CALL_BUDGET_TABLE` unset, `CallBudget` enforces in-memory only.

### Capacity

- **Billing Mode**: Pay-per-request
- **TTL**: Enabled on `expires_at` (8 days)
- **Encryption**: DynamoDB default — this table deliberately does not take the stack's `tableDefaults`
- **Point-in-Time Recovery**: Not enabled
- **Removal Policy**: DESTROY

### Environment Variables

- `MODEL_CALL_BUDGET_TABLE` — set on `agentic_data_ingestion_schema_mapping` (`compute-stack.ts`, which also grants that Lambda `dynamodb:UpdateItem` on this table) and on every AgentCore agent container via `agentContainerEnv` (`agentcore-stack.ts`); the runtime role already carries `UpdateItem` on `agentic_data_ingestion*`.
- `MODEL_CALL_BUDGET` — the ceiling. Default 50 (`model_config.DEFAULT_MODEL_CALL_BUDGET`).

---

## Access Patterns

### Common Query Patterns

1. **Get all steps for a run**:
   - Table: `agentic_data_ingestion_state`
   - Index: Base table (PK=`run_id`)
   - Query: `run_id = 'run-2025-01-15-12-00-00-abcd1234'`

2. **Get pipeline execution list** (fast path):
   - Table: `agentic_data_ingestion_executions`
   - Index: Base table (PK=`pipeline_id`)
   - Query: `pipeline_id = 'pipeline-customer-data'`, ScanIndexForward=False

3. **Get pipeline execution history** (detailed):
   - Table: `agentic_data_ingestion_state`
   - Index: `PipelineIndex` GSI
   - Query: `pipeline_id = 'pipeline-customer-data'`

4. **Get active pipelines**:
   - Table: `agentic_data_ingestion_pipelines`
   - Index: `status-created_at-index`
   - Query: `status = 'active'`

5. **Get pending approvals**:
   - Table: `agentic_data_ingestion_approvals`
   - Index: `status-created-index`
   - Query: `status = 'pending'`

6. **Find mappings written for a given schema pair** (provenance lookup, **not** a reuse cache):
   - Table: `agentic_data_ingestion_schema_mappings`
   - Index: `schema-pair-index`
   - Query: `source_schema_hash = 'sha256:abc...' AND target_schema_hash = 'sha256:def...'`
   - ⚠️ This returns rows that *exist*, not a mapping cleared for use. **Mapping reuse is not a
     hash-pair lookup.** Reuse runs through *approved* mappings only — AgentCore mapping memory as
     prompt guidance, and freshest-approved-mapping substitution at code generation — and taking the
     reuse path still passes the human approval gate.
     See [DATABASE-ARCHITECTURE.md](DATABASE-ARCHITECTURE.md) §Relationship Details 4

7. **Get task token for approval**:
   - Table: `agentic_data_ingestion_task_tokens`
   - Index: Base table (PK=`approval_id`)
   - Pattern: Read → use → delete (one-time use)

8. **Get run history in chronological order**:
   - Table: `agentic_data_ingestion_executions`
   - Index: `RunsByTimeIndex` GSI
   - Query: `pipeline_id = 'pipeline-customer-data'`, `ScanIndexForward=False`
   - ⚠️ Sparse, so a row with no `started_at` is absent from the answer, silently. See §8

9. **Look up an execution by run id alone**:
   - Table: `agentic_data_ingestion_executions`
   - Index: `run_id-index` GSI (`run_id` is the base table's *sort* key, so this is the only
     key-bounded path)
   - Query: `run_id = 'run-2025-01-15-12-00-00-abcd1234'`

10. **Count or list state rows for one step**:
    - Table: `agentic_data_ingestion_state`
    - Index: `step_name-timestamp-index` GSI
    - Query: `step_name = 'file_validation'`, optionally bounded on `timestamp`
    - Sparse — a state row with no `timestamp` is absent from the index

11. **Find the approvals belonging to a run**:
    - Table: `agentic_data_ingestion_approvals`
    - Index: `run_id-index` GSI (PK-only)
    - Query: `run_id = 'run-20241112-abc123'`

---

## Best Practices

### Data Retention

- **State Table**: Keep 90 days of execution history
- **Generated Code**: Keep all versions indefinitely
- **Test Results**: Keep 30 days of test history
- **Approvals**: Auto-delete after 30 days (TTL)
- **Notifications**: Auto-delete after 30 days (TTL)

### Performance Optimization

- Use GSIs for all query patterns
- Batch write operations when possible
- Use consistent read only when necessary
- Bound every paginating read; there is no provisioned capacity to tune

### Capacity mode

**Every table is `PAY_PER_REQUEST`** — no exceptions, so the claim needs no count to be checkable.
Read against `deploy-cdk/lib/data-stack.ts`, they reach it three different ways, and the three
groups below are exhaustive:

- **11 inherit it** from the stack's `tableDefaults` (`:38-43`) and set no `billingMode` of their
  own — `state`, `pipelines`, `schema_mappings`, `generated_code`, `test_results`, `approvals`,
  `notifications`, `executions`, `pipeline_locks`, `processed_files`, `pipeline_watermarks`.
- **4 set it explicitly and take no defaults** — `task_tokens` (`:221`), `ws_connections` (`:231`),
  `errors` (`:246`), `model_call_budget` (`:364`).
- **2 take the defaults and restate it anyway** — `audit_log` (`:306`), `agent_traces` (`:323`).

`PROVISIONED` appears nowhere in the stack, and no table declares `readCapacity`, `writeCapacity`
or auto-scaling.

There is therefore **no read/write-unit arithmetic to do here, and no provisioned-capacity
recommendation to follow.** An earlier revision of this section recommended provisioned capacity
for high-traffic tables and auto-scaling for provisioned tables; both are inapplicable to this
deployment and following them would mean changing the billing mode of a live table rather than
tuning one. Cost control on these tables is request volume — bound the reads — plus TTL on the
tables that carry one.

### Cost Optimization

- Bound paginating reads and prefer a keyed Query over a Scan; request count is the cost
- Set appropriate TTL for temporary data
- Keep GSI projections no wider than a reader needs, except where a narrower projection would force
  a second read per row (`RunsByTimeIndex` is the documented exception)

---

## Monitoring

### CloudWatch Metrics

Monitor these key metrics:
- `ConsumedReadCapacityUnits`
- `ConsumedWriteCapacityUnits`
- `UserErrors` (throttling)
- `SystemErrors`
- `SuccessfulRequestLatency`

### Alarms

Set alarms for:
- Throttled requests > 0
- User errors > 10/minute
- Latency > 100ms (p99)

There is no "capacity > 80%" alarm to set: with `PAY_PER_REQUEST` there is no provisioned ceiling to
sit at 80% of. Throttling under on-demand comes from exceeding a table's account or partition
throughput limit, which the throttled-request alarm above already covers.

---

## Migration and Backup

### Point-in-Time Recovery

All critical tables have PITR enabled:
- Recovery window: 35 days
- Restore to any point in time
- No performance impact

### Backup Strategy

- **Automated**: PITR for all tables
- **Manual**: On-demand backups before major changes
- **Cross-Region**: Consider for disaster recovery

---

## Related Documentation

- [Architecture Overview](ARCHITECTURE.md)
- [Lambda Functions](LAMBDA-FUNCTIONS.md)
- [Database Architecture](DATABASE-ARCHITECTURE.md)
- [Workflow](WORKFLOW.md)
