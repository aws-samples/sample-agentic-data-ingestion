# E2E Test Suite

The `tests/e2e/` layer runs against a **deployed** Agentic Data Ingestion
environment in your AWS account. Configure credentials via `AWS_PROFILE` (or
the default credential chain) and `AWS_REGION` to point at the account/region
where the stacks are deployed. It is **nightly / opt-in only** — never
per-commit.

## Two-Level Gating

All e2e tests are excluded from regular `make test-fast` / `make test-ci`
runs via the root conftest's `not e2e` default marker expression. To run:

### Gate 1: Real_AWS_Gate

```bash
make test-e2e
# Equivalent to: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true pytest tests/e2e/ -m e2e
```

Non-mutating tests (smoke verification, infra posture) run at this level.
Without valid AWS credentials the entire layer is session-skipped.

### Gate 2: Trigger_Opt_In

```bash
make test-e2e-trigger
# Equivalent to: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true AGENTIC_DATA_INGESTION_E2E_TRIGGER=true pytest tests/e2e/ -m e2e
```

Triggering/mutating tests (fresh pipeline runs, approval flows, target writes)
require this additional opt-in. These tests create real AWS resources and incur
cost — they must only run in scheduled nightly pipelines or developer opt-in.

## Cost_Budget Ceilings

Every polling helper receives a timeout from the `cost_budget` fixture to
prevent runaway cost:

| Scenario | Default | Env override |
|----------|---------|--------------|
| Standard run (CSV/Parquet/JSON → S3 Tables) | 420s | `E2E_DEFAULT_TIMEOUT` |
| JDBC target run (Aurora Postgres) | 900s | `E2E_JDBC_TIMEOUT` |

Tests exceeding the standard ceiling carry `@pytest.mark.slow`.
On timeout expiry, polling raises `TimeoutError`.

## Auto-Approve Investigation Finding

### Summary

The `always_approve_schema_changes=True` default in pipeline creation forces
every fresh run into `WaitForSchemaMappingApproval` — nothing auto-releases
it. This is **expected behavior given the (misleadingly named) flag**, not a
defect in the token mechanism.

### Root Cause

`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py::determine_approval_requirement`
returns `True` whenever the schema changed **and** `approval_config.always_approve_schema_changes`
is truthy. The `create_pipeline` handler writes that flag as `True` by default.

A `True` result routes the `CheckSchemaMappingApproval` Choice state **into**
`WaitForSchemaMappingApproval` (a `waitForTaskToken` gate with a 72-hour timeout),
and nothing in the pipeline path ever calls `send_task_success` autonomously.
So a fresh run **stalls** at the schema-mapping gate until an external approver
acts via the dashboard REST API.

### Classification

- **Category:** Expected behavior given flag semantics (not a token defect)
- **Misleading naming:** `always_approve_schema_changes` sounds like "auto-release",
  but actually means "always *require* approval on schema change"
- **Token plumbing:** Works correctly once an approver acts — the token is stored
  in `agentic_data_ingestion_task_tokens`, read-and-deleted by the approve endpoint, and used
  to call `send_task_success`
- **Tracking:** Naming/UX issue tracked separately, not fixed by this test feature

### Impact on E2E Tests

Keystone tests release the gate **explicitly** via the `ApprovalDriver` harness
fixture (`tests/e2e/_harness/approval.py`). The driver discovers the pending
approval via `GET /api/approvals/pending`, then posts `approve` or `reject`.

A no-approver run is possible by provisioning with
`approval_config.schema_mapping_enabled=False` (drives the Choice to
`.otherwise(CreateCode)`), but the default explicit-approval path is preferred
because it also exercises the REST approve flow needed by Phase T2 tests.

## WebSocket `$connect` Authorization Gap (Security Finding)

**Status:** Documented as a strict xfail in `tests/e2e/infra/test_security_posture.py::TestWebSocketAuth`

The WebSocket API's `$connect` route has `AuthorizationType=NONE` — no IAM or
Lambda authorizer is attached. This means any client can open a WebSocket
connection without authentication.

- **Location:** `tests/e2e/infra/test_security_posture.py::TestWebSocketAuth::test_connect_route_requires_auth`
- **Marker:** `@pytest.mark.xfail(strict=True)` — the test asserts auth is present,
  fails (confirming the gap still exists), and is marked as expected failure
- **Classification:** Security finding, NOT a functional test failure
- **Impact on WebSocket tests:** `tests/e2e/websocket/test_live_updates.py` connects
  without SigV4 signing because the endpoint has no auth requirement. This is the
  gap in action — once an authorizer is attached, the WebSocket tests will need to
  sign connections (and the xfail will flip to a pass)
- **Tracking:** Filed as a security gap, separate from the functional WebSocket
  coverage in Phase T3

## Harness Package (`_harness/`)

Shared test infrastructure lives in `tests/e2e/_harness/`:

- `gating.py` — `requires_trigger` and `cost_budget` fixtures
- `ledger.py` — `ResourceLedger` cleanup contract (LIFO, failure-safe)
- `provisioner.py` — `TestPipelineProvisioner` (throwaway pipelines)
- `approval.py` — `ApprovalDriver` (release waitForTaskToken gates)
- `verify.py` — `TargetVerifier` (Athena / Parquet / RDS Data API / fallback)
- `rest.py` — SigV4 dashboard REST client

All fixtures are registered in `tests/e2e/conftest.py`.

## AI Agent Coverage (`agents/`)

### Direct-Invoke Baseline

The `tests/e2e/agents/test_agents.py` module invokes the five AgentCore
runtimes **directly** (not via a full pipeline run):

| Agent | Runtime Name | Invocation Contract | Assertion |
|---|---|---|---|
| Self-Healing | `agentic_data_ingestion_agent_self_healing` | Failed execution context (`pipeline_id`, `error_message`, `job_name`) | Recovery attempt recorded (`error_classification` + `remediation_applied`) |
| Data Quality | `agentic_data_ingestion_agent_data_quality` | Pipeline + target info (`target_table_info`, `source_row_count`) | Quality assessment returned (checks / report) |
| Cost Optimization | `agentic_data_ingestion_agent_cost_optimization` | Empty payload (weekly scan) | Recommendations returned |
| NL Pipeline Builder | `agentic_data_ingestion_agent_nl_pipeline_builder` | Natural language description (`message`, `conversation_id`) | Pipeline definition / proposal produced |
| Complex Mapping | `agentic_data_ingestion_agent_complex_mapping` | Low-confidence payload (`source_schema`, `target_schema`, `sample_data_uri`, `confidence_threshold`) | Evidence-based mappings with confidence scores |

### Invocation Strategy

Tests attempt invocation in priority order:
1. **AgentCore Runtime API** — `bedrock-agentcore invoke_agent_runtime`
2. **Lambda fallback** — Direct Lambda invoke (`agentic_data_ingestion_{agent_suffix}`)
3. **Skip** — If neither path is reachable, the test skips gracefully

### Complex Mapping Agent — In-Pipeline Coverage (Future)

The complex mapping agent is **not yet wired into the pipeline**:
- `agentic_data_ingestion_schema_mapping/lambda_function.py` sets
  `complex_mapping_eligible=True` but the invocation is a `TODO`
- The baseline coverage exercises it via direct AgentCore Runtime invocation only

**Prerequisite:** The `complex-mapping-agent-integration` spec must be deployed
first. Once that integration ships, add a full low-confidence pipeline run to
`tests/e2e/agents/` (or `tests/e2e/runs/`) asserting:
1. The `complex_mapping` step ran within the execution
2. Evidence is present in the schema-mapping approval record
3. Questions were generated for low-confidence fields

## S3-Tables Verification Approach

### Chosen Strategy: Athena with Capability Detection + State-Table Fallback

The `TargetVerifier` uses a **capability-detection** pattern for S3 Tables
(Iceberg) targets. At runtime, it attempts a lightweight Athena probe query
(`SELECT 1`) against the expected S3 Tables catalog (`s3tablescatalog`) and
workgroup. Based on the result:

| Probe Result | Verification Path | Reads Rows? |
|---|---|---|
| Probe SUCCEEDED | Athena `SELECT COUNT(*)` + key query | **Yes** |
| Probe FAILED (catalog not found, federation error) | State-table `verify_target_data` | No — evidence only |
| Probe raises exception | State-table `verify_target_data` | No — evidence only |

### Athena / S3 Tables Federation Availability

**Current status: NOT available** in the deployed environment.

Investigation confirmed that:
- No S3 Tables catalog federation is wired in the account
- The only Athena usage in the repo is in `agentcore/data_quality_agent`
- The existing `verify_target_data` in `conftest.py` explicitly avoids
  Athena for this reason

The verifier's capability-detection pattern ensures tests degrade gracefully:
- When federation becomes available, the verifier automatically uses it
  (real row reads, full assertion)
- Until then, the State-table fallback verifies write evidence only
  (job_run_id present, no errors, completed_at set)
- A log warning is emitted whenever the fallback is used, stating clearly
  that target rows are NOT being read

### Per-Target Verification Methods

| Target Type | Backend | Mechanism | VPC Required? |
|---|---|---|---|
| `RDS_DATABASE` | RDS Data API | `execute_statement` via cluster ARN + secret ARN | No (HTTP endpoint) |
| `S3_PARQUET` | S3 + pyarrow | List objects, read Parquet files, count rows + check keys | No |
| `NEW_S3_TABLE_BUCKET` / `GLUE_CATALOG` | Athena (if available) | `start_query_execution` SELECT against catalog | No |
| Any (fallback) | DynamoDB State table | `verify_target_data` — RunPipeline evidence check | No |

### Row Assertions

The verifier asserts equality between:
1. **Row count** — verified rows must equal expected source rows
2. **Key values** — the set of `key_field` values in the target must exactly
   match the set from the uploaded source

A pass requires both checks to hold. The State-table fallback path skips these
assertions (it cannot read rows) and instead verifies evidence of a successful
write.
