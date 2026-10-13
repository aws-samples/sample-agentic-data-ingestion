# Agentic Data Ingestion — Well-Architected Framework Review

This document evaluates the Agentic Data Ingestion architecture against the six pillars of the AWS Well-Architected Framework. Agentic Data Ingestion is an AI-powered ETL pipeline automation system that uses Amazon Bedrock, Strands Agents, and Model Context Protocol (MCP) to generate, test, and deploy AWS Glue ETL jobs with human-in-the-loop approval gates. Those gates are on by default and operator-configurable per pipeline; see **Human-in-the-loop approval gates** under Security for what turning them off removes.

---

## 1. Operational Excellence

### Design Principles Applied

**Infrastructure as Code.** All infrastructure is defined in AWS CDK v2 TypeScript across 8 stacks (Foundation, Data, Auth, AgentCore, Compute, Orchestration, EdgeWaf, Hosting) — the WebSocket API is defined inline in the Compute stack, and the CLOUDFRONT-scope WAF in the EdgeWaf stack is pinned to us-east-1 and wired into Hosting via cross-region references. An optional, context-gated TestFixtures stack provisions JDBC test databases. No manual resource creation or CloudFormation templates outside CDK. Stack dependencies are explicit and deploy in deterministic order.

**Observability built in, with stated exceptions.** The pipeline-step, dashboard, and WebSocket Lambdas in the Compute stack are instrumented with AWS Distro for OpenTelemetry (ADOT) and use AWS Lambda Powertools for structured logging, metrics, and tracing. Coverage is not universal, and the gaps fall into three groups:

- **Schema extraction** carries no ADOT, as a deliberate hold pinned by a CDK assertion (`deploy-cdk/test/p46-schema-extract-layers.test.ts`). The function already carries a large PyArrow layer, and the assertion exists so that adding ADOT back is a conscious change.
- **The WebSocket `$connect` authorizer** carries no ADOT.
- **The Orchestration stack's small EventBridge and Step Functions helpers** carry no ADOT: the failure recorder, lock release, the approval-status, Glue-job-state and MCP-metrics updaters, and the queue-loss recorder. All of them except the queue-loss recorder also skip Powertools and log through the Python standard library; lock release and the three updaters are inline code in the stack definition. The pipeline trigger is the one Orchestration function with ADOT.

Which layers each function carries is declared in `deploy-cdk/lib/compute-stack.ts` and `deploy-cdk/lib/orchestration-stack.ts`; treat those as the source of truth over this summary. AgentCore Runtime containers use OpenTelemetry auto-instrumentation via `opentelemetry-instrument`. The `AgentTracePlugin` captures per-invocation model calls, tool calls, token usage, and estimated cost into a dedicated `agentic_data_ingestion_agent_traces` DynamoDB table with 90-day TTL.

**Automated pipeline orchestration.** AWS Step Functions orchestrates the 10-step pipeline with built-in error handling, retry logic (3 retries with exponential backoff for Lambda service exceptions), and catch blocks that route failures to a dedicated failure recorder Lambda. Pipeline locks prevent concurrent executions on the same source prefix. The final step runs the Glue ETL job with inline data quality evaluation (EvaluateDataQuality transform) that writes DQDL rule results to DynamoDB.

**Real-time monitoring.** A CloudWatch dashboard (`AgenticDataIngestion-Pipeline-Monitoring`) tracks Lambda invocations, errors, duration, Step Functions execution metrics, Glue job operations, approval operations, and cost tracking. Five CloudWatch alarms (pipeline error rate, Glue job failures, SQS message age, approval timeouts, and Glue job monitor DLQ depth) publish to the critical-alerts SNS topic. The pipeline error-rate alarm fires when more than 20% of the runs that finished in a 15-minute window failed; approval rejections do not count as failures. Each finished run publishes one datapoint (100 from the failure recorder, 0 from the completion handler), so a window with no finished run has no data and does not alarm. The wiring is covered by unit, contract and CDK tests, but no deployed alarm has yet been seen to fire on a controlled failure. **Email delivery is optional.** The topic, and the separate approval-notifications topic, have an email subscription only when you deploy with `-c notificationEmail=you@example.com`. Without it the topics still exist and still receive every alarm, but nothing is subscribed, so no one is notified. Set the email, or subscribe your own endpoint, before you rely on these alarms.

**Self-healing capabilities.** The self-healing agent is invoked automatically when a Glue job fails. It classifies the error by pattern and acts on the class:

- **Out of memory:** it upgrades the job's worker type one step (G.1X → G.2X → G.4X → G.8X) and re-runs the pipeline.
- **Schema mismatch and type-cast errors:** it re-runs the standard pipeline, so the `create_code` step regenerates the transform from the current approved mappings and `test_code` re-validates it before deployment. The agent does not write code itself.
- **Permission errors, SQL the database rejected, and unrecognised errors:** it does not re-run. It records the diagnosis and flags the run for review, because a re-run cannot change the outcome. A rejected SQL statement is deterministic output of code generation and would fail identically.

A partial-write guard can also decline a re-run for targets where a second write is unsafe, and the re-run's outcome is recorded against the original run. This shortens recovery for the error classes that a re-run or a larger worker can actually fix. It does not remove the operator from the others.

**Audit trail.** Every agent action, approval decision, and pipeline state change is recorded in the `agentic_data_ingestion_audit_log` DynamoDB table with structured metadata (agent type, action, resource ID, timestamp). The dashboard exposes audit history per pipeline.

### Recommendations for Production

- Enable AWS CloudTrail for API-level audit logging across all services.
- Review log retention against your policy. Lambda, REST API execution and Step Functions log groups keep logs for one month (`deploy-cdk/lib/log-retention.ts`); the log groups Glue and AgentCore create for themselves have no retention set and keep logs indefinitely.
- Add runbook documentation for common operational scenarios (Glue job stuck, approval timeout, AgentCore container restart).

---

## 2. Security

### Design Principles Applied

**Least-privilege IAM.** Each Lambda function has a dedicated IAM role scoped to the specific DynamoDB tables, S3 buckets, and AWS services it needs. For example, the file validation Lambda can only read from the source bucket and write to the state table — it cannot access Glue or Bedrock. The foundation stack defines one role per Lambda function group and one per AgentCore agent runtime, plus a read-only role shared by the MCP server runtimes; only the job-builder runtime can create IAM roles, and only the four pipeline-step runtimes can complete a Step Functions task.

**Encryption at rest.** **Every** S3 bucket uses S3-managed encryption (SSE-S3) and **every** DynamoDB table uses AWS-managed encryption — the coverage claim is universal, so it holds whatever the inventory count is (counts and derivations: [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory)). The ECR repository has image scanning enabled on push.

**Encryption in transit.** All service-to-service communication uses HTTPS/TLS. API Gateway enforces HTTPS. WebSocket API uses WSS. AgentCore Runtime containers communicate over HTTPS endpoints.

**Authentication and edge protection.** The React dashboard is gated by Amazon Cognito (User Pool with Hosted UI, OIDC Authorization Code flow + PKCE via `react-oidc-context`). The REST API Gateway uses a Cognito User Pools authorizer; the WebSocket API uses a Lambda authorizer on `$connect` that validates Cognito JWTs passed via the `?token=` query string. Two AWS WAF web ACLs protect the edge: a CLOUDFRONT-scope WAF (managed rules + rate limiting, deployed in the us-east-1 EdgeWaf stack) on the CloudFront distribution, and a REGIONAL-scope WAF on the API stage that enforces CloudFront-only access via an `X-Origin-Verify` secret header (stored in Secrets Manager). Direct `execute-api` access returns 403.

**Secrets Manager for credentials.** Secrets Manager is in use today, in two load-bearing places. **JDBC credentials** are only ever referenced by secret ARN — `shared/jdbc_helpers.py` writes the ARN to the Glue connection's `SECRET_ID` property and mirrors it on the `adi:secret_arn` resource tag, and the readers in `handlers/pipelines_jdbc.py` and `create_glue_pipeline/job_arguments.py` resolve credentials from there. Plaintext user and password never appear in Glue job arguments or `ConnectionProperties`; that is a standing invariant, because job arguments are visible in the Glue console, CloudTrail, and job-run history, and this path leaked a target database credential once. The ARN's presence in both the connection property and the tag is deliberate rather than redundant: Glue consults `SECRET_ID` at job runtime and never consults tags, and a connection carrying only a URL is rejected outright, so storing the ARN in the tag alone broke all JDBC provisioning once already. Do not clean up either copy. See [JDBC Source](JDBC_SOURCE.md) for the full target credential contract. The **`X-Origin-Verify` origin secret** that enforces CloudFront-only API access is a generated Secrets Manager secret created in the Compute stack (`deploy-cdk/lib/compute-stack.ts`), read by the Hosting stack and injected as a CloudFront custom origin header (`deploy-cdk/lib/hosting-stack.ts`) — see *Authentication and edge protection* above. No API keys or credentials are hardcoded anywhere; a future third-party integration uses this same mechanism rather than introducing one.

**S3 public access blocked.** Every S3 bucket is configured with `BlockPublicAccess.BLOCK_ALL` — no public read, write, or ACL access is possible.

**Human-in-the-loop approval gates, on by default and operator-configurable.** By default, AI-generated schema mappings and code are not deployed until a human approves them. Step Functions uses `waitForTaskToken` to pause execution until someone approves or rejects the output in the dashboard.

This is a default rather than a guarantee. The code gate is decided in the `test_code` step, and it asks for a human if **any** of these holds: the pipeline's `approval_config.code_generation_enabled` is true, the generated code's checks failed, the review verdict is a fail, or the code was regenerated during the run. An operator can set `code_generation_enabled` to `false` per pipeline. That removes the human only from runs whose code passes cleanly; a failing run still stops for review. Both halves fail closed. A missing or non-boolean setting counts as true, and the Step Functions Choice skips the gate only when `approval_required` is an explicit boolean `false`. Schema-mapping approval is decided separately, by mapping confidence and schema change; [WORKFLOW.md](WORKFLOW.md) describes it.

The approval-config API also accepts an `auto_approval_thresholds.code_quality_score`. **Nothing reads it.** The score it was meant to compare against was removed, so setting it changes no approval decision.

The approval gate is the primary control between model output and execution against your data, so keep it enabled for any pipeline that reads data you care about. [SECURITY.md](../SECURITY.md) describes the threat model, including what the generated code's IAM role can reach once approved.

**S3 versioning enabled.** All buckets have versioning enabled, providing an audit trail of object changes and the ability to recover from accidental overwrites or deletions. The generated code bucket has a 90-day noncurrent version expiration lifecycle rule.

**Security scanning.** Two layers, and it is worth being precise about where each runs, because this repository ships **no CI workflows** — there is no `.github/workflows/`, so nothing here scans automatically on push or on a pull request.

- **Locally, on demand.** `make security` runs Bandit over `lambda/`, `shared/`, `mcp/` and `agentcore/` (configured in `pyproject.toml`), and Ruff enforces the `flake8-bandit` (`S`) rule set as part of `make lint`, whose scope is `lambda/`, `shared/`, `mcp/` and `tests/`. Both are **advisory**: their recipes end in `|| echo`, so neither can fail `make check`. They are a signal to read, not a gate that blocks.
- **In AWS, automatically.** ECR image scanning is genuinely automatic — `imageScanOnPush: true` on the repository in `deploy-cdk/lib/foundation-stack.ts` means every container image is scanned for known vulnerabilities when it is pushed, independent of any local tooling.

Adopting this pattern in production means wiring the first layer into your own CI and deciding which findings block a merge. See [Recommendations for Production](#recommendations-for-production) below.

### Recommendations for Production

- Replace S3-managed encryption (SSE-S3) with AWS KMS customer-managed keys (CMK) for buckets containing sensitive data.
- Enable VPC endpoints for DynamoDB, S3, and Bedrock to keep traffic off the public internet.

---

## 3. Reliability

### Design Principles Applied

**Serverless-first architecture.** Lambda, DynamoDB, Step Functions, EventBridge, SQS, S3, and API Gateway are all managed services that provide automatic multi-AZ redundancy, scaling, and failover without infrastructure management.

**DynamoDB point-in-time recovery.** The 13 retained tables (state, pipelines, mappings, generated code, test results, approvals, audit log, agent traces and the rest of the shared `tableDefaults` in `deploy-cdk/lib/data-stack.ts`) have point-in-time recovery enabled, allowing restoration to any second within the last 35 days. The four TTL-backed ephemeral tables do not. No restore has been exercised against this sample; see the objectives in [ARCHITECTURE.md § Disaster Recovery](ARCHITECTURE.md#disaster-recovery).

**DynamoDB Streams for event propagation.** The state table, generated code table, test results table, and executions table use DynamoDB Streams to propagate changes to downstream consumers (WebSocket broadcast, execution summary updates) without polling.

**Dead letter queue.** A dedicated SQS dead letter queue captures failed EventBridge deliveries, preventing message loss during transient failures.

**Pipeline lock mechanism.** A `pipeline_locks` DynamoDB table prevents concurrent pipeline executions on the same source prefix. Locks are released on both success and failure paths (including Step Functions ABORTED/TIMED_OUT states via an EventBridge rule), and every release is conditional on the lock's recorded holder, so a late event from one execution cannot release a lock the next execution acquired. The lock's TTL (11 days) is a backstop sized above the state machine's 10-day execution timeout, not a release mechanism; see `docs/DYNAMODB-TABLES.md` § 12.

**Retry with backoff.** Every Lambda invocation in the Step Functions pipeline has a retry configuration: 3 attempts with a 5-second initial interval and 2x backoff rate for transient step failures and Lambda service exceptions. The Glue job execution step waits up to the pipeline's `max_job_duration_seconds`, 6 hours when the pipeline sets none (`DEFAULT_MAX_JOB_DURATION_SECONDS` in `deploy-cdk/lib/orchestration-stack.ts`); no producer sets a per-pipeline value today, so every run uses the default. The whole execution is capped at 10 days (`EXECUTION_TIMEOUT_SECONDS`).

**Error handling.** Step Functions `States.ALL` catch blocks route task failures and task timeouts to a failure recorder Lambda that persists error details to DynamoDB before releasing the pipeline lock. `States.ALL` does not catch `States.Runtime` errors, which is why the state machine's Choice states are guarded by `IsPresent` conjuncts rather than relying on a catch. Lock release is best-effort and reports `released: false` when it cannot delete; a lock that no release path removes expires at the TTL, so "no dangling lock" holds up to 11 days, not instantly.

**Data table retention.** Primary data tables (state, pipelines, mappings) use `RemovalPolicy.RETAIN` to prevent accidental deletion during CDK stack updates. Transient tables (task tokens, WebSocket connections) use `DESTROY` since they're ephemeral.

**TTL-based cleanup.** Approvals (30-day record TTL; task tokens 7-day TTL, deliberately longer than the 72-hour approval window so a token outlives the wait rather than racing it), notifications, WebSocket connections, agent traces (90-day TTL), and execution summaries use DynamoDB TTL to automatically expire stale records without manual cleanup. The Python-side windows and TTLs are defined once in `shared/config.py`; the one exception is the **approval gate window**, which is owned by `APPROVAL_TIMEOUT_SECONDS` in `deploy-cdk/lib/orchestration-stack.ts` because that timeout is what actually terminates the wait — `shared/config.py` mirrors it, and `tests/contracts/test_approval_window_cross_language.py` asserts the two agree.

### Recommendations for Production

- Enable DynamoDB global tables for multi-region disaster recovery if cross-region availability is required.
- Add circuit breaker patterns for Bedrock API calls (currently retried by Strands Agent internally, but no explicit circuit breaker).
- Configure S3 cross-region replication for the source and generated code buckets.
- Add health check alarms for AgentCore Runtime container availability.

---

## 4. Performance Efficiency

### Design Principles Applied

**Right-sized Lambda functions.** Lambda memory is tuned per function based on workload: 128 MB for lightweight WebSocket handlers, 256 MB for approval and execution updaters, 512 MB for file validation and schema operations, 1024 MB for schema mapping and Glue pipeline creation, and 2048 MB for AI-intensive code generation and testing. Timeouts range from 10 seconds (WebSocket) to 15 minutes (code generation).

**ARM64 architecture.** All Lambda functions and AgentCore Runtime containers use ARM64 (Graviton) processors, providing up to 20% better price-performance compared to x86_64 for the same workload.

**AgentCore Runtime for AI workloads.** AI-intensive operations (code generation, testing, Glue pipeline creation, infrastructure provisioning) run on long-lived AgentCore Runtime containers instead of Lambda. A container that is already running keeps its MCP server connections between calls, which avoids repeated connection setup. That reduces start-up latency rather than removing it: a call that needs a new container still pays its start-up time, and this sample has no measurement of either.

**DynamoDB on-demand capacity.** All tables use PAY_PER_REQUEST billing mode, which scales read/write capacity with traffic and removes capacity planning. It does not remove limits: on-demand tables still have per-table and per-partition throughput limits and can throttle a sudden spike well above previous traffic. The AWS SDK's default retries absorb short bursts; a sustained one surfaces as errors. No load test has been run against this sample.

**Focused MCP server profiles.** Instead of a single monolithic MCP server with 32+ tools, the architecture uses focused server profiles (codegen ~10 tools, analytics ~8 tools, infra ~12 tools) so each AI agent only loads the tools it needs, reducing prompt size and improving model response quality.

**SQS-based pipeline triggering.** S3 upload events flow through EventBridge → SQS → Lambda trigger with `batchSize: 1` and `maximumConcurrency: 5`. This decouples file uploads from pipeline execution, prevents thundering herd on bulk uploads, and provides natural backpressure.

**WebSocket for real-time updates.** The dashboard uses WebSocket API Gateway for real-time pipeline status updates instead of polling, reducing API Gateway invocations and providing sub-second update latency.

### Recommendations for Production

- Enable Lambda Provisioned Concurrency for the dashboard API Lambda if consistent sub-100ms response times are required.
- Consider Bedrock Provisioned Throughput for predictable AI workloads to avoid on-demand throttling.
- Add CloudFront distribution in front of API Gateway for edge caching of read-heavy dashboard API calls.
- Profile Lambda memory settings with AWS Lambda Power Tuning to find optimal cost-performance points.

---

## 5. Cost Optimization

### Design Principles Applied

**Serverless pay-per-use.** The entire architecture uses serverless services (Lambda, DynamoDB on-demand, Step Functions, EventBridge, SQS, API Gateway, S3) that charge only for actual usage. During periods of inactivity, the system incurs near-zero compute costs.

**S3 lifecycle policies.** Test outputs expire after 30 days. Generated code noncurrent versions expire after 90 days. Checkpoint data expires after 7 days. These policies prevent unbounded storage growth for transient artifacts.

**DynamoDB TTL for automatic cleanup.** Agent traces (90 days), approval records (30 days), task tokens (7 days), and execution summaries use TTL-based expiration, eliminating the need for scheduled cleanup jobs and reducing storage costs over time.

**Cost-aware AI model selection.** The `AgentTracePlugin` tracks token usage and estimated cost per invocation using configurable per-model pricing (Claude Sonnet at $0.003/$0.015 per 1K tokens, Nova Pro at $0.0008/$0.0032, Nova Micro at $0.000035/$0.00014). The dashboard displays token usage metrics and cost breakdowns, enabling teams to choose cost-appropriate models per use case.

**Built-in cost optimization agent.** The cost optimization agent runs weekly to analyze Glue job DPU utilization, detect over-provisioned jobs (utilization < 40%), identify idle pipelines (no runs in 30+ days), and generate right-sizing recommendations with projected monthly savings.

**It is recommend-only as shipped, and nothing is applied automatically.** Auto-apply is gated on `AUTO_APPLY_ENABLED`, which **no CDK stack sets** — on neither the agent runtime nor the dashboard API Lambda. `POST /api/cost-recommendations/{id}/apply` therefore returns `200` with `applied: false` and a message saying so. That is a deliberate posture rather than a gap, and enabling it means setting the flag in *both* places: on the runtime, which holds the permission to mutate a Glue job, and on the API Lambda, so the HTTP response stays honest. See [AGENTS.md § 4](AGENTS.md).

**ARM64 for cost savings.** All compute runs on Graviton (ARM64), which is approximately 20% cheaper than equivalent x86_64 for Lambda and container workloads.

### Cost Estimate (Typical Workload)

An illustrative, undated estimate for a workload processing 10 pipelines with 5 runs per day, not a measurement. [COST.md](COST.md) is the maintained cost model, with an idle baseline and per-run figures; read it in preference to this table. Two limits of the table below:

- **It leaves out AWS Glue**, which runs on every pipeline run: the ETL job (COST.md: ~$0.05–0.35 per run) and the verification interactive session in the testing step (~$0.027 per run, measured). The total excludes both.
- **The volumes in the Notes column are the assumptions the figures were written against, and some undercount.** At about 20 state transitions per run (COST.md), 50 runs a day is about 1,000 transitions a day, not the ~50 the Step Functions row states. The dollar figures have not been recomputed.

| Service | Monthly Estimate | Notes |
|---------|-----------------|-------|
| Lambda | $15–30 | ~150 invocations/day. Function count: see [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory) |
| DynamoDB | $5–15 | On-demand, ~50K reads + 10K writes/day |
| Step Functions | $5–10 | ~50 state transitions/day |
| S3 | $2–5 | Source files, generated code, test outputs |
| Bedrock | $50–200 | Varies by model and token volume |
| AgentCore Runtime | $20–50 | usage-based, across **all** runtimes. The agent containers and the MCP server runtimes bill the same way, so an estimate covering only the agents understates it. Counts: see [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory) |
| API Gateway + WebSocket | $2–5 | Dashboard API calls |
| EventBridge + SQS | <$1 | Event routing |
| AWS WAF (two web ACLs) | $14–20 | CLOUDFRONT-scope in us-east-1 plus REGIONAL on the API stage. **Bills at zero traffic** |
| AWS Glue (ETL job + verification session) | not estimated | Per-run figures in [COST.md](COST.md#per-pipeline-run-variable-cost); excluded from the total |
| **Total, excluding Glue** | **$114–335/month** | Scales linearly with pipeline count, apart from the WAF floor |

> **Most of this scales to zero; the WAF does not.** The two web ACLs are charged per ACL and per
> rule, so they bill whether or not a pipeline runs, and they are the dominant cost of a deployed
> but idle environment. Everything else in the table above is consumption-based and approaches zero
> when nothing is running, including AgentCore Runtime. [COST.md](COST.md) carries the full idle
> baseline per component; read it before leaving an evaluation environment deployed.

### Recommendations for Production

- Use Bedrock prompt caching for repeated context across invocations to reduce token costs.
- Consider Savings Plans for Lambda if usage is predictable and sustained.
- Monitor the cost optimization agent's recommendations and apply them regularly.
- Use S3 Intelligent-Tiering for source file buckets if access patterns are unpredictable.

---

## 6. Sustainability

### Design Principles Applied

**Serverless eliminates idle resources.** Lambda, DynamoDB on-demand, Step Functions, EventBridge, SQS, and API Gateway do not consume compute resources during periods of inactivity. There are no always-on EC2 instances, ECS tasks, or provisioned database clusters. Resources are allocated only during active pipeline execution and de-provisioned immediately after.

**Graviton processors.** All Lambda functions and AgentCore containers run on ARM64 (Graviton) processors, which deliver better performance per watt compared to x86_64 equivalents, reducing the energy footprint of every invocation.

**Automatic data expiration.** TTL-based cleanup on DynamoDB tables and S3 lifecycle policies ensure that transient data (test outputs, checkpoints, expired approvals, old agent traces) is automatically deleted, preventing unnecessary storage consumption.

**Right-sized compute.** Lambda memory is tuned per function (128 MB to 2048 MB) based on actual workload requirements rather than using a one-size-fits-all configuration. This avoids over-provisioning compute resources for lightweight operations.

**Efficient AI usage.** The focused MCP server profiles ensure AI agents only load relevant tools, reducing prompt token counts and therefore the compute required per Bedrock invocation. The mapping memory system reuses historical mapping decisions, avoiding redundant AI invocations for previously-seen schema patterns.

**Idle pipeline detection.** The cost optimization agent identifies pipelines with no runs in 30+ days and recommends archiving, preventing resource allocation for abandoned workloads.

### Recommendations for Production

- Track and report on the carbon footprint using AWS Customer Carbon Footprint Tool.
- Evaluate whether smaller Bedrock models (Nova Micro vs Claude Sonnet) can handle simpler pipeline steps to reduce compute per invocation.
- Implement request deduplication to prevent redundant pipeline executions from duplicate S3 upload events.

---

## Summary

| Pillar | Maturity | Key Strengths |
|--------|----------|---------------|
| Operational Excellence | High | Full IaC, OTEL instrumentation on most Lambdas (exceptions listed above), self-healing agents, CloudWatch dashboard, audit trail. Alarm email is opt-in; Lambda, API and Step Functions logs are kept one month, while Glue and AgentCore log groups have no retention set. |
| Security | Medium-High | Per-function IAM roles, encryption at rest/transit, S3 public access blocked, approval gates (on by default, configurable per pipeline), Cognito auth, WAF (CLOUDFRONT + REGIONAL), CloudFront-only API access. Needs KMS CMK for sensitive data in production. |
| Reliability | High | Serverless multi-AZ, PITR on the 13 retained DynamoDB tables, DLQ, pipeline locks, retry with backoff, graceful error handling. RTO/RPO are objectives; no restore or failover has been exercised |
| Performance Efficiency | High | ARM64, right-sized Lambda, AgentCore for AI workloads, focused MCP profiles, WebSocket real-time updates |
| Cost Optimization | High | Serverless pay-per-use, lifecycle policies, TTL cleanup, cost-aware model selection, built-in cost optimization agent |
| Sustainability | Medium-High | Serverless idle elimination, Graviton, data expiration, right-sized compute. Could improve with carbon tracking. |
