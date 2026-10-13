# Cost Estimate

Approximate costs for deploying and running Agentic Data Ingestion, based on
us-west-2 public pricing at the time of writing. Actual costs depend on usage,
region, and current pricing — validate with the
[AWS Pricing Calculator](https://calculator.aws/) before relying on these
numbers.

**The estimates below carry no capture date**, so treat them as order-of-magnitude
rather than current. The one exception is the Glue interactive session figure in
[Verification against real source data](#verification-against-real-source-data-p44),
which traces to a specific measurement (p42 design C10) and is quoted rather than
re-derived.

## Monthly baseline (deployed, idle)

The solution is predominantly serverless and scales to zero. With no pipeline
runs, the recurring costs are:

| Component | Driver | Est. monthly (idle) |
|---|---|---|
| AWS WAF (CloudFront-scope, us-east-1) | 1 web ACL + managed rule groups + rate rule | ~$8–12 |
| AWS WAF (regional, API stage) | 1 web ACL + header rule | ~$6–8 |
| Secrets Manager | Origin-verify secret (+1 per JDBC pipeline) | ~$0.40/secret |
| CloudWatch | Alarms, dashboards, log storage (including the REST API access log, one-month retention) | ~$3–10 |
| S3 (11 buckets) | Storage of code/templates/artifacts (9 in Foundation + the dashboard bucket and the CloudFront access-log bucket in Hosting; access logs expire after 30 days) | <$1 at sample volumes |
| ECR | Container images for 14 AgentCore runtimes | ~$1–3 |
| DynamoDB (17 tables, on-demand) | Storage only when idle | <$1 at sample volumes |
| Cognito | Free tier covers small user counts | ~$0 |
| CloudFront | Per-request/transfer; negligible idle | ~$0 |
| AgentCore Runtime (14 runtimes) | Consumption-based; **$0 when idle** | ~$0 |
| Lambda / Step Functions / EventBridge | Per-invocation; $0 idle | ~$0 |

**Estimated idle baseline: roughly $20–35/month**, dominated by the two WAF
web ACLs.

## Per-pipeline-run variable cost

Each end-to-end run (upload → deployed Glue job → executed ETL) adds:

| Component | Driver | Est. per run |
|---|---|---|
| Amazon Bedrock | ~50K–200K tokens across schema mapping, code generation, testing, and job configuration | Claude Sonnet: ~$0.50–2.50 · Nova Pro: ~$0.10–0.50 · Nova Micro: <$0.05 |
| AWS Glue | Job run, 2× G.1X workers, 2–5 min typical for sample data | ~$0.05–0.35 (at $0.44/DPU-hour) |
| AgentCore Runtime | Agent execution seconds (CPU/memory-metered) for the 4 pipeline agents | ~$0.01–0.10 |
| AgentCore Code Interpreter | Sandboxed test execution session | ~$0.01–0.05 |
| Glue interactive session (code verification) | One create → 3 statements → stop → delete cycle in the testing step | ~$0.0269 measured (see below) |
| Step Functions | ~20 state transitions | <$0.01 |
| DynamoDB / S3 / CloudWatch | State writes, artifacts, logs | <$0.05 |

**Estimated total: ~$0.20–3.00 per run**, dominated by the Bedrock model tier
you select in the pipeline wizard.

### Verification against real source data (p44)

The code-testing step runs the generated transform in a **Glue interactive
session**. Since p44 it reads a bounded **100-row sample of the pipeline's own
source S3 object** rather than always synthesising test rows from the declared
schema. The behaviour is gated by `VERIFICATION_FRAME_MODE`, set to `'sample'` in
both `compute-stack.ts` and `agentcore-stack.ts`, so it is **on by default for
S3-sourced pipelines**.

What this costs, using the figures the feature was designed against rather than
new ones:

- **The session is billable, and it is the dominant term.** The measured baseline
  carried forward from p42 design C10 is **220.388 DPU-seconds — `$0.0269`** for
  one create, measure, stop and delete cycle, at `$0.44` per DPU-hour with a
  2-DPU and 1-minute minimum. The spec derives no new rate from that baseline,
  and neither does this document.
- **Reading real data is not what creates the session.** Requirement 8.1 states
  the incremental cost of the sample read as *additional statement seconds
  inside a session that is already created and already billed*, plus the S3
  request cost of the objects the reader opens. So p44 does not add a session to
  the run — it changes what the existing session reads.
- **The S3 cost is negligible.** One `GetObject` of a bounded read, against the
  session cost above.
- **Session release is attempted on every exit path, and is best-effort.** Stop
  and delete run in a `finally` block (`GlueSessionTestRunner._release_session` in
  `lambda/agentic_data_ingestion_test_code/glue_session_runner.py`), including when
  the run raised. Each call is attempted independently and its error is logged, not
  raised, and the delete retries for up to 2 minutes while the session is still
  provisioning. A session that is still not deleted is logged as `LEAKED`; it is
  created with a 1-minute idle timeout, so it stops billing about a minute after
  its last statement (~$0.015). `finally` guarantees the attempt, not the deletion.
- **The worst case is bounded.** `SHADOW_GLUE_WORST_CASE_SECONDS` is **660
  seconds (11 minutes)** — `180 + 3 × 120 + 120`, for session creation, three
  statements and release — defined at
  `lambda/agentic_data_ingestion_test_code/lambda_function.py:793`. The testing
  step refuses to start the Glue half unless that whole worst case fits inside
  the remaining deadline, so the session cannot be started late and overrun.
- **JDBC-sourced pipelines are unaffected and add no cost.** They cannot be
  sampled — a verification session holds no database credentials and no Secrets
  Manager access (a standing invariant) — so they fall back to a
  synthetic frame. That fallback is the ordinary path for JDBC, not a
  degradation.

Requirement 8.3 records that this cost statement was **derived without creating a
Glue interactive session**, so the `$0.0269` figure is the p42 measurement
carried forward, not a fresh p44 measurement.

## Cost-reduction levers

- **Model tier**: Nova Pro is roughly 80% cheaper than Claude Sonnet for
  development and simple schemas; Nova Micro suits smoke testing. The model is
  selectable per pipeline.
- **Skip optional stacks**: the TestFixtures stack is off by default; the five
  autonomous agent runtimes (self-healing, data quality, cost optimization, NL
  builder, complex mapping) can be left undeployed if unused. Note they are not
  all automatically triggered — self-healing, complex mapping and cost
  optimization run on their own, while data quality has no trigger (its on-demand
  action was disabled and its automatic trigger is deferred by decision) and
  the NL builder is on-demand.
  An untriggered runtime costs $0 when idle, so the saving here is ECR image
  storage rather than execution. See [AGENTS.md](AGENTS.md) for the per-agent
  trigger status.
- **Approval gates**: rejected mappings/code stop the pipeline before the more
  expensive code-generation and Glue steps re-run.
- **Glue workers**: the analytics agent recommends worker count/type per run —
  keep sample data small during evaluation.
- **Log retention**: the log group of every Lambda function in the Compute,
  Orchestration and Hosting stacks, the REST API execution log group and the
  Step Functions log group keep logs for **one month** (`LOG_RETENTION` in
  `deploy-cdk/lib/log-retention.ts`, pinned by `test/log-retention.test.ts`).
  CDK sets no retention on the log groups services create for themselves, such
  as Glue job logs and AgentCore runtime logs, so those keep logs indefinitely
  and their storage cost grows for as long as the stack runs. Set a retention
  period on them with `aws logs put-retention-policy` if cost matters.

## Optional: TestFixtures stack

`AgenticDataIngestion-TestFixtures-{env}` (gated behind
`-c adi:deployTestFixtures=true`, **not** deployed by default) provisions JDBC
test databases:

| Resource | Est. monthly |
|---|---|
| MySQL 8.0 on db.t4g.micro (single-AZ, 20 GB gp3) | ~$13 |
| Aurora PostgreSQL, db.t4g.medium writer | ~$50 (running) / ~$0 (stopped) |

**~$63/month while running.** Stop or destroy it when not testing JDBC flows.

## Cleanup

Start with:

```bash
cd deploy-cdk
npx cdk destroy --all
# If the TestFixtures stack was deployed:
npx cdk destroy AgenticDataIngestion-TestFixtures-dev -c adi:deployTestFixtures=true
```

That is **not** the end of the recurring cost. `destroy --all` destroys seven of
the eight stacks and fails on the Data stack, which has termination protection;
13 of its 17 DynamoDB tables are `RemovalPolicy.RETAIN` and survive even once the
stack is gone. (Verified against `deploy-cdk/lib/data-stack.ts`: 17 tables, of
which 13 inherit `RETAIN` from the shared `tableDefaults`; the four that override
to `DESTROY` are the task-tokens, WebSocket-connections, errors and
model-call-budget tables, all TTL-backed and ephemeral.) Those tables are
on-demand, so an idle table charges nothing for throughput, but you keep paying
for stored data and — because the same `tableDefaults` enables point-in-time
recovery on all 13 retained tables — for continuous backups.

S3 is not the problem: every bucket is `RemovalPolicy.DESTROY` with
`autoDeleteObjects`, so all ten are emptied and deleted with their stacks.
What does persist is the S3 Tables data, Glue databases and jobs, per-pipeline
secrets and IAM roles — the pipeline agents create those at runtime, outside
CloudFormation.

The explicit end-to-end removal procedure, including disabling termination
protection and deleting the retained tables, is in
[QUICKSTART.md](../QUICKSTART.md#teardown).
