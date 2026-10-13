# JDBC Data Source

## Overview

Agentic Data Ingestion supports two ways to feed source data into a pipeline:

- **S3 upload** — files dropped under `s3://adi-source-files-*/{pipeline_id}/` trigger a pipeline via EventBridge → SQS → `Pipeline_Trigger_Lambda`. This is the default and applies to every pipeline created before P17.
- **JDBC database** — the pipeline reads directly from a relational database on a schedule (or on demand via Run Now), using AWS Glue's JDBC connector. No file ever lands in S3 on the source side.

Use **JDBC** when:

- The source of record is a managed RDS / Aurora cluster, an EC2-hosted database, or another JDBC-reachable engine.
- You need incremental loads keyed off a watermark column (timestamp or monotonic numeric).
- You want a scheduled extract (`cron(...)` / `rate(...)`) rather than an upload-driven trigger.

Use **S3 upload** when:

- The source emits files (CSV, JSON, Parquet) — there is no database in the loop.
- Upstream systems already drop files into a landing bucket.
- You want event-driven, per-file processing.

The rest of the pipeline — schema mapping, code generation, testing, infrastructure provisioning, Glue job execution, data quality — is identical across both source types. Only `file_validation` and `schema_extract` branch on `source_type`; everything from `schema_compare` onwards is source-agnostic.

---

## Table of Contents

1. [Supported engines](#supported-engines)
2. [Architecture](#architecture)
3. [Connection modes](#connection-modes)
4. [Discovery flow](#discovery-flow)
5. [Watermark semantics](#watermark-semantics)
6. [Networking model](#networking-model)
7. [Triggering](#triggering)
8. [Code generation](#code-generation)
9. [Target credential contract](#target-credential-contract)
10. [IAM](#iam)
11. [Source config shape](#source-config-shape)
12. [Target schema modes](#target-schema-modes)
13. [Known limitations](#known-limitations)
14. [Manual Connection Mode (P20)](#manual-connection-mode-p20)
15. [Related docs](#related-docs)

---

## Supported engines

The supported engine set is the keys of `JDBC_DRIVER_MAP` in `shared/jdbc_helpers.py`. The same set is mirrored byte-for-byte in the frontend `JDBC_DRIVER_MAP` in `dashboard-react/src/pages/wizard/WizardStepTarget.tsx`.

| Engine key            | JDBC driver class                                | Default port | URL pattern                                                                            |
|-----------------------|--------------------------------------------------|--------------|----------------------------------------------------------------------------------------|
| `postgres`            | `org.postgresql.Driver`                          | 5432         | `jdbc:postgresql://{host}:{port}/{db}?ssl=true&sslmode=require`                        |
| `aurora-postgresql`   | `org.postgresql.Driver`                          | 5432         | same as `postgres`                                                                     |
| `mysql`               | `com.mysql.cj.jdbc.Driver`                       | 3306         | `jdbc:mysql://{host}:{port}/{db}?useSSL=true&requireSSL=true`                          |
| `aurora-mysql`        | `com.mysql.cj.jdbc.Driver`                       | 3306         | same as `mysql`                                                                        |
| `mariadb`             | `org.mariadb.jdbc.Driver`                        | 3306         | `jdbc:mariadb://{host}:{port}/{db}?useSSL=true&requireSSL=true`                        |
| `oracle-ee`           | `oracle.jdbc.OracleDriver`                       | 1521         | `jdbc:oracle:thin:@{host}:{port}/{service_name}`                                       |
| `oracle-se2`          | `oracle.jdbc.OracleDriver`                       | 1521         | same as `oracle-ee`                                                                    |
| `sqlserver-ee`        | `com.microsoft.sqlserver.jdbc.SQLServerDriver`   | 1433         | `jdbc:sqlserver://{host}:{port};databaseName={db};encrypt=true`                        |
| `sqlserver-se`        | same                                             | 1433         | same as `sqlserver-ee`                                                                 |
| `sqlserver-ex`        | same                                             | 1433         | same as `sqlserver-ee`                                                                 |
| `sqlserver-web`       | same                                             | 1433         | same as `sqlserver-ee`                                                                 |

All drivers in the table above ship in the AWS Glue 5.0 runtime — no custom JAR upload is required. Engines outside this list (DB2, Snowflake JDBC, Teradata, etc.) are out of scope for V1; see [Known limitations](#known-limitations).

---

## Architecture

### High-level flow

```
┌─────────────────────────────────────────────────────────────────┐
│                          Wizard (React)                          │
│  Name → Source → Target → Schema → Model → Review               │
│  Source step:                                                    │
│    - choose s3_upload or jdbc                                    │
│    - if jdbc: pick connection mode, schema, table, read mode,    │
│      watermark column (incremental), schedule                    │
│    - "Detect Schema" → wizard-time discovery crawler             │
└─────────────────────────────────────────────────────────────────┘
                              │ POST /api/pipelines
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                 Dashboard API — handlers/pipelines.py            │
│  create_pipeline:                                                │
│    1. Persist agentic_data_ingestion_pipelines item with source_type +        │
│       source_config                                              │
│    2. (select_rds only) create Source_Connection                 │
│       agentic-data-ingestion-{pipeline_id}-source via                         │
│       create_glue_jdbc_connection() — sets                       │
│       PhysicalConnectionRequirements                             │
│    3. Create per-pipeline runtime crawler                        │
│       agentic-data-ingestion-{pipeline_id}-source-discover                    │
│    4. Create / update schedule rule                              │
│       agentic-data-ingestion-schedule-{pipeline_id}                           │
└─────────────────────────────────────────────────────────────────┘
                              │ EventBridge schedule fires
                              │   OR  POST /api/pipelines/{id}/run
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│            Pipeline_Trigger_Lambda (agentic_data_ingestion_            │
│            pipeline_trigger)                                     │
│  Branches on event.trigger ∈ {scheduled, manual, s3_upload}     │
│  AND event.source_type ∈ {S3_UPLOAD, JDBC}.                     │
│  For source_type=JDBC: starts SFN with                           │
│  {pipeline_id, source_type: "JDBC", trigger}.                   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Step Functions: agentic-data-ingestion-pipeline           │
│                                                                  │
│  FileValidation  ──► branches on event.source_type:              │
│                     - S3_UPLOAD: existing S3 HEAD path           │
│                     - JDBC: writes synthetic file_validation     │
│                       row with original_s3_uri =                 │
│                       jdbc://{engine}/{schema}/{table}          │
│        │                                                         │
│        ▼                                                         │
│  SchemaExtract   ──► branches on source_format:                  │
│                     - "jdbc": StartCrawler on per-pipeline       │
│                       runtime crawler, GetTables on prefix       │
│                       agentic_data_ingestion_{pipeline_id}_*                  │
│        │                                                         │
│        ▼                                                         │
│  SchemaCompare → SchemaMapping → CreateCode → TestCode           │
│  → CreateInfrastructure → CreateGluePipeline                     │
│  → CreateQualityChecks → RunPipeline                             │
│                                                                  │
│  All states from SchemaExtract onwards are source-agnostic —     │
│  they consume (run_id, step_name) rows from agentic_data_ingestion_state.    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Generated Glue ETL Job                        │
│  Template: glue_job_jdbc_source.py.j2                           │
│  Reads: spark.read.format("jdbc")                               │
│         .option(dbtable | query) ← incremental builds query     │
│         from build_watermark_predicate(engine, ...)             │
│  Writes via one of four shared partials:                        │
│    s3_tables / glue_catalog / jdbc / s3_parquet                 │
│  Glue routes the JDBC connection through the customer's         │
│  RDS VPC via the Source_Connection's                            │
│  PhysicalConnectionRequirements.                                │
└─────────────────────────────────────────────────────────────────┘
```

### Source-agnostic boundary

The key insight that lets P17 add a new source type without rewriting the state machine is that **all 16 Step Functions states from `SchemaExtract` onwards consume `(run_id, step_name)` rows from `agentic_data_ingestion_state`**. The `extracted_schema` written by `schema_extract` is the same JSON Schema shape regardless of how it was extracted (S3 file inspection vs. Glue crawler `StorageDescriptor`). Downstream Lambdas (`schema_compare`, `schema_mapping`, `create_code`, `test_code`, `create_infrastructure`, `create_glue_pipeline`, `create_quality_checks`, `run_pipeline`) require zero code changes.

Only `file_validation` and `schema_extract` branch on `source_type` / `source_format`.

---

## Connection modes

The wizard exposes two connection modes for JDBC sources (and the consolidated JDBC target form). Both modes share the `JdbcConnectionSelector` React component.

### `select_rds`

The user picks an RDS instance or Aurora cluster from the merged list of `/api/resources/rds/instances` and `/api/resources/rds/clusters`. Agentic Data Ingestion then:

- Auto-populates JDBC URL, driver class, port, and engine via `JDBC_DRIVER_MAP[engine]`.
- At pipeline-creation time, calls `create_glue_jdbc_connection()` from `shared/jdbc_helpers.py` to provision a Glue Connection named `agentic-data-ingestion-{pipeline_id}-source` using `PhysicalConnectionRequirements` derived from the instance's `DBSubnetGroup.Subnets[0]` and `VpcSecurityGroups`.
- Owns the connection's lifecycle: it is deleted alongside the pipeline by `delete_pipeline`. A JDBC target in `select_rds` or manual mode gets `agentic-data-ingestion-{pipeline_id}-rds` the same way, and `delete_pipeline` deletes that too. Either one is kept while another pipeline still references it, which a pipeline created before the picker filter below could do in `glue_connection` mode; the response lists those pipelines under `cleanup_results.source_connection_kept_for` or `target_connection_kept_for`.

### `glue_connection`

The user selects an existing Glue Connection from `/api/resources/rds/glue-connections`. That list leaves out the connections Agentic Data Ingestion creates for itself: each pipeline's `agentic-data-ingestion-{pipeline_id}-source` and `-rds` connection, and the `agentic-data-ingestion-discover-{hash}` connections Detect Schema creates (`is_managed_glue_connection` in `shared/jdbc_helpers.py`, which also matches the `AgenticDataIngestion JDBC connection (…)` description the provisioner writes). A `-source` or `-rds` connection is deleted with its pipeline and a `discover-` connection by the discovery janitor, so a pipeline that reused one would break when its owner went. `POST /pipelines` refuses those names in `glue_connection_name` on either side with a 400, and an edit refuses switching a target to one (`_reject_managed_glue_connection` in `handlers/pipelines_jdbc.py`). Agentic Data Ingestion then:

- Auto-populates JDBC URL and driver from the connection's `JDBC_CONNECTION_URL` and `JDBC_DRIVER_CLASS_NAME`.
- **Does not create a new Glue Connection.** The user's connection is referenced as-is by the runtime crawler and Glue ETL Job.
- **Does not delete the user's connection** when the pipeline is deleted. Agentic Data Ingestion does not own its lifecycle.

This split exists because some teams already manage their Glue Connections via Terraform / CloudFormation and need Agentic Data Ingestion to consume rather than replace them.

---

## Discovery flow

Schema discovery runs a Glue Crawler against the source table and surfaces the resulting `StorageDescriptor.Columns` to the wizard (and later to `schema_extract` at runtime). There are two distinct discovery scopes with different lifecycles.

### Wizard-time discovery (transient)

Triggered when the user clicks **Detect Schema** in the wizard's Source step.

- **Crawler name**: `agentic-data-ingestion-discover-{hash}` where `hash = sha256(connection_identifier + schema + table)[:12]` and `connection_identifier` is `secret_arn + jdbc_url` for `select_rds` mode or `glue_connection_name` for `glue_connection` mode.
- **Database**: `agentic_data_ingestion_jdbc_discovery_{stage}` (provisioned by `Foundation_Stack` with `removalPolicy: DESTROY`).
- **Table prefix**: `agentic_data_ingestion_{hash}_` — forces a deterministic prefix on whatever table name Glue produces.
- **Tags**: `Type=agentic-data-ingestion-discovery, CreatedAt=<ISO timestamp>, Hash={hash}`.
- **TTL**: 7 days. A daily EventBridge rule `agentic-data-ingestion-discovery-cleanup-{stage}` fires at 03:00 UTC and invokes the dashboard API Lambda's `cleanup_discovery_resources()` handler, which deletes crawlers / connections / tables tagged `Type=agentic-data-ingestion-discovery` whose `CreatedAt` tag is older than 7 days. A second pass starts from the connections: an `agentic-data-ingestion-discover-{hash}` connection with no crawler of its name, created and last updated more than 7 days ago, is deleted too. Without it, a connection outlived its crawler whenever the two deletes did not both succeed, or the crawler was never created.
- **Connection**: in `select_rds` mode, Agentic Data Ingestion creates a transient Glue Connection also named `agentic-data-ingestion-discover-{hash}`. In `glue_connection` mode, the user's existing connection is reused; no AgenticDataIngestion-owned connection is created.

The `POST /api/resources/jdbc/detect-schema` endpoint returns HTTP 202 with `{crawler_name, glue_database, table_prefix, status: "RUNNING"}`. The frontend polls `GET /api/resources/jdbc/detect-schema/{crawler_name}` every 3 s for up to 5 minutes. When the crawler completes, the GET handler calls `glue:GetTables(DatabaseName, Expression="agentic_data_ingestion_{hash}_*")` to find the produced table — exactly one match is expected. The wizard persists `detected_glue_database`, `detected_glue_table`, and `detected_table_prefix` in `source_config` for **diagnostic display only**; they are not consumed by the runtime path.

### Runtime discovery (per-pipeline)

Created at pipeline-creation time and bound to the pipeline's lifecycle.

- **Crawler name**: `agentic-data-ingestion-{pipeline_id}-source-discover`.
- **Database**: same `agentic_data_ingestion_jdbc_discovery_{stage}` as the wizard-time crawler.
- **Table prefix**: `agentic_data_ingestion_{pipeline_id}_`.
- **Tags**: `Type=agentic-data-ingestion-runtime, PipelineId={pipeline_id}` — distinguishes them from wizard-time crawlers so the janitor never deletes them.
- **Lifecycle**: created by `handlers/pipelines.create_pipeline`, deleted by `handlers/pipelines.delete_pipeline` along with its produced tables and (in `select_rds` mode) the `agentic-data-ingestion-{pipeline_id}-source` connection.

The runtime `schema_extract` Lambda starts this crawler on every pipeline run, polls `glue:GetCrawler` until `State != "RUNNING"`, then calls `glue:GetTables(Expression="agentic_data_ingestion_{pipeline_id}_*")` and converts the matched table's `StorageDescriptor.Columns + PartitionKeys` to the same `extracted_schema` JSON Schema format the existing S3 path produces.

> **Why two crawlers and not one?** The wizard-persisted `detected_glue_table` exists for UX traceability, but the wizard-time resources have a 7-day TTL — by the time a pipeline is delete-protected and running for months, those resources are gone. The runtime crawler with its own table prefix gives `schema_extract` a stable lookup target that survives wizard-time cleanup.

---

## Watermark semantics

Watermarks let an incremental pipeline read only rows that are new (or updated) since the last successful run.

### Read modes

- **`full_table`** — every run reads the entire source table via Spark's `dbtable` option. Simple, no state. Use when the table is small enough to re-read each run, or when there is no monotonic column.
- **`incremental`** — every run reads only rows where the watermark column is greater than the value stored in `agentic_data_ingestion_pipeline_watermarks` from the previous successful run. Use when the table has a monotonic timestamp or numeric column.

### Watermark types

| `watermark_type` | Validated regex                                                                          | Examples                                          |
|------------------|------------------------------------------------------------------------------------------|---------------------------------------------------|
| `numeric`        | `\A-?\d+(\.\d+)?\Z`                                                                      | `0`, `123`, `-42`, `3.14`                         |
| `timestamp`      | `\A\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z\|[+-]\d{2}:?\d{2})?)?\Z`           | `2024-12-15`, `2024-12-15T08:42:31Z`, `2024-12-15 08:42:31.123+00:00` |

The regexes use `\A` / `\Z` (absolute string anchors) rather than `^` / `$` so trailing newlines cannot smuggle past the validator. They are enforced both at wizard time (in `JdbcSourceForm` validation) and at job runtime (in the rendered template) as defence-in-depth — Spark JDBC does **not** support `?` bind parameters on the `query` option, so strict allow-list validation followed by string interpolation is the only safe approach.

### Watermarks_Table shape

The `agentic_data_ingestion_pipeline_watermarks` DynamoDB table (`PAY_PER_REQUEST`, encrypted, point-in-time recovery, partition key `pipeline_id`) stores one item per pipeline:

```json
{
  "pipeline_id": "p-7f3c9a2b",
  "watermark_column": "updated_at",
  "watermark_type": "timestamp",
  "last_value": "2024-12-15T08:42:31Z",
  "last_updated_at": "2024-12-15T08:43:02Z",
  "last_run_id": "r-2b18c7d4"
}
```

`last_value` is always stored as a string regardless of `watermark_type` for DDB consistency. The template parses it back to the correct type at read time.

The decision to use a dedicated table — rather than reusing `agentic_data_ingestion_state` (per-run rows) or adding a `watermark_state` Map to `agentic_data_ingestion_pipelines` — is documented in the design phase: per-pipeline keying matches the runtime concurrency semantics, avoids stream amplification, and keeps watermark history accessible without churning the `agentic_data_ingestion_pipelines` item version on every run.

### Per-engine `WHERE`-fragment construction

Spark JDBC's `query` option must be plain SQL — no bind parameters. The watermark `WHERE` fragment is built by `shared/jdbc_helpers.py::build_watermark_predicate(engine, column, watermark_type, validated_value)`:

| Engine family                     | Identifier quoting | Timestamp comparison                                                       | Numeric comparison |
|-----------------------------------|--------------------|----------------------------------------------------------------------------|--------------------|
| `postgres` / `aurora-postgresql`  | `"col"` (double)   | `"col" > '2024-12-15T08:42:31Z'::timestamp`                                | `"col" > 12345`    |
| `mysql` / `aurora-mysql`          | `` `col` `` (back) | `` `col` > '2024-12-15T08:42:31Z' `` (implicit cast)                       | `` `col` > 12345 ``|
| `mariadb`                         | `` `col` `` (back) | `` `col` > '2024-12-15T08:42:31Z' `` (implicit cast)                       | `` `col` > 12345 ``|
| `oracle-ee` / `oracle-se2`        | `"col"` (double)   | `"col" > TO_TIMESTAMP('2024-12-15T08:42:31Z', 'YYYY-MM-DD"T"HH24:MI:SS.FF"Z"')` | `"col" > 12345`    |
| `sqlserver-*` (all editions)      | `"col"` (double)   | `"col" > CONVERT(datetime2, '2024-12-15T08:42:31Z', 127)`                  | `"col" > 12345`    |

Hard-coded double quotes would fail on MySQL by default — MySQL treats `"name"` as a string literal unless `sql_mode=ANSI_QUOTES`, which Spark does not set. Hence `quote_identifier(engine, name)` is engine-aware.

### Read → write → watermark ordering

The template caches the source DataFrame, computes `MAX(watermark_column)` from `df` (not `df_transformed` — transforms may drop or rename rows), runs the user's transform, writes to the target, and only then writes the new watermark via a conditional `PutItem`:

```
1.  df = spark.read.format("jdbc")
            .option("query", "SELECT * FROM <schema>.<table> WHERE <predicate>")
            .load()                                  # or option("dbtable", ...) when there is no bound
2.  df.cache()                                       # incremental mode only
3.  if df.count() == 0:
        return                                       # no rows → no watermark write
4.  new_watermark = df.agg(F.max("watermark_column")).first()[0]
5.  df_transformed = transform_block(df)             # AI-generated transform
6.  df_transformed.write.{target_specific}(...)      # may take minutes
7.  ddb.put_item(
        TableName="agentic_data_ingestion_pipeline_watermarks",
        Item={"pipeline_id": ..., "last_value": new_watermark, ...},
        ConditionExpression="attribute_not_exists(last_updated_at) "
                            "OR last_updated_at < :now",
    )
8.  df.unpersist()
```

Why this order:

- **`df.cache()` before MAX** — without caching, Spark's lazy evaluation triggers a separate JDBC read for the MAX action *and* another for the transform, doubling source-database load.
- **MAX before transform** — the watermark must reflect what was actually read, not what was transformed (a transform could drop the latest row).
- **PutItem after target write succeeds** — a write failure leaves the watermark unchanged, so the next run safely re-reads the same rows.
- **Runs never overlap** — each pipeline's Glue job is created with `MaxConcurrentRuns: 1` (`create_glue_pipeline/job_builders.py`), so Glue refuses a second start while a run is in progress, and every run reads the watermark the previous run committed. That is what keeps the stored watermark moving forward. The `last_updated_at < :now` condition compares completion times, not watermark values, so it would **not** stop an older run that finished later from writing a lower value if runs could overlap. Do not raise `MaxConcurrentRuns` without first making the write compare values.
- **The `query` option gets a bare `SELECT`** — Spark wraps that option itself as `SELECT * FROM (<query>) SPARK_GEN_SUBQ_n`, so passing an already-parenthesised, aliased subquery produces a statement the database rejects. The parenthesised `(subquery) AS alias` form belongs only to the `dbtable` option.

### First-run behaviour

On the first run there is no item in `agentic_data_ingestion_pipeline_watermarks`. The template:

1. Issues `GetItem` and gets `None`.
2. Falls back to `source_config.watermark_initial_value` (the wizard's "Initial watermark" field) if present.
3. If there is no initial value, omits the `WHERE` clause entirely and reads the whole table through `dbtable`.

`watermark_initial_value` is the one key new records carry. Records stored under the older spellings `initial_watermark` or `initial_watermark_value` are still read, in that order, after it. The API validates the initial value with the same resolver code generation uses (`shared/jdbc_helpers.py::initial_watermark_entry`), so the value that is validated is the value the job uses.

After the first successful run, every subsequent run is bounded by the previous run's `MAX(watermark_column)`.

---

## Networking model

This section is critical — most JDBC source failures in practice are network reachability issues, not data issues.

### What lives where

```
┌────────────────────────────────────────────────────────────────┐
│  Agentic Data Ingestion AWS account / region                                 │
│                                                                 │
│  ┌──────────────────────────────┐                              │
│  │  Agentic Data Ingestion Lambdas           │                              │
│  │  (NOT in any VPC)            │  ← every Lambda — handlers,  │
│  │                              │    triggers, Step Functions  │
│  │  - dashboard_api             │    workers, AgentCore        │
│  │  - pipeline_trigger          │    runtime — run outside VPC.│
│  │  - file_validation           │                              │
│  │  - schema_extract            │                              │
│  │  - create_glue_pipeline      │                              │
│  │  - run_pipeline              │                              │
│  │  - …                         │                              │
│  └──────────────────────────────┘                              │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Glue (managed VPC routing)                              │  │
│  │  - Crawler agentic-data-ingestion-{pipeline_id}-source-discover       │  │
│  │  - ETL Job agentic-data-ingestion-{pipeline_id}                       │  │
│  │  Both use:                                               │  │
│  │    Connections=[ agentic-data-ingestion-{pipeline_id}-source ]        │  │
│  │  Glue itself plumbs an ENI into the connection's         │  │
│  │  PhysicalConnectionRequirements.SubnetId.                │  │
│  └──────────────────────────────────────────────────────────┘  │
│                              │                                  │
└──────────────────────────────┼──────────────────────────────────┘
                               │   Glue-managed ENI
                               ▼
┌────────────────────────────────────────────────────────────────┐
│  Customer's RDS VPC (existing — Agentic Data Ingestion does NOT create it)   │
│                                                                 │
│  ┌───────────────────────────┐    ┌───────────────────────────┐│
│  │  Private subnet           │    │  Private subnet           ││
│  │  (Glue ENI lives here)    │    │  (RDS instance)           ││
│  │  SG: allow 5432 to RDS SG │    │  SG: allow 5432 from Glue ││
│  └───────────────────────────┘    └───────────────────────────┘│
│                              │                                  │
│  Route table for the Glue subnet MUST include:                 │
│    - S3 VPC Gateway Endpoint (com.amazonaws.{region}.s3)       │
│      so Glue can reach its script bucket and log destinations. │
│    - (No NAT gateway / IGW required for Agentic Data Ingestion's purposes.) │
└────────────────────────────────────────────────────────────────┘
```

### Three rules

1. **No Agentic Data Ingestion Lambda runs in a VPC.** This is intentional and the current state of the codebase. JDBC source does not change this.
2. **The Glue Connection's `PhysicalConnectionRequirements` carries the customer's VPC config** — `SubnetId`, `SecurityGroupIdList`, optional `AvailabilityZone`. Glue uses this to plumb an ENI into the customer's VPC for the duration of the crawl or ETL job.
3. **The customer's RDS subnet must have an S3 VPC Gateway Endpoint.** Without it, the Glue Crawler and the ETL Job cannot reach S3 from inside the VPC, and they will fail with cryptic connect-timeout errors.
4. **It should also have a DynamoDB VPC Gateway Endpoint.** Without it the pipeline still loads its data correctly, but the ETL job cannot record its row counts, so the dashboard reports `not_established` for the run. See below — this one degrades rather than fails, which is why it is easy to miss.

> **An internet gateway is not a substitute for either endpoint.** A Glue ENI receives no public IP, so a `0.0.0.0/0 -> igw-...` route is a blackhole for it: the route exists, the packets go nowhere, and the caller sees a connect timeout rather than a refusal. A subnet can look fully routed and still reach no AWS service. Only a VPC endpoint or a NAT gateway gives a Glue ENI egress.

### S3 VPC Gateway Endpoint requirement

Glue itself stores its job script in S3, writes its CloudWatch log shipper output through S3, and (for S3-backed targets) reads/writes the target data through S3. When Glue runs inside a private subnet via `PhysicalConnectionRequirements`, all of those S3 calls must traverse a route the subnet knows about.

The cheap, secure, AgenticDataIngestion-recommended option is an **S3 VPC Gateway Endpoint** — a route table entry that sends `s3.{region}.amazonaws.com` traffic directly to S3 without leaving the AWS network. The alternative is a NAT gateway, which costs more and is not strictly required.

The wizard performs a pre-flight check during schema discovery: when `select_rds` mode is active and the user clicks **Detect Schema**, the dashboard API calls `ec2:DescribeRouteTables` filtered by the selected RDS instance's subnet and looks for a route whose target is an `vpce-*` endpoint with `ServiceName == "com.amazonaws.{region}.s3"`. If no such route exists, the API fails fast with an actionable HTTP 400:

```
Source RDS subnet subnet-0abc123def has no S3 connectivity — add an
S3 VPC Gateway Endpoint to vpc-0xyz789, or attach a NAT gateway to
the subnet's route table.
```

This pre-flight catches the most common configuration mistake at wizard time, before the user ever creates a pipeline.

### DynamoDB VPC Gateway Endpoint requirement

After writing to the target, the generated ETL job records the run's `rows_read` and `rows_written` to the state table, which is how the dashboard populates a run's load outcome. That write is a DynamoDB `PutItem` issued from inside the VPC, so it needs a route to DynamoDB exactly as the S3 calls need one to S3. A **DynamoDB VPC Gateway Endpoint** is the cheap option; like the S3 one it carries no hourly charge and needs no security group.

**There is no wizard pre-flight for this one.** The `Detect Schema` check described above looks only for the S3 endpoint, so a subnet missing the DynamoDB endpoint passes discovery, creates a pipeline, and runs it successfully.

What you see when it is missing:

- The run **succeeds** and the data lands in the target. This is a telemetry gap, not a data-loss bug — the metrics write is deliberately best-effort so it can never fail a job that has already written.
- The run's row counts are absent, and the dashboard reports the load outcome as `not_established`.
- The Glue job log ends with:

  ```
  WARNING Failed to write load metrics to DynamoDB: Connect timeout on
  endpoint URL: "https://dynamodb.{region}.amazonaws.com/"
  ```

- The job takes about three seconds longer than it otherwise would. The client is bounded to a single attempt with a 3-second connect timeout precisely so that a missing endpoint costs seconds rather than minutes; before that bound existed, an unreachable DynamoDB cost one run eleven minutes of billed DPU time on connect retries, and could push a longer job past its Glue timeout **after** a successful write — reporting a failed run whose data had already landed.

Adding the endpoint turns the warning into a recorded metrics row with no other change. The project's own test-fixtures VPC declares both endpoints (`deploy-cdk/lib/test-fixtures-stack.ts`) and is a working reference for the shape.

### Security group requirements

The Glue Connection's security group(s) must allow:

- **Inbound from itself** on the database port — Glue's ENI shares the security group with itself, so a self-referencing inbound rule is the simplest way to let Glue talk to RDS when both are in the same SG.
- **Outbound to S3** via the gateway endpoint — by default `0.0.0.0/0:443` covers it. If outbound is locked down, an explicit rule for the S3 prefix list (`pl-*`) suffices.

If Glue and RDS are in *different* security groups, the RDS SG must explicitly allow inbound from the Glue SG on the database port.

---

## Triggering

JDBC-source pipelines run on a schedule, on demand via Run Now, or both.

### Schedule rule

Every JDBC-source pipeline is required to specify a `schedule` (cron or rate expression). The dashboard API extends the existing `_manage_schedule_rule` function in `handlers/pipelines.py` to:

- Use the existing rule name pattern `agentic-data-ingestion-schedule-{pipeline_id}` — same lifecycle as today's schedule rules for S3 sources, so `archive_pipeline` (disable) and `restore_pipeline` (re-enable) work unchanged.
- Send a target input that includes `source_type` so the trigger Lambda can branch:

```json
{
  "trigger": "scheduled",
  "pipeline_id": "p-7f3c9a2b",
  "source_type": "JDBC",
  "source_prefix": "p-7f3c9a2b"
}
```

`source_prefix` is preserved for backward compatibility — JDBC-source pipelines ignore it; S3-upload-source pipelines continue to use it to find the most recent uploaded file (the trigger lists `{source_prefix}/`).

The payload is built by `_schedule_target_input`, and `_manage_schedule_rule` refuses to create or update a rule without a known `source_type`. Create passes the request's `source_type`; edit reads it from the stored record, since an edit cannot change it, and treats a record with none as `S3_UPLOAD`, the same default the trigger applies. Until 2026-10-10 the payload carried no `source_type` at all, so every JDBC schedule took the trigger's S3 branch, found no uploaded file and started nothing. `tests/contracts/test_schedule_payload_cross_lambda.py` feeds the payload `create_pipeline` writes to the real trigger handler.

`PutTargets` reports a target it could not attach in the response (`FailedEntryCount`, `FailedEntries`) while still returning HTTP 200, so `_manage_schedule_rule` checks the count and raises. On create that fails the pipeline and removes the rule with the rest of the request's resources; on edit it returns a 500 before the record is written.

Clearing a schedule on edit removes the rule before the record is written. `RemoveTargets` reports a target it could not remove the same way, so the delete checks its `FailedEntryCount` too; any failure other than a rule that is already gone returns a 500 and leaves the stored schedule in place, so the dashboard keeps showing a schedule that may still run. Until 2026-10-10 the record was cleared first and a failed delete was only logged, so the edit returned 200 while the rule kept firing.

Example schedule expressions:

```
rate(1 hour)
cron(0 2 * * ? *)        # daily at 02:00 UTC
cron(0 2 ? * MON *)      # every Monday at 02:00 UTC
```

The wizard pre-flight-validates the expression against the EventBridge cron/rate regex; the same validation runs server-side (`events_client.put_rule` returns `ValidationException` on invalid expressions, which is caught and re-raised as `ValueError`).

### Run Now

Every pipeline (both S3 and JDBC sources) shows a **Run Now** button on `PipelineOverviewPage`. The button posts to `POST /api/pipelines/{pipeline_id}/run`, which:

1. Reads the pipeline's `source_type` from `agentic_data_ingestion_pipelines`.
2. Synchronously invokes `Pipeline_Trigger_Lambda` with payload `{"trigger": "manual", "pipeline_id": "...", "source_type": "<JDBC|S3_UPLOAD>"}`.
3. Returns HTTP 200 with `{execution_arn, execution_name}`, HTTP 429 if the global concurrency limit is hit, or HTTP 504 if the trigger Lambda invocation times out.

The trigger Lambda's existing 5 s timeout is sufficient because both branches (S3-listing for `S3_UPLOAD`, direct SFN start for `JDBC`) complete well under that.

> Two older endpoints remain, and neither is wired to Run Now. `POST /api/trigger-pipeline` starts a run of one S3-upload pipeline for one uploaded object, and no dashboard code calls it. It refuses an unknown pipeline (404), an archived or non-`S3_UPLOAD` pipeline (409), and an `s3_uri` that is not `s3://<upload bucket>/<pipeline_id>/<file name>` (400). It still calls `StartExecution` directly, so it takes no pipeline lock and skips the duplicate-content gate and the global concurrency limit. `POST /api/pipelines/{pipeline_id}/glue-job/run` bypasses Step Functions and only re-runs the existing Glue job. A future cleanup may consolidate them.

### Step Functions execution input

For `source_type=JDBC`:

```json
{
  "pipeline_id": "p-7f3c9a2b",
  "source_type": "JDBC",
  "trigger": "scheduled"
}
```

For `source_type=S3_UPLOAD` (preserved from today's shape):

```json
{
  "pipeline_id": "p-7f3c9a2b",
  "source_type": "S3_UPLOAD",
  "trigger": "s3_upload",
  "source_bucket": "adi-source-files-123456789012-us-west-2",
  "source_key": "p-7f3c9a2b/2024/12/15/orders.csv"
}
```

The `source_type` and `pipeline_id` fields are now present on **every** execution input produced by `Pipeline_Trigger_Lambda` after P17 ships. `file_validation` reads `pipeline_id` directly when present and falls back to S3-key-prefix derivation only for in-flight executions started before the deploy.

---

## Code generation

### `TEMPLATE_RULES`

The legacy `TEMPLATE_MAP` (a flat dict keyed on lowercased `target_type`) is replaced by `TEMPLATE_RULES`, an ordered list of `(source_type, target_type, filename)` tuples evaluated in order. The first match wins. This also fixes a pre-existing P14 bug where `rds_database` had no entry in `TEMPLATE_MAP` and silently fell through to AI-only codegen.

```python
TEMPLATE_RULES: list[tuple[str, str | None, str]] = [
    # JDBC source — wildcard target, matched first when source is JDBC.
    ("JDBC", None, "glue_job_jdbc_source.py.j2"),

    # S3 upload source — preserves all existing routes.
    ("S3_UPLOAD", "s3_tables",    "glue_job_s3_tables.py.j2"),
    ("S3_UPLOAD", "glue_catalog", "glue_job_glue_catalog.py.j2"),
    ("S3_UPLOAD", "jdbc",         "glue_job_jdbc.py.j2"),
    ("S3_UPLOAD", "rds_database", "glue_job_jdbc.py.j2"),  # P14 fix
    ("S3_UPLOAD", "s3_parquet",   "glue_job_s3_parquet.py.j2"),
]
```

The resolver picks `source_type` from `pipeline_metadata.get("source_type", "S3_UPLOAD")` (defaulting preserves every pre-P17 pipeline) and `target_type` from the existing `target_config.type` normalisation logic (`type_map`).

The Strands code-generation agent's prompt template (`shared/prompt_templates/transform_generation.j2`) is unchanged — `transform_block` operates on a Spark DataFrame `df` regardless of how `df` was loaded, so it is source-agnostic.

### `glue_job_jdbc_source.py.j2`

The new template lives at `pyspark_templates/glue_job_jdbc_source.py.j2` and reuses the four target write partials (extracted from the existing four templates) so write logic is not duplicated.

Render-time variables: `engine`, `jdbc_url`, `jdbc_driver`, `jdbc_schema`, `jdbc_table`, `secret_arn`, `read_mode`, `watermark_column`, `watermark_type`, `target_type`, `target_config`, plus the standard `transform_block` and `dqdl_rules`. `engine` is **not** a runtime arg — it is baked into the rendered code so per-engine `quote_identifier` / `build_watermark_predicate` calls compile cleanly.

Runtime arguments declared via `getResolvedOptions`:

- **Always present (source-side)**: `JOB_NAME`, `jdbc_url`, `jdbc_driver`, `jdbc_schema`, `jdbc_table`, `secret_arn`, `read_mode`, `pipeline_id`, `run_id`.
- **Incremental-only** (rendered conditionally): `watermark_column`, `watermark_type`, `watermarks_table_name`.
- **Target-specific** (rendered conditionally with the `target_` prefix to disambiguate from source-side `jdbc_*` keys when source AND target are both JDBC):
  - `S3_TABLES`: `target_table_bucket_arn`, `target_namespace`, `target_table_name`, `target_warehouse_location`.
  - `GLUE_CATALOG`: `target_database_name`, `target_table_name`.
  - `JDBC` / `RDS_DATABASE`: `target_jdbc_url`, `target_jdbc_driver`, `target_jdbc_table`, `target_secret_arn` (`glue_job_jdbc_source.py.j2:112-122`). `target_secret_arn` is declared **unconditionally** for a JDBC target; there is no plaintext `target_jdbc_user` / `target_jdbc_password` arm any more. See [Target credential contract](#target-credential-contract).
  - `S3_PARQUET`: `target_output_path`.

`getResolvedOptions` requires every declared key to be present in `sys.argv`; rendering the keys list conditionally (rather than passing empty-string sentinels) avoids that failure mode.

### Argument builders

`create_glue_pipeline` Lambda gains:

- `build_jdbc_source_arguments(source_config)` — produces the source-side `--<key>` arguments (`--jdbc_url`, `--jdbc_driver`, …) declared by the template's source-side `getResolvedOptions` list.
- `key_prefix="target_"` parameter on each existing target argument builder (`build_s3_table_bucket_arguments`, `build_glue_catalog_arguments`, `build_jdbc_target_arguments`, `build_s3_parquet_arguments`). When `source_type == "JDBC"`, the dispatcher invokes the target builder with `key_prefix="target_"` so the emitted keys match the template's prefixed names. The default empty prefix preserves S3-source pipelines.

The final Glue job `DefaultArguments` is the union of the source-arg helper output and the prefixed target-arg builder output. `tests/contracts/api_responses/test_glue_parameter_contract.py::TestJdbcSourceParameterContract` enforces parity between the union and the template's `getResolvedOptions` list for every `(read_mode, target_type)` combination.

---

## Target credential contract

A JDBC target has **one credential path: a Secrets Manager reference.** It is a **standing invariant** of this system that JDBC credentials are only ever referenced by a Secrets Manager ARN, and that plaintext user/password never appear in Glue job arguments or in Glue `ConnectionProperties`.

### One reference, resolved once

Every rendered JDBC-target script declares the reference and fetches the credential with it at run time. Both JDBC-target templates share one write, `pyspark_templates/_partials/_write_jdbc.py.j2`: `glue_job_jdbc.py.j2` includes it with `key_prefix ''` (`glue_job_jdbc.py.j2:209`) and declares `secret_arn` (`:26`), and `glue_job_jdbc_source.py.j2` includes it with `key_prefix 'target_'` and declares `target_secret_arn` (`:122`). The partial calls `boto3.client("secretsmanager").get_secret_value(SecretId=args['{prefix}secret_arn'])` and builds `jdbc_properties` from the returned `username` / `password` (`_write_jdbc.py.j2:17-25`). The job argument holds only the reference.

**The reference is resolved in one place**, `resolve_jdbc_target_secret` (`lambda/agentic_data_ingestion_create_glue_pipeline/job_arguments.py:181`), in this order:

1. `target_config.secret_arn`. The dashboard API copies the connection's `SECRET_ID` here when the pipeline is created (`_augment_glue_connection_config`), and `select_rds` / `manual` modes require it outright.
2. `ConnectionProperties.SECRET_ID` of the named Glue connection (`jdbc_connection_name`, else `glue_connection_name`), read with `GetConnection(HidePassword=True)` (`:173`) so an inline `PASSWORD` never enters the Lambda's memory.
3. Otherwise a `ValueError` naming the connection and the fix. There is no fallback to inline credentials.

`SECRET_ID` may be an ARN or a secret **name**; both are passed to the job as-is, because `get_secret_value` accepts either. The resolver never calls `GetSecretValue` itself, and the `create_glue_pipeline` Lambda holds no Secrets Manager client in `job_arguments.py` at all.

`build_jdbc_arguments` (`:601`) emits `--{prefix}secret_arn` for **every** JDBC target (`:706`) and never `--{prefix}jdbc_user` / `--{prefix}jdbc_password`. If a stored `target_config` still carries `jdbc_user` / `jdbc_password` (written before the API started refusing them), the builder ignores both and logs a warning that names the keys, never the values (`:680`). Its INFO summary line lists argument **key names** only (`:738`).

The IAM grant comes from the same resolver. The JDBC-target branch of `iam_roles.create_pipeline_specific_role` grants `secretsmanager:GetSecretValue` on the resolved reference (`iam_roles.py:368-381`), turning a secret name into the `arn:aws:secretsmanager:<region>:<account>:secret:<name>-??????` pattern IAM needs (`job_arguments.secretsmanager_resource_arn`, `:262`). The role builder runs first in `create_glue_job` and writes the resolved value back to `target_config["secret_arn"]`, so the argument builder reads the same value and the job is granted exactly the secret it is told to read.

### What is refused, and where

| Configuration | Refused by | Result |
|---|---|---|
| `target_config.jdbc_user` or `jdbc_password` present, any value, any target type | `_reject_plaintext_target_credentials` (`dashboard_api/handlers/pipelines_jdbc.py:498`), called by `create_pipeline` and `edit_pipeline` before any Glue call or write | HTTP 400 naming the keys, never the values |
| `glue_connection` target whose connection has no `SECRET_ID` (for example inline `USERNAME` / `PASSWORD`) | `_augment_glue_connection_config` (`pipelines_jdbc.py:240`), the same refusal the source side always had | HTTP 400 |
| A stored JDBC target with no `secret_arn` whose connection has no `SECRET_ID` | `resolve_jdbc_target_secret`, during `create_glue_pipeline` | `ValueError` at Glue-job creation; the run fails before any job starts |

The second row is a change. The target side used to tolerate a connection with inline credentials and leave `secret_arn` unset, because `job_arguments.py` then copied the connection's `USERNAME` / `PASSWORD` (or the plaintext behind its `SECRET_ID`, fetched by the Lambda) into `--target_jdbc_user` / `--target_jdbc_password`. That tolerance is withdrawn in favour of the invariant, superseding the part of the ratified record `LGR-c246ab8-1` that relied on it; the rest of that record (the `select_rds` check keyed on `glue_connection_name`, and no `secret_arn` needed in a `glue_connection` request) still stands. [TROUBLESHOOTING.md § JDBC target secret could not be resolved](TROUBLESHOOTING.md#jdbc-target-secret-could-not-be-resolved) covers the fix for an affected connection and the clean-up for pipelines created before the change.

### Why the plaintext path was removed

**Glue job arguments are visible in the console, CloudTrail, and job-run history**, so a plaintext `--target_jdbc_password` leaks the target credential. The builder also used to log its whole argument dictionary at INFO, which put the same value into CloudWatch. This is not hypothetical: a target database credential leaked this way once already, which is why the ARN-only rule is an invariant rather than a preference. `glue_job_jdbc.py.j2` (the S3-upload → JDBC path) had no secret branch at all before the change; it now uses the shared partial, so the two JDBC-target templates cannot drift onto different credential paths again.

### `SECRET_ID` and the `adi:secret_arn` tag — a deliberate duplication

The secret ARN is held in two places and **both are load-bearing.** `SECRET_ID` in the Glue connection's `ConnectionProperties` is required — it is what Glue consults at job runtime. The provisioner also writes an `adi:secret_arn` resource tag, intended as a read-path mirror. Neither reader consults the tag any more — Glue rejects `GetTags` on a connection ARN, so the tag read never resolved — and both read `SECRET_ID`:

- `_augment_glue_connection_config` (`lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_jdbc.py:240`) reads `props.get("SECRET_ID")` at `:341`.
- `resolve_jdbc_target_secret` (`lambda/agentic_data_ingestion_create_glue_pipeline/job_arguments.py:181`) reads it for the job's `--{prefix}secret_arn` argument and the matching IAM grant, when the target config does not already carry `secret_arn`.

**The duplication is deliberate, not redundancy.** `SECRET_ID` is the storage location and the tag is only a read-path mirror, and that ordering was established the hard way: an earlier change moved `SECRET_ID` out to the tag alone, on the mistaken belief that Glue had begun rejecting the property. That **broke all JDBC provisioning**: wizard schema discovery and both source and target connection creation. Verified against the live Glue API on 2026-06-22, a connection carrying only `JDBC_CONNECTION_URL` is rejected by both `CreateConnection` and `UpdateConnection` with the generic "Validation for connection properties failed", while URL + `SECRET_ID` succeeds. Do not "clean up" either copy. The rule holds until Glue supports tag-based secret resolution natively.

The constraint is stated in full at `shared/jdbc_helpers.py:543-568`, quoted verbatim rather than paraphrased because no symbol edge expresses it. The bracketed token near the end is that comment's own pointer into the project's internal decision record, and the comment states the reason beside it:

```python
    # AWS Glue REQUIRES credentials on a JDBC connection. A connection
    # carrying only JDBC_CONNECTION_URL is rejected by both
    # CreateConnection and UpdateConnection with the (unhelpfully generic)
    # error "Validation for connection properties failed". SECRET_ID is
    # the documented, supported property for attaching Secrets Manager
    # credentials, so it MUST live in ConnectionProperties.
    #
    # (A previous change moved SECRET_ID out to a tag on the mistaken
    # belief that Glue had started rejecting it; verified against the live
    # Glue API on 2026-06-22 that the opposite is true — URL-only fails,
    # URL+SECRET_ID succeeds. That change broke ALL JDBC connection
    # provisioning: wizard schema discovery and source/target creation.)
    #
    # We ALSO mirror the secret ARN into the adi:secret_arn tag so the
    # read path can resolve credentials without depending on the property
    # name. That read path is PLURAL — not one function, so do not go
    # looking for a single resolver: ``_augment_glue_connection_config``
    # (``handlers/pipelines_jdbc.py:249``) reads
    # ``tags.get("adi:secret_arn") or props.get("SECRET_ID")`` at ``:335``,
    # and ``create_glue_pipeline/job_arguments.py:151`` does the identical
    # read for the job's runtime arguments.
    #
    # Both copies are load-bearing — ``LGR-819980f-2``: SECRET_ID in
    # ConnectionProperties is what Glue consults at job runtime, the tag is
    # a read-path mirror, and removing either broke all JDBC provisioning
    # once already. Do NOT "clean up" the duplication.
```

Both writes happen in the same branch, which is why neither copy can be dropped by accident: `jdbc_helpers.py:570-572` sets `connection_properties["SECRET_ID"]` and `connection_tags["adi:secret_arn"]` together, guarded by one `if secret_arn:`.

> **The quoted comment's pointers into the read path are out of date, and that is not a licence to remove `SECRET_ID`.** It describes both readers as `tags.get("adi:secret_arn") or props.get("SECRET_ID")` and cites `job_arguments.py:151`. Both readers now read `SECRET_ID` only (see the list above), and the `job_arguments.py` read is `resolve_jdbc_target_secret`. The rule the comment states is unchanged: `SECRET_ID` is the required storage location.

### `JDBC_DRIVER_CLASS_NAME` is deliberately omitted

`JDBC_DRIVER_CLASS_NAME` is intentionally **not** set on the connection for the built-in engines (MySQL, Postgres, MariaDB, Oracle, SQL Server, Aurora) — `shared/jdbc_helpers.py:530-538`. Glue auto-detects the driver class from the JDBC URL's protocol prefix (`jdbc:mysql:`, `jdbc:postgresql:`, …). When `JDBC_DRIVER_CLASS_NAME` **is** set, Glue treats it as a custom-driver hint and demands `JDBC_DRIVER_JAR_URI` alongside it, which is not provided for built-in engines. The driver class still reaches the job — at runtime, via the Glue job argument (`target_jdbc_driver`, or `jdbc_driver` source-side) and Spark JDBC's `properties.driver` — so generated code uses the correct driver.

A provisioned JDBC connection therefore carries exactly three properties: `JDBC_CONNECTION_URL` and `JDBC_ENFORCE_SSL` (`jdbc_helpers.py:540-541`), plus `SECRET_ID` when a secret ARN is supplied (`:571`).

> **`create_glue_jdbc_connection` is the only Glue-connection writer.** A second function, `create_jdbc_connection` in `lambda/agentic_data_ingestion_create_glue_pipeline/connections.py`, wrote **five** properties including a plaintext `USERNAME`, which the ARN-only invariant forbids inside its declared scope (`lambda/**`). It had no caller anywhere; `lambda_function.py` only re-exported it. P17 Task 5.5 mandated replacing it with the canonical function and deleting it, and the deletion has now been done, as `LGR-38510fe-8` recorded. Do not reintroduce a connection writer in `create_glue_pipeline`. `create_database_secret` and `configure_vpc_for_jdbc` in the same module lost their only caller with it and are left for a separate dead-code pass; neither writes a credential to a job argument or a connection.

---

## IAM

JDBC source extends three existing roles. No new roles are introduced.

### `infrastructureRole` (dashboard API Lambda)

Existing role + new permissions:

- `glue:CreateCrawler`, `glue:UpdateCrawler`, `glue:StartCrawler`, `glue:GetCrawler`, `glue:DeleteCrawler`, `glue:GetTags` — for both wizard-time and per-pipeline runtime crawler lifecycle.
- `iam:PassRole` for `gluePipelineRole` ARN — so the dashboard API can attach `gluePipelineRole` as the crawler's execution role. **This is critical**: without it, JDBC pipelines fail at create-time with an opaque IAM error. CDK assertion tests pin this grant in place.
- `glue:CreateConnection`, `glue:UpdateConnection`, `glue:GetConnection`, `glue:DeleteConnection` — for `agentic-data-ingestion-{pipeline_id}-source` and the wizard-time `agentic-data-ingestion-discover-{hash}` connections.
- `glue:GetTables`, `glue:GetTable`, `glue:DeleteTable` on `agentic_data_ingestion_jdbc_discovery_*` — for discovery polling and cleanup.
- `ec2:DescribeRouteTables`, `ec2:DescribeSubnets`, `ec2:DescribeVpcEndpoints` — for the wizard-time S3 endpoint pre-flight check.

### `gluePipelineRole` (Glue jobs and crawlers)

Existing role + new permissions:

- `dynamodb:GetItem`, `dynamodb:PutItem` on `arn:aws:dynamodb:{region}:{account}:table/agentic_data_ingestion_pipeline_watermarks` — covered by the existing `agentic_data_ingestion*` policy; verified rather than duplicated by a CDK assertion test.
- `glue:GetTable`, `glue:GetDatabase` on `agentic_data_ingestion_jdbc_discovery_*` — so the runtime crawler can write into the discovery database and the ETL Job can read from it if needed.

### `schemaExtractRole` (schema_extract Lambda)

Existing role + new permissions:

- `glue:StartCrawler`, `glue:GetCrawler` on `arn:aws:glue:{region}:{account}:crawler/agentic-data-ingestion-*` — so `schema_extract` can drive the per-pipeline runtime crawler. **Critical**: without it, JDBC pipelines fail at the SchemaExtract Step Functions state with a permissions error rather than a feature error.
- `glue:GetTables`, `glue:GetTable` on `agentic_data_ingestion_jdbc_discovery_*` — for the post-crawl table lookup.

---

## Source config shape

The pipeline DDB item gains two top-level fields: `source_type` (string) and `source_config` (Map). Both follow the existing flat-field convention used by `target_type` / `target_config`.

`source_type` defaults to `"S3_UPLOAD"` when omitted, preserving every pipeline created before P17.

### `source_type = "S3_UPLOAD"`

`source_config` is empty `{}`; the trigger relies on the implicit S3 prefix `s3://adi-source-files-*/{pipeline_id}/`.

### `source_type = "JDBC"`, `read_mode = "full_table"`

```json
{
  "engine": "postgres",
  "jdbc_url": "jdbc:postgresql://my-rds.abc123.us-west-2.rds.amazonaws.com:5432/orders_db?ssl=true&sslmode=require",
  "jdbc_driver": "org.postgresql.Driver",
  "jdbc_schema": "public",
  "jdbc_table": "orders",
  "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/orders-db-credentials-AbCdEf",
  "read_mode": "full_table",
  "db_instance_identifier": "my-rds",
  "detected_glue_database": "agentic_data_ingestion_jdbc_discovery_dev",
  "detected_glue_table": "agentic_data_ingestion_a1b2c3d4e5f6_orders",
  "detected_table_prefix": "agentic_data_ingestion_a1b2c3d4e5f6_"
}
```

### `source_type = "JDBC"`, `read_mode = "incremental"`

```json
{
  "engine": "postgres",
  "jdbc_url": "jdbc:postgresql://my-rds.abc123.us-west-2.rds.amazonaws.com:5432/orders_db?ssl=true&sslmode=require",
  "jdbc_driver": "org.postgresql.Driver",
  "jdbc_schema": "public",
  "jdbc_table": "orders",
  "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/orders-db-credentials-AbCdEf",
  "read_mode": "incremental",
  "watermark_column": "updated_at",
  "watermark_type": "timestamp",
  "watermark_initial_value": "2024-01-01T00:00:00Z",
  "db_instance_identifier": "my-rds",
  "detected_glue_database": "agentic_data_ingestion_jdbc_discovery_dev",
  "detected_glue_table": "agentic_data_ingestion_a1b2c3d4e5f6_orders",
  "detected_table_prefix": "agentic_data_ingestion_a1b2c3d4e5f6_"
}
```

### Connection-mode fields

In addition to the above, exactly one of:

- `db_instance_identifier` OR `db_cluster_identifier` — for `select_rds` mode.
- `glue_connection_name` — for `glue_connection` mode.

### Notes

- `jdbc_port` is **not** a separate field; the port is embedded in `jdbc_url`. The wizard keeps a transient `_port` field for editability but folds it into `jdbc_url` before submission.
- `detected_glue_database` / `detected_glue_table` / `detected_table_prefix` are **diagnostic only**. The runtime path uses the per-pipeline crawler with `TablePrefix=agentic_data_ingestion_{pipeline_id}_` and looks up tables via `glue:GetTables` with that prefix; the wizard-persisted values may safely diverge (and the wizard-time crawler resources are deleted by the janitor after 7 days).

---

## Target schema modes

P17 covers JDBC as a first-class **source**. P19 finishes the story by making JDBC a first-class **target** along the same axis. The Wizard's Target Schema step (Step 3) offers three operational modes for choosing the columns that get written to the target table. The default for JDBC targets is `mirror_source`.

### The three modes at a glance

| Mode | `state.schemaSource` | What it reads | When to pick it |
|---|---|---|---|
| **Mode 1 — Detect schema** | `'detected'` | Columns of an existing target table, discovered by a wizard-time Glue Crawler against the target connection + `database_name.table_name` | The target table already exists and you want the pipeline to write into its current shape |
| **Mode 2 — Upload / existing** | `'upload'` or `'existing'` | A JSON Schema file the user uploads in the wizard, or a previously-uploaded schema selected from the Schema Management page | You have an explicit target schema definition you want to reuse, decoupled from any live database state |
| **Mode 3 — Mirror source schema** | `'mirror_source'` | The source-side detected schema produced by the P17 source-side Detect Schema flow on Step 2 (`state.detectedSourceSchema`) | JDBC-to-JDBC pipelines that should create or replace the target with the same columns and types as the source |

Mode 3 is the **default for JDBC targets**. Modes 1 and 2 work for every target type (S3 Tables, JDBC, S3 Parquet, and Glue Catalog on pipelines created before it stopped being offered); Mode 3 is only meaningful when the source is JDBC and the source schema has been detected.

### Mode 1 — Detect schema for JDBC targets

Pre-P19 the **Detect Schema** button on the Target step worked for `s3_tables` and `glue_catalog` targets but not for JDBC. P19 wires JDBC into the same path. The implementation reuses the P17 wizard-time crawler infrastructure with no new IAM, no new Lambda, and no live JDBC introspection:

- **Endpoint pair**: `POST /api/resources/jdbc/detect-target-schema` and `GET /api/resources/jdbc/detect-target-schema/{crawler_name}`. The wire shape mirrors the source-side `POST /api/resources/jdbc/detect-schema` byte-for-byte.
- **Crawler name**: `agentic-data-ingestion-discover-{hash}`, where `hash = sha256(connection_identifier + database_name + table_name)[:12]` — the same SHA-256-truncated-to-12-chars function as the source side. A target-side discovery against `(connection X, db Y, table Z)` collides with a hypothetical source-side discovery against `(connection X, schema Y, table Z)` by design — both produce the same Glue Catalog table.
- **Database**: `agentic_data_ingestion_jdbc_discovery_{stage}` — same database as source-side discoveries.
- **Tags**: `Type=agentic-data-ingestion-discovery`, `CreatedAt=<ISO timestamp>`, `Hash={hash}` — identical to source-side, so the existing daily janitor (`cleanup_discovery_resources`) picks up target-side crawlers automatically. **No janitor changes.**
- **Execution role**: same `gluePipelineRole` already provisioned by the P17 Foundation_Stack.
- **Connection mode handling**: `select_rds` mode reuses `create_glue_jdbc_connection()` from `shared/jdbc_helpers.py` to create a transient `agentic-data-ingestion-discover-{hash}` connection (same naming convention as source-side); `glue_connection` mode reuses the user-provided connection without creating a new one.

When the user clicks Detect Schema on a JDBC target, the wizard POSTs the target config, polls the GET endpoint every 3 seconds, and on `READY` writes the discovered columns to `state.detectedTargetSchema` (which the `'detected'` mode in Step 3 reads). On 4xx/5xx the wizard surfaces an inline `<Alert type="error">` with the response's `error` field and leaves wizard state unchanged.

### Mode 2 — Upload / existing

Unchanged from P17. The user either drops a JSON or CSV file onto the `SchemaUploadWizard` and edits types/descriptions inline before "Use This Schema", or selects a previously-saved schema from the Schema Management dropdown. The chosen fields land in `state.schemaFields` directly; no backend round-trip is involved beyond loading the existing schemas list.

### Mode 3 — Mirror source schema

The new wizard-only mode. When `state.schemaSource === 'mirror_source'` and the source is JDBC and the source schema has been detected, Step 3 copies `state.detectedSourceSchema` straight into `state.schemaFields` and renders the standard schema preview table. **There is no backend round-trip** — the data already lives in wizard state from the P17 source-side Detect Schema flow on Step 2.

Two diagnostic strings (`state.sourceConfig.detected_glue_database` and `state.sourceConfig.detected_glue_table`) populated by the P17 source-side flow render above the schema preview as `Mirroring from {detected_glue_database}.{detected_glue_table}` so users can see exactly which source table they're mirroring.

### Source-schema-required prompt

Mode 3 depends on the source-side Detect Schema flow from Step 2 having run successfully. When the user selects `mirror_source` but `state.detectedSourceSchema` is `null` or empty, Step 3 renders an inline `<Alert type="warning">` and the wizard's Next button is disabled:

> Mirror source mode requires source schema. Go back to step 2 and click Detect Schema.

When the user is on Mode 3 with a non-JDBC source (`state.sourceType === 's3_upload'`), a different warning fires:

> Mirror source mode is only available when the source is a JDBC database. Choose another schema source or change the source type on step 2.

In both cases the wizard does NOT auto-flip `state.schemaSource` to a different value — the user must explicitly pick another tile, or go back and configure the source.

### Default-selection rule

When the user advances to Step 3 and has not yet explicitly clicked a tile in the schema-source `<Tiles>` selector:

- `state.targetType === 'jdbc'` → pre-select `'mirror_source'`
- `state.targetType ∈ {'s3_tables', 'glue_catalog', 's3_parquet'}` → pre-select `'detected'` (preserving the pre-P19 default)

A `state.schemaSourceExplicitlySet` boolean tracks whether the user has clicked a tile during the current wizard session. The pre-selection rule only fires when this flag is `false`, so subsequent visits to Step 3 (via the back button) preserve the user's choice. Changing target type on Step 2 resets the flag so the rule fires again on the next visit.

### Wizard-state field split

Pre-P19, `state.detectedSchema` was shared by source-side and target-side detection — a single field with most-recent-write-wins semantics. P19 splits it cleanly so Mode 3 always reads the source-side schema and Mode 1 always reads the target-side schema:

- `state.detectedSourceSchema` — populated by `WizardStepSource` (P17) after the source-side crawler completes. Read by Mode 3.
- `state.detectedTargetSchema` — populated by `WizardStepTarget` for any target that supports detection (`s3_tables`, `glue_catalog` pre-P19, plus JDBC P19+). Read by Mode 1.
- `state.detectedSchema` — kept as a deprecated read-only alias resolving to `detectedTargetSchema` for one release to ease the transition.



---

## Known limitations

V1 deliberately scopes out a number of features. Each has a one-line rationale.

- **Single-table reads only.** Multi-table joins are out of scope. Use the `transform_block` to flatten if needed, or model the join as a database view and read the view as the source table.
- **Engines limited to those bundled in the AWS Glue 5.0 runtime.** No DB2, Snowflake, Teradata, or other custom-driver engines. Adding one would require shipping a JAR and updating `JDBC_DRIVER_MAP` in both `shared/jdbc_helpers.py` and the React frontend.
- **No CDC / Debezium / DMS / logical replication.** Watermark-based incremental is a pull model, not a streaming model. If you need sub-minute freshness or row-level deletes, use a separate CDC pipeline.
- **No stored procedures.** Spark JDBC's `query` option supports only `SELECT`-shaped SQL.
- **A JDBC target supports `overwrite` (the default) and `append` only.** There is no upsert into a database table: `merge` is implemented only for S3 Tables targets, and create, edit and code generation all refuse it for a JDBC target (`shared/write_modes.py`). With an incremental source, `append` is the mode that adds each run's new rows; `overwrite` replaces the table with only that run's rows. Before 2026-10-10 a stored `merge` on a JDBC target silently ran as `append`; such a pipeline now fails its `create_code` step until it is edited to one of the two supported modes.
- **No cross-account RDS.** The Source_Connection's `PhysicalConnectionRequirements` must point at a VPC in the same AWS account as Agentic Data Ingestion's deployment.
- **No IAM database authentication.** Credentials must be a username/password pair in Secrets Manager. Adding IAM auth would require token refresh inside the Spark JDBC driver layer.
- **One schedule per pipeline.** EventBridge rule name pattern `agentic-data-ingestion-schedule-{pipeline_id}` allows exactly one rule per pipeline. Multiple schedules would require a name namespace change and is out of scope.
- **Manual table-existence verification recommended before Detect Schema.** The crawler returns "produced no table" if the table doesn't exist or credentials lack `SELECT` privilege; both surface the same error message. A quick `psql` / `mysql` / `sqlcmd` check from a bastion host saves a round-trip if you suspect a typo.
- **Janitor only cleans up wizard-time resources.** Per-pipeline runtime crawlers are tagged `Type=agentic-data-ingestion-runtime` and bound to the pipeline's lifecycle (deleted by `delete_pipeline`). Forgotten pipelines keep their crawlers around — by design.

---



---

## Manual Connection Mode (P20)

P20 adds a third connection mode `manual` to `JdbcConnectionSelector`,
enabling pipelines for JDBC databases that Agentic Data Ingestion cannot
auto-discover: on-prem databases, databases in another AWS account,
self-managed instances, and SaaS-hosted databases.

### When to use

Use manual mode when the database is **not** an RDS / Aurora instance
in the current AWS account AND there isn't already a pre-built Glue
JDBC Connection that points at it. Concretely:

- **On-prem PostgreSQL/MySQL/Oracle/SQL Server** reachable via Direct
  Connect or Site-to-Site VPN attached to the Agentic Data Ingestion VPC.
- **Database in a different AWS account** reachable via VPC peering,
  Transit Gateway, or PrivateLink.
- **Self-managed database on EC2** in the same or a peered VPC.
- **SaaS-hosted database** with a public endpoint reachable through a
  NAT Gateway in the chosen subnet.

When the database **is** an RDS / Aurora instance in the same account,
prefer `select_rds` mode — it auto-discovers the instance and builds
`PhysicalConnectionRequirements` from the RDS instance's networking
metadata, so the user does not type subnet IDs.

### What the user supplies

The Manual_Mode_Form collects everything `create_glue_jdbc_connection`
needs to build the per-pipeline Glue Connection:

| Field | Purpose |
|---|---|
| `engine` | One of the `JDBC_DRIVER_MAP` keys (Postgres, Aurora-Postgres, MySQL, Aurora-MySQL, MariaDB, Oracle EE/SE2, SQL Server EE/SE/EX/Web). Auto-populates the driver class. |
| `jdbc_url` | The full JDBC URL (e.g. `jdbc:postgresql://onprem.example.com:5432/analytics`). |
| `jdbc_driver` | Auto-populated from `engine`; user can override if their build of the driver uses a different class name. |
| `vpc_id` | The VPC whose subnets and security groups will host the Glue ENI. Pre-selected to the Agentic Data Ingestion VPC when its `Name` tag starts with `AgenticDataIngestion-`. |
| `subnet_id` | A subnet inside `vpc_id` that has a route to the database AND an S3 VPC Gateway Endpoint. |
| `security_group_ids` | One or more security groups inside `vpc_id` whose outbound rules permit the database's port. |
| `secret_arn` | A Secrets Manager secret containing JSON `{username, password}`. |

The Secret picker has a **"Paste ARN manually"** toggle: by default a
filtered `<Select>` shows secrets whose name matches
`/db|database|rds|jdbc|postgres|mysql|oracle|sqlserver/i`; flipping
the toggle swaps in a free-text `<Input>` so the user can paste an
ARN whose secret name does not match the heuristic. The toggle is a
UX heuristic, NOT a privilege boundary — every secret the dashboard
role can list is selectable either way.

### Lifecycle

Manual-mode Glue Connections share **the same lifecycle** as
`select_rds` mode:

- Created at pipeline-creation time as
  `agentic-data-ingestion-{pipeline_id}-source` (or `-target` for JDBC targets) by
  the existing `_provision_jdbc_source_connection` /
  `_provision_jdbc_target_connection` helpers in
  `handlers/pipelines.py`. The only thing that differs from
  `select_rds` is the *source* of the
  `PhysicalConnectionRequirements` dict: `select_rds` derives it from
  `rds:DescribeDBInstances` / `DescribeDBClusters`, while manual mode
  reads `subnet_id` / `security_group_ids` / optional
  `availability_zone` directly from `source_config` /
  `target_config`.
- Reused by the runtime per-pipeline crawler and Glue ETL job —
  exactly the same `Connections.Connections` array shape as
  `select_rds` mode.
- Deleted by `delete_pipeline`'s existing P17 cleanup. Because
  manual-mode `source_config` does NOT carry `glue_connection_name`,
  the existing cleanup predicate `is_jdbc and not source_config.get("glue_connection_name")`
  evaluates to `True` for manual-mode pipelines and the cleanup
  proceeds without any new code.
- Removed again if pipeline creation fails after creating them. A failed source
  connection or crawler fails the create with a 400 and removes the pipeline row
  and, when this system owns it, the source connection. Since 2026-10-10 a failed
  **target** connection or a failed requested schedule also fails the create:
  `_undo_pipeline_create` deletes what that request made by its own
  deterministic names (never a user-supplied `glue_connection_name`), and the
  response carries `reason: "provisioning_failed"`, the `stage` that failed and a
  `cleanup` summary. If something cannot be removed, the pipeline is archived
  rather than left active, and deleting it from the dashboard retries the
  cleanup. Before that, both failures were logged and the create returned 201.

### Networking still on the customer

Manual mode does NOT magically make a non-AWS database reachable from
AWS. The user is responsible for the underlying network plumbing:

- **On-prem:** Direct Connect or Site-to-Site VPN attached to the
  VPC, plus route-table and security-group rules.
- **Cross-account:** VPC peering, Transit Gateway, or PrivateLink,
  plus cross-account security-group references.
- **Public-internet SaaS DB:** NAT Gateway in the chosen subnet so
  the Glue ENI can egress to the database's public IP.
- **S3 endpoint:** the chosen subnet must STILL have an S3 VPC
  Gateway Endpoint — Glue itself reaches S3 from the ENI for script
  staging and log delivery.

P20 V1 explicitly defers a synchronous pre-flight network reachability
test (e.g. a "test connection" Glue API call). The Glue Connection is
created without validation; the first runtime use surfaces a clear
AWS error if routing or security-group rules are misconfigured. A
future spec may add the pre-flight.

### Out of scope for P20 V1

- Custom JDBC driver JARs for engines outside `JDBC_DRIVER_MAP`.
- Cross-account RDS *picking* (manual mode lets the user type the URL
  but does not extend `select_rds`'s auto-discovery to other accounts).
- IAM database authentication (the existing JDBC paths do not support
  it either).
- Migration of pipelines created via P17's two existing modes — the
  third mode is opt-in only when creating a new pipeline.

### Cross-references

- [WORKFLOW.md](WORKFLOW.md) — manual-mode pipelines produce the same
  Glue Connection shape as `select_rds`, so the runtime path is
  unchanged.
- [DASHBOARD.md](DASHBOARD.md) — the `<ManualConnectionForm>` UI and
  the secret-picker toggle.
- The manual-mode design rationale is summarised in this section; the code of record is
  `shared/jdbc_helpers.py` and the wizard's manual-mode branch.

---

## Related docs

- [WORKFLOW.md](WORKFLOW.md) — JDBC source flow added to the 10-step workflow walkthrough; the incorrect statement that `'rds_database'` is in `TEMPLATE_MAP` is corrected.
- [ARCHITECTURE.md](ARCHITECTURE.md) — gains a source-side abstraction section reflecting `source_type` / `source_config`.
- [LAMBDA-FUNCTIONS.md](LAMBDA-FUNCTIONS.md) — entries for the modified `file_validation`, `schema_extract`, `pipeline_trigger`, and `dashboard_api` handlers (no new Lambdas).
- [DATABASE-ARCHITECTURE.md](DATABASE-ARCHITECTURE.md) — `agentic_data_ingestion_pipeline_watermarks` placement in the table-relationship diagram.
- [DYNAMODB-TABLES.md](DYNAMODB-TABLES.md) — full schema for `agentic_data_ingestion_pipeline_watermarks`.
- [DASHBOARD.md](DASHBOARD.md) — new Source step in the wizard.
- [../pyspark_templates/README.md](../pyspark_templates/README.md) — `glue_job_jdbc_source.py.j2` template and the four extracted write partials.
- [../README.md](../README.md) — top-level README lists JDBC alongside S3 upload as a supported source.

The rationale for the choices documented here is carried in this file and in the load-bearing comments it quotes, above all `shared/jdbc_helpers.py:543-568` on why the secret ARN is held in two places. Where a decision is not explained here, the git history of the file is the authority.
