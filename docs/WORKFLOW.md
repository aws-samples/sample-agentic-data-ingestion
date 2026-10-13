# Pipeline Workflow Documentation

## Overview

Agentic Data Ingestion executes a 10-step intelligent pipeline that validates, analyzes, generates, tests, deploys, and runs AWS Glue ETL jobs with inline data quality checks. This document provides a comprehensive walkthrough of each step, including inputs, outputs, AI integration points, approval gates, and conditional logic.

---

## Table of Contents

1. [Pipeline Overview](#pipeline-overview)
2. [Queue admission, loss and skips](#queue-admission-loss-and-skips)
3. [Step-by-Step Workflow](#step-by-step-workflow)
4. [Approval Gates](#approval-gates)
5. [Parameter Contract System](#parameter-contract-system)
6. [Conditional Logic](#conditional-logic)
7. [Error Handling](#error-handling)
8. [State Tracking](#state-tracking)
9. [Execution Examples](#execution-examples)
10. [Summary](#summary)

---

## Pipeline Overview

### Workflow Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Pipeline Execution                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 1: File Validation                                            │
│  Lambda: agentic_data_ingestion_file_validation                           │
│  Input: source_file_path                                            │
│  Output: validation_status, file_size_mb, record_count              │
│  Duration: ~2-5 seconds                                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 2: Schema Extraction                                          │
│  Lambda: agentic_data_ingestion_schema_extract                            │
│  Input: source_file_path, file_format                               │
│  Output: source_schema, source_schema_hash, field_count             │
│  Duration: ~3-10 seconds (depends on file size)                     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 3: Schema Comparison                                          │
│  Lambda: agentic_data_ingestion_schema_compare                            │
│  Input: source_schema, target_schema                                │
│  Output: drift_detected, compatibility, new_fields, removed_fields  │
│  Duration: ~1-2 seconds                                             │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 4: Schema Mapping (AI + Mapping Memory + Approval Gate)      │
│  Lambda: agentic_data_ingestion_schema_mapping                            │
│  Input: source_schema, target_schema, schema_change_action          │
│  AI: Bedrock Claude for semantic field matching                     │
│  Output: mappings, confidence_scores, unmapped_fields               │
│  Approval: Required on schema change or if confidence < 0.85       │
│  Duration: ~5-15 seconds (AI) / up to 72 hours (with approval)     │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 5: Code Generation (Template-First + AI Transform)            │
│  Lambda: agentic_data_ingestion_create_code                               │
│  Input: mappings, source_config, target_config                      │
│  AI: Bedrock generates transform block only (5-15 lines)            │
│  Template: Jinja2 scaffold handles boilerplate (~70 lines)          │
│  Output: code_s3_uri, required_params, optional_params              │
│  Approval: none at this step — the code gate is after Step 6        │
│  Duration: ~10-30 seconds (AI generation)                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 6: Code Testing (AI + Glue session, pandas by opt-out)        │
│  Lambda: agentic_data_ingestion_test_code                                 │
│  Input: code_version_id, code_s3_uri                                │
│  AI: Strands Agent for static analysis                              │
│  Executor: Glue session is the fall-through; pandas is opt-out only │
│  Output: test_status, tests_passed, tests_failed, coverage_%        │
│  Duration: ~10-30 seconds (Code Interpreter) / minutes (Glue)       │
│  Approval: Gate 2 (code review) sits AFTER this step                │
│  Approval: Gate 3 (forced test review) on a failed run              │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 7: Infrastructure Provisioning (CONDITIONAL)                  │
│  Lambda: agentic_data_ingestion_create_infrastructure                     │
│  Input: target_type, target_config                                  │
│  Condition: Only if target_type = "NEW_S3_TABLE_BUCKET"             │
│  API: Direct boto3 s3tables client                                  │
│  Also: Registers S3 Tables in Glue Data Catalog for DQ integration  │
│  Output: table_bucket_arn, namespace, table_name                    │
│  Duration: ~3-5 seconds (if executed)                               │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 8: Glue Job Creation (AI + Codegen + Analytics MCP)           │
│  Lambda: agentic_data_ingestion_create_glue_pipeline                      │
│  Input: code_version_id, pipeline_config, infrastructure_details    │
│  AI: one agent on Codegen + Analytics MCP, rule-based fallback      │
│  Output: glue_job_name, worker_type, worker_count, est. cost        │
│  Duration: ~10-30 seconds                                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 9: Create Quality Checks                                      │
│  Lambda: agentic_data_ingestion_create_quality_checks                     │
│  Input: run_id, pipeline_id                                         │
│  Output: quality check configuration                                │
│  Duration: ~5-10 seconds                                            │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Step 10: Run Pipeline (waitForTaskToken + Inline Data Quality)     │
│  Lambda: agentic_data_ingestion_run_pipeline                              │
│  Input: run_id, pipeline_id, taskToken                              │
│  Mechanism: Starts Glue job, waits for completion callback          │
│  Inline DQ: EvaluateDataQuality transform with DQDL rules          │
│  Output: job_status, data_quality_results                           │
│  Duration: ~2-30 minutes (depends on data volume)                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Pipeline Complete                               │
│         Glue Job Executed with Inline Data Quality Results           │
└─────────────────────────────────────────────────────────────────────┘
```

### Execution Timeline

| Scenario | Without Approval | With Schema Approval | With Code Approval | Both Approvals |
|----------|------------------|----------------------|--------------------|----------------|
| **Duration** | 5-35 minutes | up to 72 hours | up to 72 hours | up to 144 hours |
| **AI Invocations** | 3 (mapping, code gen, testing) | 3 | 3 | 3 |
| **Human Intervention** | 0 | 1 (schema review) | 1 (code review) | 2 |

The code-review column is Approval Gate 2, which sits **after Step 6 (Code Testing)**, not after Step
5. All four columns assume the test passes. A run whose test does not pass, and whose regeneration
budget cannot absorb it, reaches Approval Gate 3 (forced test review) — a human gate that no
configuration disables. See [Approval Gates](#approval-gates).

### Target type vocabulary

**There are four target types**: S3 Tables (Iceberg), Glue Catalog, JDBC, and S3 Parquet. The S3
Tables type reaches the workflow under two tokens, `NEW_S3_TABLE_BUCKET` and
`EXISTING_S3_TABLE_BUCKET`, which differ only in whether Step 7 provisions the bucket.

**Only three are offered for a new pipeline.** Glue Catalog is no longer listed by the wizard, and
`create_pipeline` refuses a `GLUE_CATALOG` target with a 400. The workflow below still handles it
in full, because pipelines created before that change keep running: every step that branches on
the target type keeps its Glue Catalog arm.

**`RDS_DATABASE` is a fifth token, not a fifth type.** P17 task 20.4 consolidated it into `jdbc`,
the wizard no longer emits it, and a reader will meet it only in pipeline records persisted before
that consolidation. Every live path ends up treating it as `jdbc`, but only one of them does so by
an alias lookup. The rest reach the same outcome through a parallel branch, which is why the token
is still spelled out in several places rather than having been deleted:

| Path | Mechanism |
|---|---|
| Dashboard API read | `_LEGACY_TARGET_TYPE_ALIASES` in `dashboard_api/handlers/schemas.py` maps `rds_database` → `jdbc` inside `_target_type_label`, so the normalisation is structural rather than repeated per call site |
| Wizard edit / hydrate | `migrateLegacyTargetConfig` in `dashboard-react/src/pages/CreatePipelinePage.tsx` rewrites a stored `RDS_DATABASE` to `jdbc` before the wizard sees it, because the `targetType` union no longer contains it |
| Create / update validation | **Still accepted on write.** `pipelines_crud.py` carries a dedicated `RDS_DATABASE` branch beside the canonical `JDBC` one, asserting the same `jdbc_url` / `jdbc_driver` / `jdbc_table` presence checks, and both tokens then share the `_augment_glue_connection_config` and manual-mode networking validation |
| Codegen | **Not collapsed.** It keeps its own `TEMPLATE_RULES` entry resolving to the same JDBC template, so the rendered script is identical while the rule is parallel rather than aliased. See [Step 5](#step-5-code-generation-template-first--ai-transform) |
| Glue job arguments | **No token-specific credential handling.** `build_jdbc_arguments` in `create_glue_pipeline/job_arguments.py` emits the same `--secret_arn` reference for every JDBC target and never a plaintext `jdbc_user` / `jdbc_password`, a standing invariant. The `_resolve_rds_secret_field` this row used to cite fetched the plaintext inside the Lambda, had no production caller, and has been deleted |

So the write path is deliberately tolerant and the read paths are normalising. Do not add
`RDS_DATABASE` to a list of target types a user can choose, and do not delete the handling either:
persisted records still carry it, and the branches above are what make those pipelines render and
run as JDBC. [ARCHITECTURE.md](ARCHITECTURE.md) and [DASHBOARD.md](DASHBOARD.md) state the same
four-not-five rule from the architecture and wizard sides.

---

## Queue admission, loss and skips

Step 0 of the workflow. Before Step 1 runs, an upload has to be *admitted*: it travels
S3 → EventBridge → `PipelineTriggerQueue` → `agentic_data_ingestion_pipeline_trigger`
(`deploy-cdk/lib/orchestration-stack.ts:1657`), and the trigger can defer it, skip it, or —
past a bound — lose it. All three outcomes are recorded. This section is the single home for
that mechanism; [DYNAMODB-TABLES.md](DYNAMODB-TABLES.md) carries the attribute names and
types only.

### Deferral is correct; the bound on it is what nobody wrote down

When another execution already holds the per-pipeline lock, the trigger returns that record
in `batchItemFailures` and SQS redelivers it after the visibility timeout. That is the
intended behaviour and is **not** a loss. What p54 established is the bound on it:

| Quantity | Value | Verified at |
|---|---|---|
| `visibilityTimeout` on `PipelineTriggerQueue` | 30 s | `orchestration-stack.ts:1548` |
| `maxReceiveCount` before the DLQ | **1000** — SQS's documented ceiling, raised from 50 | `orchestration-stack.ts:1554` |
| Redelivery budget — the product of the two | 30000 s = **8 h 20 min** | `orchestration-stack.ts:1476-1482` |
| The hold it must cover: `APPROVAL_TIMEOUT_SECONDS` | 259200 s = **72 h** | `orchestration-stack.ts:70` |

`maxReceiveCount` is the budget's *length*, not a failure count: each receive is one retry
after the visibility timeout elapses. Do not raise it expecting more budget — 1000 is the
ceiling and there is none beyond it (`orchestration-stack.ts:1552-1553`).

**`pipeline_trigger`'s own Lambda timeout is 5 seconds, and that is not a defect.** It is
fixed there by the constraint `queueVisibilityTimeout >= lambdaTimeout * 6`, so 30 / 6 = 5 is
the maximum the queue permits — the comment at `orchestration-stack.ts:1667-1668` and the
value at `:1670`. Raising it breaks SQS. `visibilityTimeout` is likewise held at 30 s
deliberately: raising it would also buy budget, but it idles every deferral for up to that
long even when the lock clears in seconds, and it is read by this ratio as well, so moving it
moves two things (`:1520-1526`).

### p54 R10.2 is UNSATISFIED, by 63 h 40 min

R10.2 requires the redelivery budget to be no shorter than the lock hold. It is still
shorter, and the residual is published rather than claimed closed. That pairing is the
ratified decision: raising the budget on its own would have left the remaining loss silent
to the operator, so the shortfall is stated rather than allowed to imply success:

```
covering 72 h at a 30 s visibility timeout needs   8640 receives
maxReceiveCount is capped by SQS at                1000 receives
so the budget reaches                             30000 s ≈ 8 h 20 min
of                                               259200 s = 72 h
```

11.6% covered — a **63 h 40 min shortfall**. It was 0.6% covered before, so this is a 20×
reduction in exposure and **not a fix** (`orchestration-stack.ts:1498-1509`). Closing it by
geometry is impossible: 8640 receives is unreachable. The arm that would close it
structurally is a durable pending-files table, in its own table, which p54 did not build.

The gap is asserted rather than merely commented:
`tests/contracts/test_pipeline_trigger_redelivery_budget_cross_language.py` parses both
literals and `APPROVAL_TIMEOUT_SECONDS` out of the TypeScript and carries the inequality as a
deliberate `xfail`. Closing the gap flips that test to an unexpected pass, which is the
signal to delete it.

### A lost file gets a durable record, not silence

Past the budget SQS moves the message to `PipelineTriggerDLQ`
(`orchestration-stack.ts:1468`), whose 14-day retention is **transport** retention and not
the record — the message is gone after a fortnight (`:1463-1467`). The durable record is
written by `QueueLossRecorderFunction` (`orchestration-stack.ts:1769`, asset at `:1785`), the
DLQ's own consumer, from
`lambda/agentic_data_ingestion_queue_loss_recorder/lambda_function.py`. It writes the record
and stops — it does not redrive, resubmit, or start anything.

The row is a *complete* executions-table row on the content-addressed key
`queued-{sha256(source_key)[:16]}` — byte-for-byte the key
`pipeline_trigger._track_queued_file` uses, so the handler needs no lookup — and it is
idempotent by construction. Four properties of it are load-bearing:

- **`status` stays `"QUEUED"`.** A fourth `executions` status was rejected: several readers
  switch exhaustively on the existing set, the `StatusIndex` GSI is partitioned on `status`,
  and the run-history read treats every non-`QUEUED` row as a run, so a novel value would
  render as a phantom run. The loss therefore rides on a separate attribute,
  `queue_outcome = "lost_to_dlq"` — constants at
  `queue_loss_recorder/lambda_function.py:118-119`, `status` written at `:214` and the
  outcome at `:227`. Carrying the loss on its own attribute is a ratified decision, not an
  accident of shape: it lets every existing `status` reader stay correct untouched.
- **`started_at` is present, and that is what makes the record readable at all.**
  `RunsByTimeIndex` is `(pipeline_id, started_at)`, and a GSI indexes only items carrying
  *both* key attributes, so a row without it is absent from the index **silently**. That
  sparseness is a standing invariant, accepted rather than designed out, and the exposure is
  held down instead by a test sweep that finds executions-table writers by key shape and a
  detector script that reports an unindexed row after the fact. `started_at` is set to the
  instant the loss was *recorded*, because the original queue time is not recoverable from a
  DLQ message and inventing one would put a fabricated value on an index sort key
  (`:217-223`).
- **`put_item`, not `update_item`.** Raising `maxReceiveCount` to 1000 reversed an ordering:
  the 8 h 20 min budget now exceeds the 1 h `QUEUED_RECORD_TTL_SECONDS`, so by the time a
  message reaches the DLQ its queued placeholder has normally expired. An `update_item`
  against a missing item *creates* it, which would mint `{pipeline_id, run_id,
  queue_outcome}` with no `status` and no `started_at` — an unindexed, invisible phantom. A
  complete `put_item` is the only form that survives the raise (`:48-59`).
- **The record's TTL is 30 days** — `LOST_FILE_RECORD_TTL_SECONDS = 30 * 24 * 60 * 60` at
  `shared/config.py:306`, applied at `queue_loss_recorder/lambda_function.py:225`. It is a
  fourth TTL constant on purpose and is deliberately *not* passed as a CDK environment
  variable, so the behavioural value keeps one home (`orchestration-stack.ts:1778-1783`).

Failure posture: a permanently-malformed body is reported as **success** — there is no DLQ
behind this DLQ, and no `source_key` to write a record under — and logged at ERROR. A
DynamoDB write failure is reported as a **failure**, so the message returns and the next
delivery can write the record.

### Reading a lost file: `lost_files`, and why it is its own array

`GET /api/pipelines/{id}/executions` publishes a third top-level array, `lost_files`
(`lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py:1851`) — always
present, `[]` when the pipeline has lost nothing. It comes from `_lost_files` (`:1023`), a
**separate keyed read**, not from a widened `queued_files`, and that separation is a ratified
decision.

The reason is that the durable record was readable for about an hour. `queued_files` is taken
from the **first page** of `RunsByTimeIndex` only — deliberately so, since accumulating it
across a filter walk would make the array a function of how far the filter had to search,
and the same request under two `?status=` values would return different queued files. But a
loss record lives 30 days while its `started_at` is when the loss was *recorded*, so on an
active pipeline it drops off page 1 within the hour and then exists, correctly, where nothing
reads it. The condition that creates a lost file — a busy pipeline holding an approval lock —
is the same condition that buries the evidence.

**The read is a keyed `Query` on the base table and carries no `IndexName`**
(`pipelines_runs.py:1105-1109`): a `KeyConditionExpression` on `pipeline_id` plus
`begins_with(run_id, "queued-")`, a `FilterExpression` on `queue_outcome`, and a
`ProjectionExpression`. Because it never consults `RunsByTimeIndex`, **this array cannot
inherit that index's sparseness** — a loss record is visible here on the strength of its key
alone. The `FilterExpression` is a cost reduction, not the decision; what decides publication
is `_is_lost_queued_row` (`:941`), the same predicate the `queued_files` split uses.

Two shapes were rejected and must not be reintroduced: widening the `queued_files` entry (its
four keys are pinned by set equality in
`test_split_derive_and_filter_order_characterization.py`), and accumulating `queued_files`
across pages. The published `lost_files` entry is four keys — `file_name`, `source_key`,
`lost_at`, `lost_reason` — and deliberately carries **no `run_id`**: the row has a real one,
but `get_run_details` on a placeholder row 404s, so publishing it would hand the frontend
something that looks navigable and is not.

### Skips: the `skipped` and `skip_reason` vocabulary

A skip is a different outcome from a loss — the trigger decided not to start a run, and said
so. Two spellings carry it, and they live in different places:

| Name | Type | Where it lives |
|---|---|---|
| `skipped` | Boolean | The trigger's **response payload** only, alongside `skip_reason` — `pipeline_trigger/lambda_function.py:817`, `:1108`, `:1133`, `:1148`. Not a stored attribute. |
| `skip_reason` | String | A **stored** attribute, on an executions-table row with `status="SKIPPED"` — written at `pipeline_trigger/lambda_function.py:233` and `:494`. |

`skip_reason` is the discriminator, so there is one row type distinguished by reason rather
than three row types (`pipeline_trigger/lambda_function.py:308-320`). Three values exist:

| Value | Meaning | Defined at |
|---|---|---|
| `duplicate_content` | Identical size and ETag already processed successfully | `shared/processed_file_ledger.py:92` |
| `global_concurrency` | The fleet-wide concurrency ceiling was reached | `pipeline_trigger/lambda_function.py:324` |
| `pipeline_lock_contention` | Another execution already holds this pipeline's lock | `pipeline_trigger/lambda_function.py:327` |

For a human-initiated run the HTTP status separates them: `409` for `duplicate_content` — a
human asked and the answer is "already done", a conflict to acknowledge rather than
contention that clears on its own (`:811-818`) — and `429` for the two contention reasons
(`:1133`, `:1148`).

The dashboard carries a matching table at `dashboard-react/src/utils/skip-reason.ts`. That
duplication is unavoidable across the Python/TypeScript boundary; **adding a reason on one
side only renders as an "unrecognised reason" badge**, which is the deliberate fallback
rather than a silent mislabel.

### Deploy ordering: Compute before Orchestration

`QueueLossRecorderFunction` lives in the **Orchestration** stack
(`orchestration-stack.ts:1769`). The only thing that interprets the rows it writes is the
**Compute**-side reader — `_lost_files` in the dashboard API Lambda (`compute-stack.ts:1026`;
handler at `lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_runs.py:1023`).

So **Compute must be deployed before Orchestration.** Orchestration-first turns the recorder
on while nothing can read what it writes, which leaves the dashboard publishing `queued` for
a file that will never be processed — the exact statement this mechanism exists to remove —
and makes it durable for 30 days instead of expiring in an hour. That is worse than not
deploying the mechanism at all. `cdk deploy --all` gets the order right unaided
(`orchestration.addDependency(compute)`, `deploy-cdk/bin/agentic_data_ingestion.ts:197`); an
individual or `--exclusively` deploy does not.

> **One-way door on the next Orchestration deploy.** The DLQ event source mapping is
> `enabled: true` (`orchestration-stack.ts:1838`). Consuming an SQS queue deletes its
> messages, so the next Orchestration deploy drains whatever the DLQ is holding — in
> the validation account that was four real dead-lettered uploads an operator could otherwise still
> have redriven, now with 8 h 20 min of budget rather than the 25 min they originally
> exhausted. The operator ratified that trade explicitly: accept losing the four in exchange
> for the recording mechanism being live (`:1796-1824`).

---

## Step-by-Step Workflow

### Step 1: File Validation

**Lambda Function**: `agentic_data_ingestion_file_validation`

**Purpose**: Validate source file format, size, and structure before processing.

#### Inputs

```json
{
  "source_file_path": "s3://agentic-data-ingestion-source-dev/customer_data.csv",
  "file_format": "CSV",
  "pipeline_id": "pipeline-customer-data",
  "run_id": "run-20241112-abc123"
}
```

#### Processing Logic

1. **Format Validation**
   - Recognises the extension and resolves it to a canonical format via
     `FORMAT_MAP` (`file_validation/lambda_function.py`). Several extensions
     collapse onto one canonical format: `.csv`, `.tsv` and `.txt` → `csv`;
     `.json`, `.jsonl` and `.ndjson` → `json`; `.parquet` and `.pq` → `parquet`;
     `.avro` → `avro`; `.orc` → `orc`; `.xlsx` → `excel`. A `.gz`, `.bz2`,
     `.snappy` or `.zst` suffix is stripped before the lookup.
   - The three members of the `csv` family differ only in their field delimiter,
     which is **not** part of the canonical format: `FORMAT_DELIMITERS` in
     `shared/format_delimiters.py` is keyed on the extension and answers `,` for
     `.csv`, a tab for `.tsv` and `|` for `.txt`. A `.txt` file that is not
     pipe-delimited therefore parses as one column — the same accepted residual
     `.tsv` carries, since detection is extension-only.
   - An unrecognised extension fails the step permanently, naming the
     recognised list.
   - This step issues a HEAD only and reads no object bytes on any format,
     including `.xlsx`. Whether the *original* object is readable by a Glue job
     is a separate question, answered by `NATIVE_FORMATS` in the same module —
     see Step 2.

2. **Size Validation**
   - Maximum: 5 GB
   - Reason: Lambda memory limits for in-memory processing
   - Larger files: Recommend direct Glue Crawler

3. **Structure Validation**
   - CSV: Check for valid delimiter, quote characters
   - JSON: Validate JSON syntax
   - Parquet: Check Parquet metadata
   - Excel: nothing is checked here. The workbook is opened at schema
     extraction (Step 2), which is where a missing or ambiguous worksheet
     selection fails the run.

4. **Record Count Estimation**
   - CSV: Count lines (approximate)
   - Parquet: Read row group metadata
   - JSON: Count top-level objects

#### Outputs

```json
{
  "validation_status": "valid",
  "file_size_mb": 234.5,
  "record_count": 1500000,
  "file_format": "CSV",
  "validation_details": {
    "delimiter": ",",
    "has_header": true,
    "encoding": "utf-8"
  }
}
```

#### Error Scenarios

| Error | Condition | Resolution |
|-------|-----------|------------|
| `INVALID_FORMAT` | Unsupported file format | Convert to supported format |
| `FILE_TOO_LARGE` | File > 5 GB | Use Glue Crawler instead |
| `CORRUPTED_FILE` | Cannot read file | Re-upload file |
| `FILE_NOT_FOUND` | S3 path invalid | Check S3 path and permissions |

#### State Tracking

```json
{
  "pipeline_id": "run-20241112-abc123.1",
  "step_name": "file_validation",
  "status": "success",
  "execution_time_ms": 2345,
  "output_data": "{\"validation_status\": \"valid\", ...}"
}
```

---

### Step 2: Schema Extraction

**Lambda Function**: `agentic_data_ingestion_schema_extract`

**Purpose**: Parse source file and extract schema (field names, types, nullable, etc.).

#### Inputs

```json
{
  "source_file_path": "s3://agentic-data-ingestion-source-dev/customer_data.csv",
  "file_format": "CSV",
  "validation_details": {
    "delimiter": ",",
    "has_header": true
  }
}
```

#### Processing Logic

1. **Format-Specific Parsers**
   - **CSV**: ranged GET of the first 64 KB, up to 100 rows parsed by `csv.DictReader` on the delimiter the object's extension implies — so `tsv` and `txt` arrive here too. Was S3 Select with this read as a fallback; that arm is gone (see [ARCHITECTURE.md](ARCHITECTURE.md#streaming-schema-extraction-phase-9))
   - **JSON**: ranged GET of the first 64 KB, complete records parsed from the partial chunk
   - **Parquet**: PyArrow footer metadata, via a seekable `pyarrow.fs.S3FileSystem` handle
   - **Excel**: openpyxl over the whole object — one field per column of the
     processed worksheet, named from that column's header cell — **plus a
     conversion of the source object to CSV**, because Spark and Glue have no
     Excel datasource
   - **Avro**: OCF header metadata (`avro.schema`), from one bounded ranged GET
   - **ORC**: postscript + footer metadata, via a seekable `pyarrow.fs.S3FileSystem` handle

   `schema_extract` dispatches on the canonical format that `file_validation`
   resolved from the extension, with one branch per canonical format:
   `csv` (`.csv`, `.tsv`, `.txt`), `json` (`.json`, `.jsonl`, `.ndjson`),
   `parquet` (`.parquet`, `.pq`), `avro` (`.avro`), `orc` (`.orc`) and `excel`
   (`.xlsx`). A compression suffix is stripped before the lookup, so
   `data.avro.gz` arrives here as `avro`.

   **Excel is the one canonical format the Glue job cannot read from the source
   object, and the conversion happens here.** The gate is `NATIVE_FORMATS` in
   `file_validation` — the set of canonical formats a Glue job reads directly
   from the source object, with no prior conversion of that object. `csv`,
   `json`, `parquet`, `avro` and `orc` are members; `excel` is not, so the
   `excel` branch writes a converted CSV to the processed bucket and reports its
   URI as `readable_source_uri`, alongside `readable_source_format`. That pair
   is present on every result this step returns, native or converted, so
   downstream consumers read a resolved value and none of them re-derives the
   gate. The conversion is not sited here by preference — this step already has
   the workbook open to satisfy the schema requirement, and every other
   placement opens it twice.

   **A multi-sheet workbook needs a worksheet selection, and the selection is
   pipeline configuration.** It is set on the pipeline (`selected_worksheet`,
   chosen in the Create Pipeline Wizard's Source step) and read once per run,
   which is what makes every subsequent run of that pipeline process the same
   sheet. It is never guessed:
   - one worksheet and no selection → that worksheet is processed;
   - more than one worksheet and no selection → the run **fails** and the
     message names every worksheet present, since that is the list to choose
     from;
   - a selection naming a worksheet the workbook does not contain → the run
     **fails**, reporting both the configured name and the names present.

   A `.xlsx` that cannot be opened as a workbook — not a zip, truncated,
   password-protected — **fails the run**, naming the object and the reason,
   rather than returning an empty schema. `openpyxl==3.1.5` is pinned in
   `shared/runtime_requirements.txt` and installed into the layer this Lambda
   attaches.

   > **Not yet followable:** the worksheet selection is write-only on the API
   > today. The wizard sends it and pipeline creation stores it, but no API read
   > path returns it and there is no post-creation edit path — so a user cannot
   > see or change what they set, even though the refusal above tells them to set
   > it on the pipeline. Changing a selection currently means recreating the
   > pipeline.
   >
   > Nothing in this section has been observed end to end. It describes the code
   > in this tree; no workbook has been uploaded to a deployed pipeline and no
   > Glue job has read a converted CSV.

   **Avro and ORC schemas are read from the container's own file metadata** —
   the field list is what the writer recorded, not what a sample of rows
   suggests. Neither extractor downloads the object or writes to `/tmp`. Avro's
   OCF header sits at the *start* of the file, so a single bounded ranged GET
   (64 KB by default) reaches it. ORC's postscript and footer sit at the *end*,
   so it needs the seekable `pyarrow.fs.S3FileSystem` handle; a forward-only
   ranged GET cannot substitute. Measured cost is one ranged read each — up to
   64 KB for Avro and 16 KB for ORC, with a second read only when an ORC footer
   overflows that block. Parquet reads its footer through the same seekable
   handle.

   `csv` and `json`, by contrast, are *sampled*: types are inferred from the
   first 100 rows or the first 64 KB, so a value absent from the sample is
   absent from the schema. Every CSV column comes back as `string`, because
   `csv.DictReader` yields every cell as a string and the extractor does not
   attempt numeric coercion. (This used to be attributed to S3 Select's
   untyped JSON output. The attribution changed with the reader; the outcome
   did not.)

   Avro and ORC also fail differently, deliberately:
   - Metadata that cannot be read **fails the run at `schema_extract`**. Neither
     extractor returns an empty schema — a run reporting success with zero
     fields is the defect they were built to remove.
   - An externally compressed container (`data.avro.gz`, `data.orc.zst`) is
     refused with gzip, bzip2 or zstd named as the cause, and a message to
     upload the container uncompressed rather than a parse error that reads as
     file corruption.

2. **Schema Inference**
   - Field names (from header or keys)
   - Data types (string, integer, float, boolean, timestamp)
   - Nullable (check for NULL/None values in sample)
   - Sample values (for AI context)

3. **Hash Generation**
   - SHA-256 hash of schema structure
   - Used for schema drift detection and mapping-record identity
   - Deterministic: same schema = same hash

#### Outputs

```json
{
  "source_schema": [
    {
      "field_name": "customer_id",
      "data_type": "integer",
      "nullable": false,
      "sample_values": ["1001", "1002", "1003"]
    },
    {
      "field_name": "first_name",
      "data_type": "string",
      "nullable": false,
      "sample_values": ["John", "Jane", "Bob"]
    },
    {
      "field_name": "signup_date",
      "data_type": "timestamp",
      "nullable": true,
      "sample_values": ["2024-01-15", "2024-02-20", null]
    }
  ],
  "source_schema_hash": "a7f3b2c1...",
  "field_count": 12
}
```

#### Schema Format

Each field object contains:
- `field_name`: Original field name from source
- `data_type`: Inferred type (string, integer, float, boolean, timestamp, date)
- `nullable`: Whether NULL values are present
- `sample_values`: Up to 5 sample values for AI context

#### Error Scenarios

| Error | Condition | Resolution |
|-------|-----------|------------|
| `NO_SCHEMA_DETECTED` | Empty file or no parseable structure | Check file content |
| `AMBIGUOUS_TYPES` | Cannot infer consistent types | Manually specify schema |
| `ENCODING_ERROR` | Character encoding issues | Convert to UTF-8 |

---

### Step 3: Schema Comparison

**Lambda Function**: `agentic_data_ingestion_schema_compare`

**Purpose**: Detect schema drift between source and target, assess compatibility.

#### Inputs

```json
{
  "source_schema": [...],
  "target_schema": [...],
  "source_schema_hash": "a7f3b2c1...",
  "target_schema_hash": "d4e5f6a7..."
}
```

#### Processing Logic

1. **Drift Detection**
   - New fields in source (not in target)
   - Removed fields (in target but not in source)
   - Type changes (e.g., string → integer)
   - Nullability changes

2. **Compatibility Assessment**
   - **Backward Compatible**: Can map source to target without data loss
   - **Forward Compatible**: Target can accommodate new source fields
   - **Breaking Change**: Requires schema update or manual intervention

3. **Change Summary**
   - Count of new, removed, modified fields
   - Severity classification (minor, major, breaking)

#### Outputs

```json
{
  "drift_detected": true,
  "compatibility": "backward_compatible",
  "new_fields": ["loyalty_points", "referral_code"],
  "removed_fields": [],
  "type_changes": [
    {
      "field": "phone_number",
      "source_type": "string",
      "target_type": "integer",
      "severity": "major"
    }
  ],
  "summary": {
    "total_changes": 3,
    "severity": "major"
  }
}
```

#### Compatibility Matrix

| Source Type | Target Type | Compatible? | Transformation |
|-------------|-------------|-------------|----------------|
| string | string | ✅ Yes | Direct mapping |
| integer | string | ✅ Yes | Cast to string |
| string | integer | ⚠️ Conditional | Cast with validation |
| float | integer | ⚠️ Conditional | Truncate/round |
| timestamp | string | ✅ Yes | Format as ISO 8601 |
| string | timestamp | ⚠️ Conditional | Parse with format |

#### Decision Logic

- **No Drift**: Proceed to schema mapping
- **Minor Drift**: Proceed with automatic adjustments
- **Major Drift**: Flag for review (optional approval)
- **Breaking Changes**: Require approval or pipeline update

---

### Step 4: Schema Mapping (AI + Mapping Memory)

**Lambda Function**: `agentic_data_ingestion_schema_mapping`

**Purpose**: Map source fields to target fields using AI-powered semantic matching, informed by previously approved mappings recalled from AgentCore Memory.

#### Inputs

```json
{
  "source_schema": [...],
  "target_schema": [...],
  "source_schema_hash": "a7f3b2c1...",
  "target_schema_hash": "d4e5f6a7..."
}
```

The auto-approve confidence threshold is not a per-run input — it is the shared constant `CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.85` in `shared/config.py` (single source of truth).

#### Processing Logic

##### 1. Mapping Memory Retrieval

There is no wholesale mapping cache that skips AI generation. Instead, previously **approved** field mappings are recalled from AgentCore Memory (`shared/mapping_memory.py`) and fed into the AI prompt as guidance:

- One batched semantic retrieval per run (not per field), filtered client-side by field name.
- Memory entries are created **on approval only** — never at generation time — so only human-vetted mappings influence future runs.
- Separately, `create_code` substitutes the freshest *approved* mapping for the pipeline when a newer one exists (see `mapping_resolution.py`).

##### 2. AI-Powered Mapping

**AI Model**: Bedrock Converse API (model-agnostic — works with Claude, Nova, Mistral, etc.)

**Prompt Structure**:
```
You are a data integration expert. Map source fields to target fields based on:
1. Field name similarity
2. Data type compatibility
3. Sample values
4. Business logic context

Source Schema:
{source_schema_json}

Target Schema:
{target_schema_json}

For each source field, provide:
- target_field: Best matching target field
- confidence: 0.0-1.0 (1.0 = perfect match)
- transformation: Any required data transformation
- reasoning: Explanation of mapping decision
```

**AI Response**:
```json
{
  "mappings": [
    {
      "source_field": "customer_id",
      "target_field": "cust_id",
      "confidence": 0.98,
      "transformation": "none",
      "reasoning": "Direct semantic match, both are primary identifiers"
    },
    {
      "source_field": "first_name",
      "target_field": "fname",
      "confidence": 0.95,
      "transformation": "none",
      "reasoning": "Common abbreviation pattern"
    },
    {
      "source_field": "phone_number",
      "target_field": "contact_phone",
      "confidence": 0.72,
      "transformation": "format_phone",
      "reasoning": "Both represent phone, but source is string, target expects E.164 format"
    }
  ],
  "unmapped_fields": {
    "source": ["internal_notes"],
    "target": ["preferred_language"]
  },
  "overall_confidence": 0.88
}
```

##### 3. Confidence Scoring

Thresholds come from `shared/config.py` (single source of truth):

- **Average confidence ≥ 0.85** (`CONFIDENCE_AUTO_APPROVE_THRESHOLD`): eligible for auto-approve (unless a schema change or approval policy forces review)
- **Average confidence < 0.85**: human approval required
- **Average confidence < 0.7** (`COMPLEX_MAPPING_THRESHOLD`, env-overridable): additionally eligible for the complex-mapping agent

##### 4. Persist Mapping Record

```python
# Store the mapping record in agentic_data_ingestion_schema_mappings
save_mapping({
    'mapping_id': mapping_id,           # derived from the mapping content hash
    'source_schema_hash': source_schema_hash,
    'target_schema_hash': target_schema_hash,
    'mapping_result': json.dumps(mapping_result),
    'confidence_score': 0.88,
    'ai_model_used': 'claude-sonnet-4.5',
    'created_timestamp': now()
})
```

The AgentCore Memory entry (used by future runs) is written later, when the mapping is **approved** — not here.

#### Outputs

```json
{
  "mapping_id": "map-20241112-xyz789",
  "mappings": [...],
  "confidence_score": 0.88,
  "unmapped_fields": {...},
  "approval_required": false
}
```

#### Approval Gate

**Trigger Conditions**:
1. Schema change detected: `schema_change_action` is `create_new_pipeline`, `update_existing_pipeline`, or `remap_required` (and pipeline's `always_approve_schema_changes` is `True`)
2. Low confidence: `confidence_score < 0.85` (`CONFIDENCE_AUTO_APPROVE_THRESHOLD` in `shared/config.py`)
3. User-configured approval policy

**Approval Flow**:
1. Create approval record in `agentic-data-ingestion-approvals` table
2. Publish notification to dashboard
3. Step Function pauses, waits for task token callback
4. User reviews mappings, sample data, AI reasoning
5. User approves/rejects via dashboard
6. Step Function resumes or fails based on decision

**Timeout**: 72 hours (`APPROVAL_WINDOW_HOURS` in `shared/config.py`, which mirrors the gate's `APPROVAL_TIMEOUT_SECONDS` in `deploy-cdk/lib/orchestration-stack.ts` — the gate is what actually abandons the execution, so the advertised deadline matches it; overridable via the `DEFAULT_TIMEOUT_HOURS` environment variable or per-pipeline `timeout_hours`), after which the timeout handler applies the configured timeout behavior. The stored task token's TTL is deliberately longer (7 days) so it outlives the window rather than racing it

---

### Step 5: Code Generation (Template-First + AI Transform)

**Lambda Function**: `agentic_data_ingestion_create_code`

**Purpose**: Generate PySpark ETL code using a template-first approach — Jinja2 templates handle all boilerplate while AI generates only the transform block.

#### Inputs

```json
{
  "mappings": [...],
  "source_config": {
    "type": "S3",
    "file_path": "s3://agentic-data-ingestion-source-dev/customer_data.csv",
    "file_format": "CSV"
  },
  "target_config": {
    "type": "S3_TABLE",
    "table_bucket_arn": "arn:aws:s3tables:...",
    "namespace": "default",
    "table_name": "customers"
  },
  "glue_version": "5.0",
  "ai_model": "us.anthropic.claude-sonnet-4-6"
}
```

#### Processing Logic

##### 1. Template Selection

The Lambda selects a Jinja2 template on the **pair** `(source_type, target_type)`, not on
`target_type` alone, from an ordered rule list in which the first match wins
(`TEMPLATE_RULES` / `_select_template`, `create_code/code_assembly.py`):

```python
TEMPLATE_RULES: list[tuple[str, str, str]] = [
    ("JDBC",      "*",             "glue_job_jdbc_source.py.j2"),
    ("S3_UPLOAD", "s3_tables",     "glue_job_s3_tables.py.j2"),
    ("S3_UPLOAD", "glue_catalog",  "glue_job_glue_catalog.py.j2"),
    ("S3_UPLOAD", "jdbc",          "glue_job_jdbc.py.j2"),
    ("S3_UPLOAD", "rds_database",  "glue_job_jdbc.py.j2"),  # legacy alias
    ("S3_UPLOAD", "s3_parquet",    "glue_job_s3_parquet.py.j2"),
]
```

The `"*"` wildcard on the first rule is load-bearing: a JDBC **source** routes to the JDBC-source
template whatever the target is, so that rule has to precede the `S3_UPLOAD` ones. No match raises
`ValueError` naming both the raw and the normalised target rather than silently picking a template.

`rds_database` is the **legacy alias** discussed under [Target type
vocabulary](#target-type-vocabulary), and codegen is the one place that does *not* collapse it onto
`jdbc`. Its own normalisation table maps `rds_database` to itself precisely so it reaches a rule of
its own, which then resolves to the same `glue_job_jdbc.py.j2` the canonical `jdbc` target uses.
The effect is identical; the mechanism is a parallel rule rather than an alias lookup. Removing that
entry does not fall through to the `jdbc` rule, it raises.

Each template provides the deterministic scaffold: imports, GlueContext init, argument parsing, source read (format-aware), target write (type-specific), error handling, and `job.commit()`. The template contains a `{{ transform_block }}` placeholder for the AI-generated code.

##### 2. AI Transform Block Generation

The AI receives a simplified prompt (under 500 tokens) requesting only PySpark transformation code:

```
Given these field mappings:
  customer_id (integer) → cust_id (integer)
  first_name (string) → fname (string, trim whitespace)
  signup_date (string) → signup_date (date, parse YYYY-MM-DD)

Generate ONLY the PySpark transformation code.
Input DataFrame: `df`. Output DataFrame: `df_transformed`.
Use only: .select(), .withColumn(), .withColumnRenamed(), .cast(), .filter()
Do NOT create classes or helper functions.
```

The AI generates 5-15 lines of transform code — no imports, no classes, no boilerplate.

##### 3. Template Rendering

```python
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader('pyspark_templates'),
    undefined=jinja2.StrictUndefined,
)
template = env.get_template(template_name)
final_code = template.render(
    transform_block=transform_block,
    **pipeline_metadata['target_config'],
)
```

Result: ~80 lines instead of 500. The AI never generates `GlueJobMetrics`, `DataQualityValidator`, `DataTransformer`, or other wrapper classes — these are eliminated by the template approach.

##### 4. Validation and Storage

```python
# Validate syntax
validate_generated_code(final_code)

# Upload to S3
code_s3_uri = f"s3://agentic-data-ingestion-code-dev/code-{code_version_id}.py"
s3.put_object(Bucket=bucket, Key=key, Body=final_code)

# Store metadata with standardized output_data
output_data = {
    'artifact_type': 'code_generation',
    'primary_id': code_version_id,
    'status': 'success',
    'summary': {
        'line_count': line_count,
        'template_used': template_name,
        'target_type': target_type,
    },
    's3_uris': {
        'code_file': code_s3_uri,
    },
}
```

#### Outputs

```json
{
  "code_version_id": "code-20241112-abc123",
  "code_s3_uri": "s3://agentic-data-ingestion-code-dev/code-20241112-abc123.py",
  "required_params": ["source_path", "table_bucket_arn", "namespace", "table_name"],
  "optional_params": {
    "worker_type": "G.1X",
    "worker_count": 2,
    "timeout": 60
  },
  "quality_score": 0.95,
  "template_used": "glue_job_s3_tables.py.j2"
}
```

> **No `approval_required` here.** p34 D2 removed the key from this response along
> with `create_code.determine_approval_requirement`; the removal is pinned by
> `tests/contracts/api_responses/test_create_code_contract.py:149` (`"approval_required" not in resp`)
> and the key is on that file's forbidden list at `:96`. The code-approval decision is published by
> Step 6 at `$.test_code_result.approval_required` — see [Approval Gate 2](#approval-gate-2-code-review).

#### Template Variations

| Template | Source Read | Target Write | Extra Config |
|----------|-----------|-------------|-------------|
| `glue_job_s3_tables.py.j2` | `spark.read.json/csv/parquet(source_path)` | `df_transformed.writeTo(table).using("iceberg").createOrReplace()` | Iceberg catalog config |
| `glue_job_glue_catalog.py.j2` | Same | `glue_context.write_dynamic_frame.from_catalog(database, table)` | Glue catalog database/table args |
| `glue_job_jdbc.py.j2` | Same | `df_transformed.write.jdbc(url, table, properties)` | JDBC URL, driver, credentials from args |
| `glue_job_s3_parquet.py.j2` | Same | `df_transformed.write.parquet(output_path)` | Partition columns, compression |

> **The legacy `RDS_DATABASE` token renders through the `glue_job_jdbc.py.j2` row above**, so the
> table has four rows rather than five. For such a pipeline the dashboard auto-creates the Glue JDBC
> Connection at creation time and credentials are resolved from Secrets Manager via `secret_arn` in
> the target config, exactly as for a canonical `jdbc` target. See
> [Target type vocabulary](#target-type-vocabulary).

#### Parameter Contract System

**Key Innovation**: Step 5 extracts parameter requirements, Step 8 fulfills them.

**Benefits**:
- Ensures generated code and Glue job are aligned
- Eliminates runtime parameter errors
- Enables intelligent default value assignment
- Supports parameter validation before job creation

---

### Step 6: Code Testing (AI + Glue interactive session)

**Lambda Function**: `agentic_data_ingestion_test_code`

**Purpose**: Verify AI-generated code before it is deployed. Verification is **Glue-first**: a Glue
interactive session running the real transform is the fall-through executor, and the pandas proxy on
AgentCore Code Interpreter is reached only by deliberate opt-out.

#### Inputs

```json
{
  "run_id": "run-20241112-abc123",
  "pipeline_id": "pipeline-customer-data",
  "code_version_id": "code-20241112-abc123",
  "code_s3_uri": "s3://agentic-data-ingestion-code-dev/code-20241112-abc123.py",
  "source_data_uri": "s3://adi-processed-files-dev/pipeline-customer-data/data.json",
  "mapping_id": "map-20241112-xyz789"
}
```

#### Processing Logic

##### Executor selection — Glue is the fall-through, pandas is opt-out-only

`resolve_verification_executor` (`lambda/agentic_data_ingestion_test_code/lambda_function.py:573`)
returns `glue` for **everything except an exactly-spelled opt-out**: unset, empty, a typo, a casing
slip such as `"GLUE"`, and a whitespace near-miss such as `" glue"` all fall through to `glue`
(`:632`). The value is compared against an explicit string and is deliberately **not** normalised —
no `.strip()`, no `.lower()`, no membership check at the read site — because normalising would remove
the only signal that the deployed value is not the intended one.

The pandas proxy (`code_interpreter_runner.CodeInterpreterTestRunner`) is therefore reached by
exactly two routes:

1. An explicit, exactly-spelled `VERIFICATION_EXECUTOR=pandas` — the deliberate opt-out.
2. `resolve_effective_executor`'s downgrade (`:686`) when `glue` is the resolved request and
   `VERIFICATION_SESSION_ROLE_ARN` is unset. That one is not a policy choice: `role_arn` is
   keyword-only and required on `glue_session_runner.GlueSessionTestRunner`, so there is no session
   to create. It is logged at ERROR rather than WARNING, because a correctly spelled but
   unsatisfiable request is a different fact from a typo and one level cannot distinguish them.
   Raising instead was rejected, since that turns a missing environment variable into a failed run.

Two consequences a reader should design against. First, a **misspelled** `VERIFICATION_EXECUTOR`
creates a billable Glue interactive session rather than degrading to the cheap executor — that cost
was accepted explicitly. Second, `VERIFICATION_EXECUTOR` is set in both `compute-stack.ts` and
`agentcore-stack.ts`, but since the code fall-through is now `glue`, that two-sided setting no longer
prevents a downgrade; it only pins that the deployed value is *stated* rather than inherited.
Setting it in one stack alone was rejected, because that leaves an inert flag on the live path
while every test still passes, though the fall-through change has weakened that argument.

`VERIFICATION_FRAME_MODE` is a separate control and its two-sided setting **is** still load-bearing:
an unset value resolves to a synthetic frame generated from the declared source schema, which is a
real downgrade in what the run proves.

##### 1. Static Analysis (Strands Agent)

The Strands Agent performs static analysis of the generated code: checks syntax, validates imports, and reviews the transform logic against the field mappings.

##### 2. PySpark-to-Pandas Conversion (opt-out path only)

`PySpark2PandasConverter` parses the transform block with `ast.parse` and translates each statement's call chain to its pandas equivalent. `convert` is a classmethod — there is nothing to instantiate — and it returns a dict, not code:

```python
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

try:
    conversion = PySpark2PandasConverter.convert(transform_block)
except ConversionError as exc:
    # exc.construct, exc.line_number, exc.line — the transform is not converted
    raise

pandas_code = conversion["code"]          # str, with a pandas/numpy import header
report = conversion["conversion_report"]  # {input_lines, output_lines, constructs_converted}
```

Conversion is all-or-nothing. A construct with no single-frame pandas equivalent raises `ConversionError(construct, line, line_number)` and the transform never runs — nothing is commented out, dropped, or partially translated. What the converter accepts is a statement-shape rule rather than a list of patterns: any number of chained `withColumn` calls optionally closed by one `select`, or a single `withColumnRenamed` / `drop` / `filter` / `select` / `withColumn` owning the whole statement. The full construct vocabulary and the shapes that refuse are in [`pyspark_templates/README.md`](../pyspark_templates/README.md#what-the-pandas-proxy-converts).

What executes is a **proxy** for the Glue job, not the Glue job — pandas semantics over generated sample data. A transform that converts and executes cleanly has not been run against Spark.

##### 3. Code Interpreter Execution (opt-out path only)

Three invocations in a single AgentCore Code Interpreter session (~10-15 seconds total):

```python
# Start session
session = start_code_interpreter_session()

try:
    # Invocation 1: Generate test data from source schema
    invoke(session, generate_test_data_code)

    # Invocation 2: Execute converted pandas transform
    invoke(session, pandas_transform_code)

    # Invocation 3: Validate output against target schema
    results = invoke(session, validation_code)
finally:
    stop_code_interpreter_session(session)
```

##### 4. Validation Checks

1. **Execution Success**: Code runs without errors
2. **Output Schema**: Matches target schema field names and types
3. **Record Count**: Expected number of records produced
4. **Null Rates**: Non-nullable fields have no nulls
5. **Quality Score**: Overall quality metric (0-1)

##### 5. The Verification Frame (Glue Executor)

Alongside the pandas proxy above, the step has a second verification executor that runs the transform block **untranslated on real Spark**, in a Glue interactive session. It submits three statements: build the frame, run the transform, validate the output. What that run proves depends entirely on **which frame** the first statement built, and that is a per-run fact — not a property of the executor.

Frame selection is gated by the `VERIFICATION_FRAME_MODE` environment variable, read at the point the runner is constructed rather than at module import, so a warm container reflects a configuration change on its next run. It is set to `'sample'` in **both** `deploy-cdk/lib/compute-stack.ts` and `deploy-cdk/lib/agentcore-stack.ts`. Both copies are required and the obvious one is the inert one: `USE_AGENTCORE_RUNTIME: 'true'` means the runner executes in the AgentCore container, so **AgentCore's copy is the live path** and Compute's covers the inline break-glass route.

A Glue verification run therefore has one of four outcomes:

| Outcome | Frame | What the run proves |
|---------|-------|---------------------|
| **synthetic** | 100 rows generated from the declared source schema — one column per declared field, three type buckets, ~5% nulls, fixed seed | Real Spark semantics. Behaviour on the pipeline's own source data is **not** verified |
| **sample** | A bounded 100-row read of the pipeline's own source object, using the reader expression the generated Glue job uses for that format | Real Spark semantics **and** behaviour on those sampled rows. The rest of the object is not covered |
| **sample_unavailable** | A sample was requested and could not be had, so a synthetic frame was built instead | Same as **synthetic**. This is a fully verified run against a generated frame and carries **no** non-verification marker |
| **sample read failed** | The frame-build statement failed inside the session, or hit the 120s statement deadline | **Nothing.** The transform was never submitted |

**Only the sample outcome may claim verification against real source data.** The report's caveat, its `overall_status` parenthetical and its deployment-decision clause all branch on the frame, so a synthetic or fallback run says in as many words that real-source behaviour is unverified.

A sample is read **only if** all three of these hold: the configured mode is byte-equal to `"sample"`, the URI passes the `s3://` scheme check, and a reader resolves for the file extension (`csv`, `tsv`, `txt`, `json`, `jsonl`, `ndjson`, `parquet`, `pq`, `avro`, `orc`, optionally under a `.gz` / `.bz2` / `.snappy` / `.zst` suffix). So a `jdbc://` URI, an `s3a://` URI and an extension-less `s3://` prefix are all structurally unable to reach a read — they resolve to `sample_unavailable`, with the reason recorded as `source_uri_not_s3` or `source_format_unrecognised`. An unrecognised mode value is not normalised or rejected either; it falls through to a synthetic frame and is named in a WARNING.

`sample read failed` is the one outcome that reports a **harness non-verification**, with reason `source_read_failed`. Because the session stops at the first failed statement, the transform is never submitted, so the report carries no execution status, no record count and no execution-error block. It is not a transform failure and there is nothing in the generated PySpark to fix — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#glue-verification-reports-source_read_failed).

**A JDBC-sourced pipeline can never be sampled.** A verification session holds no database credentials — JDBC credentials reach Glue only by Secrets Manager ARN, a standing invariant, and the session role holds no Secrets Manager action, no VPC configuration and no Glue connection. Its `source_data_uri` is a `jdbc://` URI by construction, so every JDBC run reaches `sample_unavailable`, its report claims no verification against real source data, and it claims nothing about the watermark predicate on an incremental read. That is the intended behaviour, not a fault.

#### Outputs

```json
{
  "test_id": "test-20241112-xyz789",
  "test_status": "passed",
  "tests_passed": 5,
  "tests_failed": 0,
  "coverage_percentage": 100,
  "quality_score": 0.95,
  "approval_required": false
}
```

#### Error Scenarios

| Error | Cause | Resolution |
|-------|-------|------------|
| `CODE_EXECUTION_FAILED` | Syntax or runtime error in generated code | Regenerate code, review AI prompt |
| `SCHEMA_MISMATCH` | Output schema doesn't match target | Fix field mappings, regenerate code |
| `SESSION_TIMEOUT` | Code Interpreter session timeout | Retry with simpler test data |

A conversion refusal is **not** in this table, because it is not an error. When the pandas proxy cannot express a construct, the transform never runs, so nothing about it was proven either way. The step emits a check named `not_verified_by_proxy` (in place of the `code_interpreter_execution` check, which is not emitted at all) naming the construct, the line and one of two reasons — `permanently_inexpressible` (no single-frame pandas equivalent exists; nothing to fix in the generated PySpark) or `not_yet_expressible` (a defect in the converter). That check is partitioned out of the pass/fail derivation, so a refusal alone does not fail the step; a run in which nothing was verifiable still reports `failed`. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#pyspark-to-pandas-conversion-refuses).

Neither is a failed source read on the Glue executor. Those two reasons are about the pandas proxy's reach; the harness reports its own non-verifications on the same partitioned footing, and `source_read_failed` is one of them — the frame was to be a sample of the pipeline's own source object and that read did not succeed, so the transform never ran. Like a conversion refusal, it does not fail the step, and there is nothing in the generated PySpark to fix.

---

### Step 7: Infrastructure Provisioning (Conditional)

**Lambda Function**: `agentic_data_ingestion_create_infrastructure`

**Purpose**: Create AWS resources required for target (e.g., S3 Table Bucket), only if needed. Also registers S3 Tables in the Glue Data Catalog so that Glue Data Quality console integration works.

#### Conditional Execution Logic

```python
# Step Function definition
"InfrastructureProvisioning": {
    "Type": "Choice",
    "Choices": [
        {
            "Variable": "$.target_type",
            "StringEquals": "NEW_S3_TABLE_BUCKET",
            "Next": "CreateInfrastructure"
        }
    ],
    "Default": "SkipInfrastructure"
}
```

**Execution Matrix**:

| Target Type | Infrastructure Action | Step 7 Executed? |
|-------------|----------------------|------------------|
| `NEW_S3_TABLE_BUCKET` | Create S3 Table Bucket + Namespace + Table | ✅ Yes |
| `EXISTING_S3_TABLE_BUCKET` | Use existing bucket (validate access) | ❌ No |
| `GLUE_CATALOG` | Use existing catalog database | ❌ No |
| `JDBC` | Use existing database connection | ❌ No |
| `S3_PARQUET` | Write directly to S3 (no infrastructure) | ❌ No |

`should_create_infrastructure` provisions on one positive test, `target_type in
["NEW_S3_TABLE_BUCKET", "S3_TABLES"]`, and the ❌ rows are not all reached the same way: three of
them match a named skip branch, while `S3_PARQUET` matches none and is skipped by the catch-all. The
legacy `RDS_DATABASE` token needs no row here for the same reason, and the Glue Connection such a
pipeline uses is auto-created by the dashboard at pipeline creation time rather than by this step.
[Infrastructure Provisioning Logic](#infrastructure-provisioning-logic) has the detail; see also
[Target type vocabulary](#target-type-vocabulary).

#### Inputs (If Executed)

```json
{
  "target_type": "NEW_S3_TABLE_BUCKET",
  "target_config": {
    "bucket_name_prefix": "agentic-data-ingestion-tables",
    "namespace": "default",
    "table_name": "customers",
    "location_prefix": "customers"
  },
  "pipeline_id": "pipeline-customer-data"
}
```

#### Processing Logic (S3 Table Bucket Creation)

##### 1. Create or Get Table Bucket

```python
# Direct boto3 s3tables API (no AI agent or MCP)
s3tables = boto3.client('s3tables', region_name=AWS_REGION)

bucket_name = target_config['bucket_name']

try:
    response = s3tables.create_table_bucket(name=bucket_name)
    table_bucket_arn = response['arn']
except s3tables.exceptions.ConflictException:
    # Bucket already exists — get its ARN
    buckets = s3tables.list_table_buckets()
    table_bucket_arn = next(b['arn'] for b in buckets['tableBuckets'] if b['name'] == bucket_name)
```

##### 2. Create or Get Namespace

```python
namespace = target_config.get('namespace_name', 'default').replace('-', '_')

try:
    s3tables.create_namespace(tableBucketARN=table_bucket_arn, namespace=[namespace])
except s3tables.exceptions.ConflictException:
    pass  # Namespace already exists
```

##### 3. Create or Get Table

```python
table_name = target_config['table_name']

try:
    response = s3tables.create_table(
        tableBucketARN=table_bucket_arn,
        namespace=namespace,
        name=table_name,
        format='ICEBERG'
    )
    table_arn = response['tableARN']
except s3tables.exceptions.ConflictException:
    # Table already exists — get its ARN
    response = s3tables.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=table_name)
    table_arn = response['tableARN']
```

##### 4. Get Table Metadata and Update Pipeline

```python
details = s3tables.get_table(tableBucketARN=table_bucket_arn, namespace=namespace, name=table_name)

# Store infrastructure details in agentic_data_ingestion_pipelines table
pipelines_table.update_item(
    Key={'pipeline_id': pipeline_id},
    UpdateExpression='SET infrastructure_details = :details',
    ExpressionAttributeValues={
        ':details': {
            'table_bucket_arn': table_bucket_arn,
            'table_arn': table_arn,
            'namespace': namespace,
            'table_name': table_name,
            'warehouse_location': details.get('warehouseLocation', ''),
            'metadata_location': details.get('metadataLocation', ''),
            'created_at': now()
        }
    }
)
```

#### Outputs

```json
{
  "infrastructure_created": true,
  "table_bucket_arn": "arn:aws:s3tables:us-east-1:123456789012:bucket/agentic-data-ingestion-tables-...",
  "table_arn": "arn:aws:s3tables:us-east-1:123456789012:bucket/.../table/default/customers",
  "namespace": "default",
  "table_name": "customers"
}
```

**If Not Executed**:
```json
{
  "infrastructure_created": false,
  "skipped_reason": "target_type is EXISTING_S3_TABLE_BUCKET"
}
```

---

### Step 8: Glue Job Creation (AI + Codegen + Analytics MCP)

**Lambda Function**: `agentic_data_ingestion_create_glue_pipeline`

**Purpose**: Create AWS Glue ETL job with optimized configuration, aligned with generated code parameters.

#### Inputs

```json
{
  "code_version_id": "code-20241112-abc123",
  "code_s3_uri": "s3://agentic-data-ingestion-code-dev/code-20241112-abc123.py",
  "pipeline_id": "pipeline-customer-data",
  "infrastructure_details": {
    "table_bucket_arn": "arn:aws:s3tables:...",
    "namespace": "default",
    "table_name": "customers"
  }
}
```

#### Processing Logic

##### 1. Read Code Parameter Requirements

```python
# Query the agentic_data_ingestion_generated_code table
code_metadata = code_table.get_item(
    Key={'pk': f'CODE#{code_version_id}', 'sk': 'METADATA'}
)['Item']

required_params = code_metadata['required_params']
# ['source_path', 'table_bucket_arn', 'namespace', 'table_name']

optional_params = code_metadata['optional_params']
# {'worker_type': 'G.1X', 'worker_count': 2, 'timeout': 60}
```

##### 2. Build Parameter Values from Pipeline Config

```python
# Query agentic_data_ingestion_pipelines table
pipeline_config = pipelines_table.get_item(
    Key={'pipeline_id': pipeline_id}
)['Item']

# Map required parameters to values
default_arguments = {
    '--source_path': pipeline_config['source_config']['file_path'],
    '--table_bucket_arn': infrastructure_details['table_bucket_arn'],
    '--namespace': infrastructure_details['namespace'],
    '--table_name': infrastructure_details['table_name'],
    '--enable-glue-datacatalog': 'true',
    '--enable-continuous-cloudwatch-log': 'true',
    '--enable-metrics': 'true'
}
```

**Parameter Contract Validation**:
```python
# Ensure all required parameters have values
missing_params = [p for p in required_params if f'--{p}' not in default_arguments]
if missing_params:
    raise ValueError(f"Missing required parameters: {missing_params}")
```

##### 3. Get Worker Recommendations from the Codegen + Analytics MCP Agent

**There is no `recommend_glue_worker_config` tool, and the Data Processing MCP server is not in
this path.** The recommendation is a free-form model answer, not a typed tool call:
`get_worker_recommendation_with_agent` (`create_glue_pipeline/workers.py`) builds a prompt carrying
the data characteristics and the target config, hands it to a Strands agent whose MCP tools the
model may use at its own discretion, then recovers the answer by regex-matching a `worker_type`
object out of the response text. Nothing constrains the model to call a tool at all, which is why
`mcp_tools_used` is recorded on the result as *observed* rather than required.

The two MCP servers that agent can reach are fixed by which endpoint environment variables the
Lambda is given, and `create_glue_pipeline` reads exactly two:

```python
# lambda_function.py: focused endpoints, falling back to the monolithic one
codegen_ep = os.environ.get("AGENTCORE_CODEGEN_ENDPOINT", "")
analytics_ep = os.environ.get("AGENTCORE_ANALYTICS_ENDPOINT", "")
# Both set from compute-stack.ts's CreateGluePipeline environment block.
# No data_processing endpoint is set on any Lambda: that server is the legacy
# monolithic predecessor of codegen/infra/analytics and has no consumers
# (agentcore-stack.ts, the comment above `mcpServerEnv`). It is deployed,
# not wired.
```

Both endpoints go to **one** agent via `initialize_strands_agent_with_multi_mcp`, so the split is
per-agent and not per-concern. Do not read "codegen for job config, analytics for optimization" as
a routing rule. There is one tool surface and the model picks from it.

The parsed result is a `WorkerRecommendation` (`workers.py`):

```python
# worker_type, number_of_workers, timeout_minutes,
# estimated_cost_per_run, estimated_duration_minutes,
# rationale, confidence, mcp_tools_used
```

**Every failure on this path degrades rather than raises.** No MCP endpoint configured, no agent,
or a response the regex cannot parse all end at `get_default_worker_recommendation`, the rule-based
sizing keyed on data volume. `create_glue_job` then falls back again in its own right: a `None`
recommendation uses the baseline config, and absent that, `G.1X` / 2 workers / 60 min. So a Glue job
is always sized, and the presence of a job is not evidence that an agent sized it.

**The `FallbackUsed` metric dimension does not tell the two apart**, so do not reach for it. It is
computed as `worker_recommendation is None` (`job_builders.py`) and both paths pass a recommendation,
so it is `False` either way. The markers that do discriminate live on the recommendation itself: a
`confidence` of `0.7` and a rationale opening `Rule-based recommendation (MCP unavailable)` identify
the rule-based object, and `mcp_enhanced` is derived from `len(recommendation.mcp_tools_used) > 0`,
which makes it `False` whenever the model answered without calling a tool even though the agent path
ran normally.

> `MaxRetries` is **not** part of the recommendation, and the illustrative `create_job` call below
> predates the current behaviour. `create_glue_job` pins `"MaxRetries": 0` deliberately: retries
> belong to the self-healing agent, a Glue-native retry is invisible to the state machine because
> the first attempt's event already consumed the task token, and the retry holds the
> `MaxConcurrentRuns=1` slot against the heal rerun. See the comment on that key in
> `create_glue_pipeline/job_builders.py`.

##### 4. Create Glue Job

```python
glue = boto3.client('glue')

glue_job_name = f"agentic-data-ingestion-{pipeline_id}-{timestamp}"

response = glue.create_job(
    Name=glue_job_name,
    Role='Agentic Data IngestionExecutionRole',
    Command={
        'Name': 'glueetl',
        'ScriptLocation': code_s3_uri,
        'PythonVersion': '3'
    },
    DefaultArguments=default_arguments,
    GlueVersion='5.0',
    WorkerType=recommended_config['worker_type'],
    NumberOfWorkers=recommended_config['number_of_workers'],
    Timeout=recommended_config['timeout'],
    MaxRetries=recommended_config['max_retries'],
    Tags={
        'Agentic Data Ingestion': 'true',
        'PipelineId': pipeline_id,
        'CodeVersionId': code_version_id
    }
)
```

##### 5. Update Pipeline Metadata

```python
# Update agentic_data_ingestion_pipelines table with Glue job details
pipelines_table.update_item(
    Key={'pipeline_id': pipeline_id},
    UpdateExpression='SET glue_job_name = :job, worker_type = :worker, worker_count = :count, status = :status',
    ExpressionAttributeValues={
        ':job': glue_job_name,
        ':worker': recommended_config['worker_type'],
        ':count': recommended_config['number_of_workers'],
        ':status': 'ready'
    }
)
```

#### Outputs

```json
{
  "glue_job_name": "agentic-data-ingestion-pipeline-customer-data-20241112",
  "worker_type": "G.1X",
  "worker_count": 2,
  "timeout": 60,
  "estimated_cost_per_run": 0.44,
  "job_status": "created",
  "parameter_alignment": "validated"
}
```

#### Parameter Alignment Benefits

1. **No Runtime Errors**: Code and job parameters guaranteed to match
2. **Audit Trail**: Full parameter lineage from generation to job creation
3. **Validation**: Missing parameters detected before job creation
4. **Flexibility**: Supports parameter overrides via pipeline config

---

### Step 9: Create Quality Checks

**Lambda Function**: `agentic_data_ingestion_create_quality_checks`

**Purpose**: Generate data quality check configuration for the pipeline run. Prepares DQDL (Data Quality Definition Language) rules that will be evaluated inline during the Glue ETL job execution.

#### Inputs

```json
{
  "run_id": "run-20241112-abc123",
  "pipeline_id": "pipeline-customer-data"
}
```

#### Processing Logic

1. **Read Field Mappings**: Query the state table for the schema mapping step's output to get the approved field mappings
2. **Generate DQDL Rules**: Create rules based on field types and mapping metadata:
   - `IsComplete` rules for non-nullable target fields
   - `ColumnDataType` rules for type enforcement
   - `Uniqueness` rules for primary key columns
   - `ColumnValues` rules for known value ranges
3. **Store Configuration**: Write quality check config to the state table

#### Outputs

```json
{
  "quality_checks_configured": true,
  "rule_count": 8,
  "rules": [
    "IsComplete \"customer_id\"",
    "ColumnDataType \"signup_date\" = \"DATE\"",
    "Uniqueness \"customer_id\" > 0.95"
  ]
}
```

---

### Step 10: Run Pipeline (Inline Data Quality)

**Lambda Function**: `agentic_data_ingestion_run_pipeline`

**Purpose**: Execute the Glue ETL job and wait for completion via Step Functions `waitForTaskToken` callback. The Glue job includes inline data quality evaluation using the `EvaluateDataQuality` PySpark transform.

#### Inputs

```json
{
  "run_id": "run-20241112-abc123",
  "pipeline_id": "pipeline-customer-data",
  "taskToken": "AAAAKgAAAAIAAAAAAAAAAQ..."
}
```

#### Processing Logic

1. **Start Glue Job**: Invoke `glue.start_job_run()` with the pipeline's Glue job name and run-specific arguments
2. **Wait for Completion**: The Lambda returns immediately; the Glue job calls back to Step Functions via `send_task_success` or `send_task_failure` when it finishes
3. **Inline Data Quality**: During execution, the PySpark template runs `EvaluateDataQuality` with DQDL rules generated from the field mappings:
   - Rules are evaluated against the transformed DataFrame before writing to the target
   - Results (pass/fail per rule, overall score) are written to the `agentic_data_ingestion_state` DynamoDB table
   - Non-fatal DQ failures are logged as warnings — they don't block the ETL job
4. **Completion Callback**: The `job_completion` Lambda receives the Glue job status change event and sends the task token callback

#### Inline Data Quality (EvaluateDataQuality Transform)

The PySpark templates include an `EvaluateDataQuality` transform that runs DQDL rules inside the Glue job:

```python
from awsglue.transforms import EvaluateDataQuality

# DQDL ruleset generated from field mappings
ruleset = """
    Rules = [
        IsComplete "customer_id",
        ColumnDataType "signup_date" = "DATE",
        Uniqueness "customer_id" > 0.95
    ]
"""

# Evaluate quality on the transformed DynamicFrame
dq_results = EvaluateDataQuality.apply(
    frame=dynamic_frame,
    ruleset=ruleset,
    publishing_options={"dataQualityEvaluationContext": "agentic-data-ingestion-dq"}
)

# Write results to DynamoDB for dashboard display
for rule_result in dq_results:
    dynamodb.put_item(
        TableName=state_table,
        Item={
            "run_id": run_id,
            "step_name": "data_quality",
            "rule_name": rule_result["rule"],
            "status": rule_result["outcome"],  # "Passed" or "Failed"
            "score": rule_result.get("score", 1.0),
        }
    )
```

DQ results are displayed in the dashboard's "Data Quality" tab on the RunDetailPage with percentage-formatted scores and per-rule pass/fail indicators.

#### Outputs

```json
{
  "job_status": "SUCCEEDED",
  "job_run_id": "jr_abc123def456",
  "execution_time_seconds": 180,
  "data_quality_results": {
    "overall_status": "pass",
    "rules_passed": 7,
    "rules_failed": 1,
    "rules_total": 8,
    "details": [
      {"rule": "IsComplete \"customer_id\"", "outcome": "Passed", "score": 1.0},
      {"rule": "Uniqueness \"customer_id\" > 0.95", "outcome": "Passed", "score": 0.99}
    ]
  }
}
```

#### Timeout

The RunPipeline step uses `waitForTaskToken` with a timeout read from the pipeline's `pipeline_config.max_job_duration_seconds`. When the pipeline sets none, the `ApplyDefaultJobTimeout` state supplies 6 hours (`DEFAULT_MAX_JOB_DURATION_SECONDS` in `deploy-cdk/lib/orchestration-stack.ts`). If the Glue job doesn't complete within that window, the step fails with a `States.Timeout` error.

---

## Approval Gates

### Approval Gate 1: Schema Mapping

**Location**: After Step 4 (Schema Mapping)

**Trigger Conditions**:
1. Schema change detected: `schema_change_action` is `create_new_pipeline`, `update_existing_pipeline`, or `remap_required` — forces approval when the source schema has genuinely changed, regardless of mapping confidence
2. `confidence_score < 0.85` (`CONFIDENCE_AUTO_APPROVE_THRESHOLD` in `shared/config.py`)
3. User-configured policy: `require_approval_for_all_mappings: true`
4. Critical schema changes detected (e.g., data type downgrades)

**Pipeline-Level Configuration**: Each pipeline has an `approval_config` written at creation time:
```python
{
    'schema_mapping_enabled': True,
    # mirrors CONFIDENCE_AUTO_APPROVE_THRESHOLD in shared/config.py —
    # the enforced gate is the shared constant
    'schema_mapping_confidence_threshold': 0.85,
    'code_generation_enabled': True,
    'always_approve_schema_changes': True,  # force approval on schema change
}
```

**Approval Interface** (Dashboard):
- Display source and target schemas side-by-side
- Show AI-generated mappings with confidence scores
- Highlight unmapped fields
- Display AI reasoning for each mapping
- Sample data preview (3-5 records)

**User Actions**:
1. **Approve**: Continue to code generation
2. **Reject**: End pipeline execution, flag for manual review
3. **Modify**: Edit mappings (advanced feature), re-submit for AI regeneration

**Timeout**: 72 hours (`APPROVAL_WINDOW_HOURS` in `shared/config.py`, mirroring the gate's `APPROVAL_TIMEOUT_SECONDS`; `DEFAULT_TIMEOUT_HOURS` env or per-pipeline `timeout_hours` overrides)

---

### Approval Gate 2: Code Review

**Location**: **After Step 6 (Code Testing)**, not after Step 5. `TestResultChoice` routes a passed
test to `CheckCodeApproval` (`deploy-cdk/lib/orchestration-stack.ts:738`), whose permissive branch
goes to `CreateInfrastructure` and whose Default is `WaitForCodeApproval` (`:741`). Nothing between
`CreateCode` and `TestCode` consults an approval field, so Step 5 has no gate of its own.

**Producer and the single field the gate reads**: `CheckCodeApproval` reads
**`$.test_code_result.approval_required`** — `IsPresent`, then `IsBoolean`, then `BooleanEquals
false` (`orchestration-stack.ts:1078-1080`). `test_code` owns this decision.
`create_code.determine_approval_requirement` **no longer exists**: p34 D2 deleted it in a ratified
decision, because it wrote to `$.create_code_result.approval_required`,
a path no state in the ASL has ever read. Tombstone comments recording the deletion remain at
`lambda/agentic_data_ingestion_create_code/lambda_function.py:25` and `:586`. Do not reintroduce it,
and do not document `$.create_code_result.approval_required` as a gate input.

> The identically named `determine_approval_requirement` in
> `lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1578` is a **different** function
> serving Gate 1. It is live and correct; only the `create_code` one was deleted.

**Trigger Conditions** — the disjunction in `determine_merged_approval_requirement`
(`lambda/agentic_data_ingestion_test_code/lambda_function.py:1956`). Approval is required when
**any** of three holds:

1. `config_requires_approval` — the pipeline's `approval_config.code_generation_enabled`, resolved by
   `resolve_code_approval_setting` (`:1839`) with fail-closed defaults for absent or malformed values.
2. `quality_requires_approval` — the code's own test outcome. Code that failed its tests cannot reach
   `CreateInfrastructure` unreviewed, even on a pipeline whose routine approvals are switched off.
3. `regeneration_attempt > 0` — regenerated code is reviewer-flagged code.

Approval is skipped only when none of the three requires it: the setting explicitly `false`, the
quality gate clean, and the attempt count `0`.

**There is no `require_code_approval` key, no "new template used" condition, and no 0.90 quality
threshold.** All three appear nowhere in `lambda/`, `shared/` or `deploy-cdk/lib/`; they were
documentation artifacts. The configured key is `approval_config.code_generation_enabled`.

**Fail-closed**, a standing invariant: the permissive branch fires only on
`And[IsPresent, IsBoolean, BooleanEquals false]`. Absent, `null`, a string, a number or an object all
fall to the Default, which is a human. `"false"` is a truthy string and `0` a falsy int, which is why
`IsBoolean` is present and why the conjuncts must not be reordered or dropped.

**Approval Interface** (Dashboard):
- Syntax-highlighted PySpark code
- Side-by-side diff with template (if available)
- Parameter requirements checklist
- AI-generated code explanation
- Test results from Step 6, which has always run by the time this gate is reached

**User Actions**:
1. **Approve**: Continue to infrastructure provisioning
2. **Reject**: The approvals handler fails the task token, the state's Catch routes to
   `RecordFailure`, and the execution ends. Regeneration is not part of rejecting.
3. **Request Changes** — **not implemented.** The handler accepts only `"approved"` or `"rejected"`
   and raises `ValueError` on anything else
   (`lambda/agentic_data_ingestion_approval_handler/lambda_function.py:1134-1135`); no
   `request_changes` decision exists anywhere in `lambda/`, `shared/`, `dashboard-react/src/` or
   `deploy-cdk/lib/`. Feedback-driven AI revision reaches code by the regeneration loop
   (Gate 3 below), not by a decision on this gate.

**Timeout**: 72 hours (`APPROVAL_WINDOW_HOURS` in `shared/config.py`, mirroring the gate's `APPROVAL_TIMEOUT_SECONDS`; `DEFAULT_TIMEOUT_HOURS` env or per-pipeline `timeout_hours` overrides)

---

### Approval Gate 3: Forced Test Review

**Location**: After Step 6 (Code Testing), on the failure path. `ForceTestReviewApproval`
(`deploy-cdk/lib/orchestration-stack.ts:712`) is `TestResultChoice`'s `otherwise` branch, so it is
where a run lands when the test did not pass **and** the regeneration budget cannot absorb it.

**Trigger**: reached when `$.test_code_result.test_result.status` is anything other than `"passed"`,
including a malformed payload where the path is absent — the `IsPresent` guard makes "anything else"
route to a human rather than raise an uncatchable `States.Runtime` error. A failing run whose failures
are regeneration triggers and whose attempt budget is unspent goes back to `CreateCode` inside the
same execution instead; only an exhausted or non-regenerable failure reaches this gate.

**Approval type**: `test_failure` (`approval_handler/lambda_function.py:715`). It is the one approval
type with no state-row echo (`_APPROVAL_TYPES_WITHOUT_STATE_ECHO`, `:83`).

**User Actions**:
1. **Approve anyway**: resumes at `CheckCodeApproval` — the same next state a passed test reaches
   (`orchestration-stack.ts:1051`), so approving here does not bypass Gate 2.
2. **Reject**: task token failed, Catch routes to `RecordFailure`.

**Timeout**: 72 hours, the same `APPROVAL_TIMEOUT_SECONDS` applied to every `waitForTaskToken`
approval state.

---

### The three gates, in one place

There are exactly **three** `waitForTaskToken` approval gates in the state machine:

| Gate | State | Location | Field / condition read |
|------|-------|----------|------------------------|
| 1. Schema mapping | `WaitForSchemaMappingApproval` (`:494`) | after Step 4 | `$.schema_mapping_result.approval_required` (`:949-951`) |
| 2. Code | `WaitForCodeApproval` (`:741`) | after **Step 6** | `$.test_code_result.approval_required` (`:1078-1080`) |
| 3. Forced test review | `ForceTestReviewApproval` (`:712`) | after Step 6, failure path | `$.test_code_result.test_result.status` |

Line references are to `deploy-cdk/lib/orchestration-stack.ts`. Gates 1 and 2 are both governance
Choice states and both fail closed, a standing invariant: only a field that is present, boolean and
`false` takes the permissive branch, and every other shape falls to the Default, which is a human.

---

## Parameter Contract System

### Overview

The **Parameter Contract System** ensures that AI-generated code and AWS Glue job configuration are perfectly aligned, eliminating runtime parameter mismatches. The Jinja2 template is the single source of truth for parameter names — everything else adapts to match.

### End-to-End Flow

```
Step 5: Code Generation (create_code Lambda)
├─ Select Jinja2 template by target_type
├─ AI generates transform block (5-15 lines)
├─ Render template → final PySpark code
├─ parameter_extractor parses getResolvedOptions from rendered code
│  ├─ required_parameters: ['source_path', 'table_bucket_arn', ...]
│  └─ optional_parameters: ['partition_columns', ...]
├─ Store code + required_parameters in agentic_data_ingestion_generated_code table
│
Step 8: Glue Job Creation (create_glue_pipeline Lambda)
├─ Read required_parameters from agentic_data_ingestion_generated_code table
├─ Resolve source_path from state table (file_validation step)
├─ Resolve warehouse_location from infra step or S3 Tables API (S3 Tables only)
├─ Build target-specific DefaultArguments via build_*_arguments()
│  └─ JDBC target: --secret_arn is a Secrets Manager reference (resolve_jdbc_target_secret)
├─ validate_required_parameters() checks all params present and non-empty
├─ Create Glue job with aligned DefaultArguments
└─ PySpark getResolvedOptions reads args at runtime — names match exactly
```

### Parameter Naming Convention

All parameters use **lowercase_snake_case** in both templates and `DefaultArguments`. This matches `getResolvedOptions` behavior (strips `--` prefix, uses the rest as-is). `JOB_NAME` is excluded from validation — Glue provides it automatically.

### Per-Template Parameter Contract

| Template | Required Parameters |
|----------|-------------------|
| `glue_job_s3_tables.py.j2` | `source_path`, `table_bucket_arn`, `namespace`, `table_name` |
| `glue_job_glue_catalog.py.j2` | `source_path`, `database_name`, `table_name` |
| `glue_job_jdbc.py.j2` | `source_path`, `jdbc_url`, `jdbc_table`, `jdbc_driver`, `secret_arn` |
| `glue_job_s3_parquet.py.j2` | `source_path`, `output_path` |

> **The legacy `RDS_DATABASE` token uses the `glue_job_jdbc.py.j2` contract above**, so it adds no
> fifth row. No JDBC target carries a plaintext credential argument: `secret_arn` is a Secrets
> Manager reference, which `build_jdbc_arguments` takes from `target_config.secret_arn` or else from
> the connection's `SECRET_ID` (`resolve_jdbc_target_secret`), and the generated write fetches the
> credential with it at run time. That is a standing invariant, and the reason is concrete: job
> arguments are visible in the Glue console, in CloudTrail and in job-run history, and a plaintext
> value there leaked a target database credential once already. The earlier
> `resolve_parameter_value` / `_resolve_rds_secret_field` pair, which fetched the plaintext inside
> the Lambda, had no production caller and has been deleted. See
> [Target type vocabulary](#target-type-vocabulary).

### source_path Resolution

`source_path` is common to all templates — it's the S3 URI of the processed source file from the file_validation step.

**Resolution flow** (`get_source_path(run_id)`):

```
State table query: {run_id, step_name: 'file_validation'}
  │
  ├─ Primary: item['json_s3_uri'] (top-level field)
  ├─ Fallback: item['output_data']['s3_uris']['json_file']
  └─ Error: raise ValueError if not found
```

**Example value**: `s3://adi-processed-files-dev/{pipeline_id}/customers.json`

The `file_validation` Lambda writes `json_s3_uri` as a top-level field on the state table item when it converts the uploaded file to JSON format. The `create_glue_pipeline` Lambda reads it back via `get_source_path()` and passes it as `--source_path` in `DefaultArguments`.

### warehouse_location and Iceberg Catalog Config (S3 Tables Only)

The S3 Tables Iceberg catalog (`S3TablesCatalog`) requires the **table bucket ARN** for its `warehouse` config — not an S3 URI path. The template uses `args['table_bucket_arn']` for this:

```python
spark.conf.set("spark.sql.catalog.s3tablesbucket.warehouse", args['table_bucket_arn'])
```

`warehouse_location` (the S3 URI) is still resolved by `get_warehouse_location()` and stored in `DefaultArguments` for operational metadata, but it's no longer in `getResolvedOptions` and not used by the generated PySpark code.

**Resolution flow** (`get_warehouse_location(run_id, target_config)`):

```
1. State table query: {run_id, step_name: 'CreateInfrastructure'}
   └─ output_data.s3_table_bucket.warehouse_location
   └─ OR output_data.infrastructure_status.s3_table_bucket.warehouse_location

2. If not found → S3 Tables API: get_table(bucket_arn, namespace, table_name)
   └─ response['warehouseLocation']

3. If not found → target_config.get('warehouse_location', '')

4. If still empty → raise ValueError
```

**When each path is used**:
- **Path 1** (infra step): New S3 Table Buckets created by the `CreateInfrastructure` step — the warehouse location is stored in the step's `output_data`
- **Path 2** (S3 Tables API): Existing S3 Table Buckets where the infrastructure step was skipped — queries the S3 Tables API directly
- **Path 3** (target_config): Manual override via pipeline configuration

**Important**: The `CreateInfrastructure` step must run **before** `CreateGluePipeline` in the Step Functions state machine so that the infrastructure output is available when the Glue job is created.

### Example: S3 Tables DefaultArguments

```python
{
    '--source_path': 's3://adi-processed-files-dev/pipe-abc/data.json',
    '--table_bucket_arn': 'arn:aws:s3tables:us-west-2:123456789012:bucket/my-bucket',
    '--namespace': 'default',
    '--table_name': 'customers',
}
```

### Validation

Before creating the Glue job, `validate_required_parameters()` checks:
1. Every parameter from `required_parameters` has a corresponding `--{param}` key in `DefaultArguments`
2. No value is empty or None
3. Raises `ValueError` with a clear message listing missing/empty params

### Benefits

1. **Zero Runtime Errors**: Parameters guaranteed to match code expectations
2. **Intelligent Defaults**: AI-recommended values for optional parameters
3. **Validation**: Pre-deployment checks prevent `GlueArgumentError`
4. **Audit Trail**: Full parameter lineage for troubleshooting
5. **Flexibility**: Supports parameter overrides at job run time
6. **Single Source of Truth**: Template defines params, everything else adapts

---

## Conditional Logic

### Infrastructure Provisioning Logic

**Decision Tree**:
```
if target_type == "NEW_S3_TABLE_BUCKET":
    Execute Step 7: Create S3 Table Bucket
    ├─ Create bucket
    ├─ Create namespace
    ├─ Create table
    └─ Store infrastructure_details
elif target_type == "EXISTING_S3_TABLE_BUCKET":
    Skip Step 7
    └─ Validate access to existing bucket
elif target_type == "GLUE_CATALOG":
    Skip Step 7
    └─ Validate catalog database exists
elif target_type == "JDBC":
    Skip Step 7
    └─ Validate JDBC connection
elif target_type == "S3_PARQUET":
    Skip Step 7
    └─ Validate S3 write permissions
else:
    Raise error: Invalid target_type
```

**The `else` arm above is the intent, not the behaviour, and the difference matters.**
`should_create_infrastructure` (`create_infrastructure/lambda_function.py`) provisions on the single
positive test `target_type in ["NEW_S3_TABLE_BUCKET", "S3_TABLES"]`, carries named skip branches for
`EXISTING_S3_TABLE_BUCKET`, `GLUE_CATALOG` and `JDBC`, and ends in a catch-all that logs
`Unknown target type: ..., skipping infrastructure creation` at WARNING and skips. Nothing on this
path raises on an unrecognised target type.

Two consequences follow. `S3_PARQUET` has **no branch of its own** despite its row in the matrix
above, so it reaches that catch-all and is skipped as "unknown" rather than as a recognised
no-infrastructure target. The legacy `RDS_DATABASE` token lands in the same place, which is why it
needs no arm here: it is skipped for want of a match, not because anything names it, and the Glue
Connection such a pipeline uses was auto-created by the dashboard at pipeline creation time. Reading
that WARNING as a defect is the mistake to avoid. It is the normal path for both tokens. See
[Target type vocabulary](#target-type-vocabulary).

### Approval Logic

**Schema Mapping Approval**:
```
if confidence_score < CONFIDENCE_AUTO_APPROVE_THRESHOLD:  # 0.85 — shared/config.py
    Require approval
elif require_approval_for_all_mappings == true:
    Require approval
elif critical_changes_detected:
    Require approval (e.g., type downgrade, removed fields)
else:
    Proceed automatically
```

**Code Review Approval** — decided in `test_code`, not `create_code`, and evaluated **after Step 6**.
A disjunction of three independent reasons (`determine_merged_approval_requirement`,
`lambda/agentic_data_ingestion_test_code/lambda_function.py:1956`):

```
# approval_config.code_generation_enabled, via resolve_code_approval_setting
# (fail-closed on absent or malformed)
if config_requires_approval:
    Require approval
elif quality_requires_approval:        # the code's own test outcome
    Require approval
elif regeneration_attempt > 0:         # regenerated code is reviewer-flagged code
    Require approval
else:
    Proceed automatically              # only when all three are false
```

The result is written to `$.test_code_result.approval_required`, which is the only field
`CheckCodeApproval` reads. There is no `require_code_approval` key, no `new_template_used` condition
and no 0.90 quality threshold — none of the three exists in the source.

---

## Error Handling

### Retry Logic

**Step Functions Retry Policy** — there is **one** shared policy, not a per-step table. `stepRetry`
(`deploy-cdk/lib/orchestration-stack.ts:247-252`) is declared once and applied unchanged to every task
state that retries at all:

```typescript
const stepRetry: sfn.RetryProps = {
  errors: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'],
  interval: cdk.Duration.seconds(5),
  maxAttempts: 3,
  backoffRate: 2.0,
};
```

Three points a reader designs against:

- **`TransientStepFailure` is the first entry and it is the important one.** It is the error class the
  Step Lambdas raise through the `shared/step_errors.py` contract, so retry eligibility is a decision
  the handler makes rather than a property of the AWS service error. A `StepFailure` — the default
  `error_type` is `"permanent"` — does **not** match and is not retried.
- **The interval is 5 seconds, not 2**, with `backoffRate: 2.0` and `maxAttempts: 3`.
- **`States.Timeout` is not in the list.** There is no separate `States.Timeout` retry entry; a
  timeout is not retried.

Applied at `:331` (`fileValidation`), `:368` (`schemaExtract`), `:383` (`schemaCompare`), `:401`
(`schemaMapping`) and `:806` (`createQualityChecks`). Every attempt count and interval is the same
value from the same constant — a per-step variation table would be inventing differences the stack
does not have.

### Error Catch and Cleanup

**Step Functions Catch** — the target state is named **`RecordFailure`**. There is no state named
`ErrorHandler`; that name appears nowhere in `deploy-cdk/lib/orchestration-stack.ts`.

```json
{
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "Next": "RecordFailure",
      "ResultPath": "$.error"
    }
  ]
}
```

Declared once as `stepCatch` (`orchestration-stack.ts:259-262`) for the `LambdaInvoke` states and
spelled inline as `CONTAINER_STEP_CATCH` (`:546`) for the `waitForTaskToken` ones. Both forms are
identical in effect.

**Why the whole state is passed rather than a plucked payload**: `resultPath: '$.error'` with no
JSONPath payload selection is what guarantees the recorded error is the **originating** step's error
rather than a downstream `States.Runtime` JSONPath failure.

**`RecordFailure` state** (`:268`) — invokes the record-failure Lambda (`:134`), which:
1. Parses the Catch payload and persists error details to `agentic_data_ingestion_state`
2. Publishes the failure event for the dashboard

`RecordFailure` itself catches `States.ALL` and routes to `ReleaseLockFailure` (`:1115`), so a failure
inside the recorder still releases the pipeline lock.

---

## State Tracking

### DynamoDB State Records

Each step writes a state record to `agentic_data_ingestion_state` table:

**Record Structure**:
```json
{
  "pipeline_id": "run-20241112-abc123.4",
  "step_name": "schema_mapping",
  "run_id": "run-20241112-abc123",
  "original_pipeline_id": "pipeline-customer-data",
  "status": "success",
  "timestamp": "2024-11-12T10:30:45.123Z",
  "step_sequence": 4,
  "output_data": {
    "artifact_type": "schema_mapping",
    "primary_id": "map-20241112-xyz789",
    "status": "success",
    "summary": {
      "mapped_fields": 10,
      "unmapped_fields": 2,
      "avg_confidence": 0.88,
      "approval_required": false
    },
    "s3_uris": {}
  },
  "ai_model_used": "claude-sonnet-4.5",
  "execution_time_ms": 12345
}
```

All step Lambdas write `output_data` using the standardized schema defined in `shared/artifact_schema.py`. Required keys: `artifact_type`, `primary_id`, `status`, `summary`, `s3_uris`. The `approval_data` JSON string pattern has been removed — approval fields are stored directly in `output_data`.

### State Query Patterns

**Get All Steps for a Run**:
```python
response = state_table.query(
    IndexName='RunIdIndex',
    KeyConditionExpression=Key('run_id').eq('run-20241112-abc123')
)
```

**Get Latest Status**:
```python
# Query by run_id, sort by step_sequence DESC, limit 1
response = state_table.query(
    IndexName='RunIdIndex',
    KeyConditionExpression=Key('run_id').eq('run-20241112-abc123'),
    ScanIndexForward=False,
    Limit=1
)
latest_step = response['Items'][0]
```

### EventBridge Events

Each state change triggers an EventBridge event:

**Event Pattern**:
```json
{
  "source": "agentic_data_ingestion.pipeline",
  "detail-type": "Pipeline State Change",
  "detail": {
    "pipeline_id": "pipeline-customer-data",
    "run_id": "run-20241112-abc123",
    "step_name": "schema_mapping",
    "status": "success",
    "timestamp": "2024-11-12T10:30:45.123Z"
  }
}
```

**Event Types**:
1. `pipeline.started`
2. `pipeline.step.completed`
3. `pipeline.step.failed`
4. `pipeline.approval.required`
5. `pipeline.completed`
6. `pipeline.failed`

**Dashboard Integration**: Dashboard subscribes to events for real-time UI updates

---

## Execution Examples

### Example 1: Fast Path (No Approvals, Existing Infrastructure)

**Configuration**:
- Target: EXISTING_S3_TABLE_BUCKET
- Confidence: High (0.92)
- Approvals: Disabled

**Timeline**:
```
00:00:00 - Pipeline started
00:00:02 - Step 1 (File Validation): SUCCESS
00:00:10 - Step 2 (Schema Extraction): SUCCESS
00:00:12 - Step 3 (Schema Comparison): SUCCESS
00:00:18 - Step 4 (Schema Mapping): SUCCESS (memory-guided, confidence 0.92)
00:01:05 - Step 5 (Code Generation): SUCCESS
00:06:30 - Step 6 (Code Testing): SUCCESS
00:06:30 - Step 7 (Infrastructure): SKIPPED (existing infrastructure)
00:06:45 - Step 8 (Glue Job Creation): SUCCESS
00:06:50 - Step 9 (Quality Checks): SUCCESS
00:09:50 - Step 10 (Run Pipeline): SUCCESS (Glue job + inline DQ)
00:09:50 - Pipeline completed
```

**Total Duration**: ~10 minutes

---

### Example 2: With Schema Approval (Manual Review)

**Configuration**:
- Target: NEW_S3_TABLE_BUCKET
- Confidence: Low (0.78)
- Approvals: Schema mapping required

**Timeline**:
```
00:00:00 - Pipeline started
00:00:02 - Step 1 (File Validation): SUCCESS
00:00:10 - Step 2 (Schema Extraction): SUCCESS
00:00:12 - Step 3 (Schema Comparison): SUCCESS
00:00:23 - Step 4 (Schema Mapping): WAITING_APPROVAL (confidence 0.78)
          ├─ Approval created
          ├─ Notification sent to dashboard
          └─ Step Function pauses

[User reviews mapping in dashboard for 2 hours]

02:00:00 - User approves mapping
02:00:01 - Step 4: APPROVED, pipeline resumes
02:01:15 - Step 5 (Code Generation): SUCCESS
02:07:45 - Step 6 (Code Testing): SUCCESS
02:08:30 - Step 7 (Infrastructure): SUCCESS (created S3 Table Bucket, ~5s)
02:08:55 - Step 8 (Glue Job Creation): SUCCESS
02:09:00 - Step 9 (Quality Checks): SUCCESS
02:12:00 - Step 10 (Run Pipeline): SUCCESS (Glue job + inline DQ)
02:12:00 - Pipeline completed
```

**Total Duration**: ~2 hours 12 minutes (mostly waiting for approval)

---

### Example 3: Failure in Testing

**Configuration**:
- Target: GLUE_CATALOG
- Test failures detected

**Timeline**:
```
00:00:00 - Pipeline started
00:00:02 - Step 1 (File Validation): SUCCESS
00:00:10 - Step 2 (Schema Extraction): SUCCESS
00:00:12 - Step 3 (Schema Comparison): SUCCESS
00:00:20 - Step 4 (Schema Mapping): SUCCESS
00:01:05 - Step 5 (Code Generation): SUCCESS
00:06:45 - Step 6 (Code Testing): FAILED
          ├─ Error: Output schema mismatch
          ├─ Expected field "cust_id" (integer)
          └─ Found field "customer_id" (string)

00:06:45 - TestResultChoice: status != "passed"
          ├─ Regeneration budget spent (or the failures are not regeneration
          │  triggers), so the run does NOT loop back to CreateCode
          └─ Routed to ForceTestReviewApproval — Approval Gate 3

00:06:46 - Step 6: WAITING_APPROVAL (approval_type = "test_failure")
          ├─ Approval created
          ├─ Notification sent to dashboard
          └─ Step Function pauses (72h window)

[A human decides]
```

**A failing test does not end the execution on its own.** The run stalls at a human gate; what happens
next is the reviewer's decision:

- **Approve anyway** → resumes at `CheckCodeApproval`, the same next state a passed test reaches, so
  Gate 2 still applies.
- **Reject** → the approvals handler fails the task token, the state's Catch routes to `RecordFailure`
  (**not** a state called `ErrorHandler` — no such state exists), which persists the error, publishes
  `pipeline.failed`, and releases the pipeline lock through `ReleaseLockFailure`.

**Total Duration**: ~7 minutes to the gate, then bounded by the reviewer (72h window).

**Resolution**:
1. Review test results in dashboard
2. Identify mapping error (field name mismatch)
3. Reject the forced review, correct the mappings, and re-run — or approve anyway if the failure is
   understood and acceptable

---

## Summary

The Agentic Data Ingestion 10-step pipeline workflow provides:

1. **Automated Validation**: Ensures data quality before processing
2. **AI-Powered Intelligence**: Semantic field mapping, code generation, optimization
3. **Human-in-the-Loop**: three approval gates — schema mapping and code are configurable and fail
   closed; forced test review is not optional, a failing run always reaches a human
4. **Robust Testing**: Glue-first verification of generated code before deployment, with the pandas
   proxy reachable only by explicit opt-out
5. **Conditional Logic**: Adapts to different target types and configurations
6. **Parameter Contract**: Eliminates runtime parameter errors
7. **State Tracking**: Full audit trail and real-time monitoring
8. **Error Handling**: Automatic retries, cleanup, and notifications
9. **Inline Data Quality**: DQDL rules evaluated inside the Glue ETL job with results written to DynamoDB
10. **End-to-End Execution**: Pipeline runs the Glue job and reports results in a single workflow

The workflow balances automation with control, enabling fast execution while maintaining quality and compliance.

---

**Currency**: this document carries no "last updated" date, deliberately. A date is a claim about the
whole file that nobody re-verifies when one section is corrected, and the stale `2026-04-03` footer it
replaces had outlived four structural changes to the state machine. What to check instead: every gate,
retry and step-framing claim above cites `deploy-cdk/lib/orchestration-stack.ts` or a handler by
`file:line`. Re-read the citation. If a cited line has moved, the claim is the thing to re-verify —
not a date in the footer.

**Document revision**: 4.0 (CreateQualityChecks and RunPipeline steps, inline Glue Data Quality, Glue Catalog
registration, Glue-first code verification with the pandas proxy as opt-out). This is the revision of this
document, not the project's release version, which is in [CHANGELOG.md](../CHANGELOG.md).
