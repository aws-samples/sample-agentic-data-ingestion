# Agentic Data Ingestion Architecture Documentation

## System Overview

Agentic Data Ingestion is a comprehensive AI-powered ETL pipeline automation platform that combines AWS services, AI/ML capabilities, and event-driven architecture to automatically generate, test, and deploy data transformation code.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [System Inventory](#system-inventory)
3. [High-Level Architecture](#high-level-architecture)
4. [Layer Architecture](#layer-architecture)
5. [Data Flow](#data-flow)
6. [AI/ML Integration](#aiml-integration)
7. [Security Architecture](#security-architecture)
8. [Scalability & Performance](#scalability--performance)
9. [Disaster Recovery](#disaster-recovery)
10. [Cost Optimization](#cost-optimization)
11. [Real-Time Updates (WebSocket)](#real-time-updates-websocket)
12. [Glue Job Parameter Contract System](#glue-job-parameter-contract-system)
13. [Infrastructure Deployment](#infrastructure-deployment)
14. [Testing Architecture](#testing-architecture)
15. [Feature Architecture by Phase](#feature-architecture-by-phase)
16. [Related Documentation](#related-documentation)

---

## System Inventory

One owner for every inventory count in this documentation set. Each figure appears here as a
literal exactly once; other docs point at this table rather than restating it. Every row also
states the rule that produces its number, because a figure whose rule is unstated cannot be
checked, only re-litigated — "37 shared modules" and "38 shared modules" are both defensible
until you say whether `__init__.py` counts.

Paths are relative to `agentic_data_ingestion/`.

**Class** says what kind of guarantee a row carries:

- **A** — derivable from the filesystem.
- **B** — derivable from CDK source read as text. No `cdk synth` and no credentials, following
  the pattern `tests/cdk/stacks/test_cdk_table_names.py` already establishes.
- **C** — derivable in principle, derivation not yet pinned. **A holding pen, not a resting
  place**: each C row is expected to become a B row, and until it does its figure carries a
  command and the date that command was last run.
- **D** — irreducibly measured, and **owned elsewhere**. A test count or a wall clock has no
  derivation. This table points at the owner and never restates the number.

A and B rows carry a machine-executable expression in a three-verb derivation language —
`dirs(glob)`, `files(glob)`, `matches(path, "literal")`, with `-name` exclusions — and no date,
because the derivation *is* the currency guarantee. There is no `+` combinator: the two bucket
rows stay separate, and the arithmetic lives in the prose. C and D rows carry a command a human
can re-run plus the ISO date it was last run, and no expression and no number.

| Figure | Count | Class | Derivation |
|---|---|---|---|
| Lambda directories | 25 | A | `dirs(lambda/*/)` |
| shared modules | 38 | A | `files(shared/*.py, -__init__.py)` |
| MCP server directories | 5 | A | `dirs(mcp/*/, -vendor)` |
| PySpark templates | 5 | A | `files(pyspark_templates/*.j2)` |
| AgentCore agent containers | 9 | A | `dirs(agentcore/*/)` |
| files in `docs/` | 21 | A | `files(docs/*)` |
| DynamoDB tables | 17 | B | `matches(deploy-cdk/lib/data-stack.ts, "tableName: '")` |
| GSIs | 21 | B | `matches(deploy-cdk/lib/data-stack.ts, "addGlobalSecondaryIndex")` |
| S3 buckets — Foundation | 9 | B | `matches(deploy-cdk/lib/foundation-stack.ts, "new s3.Bucket")` |
| S3 buckets — Hosting | 2 | B | `matches(deploy-cdk/lib/hosting-stack.ts, "new s3.Bucket")` |
| `AgentRuntimeName:` entries | 14 = 5 + 9 | B | `matches(deploy-cdk/lib/*.ts, "AgentRuntimeName")` |
| CDK stacks, declared | 9 | B | `matches(deploy-cdk/bin/agentic_data_ingestion.ts, "Stack(app,")` |
| Compute Lambdas | 22 | B | `matches(deploy-cdk/lib/compute-stack.ts, "new lambda.Function")` |
| Orchestration Lambdas, asset-sourced | 3 | B | `matches(deploy-cdk/lib/orchestration-stack.ts, "handler: 'lambda_function.lambda_handler'")` |
| wizard steps | 6 | C | `ls dashboard-react/src/pages/wizard/WizardStep*.tsx` — 2026-09-02 |
| approval gates | 3 | C | `grep -c "new sfn.CustomState(this, '[A-Za-z]*Approval" deploy-cdk/lib/orchestration-stack.ts` — 2026-09-02 |
| standing invariants | 11 | C | `chesterton rootmap` — 2026-09-18 |


### Counting rules the table cannot hold in one cell

**S3 buckets are two rows, not one.** Ten buckets total: **9 in Foundation plus 1 hosting site
bucket in Hosting**. They stay separate because that phrasing is what a reader needs anyway,
which removes any need for a `+` combinator in the derivation language.

**`AgentRuntimeName:` is 14 = 5 + 9** — 5 MCP server runtimes plus 9 agent runtimes. All 14
entries live in `deploy-cdk/lib/agentcore-stack.ts`; the glob spans `lib/*.ts` so a runtime
declared in another stack would still be counted.

**`mcp/vendor/` is not an MCP server.** `mcp/` holds 6 directories; `vendor/` carries vendored
handler classes the five servers import, which is why it appears as an explicit exclusion in
the derivation rather than as a footnote.

**shared modules exclude `__init__.py`** — 38 excluding it, 39 including it. Both numbers are
defensible; only one is stated, and the exclusion is visible in the derivation.

**`files in docs/` counts every top-level file, not only Markdown** — 21 files, all `.md` today, and
`docs/` has no subdirectories. Dotfiles are not matched by the glob, and were the directory to gain a
subdirectory it would not be counted either.

The rule is deliberately broader than the current contents. It counted 23 until p67 removed
everything from `docs/` that was not current documentation: two `.html` files (an internal sales deck
carrying non-public customer figures, and a mock-up of an AWS Solutions Library page) were deleted,
and the `archive/` and `blog/` subdirectories were relocated **above the project prefix** to
`internal-docs/`, so they are outside the publishable unit rather than merely uncounted. Stating the
glob rather than "Markdown files" means adding a non-Markdown file changes the count instead of
silently escaping it.

**The derivation no longer carries a carve-out, and that is the point.** It read
`files(docs/*, -PUBLISHING.md)` for as long as `PUBLISHING.md` lived here, because that file is
internal-only and the export dropped it, so counting it made the row right in this checkout and
wrong by one in the export. p69 moved the file to `internal-docs/` instead, which removes the
exception rather than encoding it: the glob is now plain and the row holds in the internal checkout,
the derived `clean` branch, and the export alike. **A carve-out in a derivation is a sign the file is
in the wrong directory** — prefer moving the file to widening the rule.

**Orchestration Lambdas: 3 is the asset-sourced count, not the function count.** The full rule,
with construct ids and line numbers for all 7 `new lambda.Function`s, is in
[Infrastructure Deployment](#infrastructure-deployment). Never quote the 3 alone.

**Compute + Orchestration reconciles with the directory count:** 22 + 3 = 25.

**CDK stacks: 9 declared, 8 deployed by a default `cdk deploy --all`.** The row counts declarations,
because that is what a derivation can re-run. The ninth is `TestFixtures`, which is instantiated only
when `-c adi:deployTestFixtures=true` is passed and provisions the JDBC test databases, so a plain
`--all` skips it. **"8 stacks" is therefore correct in every document that says it** — it is the
deployed count, and it was the most-restated figure in the documentation before this row existed,
appearing in seven files with no owner. Quote 8 when describing a deploy and 9 when describing the
file; do not reconcile them by changing one to match the other.

**Test counts, suite wall clocks, and the `lint` / `type-check` / `security` / `format-check`
baselines are deliberately absent from this table, and from the documentation entirely.** They were
Class D rows here until p67, pointing at a figure held elsewhere. They are not inventory: an
inventory row answers "how many of these exist in the tree", which a derivation can re-run, whereas
a test count or a wall clock measures one tree on one machine at one moment and cannot be re-derived
from source. Carrying them as dated rows meant a reader had to judge whether a date was recent
enough to trust, which is a worse position than having no number and one command.
[TESTING.md](TESTING.md) therefore carries the command for each instead of the figure: § **Test
Tiers** for the suites, and § **Several gates exit non-zero on a clean tree** for the four static
gates, each with the exact invocation. Run it.
## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        PRESENTATION LAYER                        │
│  Dashboard (S3 + CloudFront/OAC) + Cognito auth + API Gateway   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      ORCHESTRATION LAYER                         │
│         Step Functions State Machine (29 states, 10 Lambda steps)│
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                       PROCESSING LAYER                           │
│  25 Lambda Functions (File validation → Glue job creation + WS) │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                         AI/ML LAYER                              │
│  Bedrock + Strands Agents + AgentCore MCP (5 Focused Servers)   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                        STORAGE LAYER                             │
│        S3 + DynamoDB (tables, GSIs) + EventBridge                │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                       EXECUTION LAYER                            │
│              AWS Glue Jobs (Generated ETL code)                  │
└─────────────────────────────────────────────────────────────────┘
```

> The Lambda function count in the diagram above is owned by
> [System Inventory](#system-inventory), which carries its derivation. It is repeated here as a
> literal only because a fixed-width diagram cannot hold a pointer — if the two ever disagree, the
> manifest is right.

---

## Layer Architecture

### 1. Presentation Layer

**Components**:
- **Dashboard**: React 18 + TypeScript + Cloudscape Design System
- **CloudFront Distribution**: Serves static assets from S3 (OAC) + proxies `/api/*` to REST API
- **API Gateway**: REST API with Cognito User Pools authorizer
- **Dashboard API Lambda**: Request routing and business logic

**Technology Stack**:
- Frontend: React 18, TypeScript, Cloudscape, TanStack React Query, React Router v6
- Auth: `react-oidc-context` + `oidc-client-ts` (Hosted UI OIDC code flow + PKCE)
- Build: Vite 6
- API: Python 3.12, boto3
- Real-Time: API Gateway WebSocket API + `useWebSocket` React hook (JWT on `$connect`)
- Hosting: S3 (private, OAC) + CloudFront + WAF
- Local Dev: Vite dev server with CloudFront as proxy target (see `docs/LOCAL_DEVELOPMENT.md`)

### 2. Orchestration Layer

**Component**: AWS Step Functions State Machine

**Definition**: Type-safe CDK constructs (Phase 5). The state machine is defined using `sfn.Chain`, `tasks.LambdaInvoke`, `sfn.Choice`, `sfn.Condition`, and `sfn.CustomState` constructs instead of raw JSON. This provides compile-time validation, IDE autocomplete, and eliminates `$` prefix bugs.

**CDK Construct Patterns**:
- `tasks.LambdaInvoke` — each Lambda step with typed `resultPath`, `payload` via `sfn.TaskInput.fromObject()`, and `retryOnServiceExceptions`
- `sfn.Choice` + `sfn.Condition.stringEquals()` — branching on schema comparison results (e.g., `CheckSchemaAction`, `CheckSchemaMappingApproval`, `CheckCodeApproval`)
- `sfn.CustomState` — `waitForTaskToken` integration pattern for approval gates, the four container-backed steps, and Glue job completion (CDK lacks a native construct for this)
- `sfn.Pass` — pass-through states like `ReuseExistingMapping`

**Workflow States**: The state machine has **29 states total**. The 24 core workflow states below include **10 Lambda processing steps** plus `Choice`, `Pass`, and `waitForTaskToken` states. The remaining 5 are lifecycle states — `ReleaseLockSuccess`, `UpdateSuccessState` (Pass), `RecordFailure`, `ReleaseLockFailure`, and `PipelineFailed` (Fail) — that run on completion/failure and are not shown in the workflow sequence below.

Core workflow states (24), in graph order:
1. **FileValidation** — Validate format, size, structure
2. **SchemaExtract** — Parse and extract schema
3. **SchemaCompare** — Detect schema drift
4. **CheckSchemaAction** — Choice: reuse existing or create new mapping
5. **ReuseExistingMapping** — Pass state for existing pipeline reuse. **It saves the mapping computation, not the approval round-trip.** Taking this branch skips the `SchemaMapping` step and with it the Bedrock Converse call, which is a real per-run saving. It does *not* skip the schema-mapping approval gate: the Pass injects no `approval_required`, and `CheckSchemaMappingApproval` admits its permissive branch only on `And[IsPresent, IsBoolean, BooleanEquals false]`, so an absent field fails the first conjunct and the run falls to the Default — a human. That fail-closed behaviour is a standing invariant, deliberate (p47 R4, Arm B) rather than an unfinished optimization. The `IsBoolean` conjunct is load-bearing: a value-only check would read the string `"false"` as truthy and the integer `0` as falsy, inverting the owner's setting in both directions
6. **SchemaMapping** — AI-powered field mapping
7. **CheckSchemaMappingApproval** — Choice: approval required?
8. **WaitForSchemaMappingApproval** — waitForTaskToken approval gate
9. **CheckRegenerationAttempt** — Choice: carry a trigger-supplied `regeneration_attempt` through, or default it *(P32)*
10. **ApplyDefaultRegenerationAttempt** — Pass: seed `regeneration_attempt=0` for a first generation
11. **CreateCode** — Generate PySpark code (template-first + AI transform); waitForTaskToken
12. **TestCode** — Verify the transform (Glue interactive session, or the pandas proxy); waitForTaskToken
13. **TestResultChoice** — Choice: passed → approval check; regeneration in bounds → CreateCode; otherwise forced review *(P28 R4.1)*
14. **ComputeNextRegenerationAttempt** — Pass: compute the next attempt number
15. **IncrementRegenerationAttempt** — Pass: land the incremented counter, then re-enter CreateCode
16. **ForceTestReviewApproval** — waitForTaskToken gate of type `test_failure`, reached once the regeneration budget is spent
17. **CheckCodeApproval** — Choice: code approval required?
18. **WaitForCodeApproval** — waitForTaskToken approval gate
19. **CreateInfrastructure** — Create AWS resources (direct boto3 API, conditional); waitForTaskToken; registers S3 Tables in the Glue Data Catalog for DQ console integration
20. **CreateGluePipeline** — Deploy Glue ETL job; waitForTaskToken. Runs *after* CreateInfrastructure so `warehouse_location` is available
21. **CreateQualityChecks** — Generate data quality rules
22. **CheckJobTimeoutConfig** — Choice: is a job timeout configured? **Its configured branch has no producer, so every execution takes the Default.** The permissive branch requires `And[IsPresent, IsNumeric, NumericGreaterThan 0]` on `$.pipeline_config.max_job_duration_seconds`, and nothing writes that path into execution state — the identifier appears only in `orchestration-stack.ts` and its two test files, and nowhere under `lambda/`, `shared/`, `dashboard-react/src/`, `mcp/` or `agentcore/`. This is a deliberate forward-compatibility seam, not an unfinished feature (p47 R5, Arm B): a producer plugs in with zero ASL changes. Note that `get_pipeline_config` (`approval_handler/lambda_function.py:156`) is **not** that producer — it reads a same-named JSON attribute off a DynamoDB *state row* carrying approval settings, which is a different store from Step Functions execution state, and itself also has no producer
23. **ApplyDefaultJobTimeout** — Pass: seed the default job timeout (21600s / 6h). **Load-bearing, not a convenience default.** `RunPipeline` uses `TimeoutSecondsPath`, and a `TimeoutSecondsPath` that fails to resolve is a `States.Runtime` error no `Retry`/`Catch` can intercept — so this Pass is what makes the path safe to evaluate at all. Since state 22's configured branch has no producer, this is the path *every* execution takes today; removing it would fail every run. The conjunct order and the 21600s result are pinned by assertions in both `deploy-cdk/test/orchestration-stack.test.ts` and `tests/contracts/event_schemas/test_sfn_states_contract.py`
24. **RunPipeline** — Execute Glue job (waitForTaskToken callback); includes inline Glue Data Quality evaluation (EvaluateDataQuality transform) using DQDL rules generated from the schema mapping

**Features**:
- Automatic retry logic (3 attempts per step)
- Error handling with catch blocks
- State persistence in DynamoDB
- EventBridge integration for real-time updates
- **8 `waitForTaskToken` states**: three approval gates (schema mapping, code, forced test review), the Glue job completion callback (`RunPipeline`), and the four container-backed steps (`CreateCode`, `TestCode`, `CreateGluePipeline`, `CreateInfrastructure`). The container-backed four are token states so the proxy Lambda does not idle-bill for the container's full duration — the proxy acks the dispatch in seconds and the AgentCore container closes the loop with `SendTaskSuccess`/`SendTaskFailure` *(P28 R19.11)*. Each carries an explicit `TimeoutSeconds` (the ASL default is a year)
- Timeout handling (15 minutes per step)
- Failure recording via the `RecordFailure` state → `agentic_data_ingestion_record_failure` Lambda (construct id keeps the legacy `FailureRecorder` name for CFN logical-id stability)

**Execution Modes**:
- **Automated**: No approval gates (development)
- **Supervised**: approval gates (production) — schema mapping, code, and forced test review, agreeing with the `waitForTaskToken` list above. The gate count and its derivation are in [System Inventory](#system-inventory)

### 3. Processing Layer

**Lambda Functions** — count and derivation: [System Inventory](#system-inventory). The groups below
enumerate every one of them, so the group counts sum to the manifest's figure. Three of the functions
listed are defined in the Orchestration stack rather than Compute (`pipeline_trigger`,
`record_failure`, `queue_loss_recorder`); the per-stack split is in
[System Inventory](#system-inventory) as well.

#### Pipeline Management (2 functions)
- `agentic_data_ingestion_run_pipeline` - Execute Glue job runs
- `agentic_data_ingestion_job_completion` - Handle job completion callbacks

#### Pipeline Trigger (1 function)
- `agentic_data_ingestion_pipeline_trigger` - SQS-driven trigger that starts Step Functions executions for uploaded/scheduled files (defined in the Orchestration stack, with pipeline-lock dedup)

#### Data Processing (4 functions)
- `agentic_data_ingestion_file_validation` - Validate uploaded files
- `agentic_data_ingestion_schema_extract` - Extract schema from data
- `agentic_data_ingestion_schema_compare` - Compare source/target schemas
- `agentic_data_ingestion_schema_mapping` - Create field mappings with AI (Bedrock Converse API, model-agnostic)

#### Code Generation & Testing (3 functions)
- `agentic_data_ingestion_create_code` - Generate PySpark code (template-first: Jinja2 scaffold + AI transform block); checks for newer mappings from reruns (P15)
- `agentic_data_ingestion_test_code` - Verify the generated transform. Two executors: a Glue interactive session (default) or the AgentCore Code Interpreter pandas proxy (PySpark→pandas conversion + sandboxed execution)
- `agentic_data_ingestion_create_quality_checks` - Generate data quality checks

#### Infrastructure (2 functions)
- `agentic_data_ingestion_create_glue_pipeline` - Create Glue jobs (Strands Agent); for JDBC targets it auto-creates the Glue JDBC Connection from the selected endpoint, including RDS/Aurora instances and clusters (P14). Credentials reach the job as a Secrets Manager ARN wherever the connection is secret-backed
- `agentic_data_ingestion_create_infrastructure` - Create S3 Tables infrastructure (direct boto3 API)

#### Approval System (3 functions)
- `agentic_data_ingestion_approval_config` - Manage approval configurations
- `agentic_data_ingestion_approval_handler` - Process approval requests
- `agentic_data_ingestion_approval_timeout_handler` - Handle approval timeouts

#### Agent Dispatch (2 functions)
- `agentic_data_ingestion_agent_dispatch` - Generic agent dispatcher and **the only route from EventBridge or the dashboard API to an AgentCore agent**. AgentCore Runtime cannot be an EventBridge target (the target list is fixed and API Destinations cannot sign SigV4), so an event-driven agent invocation is always Rule → this Lambda → AgentCore. It owns the long synchronous `bedrock-agentcore:InvokeAgentRuntime` call inside its own 15-minute timeout
- `agentic_data_ingestion_self_healing_dispatch` - Self-healing dispatcher; async-invoked (`InvocationType="Event"`) by `job_completion` on Glue job failure

#### Failure Recording (1 function)
- `agentic_data_ingestion_record_failure` - Backs the `RecordFailure` state: parses the `States.ALL` Catch payload and writes the Failure_Record (originating step, error type, message, run/pipeline ids) before the execution transitions to `PipelineFailed`. Defined in the Orchestration stack, not Compute *(P28 R1.3/R1.4)*

#### Queue Loss Recording (1 function)
- `agentic_data_ingestion_queue_loss_recorder` - Consumes `PipelineTriggerDLQ` and writes the durable Lost_File record for a file SQS gave up redelivering. It records the loss and starts nothing — redrive is deliberately out of scope. Defined in the Orchestration stack *(p54)*. Mechanism: [WORKFLOW.md § Queue admission, loss and skips](WORKFLOW.md#queue-admission-loss-and-skips)

#### Data Layer (1 function)
- `agentic_data_ingestion_execution_updater` - DynamoDB Stream handler, populates `agentic_data_ingestion_executions` summary table

#### API (1 function)
- `agentic_data_ingestion_dashboard_api` - REST API for dashboard

#### WebSocket (4 functions)
- `agentic_data_ingestion_ws_authorizer` - API Gateway WebSocket `$connect` Lambda authorizer; validates the Cognito JWT from the `?token=` query string against the User Pool JWKS (signature, issuer, expiry, token_use, and the app client against the allowed list) and returns an IAM Allow/Deny policy
- `agentic_data_ingestion_ws_connect` - Stores connection ID and pipeline subscriptions in DynamoDB on `$connect`; handles `subscribe` action on `$default` route
- `agentic_data_ingestion_ws_disconnect` - Removes all subscription records for a connection on `$disconnect`
- `agentic_data_ingestion_ws_broadcast` - Triggered by DynamoDB Streams on `agentic_data_ingestion_executions` table; queries subscribers by `pipeline_id` and pushes updates via `post_to_connection`; cleans up stale connections on `GoneException`

**Common Characteristics**:
- Runtime: Python 3.12
- Architecture: arm64
- Timeout: 300-900 seconds
- Memory: 512-1024 MB
- Layers: Strands Agents layer

### 4. AI/ML Layer

**Components**:

#### Amazon Bedrock
- **Primary Model**: Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)
- **Fallback Model**: Claude Sonnet 4.5 (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- **Cost-Effective**: Nova Pro (`us.amazon.nova-pro-v1:0`)
- **Budget**: Nova Micro (`us.amazon.nova-micro-v1:0`)

**Use Cases**:
- Schema mapping generation
- PySpark code generation
- Test case generation
- Data quality rule generation
- Worker configuration recommendations

#### Strands Agents
- **Framework**: Agentic AI with tool calling
- **Features**: Agent loop, state management, prompt templates
- **Integration**: Lambda layer deployment
- **MCP Support**: Connects to AgentCore MCP servers

#### AgentCore MCP (Focused Server Profiles)

**Architecture**: each MCP server is deployed as its own AgentCore runtime, scoped to a specific domain — one runtime per server directory, and `mcp/vendor/` is not a server. Server count and derivation: [System Inventory](#system-inventory).

**Server 1: Codegen MCP** (`mcp/codegen_server/`)
- **Tools**: ~10 Glue ETL and data catalog tools
- **Capabilities**:
  - Glue ETL job CRUD and execution
  - Data catalog read (databases, tables, versions)
  - Interactive sessions for code testing
- **Consumers**: `create_code`, `test_code` Lambdas

**Server 2: Infra MCP** (`mcp/infra_server/`)
- **Tools**: ~12 infrastructure management tools
- **Capabilities**:
  - S3 Tables CRUD, namespace management
  - Glue catalog creation, crawlers
  - Workflow and trigger management
- **Consumers**: `create_infrastructure`, `create_glue_pipeline` Lambdas

**Server 3: Analytics MCP** (`mcp/analytics_server/`)
- **Tools**: ~15 analytics and monitoring tools
- **Capabilities**:
  - Athena query execution and optimization
  - EMR cluster management
  - CloudWatch metrics and cost estimation
- **Consumers**: `create_glue_pipeline` Lambda (worker optimization)

**Server 4: S3 Tables MCP** (`mcp/s3_tables/`)
- **Tools**: 17 S3 Tables and Iceberg management tools
- **Capabilities**:
  - S3 Table Bucket creation
  - Iceberg metadata management
  - Schema evolution
  - Table optimization
  - Partition management

**Server 5: Data Processing MCP** (`mcp/data_processing/`)
- **Capabilities**:
  - PySpark transform and data-processing helpers used during code generation/testing
- **Consumers**: `create_code`, `test_code` Lambdas

**Tool Allowlist Pattern**: `strands_mcp_factory.py` supports an optional `tool_allowlist` parameter. Even if a server exposes extra tools, the Lambda only passes the allowed subset to the Strands agent — a safety net for prompt token control.

**Benefits**:
- Each Lambda loads only the tools it needs (10-15 instead of 47+)
- Reduced prompt token usage and improved tool selection accuracy
- Lower latency per agent invocation
- Independent scaling and deployment per server

**Authentication**: AWS SigV4 via streamable HTTP

### 5. Storage Layer

#### S3 Buckets

The enumeration below is the Foundation-stack set, one heading per bucket. Totals — Foundation plus
the Hosting site bucket and the CloudFront access-log bucket — and their derivations are in
[System Inventory](#system-inventory). The access-log bucket is named
`agentic-data-ingestion-cf-logs-<account>-<region>`, outside the `adi-*` family, because several
roles hold read and write on `adi-*` buckets.

1. **adi-source-files-{account}-{region}**
   - Purpose: Raw uploaded files
   - Lifecycle: 90 days retention
   - Encryption: SSE-S3

2. **adi-processed-files-{account}-{region}**
   - Purpose: Validated files
   - Lifecycle: 30 days retention
   - Encryption: SSE-S3

3. **adi-generated-code-{account}-{region}**
   - Purpose: Generated PySpark code
   - Structure: `{pipeline_id}/code/{version}/`
   - Lifecycle: Indefinite retention, transition to IA after 30 days
   - Versioning: Enabled

4. **adi-test-outputs-{account}-{region}**
   - Purpose: Test execution results
   - Structure: `{pipeline_id}/test-outputs/{execution_id}/`
   - Lifecycle: 30 days retention

5. **adi-target-schemas-{account}-{region}**
   - Purpose: User-uploaded target schemas
   - Structure: `uploaded_schemas/`
   - Lifecycle: Indefinite retention

6. **adi-templates-{account}-{region}**
   - Purpose: PySpark templates
   - Lifecycle: Indefinite retention

7. **adi-lambda-code-{account}-{region}**
   - Purpose: Lambda deployment packages
   - Lifecycle: 30 days for old versions

8. **adi-dependencies-{account}-{region}**
   - Purpose: Glue JAR dependencies (S3 Tables Iceberg runtime)
   - Lifecycle: Indefinite retention

9. **adi-checkpoints-{account}-{region}**
   - Purpose: Spark/Glue checkpoint and intermediate run state
   - Lifecycle: 7 days retention
   - Encryption: SSE-S3

#### DynamoDB Tables

See [DYNAMODB-TABLES.md](DYNAMODB-TABLES.md) for detailed schemas. The table and GSI totals, with
their derivations, are in [System Inventory](#system-inventory); the list below is one entry per
table, and the per-table GSI counts in it are local to each table rather than a total.

1. **agentic_data_ingestion_state** - Pipeline execution state (PK=`run_id`, SK=`step_name`, 3 GSIs, DynamoDB Stream)
2. **agentic_data_ingestion_pipelines** - Pipeline metadata (2 GSIs)
3. **agentic_data_ingestion_generated_code** - Generated code metadata (2 GSIs, Streams enabled)
4. **agentic_data_ingestion_test_results** - Test execution results (1 GSI, Streams enabled)
5. **agentic_data_ingestion_schema_mappings** - Reusable schema mappings (3 GSIs)
6. **agentic_data_ingestion_approvals** - Approval workflows (4 GSIs, TTL enabled)
7. **agentic_data_ingestion_notifications** - Dashboard notifications (1 GSI, TTL enabled)
8. **agentic_data_ingestion_executions** - Execution summaries, Stream-populated (2 GSIs, DynamoDB Stream for WebSocket broadcast)
9. **agentic_data_ingestion_task_tokens** - Step Functions task tokens (TTL-enabled, 7-day expiry)
10. **agentic_data_ingestion_ws_connections** - WebSocket connection subscriptions (PK=`connection_id`, SK=`pipeline_id`, `PipelineIndex` GSI on `pipeline_id`, TTL-enabled 24h)
11. **agentic_data_ingestion_errors** - Centralized error tracking (PK=`error_date`, SK=`timestamp`, TTL-enabled 90 days) *(Phase 8)*
12. **agentic_data_ingestion_pipeline_locks** - Concurrency control locks (PK=`pipeline_prefix`, TTL-enabled) *(Phase 11)*
13. **agentic_data_ingestion_audit_log** - Audit trail for pipeline operations (PK=`resource_id`, SK=`timestamp`) *(Phase 13)*
14. **agentic_data_ingestion_agent_traces** - Per-step agent reasoning/tool-call traces (PK=`run_id`, SK=`step_name`, TTL-enabled)
15. **agentic_data_ingestion_pipeline_watermarks** - JDBC incremental-source watermarks (PK=`pipeline_id`, PK-only, RETAIN) *(P17 — JDBC source)*
16. **agentic_data_ingestion_processed_files** - Processed_File_Ledger: upload-level dedup (PK=`pipeline_id`, SK=`fingerprint` — size + normalised S3 ETag; TTL-enabled 30 days; no GSI and no Stream, both deliberate). Written by `job_completion` on the Glue SUCCEEDED callback and read by `pipeline_trigger` before it starts a run, so a duplicate upload is skipped instead of reprocessed *(p30 R9.1/R9.2)*
17. **agentic_data_ingestion_model_call_budget** - Cross-Lambda per-run model-call counter backing `CallBudget` in `shared/model_config.py` (PK=`run_id`, PK-only, atomic `ADD call_count 1` per `invoke_model()`, TTL attribute `expires_at` at an 8-day window) *(P28 R15.2)*

#### EventBridge

**Event Bus**: `agentic-data-ingestion-events`

**Event Types**:
- `PipelineStateChange` - Pipeline execution status updates
- `ApprovalRequired` - Human approval needed
- `SchemaDriftDetected` - Schema changes detected
- `CodeGenerationComplete` - Code generation finished
- `TestsCompleted` - Test execution finished
- `GlueJobComplete` - Glue job execution finished

**Consumers**:
- Dashboard API (real-time updates)
- CloudWatch Logs (audit trail)
- SNS Topics (notifications)
- Lambda Functions (event processing)

### 6. Execution Layer

**AWS Glue Jobs**:
- **Job Type**: Python Shell or Spark
- **Glue Version**: 5.0
- **Python Version**: 3.12
- **Worker Types**: G.025X, G.1X, G.2X, G.4X, G.8X, Z.2X
- **Max Capacity**: 2-50 workers (AI-recommended)
- **Timeout**: 15-2880 minutes (AI-recommended)

**Job Configuration**:
- Script Location: S3 (generated-code bucket)
- IAM Role: Pipeline-specific role (least privilege)
- Default Arguments: Aligned with code requirements
- Monitoring: CloudWatch Logs and Metrics
- Job Bookmarks: Enabled (optional)

**Target Types Supported** (four in the runtime, three offered for new pipelines):
- S3 Table Buckets (Iceberg format)
- Glue Catalog tables — **no longer offered for new pipelines.** The wizard does not list it and `POST /pipelines` refuses `GLUE_CATALOG` with a 400 (`create_pipeline` in `handlers/pipelines_crud.py`). It wrote into a catalog table the user had to create first, under database-name and bucket rules, while the other three create their own destination. Existing Glue Catalog pipelines keep running: the template, job builder and IAM grants are unchanged
- JDBC databases — MySQL, PostgreSQL, MariaDB, Oracle, SQL Server and Aurora via the JDBC driver map. Selecting an RDS instance or cluster auto-creates the Glue JDBC Connection from its endpoint (P14). Credentials reach the job only as a Secrets Manager ARN, never as plaintext arguments; a JDBC connection without a `SECRET_ID` is refused
- S3 (Parquet, CSV, JSON)

`RDS_DATABASE` is a **legacy alias consolidated into `jdbc`** (P17 task 20.4), not a fifth type: it was removed from the wizard's target-type selector and is normalised to `jdbc` on read (`_LEGACY_TARGET_TYPE_ALIASES` in `handlers/schemas.py`). Persisted pipelines still carrying `RDS_DATABASE` render and generate code as JDBC.

---

## Data Flow

### End-to-End Pipeline Execution

```
1. User uploads file → S3 source bucket
   ↓
2. Dashboard creates pipeline → API Gateway → Lambda
   ↓
3. Lambda invokes Step Functions → Pipeline execution starts
   ↓
4. Step 1: File Validation
   - Validate format (JSON/CSV/Parquet)
   - Check file size and structure
   - Write state to DynamoDB
   ↓
5. Step 2: Schema Extraction
   - Parse source file
   - Extract field names and types
   - Generate source schema
   - Write state to DynamoDB
   ↓
6. Step 3: Schema Comparison
   - Compare with target schema
   - Detect schema drift
   - Identify new/changed fields
   - Write state to DynamoDB
   ↓
7. Step 4: Schema Mapping (AI-Powered)
   - Strands Agent + Bedrock Claude
   - Intelligent field matching
   - Confidence scoring
   - [APPROVAL GATE if low confidence]
   - Write state to DynamoDB
   ↓
8. Step 5: Code Generation (Template-First + AI Transform)
   - Select Jinja2 template by target_type (s3_tables, glue_catalog, jdbc, s3_parquet)
   - AI generates only the transform block (5-15 lines of PySpark)
   - Render template with transform block + pipeline metadata
   - Result: ~80 lines instead of 500
   - Extract parameter requirements
   - Store code + params in DynamoDB
   - [APPROVAL GATE if quality check fails]
   - Write state to DynamoDB
   ↓
9. Step 6: Code Testing (executor + frame mode, both set from CDK)
   - Executor `glue` (deployed default): run the transform in a Glue interactive session
     Frame mode `sample` (deployed default): 100-row read of the pipeline's own source S3 object
     Non-S3 sources (e.g. JDBC) fall back to a synthetic frame from the declared schema
   - Executor `pandas`: PySpark-to-pandas conversion via `PySpark2PandasConverter`,
     sandboxed in an AgentCore Code Interpreter session over generated test data
   - Validate output against target schema (record counts, null rates, schema compliance)
   - Write state to DynamoDB
   ↓
10. Step 7: Infrastructure Provisioning (Conditional)
    - If target_type = "NEW_S3_TABLE_BUCKET":
      - Direct boto3 s3tables API: create bucket, namespace, table
      - Idempotent create-or-get pattern (handles ConflictException)
      - Returns real ARNs from API response
    - Else (GLUE_CATALOG, JDBC, EXISTING_S3_TABLE_BUCKET):
      - Skip creation, pass through config
    - Write state to DynamoDB
   ↓
11. Step 8: Glue Job Creation (AI + Codegen + Analytics MCP)
    - Read parameter requirements from DynamoDB
    - Build parameter values from pipeline config
    - Use Codegen MCP for job configuration
    - Use Analytics MCP for worker optimization and cost estimation
    - Create Glue job with aligned parameters
    - Create pipeline-specific IAM role
    - Write state to DynamoDB
   ↓
12. Pipeline Ready
    - EventBridge event published
    - Dashboard shows completion
    - Glue job ready for execution
```

### Parameter Contract System

**Ensures Glue job parameters always match generated code requirements**:

1. **Code Generation (Step 5)**:
   - Select Jinja2 template by `target_type`
   - AI generates only the transform block (5-15 lines)
   - Render template with transform block + pipeline metadata
   - Parse script for `REQUIRED_PARAMS` and `OPTIONAL_PARAMS`
   - Store parameters in the `agentic_data_ingestion_generated_code` table
   - Create parameter contract

2. **Glue Job Creation (Step 8)**:
   - Read parameter requirements from DynamoDB
   - Build parameter values from pipeline configuration
   - Merge target-specific parameters (Iceberg, JDBC, etc.)
   - Validate all required parameters present
   - Create Glue job with aligned `DefaultArguments`

**Benefits**:
- No parameter name mismatches
- No missing required parameters
- No empty parameter values
- Automatic alignment when code requirements change
- Full audit trail of parameter requirements per version

### Unified Artifact Storage

Every step Lambda writes `output_data` as a DynamoDB Map with a standardized schema defined in `shared/artifact_schema.py`:

```python
'output_data': {
    'artifact_type': str,    # e.g., 'code_generation', 'test_results', 'schema_mapping'
    'primary_id': str,       # main artifact ID
    'status': str,           # 'success', 'failed', 'skipped'
    'summary': dict,         # step-specific summary metrics
    's3_uris': dict,         # any S3 references (code files, reports)
}
```

**Artifact types**: `file_validation`, `schema_extraction`, `schema_comparison`, `schema_mapping`, `code_generation`, `test_results`, `infrastructure`, `glue_pipeline`, `quality_checks`, `run_pipeline`.

**Storage strategy per artifact type**:
- Generated code → S3 only (`.py` file); DynamoDB stores metadata + `code_s3_uri` in `s3_uris`
- Test reports → DynamoDB inline (markdown, 2-5KB); S3 stores full JSON for archival
- All other artifacts → DynamoDB `output_data` Map directly

**Single API endpoint**: `GET /api/pipeline/{id}/run/{run_id}/artifacts` returns all artifacts for a run in one response, replacing the previous 3-5 separate API reads. The dashboard `use-code.ts` hook consumes this single endpoint.

**`approval_data` removal**: The old `approval_data` JSON string pattern is eliminated — approval-specific fields are stored directly in the `output_data` Map.

---

## AI/ML Integration

### Model Selection Strategy

**Primary Model**: Claude Sonnet 4.6
- Best quality for complex transformations
- Higher cost (~$0.015 per 1K tokens)
- Use for: Production pipelines, complex schemas

**Fallback Model**: Claude Sonnet 4.5
- Good quality, moderate cost
- Use for: Standard pipelines

**Cost-Effective**: Nova Pro
- Good quality, lower cost (~$0.0008 per 1K tokens)
- 80% cheaper than Claude
- Use for: Development, simple schemas

**Budget**: Nova Micro
- Basic quality, lowest cost
- Use for: Testing, prototyping

### Strands Agent Architecture

**Agent Loop**:
```
1. Receive task (e.g., "Generate PySpark code")
   ↓
2. Analyze requirements and available tools
   ↓
3. Select appropriate MCP tools
   ↓
4. Execute tools and gather results
   ↓
5. Synthesize results into final output
   ↓
6. Validate output quality
   ↓
7. Return result or retry if needed
```

**Tool Selection**:
- Agent autonomously selects tools from unified MCP tool list
- Can use multiple tools in sequence
- Handles tool failures gracefully
- Logs tool usage for debugging

### MCP Integration Patterns

**Pattern 1: Template-First Code Generation**
```python
# create_code Lambda uses Codegen MCP server
# 1. Select Jinja2 template by target_type (deterministic)
template = TEMPLATE_MAP[target_type]  # e.g., glue_job_s3_tables.py.j2

# 2. AI generates ONLY the transform block (5-15 lines)
transform_block = generate_transform_with_ai(field_mappings, source_schema)

# 3. Render template with transform block + pipeline metadata
final_code = template.render(transform_block=transform_block, **target_config)
# Result: ~80 lines instead of 500
```

**Pattern 2: Infrastructure Provisioning**
```python
# Direct boto3 s3tables API (no AI agent or MCP)
1. create_table_bucket(bucket_name)  # or get existing
2. create_namespace(bucket_arn, namespace)  # or get existing
3. create_table(bucket_arn, namespace, table_name)  # or get existing
4. get_table(bucket_arn, namespace, table_name)  # retrieve metadata
```

**Pattern 3: Glue Job Configuration**
```python
# Agent uses Codegen + Analytics MCP servers
1. analyze_data_characteristics(data_volume)  # Codegen MCP
2. calculate_optimal_workers(data_volume, complexity)  # Analytics MCP
3. estimate_glue_cost(worker_type, worker_count)  # Analytics MCP
```

---

## Security Architecture

### Authentication & Authorization

**Dashboard**:
- Cognito User Pool login gate (Hosted UI, Authorization Code + PKCE)
- Frontend auth via `react-oidc-context` / `oidc-client-ts`
- Served from S3 via CloudFront (OAC); runtime config from `/config.json`

**REST API Gateway**:
- Cognito User Pools authorizer (JWT validation)
- Token forwarded via CloudFront `/api/*` behavior
- CORS narrowed to CloudFront origin

**WebSocket API Gateway**:
- Lambda authorizer on `$connect` validates Cognito JWT from `?token=` query param, accepting only tokens issued to this deployment's app clients (IDs read from SSM parameters that Hosting and Auth write)
- Connections without valid token are rejected

**Edge Hardening (WAF + CloudFront-only)**:
- CLOUDFRONT-scope WAF on distribution (managed rules + rate limit), deployed in the us-east-1 `EdgeWaf` stack and attached to CloudFront via a cross-region `webAclId` reference
- REGIONAL-scope WAF on API stage blocks requests missing `X-Origin-Verify` header
- Secret header stored in Secrets Manager (rotatable)
- Direct `execute-api` access returns 403

**Lambda Functions**:
- IAM roles with least privilege
- Separate role per function
- No hardcoded credentials

**Bedrock**:
- IAM role-based access
- Model-specific permissions
- Usage tracking and quotas

**MCP**:
- AWS SigV4 authentication
- AgentCore Runtime IAM role
- Encrypted communication (TLS)

### Data Protection

**Encryption at Rest**:
- S3: SSE-S3 (AES-256)
- DynamoDB: Default encryption
- CloudWatch Logs: Encrypted

**Encryption in Transit**:
- HTTPS/TLS for all API calls
- SigV4 signed requests
- VPC endpoints (optional)

**Secrets Management**:
- Environment variables for configuration
- AWS Secrets Manager for credentials (optional)
- No secrets in code or logs

### Network Security

**VPC Deployment** (Optional):
- Lambda functions in private subnets
- NAT Gateway for internet access
- VPC endpoints for AWS services
- Security groups with restrictive rules

**S3 Bucket Policies**:
- Least privilege access
- Deny unencrypted uploads
- Restrict to specific IAM roles

**API Gateway**:
- Resource policies
- Throttling limits
- CORS configuration

### Compliance

**Audit Trail**:
- CloudWatch Logs for all operations
- DynamoDB state tracking
- EventBridge event history
- S3 access logs

**Data Retention**:
- 90 days for execution state
- 30 days for test results
- Indefinite for generated code
- Configurable lifecycle policies

---

## Scalability & Performance

### Horizontal Scaling

**Lambda Functions**:
- Auto-scaling concurrent executions
- Reserved concurrency (optional)
- Provisioned concurrency for low latency

**Step Functions**:
- Parallel pipeline executions
- No execution limit
- State machine versioning

**DynamoDB**:
- Auto-scaling capacity (provisioned mode)
- Pay-per-request (on-demand mode)
- Global tables for multi-region

**S3**:
- Unlimited storage
- Request rate optimization
- Transfer acceleration (optional)

### Performance Optimization

**Lambda**:
- arm64 architecture (20% faster, 20% cheaper)
- Right-sized memory allocation
- Connection pooling for AWS clients
- Warm start optimization

**DynamoDB**:
- GSIs for all query patterns
- Batch operations
- Consistent read only when necessary
- Caching with DAX (optional)

**S3**:
- Multipart upload for large files
- S3 Transfer Acceleration
- CloudFront for dashboard
- Lifecycle policies for cost optimization

**Glue**:
- AI-recommended worker configuration
- Job bookmarks for incremental processing
- Partition pruning
- Columnar formats (Parquet, Iceberg)

### Monitoring & Observability

For runnable query recipes — Logs Insights queries, error tracking, tracing, and monitoring commands — see [OBSERVABILITY.md](OBSERVABILITY.md); this section covers what the system emits and why.

#### Structured Logging (Phase 8)

**Every** Lambda function uses **AWS Lambda Powertools Logger** for structured JSON logging with correlation IDs (function count: [System Inventory](#system-inventory)):

```python
from aws_lambda_powertools import Logger
logger = Logger(service="agentic_data_ingestion")

def lambda_handler(event, context):
    logger.append_keys(run_id=event.get('run_id'), pipeline_id=event.get('pipeline_id'), step_name='FileValidation')
    logger.info("Processing file", extra={"bucket": bucket, "key": key})
```

Every log entry includes:
- `run_id` — correlates all log entries for a single pipeline execution across all Lambdas
- `pipeline_id` — identifies the pipeline configuration
- `step_name` — identifies which Lambda produced the log entry
- `xray_trace_id` — OTEL trace ID injected automatically by the ADOT Lambda Layer
- `timestamp`, `level`, `service`, `function_name`, `function_memory_size`

**Cross-Lambda log correlation**: Query all logs for a pipeline run using CloudWatch Logs Insights:
```
fields @timestamp, step_name, @message
| filter run_id = "run-2026-03-03-abc123"
| sort @timestamp asc
| limit 200
```

#### Distributed Tracing (Phase 8)

**ADOT Lambda Layer** provides auto-instrumentation for all boto3 calls, HTTP requests, and Lambda invocations as OTEL spans — zero code changes required.

| Component | Auto-Instrumented | Visible As |
|-----------|-------------------|------------|
| boto3 DynamoDB calls | ✅ | Child span: `DynamoDB.GetItem` |
| boto3 S3 calls | ✅ | Child span: `S3.GetObject` |
| boto3 Bedrock calls | ✅ | Child span: `BedrockRuntime.InvokeModel` |
| HTTP requests to MCP servers | ✅ | Child span: `HTTP POST bedrock-agentcore...` |
| Strands agent loops | ✅ | Child spans per tool call and LLM invocation |
| Lambda cold start | ✅ | Span attribute: `faas.coldstart=true` |

CDK configuration per Lambda:
```typescript
fn.addLayers(adotLayer);
fn.addEnvironment('AWS_LAMBDA_EXEC_WRAPPER', '/opt/otel-instrument');
fn.addEnvironment('OTEL_RESOURCE_ATTRIBUTES', `service.name=agentic-data-ingestion-${stepName}`);
fn.addEnvironment('POWERTOOLS_ENABLED', 'true');
fn.addEnvironment('POWERTOOLS_SERVICE_NAME', 'agentic_data_ingestion');
fn.addEnvironment('POWERTOOLS_LOG_LEVEL', 'INFO');
```

CloudWatch Transaction Search connects X-Ray traces with CloudWatch Logs, enabling trace-to-log correlation across all Lambdas in a pipeline run.

#### Dashboard Step Log Retrieval (Phase 8)

The step flow visualization displays actual log lines for each pipeline step. The backend queries CloudWatch Logs Insights filtered by `run_id`:

```python
def get_step_logs(step_name: str, run_id: str, limit: int = 30) -> list:
    query = f'fields @timestamp, @message | filter run_id = "{run_id}" | sort @timestamp asc | limit {limit}'
    query_id = logs_client.start_query(logGroupName=log_group, ...)['queryId']
    # Poll for results (async API, max 5s timeout)
    for _ in range(10):
        result = logs_client.get_query_results(queryId=query_id)
        if result['status'] == 'Complete':
            return [row[1]['value'] for row in result['results']]
        time.sleep(0.5)
    return []
```

**Caching**: Completed step logs are cached in the `agentic_data_ingestion_executions` DynamoDB table under `step_logs.{step_name}`. Subsequent reads serve from cache (no repeated CloudWatch queries). DynamoDB 400KB item limit is respected — logs are truncated to the last 50 lines if needed.

#### Error Tracking (Phase 8)

Failed pipeline steps are automatically captured in the `agentic_data_ingestion_errors` DynamoDB table:

| Field | Description |
|-------|-------------|
| `error_date` (PK) | Date string `YYYY-MM-DD` |
| `timestamp` (SK) | ISO timestamp + run_id for uniqueness |
| `run_id` | Pipeline execution ID |
| `pipeline_id` | Pipeline configuration ID |
| `step_name` | Which Lambda step failed |
| `error_type` | Classified category (see below) |
| `error_message` | Truncated to 1000 chars |
| `ttl` | Auto-expires after 90 days |

**Error classification** (`classify_error()` in execution_updater):
- `permission` — AccessDenied, Unauthorized
- `timeout` — Timeout, TimedOut
- `data_format` — JSONDecodeError, CSV, schema, parse
- `code_error` — SyntaxError, NameError, TypeError
- `infrastructure` — ResourceNotFound, BucketNotFound
- `throttling` — Throttling, TooManyRequests
- `unknown` — default

The dashboard Errors page (`/errors`) shows recent failures across all pipelines with date range and error type filters.

#### CloudWatch Metrics (real-time via `/api/metrics/comprehensive` endpoint)

- Lambda: Invocations, errors, duration, throttles (`AWS/Lambda` namespace)
- Step Functions: Executions, failures, duration (`AWS/States` namespace)
- DynamoDB: Consumed capacity, throttles, latency (`AWS/DynamoDB` namespace)
- Custom pipeline metrics: Pipeline runs, success rates (`Agentic Data Ingestion/Pipeline` namespace)
- Configurable time periods: 1h, 6h, 24h, 7d via query parameter
- DynamoDB-backed metrics cache with 5-minute TTL to avoid CloudWatch API limits

#### CloudWatch Dashboard (`AgenticDataIngestion-Pipeline-Monitoring-{env}`)

Created via CDK in `orchestration-stack.ts`:
- **Row 1**: Lambda invocations, errors, and duration graphs (every function — count in [System Inventory](#system-inventory))
- **Row 2**: Step Functions execution metrics (started, succeeded, failed)
- **Row 3**: DynamoDB consumed capacity
- **Row 4**: Custom `Agentic Data Ingestion/Pipeline` namespace metrics (stage duration, step success)
- Linked from the dashboard MetricsDashboardPage for deep-dive analysis

#### EMF Metrics

`POWERTOOLS_ENABLED=true` on all Lambdas enables Embedded Metric Format (EMF) via `shared/metrics_facade.py`. Custom metrics are emitted to the `Agentic Data Ingestion/Pipeline` CloudWatch namespace.

#### CloudWatch Alarms

- Lambda errors > threshold
- Step Functions failures
- DynamoDB throttling
- Glue job failures
- Cost anomalies

---

## Disaster Recovery

### Backup Strategy

**DynamoDB**:
- Point-in-Time Recovery (35 days)
- On-demand backups before major changes
- Cross-region replication (optional)

**S3**:
- Versioning enabled for critical buckets
- Cross-region replication (optional)
- Lifecycle policies for archival

**Lambda**:
- Code stored in S3
- Version management
- Alias for blue/green deployments

### Recovery Procedures

**RTO (Recovery Time Objective)**: 4 hours
**RPO (Recovery Point Objective)**: 1 hour

Both are **objectives, not demonstrated results**. No restore or failover
exercise has been run against this sample, so nothing here shows they can be
met. The scenarios below are a recovery plan listing the mechanisms available;
the cross-region items are not configured by the CDK app. Of the backups above,
what the app does configure is point-in-time recovery on the 13 retained
DynamoDB tables (`tableDefaults` in `deploy-cdk/lib/data-stack.ts`); the four
TTL-backed ephemeral tables have none.

**Failure Scenarios**:

1. **Lambda Function Failure**:
   - Automatic retry (3 attempts)
   - Fallback to previous version
   - Manual rollback if needed

2. **Step Functions Failure**:
   - Resume from last successful step
   - Replay failed step
   - Manual intervention for approval gates

3. **DynamoDB Failure**:
   - Restore from PITR
   - Switch to backup table
   - Cross-region failover (if configured)

4. **S3 Failure**:
   - Restore from versioning
   - Cross-region replication
   - Recreate from source

5. **Region Failure**:
   - Deploy to secondary region
   - Restore data from backups
   - Update DNS/endpoints

### High Availability

**Multi-AZ Deployment**:
- Lambda: Automatic multi-AZ
- DynamoDB: Automatic multi-AZ
- S3: Automatic multi-AZ
- Step Functions: Automatic multi-AZ

**Cross-Region** (Optional):
- DynamoDB Global Tables
- S3 Cross-Region Replication
- Multi-region Lambda deployment
- Route 53 health checks

---

## Cost Optimization

### Compute Costs

**Lambda**:
- arm64 architecture (20% cheaper)
- Right-sized memory allocation
- Reserved concurrency for predictable workloads
- ~$0.20 per pipeline execution

**Step Functions**:
- Standard workflow (long-running)
- ~$0.025 per execution

**Glue**:
- AI-optimized worker configuration
- Job bookmarks for incremental processing
- Spot instances (optional)
- ~$0.44-$3.52 per DPU-hour

### Storage Costs

**S3**:
- Lifecycle policies (transition to IA, Glacier)
- Intelligent-Tiering for unpredictable access
- ~$0.023 per GB/month (Standard)

**DynamoDB**:
- On-demand for low-traffic tables
- Provisioned with auto-scaling for high-traffic
- ~$1.25 per million writes

### AI/ML Costs

**Bedrock**:
- Claude Sonnet 4.6: ~$0.015 per 1K tokens
- Nova Pro: ~$0.0008 per 1K tokens (80% cheaper)
- Model selection based on use case

**Estimated Total Cost**:
- $5-20 per 100 pipeline executions
- Depends on model choice and data volume

---

## Real-Time Updates (WebSocket)

### Architecture

```
Browser (useWebSocket hook)
  │
  │  wss:// connection
  ▼
API Gateway WebSocket API (agentic-data-ingestion-ws-api)
  │
  ├── $connect   → ws-connect Lambda   → DynamoDB (agentic_data_ingestion_ws_connections)
  ├── $disconnect → ws-disconnect Lambda → DynamoDB (delete subscriptions)
  └── $default   → ws-connect Lambda   → DynamoDB (subscribe action)

DynamoDB Streams (agentic_data_ingestion_executions table)
  │
  └──▶ ws-broadcast Lambda
            │
            ├── Query agentic_data_ingestion_ws_connections by pipeline_id (PipelineIndex GSI)
            ├── Include __all__ subscribers (watching all pipelines)
            └── post_to_connection() for each subscriber
                  │
                  └── On GoneException → delete stale connection record
```

### Connection Management

Each WebSocket connection creates subscription records in `agentic_data_ingestion_ws_connections`:
- On `$connect`: one record with `pipeline_id: __all__` (default subscription)
- On `subscribe` action: replaces `__all__` with one record per pipeline ID
- On `$disconnect`: deletes all records for the connection
- TTL: 24 hours as a safety net for orphaned connections

### Broadcast Flow

1. Pipeline execution step completes → `agentic_data_ingestion_executions` table updated
2. DynamoDB Stream triggers `ws-broadcast` Lambda (batch size 10, 3 retries)
3. Broadcast Lambda queries `PipelineIndex` GSI for subscribers to the specific `pipeline_id` + `__all__`
4. Deduplicates by `connection_id` and sends JSON message via `post_to_connection`
5. Stale connections (`GoneException`) are cleaned up automatically

### Message Format

```json
{
  "type": "execution_update",
  "pipeline_id": "pipe-abc",
  "run_id": "run-123",
  "status": "RUNNING",
  "current_step": "SchemaMapping",
  "updated_at": "2026-02-23T12:00:00Z"
}
```

### WebSocket API (CDK)

Defined inline in `deploy-cdk/lib/compute-stack.ts` (there is no separate WebSocket stack):
- API Gateway WebSocket API with `$connect`, `$disconnect`, `$default` routes
- `WebSocketStage` with `autoDeploy: true` (stage name: `prod`)
- Grants `ManageConnections` permission to the broadcast Lambda
- Exports `wsApiEndpoint` for dashboard configuration

---

## Glue Job Parameter Contract System

The **Parameter Contract System** ensures that Glue job `DefaultArguments` exactly match the parameter names expected by the generated PySpark code's `getResolvedOptions`. The Jinja2 template is the single source of truth.

### Data Flow

```
Template (source of truth)
  │  defines getResolvedOptions(['source_path', 'table_bucket_arn', ...])
  ▼
Parameter Extractor (create_code Lambda)
  │  parses generated code, extracts required_parameters list
  │  stores in agentic_data_ingestion_generated_code table
  ▼
Argument Builders (create_glue_pipeline Lambda)
  │  build_<target>_arguments() emits each template param for its target type
  │  get_source_path(run_id) → queries state table for json_s3_uri
  │  get_warehouse_location(run_id, target_config) → queries infra step or S3 Tables API
  ▼
Validator (create_glue_pipeline Lambda)
  │  validate_required_parameters() checks all params present and non-empty
  ▼
Glue Job DefaultArguments
  │  --source_path, --table_bucket_arn, --namespace, etc.
  ▼
PySpark getResolvedOptions
  │  reads args at runtime — names match exactly
  ✅ No GlueArgumentError
```

### Per-Template Parameter Contract

| Template | Required Parameters |
|----------|-------------------|
| `glue_job_s3_tables.py.j2` | `source_path`, `table_bucket_arn`, `namespace`, `table_name` |
| `glue_job_glue_catalog.py.j2` | `source_path`, `database_name`, `table_name` |
| `glue_job_jdbc.py.j2` | `source_path`, `jdbc_url`, `jdbc_table`, `jdbc_driver`, `secret_arn` |
| `glue_job_s3_parquet.py.j2` | `source_path`, `output_path` |

Template selection is by `(source_type, target_type)` via `TEMPLATE_RULES` in `create_code/code_assembly.py`. The four templates above serve `S3_UPLOAD` sources; a JDBC **source** routes to a fifth template, `glue_job_jdbc_source.py.j2`, whose key list is built per render (`jdbc_url`, `jdbc_driver`, `jdbc_schema`, `jdbc_table`, `secret_arn`, `read_mode`, `pipeline_id`, `run_id`, plus incremental and `target_`-prefixed keys as applicable). Legacy `rds_database` targets map to `glue_job_jdbc.py.j2` alongside `jdbc`.

**JDBC credentials — a Secrets Manager reference is the only path.** A JDBC connection created by this system carries `SECRET_ID` in its `ConnectionProperties` (`shared/jdbc_helpers.py`). For every JDBC target, `create_glue_pipeline` passes **only `--…secret_arn`** — a reference — and the generated write block fetches the credential at job runtime via `boto3 get_secret_value` (`pyspark_templates/_partials/_write_jdbc.py.j2`). Plaintext `jdbc_user`/`jdbc_password` must never reach Glue job arguments or connection properties: job arguments are visible in the Glue console, CloudTrail, and job-run history, and this has leaked a real credential once (standing invariant).

How that holds on every path:
- Both JDBC-target templates use the same `_write_jdbc.py.j2` partial: `glue_job_jdbc.py.j2` with no prefix (declaring `secret_arn`), `glue_job_jdbc_source.py.j2` with `target_` (declaring `target_secret_arn`). Neither declares a plaintext credential key, whatever the render context.
- `job_arguments.resolve_jdbc_target_secret` resolves the reference: `target_config.secret_arn`, else the named connection's `SECRET_ID`, else a `ValueError` at Glue-job creation. It never calls `GetSecretValue`, and there is no fallback to inline credentials. The same resolver feeds the `secretsmanager:GetSecretValue` grant on the pipeline's execution role (`iam_roles.py`), so the job is granted the secret it is told to read.
- The dashboard API refuses `target_config.jdbc_user` / `jdbc_password` on create and edit, and refuses a `glue_connection` target whose connection has no `SECRET_ID`, both with a 400. See [JDBC_SOURCE.md § Target credential contract](JDBC_SOURCE.md#target-credential-contract) and [TROUBLESHOOTING.md § JDBC target secret could not be resolved](TROUBLESHOOTING.md#jdbc-target-secret-could-not-be-resolved).

All parameter names use **lowercase_snake_case**. `JOB_NAME` is excluded — Glue provides it automatically.

### Key Functions (create_glue_pipeline Lambda)

| Function | Purpose |
|----------|---------|
| `get_source_path(run_id)` | Queries state table for `file_validation` step's `json_s3_uri` |
| `get_warehouse_location(run_id, target_config)` | Resolves from `CreateInfrastructure` step output, S3 Tables API, or `target_config` |
| `validate_required_parameters(default_args, required_params)` | Validates all required params are present and non-empty before creating the Glue job |
| `build_s3_table_bucket_arguments()` | Builds `DefaultArguments` for S3 Tables (Iceberg) targets |
| `build_glue_catalog_arguments()` | Builds `DefaultArguments` for Glue Catalog targets |
| `build_jdbc_arguments()` | Builds `DefaultArguments` for JDBC targets, including the `--…secret_arn` reference; never a plaintext credential |
| `resolve_jdbc_target_secret(target_config)` | Returns the JDBC target's Secrets Manager reference (`secret_arn`, else the connection's `SECRET_ID`) or raises; shared by the job argument and the IAM grant |
| `build_s3_parquet_arguments()` | Builds `DefaultArguments` for S3 Parquet targets |

### source_path Resolution

The processed source file S3 URI is stored by the `file_validation` Lambda in the state table:

```python
# State table key: {run_id, step_name: 'file_validation'}
# Field: json_s3_uri (top-level)
# Example: s3://adi-processed-files-dev/{pipeline_id}/filename.json
source_path = get_source_path(run_id)  # queries state table
```

### Iceberg Catalog Config and warehouse_location (S3 Tables only)

The S3 Tables Iceberg catalog uses `table_bucket_arn` (not `warehouse_location`) for its `warehouse` config:
```python
spark.conf.set("spark.sql.catalog.s3tablesbucket.warehouse", args['table_bucket_arn'])
```

`warehouse_location` is still resolved for operational metadata but is not in `getResolvedOptions`.

Resolution order for `warehouse_location`:
1. `CreateInfrastructure` step output in state table (`output_data.s3_table_bucket.warehouse_location`)
2. S3 Tables API `get_table()` → `warehouseLocation` field (for existing buckets)
3. `target_config.warehouse_location` fallback

**Step ordering**: `CreateInfrastructure` must run before `CreateGluePipeline` in the Step Functions state machine so infrastructure output is available.

### Field Mappings in Code Generation

The `create_code` Lambda extracts field mappings from the `mapping_result` JSON string stored by the schema mapping Lambda. The mapping structure is `{"mappings": [...], "unmapped_source_fields": [...]}`. These mappings drive the AI-generated transform block — without them, the code defaults to a passthrough (`df_transformed = df`).

### Contract Tests

`tests/contracts/api_responses/test_glue_parameter_contract.py` validates the contract for the four `S3_UPLOAD` templates: renders each with mock data, extracts `getResolvedOptions` params, builds mock `DefaultArguments`, and verifies all required params are present and non-empty.

---

## Infrastructure Deployment

All infrastructure is deployed via AWS CDK v2 (TypeScript). See `deploy-cdk/` for the 8 stack definitions:
- Foundation (IAM, ECR, S3)
- Data (DynamoDB tables — count in [System Inventory](#system-inventory) — EventBridge, DynamoDB Streams)
- Auth (Cognito User Pool + Hosted UI domain)
- AgentCore (MCP runtimes)
- Compute (Lambda — 22 of the 25 functions, API Gateway with Cognito authorizer, WebSocket API + `$connect` Lambda authorizer defined inline, regional WAF on the API stage, Stream event sources)
- Orchestration (Step Functions with type-safe CDK constructs, the remaining 3 asset-sourced Lambda functions — `pipeline_trigger`, `record_failure` and `queue_loss_recorder` — EventBridge rules, CloudWatch Dashboard and Alarms)
- EdgeWaf (CLOUDFRONT-scope WAF, pinned to us-east-1 and wired into Hosting via cross-region references)
- Hosting (private S3 + CloudFront with OAC, Cognito app client, runtime `config.json`)

An optional `TestFixtures` stack (MySQL/Postgres JDBC test databases) is gated behind
`-c adi:deployTestFixtures=true` and is not part of the default `cdk deploy --all`.

> **Counting rule for the Orchestration Lambdas — never quote the 3 without it.** A bare
> "the Orchestration stack deploys 3 Lambdas" is false. `orchestration-stack.ts` creates
> **7** `new lambda.Function`s. Three are **asset-sourced** against a directory under
> `lambda/`, and those are the three named above: `FailureRecorderFunction` (`:150`, asset
> `:160`), `PipelineTrigger` (`:1657`, asset `:1662`) and `QueueLossRecorderFunction`
> (`:1769`, asset `:1785`). The other four are **inline** and have no directory of their
> own: `LockReleaseFunction` (`:185`, a direct `lambda.Code.fromInline`) plus
> `ApprovalStatusUpdateFunction` (`:1190`), `MCPMetricsUpdateFunction` (`:1244`) and
> `GlueJobStateUpdateFunction` (`:1290`), which go through the `inlineLambdaCode` helper at
> `:1163`. It is the asset-sourced three that reconcile with the directory count:
> **22 Compute + 3 Orchestration = 25 directories under `lambda/`**. Note also that
> `grep -c "lambda.Code.fromAsset"` over this file returns **4**, not 3 — the fourth
> (`:1604`) is the `PowertoolsTriggerLayer` `LayerVersion`, not a Lambda function — which is
> why the [System Inventory](#system-inventory) row for this figure keys on the
> asset-sourced handler filename instead.

### Type-Safe Step Functions (Phase 5)

The state machine in `orchestration-stack.ts` uses CDK Step Functions construct library instead of raw JSON:

```typescript
// Lambda steps use tasks.LambdaInvoke with typed payloads
const fileValidation = new tasks.LambdaInvoke(this, 'FileValidation', {
  lambdaFunction: compute.fileValidationLambda,
  resultPath: '$.file_validation_result',
  payloadResponseOnly: true,
  retryOnServiceExceptions: true,
});

// Choice states use sfn.Condition for type-safe branching
const checkSchemaAction = new sfn.Choice(this, 'CheckSchemaAction')
  .when(sfn.Condition.stringEquals('$.schema_compare_result.action', 'use_existing_pipeline'), reuseMapping)
  .otherwise(schemaMapping);

// waitForTaskToken uses sfn.CustomState (no native CDK construct)
const approvalGate = new sfn.CustomState(this, 'ApprovalGate', {
  stateJson: {
    Type: 'Task',
    Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
    Parameters: { /* ... */ },
    ResultPath: '$.approval_result',
  },
});
```

### Real CloudWatch Metrics Endpoint (Phase 5)

The `/api/metrics/comprehensive` endpoint in `handlers/metrics.py` queries CloudWatch Metrics API directly:
- `_get_lambda_metrics()` — invocation counts, error rates, duration percentiles
- `_get_sfn_metrics()` — execution counts, success/failure rates, duration
- `_get_dynamodb_metrics()` — consumed read/write capacity
- `_get_custom_pipeline_metrics()` — custom `Agentic Data Ingestion/Pipeline` namespace metrics

Metrics are cached in DynamoDB with a 5-minute TTL (`cache_key: metrics_{period}`) to avoid CloudWatch API rate limits.


---

## Testing Architecture

### Test Hierarchy

```
tests/                          # layered suite (unit, contracts, integration, cdk, e2e)
├── unit/                       # isolated Lambda handler tests
│   ├── lambdas/                # One subdir per Lambda handler
│   ├── shared/                 # Shared utility tests
│   ├── templates/              # PySpark template tests
│   └── *.py                    # cross-layer meta-tests at the unit/ root (see `make test-meta`)
├── contracts/                  # API & event schema validation
│   ├── api_responses/          # JSON Schema validation for API endpoints
│   └── event_schemas/          # Event payload validation
├── integration/                # multi-Lambda flows (moto)
│   ├── pipeline_flow/          # Trigger → SFN → completion flows
│   ├── approval_flow/          # Approval lifecycle tests
│   ├── concurrency/            # Deduplication and concurrent access
│   ├── performance/            # N+1 query regression detection
│   ├── migration/              # Schema migration safety
│   ├── websocket/              # WebSocket broadcast path
│   └── multi_format/           # CSV/JSON/Parquet format handling
├── e2e/                        # real-AWS tests, excluded by default — need deployed infrastructure
├── fixtures/                   # Shared test data and fixtures
│   ├── conftest.py             # Moto/real-AWS toggle
│   ├── data/                   # Canonical CSV/JSON/Parquet files
│   ├── dynamodb/               # DynamoDB item snapshots
│   ├── api_responses/          # Expected API response JSON
│   ├── events/                 # Sample event payloads
│   └── golden_scripts/         # Golden PySpark rendered scripts
└── cdk/                        # CDK assertion tests (Python/pytest)
```

The TypeScript CDK assertions are separate, in `deploy-cdk/test/`, and run via `make test-cdk` (vitest). `tests/cdk/` is Python and runs under pytest.

> **No test count, file count or wall clock appears in this section, or anywhere in the
> documentation.** [TESTING.md](TESTING.md) carries the command that produces each figure instead of
> the figure, because every such number measures one tree at one moment. Run the command rather than
> quoting a copy. Two stated test counts once sat in two documents disagreeing with each other,
> neither having been re-measured, which is the drift a second literal invites.

### Shared Fixtures (Moto/Real-AWS Toggle)

`tests/fixtures/conftest.py` provides shared pytest fixtures that switch between moto (in-memory) and real AWS based on the `AGENTIC_DATA_INGESTION_TEST_REAL_AWS` environment variable. Default is moto — no AWS credentials needed. Real-AWS mode prefixes resources with `test-{uuid[:8]}-` for isolation and cleans up in fixture teardown.

### Contract Tests (JSON Schema Validation)

Contract tests validate API responses and event payloads against JSON Schemas derived from TypeScript interfaces. This catches drift between the Python Lambda outputs and the React dashboard's expected types. Schemas live in `tests/contracts/` and are validated with `jsonschema`.

### Property Tests (Hypothesis)

Property tests use hypothesis, are named `test_*_prop.py`, and live in the layer directory of the code under test. They verify correctness properties rather than specific examples:
- Schema round-trip invariants (extract → compare → mapping preserves fields)
- Parameter builder contracts (required params always present, no empty values)
- Artifact schema validation (all artifact types conform to `artifact_schema.py`)
- Idempotency checks (repeated calls produce identical results)
- Rerun-mapping invariants and mapping context round-trips (P15)

### CI/CD Test Tiers

→ **Authority:** [TESTING.md](TESTING.md) § **Test Tiers — pick the tier by kind of change**. It
holds the tier table, the command for each tier, the code-to-test mapping a scoped run needs, and
the list of gates that exit non-zero on a clean tree with the invocation that establishes each. A
second tier table here would be the drift D1's ownership rule exists to prevent, so this section
carries no commands, no timings and no counts.

What matters architecturally, and is not a measurement: **`make test-fast` must run alone**, because
one meta-test writes stub test files into all five layer directories for the duration of its run and
a second concurrent pytest process observing that window fails spuriously. The Authority states the
mechanism, the window and which targets carry the rule; do not re-derive it here.

Each layer runs in a separate pytest process to avoid `sys.modules` caching issues between
moto-patched unit tests and integration tests. The Makefile targets handle this automatically.


---

## Feature Architecture by Phase

The sections below document individual features by the delivery phase that introduced them.
The phase numbers are historical labels, not a current structure, and nothing in the system
reads them. Start from the architecture sections above; come here for the detail of one
feature. Each section's own heading text is unchanged from when it sat at the top level, so
existing links into them still resolve.

---

### Native Format Passthrough (Phase 9)

Phase 9 removes the ~400MB file size ceiling and adds support for Parquet, Avro, and ORC files.

#### File Validation — Dual-Path Architecture

The file validation Lambda now has two execution paths based on file format and size:

```
S3 PUT → file_validation Lambda:
  1. s3_client.head_object(bucket, key)     # Size check only — O(1), no file read
  2. detect_format(key)                      # Extension-based format detection
  3. IF format in {parquet, avro, orc}:
       → Lightweight path: return source_s3_uri + source_format (no conversion)
     ELIF file_size < MAX_CONVERSION_SIZE (50MB):
       → Legacy path: read + convert to JSON + write (existing behavior)
     ELSE:
       → Lightweight path: return source_s3_uri + source_format (Glue reads directly)
```

**Lightweight path** (Parquet/Avro/ORC, or any file > 50MB): HEAD request only. No file content is read into memory. Returns `source_s3_uri` pointing to the original file and `source_format` indicating the detected format. The Glue job reads the file natively.

**Legacy path** (CSV/JSON/TSV under 50MB): Existing read → convert → write JSON behavior. Preserves backward compatibility and dashboard preview functionality.

#### Format Detection

`detect_format(key)` uses extension-based detection with a `FORMAT_MAP` supporting: csv, tsv, txt, json, jsonl, ndjson, parquet, pq, avro, orc, xlsx. Compressed variants (.gz, .bz2, .snappy, .zst) are handled by stripping the compression extension and detecting the underlying format.

`csv`, `tsv` and `txt` all resolve to the canonical format `csv`, because the field delimiter is not part of the canonical format. `FORMAT_DELIMITERS` in `shared/format_delimiters.py` is the one definition of it, keyed on the **extension** rather than the canonical format for exactly that reason: `,` for csv, a tab for tsv, `|` for txt.

`xlsx` resolves to the canonical format `excel`, and `excel` is the one canonical format that is **not** in `NATIVE_FORMATS` — the set of canonical formats a Glue job reads directly from the source object with no prior conversion of that object (defined in `file_validation/lambda_function.py`; the members are csv, json, parquet, avro, orc). So the passthrough described in this section does not apply to it: `schema_extract` converts the workbook's selected worksheet to CSV and reports the converted object as `readable_source_uri`, with `readable_source_format` beside it. `file_validation` still only HEADs, on every format including `.xlsx`. A multi-sheet workbook requires a `selected_worksheet` on the pipeline or the run fails naming every worksheet present. See [LAMBDA-FUNCTIONS.md](LAMBDA-FUNCTIONS.md) for the mechanism, including the limitation that the selection is write-only on the API today.

#### `source_format` Propagation

`source_format` flows through the Step Functions payload:

```
file_validation → $.file_validation_result.source_format
  → schema_extract (routes to format-specific extractor)
  → create_code (passes to Jinja2 template renderer)
  → Glue job (PySpark template reads source file in the correct format)
```

**Every** PySpark template has `source_format` conditional read blocks for csv, parquet, avro and orc, with json as the `{% else %}` arm — so a rendered job reads its source in the declared format, and the else arm is reached only when `source_format` never arrived. The template count and its derivation are in [System Inventory](#system-inventory).

#### Environment Variables

| Variable | Lambda | Default | Purpose |
|----------|--------|---------|---------|
| `MAX_CONVERSION_SIZE_BYTES` | file_validation | `52428800` (50MB) | Threshold for legacy conversion vs lightweight passthrough |

---

### Streaming Schema Extraction (Phase 9)

Schema extraction no longer loads entire files into memory. Each format has a dedicated extraction method:

| Format | Method | Memory Usage | How It Works |
|--------|--------|-------------|--------------|
| CSV | Range GET | O(sample_bytes) | `Range: bytes=0-65535` — reads first 64KB, `csv.DictReader` splits it on the delimiter [`delimiter_for_key`](../shared/format_delimiters.py) derives from the object's extension. Also serves `tsv` and `txt`, which share the `csv` canonical format |
| JSON | Range GET | O(sample_bytes) | `Range: bytes=0-65535` — reads first 64KB, parses complete JSON records |
| Parquet | PyArrow metadata | O(metadata) | Reads Parquet footer only — zero data rows scanned, over a seekable `pyarrow.fs.S3FileSystem` handle |
| Avro | Range GET | O(sample_bytes) | `Range: bytes=0-65535` — reads the Object Container File **header**, whose metadata map carries the writer schema. The header parser is hand-written; `fastavro` does not fit the layer budget |
| ORC | PyArrow metadata | O(metadata) | Reads the ORC footer only, over a seekable `pyarrow.fs.S3FileSystem` handle — ORC keeps its schema at the **end** of the file, so a forward-only Range GET cannot reach it |

**The CSV row used to read "S3 Select", with the Range GET as a fallback.** That arm was removed, and it must not return as a fast path: [Amazon S3 Select has been closed to new customers since 2024-07-25](https://aws.amazon.com/blogs/storage/how-to-optimize-querying-your-data-in-amazon-s3/), so in any account created after that date it answers `MethodNotAllowed` and the fallback did all the work. Verified before removal rather than assumed — a direct `SelectObjectContent` against a real object in the primary dev account returns `MethodNotAllowed`, and both generations of the `schema_extract` log group hold **zero** `CSV S3 Select: parsed` success lines against many `S3 Select failed` warnings. It had never once succeeded, so removing it changed no behaviour and removed a guaranteed-failed API round-trip per extraction.

No extractor downloads the object and none of them reads data rows, so "the file has no records" is not a cause of extraction failure. The Avro and ORC extractors **raise** rather than returning an empty schema: an unreadable container fails the run at `schema_extract` instead of reporting success with zero fields. Troubleshooting for both is in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#avroorc-schema-extraction).

#### Multi-Record Type Inference

`infer_schema_from_records()` samples up to 100 records and resolves type conflicts:
- If all records agree on a type → use that type
- If `string` appears in any record → resolve to `string` (string wins)
- If `number` and `integer` conflict → resolve to `number`
- If field is `null` in all records → resolve to `string`

#### PyArrow Lambda Layer

PyArrow cannot fit in the shared Strands layer. A dedicated PyArrow layer is attached only to the schema_extract Lambda, which carries **exactly two** layers:

```
schema_extract Lambda layers:
  1. Strands Layer (agents, MCP, Powertools)
  2. PyArrow Layer (pyarrow only — schema_extract only)
```

`s3fs` is **not** in the layer — `compute-stack.ts:220-221` excludes it "to save ~60MB", and Parquet and ORC footer reads use `pyarrow.fs.S3FileSystem` instead. The ADOT layer is excluded from this one function (`compute-stack.ts:312` — *"combined ADOT+Strands+PyArrow exceeds 250MB limit"*); schema extraction gets structured logging via the Powertools Logger instead of OTEL spans.

The 250 MB combined-unzipped limit counts function code plus every attached layer, and it is binding here. Measured: Strands 123.82 MiB + PyArrow 123.98 MiB + 0.06 MiB of function code = **247.86 MiB of 250 MiB, leaving 2.14 MiB** — under 1% of the limit. Restoring ADOT adds roughly 50 MB, so `CreateFunction` refuses and the **deploy** fails; there is no graceful degradation at runtime. `deploy-cdk/test/p46-schema-extract-layers.test.ts` asserts the attachment is exactly `[strandsLayer, pyarrowLayer]`, in that order, with ADOT absent.

2 of 5 Lambda layer slots used, but the headroom, not the slot count, is what constrains this function.

#### Backward Compatibility

When `source_format` is not in the event (old file_validation Lambda version), schema extraction falls back to the existing `read_json_from_s3()` + `extract_json_schema()` path.

---

### Partition Support for All Target Types (Phase 9)

Partition columns are now configurable for S3 Tables (Iceberg), Glue Catalog, and S3 Parquet targets.

| Target Type | Template | Partition Mechanism |
|-------------|----------|-------------------|
| S3 Parquet | `glue_job_s3_parquet.py.j2` | `df.write.partitionBy(columns).parquet(path)` *(existing)* |
| S3 Tables (Iceberg) | `glue_job_s3_tables.py.j2` | `df.writeTo(table).partitionedBy(columns).createOrReplace()` *(Phase 9)* |
| Glue Catalog | `glue_job_glue_catalog.py.j2` | `df.repartition(columns)` before DynamicFrame conversion *(Phase 9)* |
| JDBC | `glue_job_jdbc.py.j2` | N/A — JDBC doesn't support partitioning |

#### Configuration Flow

1. `partition_columns` is stored in the pipeline's `target_config` in DynamoDB as a comma-separated string (`"region,dt"`). Create and edit accept that string or a list, refuse any entry that is not an identifier (`^[A-Za-z_][A-Za-z0-9_]*$`) with a 400, and store the string
2. `create_code` Lambda validates the names again and passes them to the render as string literals (`partition_columns='"region", "dt"'`), so the template emits `partitionBy("region", "dt")`. The stored text is never emitted as code
3. `create_glue_pipeline` Lambda adds `--partition_columns` to Glue job `DefaultArguments`
4. PySpark template uses `{% if partition_columns %}` to conditionally add partition logic

#### Coexistence with `--partition-keys`

`--partition_columns` (for PySpark `partitionBy()`) coexists with the existing `--partition-keys` (for Glue Catalog partition management). They serve different purposes and are both present in `DefaultArguments` when configured.


---

### Write Mode Configuration (Phase 10)

Phase 10 adds configurable write semantics to prevent data loss on every pipeline run. Previously, all templates used `createOrReplace()` (Iceberg) or `mode("overwrite")`, destroying existing data on each execution.

#### Write Mode Options

| Target Type | `overwrite` | `append` | `merge` |
|-------------|:-----------:|:--------:|:-------:|
| S3 Tables (Iceberg) | ✅ `createOrReplace()` | ✅ `.append()` | ✅ `MERGE INTO` |
| S3 Parquet | ✅ `mode("overwrite")` | ✅ `mode("append")` | ❌ Not supported |
| Glue Catalog | ✅ Default write | ✅ `enableUpdateCatalog` | ❌ Not supported |
| JDBC | ✅ `mode="overwrite"` (default) | ✅ `mode="append"` | ❌ Not supported |

- `overwrite` (default): Replaces all existing data. Backward compatible with pre-Phase 10 behavior.
- `append`: Adds new records without touching existing data. Row count after run = previous count + new records.
- `merge`: Iceberg-only. Upserts by `merge_key` — matching keys are updated, new keys are inserted. Uses Spark SQL `MERGE INTO`.

#### Merge Key Configuration

When `write_mode=merge`, the `merge_key` field specifies which column to match on:

```json
{
  "target_config": {
    "write_mode": "merge",
    "merge_key": "customer_id"
  }
}
```

- Default merge key: `id`
- The dashboard API validates that `merge_key` is required when `write_mode=merge`
- `write_mode=merge` is refused for every target except S3 Tables, at three points that share one
  policy (`shared/write_modes.py`): `create_pipeline` and `edit_pipeline` return 400 before any
  write, and code generation fails the run's `create_code` step with the reason. Edit checks the
  stored target type merged with the submitted keys, because `target_config` is merged per key.
  The Edit Pipeline dialog offers only the modes the target supports.
- Until 2026-10-10 none of these refusals existed for edit or generation, and create accepted
  `merge` for every target. The non-Iceberg writers branch on one value and send anything else to
  their `else` arm, so a stored `merge` ran as `append` on JDBC, as `overwrite` on S3 Parquet
  (replacing all existing data each run) and as the plain catalog write on Glue Catalog, and each
  run reported success. A pipeline stored that way now fails at `create_code` with a message to
  pick `append` or `overwrite`.

#### Write Mode Parameter Flow

`write_mode` and `merge_key` are **Jinja2 render-time variables**, not Glue runtime args. The template bakes the correct write logic into the generated PySpark code at code-generation time.

```
Pipeline Config (DynamoDB)
  │  target_config.write_mode, target_config.merge_key
  ▼
create_code Lambda
  │  template_vars['write_mode'] = target_config.get('write_mode', 'overwrite')
  │  template_vars['merge_key'] = target_config.get('merge_key', 'id')
  ▼
Jinja2 Template Rendering
  │  {% if write_mode == 'merge' %} → MERGE INTO SQL
  │  {% elif write_mode == 'append' %} → .append()
  │  {% else %} → createOrReplace()
  ▼
Generated PySpark Code (write logic baked in)
  ▼
create_glue_pipeline Lambda
  │  --write_mode and --merge_key added to DefaultArguments
  │  (operational metadata/logging only — not used by PySpark at runtime)
  ▼
Glue Job Execution
```

#### Template Defaults (StrictUndefined Safety)

Two layers of defense against `StrictUndefined` errors for existing pipelines:

1. **Lambda layer**: `create_code` injects defaults before rendering (`write_mode='overwrite'`, `merge_key='id'`)
2. **Template layer**: Each template includes `{% set write_mode = write_mode or 'overwrite' %}` as a safety net

---

### Multi-File Ingestion (Phase 10)

Phase 10 adds prefix-based batch processing alongside the existing single-file trigger. Instead of N pipeline executions for N files at N × $0.15, a single Glue job processes all files in a prefix for $0.15.

#### Trigger Modes

| Mode | Trigger | Files Processed | Cost |
|------|---------|----------------|------|
| `single_file` (default) | S3 PUT event | 1 file | $0.15/file |
| `prefix` | Dashboard API or scheduled | All files in prefix | $0.15/batch |

#### Prefix Trigger Flow

```
POST /api/pipelines/{id}/trigger-batch
  Body: { "prefix": "customer-data/2026-03-03/" }
  │
  ▼
Dashboard API Lambda (trigger_batch handler)
  │  Starts Step Functions execution with trigger_mode='prefix'
  ▼
File Validation Lambda
  │  trigger_mode == 'prefix' → handle_prefix_trigger()
  │  list_s3_files(bucket, prefix) — paginated list_objects_v2
  │  Validates first file format via detect_format()
  │  Returns: source_s3_uri (prefix), file_count, file_size_bytes
  ▼
Schema Extract Lambda
  │  source_s3_uri ends with '/' → prefix mode
  │  Lists first file in prefix, extracts schema from that file
  ▼
Remaining Pipeline Steps (unchanged)
  │  Glue job reads all files in the prefix via source_path
  ▼
Single Glue Job processes all files
```

#### Key Functions

| Function | Location | Purpose |
|----------|----------|---------|
| `list_s3_files(bucket, prefix)` | file_validation Lambda | Paginated `list_objects_v2`, handles >1000 files |
| `handle_prefix_trigger(event, context)` | file_validation Lambda | Validates prefix, returns aggregated metadata |
| `trigger_batch(event)` | dashboard_api pipelines handler | Starts Step Functions execution with prefix trigger |

#### Batch Trigger API

```
POST /api/pipelines/{id}/trigger-batch
Content-Type: application/json

{
  "prefix": "customer-data/2026-03-03/"
}

Response: { "execution_arn": "arn:aws:states:...", "status": "RUNNING" }
```

#### IAM Permissions

`s3:ListBucket` is already granted on source and processed buckets for both `file_validation` and `schema_extract` IAM roles. No additional IAM changes were needed.


---

### Operational Maturity (Phase 11)

#### Concurrency Control

```
S3 PUT → EventBridge (existing rule) → SQS Queue → Trigger Lambda → Step Functions
                                           ↓ (after 3 retries)
                                       Dead Letter Queue
```

The S3 upload trigger path changed from direct Step Functions invocation to an SQS-buffered flow. The existing EventBridge rule (`S3UploadTriggerRule`) now targets an SQS queue instead of the state machine directly.

**SQS Queue** (`agentic-data-ingestion-pipeline-triggers-{env}`):
- Visibility timeout: 120 seconds (allows Lambda retry via SQS)
- Retention: 7 days
- DLQ: `maxReceiveCount: 3` — failed events land in the dead letter queue after 3 attempts

**Trigger Lambda** (`agentic_data_ingestion_pipeline_trigger`):
- `reservedConcurrentExecutions: 5` — limits parallel SQS consumers
- SQS event source: `batchSize: 1`, `maxConcurrency: 5`
- Layers: ADOT only (no Strands/MCP — keeps cold start under 1 second)
- Logic: checks `sfn_client.list_executions(statusFilter='RUNNING')` count against `MAX_CONCURRENT_EXECUTIONS` (default 10). If at limit, raises an exception — SQS retries after visibility timeout.

**Coexistence with batch triggers**: The dashboard API's `trigger_batch()` calls `sfn_client.start_execution()` directly, bypassing SQS. User-initiated batch triggers are not subject to the concurrency queue.

#### Worker Sizing

A baseline sizing function provides data-volume-aware defaults for Glue job configuration.

**`get_baseline_worker_config(file_size_bytes, file_count=1)`**:

| Total Data Volume | Worker Type | Workers | Timeout (min) |
|-------------------|-------------|---------|---------------|
| < 1 GB            | G.1X        | 2       | 30            |
| 1–10 GB           | G.1X        | 5       | 60            |
| 10–50 GB          | G.2X        | 10      | 90            |
| > 50 GB           | G.2X        | 20      | 120           |

**Data flow**: Step Functions passes `file_size_bytes` and `file_count` from the `file_validation` result to the `CreateGluePipeline` payload. The `create_glue_pipeline` Lambda calls `get_baseline_worker_config()` and uses the result as the default before the Strands agent recommendation.

**3-tier fallback**: Agent recommendation → baseline config → hardcoded defaults (G.1X/2/60min).

#### Checkpoint/Resume

**Checkpoint Bucket** (`adi-checkpoints-{account}-{region}`):
- 7-day lifecycle policy — checkpoint data auto-expires
- Added to the per-pipeline Glue IAM role via `create_pipeline_specific_role()`

**Glue Job Arguments** (when checkpoints enabled):
- `--checkpoint_bucket`: S3 bucket name (from `CHECKPOINT_BUCKET` env var)
- `--checkpoint_enabled=true`: Enables checkpoint logic in templates

**PySpark Template Integration**: **every** Jinja2 template includes optional checkpoint support (template count: [System Inventory](#system-inventory)):
```jinja2
{% if checkpoint_enabled %}
    checkpoint_path = f"s3://{checkpoint_bucket}/{JOB_NAME}/{run_id}/"
    try:
        df_transformed = spark.read.parquet(checkpoint_path)
    except Exception:
        # Run transform block, then write checkpoint
        df_transformed.write.mode("overwrite").parquet(checkpoint_path)
{% else %}
    # Standard transform block
{% endif %}
```

**Job Bookmarks**: Already enabled by default in `parameter_builder.py` (`--job-bookmark-option: job-bookmark-enable`). Phase 11 does not duplicate this setting.


---

### Phase 12: AgentCore Platform Integration

Phase 12 replaces fragile custom implementations with managed AgentCore platform services across three optimizations.

#### Optimization 1: Code Interpreter for Testing

**Before**: The test Lambda used MCP → Glue Interactive Sessions to execute code. This was broken — tools returned generic responses with all metrics at 0. Each test required 7 separate MCP connections and 2-5 minutes for Glue session startup.

**After**: The test Lambda uses AgentCore Code Interpreter via `boto3.client('bedrock-agentcore')`. One session, three invocations (generate data → execute transform → validate), ~10-15 seconds total.

> **This section records what Phase 12 changed, and a later phase reversed the default it
> established. Read it as history.** Both stacks now publish `VERIFICATION_EXECUTOR: 'glue'`
> (`compute-stack.ts:724`, `agentcore-stack.ts:170`), so the live executor is a **Glue interactive
> session** and the Code Interpreter path described below is the fallback. The table at the end of
> this section already states `glue`; this paragraph is why the two once disagreed. The reversal is
> deliberate and reasoned at both CDK sites and at
> `lambda/agentic_data_ingestion_test_code/glue_session_runner.py:52-64`. What Phase 12 built is
> still real and still reachable; what changed is which branch runs by default.

```
lambda_handler()
  ├── Phase 1: Static Analysis (Strands Agent → Bedrock) — unchanged
  └── Phase 2: Code Interpreter Execution — new
        ├── PySpark2PandasConverter.convert(transform_block)
        ├── start_code_interpreter_session()
        ├── invoke: generate test data from source_schema
        ├── invoke: execute converted pandas transform
        ├── invoke: validate output against target_schema
        └── stop_code_interpreter_session() (always, in finally block)
```

Key components:
- `pyspark_to_pandas.py` — PySpark→pandas conversion. The block is parsed once with `ast.parse` and each statement's call chain is translated; conversion is all-or-nothing, so a construct with no single-frame pandas equivalent raises `ConversionError` naming the construct and the line, and nothing is commented out, dropped or partially translated. The accepted statement shapes and the converter's own construct vocabulary are in [`pyspark_templates/README.md`](../pyspark_templates/README.md#what-the-pandas-proxy-converts)
- `code_interpreter_runner.py` — Session lifecycle management with graceful fallback. A conversion refusal is classified as *not verified by the pandas proxy* rather than as a failed execution: `classify_conversion_refusal` splits `permanently_inexpressible` (nothing to fix) from `not_yet_expressible` (a converter defect)
- Test reports now show real metrics: record counts, null rates, schema compliance, quality scores

What runs in the Code Interpreter is a **proxy** for the Glue job, not the Glue job — pandas semantics over generated sample data. A transform that converts and executes cleanly has not been run against Spark.

**The pandas proxy is no longer the only executor.** Two independent dimensions now govern verification, each set from CDK in **both** `compute-stack.ts` and `agentcore-stack.ts` (a value set only on the Lambda is inert, because the handler dispatches to the AgentCore container when `USE_AGENTCORE_RUNTIME` is `'true'`. Setting only `compute-stack.ts` was rejected for exactly that reason: it leaves every test green while the live path ignores the flag, which is why a CDK assertion covers both stacks):

| Dimension | Env var | Deployed value | Meaning |
|---|---|---|---|
| Executor | `VERIFICATION_EXECUTOR` | `glue` | Run the transform in a Glue interactive session (real Spark) rather than the pandas proxy |
| Frame mode | `VERIFICATION_FRAME_MODE` | `sample` | Build the test frame from a bounded 100-row read of the pipeline's **own source S3 object**, rather than synthesising rows from the declared schema *(p44)* |

A sample frame is only attempted when the run's `source_data_uri` is an `s3://` object URI of a recognised format; anything else — notably a JDBC source, where a verification session holds no database credentials — falls back to a synthetic frame, and this is structural rather than incidental: JDBC credentials are only ever referenced by a Secrets Manager ARN, and a verification session holds no access to resolve one. A failed or timed-out source read is reported as a **harness non-verification** with `not_verified_reason: source_read_failed`, not as a transform failure: the session existed and was billed, and the object or the session's read access is what needs checking. See [WORKFLOW.md](WORKFLOW.md) for the full verification model.

#### Optimization 2: AgentCore Memory for Schema Mapping

**Before**: Every schema mapping invocation started from scratch. Approved/rejected mappings were stored in DynamoDB but never fed back to improve future mappings.

**After**: AgentCore Memory stores mapping decisions as semantic memory records. Before calling Bedrock, the schema mapping Lambda queries Memory for similar past mappings and includes them in the prompt.

```
Schema Mapping Flow:
  1. Load source/target schemas
  2. NEW: Query Memory for similar past mappings
  3. Call Bedrock with source + target + historical context
  4. Parse field mappings
  5. NEW: Store mapping decisions in Memory
  6. Store mapping in DynamoDB
  7. (Later) Approval handler stores feedback in Memory
```

Key components:
- `shared/mapping_memory.py` — `MappingMemoryClient` with `get_similar_mappings()`, `store_mapping_decision()`, `store_approval_feedback()`, `format_historical_context()`
- Feature flag: `MEMORY_ENABLED=false` disables all Memory calls (client returns empty results)
- `scripts/seed_mapping_memory.py` — One-time migration of historical approved mappings into Memory

#### Optimization 3: AgentCore Runtime for Agent Lambdas

**Before**: Four Lambda functions (`create_code`, `test_code`, `create_glue_pipeline`, `create_infrastructure`) had ~3s cold starts, 15-minute timeout limits, and created new MCP connections per invocation.

**After**: Each Lambda has a `USE_AGENTCORE_RUNTIME` feature flag. When enabled, the Lambda acts as a thin proxy — forwarding the Step Functions payload to an AgentCore Runtime container via SigV4-signed HTTP POST.

```
Step Functions → LambdaInvoke(proxy) → HTTP POST → AgentCore Runtime (warm container)
```

Key components:
- `agentcore/{create_code,test_code,create_glue_pipeline,create_infrastructure}/` — Container entrypoints using `BedrockAgentCoreApp`
- `shared/agentcore_proxy.py` — Shared `invoke_agentcore_runtime()` function with SigV4 signing, retry logic, and timeout handling
- MCP connections initialized once at container startup and reused across invocations
- No 15-minute timeout constraint; containers stay warm for subsequent invocations

#### Feature Flags

| Flag | Default | Effect |
|------|---------|--------|
| `MEMORY_ENABLED` | `true` | Enables/disables AgentCore Memory for schema mapping |
| `USE_AGENTCORE_RUNTIME` | `false` | Routes agent Lambdas through AgentCore Runtime containers |
| `AGENTCORE_*_ENDPOINT` | (empty) | AgentCore Runtime endpoint URLs per agent |


---

### Agentic Capabilities (Phase 13)

Phase 13 adds five autonomous agents that implement observe→decide→act→verify loops on top of the existing pipeline infrastructure. Each agent is deployed as an AgentCore Runtime container and writes structured trace data to `agentic_data_ingestion_agent_traces` (P13a) for dashboard visibility.

#### Agent Architecture Pattern

All agents share a common entrypoint pattern using `BedrockAgentCoreApp` from the AgentCore Runtime SDK, `Strands Agent` for LLM-powered reasoning, and `AgentTracePlugin` (P13a) for automatic observability:

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.bedrock import BedrockModel
from agent_trace_plugin import AgentTracePlugin

app = BedrockAgentCoreApp()

@app.entrypoint
def handler(payload: dict) -> dict:
    trace_plugin = AgentTracePlugin(
        pipeline_id=payload['pipeline_id'],
        run_id=payload.get('run_id'),
        step_name='self_healing',  # agent-specific step name
    )
    agent = Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=AGENT_PROMPT,
        hooks=[trace_plugin],
    )
    response = agent(build_prompt(payload))
    trace_plugin.flush()  # Write trace summary to DynamoDB
    return parse_response(response)

if __name__ == "__main__":
    app.run()
```

Each agent container:
- Receives HTTP requests on port 8080 (AgentCore Runtime protocol)
- Includes `/ping` healthcheck endpoint (provided by `BedrockAgentCoreApp`)
- Copies `shared/` into the container at `/app/shared` for access to `env_utils`, `audit_utils`, `agent_trace_plugin`, and `mapping_memory`
- Uses `opentelemetry-instrument` as the CMD wrapper for automatic OTEL tracing
- Writes results to `agentic_data_ingestion_state` DynamoDB table and `agentic_data_ingestion_audit_log` via `write_agent_audit_entry()`

#### Agent Container Deployment Pattern

All five agents use an identical Dockerfile structure:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY agentcore/<agent_name>/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir aws-opentelemetry-distro
COPY shared /app/shared
COPY agentcore/<agent_name>/main.py .
ENV PYTHONPATH="/app:/app/shared"
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/ping || exit 1
CMD ["opentelemetry-instrument", "python", "main.py"]
```

CDK deploys each container as an AgentCore Runtime resource in `agentcore-stack.ts` with agent-specific IAM permissions and environment variables.

#### Five Agents Overview

| Agent | Real trigger | Automatic? | Key Capability |
|-------|--------------|------------|----------------|
| Self-Healing | `job_completion` Lambda async-invokes `self_healing_dispatch` on Glue job failure | ✅ yes | Classifies errors, applies remediation, re-triggers pipeline |
| Complex Mapping | `schema_mapping` Lambda invokes it when average mapping confidence < threshold (default 0.7) | ✅ yes | Uses sample data values to resolve ambiguous field mappings |
| Data Quality | None. The on-demand action was disabled on 2026-10-11 ([AGENTS.md § 3](AGENTS.md)) | ⚠️ no | Row counts, null rates, duplicates, freshness checks |
| Cost Optimization | `CostOptimizationScheduleRule` (`cron(weekDay: MON, hour: 6, minute: 0)`) → agent dispatch Lambda | ✅ yes | DPU utilization analysis, right-sizing recommendations |
| NL Pipeline Builder | `POST /api/chat` from the dashboard chat interface | ⚠️ on-demand | Parses natural language into a pipeline proposal; creation goes through `POST /pipelines` |

Notes on the two ⚠️ rows:
- **Data Quality** has no trigger. Its EventBridge rule is deliberately still pointed at SNS (see below), and the on-demand endpoint was removed because it could only overwrite the inline Glue report with a failing one ([AGENTS.md § 3](AGENTS.md)). Inline Glue Data Quality is the quality mechanism.
- **NL Pipeline Builder** is reachable and returns the structured proposal. It never creates a pipeline: the dashboard creates one from the proposal through `POST /pipelines`, which owns input validation (threat model T-05).

None of the five agents reaches an MCP server — they use boto3, Bedrock, and AgentCore Memory directly. See the MCP allowlist table in [AGENTS.md](AGENTS.md#mcp-tool-allowlist-per-agent-p28-r163) for which surfaces have MCP endpoints wired at all.

#### Triggers

**AgentCore Runtime cannot be an EventBridge target.** The target list is fixed and API Destinations cannot sign SigV4, so every event-driven agent invocation goes **Rule → `agentic_data_ingestion_agent_dispatch` Lambda → AgentCore**. The dispatch Lambda owns the long synchronous `InvokeAgentRuntime` call inside its own 15-minute timeout. This is why the architecture has a dispatch hop at all.

**EventBridge Rules** (defined in `orchestration-stack.ts`). Three rules look like agent triggers; **only one invokes an agent**:

| Rule | Pattern / schedule | Target | Invokes an agent? |
|------|--------------------|--------|-------------------|
| `GlueJobFailureSelfHealingRule` | `detail-type: "Glue Job State Change"`, `detail.state` in `FAILED`, `ERROR`, `TIMEOUT` | SNS critical-alerts topic, static text | ❌ notification only |
| `GlueJobSuccessDataQualityRule` | `detail-type: "Glue Job State Change"`, `detail.state: "SUCCEEDED"` | SNS critical-alerts topic, static text | ❌ notification only |
| `CostOptimizationScheduleRule` | `cron(weekDay: MON, hour: 6, minute: 0)` | `agentDispatchLambda` with `agent_endpoint_env: 'COST_OPTIMIZATION_AGENT_ENDPOINT'`, `step_name: 'cost_optimization'` | ✅ Cost Optimization Agent *(p30a R1.1)* |

Neither Glue-state rule filters on job name — the pattern matches every Glue job in the account.

**Why the two Glue-state rules are notification-only.** This is a hold, not an oversight (p30a R1.5). The data-quality rule's pattern matches *every* successful Glue job in the account, so switching its target to the dispatch Lambda is a load and cost decision rather than a one-line target swap; a CDK assertion pins the SNS target and its static text so enabling it has to be deliberate. The IAM grant for the dispatch path already exists, added for the on-demand endpoint. Self-healing does not need its rule at all — see below.

**Direct Lambda invocation** (the working automatic paths):

| Source | Target Agent | Mechanism |
|--------|-------------|-----------|
| `job_completion` Lambda, on Glue job failure | Self-Healing Agent | Async invoke (`InvocationType="Event"`) of `agentic_data_ingestion_self_healing_dispatch` *(P28 R9.3)* |
| `agentic_data_ingestion_schema_mapping` Lambda | Complex Mapping Agent | Conditional synchronous invoke when average mapping confidence < threshold |

**API Invocation** (via dashboard API Lambda). The two dispatch-backed endpoints are fire-and-forget async invokes of the dispatch Lambda — never a `threading.Thread`, which the Lambda freeze kills on return, and which is how both endpoints once returned 202 for work that never ran:

| Endpoint | Target Agent | Notes |
|----------|-------------|-------|
| `POST /api/cost-recommendations/{id}/apply` | Cost Optimization Agent | Gated on `AUTO_APPLY_ENABLED`, which is set on neither the runtime nor the dashboard API today, so this correctly reports recommend-only |
| `POST /api/chat` | NL Pipeline Builder | Synchronous AgentCore call; the handler returns only `{response, message}` and drops any structured proposal |

#### MCP Tool Profiles Per Agent

| Agent | MCP Server(s) | Key Tools Used |
|-------|---------------|----------------|
| Self-Healing | `agentic-data-ingestion-mcp-codegen` | Glue job read/update, interactive sessions for code testing |
| Self-Healing | CloudWatch Logs (direct boto3) | `get_log_events`, `filter_log_events` for error diagnosis |
| Complex Mapping | `agentic-data-ingestion-mcp-analytics` | Athena queries for target table inspection |
| Complex Mapping | S3 (direct boto3) | `get_object` for sample data reading |
| Complex Mapping | AgentCore Memory | `MappingMemoryClient.get_similar_mappings()` for historical decisions |
| Data Quality | `agentic-data-ingestion-mcp-analytics` | Athena queries: row counts, null rates, duplicates, freshness |
| Cost Optimization | CloudWatch (direct boto3) | `get_metric_data` for DPU utilization metrics |
| Cost Optimization | `agentic-data-ingestion-mcp-codegen` | Glue job config read/update for auto-apply |
| NL Pipeline Builder | S3 (direct boto3) | `list_objects_v2`, `get_object` for source discovery and schema extraction |

#### IAM Permissions Per Agent

| Agent | Key IAM Permissions |
|-------|-------------------|
| Self-Healing | `glue:UpdateJob`, `glue:GetJob`, `states:StartExecution`, `logs:GetLogEvents`, `logs:FilterLogEvents` |
| Complex Mapping | `s3:GetObject` (sample data), AgentCore Memory access via `MAPPING_MEMORY_ID` env var |
| Data Quality | `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `s3:GetObject/PutObject` (Athena results) |
| Cost Optimization | `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, `glue:GetJob`, `glue:UpdateJob`, `glue:GetJobRuns` |
| NL Pipeline Builder | `s3:ListBucket`, `s3:GetObject`, `s3tables:*`, `glue:GetDatabase`, `glue:GetTables` |

#### Dashboard API Endpoints (Phase 13)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/pipelines/{id}/runs/{runId}/self-healing` | GET | Self-healing activity for a run |
| `/api/pipelines/{id}/runs/{runId}/quality-report` | GET | Data quality report for a run |
| `/api/cost-recommendations` | GET | Latest cost optimization recommendations |
| `/api/cost-recommendations/{id}/apply` | POST | Apply a cost recommendation |
| `/api/chat` | POST | Send message to NL pipeline builder |

#### Agent Files

```
agentcore/
├── self_healing_agent/
│   ├── main.py           # Error classification, remediation, re-trigger, verification
│   ├── Dockerfile
│   └── requirements.txt
├── complex_mapping_agent/
│   ├── main.py           # Sample data reading, evidence-based mapping, question generation
│   ├── Dockerfile
│   └── requirements.txt
├── data_quality_agent/
│   ├── main.py           # Athena queries, row counts, null rates, duplicates, freshness
│   ├── Dockerfile
│   └── requirements.txt
├── cost_optimization_agent/
│   ├── main.py           # CloudWatch metrics, over/under-provisioning, idle detection, auto-apply
│   ├── Dockerfile
│   └── requirements.txt
└── nl_pipeline_builder/
    ├── main.py           # Intent parsing, source discovery, schema proposal, mapping generation
    ├── Dockerfile
    └── requirements.txt
```

---

## Related Documentation

- [DynamoDB Tables](DYNAMODB-TABLES.md)
- [Lambda Functions](LAMBDA-FUNCTIONS.md)
- [Dashboard Documentation](DASHBOARD.md)
- [MCP Integration](MCP-INTEGRATION.md)
- [Agents Guide](AGENTS.md)
- [Getting Started](GETTING-STARTED.md)

