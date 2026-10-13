# Agentic Data Ingestion Agents Guide (Phase 13)

Five autonomous agents that add observe→decide→act→verify loops to the Agentic Data Ingestion ETL platform. Each agent runs as an AgentCore Runtime container, uses Strands Agent for LLM reasoning, and writes structured traces via `AgentTracePlugin` (P13a).

All five agents exist and are deployed, but **not all of them are automatically triggered**. Check this table before assuming an agent runs on its own:

| Agent | Trigger status |
|-------|----------------|
| 1. Self-Healing | **Automatic** — `job_completion` Lambda async-invokes the dispatcher on Glue job failure |
| 2. Complex Mapping | **Automatic** — schema mapping Lambda invokes it when mean mapping confidence is below threshold, provided `COMPLEX_MAPPING_AGENT_ENDPOINT` is set on that Lambda. A full deploy sets it; §2 records what happens when it is not |
| 3. Data Quality | **Not reachable** — the on-demand action was disabled on 2026-10-11 (see §3), and the automatic trigger is deliberately deferred (p30a R1.5) |
| 4. Cost Optimization | **Automatic** — weekly EventBridge schedule via the agent dispatch Lambda (p30a R1.1) |
| 5. NL Pipeline Builder | **On-demand** — reachable at `POST /api/chat`, which returns the structured proposal. The agent only proposes; pipelines are created through `POST /pipelines`. See §5 |

## Architecture Summary

All agents share a common pattern:

```
HTTP request (port 8080) → BedrockAgentCoreApp entrypoint
  → AgentTracePlugin setup (observability)
  → Strands Agent with BedrockModel (LLM reasoning)
  → Write results to agentic_data_ingestion_state table
  → Write audit entry via write_agent_audit_entry()
  → Flush trace data to agentic_data_ingestion_agent_traces table
  → Return JSON response
```

Each container includes `shared/` modules (`env_utils`, `audit_utils`, `agent_trace_plugin`, `mapping_memory`) and is instrumented with OpenTelemetry.

**AgentCore Runtime cannot be an EventBridge target.** The EventBridge target list is fixed, and API Destinations cannot sign SigV4. Every event-driven agent invocation therefore takes the route:

```
EventBridge Rule → agent dispatch Lambda → AgentCore Runtime
```

The dispatch Lambda owns the long synchronous `InvokeAgentRuntime` call inside its own 15-minute timeout. This constraint explains the shape of the triggers below: an EventBridge rule whose target is *not* the dispatch Lambda does not reach an agent. Request handlers dispatch the same way, with `InvocationType="Event"` — never a `threading.Thread`, which is forbidden anywhere under `lambda/` as a standing invariant: Lambda freezes the execution environment on return, so the thread is killed mid-call and the API reports `202` for work that never ran. Keeping the thread was rejected as worse than plainly broken, because a warm container occasionally let it finish, and success nobody can reproduce cannot be tested or relied on.

---

## 1. Self-Healing Agent

**Purpose:** Automatically diagnose and fix failed Glue ETL jobs without manual intervention.

**File:** `agentcore/self_healing_agent/main.py`

**Trigger:** Automatic, on Glue job failure — but **not** via an EventBridge rule. The `job_completion` Lambda async-invokes the self-healing dispatcher (`SELF_HEALING_FN`, `InvocationType="Event"`) after sending the task failure to Step Functions (`lambda/agentic_data_ingestion_job_completion/lambda_function.py:892-909`). Dispatch is best-effort: a failed dispatch is logged and does not fail `job_completion`. A Glue job stopped by a dashboard cancel is not a failure and dispatches nothing: `job_completion` closes that run as cancelled before it reaches the dispatch.

> The similarly-named `GlueJobFailureSelfHealingRule` EventBridge rule in `orchestration-stack.ts` is a **notification only** — its target is the critical alerts SNS topic carrying static text, and it invokes no agent. Self-healing does not need that rule to reach its agent.

**Payload:**
```json
{
  "run_id": "run-2025-...",
  "pipeline_id": "pipe-123",
  "step_name": "glue_etl",
  "error_message": "AnalysisException: cannot resolve ...",
  "job_name": "agentic-data-ingestion-pipe-123-etl"
}
```

**Output** (the outcome record, written to the state table under `step_name: self_healing`; an out-of-memory heal is shown):
```json
{
  "agent_type": "self_healing",
  "healing_id": "heal-3f9c1a2b7d4e",
  "error_classification": "oom_error",
  "action": "upgrade_worker_type",
  "remediation_applied": "Upgraded worker type from G.1X to G.2X",
  "original_run_id": "run-2025-...",
  "rerun_id": "run-2025-...",
  "rerun_started": true,
  "rerun_status": "running",
  "diagnosis_duration_ms": 3,
  "total_duration_ms": 1850
}
```

`rerun_status` is written as `running` when the re-run starts; the `job_completion` Lambda later moves it to `success` or `failed`. The other values it can hold are `skipped` (no fix applies), `skipped_heal_guard` (the failed run was itself a heal re-run), `skipped_partial_write_guard` (a re-run is unsafe for this target and write mode), and `start_failed`. Diagnosis is regex matching, not an LLM call, which is why it takes milliseconds.

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `classify_error(error_message)` | Regex classification into `schema_mismatch`, `oom_error`, `type_cast_error`, `permission_error`, `sql_syntax_error`, or `unknown`. First match wins, in that order |
| `remediate_by_rerun(error_class)` | The remediation for `schema_mismatch` and `type_cast_error`: re-run the standard pipeline, so `create_code` regenerates the transform from the current approved mappings and `test_code` re-validates it. The agent writes no code itself; the side channel that used to do so was removed (P28 R9.6) |
| `remediate_oom(pipeline_id, job_name, current_worker_type=None)` | Upgrades the Glue worker type one step along G.1X→G.2X→G.4X→G.8X. Already at G.8X, it applies nothing and flags for review |
| `was_heal_triggered(payload)` | The heal guard: declines to heal a run that was itself a heal re-run, so a failure cannot loop |
| `evaluate_rerun_safety(pipeline_record)` | The partial-write guard: declines a re-run for target type and write-mode combinations where a second write is unsafe. Matrix-gated, not detection-gated |
| `retrigger_pipeline(pipeline_id, run_id, fix_description, job_config_overrides=None, pipeline_record=None)` | Starts a new Step Functions execution, building its input from the shared rerun contract (`shared/rerun_contract.py`) |
| `build_outcome_record(...)` | Assembles the record above, which is what the dashboard reads |

The agent does not poll the re-run to completion. There is no verification loop in the agent; the outcome arrives through `job_completion`.

**Error Classification Patterns:**

| Category | Regex Patterns | What happens |
|----------|---------------|--------------|
| `schema_mismatch` | `AnalysisException`, `cannot resolve`, `schema mismatch`, `column not found` | Re-run |
| `oom_error` | `OutOfMemoryError`, `Container killed by YARN`, `memory exceeded` | Worker upgrade, then re-run |
| `type_cast_error` | `NumberFormatException`, `DateTimeParseException`, `cannot cast`, `type mismatch` | Re-run |
| `permission_error` | `AccessDeniedException`, `not authorized`, `permission denied` | Flagged for review |
| `sql_syntax_error` | `You have an error in your SQL syntax` (MySQL/MariaDB), `syntax error at or near` (PostgreSQL), `Incorrect syntax near` (SQL Server), `ORA-00900` / `ORA-00933` / `ORA-00936` (Oracle), `SQLSyntaxErrorException` (JDBC wrapper) | Flagged for review |

Anything else classifies as `unknown` and is flagged for review. The three review classes never re-trigger, because a re-run cannot change their outcome. `sql_syntax_error` in particular is deliberately excluded from the re-run branch even though it is a code-level failure: the source read's SQL is rendered deterministically from the pipeline record, so a re-run would send the identical rejected statement and fail the same way.

**Dashboard UI:** "Self-Healing Activity" section on RunDetailPage showing the error classification badge, the re-run status, a link to the re-run when one was started (otherwise "None — no re-run was started"), and the remediation description. Re-run status severity: `success` green, `running` in-progress, any `skipped*` value blue info (a skip is a decision, not a fault), and `failed`, `start_failed` or an unrecognised value red.

---

## 2. Complex Mapping Agent

**Purpose:** Resolve ambiguous schema mappings using actual sample data values and historical mapping decisions from AgentCore Memory.

**File:** `agentcore/complex_mapping_agent/main.py`

**Trigger:** Automatic, from the schema mapping Lambda. The only gate is a confidence comparison, `if overall_confidence < COMPLEX_MAPPING_THRESHOLD` (`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:3426`). `overall_confidence` is the mean of the per-mapping confidences just computed for this run (`:3424`), and the threshold comes from the single config source `complex_mapping_threshold()` (`shared/config.py:345-351`, default `0.7` at `shared/config.py:216`, overridable per invocation by `COMPLEX_MAPPING_THRESHOLD`). **There is no field-count condition**: no schema-size gate for this agent exists anywhere in `lambda/`, `shared/`, `agentcore/` or `deploy-cdk/lib/`. The nearest 50 in that file is `_MAX_REPORTED_UNMAPPED_SOURCE_FIELDS` (`:2585`), an unrelated cap on how many unmapped field names a response reports.

> **Eligibility and invocation are two different things, and only the first is gated on confidence.** Crossing the confidence gate sets `complex_mapping_eligible = True` (`:3432`), and that flag is returned in the step response (`:3521`) whether or not the agent ran. The invocation sits inside a second, nested condition: `if COMPLEX_MAPPING_AGENT_ENDPOINT:` (`:3434`), read from the environment at `:3423`. When that variable is empty the Lambda logs `COMPLEX_MAPPING_AGENT_ENDPOINT not configured — skipping agent invocation` at INFO level (`:3507`) and still returns `complex_mapping_eligible: true`. So that flag means "confidence was low enough to warrant the agent", never "the agent was invoked", and the skip is quiet by design: a missing endpoint is not an error here, and nothing in the response distinguishes it.
>
> **A full deployment satisfies the precondition**, which is why the table above still says Automatic. `compute-stack.ts:607` sets the variable from `props.agentCoreComplexMappingEndpoint`, which `bin/agentic_data_ingestion.ts:179` imports from the AgentCore stack's `ComplexMappingAgentEndpoint` export. The skip path is what a Compute stack synthesised without that prop threaded produces, so treat it as a wiring check when evidence and questions are absent from a low-confidence run rather than as normal behaviour.

**Payload:**
```json
{
  "source_schema": {"fields": [{"name": "addr_1", "type": "string"}, ...]},
  "target_schema": {"fields": [{"name": "street_address", "type": "string"}, ...]},
  "sample_data_uri": "s3://bucket/path/to/data.csv",
  "pipeline_id": "pipe-123",
  "run_id": "run-2025-...",
  "confidence_threshold": 0.7
}
```

**Output:**
```json
{
  "evidence": {
    "addr_1": {
      "sample_values": ["123 Main St", "456 Oak Ave"],
      "reasoning": "Values are street addresses, not full addresses",
      "memory_matches": 2,
      "confidence_before": 0.62,
      "confidence_after": 0.94
    }
  },
  "questions": [
    {
      "field": "code_x",
      "question": "Field 'code_x' contains values like 'A1', 'B2'. Is this a product_category or region_code?",
      "options": ["product_category", "region_code"],
      "sample_values": ["A1", "B2", "C3"]
    }
  ],
  "mappings": [{"source": "addr_1", "target": "street_address", "confidence": 0.94}]
}
```

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `read_sample_data(s3_uri, max_rows=100)` | Downloads first 100 rows from S3 CSV/JSON, returns field→values dict (up to 10 unique values per field) |
| `lookup_historical_mappings(pipeline_id, source_fields)` | Queries `MappingMemoryClient.get_similar_mappings()` for past decisions |
| `build_evidence_prompt(source_schema, target_schema, sample_values, historical_mappings)` | Constructs Bedrock prompt with sample values and historical context |
| `generate_questions(low_confidence_mappings, sample_values)` | Creates specific questions with evidence for fields below confidence threshold |
| `_parse_agent_response(response_text)` | Extracts mappings JSON from agent response (handles markdown fences) |

**Dashboard UI:** Evidence cards in ApprovalWorkflowSection showing sample values, reasoning, confidence before/after, and memory indicator. Questions section with specific questions and suggested options for ambiguous fields.

---

## 3. Data Quality Agent

**Purpose:** Automated post-run quality investigation that checks row counts, null rates, duplicates, and data freshness, producing a structured report with root cause classification. The primary quality check mechanism is now inline Glue Data Quality (EvaluateDataQuality transform), which runs inside the Glue ETL job itself. DQDL rule results are written to DynamoDB and displayed in the dashboard's Data Quality tab. The Athena-based agent still exists but is no longer the primary quality check mechanism.

**File:** `agentcore/data_quality_agent/main.py`

**Trigger:** **None in the product.** The on-demand action, `POST /api/pipelines/{id}/runs/{runId}/quality-report` and the dashboard's "Run Quality Check" button, was removed on 2026-10-11: it dispatched the agent with no target table, the agent's runtime role holds no Athena, catalog or target-read grant, and its report replaced the inline Glue report on the same state-row key, so the only possible result was a failing report written over a real one. The agent runtime, its dispatch grant and the GET report endpoint remain, so the agent can be finished later. Finishing it means resolving and validating the target table per target type (only Glue Catalog targets are queryable by Athena without further integration), granting the runtime role scoped Athena, Glue catalog, target-read and results-location access, creating the results location (`ATHENA_RESULTS_BUCKET` is set by no stack), and writing the agent's report under its own state-row key so it cannot replace the inline report.

> There is no automatic trigger. The `GlueJobSuccessDataQualityRule` EventBridge rule targets the critical alerts SNS topic with static text and invokes no agent. This is **deferred by decision, not an oversight** — see the comment on that rule in `deploy-cdk/lib/orchestration-stack.ts` (p30a R1.5, design §3 C4). The reason matters: the rule's event pattern matches *every* successful Glue job in the account, so enabling it is a load and cost decision, not just a target swap. A CDK assertion pins the SNS target so turning it on has to be deliberate. Note also that the state table holds two `data_quality` rows from the pre-p30a threaded handler, so the SNS target should not be read as "data quality has never run".

**Payload:**
```json
{
  "pipeline_id": "pipe-123",
  "run_id": "run-2025-...",
  "target_table_info": {
    "database": "agentic_data_ingestion_db",
    "table": "customers"
  },
  "source_row_count": 1500,
  "primary_key_columns": ["customer_id"]
}
```

**Output:**
```json
{
  "pipeline_id": "pipe-123",
  "run_id": "run-2025-...",
  "overall_status": "warning",
  "checks": [
    {"name": "row_count", "status": "pass", "expected": 1500, "actual": 1500},
    {"name": "null_rate_email", "status": "warning", "rate": 0.12, "threshold": 0.05},
    {"name": "duplicate_keys", "status": "pass", "duplicates": 0},
    {"name": "freshness", "status": "pass", "latest_record": "2025-03-15T10:00:00Z"}
  ],
  "root_cause": "data_issue",
  "recommendations": ["Investigate null email addresses — 12% null rate exceeds 5% threshold"]
}
```

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `run_athena_query(query, database)` | Executes Athena query, polls for completion (max 2 min), returns rows as dicts |
| `check_row_count(database, table, expected_count)` | Compares `COUNT(*)` against expected |
| `check_null_rates(database, table, columns, threshold=0.05)` | Single Athena query computing null rate for all columns; fetches sample null rows for failures |
| `check_duplicates(database, table, key_columns)` | `GROUP BY` + `HAVING COUNT(*) > 1` with sample duplicates |
| `check_freshness(database, table, timestamp_column, max_age_hours=24)` | `MAX(timestamp_column)` vs current time |
| `_classify_root_cause(checks)` | Returns `transform_issue` (duplicates/row count), `infrastructure_issue` (freshness), or `data_issue` (null rates) |
| `generate_report(pipeline_id, run_id, checks)` | Assembles structured report with overall status, root cause, and recommendations |

**Timestamp Column Discovery:** The agent auto-detects timestamp columns by checking for common names: `updated_at`, `created_at`, `timestamp`, `last_modified`, `event_time`, `ingestion_time`, `load_timestamp`.

**Dashboard UI:** "Data Quality" tab on RunDetailPage with pass/fail scorecard, percentage-formatted metrics, and expandable drill-down per rule. Results are produced by inline Glue Data Quality (EvaluateDataQuality transform) during the ETL job.

---

## 4. Cost Optimization Agent

**Purpose:** Weekly analysis of Glue job DPU utilization with right-sizing recommendations and projected savings.

**File:** `agentcore/cost_optimization_agent/main.py`

**Trigger:** Automatic — `CostOptimizationScheduleRule`, an EventBridge schedule of `cron(weekDay: MON, hour: 6, minute: 0)`. This is the one Glue/schedule rule whose target **is** an agent route: `targets.LambdaFunction(compute.agentDispatchLambda)`, working since p30a R1.1. The rule's static input carries `agent_endpoint_env: 'COST_OPTIMIZATION_AGENT_ENDPOINT'`, `step_name: 'cost_optimization'`, and a payload of `{trigger: 'weekly_schedule'}`. The agent's entrypoint ignores that payload today and always runs the full fleet sweep; the marker distinguishes a scheduled run from the id-scoped apply path.

**Output:**
```json
{
  "generated_at": "2025-03-15T00:00:00Z",
  "recommendations": [
    {
      "pipeline_id": "pipe-123",
      "type": "downsize",
      "current": {"worker_type": "G.2X", "workers": 10},
      "recommended": {"worker_type": "G.1X", "workers": 5},
      "avg_dpu_utilization": 0.28,
      "projected_savings_monthly": 45.60,
      "risk": "low",
      "auto_apply_eligible": true
    }
  ],
  "total_projected_savings_monthly": 78.40,
  "jobs_scanned": 12
}
```

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `get_all_glue_jobs()` | Lists all `agentic_data_ingestion*` Glue jobs with config (worker type, count, default args) |
| `query_glue_job_metrics(job_names, days=7)` | Batch CloudWatch `GetMetricData` for execution time + `GetJobRuns` for DPU-hours and OOM detection |
| `detect_over_provisioned(jobs, metrics, threshold=0.4)` | Flags jobs where actual DPU utilization < 40%; recommends downgrade along G.8X→G.4X→G.2X→G.1X path |
| `detect_under_provisioned(jobs, metrics)` | Flags jobs with execution time > 2x baseline (600s) or OOM failures; recommends upgrade |
| `detect_idle_pipelines(jobs, days=30)` | Flags jobs with no runs in 30+ days; recommends archiving |
| `calculate_savings(recommendations, jobs, metrics)` | Computes projected monthly savings per recommendation based on DPU pricing ($0.44/DPU-hour) |
| `auto_apply_recommendation(recommendation)` | Applies low-risk downsize changes when `AUTO_APPLY_ENABLED=true` — **unset in every deployment as shipped**, so this path does not run today |

**Recommendation Types:**

| Type | Condition | Risk | Auto-Apply |
|------|-----------|------|------------|
| `downsize` | DPU utilization < 40% | low | Eligible, but off — requires `AUTO_APPLY_ENABLED=true`, set by no CDK stack |
| `upsize` | Execution > 2x baseline or OOM failures | medium | No |
| `idle` | No runs in 30+ days | low | No |

**Auto-apply is off, so the system is recommend-only.** `AUTO_APPLY_ENABLED` is set on **neither** the dashboard API Lambda nor the cost-optimization AgentCore runtime — it appears in no file under `deploy-cdk/`. `POST /api/cost-recommendations/{id}/apply` therefore returns `200` with `applied: false`, `dispatched: false`, `auto_apply_enabled: false` and a message explaining that nothing was applied and no agent run was started. That is a deliberate posture, not an error, which is why it is a `200` rather than a `202` implying work is in flight.

The flag is checked in **both** the caller and the agent, on purpose (see the `apply_cost_recommendation` docstring in `lambda/agentic_data_ingestion_dashboard_api/handlers/agents.py`):

- **In the handler**, because the dispatch is fire-and-forget — the agent's answer never reaches the caller, so the handler is the only thing that can make the HTTP response honest. It also avoids starting a run that could not change anything.
- **In the agent**, because that is the process holding the permissions to mutate a Glue job. A guard that only lives in the caller is a suggestion, not a control.

Enabling auto-apply means setting the flag in both places: on the AgentCore runtime (enforcement) and on the dashboard API Lambda (response honesty).

**Dashboard UI:** "Cost Recommendations" section on `MetricsDashboardPage` with per-pipeline recommendation cards (current vs recommended config, savings projection, risk badge) and an "Apply" button offered only on low-risk `downsize` recommendations. The UI states inline that Apply changes nothing until an operator sets `AUTO_APPLY_ENABLED=true` on both the dashboard API and the agent.

---

## 5. Natural Language Pipeline Builder

**Purpose:** Parse natural language descriptions into pipeline configurations for human confirmation and creation.

**File:** `agentcore/nl_pipeline_builder/main.py`

**Trigger:** `POST /api/chat` from the dashboard chat interface.

> **The agent proposes; it never creates (threat model T-05).** `handle_chat_message` in `lambda/agentic_data_ingestion_dashboard_api/handlers/agents.py` forwards exactly `message` and `conversation_id`; a `confirm` or `proposal` in the request body is dropped. The agent has no pipeline write path: `create_pipeline_from_config` and its confirm branch were removed, because writing a proposal straight to the pipelines table skipped every input check `POST /pipelines` applies. `ChatPage.handleCreatePipeline` (`dashboard-react/src/pages/ChatPage.tsx`) creates the pipeline through `POST /pipelines`, which validates it, so no feature was lost.
>
> The handler copies `AGENT_RESULT_PASSTHROUGH` back down: `status`, `reasoning_steps`, `proposal`, `pipeline_id`, `pipeline_name`. Each key is copied only when the agent supplied it, because `ChatPage` renders `PipelineProposalCard` on any truthy `proposal` and a defaulted `{}` would draw an empty card. `pipeline_id` and `pipeline_name` no longer occur, since the agent never creates.
>
> Two consequences worth knowing. There is no conversation continuity: nothing in `dashboard-react/src/` sends `conversation_id`, so the handler always forwards an empty string. And an unconfigured `NL_PIPELINE_BUILDER_AGENT_ENDPOINT` returns `200` with an explanatory `response` string.

**Payload (proposal flow):**
```json
{
  "message": "Load customer data from s3://my-bucket/customers/ into an Iceberg table partitioned by region",
  "conversation_id": "conv-abc123"
}
```

**Output (proposal) — agent-level; the API endpoint returns only `{response, message}`:**
```json
{
  "status": "proposal",
  "reasoning_steps": [
    "Parsing intent from: \"Load customer data...\"",
    "Scanning s3://my-bucket/customers/...",
    "Found 3 csv files",
    "Proposing target schema based on source data...",
    "Generated 8 field mappings"
  ],
  "proposal": {
    "pipeline_name": "customer-data-pipeline",
    "source": {"type": "s3", "location": "s3://my-bucket/customers/", "format": "csv", "files_found": 3},
    "target": {"type": "s3_tables", "table_name": "customers", "partitioning": ["region"]},
    "schema": {"fields": [...]},
    "mappings": [...]
  },
  "conversation_id": "conv-abc123"
}
```

**Key Functions:**

| Function | Purpose |
|----------|---------|
| `parse_intent(description)` | Strands Agent extracts source_location, target_type, table_name, partitioning, custom_transforms |
| `discover_source(s3_uri, source_format)` | Lists S3 prefix, picks representative file, extracts schema from first rows. Reads only the bucket named by `SOURCE_BUCKET`; any other bucket, and every read when it is unset, is refused before an S3 call |
| `propose_target_schema(source_schema, description, target_type)` | Strands Agent proposes optimized target schema with normalized field names |
| `generate_mappings(source_schema, target_schema, description)` | Strands Agent generates field mappings with transform expressions and confidence |
| `build_pipeline_config(intent, source_info, target_schema, mappings)` | Assembles full pipeline config for user review |

**Format Detection:** Supports CSV, JSON, JSONL, TSV, Parquet (extension-based). Type inference from sample values: integer, double, date, boolean, string.

**Dashboard UI:** `/chat` page with a Cloudscape chat interface, **request/response, not streamed** — `hooks/use-chat-agent.ts` is a plain `apiClient.post('/chat', { message })` with no WebSocket. The reasoning steps and the proposal card do render, because the endpoint returns them. `PipelineProposalCard` is **read-only**: it holds no state and renders no `Input`, so nothing on it can be edited. Its "Create Pipeline" button calls `POST /pipelines`, the only way a pipeline is created, per the note under **Trigger**. The route is registered at `/chat` but `NAV_ITEMS` in `App.tsx` has no Chat entry, so the page is reachable only by typing the URL. [DASHBOARD.md](DASHBOARD.md#chat-page-chat) carries the UI detail.

---

## Configuration

### Environment Variables (All Agents)

| Variable | Default | Purpose |
|----------|---------|---------|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Bedrock model for Strands Agent |
| `STATE_TABLE` | `agentic_data_ingestion_state` | DynamoDB table for agent results |
| `AUDIT_TABLE` | `agentic_data_ingestion_audit_log` | DynamoDB table for audit entries |
| `AGENT_TRACES_TABLE` | `agentic_data_ingestion_agent_traces` | DynamoDB table for trace data (P13a) |

### Agent-Specific Environment Variables

| Agent | Variable | Purpose |
|-------|----------|---------|
| Self-Healing | `STATE_MACHINE_ARN` | Step Functions ARN for re-triggering |
| Self-Healing | `PIPELINES_TABLE` | Pipeline record, read to build the re-run's trigger input and to evaluate the partial-write guard |

The container also receives `MAPPINGS_TABLE` and `GENERATED_CODE_TABLE`, because every agent container gets the same base environment (`agentContainerEnv` in `deploy-cdk/lib/agentcore-stack.ts`). The self-healing agent reads neither. They served the in-agent code regeneration that P28 R9.6 removed; code is now regenerated by the pipeline's own `create_code` step on the re-run.
| Complex Mapping | `MAPPING_MEMORY_ID` | AgentCore Memory resource ID |
| Complex Mapping | `COMPLEX_MAPPING_AGENT_ENDPOINT` | Set on the **caller**, the schema mapping Lambda, not on the agent runtime (`compute-stack.ts:607`). Unset or empty means an eligible run skips the agent silently at INFO, still reporting `complex_mapping_eligible: true` (see §2) |
| Data Quality | `ATHENA_RESULTS_BUCKET` | S3 bucket for Athena query results |
| Cost Optimization | `AUTO_APPLY_ENABLED` | `true` to enable automatic low-risk changes. **Set by no CDK stack — unset on both the agent runtime and the dashboard API Lambda, so the system is recommend-only today.** Enabling it requires setting it in both places |
| NL Pipeline Builder | `SOURCE_BUCKET` | The only bucket source discovery may read; unset refuses every read |

### Thresholds

| Threshold | Default | Agent | Purpose |
|-----------|---------|-------|---------|
| Mapping confidence | 0.7 | Complex Mapping | Below this makes the agent eligible; invocation also requires `COMPLEX_MAPPING_AGENT_ENDPOINT` (see §2) |
| Null rate | 0.05 (5%) | Data Quality | Above this triggers a warning |
| DPU utilization | 0.4 (40%) | Cost Optimization | Below this triggers downsize recommendation |
| Idle days | 30 | Cost Optimization | No runs in this period triggers idle recommendation |
| Freshness max age | 24 hours | Data Quality | Older than this triggers freshness failure |

---

## Deployment

### Build and Deploy All Agents

```bash
cd agentic_data_ingestion/deploy-cdk
npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never
```

This builds all 9 agent Docker images and deploys them as AgentCore Runtime resources.

### Deploy EventBridge Rules

```bash
npx cdk deploy AgenticDataIngestion-Orchestration-dev --require-approval never
```

This stack creates eleven EventBridge rules. Re-derive that rather than trusting the number here, and note that two construct spellings are in play:

```bash
grep -cE 'new (events|cdk\.aws_events)\.Rule\(' deploy-cdk/lib/orchestration-stack.ts
```

Ten use the `events.Rule` alias; `SfnTerminalLockRelease` (`orchestration-stack.ts:1149`) uses `cdk.aws_events.Rule`, so a pattern matching only `new events.Rule(` undercounts by one. Eight of the eleven carry pipeline orchestration and have nothing to do with agents: approval status change and timeout, MCP operations, Glue job state change and monitoring, discovery cleanup, S3 upload triggering, and Step Functions lock release.

**Three are the agent-facing block**, labelled Rule 1 / Rule 2 / Rule 3 in the source, and of those **one reaches an agent**: the weekly `CostOptimizationScheduleRule` (`:2188`) targets the agent dispatch Lambda. The two Glue-state rules, `GlueJobFailureSelfHealingRule` (`:2119`) and `GlueJobSuccessDataQualityRule` (`:2164`), publish notifications to the critical alerts SNS topic and invoke no agent. Automatic self-healing does not come from this stack at all, it comes from the `job_completion` Lambda in the Compute stack.

### Deploy Dashboard API Endpoints

```bash
npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never
```

This deploys the new API endpoints (`/api/chat`, `/api/cost-recommendations`, etc.) in the dashboard API Lambda.

### Local Testing

Agent unit tests are in `tests/unit/agents/<agent_name>/`:

```bash
cd agentic_data_ingestion
make test-fast  # Runs all unit + contract tests including agent tests
```

---

## MCP Tool Allowlist per Agent (P28 R16.3)

Which LLM agent can reach which MCP tools, and why. The allowlist is enforced in three layers:

1. **Endpoint wiring** — an agent can only reach the MCP servers whose endpoints are in its environment (`deploy-cdk/lib/compute-stack.ts` / `agentcore-stack.ts`). No endpoint, no tools.
2. **Server-side registration** — each focused server registers only its profile's tools, write/sensitive tools are env-gated default-False (`MCP_ALLOW_WRITE` / `MCP_ALLOW_SENSITIVE_DATA_ACCESS`, pinned to `false` per-runtime in `deploy-cdk/lib/agentcore-stack.ts`), and IAM-mutation tools are never registered (P28 R16.2). See `mcp/README.md`.
3. **Client-side filtering** — `shared/strands_mcp_factory.py` accepts a `tool_allowlist` parameter for finer per-call filtering when needed.

| Agent / LLM surface | MCP servers reachable | Effective tool surface | Writes through MCP? |
|---------------------|-----------------------|------------------------|---------------------|
| create_glue_pipeline (worker-recommendation agent) | codegen + analytics | All 8 codegen tools: `manage_aws_glue_jobs`, `manage_aws_glue_sessions`, `manage_aws_glue_statements`, `manage_aws_glue_databases`, `manage_aws_glue_tables`, `manage_aws_glue_catalog`, `manage_aws_glue_connections`, `manage_aws_glue_partitions`. All 8 analytics tools: `manage_aws_athena_query_executions`, `manage_aws_athena_named_queries`, `manage_aws_athena_data_catalogs`, `manage_aws_athena_databases_and_tables`, `manage_aws_athena_workgroups`, `manage_aws_emr_clusters`, `manage_aws_emr_ec2_steps`, `manage_aws_emr_ec2_instances`. Each is a multiplexer taking an `operation` argument, and mutating operations inside them are refused while `MCP_ALLOW_WRITE=false` (see [MCP-INTEGRATION.md](MCP-INTEGRATION.md#tool-naming-coarse-multiplexers-not-one-tool-per-operation)) | No — reads/describes only; `create_glue_database`/`create_glue_job` run in Lambda code via boto3 under the Lambda's IAM role |
| create_code (AI transform generation) | none | — (Bedrock via AgentCore proxy; `AGENTCORE_ENDPOINT` env var present but unused) | No |
| test_code (AI test analysis) | none, as deployed | — (Bedrock via AgentCore proxy). It holds the codegen server's URL as `AGENTCORE_ENDPOINT` (`compute-stack.ts:673`) and does contain a live MCP call (`lambda/agentic_data_ingestion_test_code/lambda_function.py:3259`), but that line is unreachable in deployment: `USE_AGENTCORE_RUNTIME='true'` is set, and the handler returns at `:3064-3071` before it. Reading "none" here as "no MCP code exists" would be wrong; the endpoint wiring is one env var away from live | No |
| create_infrastructure | none | — (provisions S3 Tables/Glue via boto3 directly) | No |
| Self-Healing Agent | none | — (boto3 + Bedrock reasoning only) | No |
| Complex Mapping Agent | none | — (S3 sample reads + AgentCore Memory + Bedrock) | No |
| Data Quality Agent | none | — (Athena via boto3, identifier-validated) | No |
| Cost Optimization Agent | none | — (CloudWatch/Glue metrics via boto3) | No |
| NL Pipeline Builder | none | — (S3 discovery + DynamoDB via boto3) | No |

Unreachable-by-design: the infra, s3_tables, and data_processing MCP runtimes are deployed but wired to no agent; their write tools are additionally absent (s3_tables registration-gates them; infra/data_processing never register IAM mutation). A future consumer must add the endpoint env var, flip the runtime's flag in CDK with a rationale, and extend this table.

## Related Documentation

- [Architecture](ARCHITECTURE.md) — System overview and agentic capabilities section
- [Dashboard](DASHBOARD.md) — UI pages and components for agent features
- [MCP Integration](MCP-INTEGRATION.md) — MCP server profiles used by agents
- [Testing](TESTING.md) — Test structure and agent test patterns
