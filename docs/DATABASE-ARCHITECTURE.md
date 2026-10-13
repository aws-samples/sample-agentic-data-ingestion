# Database Architecture Documentation

## Overview

Agentic Data Ingestion uses a DynamoDB-based database architecture in which each table is optimized
for a different access pattern. This document details the relationships *between* tables, the data
flow patterns that cross them, and how they support the complete ETL pipeline lifecycle. **The table
count is not restated here** — it lives with its derivation in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory).

> **This is the cross-table design document, not the per-table reference.**
> Keys, attributes, GSI definitions, capacity mode and example records for every table
> live in **[DYNAMODB-TABLES.md](DYNAMODB-TABLES.md)**, which is the single per-table reference.
> Look here for how the tables relate; look there for what any one table contains.
> See [§Consolidation decision](#consolidation-decision) for why the split is drawn this way.

> **Phase 4 Changes (2026-02-22):** Table names standardized to `agentic_data_ingestion_{name}`, state table PK changed to `run_id`, GSIs consolidated downward, `agentic_data_ingestion_schema_metadata` deleted, two new tables added (`agentic_data_ingestion_executions`, `agentic_data_ingestion_task_tokens`).
>
> **The GSI consolidation is history, and it did not hold.** The count fell on that date and has
> risen since, because most tables added in later phases brought indexes with them. The two figures
> that bracketed the change on the day are deliberately not restated here, because a number pinned
> inside a dated note reads as current and goes stale in place. For the live total, read the GSI row
> in [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory), which derives it from
> `deploy-cdk/lib/data-stack.ts` rather than quoting it.
>
> **Later Phases:** Added `agentic_data_ingestion_ws_connections` (Phase 6 — WebSocket real-time updates), `agentic_data_ingestion_errors` (Phase 8 — centralized error tracking), `agentic_data_ingestion_pipeline_locks` (Phase 11 — concurrent execution prevention), `agentic_data_ingestion_audit_log` (Phase 13 — user action audit trail), `agentic_data_ingestion_agent_traces` (Phase 13a — agent execution traces), and `agentic_data_ingestion_pipeline_watermarks` (Phase 17 — JDBC incremental-read watermarks).
>
> **Table names in this document are the canonical ones**, matching `deploy-cdk/lib/data-stack.ts`:
> `agentic_data_ingestion_{name}`, underscores throughout, no environment suffix. The pre-Phase-4
> spellings that the diagrams and SQL examples below used to carry have been replaced, so a name
> here can be grepped against the stack and against a deployed account.

---

## Table of Contents

1. [Consolidation decision](#consolidation-decision)
2. [Database Schema Overview](#database-schema-overview)
3. [Table Relationships](#table-relationships)
4. [Data Flow Patterns](#data-flow-patterns)
5. [Access Patterns](#access-patterns)
6. [Schema Relationships Diagram](#schema-relationships-diagram)
7. [Data Lifecycle](#data-lifecycle)
8. [Query Patterns](#query-patterns)
9. [Best Practices](#best-practices)

---

## Consolidation decision

Two documents described the DynamoDB layer, and they disagreed. This one claimed 15 tables in its
prose while its own summary table listed 13 rows — so a reader who counted the rows concluded four
tables did not exist. `DYNAMODB-TABLES.md` said 17. Both shared two identical errors, which is what
established that one had been copied from the other rather than written independently.

**Chosen: keep both files, with a strict reference/design split.** `DYNAMODB-TABLES.md` is the
single **per-table reference** — keys, attributes, GSI definitions, capacity mode, example records.
This file keeps only what is **not derivable from a per-table schema**: stream-driven
denormalisation, the approval lifecycle, the mapping-reuse flow, cross-table access patterns, and
the data lifecycle. Where the two must agree on a fact, this file points rather than restates.

Three alternatives were considered and rejected.

- **Merge into one file.** Rejected because the two documents answer different questions for
  different readers. Someone looking up an attribute name wants a table's schema and nothing else;
  someone tracing how an approval resumes a Step Functions execution wants prose that spans five
  tables and no schemas at all. Merging produces a single file where each reader must skip most of
  it, and it does not remove the duplication — it relocates it inside one file, where a future
  divergence is *harder* to spot because the two statements are no longer in separate files a diff
  can compare.
- **Fix the contradictions only, leaving both files describing every table.** Rejected because it
  treats the symptom. The contradictions are a consequence of two files holding one fact; fixing the
  three current disagreements leaves the mechanism that produced them intact, so the next per-table
  change re-forks them. The copy-paste lineage is the defect, not the specific numbers.
- **Delete `DATABASE-ARCHITECTURE.md` and keep only the per-table reference.** Rejected because a
  measurable amount of content here is genuinely not per-table and has no other home: the
  state→executions stream denormalisation, the approval-and-task-token lifecycle, the nine
  cross-table access patterns, the mapping-reuse flow, and the hot/warm/cold data lifecycle. Deleting
  the file would delete that material or force it into a per-table reference where it does not
  belong — which is the same duplication defect, arrived at from the other direction.

**What was cut from this file, and why.** The `Tables Summary` table (13 rows against a stated 15,
duplicating `DYNAMODB-TABLES.md`); the per-table key columns inside both entity-relationship
diagrams — **the diagrams themselves stay**, because the relationships they draw are exactly the
non-derivable content this file is for; and the whole `Capacity Planning` chapter, whose
read/write-unit arithmetic and provisioned-capacity recommendations are meaningless when
**every table is `PAY_PER_REQUEST`** (`deploy-cdk/lib/data-stack.ts`).

---

## Database Schema Overview

Every table is `PAY_PER_REQUEST` and named `agentic_data_ingestion_{name}`, and each is declared as
a `new dynamodb.Table` in `deploy-cdk/lib/data-stack.ts` — that file is the authority for the
schemas, and the count derived from it is stated once in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory).
Per-table detail: **[DYNAMODB-TABLES.md](DYNAMODB-TABLES.md)**.

Three facts are recorded here because both documents must agree on them:

- **The state table's `PipelineIndex` GSI sorts on `timestamp`**, not `started_at`
  (`data-stack.ts:54-56`). `started_at` is the sort key of the *executions* table's GSIs
  (`data-stack.ts:166`, `:213`), and the two were conflated. The state table's **base** keys are
  `run_id` (PK) and `step_name` (SK) (`data-stack.ts:49-50`).
- **`GlueJobRunIndex` is present**, on the state table, partitioned by `glue_job_run_id`
  (`data-stack.ts:60`). It was previously described here as removed, under the misspelling
  `GlueJobRunIdIndex`.
- **The state table carries 3 GSIs** — `PipelineIndex`, `GlueJobRunIndex` and
  `step_name-timestamp-index` (`data-stack.ts:53-79`).

### Deleted Tables

| Table Name | Reason | Date |
|------------|--------|------|
| `agentic_data_ingestion_schema_metadata` | Zero reads/writes in any Lambda code | 2026-02-22 |

---

## Table Relationships

### Entity Relationship Diagram

Relationships only — no keys, no attributes, no GSI lists. For any single table's schema, see
[DYNAMODB-TABLES.md](DYNAMODB-TABLES.md). `..._` abbreviates the `agentic_data_ingestion_` prefix.

```
┌────────────────────────────────┐
│ agentic_data_ingestion_        │
│ pipelines                      │
│ (Master Pipeline)              │
└───────────────┬────────────────┘
                │
                │ 1:N (one pipeline has many runs)
                │
      ┌─────────┴───────────┬────────────────────────────┐
      │                     │                            │
      ▼                     ▼                            ▼
┌──────────────────┐  ┌─────────────────────┐  ┌──────────────────────┐
│ ..._state        │  │ ..._approvals       │  │ ..._executions       │
│ (Execution       │  │ (Workflow Approvals)│  │ (Summary,            │
│  State)          │  │                     │  │  Stream-populated)   │
└────────┬─────────┘  └──────────┬──────────┘  └──────────────────────┘
         │                       │                        ▲
         │                       │ 1:1                    │
         │                       ▼                        │
         │            ┌──────────────────────┐            │
         │            │ ..._task_tokens      │            │
         │            │ (Token Storage, TTL) │            │
         │            └──────────────────────┘            │
         │                                                │
         ├─ Stream: NEW_IMAGE ─▶ execution_updater ───────┘
         │
         ├─ References (via output_data Map)
         │
      ┌──┴──────────────────┬──────────────────┬─────────────────────┐
      │                     │                  │                     │
      ▼                     ▼                  ▼                     ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌────────────────────┐
│ ..._generated_  │  │ ..._test_results │  │ ..._schema_      │  │ ..._notifications  │
│ code            │  │                  │  │ mappings         │  │                    │
│ (AI Code        │  │ (Test Results)   │  │ (Mapping Records)│  │ (Dashboard Events) │
│  Versions)      │  │                  │  │                  │  │                    │
└─────────────────┘  └──────────────────┘  └──────────────────┘  └────────────────────┘
```

> The stream arrow runs **state → executions**, via the `execution_updater` Lambda (see §1b below).
> An earlier revision of this diagram drew it from state to *approvals*, which no code path matches.

### Relationship Details

#### 1. Pipeline → State (1:N)
- **Type**: One-to-Many
- **Key Relationship**: `agentic_data_ingestion_pipelines.pipeline_id` → `agentic_data_ingestion_state.pipeline_id` (via PipelineIndex GSI)
- **Description**: Each pipeline configuration can have multiple execution runs, each with 10 state records (one per step)
- **Cascade**: Pipeline archive/restore affects related state records
- **Query Pattern**: GSI `PipelineIndex` for all runs of a pipeline; base table Query by `run_id` for all steps of a run
- **Sort key**: `PipelineIndex` is `(pipeline_id, timestamp)` — **`timestamp`, not `started_at`**
  (`deploy-cdk/lib/data-stack.ts:54-56`). `started_at` belongs to the executions table's GSIs

#### 1b. State → Executions (via DynamoDB Stream)
- **Type**: Stream-driven denormalization
- **Key Relationship**: State table Stream → `agentic_data_ingestion_execution_updater` Lambda → `agentic_data_ingestion_executions`
- **Description**: Every INSERT/MODIFY on the state table triggers an upsert to the executions summary table
- **Query Pattern**: Dashboard queries `agentic_data_ingestion_executions` by `pipeline_id` for fast execution list

#### 2. State → Code Versions (N:1)
- **Type**: Many-to-One (via JSON reference)
- **Key Relationship**: `agentic_data_ingestion_state.output_data.code_version_id` → `agentic_data_ingestion_generated_code.code_version_id`
- **Description**: Step 5 (Code Generation) creates code version, stores reference in state
- **Access Pattern**: State record contains code_version_id, direct lookup in generated-code table
- **Versioning**: Multiple state records can reference same code version (reuse)

#### 3. State → Test Results (1:1)
- **Type**: One-to-One (per execution)
- **Key Relationship**: `agentic_data_ingestion_state.output_data.test_id` → `agentic_data_ingestion_test_results.test_id`
- **Description**: Step 6 (Testing) creates test result, links to code version
- **Access Pattern**: State record from step 6 contains test_id
- **Lifecycle**: Test results persist for quality tracking, cleanup via TTL (optional)

#### 4. State → Schema Mappings (N:1)
- **Type**: Many-to-One (mapping records)
- **Key Relationship**: `agentic_data_ingestion_state.output_data.mapping_id` → `agentic_data_ingestion_schema_mappings.mapping_id`
- **Description**: Step 4 (Schema Mapping) writes a mapping record per generated mapping; `mapping_id` derives from the mapping content hash, so an identical regenerated mapping dedupes onto the existing record
- **Reuse**: NOT a lookup cache — reuse happens via AgentCore mapping memory (approved mappings guide future AI generation) and via freshest-approved-mapping substitution at code-generation time (P28 R6)
- **Why this matters**: only an **approved** mapping is a reuse candidate, and taking the reuse path
  still passes a human — it skips the Bedrock mapping call, not the approval gate. There is no
  hash-pair lookup that returns a mapping for direct use. Reading the `schema-pair-index` GSI as a
  reuse cache is the specific mistake this design exists to stop. An earlier unfiltered "newest row
  carrying a `mapping_id`" lookup made a human-*rejected* mapping a valid candidate, and because the
  newest row won regardless of status it also cost a pipeline access to its own last approved
  mapping; the candidate query now filters on approved status
- **Access Pattern**:
  - Direct lookup by mapping_id (existence check before write)
  - Dashboard reads for mapping review

#### 5. Pipeline → Approvals (1:N)
- **Type**: One-to-Many
- **Key Relationship**: `agentic_data_ingestion_pipelines.pipeline_id` → `agentic_data_ingestion_approvals.pipeline_id`
- **Description**: Pipeline execution can trigger multiple approvals (schema mapping, code review)
- **Lifecycle**: Approval rows auto-delete **30 days** after creation via the `ttl` attribute. That
  is row retention and is **not** the approval window — the window is **72 hours** (see the timeout
  branch in §2 below). Confusing the two tells an approver they have weeks
- **Query Pattern**: GSI `pipeline-id` to find all approvals for a pipeline

#### 5b. Approvals → Task Tokens (1:1)
- **Type**: One-to-One
- **Key Relationship**: `agentic_data_ingestion_approvals.approval_id` → `agentic_data_ingestion_task_tokens.approval_id`
- **Description**: Each approval with a Step Functions wait stores its task token in the dedicated tokens table
- **Lifecycle**: Token written on approval creation, read-and-deleted on approve/reject, auto-expires via 7-day TTL
- **Access Pattern**: Approval record stores `has_task_token: True`; approve/reject handler reads token from `agentic_data_ingestion_task_tokens`

#### 6. State/Pipeline → Notifications (1:N)
- **Type**: One-to-Many (event-driven)
- **Key Relationship**: EventBridge events trigger notification creation
- **Description**: State changes, errors, completions generate notifications
- **Lifecycle**: 30-day TTL, auto-cleanup
- **Access Pattern**: Query by priority via `PriorityIndex` GSI

---

## Data Flow Patterns

### 1. Pipeline Execution Flow

```
Step 0: Pipeline Creation
├─ Write to: agentic_data_ingestion_pipelines
│  - pipeline_id, source_config, target_config
│
Step 1: File Validation
├─ Write to: agentic_data_ingestion_state
│  - pipeline_id: {run_id}.1
│  - output_data: {validation_status, file_size_mb}
│
Step 2: Schema Extraction
├─ Write to: agentic_data_ingestion_state
│  - pipeline_id: {run_id}.2
│  - output_data: {source_schema, source_schema_hash}
│
Step 3: Schema Comparison
├─ Read from: agentic_data_ingestion_pipelines (target_schema)
├─ Write to: agentic_data_ingestion_state
│  - output_data: {drift_detected, compatibility}
│
Step 4: Schema Mapping (AI + Mapping Memory)
├─ Retrieve approved-mapping guidance: AgentCore Memory (mapping memory)
│  - One batched semantic retrieval per run (client-side field filtering)
│  - Entries exist only for previously APPROVED mappings
├─ AI generates mapping (memory guidance in prompt)
│  - Write to: agentic_data_ingestion_schema_mappings
│  - mapping_id (content-hash derived), mapping_result, confidence_score
├─ Write to: agentic_data_ingestion_state
│  - output_data: {mapping_id, mappings, confidence}
├─ If confidence < 0.8 (CONFIDENCE_AUTO_APPROVE_THRESHOLD, shared/config.py):
│  - Write to: agentic_data_ingestion_approvals
│  - approval_type: "schema_mapping"
│  - Wait for human approval
│
Step 5: Code Generation (AI + Dual MCP)
├─ Read from: agentic_data_ingestion_state (step 4 output for mappings)
├─ AI generates PySpark code
├─ Write to: agentic_data_ingestion_generated_code
│  - code_version_id, code_s3_uri, required_params
├─ Write to: agentic_data_ingestion_state
│  - output_data: {code_version_id, quality_score}
│
Step 6: Code Testing
├─ Read from: agentic_data_ingestion_generated_code (code to test)
├─ Run Interactive Session
├─ Write to: agentic_data_ingestion_test_results
│  - test_id, test_status, coverage_percentage
├─ Write to: agentic_data_ingestion_state
│  - output_data: {test_id, test_status}
│
Step 7: Infrastructure Provisioning (Conditional)
├─ Read from: agentic_data_ingestion_pipelines (target_type)
├─ If NEW_S3_TABLE_BUCKET:
│  - Create S3 Table Bucket via MCP
│  - Update agentic_data_ingestion_pipelines (infrastructure details)
├─ Write to: agentic_data_ingestion_state
│  - output_data: {infrastructure_created}
│
Step 8: Glue Job Creation
├─ Read from: agentic_data_ingestion_generated_code (parameter requirements)
├─ Read from: agentic_data_ingestion_pipelines (pipeline config for values)
├─ Create Glue job
├─ Update: agentic_data_ingestion_pipelines
│  - glue_job_name, worker_type, worker_count
├─ Write to: agentic_data_ingestion_state
│  - output_data: {glue_job_name, estimated_cost}
│
Step 9: EventBridge Notifications (throughout)
├─ Each state change triggers EventBridge event
├─ Write to: agentic_data_ingestion_notifications
│  - notification_id, title, message, pipeline_id
```

### 2. Approval Workflow Data Flow

```
Approval Request Created
├─ Write to: agentic_data_ingestion_approvals
│  - approval_id, approval_type, status: "pending"
│  - approval_data: {relevant context}
│  - timeout_at: current_time + 72 h (default; DEFAULT_TIMEOUT_HOURS)
│  - ttl: current_time + 30 days (DynamoDB auto-cleanup)
│
├─ Write to: agentic_data_ingestion_notifications
│  - notification_type: "approval_required"
│
Step Function Waits
├─ Task token saved in approval record
├─ Periodic polling or callback pattern
│
User Reviews (via Dashboard)
├─ Read from: agentic_data_ingestion_approvals
├─ Display approval_data to user
│
User Approves/Rejects
├─ Update: agentic_data_ingestion_approvals
│  - status: "approved" or "rejected"
│  - approved_by, approved_at
│  - approval_notes
│
├─ Write to: agentic_data_ingestion_notifications
│  - notification_type: "approval_completed"
│
Step Function Resumes
├─ Callback to Step Functions with decision
├─ If approved: Continue to next step
├─ If rejected: Mark state as failed, end execution
│
Timeout Handling (timeout_at reached — default 72 h, shared/config.py)
├─ approval_timeout_handler escalates the pending approval for a human
│  (timeout_behavior is always "manual" — not configurable; the handler's
│   auto_approve / auto_reject branches cannot be selected and cannot fire,
│   see p47 R6 at its dispatch)
│  - Write to: agentic_data_ingestion_notifications
│  - notification_type: "approval_timeout"
├─ Record auto-deletes 30 days after creation (DynamoDB TTL)
```

### 3. Schema Mapping Reuse Flow

There is no wholesale mapping cache that skips AI generation. Two reuse
mechanisms exist, both driven by *approved* mappings:

```
Step 4: Schema Mapping Request
│
├─ Mapping memory recall (AgentCore Memory, shared/mapping_memory.py)
│  ├─ ONE batched semantic retrieval per run (client-side field filter)
│  ├─ Returns previously APPROVED field-mapping decisions
│  └─ Fed into the AI prompt as guidance (memory entries are written
│     on approval only — never at generation time)
│
├─ AI (Bedrock Converse) generates the mapping
│  └─ Write record to: agentic_data_ingestion_schema_mappings
│     - mapping_id (derived from mapping content hash)
│     - source_schema_hash, target_schema_hash
│     - mapping_result, confidence_score, ai_model_used
│     - created_timestamp, generated_by (honest provenance)
│
└─ Later, at code generation (create_code/mapping_resolution.py):
   └─ Freshest-APPROVED-mapping substitution — all approved
      schema_mapping state rows for the pipeline are evaluated and
      the newest by created_timestamp wins (P28 R6)
```

---

## Access Patterns

### Pattern 1: Dashboard - Recent Pipeline Executions

**Requirement**: Display recent pipeline runs sorted by time

**Query**:
```python
# Query agentic_data_ingestion_pipelines
response = pipelines_table.scan(
    FilterExpression=Attr('status').ne('archived')
)
# Sort by created_at DESC
pipelines = sorted(response['Items'],
                  key=lambda x: x['created_at'],
                  reverse=True)
```

**Optimization**: Use GSI `status-created_at` for efficient time-ordered queries

---

### Pattern 2: Pipeline Run Details

**Requirement**: Show all 10 steps of a specific pipeline run

**Query**:
```python
# Query agentic_data_ingestion_state base table by run_id (PK)
response = state_table.query(
    KeyConditionExpression=Key('run_id').eq(run_id)
)
# Results contain all steps for this run
steps = response['Items']
```

**Result**: 10 records (one per step) with status, output_data, timing

---

### Pattern 2b: Pipeline Execution List (Fast Path)

**Requirement**: Show all executions for a pipeline (dashboard polls every 10s)

**Query**:
```python
# Query agentic_data_ingestion_executions by pipeline_id (PK)
response = executions_table.query(
    KeyConditionExpression=Key('pipeline_id').eq(pipeline_id),
    ScanIndexForward=False  # newest first
)
executions = response['Items']
```

**Result**: Denormalized execution summaries with status, current_step, steps_status Map. Sub-100ms response.

---

### Pattern 3: Pipeline Execution History

**Requirement**: Show all runs for a specific pipeline configuration

**Query**:
```python
# Query using PipelineIndex GSI
response = state_table.query(
    IndexName='PipelineIndex',
    KeyConditionExpression=Key('pipeline_id').eq(pipeline_id),
    ScanIndexForward=False  # Most recent first
)
runs = response['Items']
```

**Grouping**: Group by `run_id` to get distinct executions

---

### Pattern 4: Error Monitoring

**Requirement**: Find all failed pipeline executions

**Query**:
```python
# Query agentic_data_ingestion_executions using StatusIndex GSI
response = executions_table.query(
    IndexName='StatusIndex',
    KeyConditionExpression=Key('status').eq('failed') &
                          Key('started_at').gt(cutoff_time)
)
failed_executions = response['Items']
```

**Note**: The state table's `StatusIndex` GSI was removed in Phase 4 (unused). Error monitoring now uses the executions table's `StatusIndex` GSI instead.

---

### Pattern 5: Code Version Lookup

**Requirement**: Retrieve AI-generated code for deployment

**Query**:
```python
# Direct lookup by code_version_id
response = code_table.query(
    KeyConditionExpression=Key('pk').eq(f'CODE#{code_version_id}') &
                          Key('sk').eq('METADATA')
)
code_metadata = response['Items'][0]

# Get code from S3
code_s3_uri = code_metadata['code_s3_uri']
```

**Alternative**: Query all versions for comparison using `begins_with(sk, 'VERSION')`

---

### Pattern 6: Freshest Approved Mapping Selection

**Requirement**: At code-generation time, use the newest *approved* mapping for the pipeline (P28 R6 — `create_code/mapping_resolution.py`)

**Query**:
```python
# Query the state table's PipelineIndex GSI for the pipeline's
# schema_mapping rows, keep approved ones, take the newest.
response = state_table.query(
    IndexName='PipelineIndex',
    KeyConditionExpression=Key('pipeline_id').eq(pipeline_id),
)

approved = [
    row for row in response['Items']
    if row.get('step_name') == 'schema_mapping' and row.get('status') == 'success'
]
freshest = max(approved, key=lambda row: row.get('created_timestamp', ''), default=None)
# The substitution is logged when it differs from the run's own mapping
```

**Note**: Field-level reuse guidance comes from AgentCore mapping memory (entries created on approval only, one batched retrieval per run) — see "Schema Mapping Reuse Flow" above.

---

### Pattern 7: Pending Approvals for User

**Requirement**: Show all approvals awaiting user action

**Query**:
```python
# Query using status-created_at GSI
response = approvals_table.query(
    IndexName='status-created_at',
    KeyConditionExpression=Key('status').eq('pending'),
    ScanIndexForward=False  # Newest first
)

# Optional: Filter by approval_type
approvals = [item for item in response['Items']
            if item.get('approval_type') == 'schema_mapping']
```

**Expiration**: TTL automatically removes stale approvals

---

### Pattern 8: Dashboard Notifications

**Requirement**: Fetch unread notifications for dashboard

**Query**:
```python
# Scan for recent unread notifications (optimize with GSI if high volume)
from datetime import datetime, timedelta

cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()

response = notifications_table.scan(
    FilterExpression=Attr('read').eq(False) &
                     Attr('created_at').gt(cutoff)
)
notifications = sorted(response['Items'],
                      key=lambda x: x['created_at'],
                      reverse=True)
```

**Optimization**: Add GSI on `read` + `created_at` for high-volume scenarios

---

### Pattern 9: Glue Job Run Callback

**Requirement**: Update pipeline state when Glue job completes

**Query**:
```python
# The job_completion Lambda receives run_id and step_name from the event
# Direct base table query by run_id (PK) + step_name (SK)
response = state_table.get_item(
    Key={'run_id': run_id, 'step_name': step_name}
)

if 'Item' in response:
    state_record = response['Item']

    # Update status
    state_table.update_item(
        Key={'run_id': run_id, 'step_name': step_name},
        UpdateExpression='SET #status = :status, glue_job_status = :job_status',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':status': 'success',
            ':job_status': job_status
        }
    )
```

**Note**: `GlueJobRunIndex` **is present** on the state table, partitioned by `glue_job_run_id`
(`deploy-cdk/lib/data-stack.ts:60`). An earlier revision of this section claimed the index had been
removed in Phase 4, and misspelled it `GlueJobRunIdIndex`; both halves were wrong. The job completion
Lambda nevertheless does *not* use it on this path — it has `run_id` and `step_name` from the event
payload, so it queries the base table directly, which is cheaper than a GSI lookup. The index exists
for the reverse direction: resolving a Glue job run id back to its state row.

---

## Schema Relationships Diagram

### Detailed Entity Relationship

The same relationships with the S3 buckets included, showing where artifacts leave DynamoDB. Keys and
attributes are deliberately absent — see [DYNAMODB-TABLES.md](DYNAMODB-TABLES.md) for those.
`..._` abbreviates the `agentic_data_ingestion_` prefix.

```
┌────────────────────────────────────────────────────────────────────────┐
│                    agentic_data_ingestion_pipelines                    │
│                          (Master Pipeline)                             │
└────────────────────────┬───────────────────────────────────────────────┘
                         │
         ┌───────────────┼─────────────────┬──────────────────┐
         │               │                 │                  │
         ▼               ▼                 ▼                  ▼
┌─────────────────┐ ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐
│ ..._approvals   │ │ ..._state        │ │ ..._notifications│ │ ..._executions       │
│                 │ │ (Execution State)│ │                  │ │ (Stream-populated)   │
└────────┬────────┘ └────────┬─────────┘ └──────────────────┘ └──────────────────────┘
         │                   │                                           ▲
         │ 1:1               ├─ Stream: NEW_IMAGE ─▶ execution_updater ───┘
         ▼                   │
┌──────────────────┐         │
│ ..._task_tokens  │         │
│ (7-day TTL)      │         │
└──────────────────┘         │
                   ┌─────────┴────────┬──────────────┬────────────────┐
                   │                  │              │                │
                   ▼                  ▼              ▼                ▼
       ┌────────────────┐  ┌──────────────┐  ┌──────────────┐  ┌─────────────┐
       │ ..._generated_ │  │ ..._test_    │  │ ..._schema_  │  │ S3 Buckets  │
       │ code           │  │ results      │  │ mappings     │  │             │
       │                │  │              │  │              │  │ - code/     │
       │ (code_s3_uri   │  │              │  │              │  │ - schemas/  │
       │  points to S3) │  │              │  │              │  │ - test-out/ │
       └───────┬────────┘  └──────────────┘  └──────────────┘  └─────────────┘
               │                                                      ▲
               └──────────────────────────────────────────────────────┘
                   the row holds a URI; the artifact itself is in S3
```

---

## Data Lifecycle

### 1. Hot Data (Active Execution)
- **Duration**: Pipeline execution time (5-30 minutes)
- **Tables**: agentic_data_ingestion_state, agentic_data_ingestion_approvals
- **Access**: High read/write frequency
- **Capacity**: `PAY_PER_REQUEST`, like every other table — there is no provisioned tier to size

### 2. Warm Data (Recent History)
- **Duration**: 7-30 days
- **Tables**: All tables
- **Access**: Dashboard queries, audit reviews
- **Optimization**: Pay-per-request billing

### 3. Cold Data (Historical)
- **Duration**: 30+ days
- **Tables**: agentic_data_ingestion_state, agentic_data_ingestion_test_results
- **Access**: Rare, analytical queries
- **Optimization**: Consider export to S3 + Athena for cost savings

### 4. TTL-Managed Data
- **agentic_data_ingestion_approvals**: Auto-delete **30 days** after creation. Row retention only —
  the approval **window** is 72 hours, enforced by the Step Functions gate, not by this TTL
- **agentic_data_ingestion_notifications**: Auto-delete after 30 days
- **agentic_data_ingestion_task_tokens**: Auto-delete after 7 days (backstop, deliberately longer
  than the 72 h window so the token outlives the window it exists to survive rather than racing it)
- **Benefit**: Automatic cleanup, no manual housekeeping

---

## Query Patterns

### 1. Real-Time Monitoring Queries

**Pipeline Status Dashboard**:
```sql
-- Conceptual SQL (DynamoDB PartiQL)
SELECT pipeline_id, status, created_at
FROM agentic_data_ingestion_pipelines
WHERE status IN ('running', 'pending')
ORDER BY created_at DESC
```

**Recent Errors**:
```sql
SELECT pipeline_id, step_name, error_details, timestamp
FROM agentic_data_ingestion_state
WHERE status = 'error' AND timestamp > '2024-11-11T00:00:00Z'
ORDER BY timestamp DESC
```

### 2. Analytics Queries

**AI Model Performance**:
```sql
SELECT ai_model_used,
       COUNT(*) as total_runs,
       AVG(execution_time_ms) as avg_duration,
       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes
FROM agentic_data_ingestion_state
WHERE step_name = 'schema_mapping'
GROUP BY ai_model_used
```

**Schema Mapping Confidence Distribution**:
```sql
SELECT
  COUNT(*) as total_mappings,
  AVG(confidence_score) as avg_confidence,
  SUM(CASE WHEN confidence_score < 0.8 THEN 1 ELSE 0 END) as below_auto_approve
FROM agentic_data_ingestion_schema_mappings
```

### 3. Operational Queries

**Stale Approvals**:
```sql
SELECT approval_id, pipeline_id, created_at
FROM agentic_data_ingestion_approvals
WHERE status = 'pending' AND created_at < (NOW() - INTERVAL '6 HOURS')
```

**Glue Job Execution Summary**:
```sql
SELECT p.glue_job_name,
       COUNT(DISTINCT s.run_id) as total_runs,
       AVG(s.execution_time_ms) as avg_duration
FROM agentic_data_ingestion_pipelines p
JOIN agentic_data_ingestion_state s ON p.pipeline_id = s.original_pipeline_id
WHERE s.step_name = 'glue_job_creation'
GROUP BY p.glue_job_name
```

---

## Best Practices

### 1. Key Design

**Natural Keys**:
- Use natural keys that match access patterns: `run_id` (PK) + `step_name` (SK) for the state table
- The state table PK was changed from synthetic `{run_id}.{seq}` to natural `run_id` in Phase 4
- This makes the most common query ("get all steps for a run") a simple base table Query

**Hash Keys**:
- Use high-cardinality attributes for even distribution
- Avoid hot partitions (e.g., status with only 3-4 values as hash key)

### 2. GSI Design

**Selective Indexes**:
- Create GSIs only for specific access patterns
- Each GSI doubles storage costs
- Limit projections to required attributes (not ALL)

**Query Optimization**:
- Use sparse indexes (GSI on attributes that don't exist on all items)
- Leverage RANGE keys for sorting and filtering

### 3. Data Modeling

**Single Table Design** (Not Used):
- Agentic Data Ingestion uses multiple tables for clarity and flexibility
- Trade-off: Easier to understand, slightly higher cost

**Denormalization**:
- Store frequently-accessed data in multiple places
- Example: `target_schema` in both pipelines and state tables
- Reduces cross-table lookups

### 4. Consistency

**Eventually Consistent Reads**:
- Use for dashboard queries (cost savings)
- Acceptable for UI with ~1 second lag

**Strongly Consistent Reads**:
- Use for workflow decisions (approval status)
- Critical for Step Functions state checks

### 5. Monitoring

**Key Metrics**:
- `UserErrors` (client-side issues, e.g., validation failures)
- `SystemErrors` (server-side issues)
- `ConsumedReadCapacityUnits` and `ConsumedWriteCapacityUnits` (still emitted under
  `PAY_PER_REQUEST` — they measure consumption, not headroom against a provisioned ceiling)
- `ThrottledRequests` — under `PAY_PER_REQUEST` this signals a hot partition or a burst past the
  table's adaptive limit, **not** under-provisioning

**Alarms**:
- Throttling > 0 for 5 minutes → investigate the partition key distribution; there is no capacity
  dial to turn up
- System errors > 10 in 1 minute → Alert ops team

### 6. Security

**Encryption**:
- At-rest: AWS-managed keys (SSE-S3)
- In-transit: TLS 1.2+ for all API calls

**Access Control**:
- IAM roles with least-privilege policies
- Lambda execution roles scoped to specific tables
- No direct table access from dashboard (API Gateway only)

**Audit**:
- Enable DynamoDB Streams for change tracking
- CloudTrail for API call logging
- CloudWatch Logs for application-level audit

---

## Summary

The Agentic Data Ingestion database architecture spreads 17 `PAY_PER_REQUEST` tables across distinct
access patterns:
- **State tracking**: high write volume, one row per step per run
- **Configuration**: low-volume reads and writes on the pipelines table
- **Denormalisation**: the executions summary is derived from the state table's stream, not written
  twice by application code
- **Approvals**: transient rows, 72-hour decision window, 30-day row retention

The design emphasizes:
- **Scalability**: GSIs for the queries that would otherwise scan
- **Data lifecycle**: TTL for automatic cleanup of transient rows
- **Flexibility**: multiple tables for clear separation of concerns

**Reuse is not caching.** No table in this architecture is a lookup cache for AI output. Mapping
reuse runs through *approved* mappings only, and still passes a human — see §Relationship Details 4.

## Related Documentation

- **[DYNAMODB-TABLES.md](DYNAMODB-TABLES.md)** — the per-table reference: keys, attributes, GSI
  definitions, capacity, example records for every table. Start there for any single-table question
- [ARCHITECTURE.md](ARCHITECTURE.md) — system overview and the Step Functions state machine
- [WORKFLOW.md](WORKFLOW.md) — pipeline execution, gates and step framing

---

**Document revision**: 4.0 (cross-table design document; per-table reference split out to DYNAMODB-TABLES.md). This is the revision of this document, not the project's release version, which is in [CHANGELOG.md](../CHANGELOG.md). No "last updated" date is given, for the reason [WORKFLOW.md](WORKFLOW.md) records: a date is a claim about the whole file that nobody re-verifies when one section changes.
