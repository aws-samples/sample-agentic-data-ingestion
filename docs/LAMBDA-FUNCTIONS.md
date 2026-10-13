# Lambda Functions Documentation

## Overview

This document details the Lambda functions organized into 7 functional groups. It does **not** cover
every function, and the exclusions are enumerated here so a reader can tell an omission from a gap:

- The `agent_dispatch` and `self_healing_dispatch` AgentCore dispatchers are excluded. The route they
  serve is described in [AGENTS.md](AGENTS.md), which explains why every event-driven agent
  invocation goes through a dispatch Lambda rather than reaching AgentCore directly. Note that
  AGENTS.md describes that mechanism in prose and does not carry a per-function section for either.
- `record_failure` is excluded, and it is documented under
  [ARCHITECTURE.md § Processing Layer](ARCHITECTURE.md#3-processing-layer) rather than in AGENTS.md.

Being deployed from `orchestration-stack.ts` is **not** an exclusion criterion. Two
Orchestration-stack functions have sections below, `pipeline_trigger` and `queue_loss_recorder`,
because they are pipeline-execution machinery rather than agent plumbing. All functions use the
Python 3.12 runtime on arm64.

The function total, the Compute-versus-Orchestration split and their derivations are stated once in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory), which also links the counting
rule for the Orchestration side. No count is restated here, because a partial reference stating a
total invites a reader to conclude the missing rows do not exist.

**Most, but not all, carry the Strands Agents layer.** The exceptions are enumerated below rather
than counted, and each is load-bearing rather than incidental:

- `schema_extract` is `[sharedBaseLayer, pyarrowLayer]` — `compute-stack.ts:522`, a ratified
  decision. It imports no `strands`, `bedrock_agentcore` or `mcp` package at any depth, and
  the swap exists because with Strands attached the function sat 0.5% under the 250 MiB unzipped
  ceiling. Adding a Strands import here is a failed deploy, not a slow one.
- `pipeline_trigger` is `[adotLayer, powertoolsLayer]` — `orchestration-stack.ts:1672`.
- `queue_loss_recorder` is `[powertoolsLayer]` alone (`orchestration-stack.ts:1784`). That layer
  carries `shared/*.py` at `/opt/python`, which is how the handler resolves its TTL constant from
  `shared/config.py`.

### Configuration provenance

Every **Configuration** block below was read against CDK at commit `768d884` and cites the
declaring `file:line`. Timeout and memory come from `deploy-cdk/lib/compute-stack.ts`, except
`pipeline_trigger`, which is declared in `deploy-cdk/lib/orchestration-stack.ts`. Re-derive rather
than trust these figures if the stacks have moved since: `grep -n 'functionName\|timeout: cdk\|memorySize' deploy-cdk/lib/compute-stack.ts`.

### Output contract provenance

The **Output** blocks are illustrative examples, and three of three sampled at `768d884` did not
match their handler. Only `file_validation`, `schema_extract` and `schema_compare` have since been
read key-by-key against the code; every other output block carries

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

Treat a marked block as a sketch of intent, not as a contract, and read the handler before you
write a consumer against it. The marker's absence means the keys were checked; it does not mean the
example values are real.

---

## Table of Contents

1. [Pipeline Management Functions](#1-pipeline-management-functions)
2. [Data Processing Functions](#2-data-processing-functions)
3. [Code Generation & Testing Functions](#3-code-generation--testing-functions)
4. [Infrastructure Functions](#4-infrastructure-functions)
5. [Approval System Functions](#5-approval-system-functions)
6. [API Functions](#6-api-functions)
7. [WebSocket & Concurrency Functions](#7-websocket--concurrency-functions)
8. [Common Patterns](#common-patterns)
9. [Error Handling](#error-handling)

---

## 1. Pipeline Management Functions

### agentic_data_ingestion_run_pipeline

**Purpose**: Executes Glue job runs for a pipeline.

**Trigger**: Step Functions — the `RunPipeline` state, a
`arn:aws:states:::lambda:invoke.waitForTaskToken` task (`orchestration-stack.ts:901-918`). **Not API
Gateway.** The step holds the token until the Glue job's completion callback resolves it, and its
`TimeoutSecondsPath` is supplied by the `CheckJobTimeoutConfig` / `ApplyDefaultJobTimeout` guard
directly upstream.

**Input** (the state's `Payload`, `orchestration-stack.ts:907-911`):
```json
{
  "run_id": "run-2026-08-31-10-00-00-abc123",
  "pipeline_id": "pipeline-customer-data-etl",
  "taskToken": "<$$.Task.Token>"
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "run_id": "jr_abc123",
  "status": "RUNNING",
  "start_time": "2024-11-12T10:00:00Z"
}
```

**Configuration** (verified — `compute-stack.ts:873-874`):
- Timeout: 300 seconds
- Memory: 512 MB
- Permissions: `glue:StartJobRun`, `glue:GetJobRun`

---

### agentic_data_ingestion_job_completion

**Purpose**: Handles Glue job completion callbacks and updates state.

**Trigger**: EventBridge (Glue job state change)

**Input**:
```json
{
  "detail": {
    "jobName": "pipeline-customer-data-etl-job",
    "jobRunId": "jr_abc123",
    "state": "SUCCEEDED",
    "completedOn": "2024-11-12T10:15:00Z"
  }
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "pipeline_id": "pipeline-customer-data-etl",
  "run_id": "jr_abc123",
  "status": "completed",
  "duration_seconds": 900
}
```

**Configuration** (`compute-stack.ts:948-949`):
- Timeout: 300 seconds
- Memory: 512 MB
- Permissions: `dynamodb:UpdateItem`, `eventbridge:PutEvents`

---

## 2. Data Processing Functions

### agentic_data_ingestion_file_validation

**Purpose**: Validates uploaded files for format, size, and structure.

**Trigger**: Step Functions (Step 1)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "file_uri": "s3://source-bucket/customers/data.parquet",
  "expected_format": "parquet"
}
```

**Output** — verified against the handler. **None of `validation_status`, `file_size_mb`,
`record_count`, `format` or `next_step` exists**; they were documented here for a shape the handler
never returned. There are three return arms and they do not carry identical key sets:

*Single-object S3 mode* (`lambda_function.py:789-806`):
```json
{
  "statusCode": 200,
  "run_id": "run-2026-08-31-10-00-00-abc123",
  "original_pipeline_id": "pipeline-customer-data-etl",
  "source_format": "parquet",
  "file_size_bytes": 131596288,
  "file_count": 1,
  "retry_from_step": "",
  "body": "{\"message\": \"File processing completed successfully\", \"status\": \"success\", ...}"
}
```

*Prefix mode* (`lambda_function.py:407-421`) — the same keys, plus `source_s3_uri` and
`trigger_mode: "prefix"`, with `file_count` the number of listed objects and `file_size_bytes` their
sum. It carries **no** `body`.

*JDBC source mode* (`lambda_function.py:537-554`) — `source_format` is always `"jdbc"`,
`file_size_bytes` `0` and `file_count` `1`; no S3 object is read.

`retry_from_step` is `RESUME_MARKER_KEY` (`shared/retry_utils.py:70`) and is **always a string**,
`""` when the run is not a retry. That is deliberate: every downstream `Payload` resolves
`$.file_validation_result.retry_from_step`, and an absent path there is a
`States.ParameterPathFailure` that neither Retry nor Catch can intercept.

**Validation Checks**:
- Extension recognised and resolved to a canonical format via `FORMAT_MAP`
  in `lambda_function.py` — `csv` (`.csv`, `.tsv`, `.txt`), `json` (`.json`,
  `.jsonl`, `.ndjson`), `parquet` (`.parquet`, `.pq`), `avro`, `orc` and
  `excel` (`.xlsx`),
  with a `.gz`/`.bz2`/`.snappy`/`.zst` suffix stripped first. An unrecognised
  extension fails the step permanently, naming the recognised list.
- File size (< 5 GB)
- Record count estimation

This step issues a HEAD only and reads no object bytes on any format, `.xlsx`
included, so it performs no structure validation and no conversion. Whether the
*original* object is readable by a Glue job is decided by `NATIVE_FORMATS` in
the same module; a canonical format outside that set — `excel` — has its source
object converted at `schema_extract`.

**Configuration** (`compute-stack.ts:473-474`):
- Timeout: 300 seconds
- Memory: 512 MB
- Permissions: `s3:GetObject` (HeadObject authorizes as GetObject)

---

### agentic_data_ingestion_schema_extract

**Purpose**: Extracts schema from source data files.

**Trigger**: Step Functions (Step 2)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "file_uri": "s3://source-bucket/customers/data.parquet",
  "format": "parquet"
}
```

**Output** — verified against the handler. The keys are `extracted_schema` and `schema_hash`, not
`source_schema` / `source_schema_hash`; there is no `next_step`, and **no `field_count`** (see the
note below). S3 arm, `lambda_function.py:2868-2884`:
```json
{
  "statusCode": 200,
  "run_id": "run-2026-08-31-10-00-00-abc123",
  "original_pipeline_id": "pipeline-customer-data-etl",
  "schema_hash": "a1b2c3...",
  "extracted_schema": {"type": "object", "properties": {"cust_id": {"type": "string"}}},
  "json_s3_uri": "s3://source-bucket/customers/data.parquet",
  "source_sample_uri": "s3://processed-bucket/samples/pipeline-customer-data-etl.csv",
  "readable_source_uri": "s3://source-bucket/customers/data.parquet",
  "readable_source_format": "parquet",
  "processed_worksheet": "",
  "source_format": "parquet",
  "message": "Schema extraction completed successfully"
}
```

The JDBC arm (`lambda_function.py:2438-2461`) returns the same keys and one more, `sample_s3_uri`,
which no ASL path reads — it is the honest "is there a sample" signal and is `""` when there is none.

**`source_sample_uri` is present on BOTH arms, on every run, and must stay so** — a standing
invariant. ASL has no coalesce and no conditional intrinsic, so a `Payload` reading a
sometimes-absent path raises `States.ParameterPathFailure`, which neither Retry nor Catch can
intercept. The preference (Sample_Artifact when one exists, canonical source URI otherwise) is
therefore resolved in Python, not in the payload mapping. `readable_source_uri` /
`readable_source_format` and `processed_worksheet` are echoed unconditionally for the same reason —
`""` on the arm where they are meaningless, never absent.

> **`field_count` is three-state, and it is not returned here.** This step returns no `field_count`;
> the figure appears in the state row's `output_data.summary`
> (`schema_extract/lambda_function.py:1920`) and on schema-listing rows served by
> `GET /api/schemas`. On a listing row it is **a positive number** (the source stated a count), **`0`**
> (the source stated zero — which may refuse the schema), or **`null`** (no count was established —
> every uploaded and every S3 Tables row). **`null` is never collapsed to `0`**: a
> hand-written `fields?.length ?? 0` reports "no count was established" as "declares nothing" and
> refuses every schema a user uploads. The expression lives once, in
> `dashboard-react/src/utils/schema-field-count.ts`.

**Extraction Methods**:
- **Parquet**: PyArrow footer metadata via a seekable `pyarrow.fs.S3FileSystem` handle
- **JSON**: infer from records parsed out of the first 64 KB (ranged GET)
- **CSV**: infer from up to the first 100 rows parsed out of a 64 KB ranged GET, split on the delimiter the object's extension implies (so `tsv` and `txt` are served here too). Was S3 Select with this read as a fallback; that arm is gone — see [ARCHITECTURE.md](ARCHITECTURE.md#streaming-schema-extraction-phase-9)
- **Excel**: openpyxl over the whole object — one field per column of the
  processed worksheet, named from that column's header cell — **plus a
  conversion of the source object to CSV**, because Spark and Glue have no Excel
  datasource
- **Avro**: read from the OCF header's `avro.schema` metadata (one bounded ranged GET)
- **ORC**: read from the postscript + footer via a seekable `pyarrow.fs.S3FileSystem` handle

There is one dispatch branch per canonical format: `csv` (`.csv`, `.tsv`,
`.txt`), `json` (`.json`, `.jsonl`, `.ndjson`), `parquet` (`.parquet`, `.pq`),
`avro` (`.avro`), `orc` (`.orc`) and `excel` (`.xlsx`) — the value set of `FORMAT_MAP`
in `file_validation`.

**Excel is the one canonical format a Glue job cannot read from the source
object, and this Lambda is where the conversion happens.** The gate is
`NATIVE_FORMATS` in `file_validation` — the canonical formats a Glue job reads
directly from the source object, with no prior conversion of that object. `csv`,
`json`, `parquet`, `avro` and `orc` are members; `excel` is not, so the `excel`
branch writes a converted CSV to the processed bucket and reports its URI as
`readable_source_uri`, with `readable_source_format` beside it. That pair is on
every result this step returns, native or converted, so `create_glue_pipeline`
and the templates consume a resolved value and never re-derive the gate. The
conversion sits here because this step already opens the workbook to produce the
schema. `openpyxl==3.1.5` is pinned in `shared/runtime_requirements.txt` and
installed into the layer this function attaches.

**A multi-sheet workbook needs a worksheet selection.** The selection is
pipeline configuration (`selected_worksheet`, set in the Create Pipeline
Wizard's Source step) and is read once per run, which is what makes every
subsequent run process the same sheet. One worksheet and no selection is
processed as-is; more than one worksheet and no selection **fails the run** with
every worksheet name in the message; a selection naming a worksheet the workbook
lacks **fails the run**, reporting the configured name and the names present. A
`.xlsx` that cannot be opened as a workbook fails the run naming the object and
the reason, rather than returning an empty schema.

> **Not yet followable:** the worksheet selection is write-only on the API. The
> wizard sends it and pipeline creation stores it, but no API read path returns
> it and there is no post-creation edit path, so a user cannot see or change
> what they set — even though the refusal message tells them to set it on the
> pipeline. Changing a selection currently means recreating the pipeline.
>
> None of the Excel behaviour above has been observed end to end; it describes
> the code in this tree.

**Avro and ORC schemas are read from file metadata**, so the field list is what
the writer recorded rather than what sampled rows suggest. Avro's header is at
the start of the object and is reached with a single bounded ranged GET; ORC's
footer is at the end, so it needs a seekable handle. Neither downloads the
object or writes to `/tmp`, and neither returns an empty schema — unreadable
metadata fails the run at `schema_extract`, and an externally compressed
container (`.avro.gz`, `.orc.zst`) is refused with the compression named as the
cause.

**Configuration** (`compute-stack.ts:501-502`, layers at `:522`):
- Timeout: 900 seconds (15 minutes — the Lambda maximum). The JDBC path starts a Glue crawler and
  polls it to completion in-process, and a crawl's wall-clock time routinely exceeded the old
  5-minute ceiling.
- Memory: 512 MB
- Layers: `[sharedBaseLayer, pyarrowLayer]` — **not** the Strands layer (see
  [Overview](#overview))
- Permissions: `s3:GetObject`

---

### agentic_data_ingestion_schema_compare

**Purpose**: Compares source schema with target schema to detect drift.

**Trigger**: Step Functions (Step 3)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "source_schema_hash": "sha256:abc123...",
  "target_schema": {...}
}
```

**Output** — verified against the handler. **None of `drift_detected`, `new_fields`,
`removed_fields`, `type_changes`, `compatibility` or `next_step` exists.** The comparison detail
lives nested under `comparison_result`. Main arm, `lambda_function.py:837-848`:
```json
{
  "statusCode": 200,
  "run_id": "run-2026-08-31-10-00-00-abc123",
  "original_pipeline_id": "pipeline-customer-data-etl",
  "schema_hash": "a1b2c3...",
  "previous_schema_hash": "d4e5f6...",
  "action": "use_existing_pipeline",
  "comparison_result": {
    "is_identical": true,
    "is_compatible": true,
    "differences": [],
    "summary": {"identical_schemas": true, "target_schema_changed": false},
    "action": "use_existing_pipeline",
    "existing_mapping_id": "mapping-abc123"
  },
  "existing_mapping_id": "mapping-abc123",
  "target_schema_changed": false,
  "message": "Schema comparison completed - action: use_existing_pipeline"
}
```

The first-schema arm (`lambda_function.py:637-646`) omits `previous_schema_hash` and
`existing_mapping_id`.

**The state machine branches on `action` and `existing_mapping_id`, so both are contract, not
telemetry.** `CheckSchemaAction` (`orchestration-stack.ts:922-937`) takes the reuse branch only on
`And[action == "use_existing_pipeline", IsPresent(existing_mapping_id), existing_mapping_id != ""]`
and otherwise falls through to `SchemaMapping` — the same fail-closed shape as the governance gates,
which is a standing invariant. `existing_mapping_id` is `""` rather than absent when no approved
mapping was found, and `$.schema_compare_result.existing_mapping_id` is also read as `mapping_id` by the
`ReuseExistingMapping` Pass (`orchestration-stack.ts:446`).

The four `action` values the handler can emit: `use_existing_pipeline` (`:688`), `remap_required`
(`:602`, `:669`), `update_existing_pipeline` (`:793`) and `create_new_pipeline` (`:600`, `:781`,
`:803`). Only a mapping with `status == "success"` — approved — is a reuse candidate; an identical
schema with no approved mapping is forced to `create_new_pipeline`. That filter fixed a real defect:
the lookup used to take the newest `schema_mapping` row carrying a `mapping_id` whatever its status,
so a human-rejected mapping was reusable, while a pipeline whose latest mapping had been rejected
lost access to its last approved one. Do not widen the filter to admit a row with no `status`: no
writer produces one, and a permissive clause there would make an unknown-status row eligible on a
governance path.

**Drift Detection**:
- New fields in source (`field_added`)
- Removed fields from source (`field_removed` — breaking)
- Declared type changes (`type_mismatch` — breaking unless the change is a listed widening, or
  involves `null`, which the extractors use for "no values observed" rather than as a type claim)
- Other value changes (`value_changed`)
- Compatibility assessment: `is_compatible` is false when any `field_removed` or `type_mismatch`
  difference is present (`lambda_function.py:338`)

**Configuration** (`compute-stack.ts:576-577`):
- Timeout: 300 seconds
- Memory: 512 MB
- Permissions: `dynamodb:GetItem`, `s3:GetObject`

---

### agentic_data_ingestion_schema_mapping

**Purpose**: Creates intelligent field mappings using AI.

**Trigger**: Step Functions (Step 4)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "source_schema": {...},
  "target_schema": {...}
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "mappings": [
    {
      "source_field": "cust_id",
      "target_field": "customer_id",
      "transformation": "DIRECT",
      "confidence": 0.98
    }
  ],
  "overall_confidence": 0.95,
  "unmapped_source_fields": ["legacy_field"],
  "unmapped_target_fields": ["status"],
  "approval_required": false
}
```

**AI Integration**:
- Uses Bedrock Converse API (model-agnostic) for semantic matching
- Supports mapping context from previous runs (P15 rerun-mapping)
- Confidence scoring per mapping
- Transformation detection
- Auto-cast in PySpark templates for type mismatches (STRING→INT, STRING→DATE)
- Fallback to rule-based matching

**Approval Logic**:
- Confidence < 0.8 (`CONFIDENCE_AUTO_APPROVE_THRESHOLD`, `shared/config.py`): Requires approval
- Unmapped required fields: Requires approval
- Complex transformations: Requires approval
- Mapping memory: stores approved mappings only (not at generation time)

**Configuration** (`compute-stack.ts:592-593`):
- Timeout: 600 seconds (10 minutes)
- Memory: 1024 MB
- Permissions: `bedrock:InvokeModel`, `dynamodb:PutItem`

---

## 3. Code Generation & Testing Functions

### agentic_data_ingestion_create_code

**Purpose**: Generates PySpark ETL code using Strands Agent and Dual MCP.

**Trigger**: Step Functions (Step 5)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "mappings": [...],
  "source_config": {...},
  "target_config": {...}
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "code_version_id": "code-20241112-xyz789",
  "code_s3_uri": "s3://generated-code/pipeline-customer-data-etl/code/v1/etl_script.py",
  "validation_status": "valid",
  "quality_score": 95,
  "required_params": ["SOURCE_S3_PATH", "TARGET_S3_PATH"],
  "optional_params": ["PARTITION_KEYS"],
  "mcp_enhanced": true
}
```

**AI Integration**:
- **Strands Agent**: Orchestrates code generation
- **Data Processing MCP**: Provides optimization recommendations
- **S3 Tables MCP**: Generates Iceberg-specific code
- **Bedrock Claude Sonnet 4.6**: Primary model

**Code Generation Process**:
1. Load PySpark template from S3
2. Agent analyzes mappings and target config
3. Agent uses MCP tools for optimization
4. Generate transformation code
5. Validate syntax
6. Extract parameter requirements
7. Store code and metadata in DynamoDB

**Configuration** (`compute-stack.ts:633-635`):
- Timeout: 900 seconds (15 minutes)
- Memory: 512 MB — sized for module imports plus one boto3 call, because the function is
  dispatch-only under `waitForTaskToken`; the work happens on AgentCore Runtime
- Layers: Strands Agents layer
- Environment Variables:
  - `AGENTCORE_ENDPOINT`
  - `TEMPLATES_BUCKET`
  - `GENERATED_CODE_TABLE`

**Note**: This Lambda runs on AgentCore Runtime. Code changes require deploying both Compute AND AgentCore stacks. The `create_code` Lambda also checks for newer mappings from rerun-mapping (P15) before generating code.

---

### agentic_data_ingestion_test_code

**Purpose**: Tests generated code using Glue Interactive Sessions.

**Trigger**: Step Functions (Step 6)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "code_version_id": "code-20241112-xyz789",
  "code_s3_uri": "s3://generated-code/.../etl_script.py"
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "test_status": "passed",
  "tests_passed": 12,
  "tests_failed": 0,
  "coverage_percentage": 87,
  "execution_time_ms": 45678,
  "spark_session_id": "agentcore-spark-abc123",
  "output_uri": "s3://test-outputs/.../",
  "approval_required": false
}
```

**Testing Process**:
1. Start Interactive Session via MCP
2. Generate test data via MCP
3. Execute code in Spark environment
4. Validate output schema
5. Run data quality checks
6. Analyze performance metrics
7. Stop Interactive Session

**Test Types**:
- **Unit Tests**: Individual transformation functions
- **Integration Tests**: End-to-end data flow
- **Schema Tests**: Output schema validation
- **Quality Tests**: Data quality rules
- **Performance Tests**: Execution time and resource usage

**Configuration** (`compute-stack.ts:662-665`):
- Timeout: 900 seconds (15 minutes)
- Memory: 512 MB — dispatch-only under `waitForTaskToken` (P28 R19.11)
- Layers: Strands Agents layer
- Environment Variables:
  - `AGENTCORE_ENDPOINT`
  - `TEST_OUTPUTS_BUCKET`

---

### agentic_data_ingestion_create_quality_checks

**Purpose**: Generates data quality check rules.

**Trigger**: Step Functions (optional step)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "target_schema": {...},
  "quality_requirements": {
    "completeness": 0.95,
    "uniqueness": ["customer_id"],
    "validity": {"email": "email_format"}
  }
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "quality_checks": [
    {
      "check_type": "completeness",
      "field": "customer_id",
      "threshold": 0.95
    },
    {
      "check_type": "uniqueness",
      "field": "customer_id"
    }
  ],
  "glue_data_quality_rules": "..."
}
```

**Configuration** (`compute-stack.ts:856-857`):
- Timeout: 300 seconds
- Memory: 512 MB

---

## 4. Infrastructure Functions

### agentic_data_ingestion_create_infrastructure

**Purpose**: Creates AWS infrastructure (S3 Tables, IAM roles) conditionally.

**Trigger**: Step Functions (Step 7)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "target_type": "NEW_S3_TABLE_BUCKET",
  "target_config": {
    "s3_table_bucket": "customer-data-lake",
    "table_name": "customers",
    "namespace": "default"
  }
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "infrastructure_created": true,
  "s3_table_bucket": {
    "bucket_name": "customer-data-lake",
    "table_name": "customers",
    "data_location": "s3://customer-data-lake/tables/default/customers/data/",
    "iceberg_metadata_location": "s3://customer-data-lake/tables/default/customers/metadata/"
  }
}
```

**Conditional Logic**:
- **NEW_S3_TABLE_BUCKET**: Create S3 Table Bucket with Iceberg
- **GLUE_CATALOG**: Skip (use existing catalog)
- **JDBC**: Skip (use existing database)
- **EXISTING_S3_TABLE_BUCKET**: Skip (use existing bucket)

**MCP Integration**:
- Uses S3 Tables MCP to create bucket
- Initializes Iceberg metadata
- Configures table properties

**Configuration** (verified — `compute-stack.ts:837-838`):
- Timeout: 600 seconds (10 minutes)
- Memory: 512 MB
- Layers: Strands Agents layer
- Environment Variables:
  - `AGENTCORE_ENDPOINT`

---

### agentic_data_ingestion_create_glue_pipeline

**Purpose**: Creates AWS Glue job with intelligent resource allocation.

**Trigger**: Step Functions (Step 8)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "code_version_id": "code-20241112-xyz789",
  "target_type": "S3_TABLES",
  "target_config": {...},
  "data_volume": "50GB"
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "glue_job": {
    "job_name": "pipeline-customer-data-etl-job",
    "job_arn": "arn:aws:glue:...",
    "role_arn": "arn:aws:iam:...",
    "script_location": "s3://...",
    "worker_type": "G.2X",
    "number_of_workers": 8,
    "timeout": 45,
    "estimated_cost_per_run": "$2.11",
    "mcp_enhanced": true
  }
}
```

**AI Integration**:
- **Strands Agent**: Orchestrates job creation
- **Data Processing MCP**: Recommends worker configuration
- **S3 Tables MCP**: Optimizes Iceberg settings
- **Bedrock Claude Sonnet 4.6**: Primary model

**Worker Recommendation Process**:
1. Extract data characteristics (volume, complexity)
2. Agent analyzes requirements
3. Agent uses MCP tools:
   - `analyze_data_characteristics`
   - `calculate_optimal_workers`
   - `estimate_glue_cost`
4. Agent returns recommendation with rationale
5. Create Glue job with recommended config

**Parameter Contract**:
1. Read parameter requirements from DynamoDB
2. Build parameter values from pipeline config
3. Merge target-specific parameters
4. Validate all required parameters present
5. Create Glue job with aligned `DefaultArguments`

**Configuration** (`compute-stack.ts:804-807`):
- Timeout: 900 seconds (15 minutes)
- Memory: 512 MB — dispatch-only under `waitForTaskToken` (P28 R19.11)
- Layers: Strands Agents layer
- Environment Variables:
  - `AGENTCORE_ENDPOINT`
  - `GENERATED_CODE_TABLE`
  - `PIPELINES_TABLE`

---

## 5. Approval System Functions

### agentic_data_ingestion_approval_config

**Purpose**: Manages approval configuration and policies.

**Trigger**: API Gateway (POST /api/approvals/config)

**Input**:
```json
{
  "pipeline_id": "pipeline-customer-data-etl",
  "approval_policy": {
    "schema_mapping": {
      "required": true,
      "threshold": 0.8,
      "timeout_hours": 72
    },
    "code_generation": {
      "required": false
    }
  }
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "config_id": "config-abc123",
  "policy_applied": true
}
```

**Configuration** (`compute-stack.ts:970-971`):
- Timeout: 60 seconds
- Memory: 256 MB

---

### agentic_data_ingestion_approval_handler

**Purpose**: Processes approval requests (approve/reject).

**Trigger**: API Gateway (POST /api/approvals/{id}/approve)

**Input**:
```json
{
  "approval_id": "approval-20241112-xyz789",
  "action": "approve",
  "approved_by": "user@example.com",
  "comments": "Mappings look good"
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "approval_id": "approval-20241112-xyz789",
  "status": "approved",
  "pipeline_resumed": true
}
```

**Actions**:
- **Approve**: Resume Step Functions execution
- **Reject**: Fail Step Functions execution
- **Request Changes**: Pause and notify

**Configuration** (`compute-stack.ts:1138-1139`):
- Timeout: 60 seconds
- Memory: 256 MB
- Permissions: `states:SendTaskSuccess`, `states:SendTaskFailure`

---

### agentic_data_ingestion_approval_timeout_handler

**Purpose**: Handles approval request timeouts.

**Trigger**: EventBridge (scheduled rule)

**Input**:
```json
{
  "approval_id": "approval-20241112-xyz789",
  "created_at": "2024-11-12T10:00:00Z",
  "timeout_hours": 12
}
```

**Output**:

> ⚠️ **Unverified against the handler** — see [Output contract provenance](#output-contract-provenance).

```json
{
  "statusCode": 200,
  "approval_id": "approval-20241112-xyz789",
  "status": "timeout",
  "pipeline_failed": true
}
```

**Configuration** (`compute-stack.ts:1171-1172`):
- Timeout: 60 seconds
- Memory: 256 MB
- Schedule: Every 15 minutes (`ApprovalTimeoutRule`,
  `events.Schedule.rate(cdk.Duration.minutes(15))` in
  `deploy-cdk/lib/orchestration-stack.ts`). The 15-minute figure is load-bearing,
  not incidental: `auto_approve` / `auto_reject` are unreachable precisely because
  Step Functions abandons the run at exactly 72 h while the sweep only arrives every
  15 minutes, so it always finds a consumed token. A stale figure here undercuts that
  argument.

---

## 6. API Functions

### agentic_data_ingestion_dashboard_api

**Purpose**: REST API for dashboard operations.

**Trigger**: API Gateway (all /api/* endpoints)

**Architecture**: Modular router with separate handlers

**The 25-second timeout is the ceiling every long endpoint here is designed around.** Three
in-code budgets derive from it as "25 s minus 5 s in hand" and all read `20.0`:
`stats.py:_STATS_TIME_BUDGET_SECONDS`, `schemas.py:_LISTING_TIME_BUDGET_SECONDS` and
`pipelines_crud.py:_LIST_TIME_BUDGET_SECONDS`. Two consequences follow:

- **`POST /api/rerun-mapping` is an asynchronous invoke for this reason.** A mapping run takes ~30 s
  — it includes a Bedrock Converse call, and the mapping Lambda's own ceiling is 600 s — so the
  endpoint invokes `agentic_data_ingestion_schema_mapping` with `InvocationType="Event"` and returns
  `200 {"success": true, "message": "Mapping regeneration started"}` immediately
  (`handlers/pipelines_mappings.py:110-133`). A synchronous call could not finish inside 25 s, so the
  caller would get a gateway error rather than a mapping. Progress is read back from the state row,
  not from the response.
- **Raising the timeout is not the fix for a slow endpoint.** It moves none of the three budgets
  above, so `partial_counts` and `enrichment_abandoned` would still fire at 20 s, and past 29 s it
  crosses API Gateway's integration ceiling, where the client gets a 504 with no body — the outcome
  those markers exist to avoid. Bound the work instead.

**Handlers**:
- `pipelines.py` - Pipeline management (includes rerun-mapping and mapping updates)
- `files.py` - File operations
- `stats.py` - Statistics
- `approvals.py` - Approval workflows
- `health.py` - Health checks
- `glue_jobs.py` - Glue job details
- `rds_resources.py` - RDS database discovery (P14)
- `resources.py` - AWS resource lookups (Bedrock models, S3 Tables buckets, Glue databases)
- `agents.py` - P13 agent endpoints (self-healing, quality report, cost recommendations, chat)
- `agent_traces.py` - P13a agent trace queries and token usage metrics

**Endpoints**:
- `GET /api/pipelines` - List pipelines
- `POST /api/pipelines/create` - Create pipeline
- `PUT /api/pipelines/{id}/edit` - Update pipeline
- `PUT /api/pipelines/{id}/archive` - Archive pipeline
- `PUT /api/pipelines/{id}/mappings` - Update mappings directly (P15)
- `GET /api/files` - List files
- `GET /api/stats` - Dashboard statistics
- `GET /api/approvals` - List approvals
- `GET /api/approvals/{approval_id}` - One approval with its stored payload parsed into `details`
- `POST /api/approvals/{id}/approve` - Approve request
- `POST /api/rerun-mapping` - Rerun schema mapping asynchronously (P15)
- `GET /api/pipelines/{id}/runs/{runId}/self-healing` - Self-healing activity (P13)
- `GET /api/pipelines/{id}/runs/{runId}/quality-report` - Data quality report (P13)
- `GET /api/cost-recommendations` - Cost optimization recommendations (P13)
- `POST /api/cost-recommendations/{id}/apply` - Apply recommendation (P13)
- `POST /api/chat` - NL pipeline builder chat (P13)
- `GET /api/runs/{runId}/agent-trace` - Agent traces for a run (P13a)
- `GET /api/metrics/token-usage` - Token usage metrics (P13a)
- `GET /api/resources/rds/instances` - List RDS instances (P14)
- `GET /api/resources/rds/clusters` - List RDS clusters (P14)
- `GET /api/resources/rds/glue-connections` - List Glue JDBC connections (P14)
- `GET /api/resources/rds/secrets` - List RDS secrets (P14)
- `GET /api/health` - Health check

→ `GET /api/approvals/{approval_id}` returns a `details_status` discriminator with three states —
`parsed`, `absent`, `unreadable` — and omits `details` on the latter two rather than sending `null` or
`{}`. `details: null` was rejected because every existing read site spells `approval.details ?? {}`,
and `??` catches null and absent alike, which is the collapse the discriminator exists to prevent. It
is also a separate endpoint rather than a widened listing, because parsing payloads in the listing
would ship every approval's full `proposed_mappings` on every page load to serve a modal opened for
one row. The states, and why they must not be collapsed, are documented once in
[DASHBOARD.md § GET /api/approvals/{approval_id}](DASHBOARD.md#get-apiapprovalsapproval_id).

**Configuration** (`compute-stack.ts:1033-1034`):
- Timeout: **25 seconds** — see the consequences below. Both values are asserted with `==` in
  `deploy-cdk/test/p47-dashboard-api-sizing.test.ts`, in both directions, because a range would let a
  measured value drift back to a guess. On the timeout the lower bound is as load-bearing as the
  upper: a ceiling below the 20 s those three budgets already spend converts reportable partials into
  gateway 504s.
- Memory: 512 MB (measured: `MaxMemoryUsed` max 258 MB over 1889 real invocations, so ~50% headroom;
  256 MB is excluded by that measurement rather than by caution)
- Permissions: Full DynamoDB, S3, Glue access; `rds:DescribeDBInstances`, `rds:DescribeDBClusters`, `glue:GetConnection*`, `glue:CreateConnection`, `secretsmanager:ListSecrets`, `secretsmanager:GetSecretValue` (P14); `lambda:InvokeFunction` on schema mapping Lambda (P15)

---

## 7. WebSocket & Concurrency Functions

### agentic_data_ingestion_ws_authorizer

**Purpose**: API Gateway WebSocket `$connect` Lambda authorizer. Validates the Cognito JWT passed via the `?token=<jwt>` query string against the User Pool JWKS — verifies the RS256 signature against the `kid`-named JWKS key, `alg` = RS256, the issuer, `exp` (and `nbf` when present), and that the token was issued to an allowed app client — `aud` for an ID token, `client_id` for an access token; any other `token_use` is denied. The allowed clients are the IDs held in the SSM parameters named by `USER_POOL_CLIENT_ID_PARAMETERS` (the dashboard client, written by the Hosting stack; the e2e client, written by the Auth stack when it exists), plus any in `USER_POOL_CLIENT_IDS`. If none is found every connection is denied and a configuration error is logged (threat model R-05). On success it returns an IAM `Allow` policy document (per the WebSocket authorizer contract) with the token's `sub` as the principal; otherwise it returns `Deny`, so connections without a valid token are rejected. JWKS keys are cached at module level across warm invocations.

**Trigger**: API Gateway WebSocket (`$connect` route authorizer)

**Configuration** (`compute-stack.ts`, `WsAuthorizer`):
- Timeout: 10 seconds
- Memory: 128 MB
- Layers: Strands Agents layer
- Permissions: runs on its own role (`WsAuthorizerRole`, Compute stack): `AWSLambdaBasicExecutionRole` for its logs and `ssm:GetParameters` on the two client-ID parameters, nothing else. It fetches the public User Pool JWKS over HTTPS
- Environment Variables:
  - `USER_POOL_ID` (derived from the User Pool ARN)
  - `USER_POOL_CLIENT_ID_PARAMETERS` (comma-separated SSM parameter names; a parameter that does not exist yet contributes nothing. Read at run time and cached for five minutes, because Hosting deploys after Compute)
  - `USER_POOL_CLIENT_IDS` (optional static override: comma-separated client IDs, not set by CDK). The old singular `USER_POOL_CLIENT_ID` is no longer read

---

### agentic_data_ingestion_ws_connect

**Purpose**: Handles WebSocket `$connect` and `subscribe` actions. Stores connection ID and pipeline subscriptions in DynamoDB.

**Trigger**: API Gateway WebSocket (`$connect`, `$default`)

**Configuration** (`compute-stack.ts:1219-1220`):
- Timeout: 10 seconds
- Memory: 128 MB
- Permissions: `dynamodb:PutItem`, `dynamodb:DeleteItem` on `agentic_data_ingestion_ws_connections`

---

### agentic_data_ingestion_ws_disconnect

**Purpose**: Removes all subscription records for a connection on `$disconnect`.

**Trigger**: API Gateway WebSocket (`$disconnect`)

**Configuration** (`compute-stack.ts:1234-1235`):
- Timeout: 10 seconds
- Memory: 128 MB
- Permissions: `dynamodb:Query`, `dynamodb:DeleteItem` on `agentic_data_ingestion_ws_connections`

---

### agentic_data_ingestion_ws_broadcast

**Purpose**: Pushes real-time execution updates to subscribed WebSocket clients. Triggered by DynamoDB Streams on the `agentic_data_ingestion_executions` table.

**Trigger**: DynamoDB Stream (batch size 10, 3 retries)

**Configuration** (`compute-stack.ts:1249-1250`):
- Timeout: 30 seconds
- Memory: 256 MB
- Permissions: `dynamodb:Query` on `agentic_data_ingestion_ws_connections`, `execute-api:ManageConnections`

---

### agentic_data_ingestion_pipeline_trigger

**Purpose**: SQS-buffered concurrency controller for S3 upload-triggered pipeline executions. Checks running execution count before starting new Step Functions executions.

**Trigger**: SQS (`agentic-data-ingestion-pipeline-triggers` queue, batch size 1)

**Configuration** (`orchestration-stack.ts:1670-1673` — this function is declared in the
Orchestration stack, not Compute):
- Timeout: **5 seconds**, and it cannot be raised on its own. SQS requires
  `queueVisibilityTimeout >= lambdaTimeout * 6`; the queue's visibility timeout is 30 s, so 30 / 6 = 5 s
  is the maximum this Lambda may have — the constraint is stated at `orchestration-stack.ts:1667-1668`
  and the value at `:1670`. **Raising the timeout without raising queue visibility first breaks the
  event source.** 5 s is sufficient because the work is lightweight: a DynamoDB lock check, a
  processed-file ledger read, and `states:StartExecution`.
- Memory: 256 MB
- Reserved Concurrency: 5
- Layers: `[adotLayer, powertoolsLayer]` — not the Strands layer
- Permissions: `states:StartExecution`, `states:ListExecutions`
- Environment Variables:
  - `STATE_MACHINE_ARN`
  - `MAX_CONCURRENT_EXECUTIONS` (default: 10)

---

### agentic_data_ingestion_queue_loss_recorder

**Purpose**: Writes the durable Lost_File record for an upload SQS gave up redelivering. When
`pipeline_trigger` above cannot take the per-pipeline lock it returns the record in
`batchItemFailures` and SQS redelivers, which is correct and is not a loss. Past
`maxReceiveCount` receives, though, SQS moves the message to `PipelineTriggerDLQ` and the file is
gone, while the last thing the dashboard said about it was `queued`. This function is that DLQ's
consumer, and it exists so the loss is visible.

**It records and stops.** It does not redrive, resubmit, or start anything; who presses a redrive
button is deliberately out of scope. Its IAM policy is `dynamodb:PutItem` alone
(`orchestration-stack.ts:1759-1762`), which is what makes that boundary structural rather than a
convention.

**Trigger**: SQS (`PipelineTriggerDLQ`), via an event source mapping at
`orchestration-stack.ts:1831-1843`: batch size 1, `enabled: true`, and
`functionResponseTypes: ['ReportBatchItemFailures']`. Without that last setting the handler's
return value is inert.

**Input**: an SQS event whose `body` is the original EventBridge S3 object-created event. The
handler reads only `detail.bucket.name` and `detail.object.key` from it.

**What it writes**: a *complete* executions-table row on the content-addressed key
`queued-{sha256(source_key)[:16]}`, which is byte-for-byte the key `pipeline_trigger` uses for its
queued placeholder, so no lookup is needed and the write is idempotent. Three properties of that
row are load-bearing:

- **`status` stays `"QUEUED"`.** A fourth `executions` status was rejected, because the
  `StatusIndex` GSI is partitioned on `status` and the run-history read treats every non-`QUEUED`
  row as a run, so a novel value would render as a phantom run. The loss rides on a separate
  attribute instead, `queue_outcome = "lost_to_dlq"` (constants at `lambda_function.py:118-119`).
- **`started_at` is present.** `RunsByTimeIndex` is `(pipeline_id, started_at)` and a GSI indexes
  only items carrying both key attributes, so a row without it is absent from the index silently.
  It is set to the instant the loss was recorded, because the original queue time is not
  recoverable from a DLQ message.
- **`put_item`, not `update_item`.** The redelivery budget now exceeds the queued placeholder's
  TTL, so by the time a message reaches the DLQ the placeholder has normally expired. An
  `update_item` against a missing item creates it, which would mint a row with no `status` and no
  `started_at`: an unindexed, invisible phantom.

**Output**:

```json
{
  "batchItemFailures": [{"itemIdentifier": "<messageId>"}]
}
```

Empty on a fully successful batch (`lambda_function.py:289`). A body that can never parse, or one
carrying no `source_key`, is logged at ERROR and reported as **success**, because there is no DLQ
behind this DLQ and no key to write a record under. A DynamoDB write failure is reported as a
**failure**, so the message returns and the next delivery can write the record.

**Configuration** (`orchestration-stack.ts:1769-1794`, declared in the Orchestration stack, not
Compute):
- Timeout: 30 seconds (`:1775`)
- Memory: 256 MB (`:1776`)
- Layers: `[powertoolsLayer]` (`:1784`), not the Strands layer. It carries `shared/*.py` at
  `/opt/python`, which is how the record's 30-day TTL constant resolves from `shared/config.py`.
  That TTL is deliberately not passed as an environment variable, so the behavioural value keeps
  one home
- Permissions: `dynamodb:PutItem` on the executions table; `sqs:ReceiveMessage`,
  `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on the DLQ (`:1759-1767`)
- Environment Variables (`:1789-1793`):
  - `EXECUTIONS_TABLE`
  - `POWERTOOLS_SERVICE_NAME`, `POWERTOOLS_LOG_LEVEL`

**Deployed function name**: `agentic-data-ingestion-queue-loss-recorder-{environment}`
(`:1770`), hyphenated and environment-suffixed like its Orchestration-stack siblings. The heading
above is the source directory name, which is what the rest of this document keys on.

Mechanism and the read path that surfaces these rows:
[WORKFLOW.md § Queue admission, loss and skips](WORKFLOW.md#queue-admission-loss-and-skips).

---

### agentic_data_ingestion_execution_updater

**Purpose**: DynamoDB Stream handler that populates the `agentic_data_ingestion_executions` summary table from state table changes. Also writes to `agentic_data_ingestion_errors` for failed steps.

**Trigger**: DynamoDB Stream on `agentic_data_ingestion_state` table

**Configuration** (verified — `compute-stack.ts:1192-1193`):
- Timeout: 60 seconds
- Memory: 256 MB
- Permissions: `dynamodb:PutItem`, `dynamodb:UpdateItem` on `agentic_data_ingestion_executions` and `agentic_data_ingestion_errors`

---

## Common Patterns

### Error Handling

All Lambda functions follow this error handling pattern:

```python
try:
    # Main logic
    result = process_data(event)
    
    # Write state to DynamoDB
    write_state(pipeline_id, step_name, "success", result)
    
    return {
        "statusCode": 200,
        "body": json.dumps(result)
    }
    
except ValidationError as e:
    logger.error(f"Validation error: {str(e)}")
    write_state(pipeline_id, step_name, "error", {"error": str(e)})
    return {
        "statusCode": 400,
        "body": json.dumps({"error": str(e)})
    }
    
except Exception as e:
    logger.error(f"Unexpected error: {str(e)}", exc_info=True)
    write_state(pipeline_id, step_name, "error", {"error": str(e)})
    return {
        "statusCode": 500,
        "body": json.dumps({"error": "Internal server error"})
    }
```

### State Tracking

All pipeline steps write state to DynamoDB:

```python
def write_state(pipeline_id, step_name, status, output_data):
    dynamodb.put_item(
        TableName=STATE_TABLE,
        Item={
            "pipeline_id": f"{run_id}.{step_number}",
            "step_name": step_name,
            "run_id": run_id,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
            "output_data": json.dumps(output_data),
            "execution_time_ms": execution_time
        }
    )
```

### Logging

Structured logging with JSON format:

```python
logger.info(json.dumps({
    "event": "step_started",
    "pipeline_id": pipeline_id,
    "step_name": step_name,
    "timestamp": datetime.utcnow().isoformat()
}))
```

### AWS Client Initialization

Initialize clients at module level for connection reuse:

```python
# Module level (outside handler)
dynamodb = boto3.client('dynamodb')
s3 = boto3.client('s3')
bedrock = boto3.client('bedrock-runtime')

def lambda_handler(event, context):
    # Use pre-initialized clients
    response = dynamodb.get_item(...)
```

---

## Error Handling

### Retry Logic

Step Functions automatically retries failed Lambda invocations:
- **Max Attempts**: 3
- **Interval**: 2 seconds
- **Backoff Rate**: 2.0
- **Max Interval**: 60 seconds

### Error Types

**Validation Errors** (400):
- Missing required parameters
- Invalid input format
- Schema validation failures

**Permission Errors** (403):
- IAM permission denied
- S3 access denied
- Bedrock quota exceeded

**Service Errors** (500):
- AWS service failures
- Timeout errors
- Unexpected exceptions

### Error Recovery

**Automatic Recovery**:
- Lambda retries (3 attempts)
- Step Functions retries (3 attempts)
- Exponential backoff

**Manual Recovery**:
- Resume from last successful step
- Replay failed step with corrected input
- Manual approval override

---

## Performance Optimization

### Cold Start Mitigation

- **arm64 architecture**: 20% faster cold starts
- **Minimal dependencies**: Reduce package size
- **Connection pooling**: Reuse AWS clients
- **Provisioned concurrency**: For critical functions

### Memory Allocation

Optimized memory allocation per function:
- **Light functions** (256 MB): Health checks, simple queries
- **Standard functions** (512 MB): Most processing functions
- **Heavy functions** (1024 MB): Code generation, testing, file processing

### Timeout Configuration

Appropriate timeouts per function:
- **Quick operations** (180s): Validation, comparison
- **Standard operations** (300s): Schema extraction, mapping
- **Long operations** (900s): Code generation, testing, Glue job creation

---

## Monitoring

### CloudWatch Metrics

Monitor these key metrics:
- **Invocations**: Total function invocations
- **Errors**: Failed invocations
- **Duration**: Execution time (p50, p99)
- **Throttles**: Concurrent execution limits hit
- **Iterator Age**: For stream-based functions

### CloudWatch Alarms

Set alarms for:
- Error rate > 5%
- Duration > timeout threshold
- Throttles > 0
- Memory usage > 90%

### X-Ray Tracing

Enable X-Ray for:
- Distributed tracing
- Performance bottleneck identification
- Service map visualization
- Error analysis

---

## Related Documentation

- [Architecture Overview](ARCHITECTURE.md)
- [DynamoDB Tables](DYNAMODB-TABLES.md)
- [Dashboard](DASHBOARD.md) — carries the REST endpoint list this Lambda serves
- [MCP Integration](MCP-INTEGRATION.md)
- [Getting Started](GETTING-STARTED.md) — deployment walkthrough

> There is no separate API reference document. This list previously named
> `API-REFERENCE.md` and `DEPLOYMENT-GUIDE.md`; neither has ever existed in this
> repository. The authoritative endpoint list is
> `lambda/agentic_data_ingestion_dashboard_api/router.py`, which dispatches to the
> handler modules documented above.
