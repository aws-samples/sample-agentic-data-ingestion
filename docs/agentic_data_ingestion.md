# Agentic Data Ingestion

## What It Is

Agentic Data Ingestion is an AI-powered ETL pipeline automation system that takes a raw data file and a target schema, then automatically generates, tests, and deploys an AWS Glue job to transform the data. It runs entirely on AWS — Bedrock for AI, Step Functions for orchestration, Lambda for compute, DynamoDB for state, and a React dashboard for visibility.

## The Problem

Building ETL pipelines is repetitive, error-prone, and slow. A data engineer typically needs to:

1. Understand the source data format and schema
2. Map source fields to target fields (often hundreds of columns)
3. Write PySpark transformation code
4. Test the code against sample data
5. Deploy the Glue job with the right worker configuration
6. Monitor execution and handle failures
7. Repeat everything when the source schema changes

Agentic Data Ingestion automates steps 1–7. You upload a file, point it at a target, and the system handles the rest — including re-running the pipeline when schemas drift.

## How It Works

An 8-step Step Functions workflow orchestrates the pipeline (with 2 additional steps for quality checks and execution, making 10 steps total):

1. **File Validation** — checks format, size, and structure
2. **Schema Extraction** — infers the source schema from the data
3. **Schema Comparison** — detects drift against the target schema
4. **Schema Mapping** — AI maps source fields to target fields with confidence scores
5. **Code Generation** — Jinja2 templates provide the boilerplate, Bedrock generates the transform logic
6. **Code Testing** — a Strands Agent verifies the code on real Spark in a Glue interactive session, against a bounded sample of the pipeline's own source data
7. **Infrastructure Provisioning** — creates S3 Tables, Glue databases, or other targets as needed; registers S3 Tables in Glue Data Catalog
8. **Glue Job Creation** — deploys the ETL job with AI-optimized worker configuration
9. **Quality Checks** — generates DQDL rules from field mappings
10. **Run Pipeline** — executes the Glue job with inline data quality evaluation (EvaluateDataQuality transform)

Approval gates can pause the workflow at schema mapping and code generation steps, giving humans a chance to review before proceeding.

---

## Features

### Pipeline Creation Wizard

A 6-step guided wizard for creating new pipelines: name the pipeline, configure the source (S3 upload or JDBC), choose a target destination (S3 Tables, JDBC, or S3 Parquet), define the target schema (upload, detect from existing, or select from library), pick an AI model and configure approval gates, then review and create.

**Why it matters:** Eliminates the need to manually configure Glue jobs, IAM roles, and S3 paths. A pipeline that would take hours to set up is created in minutes.

### AI-Powered Schema Mapping

Bedrock Claude analyzes source and target schemas and produces field-level mappings with confidence scores. Low-confidence mappings trigger an approval gate. The system learns from past decisions via AgentCore Memory, so mapping accuracy improves over time.

**Why it matters:** Schema mapping is the most tedious part of ETL development. AI handles the obvious mappings and flags the ambiguous ones for human review.

### Mapping Memory Management

Stores AI mapping decisions in Bedrock AgentCore Memory. Engineers can view all stored mapping patterns with confidence scores and approval status, and delete incorrect entries that would pollute future suggestions.

**Why it matters:** Bad mapping memories compound — one wrong decision gets reused across future pipelines. The management UI lets engineers curate the knowledge base.

### Template-First Code Generation

PySpark code is generated using Jinja2 templates for the deterministic boilerplate (~70 lines) and Bedrock for the transform block (~5–15 lines). The templates cover the matrix two ways: one target variant per S3-upload target (S3 Tables Iceberg, Glue Catalog, JDBC, S3 Parquet; Glue Catalog is kept for existing pipelines and is no longer offered for new ones), plus a JDBC-source template that reads from a database — resolving credentials from Secrets Manager at runtime — and dispatches to the same per-target write partials. Template count and derivation: [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory).

**Why it matters:** Pure AI code generation is unpredictable. The template-first approach keeps the boilerplate reliable while letting AI handle only the creative part — the actual data transformation logic.

### Sandboxed Code Testing

Generated code is verified on real Spark, in a Glue interactive session, against a bounded 100-row sample of the pipeline's own source S3 object. A Strands Agent generates test cases, runs them, and reports pass/fail with coverage metrics. JDBC sources are never sampled — a verification session holds no database credentials — so they are verified against a frame synthesised from the declared schema, which is also the fallback when a source object's format isn't recognised. A source read that fails is reported as a harness non-verification, never as a transform failure, so "not verified" is always distinguishable from "the code is wrong". An AgentCore Code Interpreter path that converts PySpark to pandas remains available as an explicit opt-out.

**Why it matters:** Deploying untested ETL code to production is risky. Verifying against the pipeline's own data catches the errors that synthesised test rows never surface — real nulls, real formats, real edge values — before the Glue job is created.

### Schema Drift Detection

When a new file arrives, the system compares its schema against the existing target. If fields were added, removed, or changed, the pipeline re-runs the mapping and code generation steps automatically.

**Why it matters:** Source schemas change constantly. Without drift detection, pipelines silently produce incorrect data or fail at runtime.

### Schema Diff Visualization

A visual diff viewer shows exactly what changed between two schema versions — added fields in green, removed in red, modified in yellow. Supports inline and side-by-side view modes.

**Why it matters:** When drift is detected, engineers need to quickly understand what changed before approving new mappings.

### Approval Workflows

Two configurable approval gates: one for schema mappings (triggered automatically on low confidence or schema changes) and one for generated code (optional). Approvals use Step Functions `waitForTaskToken` with a 72-hour default timeout — `APPROVAL_WINDOW_HOURS` in `shared/config.py`, mirroring the gate's `APPROVAL_TIMEOUT_SECONDS` in the CDK. Full audit trail of every decision.

**Why it matters:** AI-generated code and mappings need human oversight. Approval gates provide that without blocking the entire pipeline.

### Glue Job Log Viewer

View Spark driver and executor logs directly in the dashboard. Toggle between output and error logs, filter by keyword, and auto-refresh every 5 seconds while a job is running.

**Why it matters:** Diagnosing Glue job failures normally requires switching to the CloudWatch console and navigating log groups. The embedded viewer keeps operators in context.

### Glue Job Rollback

Roll back a Glue job script to a previous version with one click. The confirmation modal shows the current and target versions with timestamps. Success invalidates caches and refreshes the UI.

**Why it matters:** Bad deployments happen. Rolling back through the AWS console is slow and error-prone. One-click rollback from the dashboard is faster and safer.

### Glue Job Version History

Track every version of a Glue job script with timestamps. The code tab shows a version selector dropdown so engineers can compare current and previous versions.

**Why it matters:** Understanding what changed in a Glue job script is essential for debugging regressions.

### Bulk Pipeline Operations

Select multiple pipelines and archive or delete them in batch. The delete button is only enabled when all selected pipelines are already archived (safety guard). Progress indicator shows completion during execution. Partial failures are reported individually.

**Why it matters:** Managing dozens of pipelines one at a time doesn't scale. Bulk operations let administrators clean up efficiently.

### Pipeline Archive & Restore

Soft-delete pipelines by archiving them. Archived pipelines move to a separate view and can be restored at any time. Hard delete is only available for archived pipelines.

**Why it matters:** Accidental deletion is a real risk. The archive-first pattern provides a safety net.

### Execution History & Run Detail

Every pipeline execution is tracked with status, step progress, timing, and error details. The run detail page uses a tabbed layout with 6 tabs (Step Flow, AI Traces, Approvals, Schema & Mappings, Code, Data Quality) and prev/next run navigation. The step flow tab shows a vertical step flow visualization with color-coded status, MCP operations performed at each step, and error messages with stack traces.

**Why it matters:** When a pipeline fails, engineers need to know exactly which step failed and why. The step flow visualization makes this immediately obvious.

### Run Retry

Failed runs can be retried with one click. The retry creates a new execution linked to the original, and the UI shows a "Retry" badge and a link back to the source run.

**Why it matters:** Transient failures (network timeouts, throttling) are common. Retry avoids re-uploading files and re-configuring pipelines.

### Run Cancellation

Running executions can be cancelled from the dashboard, from the run list or the run detail page.

- **At a Lambda step or an approval gate:** the Step Functions execution is stopped and the run shows **CANCELLED** at once. A step Lambda that was already running finishes, but its result no longer changes the run.
- **While the Glue job runs:** the job is stopped and the run shows **CANCELLING** until Glue reports it stopped, which can take minutes. Then the execution is stopped and the run shows **CANCELLED**. The pipeline stays locked until the job has stopped, so its next run cannot collide with it. If the job finishes before the stop takes effect, the run ends **SUCCEEDED**.

A cancelled run does not trigger self-healing and does not count toward the pipeline error-rate alarm. A finished run cannot be cancelled (the API answers 409), and neither can a run whose Glue job has visibly begun starting (its state row exists but Glue has not yet returned a job run id; try again in a few seconds).

A cancel that arrives while the launcher Lambda is running but has written nothing yet is handled durably: the cancel leaves a fence on the run's callback record before stopping the execution, and the launcher checks that fence both before starting the Glue job and immediately after. If the fence is already there it never starts the job; if it appeared during the start call, the launcher stops the job it just started and the run closes as **CANCELLED** when Glue reports it stopped.

**Why it matters:** Long-running or stuck pipelines waste compute resources. Cancellation provides an escape hatch.

### Data Preview

Preview transformed data directly in the dashboard — both the latest output and run-specific previews with pagination for large datasets.

**Why it matters:** Engineers need to verify that transformations produce correct output without downloading files from S3.

### Data Lineage

Track the full data lineage for each run — from source file through each transformation step to the final output. Includes cost tracking per pipeline.

**Why it matters:** Regulatory compliance and debugging both require understanding where data came from and how it was transformed.

### File Upload & Validation

Upload source data files via presigned S3 POST with progress tracking and cancel support. Files are validated for format, size, and structure before the pipeline starts.

**Why it matters:** Catching bad files early prevents wasted compute on pipelines that would fail at the first step.

### File Queue

When a pipeline is already running, new file uploads are queued and processed automatically when the current execution completes.

**Why it matters:** Without queuing, concurrent uploads would either fail or corrupt state. The queue ensures orderly processing.

### Real-Time Updates

WebSocket API pushes execution status updates to the dashboard in real time. Automatic reconnection with exponential backoff falls back to polling if the WebSocket connection drops.

**Why it matters:** Polling-only dashboards feel sluggish. Real-time updates give operators immediate feedback on pipeline progress.

### Notifications

In-app notification bell with unread count badge. Notifications are generated for pipeline completions, failures, and approval requests. Mark-as-read support.

**Why it matters:** Engineers managing multiple pipelines need to know when something requires attention without constantly watching the dashboard.

### Metrics Dashboard

Comprehensive metrics page with pipeline success rates, execution counts, average duration, error rates, and cost tracking. Data comes from CloudWatch with configurable time periods.

**Why it matters:** Operational visibility across all pipelines helps identify trends, bottlenecks, and cost optimization opportunities.

### Error Tracking

Centralized error page with date range and error type filters. Errors are classified and stored in a dedicated DynamoDB table with TTL.

**Why it matters:** Scattered error logs across CloudWatch log groups are hard to search. A centralized view makes pattern recognition easier.

### Audit Trail

Every significant action (create, edit, archive, approve, reject, delete) is logged to an audit table with timestamps, user identity, and action details. Agent actions (self-healing, mapping, quality checks) are also recorded.

**Why it matters:** Compliance requirements and incident investigation both need a reliable record of who did what and when.

### Self-Healing Pipelines

Failed Glue jobs are diagnosed automatically. The self-healing agent classifies the error and acts on the class: it upgrades the worker type for out-of-memory failures, and re-runs the standard pipeline for schema-mismatch and type-cast failures so the transform is regenerated from the current mappings and re-tested. Permission errors, SQL the database rejected, and unrecognised errors are flagged for review rather than retried, because a re-run cannot change their outcome. The dashboard shows the classification, the remediation, and a link to the re-run.

**Why it matters:** The failures a re-run or a larger worker can fix recover without anyone stepping in, and the rest arrive with a diagnosis instead of a bare stack trace.

### AI Agent Observability

Every AI model invocation is traced — model calls, tool calls, token usage, estimated cost, and duration. The run detail page shows per-step AI trace timelines. The metrics page aggregates token usage and cost across all runs with per-model comparison.

**Why it matters:** AI costs can spiral without visibility. Token-level observability lets teams optimize model selection and prompt engineering.

### Natural Language Pipeline Builder

A chat interface where users describe a pipeline in plain English. The agent parses the intent, discovers source data in S3, and proposes a target schema and field mappings. `POST /api/chat` returns the agent's reasoning as conversational text; the structured proposal it builds is not yet carried through that endpoint, so creating the pipeline still goes through the wizard.

**Why it matters:** Describing a pipeline in plain English is a faster way to arrive at a target schema and a first set of mappings than filling in forms from scratch.

### Cost Optimization Agent

A weekly schedule runs the agent across the Glue job fleet, analysing DPU utilization and producing right-sizing recommendations. It detects over-provisioned jobs (utilization < 40%), under-provisioned jobs (OOM failures), and idle pipelines (no runs in 30+ days). The posture is recommend-only: applying a low-risk change requires `AUTO_APPLY_ENABLED=true` on both the dashboard API Lambda and the agent runtime, and it is set on neither today, so the apply endpoint reports `applied: false` and explains why rather than reporting a success it did not achieve.

**Why it matters:** Glue jobs are often over-provisioned because engineers pick safe defaults. Automated analysis surfaces the savings without anyone having to go looking for them.

### Data Quality Agent

The primary quality check mechanism is inline Glue Data Quality (EvaluateDataQuality transform), which runs inside the Glue ETL job during each pipeline execution. DQDL rules are generated from the schema mapping and produce pass/fail results that are written to DynamoDB and displayed in the dashboard's Data Quality tab. The Athena-based agent is still deployed but has no trigger: its on-demand action was removed on 2026-10-11 because it ran with no target table and no Athena access ([AGENTS.md § 3](AGENTS.md)).

**Why it matters:** ETL pipelines can produce incorrect data silently. Automated quality checks catch issues before downstream consumers are affected.

### Cost Estimation & Tracking

AI-recommended Glue worker configuration with per-run cost estimates. Actual costs are tracked and compared against estimates.

**Why it matters:** Glue jobs can be expensive. Right-sizing workers and tracking actual spend prevents cost surprises.

### Schema Management

Upload and manage target schemas independently of pipelines. Schemas can be reused across multiple pipelines.

**Why it matters:** Many pipelines share the same target schema. A schema library avoids redundant uploads and keeps definitions consistent.

### Interactive Mapping Editor

Drag-and-drop mapping editor for manually adjusting AI-generated field mappings. Engineers can add, remove, or reassign mappings and save hints that influence future AI suggestions.

**Why it matters:** AI mappings aren't always right. The editor lets engineers fix mistakes without starting over.

### MCP Tool Integration

Five Model Context Protocol servers (codegen, infra, analytics, s3_tables, data_processing) provide tools for data processing (Glue, Athena, EMR), S3 Tables management, code generation, and infrastructure provisioning. The Strands Agent selects and invokes tools autonomously during pipeline execution.

**Why it matters:** MCP gives the AI agent direct access to AWS services, enabling it to create infrastructure, run queries, and manage resources without hardcoded integrations.

### Dark Mode

Theme toggle with localStorage persistence. The entire Cloudscape UI respects the selected theme.

**Why it matters:** Engineers who work late appreciate it.

### Keyboard Shortcuts

Help modal with keyboard shortcut reference for common dashboard actions.

**Why it matters:** Power users navigate faster with keyboard shortcuts than mouse clicks.

---

## Architecture at a Glance

- **Frontend:** React 18 + TypeScript + Cloudscape Design System + Vite
- **API:** API Gateway (REST + WebSocket) → Lambda (Python 3.12, router pattern)
- **Orchestration:** Step Functions (10-step workflow with approval gates, inline data quality)
- **AI:** Bedrock Converse API (Claude Sonnet 4.6 default, configurable via `BEDROCK_MODEL_ID` — model-agnostic) + Strands Agents + AgentCore (Memory, Code Interpreter, MCP Runtime)
- **Compute:** Lambda functions + AgentCore Runtime containers + AWS Glue ETL jobs
- **Storage:** DynamoDB tables (with GSIs) + S3 buckets
- **Events:** EventBridge custom bus + SQS dead letter queue
- **Infrastructure:** CDK v2 TypeScript (8 stacks)
- **Testing:** a layered Python suite (unit, contract, integration, property, CDK, e2e) plus vitest for the CDK and the dashboard. No counts or timings are stated anywhere; [TESTING.md](TESTING.md) carries the command that produces each, so re-measure rather than quote
- **Agents:** 5 autonomous agents. Self-healing (on Glue job failure), complex mapping (on low mapping confidence), and cost optimization (weekly schedule) are invoked automatically. The data quality agent has no trigger: its automatic trigger is deferred by deliberate decision (its event pattern would match every successful Glue job in the account, so turning it on is a load and cost decision rather than a wiring change), and its on-demand action was removed because it could not complete. The NL pipeline builder is reachable at `POST /api/chat` for conversational responses

> **The counts are deliberately absent from this list.** How many Lambda functions, DynamoDB
> tables, GSIs, S3 buckets, MCP servers, agent containers, PySpark templates, wizard steps and
> approval gates exist lives as a literal exactly once — in
> [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory) — with the rule that
> produces each figure and a derivation a test executes. A glance list is exactly where a stale
> count survives longest, which is why this one points instead of counting.
