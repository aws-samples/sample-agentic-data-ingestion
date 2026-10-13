# Agentic Data Ingestion — Quickstart Guide

Deploy Agentic Data Ingestion to your AWS account, start the dashboard, and run tests.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | 20+ | `brew install node` |
| Python | 3.12+ | `brew install python@3.12` |
| `uv` | Latest | `brew install uv` — the project's Python dependency manager |
| Docker Desktop | Latest | [docker.com](https://www.docker.com/products/docker-desktop/) — must be running |
| AWS CLI | 2.x | `brew install awscli` |

Verify everything works:

```bash
node --version        # v20+
python3 --version     # 3.12+
uv --version          # should not error
docker info           # should not error
aws sts get-caller-identity   # should show your account
```

> **Build host notes:** builds target `linux/arm64` — you need an ARM64 machine
> (Apple Silicon, Graviton) or Docker with QEMU/binfmt cross-building
> (`docker buildx ls` must list `linux/arm64`; on x86 Linux run
> `docker run --privileged --rm tonistiigi/binfmt --install arm64`). A first
> full deploy wants **~100 GB free disk**. AWS CloudShell cannot deploy this
> project (no Docker) — use a local machine or an EC2 build host
> ([docs/DEPLOY_EC2.md](docs/DEPLOY_EC2.md)).

## Deploy

**The deploy walkthrough lives in [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md).** It is
the single explained path: choosing the target account, bootstrapping both regions, deploying
the stacks, creating a Cognito user, and opening the dashboard. This file used to carry a
second copy of those five steps, which is how the two drifted apart.

The terse version, once the prerequisites above are satisfied:

```bash
git clone <repo-url>          # this repository's HTTPS or SSH clone URL
cd <clone-directory>          # the project root: `ls deploy.sh deploy-cdk` must succeed here
uv sync

cd deploy-cdk
export AWS_PROFILE=              # REQUIRED — no default. Fills in your target profile
export CDK_DEFAULT_REGION=us-west-2
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
echo "deploying to $CDK_DEFAULT_ACCOUNT / $CDK_DEFAULT_REGION"   # read this before continuing

npm install
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1    # EdgeWaf stack lives here
npx cdk deploy --all --require-approval never
cd ..                         # back to the project root
```

Two things that block a deploy if skipped, both explained in GETTING-STARTED: `AWS_PROFILE`
has **no default** and leaving it blank fails the `sts` call rather than guessing an account,
and **both** regions need bootstrapping because the EdgeWaf stack deploys a CLOUDFRONT-scope
WAF to `us-east-1` and the Hosting stack consumes its ARN cross-region.

`./deploy.sh` from the project root does all of the above, and validates prerequisites first.

> **What this file still owns.** Everything below: the optional JDBC test databases, the test
> commands, the feature flags, and the **Teardown** procedure, which is the fullest one in the
> repository and is what six other documents link to. The step numbers continue from 6 because
> those anchors are load-bearing.

## 6. (Optional) Provision JDBC test databases

To try a **JDBC source** or **JDBC target** pipeline (read from / write to a
relational database instead of S3), Agentic Data Ingestion ships an opt-in CDK stack that
provisions ready-to-use MySQL and Postgres databases in your account.

This stack is **not** deployed by `cdk deploy --all` — it's gated behind a
context flag so it never provisions databases unless you explicitly ask.

### What it provisions

| Resource | Identifier | Details |
|----------|-----------|---------|
| MySQL 8.0 source | `agentic-data-ingestion-test-mysql-source-dev` | Seeded with `customers` (50 rows) + `orders` (150 rows) in database `appdb` |
| Aurora Postgres 15 target | `agentic-data-ingestion-test-postgres-target-dev` | Empty `testdb` database, single writer, RDS Data API enabled |
| Secrets Manager secrets | `agentic-data-ingestion/test-fixtures/mysql-source`, `agentic-data-ingestion/test-fixtures/postgres-target` | Auto-generated `agentic_data_ingestion_admin` credentials |
| Security groups | self-referencing + cross-allowed | Let Glue ENIs reach both DBs (incl. dual-connection source→target jobs) |
| Glue Connections | `agentic-data-ingestion-test-fixtures-mysql-source`, `agentic-data-ingestion-test-fixtures-postgres-target` | For the wizard's **Existing Glue connection** mode |

The stack reuses the existing Agentic Data Ingestion VPC — it does not create a new VPC or
NAT gateway.

### Deploy it

```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-TestFixtures-dev \
  -c adi:deployTestFixtures=true \
  --require-approval never)
```

First deploy takes ~10-15 minutes (RDS provisioning + seeding). To also skip
the optional Glue Connections, add `-c adi:skipGlueConnections=true`.

### Use it in a pipeline

- **JDBC source:** In the Create Pipeline wizard, choose a JDBC source and
  select the **Existing Glue connection** `agentic-data-ingestion-test-fixtures-mysql-source`
  (schema `appdb`, table `customers`).
- **JDBC target:** Choose **JDBC Database** as the target and select the
  connection `agentic-data-ingestion-test-fixtures-postgres-target` with Database Name
  `public` and Table Name `customers`.

### Verify writes (RDS Data API)

The Postgres target has the RDS Data API enabled, so you can query it without a
bastion or VPC access:

```bash
aws rds-data execute-statement \
  --resource-arn "arn:aws:rds:$AWS_REGION:$CDK_DEFAULT_ACCOUNT:cluster:agentic-data-ingestion-test-postgres-target-dev" \
  --secret-arn "$(aws secretsmanager describe-secret --secret-id agentic-data-ingestion/test-fixtures/postgres-target --query ARN --output text)" \
  --database testdb \
  --sql "SELECT count(*) FROM public.customers"
```

> **Cost note:** the Postgres writer runs on `db.r6g.large` (required because the
> RDS Data API is not supported on burstable `t`-class instances). This costs
> ~4-5x a `t4g.medium`. Tear down the fixtures stack when you're done:
>
> ```bash
> npx cdk destroy AgenticDataIngestion-TestFixtures-dev -c adi:deployTestFixtures=true
> ```

## 7. Run tests

From the project root (`agentic_data_ingestion/`). The Make targets run through `uv run`, so
no activation step is needed:

```bash
# Quick check — unit + contract tests
make test-fast

# Full CI — adds integration tests (informational: carries known pre-existing failures)
make test-ci

# Everything including CDK assertions
make test-all
```

Individual layers:

```bash
uv run python -m pytest tests/unit/ -q          # unit layer
uv run python -m pytest tests/contracts/ -q     # contract layer
uv run python -m pytest tests/integration/ -q   # integration layer
(cd dashboard-react && npx vitest --run)        # frontend
```

Per-layer counts and timings are stated nowhere. [docs/TESTING.md](docs/TESTING.md) carries the
command that produces each, and the tier to run for a given kind of change.

Specific test file:

```bash
uv run python -m pytest tests/unit/lambdas/test_code/test_pyspark_to_pandas.py -v
```

## Configuration

### Feature flags

| Flag | Default | Effect |
|------|---------|--------|
| `USE_AGENTCORE_RUNTIME` | `true` | Agent Lambdas proxy to AgentCore Runtime containers |
| `MEMORY_ENABLED` | `true` | Schema mapping queries historical patterns from AgentCore Memory |
| `MCP_ENABLED` | `true` (Lambdas) / `false` (containers) | Enables MCP tool access for AI agents |

To disable AgentCore Runtime (run agents inline in Lambda):

```bash
# In deploy-cdk/lib/compute-stack.ts, change USE_AGENTCORE_RUNTIME to 'false'
# Then: npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never
```

### Environments

Default is `dev`. Deploy with a different environment label:

```bash
npx cdk deploy --all -c environment=staging --require-approval never
```

Use a separate AWS account for each environment. The label does not namespace every physical
resource, so a second environment in the same account collides with the first; see
[deploy-cdk/README.md](deploy-cdk/README.md#environment-configuration).

## Teardown

`npx cdk destroy --all` does **not** remove everything, and it does not finish
cleanly. Two protections on the **Data** stack are deliberate:

| Protection | Where | Effect |
|---|---|---|
| `terminationProtection: true` on the Data stack | `deploy-cdk/lib/data-stack.ts:35` | CloudFormation refuses to delete the stack at all |
| `RemovalPolicy.RETAIN` on most of its DynamoDB tables | `deploy-cdk/lib/data-stack.ts:42` (stack-wide table default) | Those tables survive even once the stack itself is deleted |

That is the right posture for a data store — it is why a mistyped stack name or a
failed deploy cannot take your pipeline history with it. The cost is that removing
a deployment is more than one `cdk destroy`.

**Use `./destroy.sh` rather than doing it by hand.** It is the inverse of `./deploy.sh` and
handles all three classes of leftover: the stacks in reverse creation order, the retained
DynamoDB tables whose fixed physical names otherwise collide on the next deploy, and — the
class a manual `cdk destroy` sequence omits entirely — the **account-global IAM roles and
customer-managed policies** carrying the project prefix. Those outlive any one region's
stacks, so a region-scoped teardown leaves them behind (`destroy.sh:15-16`, `:163-165`).

```bash
# From the project root:
# Enumerate what would be deleted, and delete nothing:
./destroy.sh --profile <name> --region us-west-2

# Actually delete:
./destroy.sh --profile <name> --region us-west-2 --force
```

`--profile` and `--region` are both required, and the account is derived from the profile
rather than written down. Without `--force` the script enumerates every selected resource
and stops, having deleted nothing. Note that IAM is account-global while the region argument
scopes only the stacks and tables: a `us-west-2` teardown deletes prefix-matching IAM in the
whole account, including roles a `us-east-1` deploy created.

> Table counts are not restated here — they live once, with their derivation, in
> [docs/ARCHITECTURE.md § System Inventory](docs/ARCHITECTURE.md#system-inventory).

### What `cdk destroy --all` actually does

```bash
(cd deploy-cdk && npx cdk destroy --all)
```

Seven of the eight stacks destroy: Hosting, EdgeWaf, Orchestration, Compute,
AgentCore, Auth, Foundation. **The Data stack fails** on termination protection,
leaving its DynamoDB tables and the EventBridge bus in place. That failure is
deterministic, not environmental — expect it every time.

Side effects of the seven that do go:

- **S3 buckets are emptied and deleted.** **Every** bucket — the `adi-*`
  buckets in Foundation and the dashboard bucket in Hosting (counts:
  [docs/ARCHITECTURE.md § System Inventory](docs/ARCHITECTURE.md#system-inventory)) — is
  `RemovalPolicy.DESTROY` with `autoDeleteObjects: true`, so CDK empties them
  (including noncurrent object versions) before deletion. No bucket is retained.
- **Every Cognito user is deleted** with the Auth stack — the User Pool is
  `RemovalPolicy.DESTROY` (`deploy-cdk/lib/auth-stack.ts:51`).
- The ECR repository is emptied (`emptyOnDelete: true`).
- The CDK **bootstrap** stack and its staging bucket are separate infrastructure
  and are not touched by `destroy --all`.

### Fully removing a deployment

**Irreversible.** This deletes the pipeline history, approvals, audit log, agent traces, and
generated-code metadata — data that nothing else in the account holds a copy of.

```bash
# From the project root:
./destroy.sh --profile <name> --region us-west-2            # enumerate only, deletes nothing
./destroy.sh --profile <name> --region us-west-2 --force    # delete
```

`./destroy.sh` performs the whole sequence that was previously manual: it disables
termination protection on the Data stack, destroys the stacks in reverse creation order,
deletes the retained DynamoDB tables by prefix, and deletes the prefix-matching
account-global IAM roles and customer-managed policies. Roles are deleted before policies, so
the attachments are gone by the time the customer-managed policies are removed.

If you deployed the JDBC test fixtures, destroy that stack too — it is the most expensive
thing here (~$63/month while running):

```bash
(cd deploy-cdk && npx cdk destroy AgenticDataIngestion-TestFixtures-dev -c adi:deployTestFixtures=true)
```

### Retained tables block a redeploy

If you destroy the Data stack by hand and redeploy without deleting the retained tables, the
deploy fails. Every table name is hardcoded (`tableName: 'agentic_data_ingestion_…'`), so
CloudFormation tries to create a table that already exists and stops. Deleting the
Data stack and redeploying is therefore not a way to reset state — either keep the
tables and keep the stack, or delete the tables before redeploying. This is the collision
`./destroy.sh` exists to prevent: it deletes the retained tables by prefix in the same run.

Tables are not the only fixed-name class. A teardown that ends in `DELETE_FAILED` can abandon
resources whose names are equally hardcoded — AgentCore runtimes in particular — and those
collide the same way. See
[docs/TROUBLESHOOTING.md § Teardown failed and the next deploy collides](docs/TROUBLESHOOTING.md#teardown-failed-and-the-next-deploy-collides).

### Retained tables keep costing money

The retained tables use on-demand billing, so an idle table charges nothing for
throughput — but you are still billed for **stored data**, and because
point-in-time recovery is enabled on every one of them, for **continuous backups**
as well. On a small evaluation deployment that is cents per month; on one that has
processed real volume it is not. A retained deployment is a standing charge, not
a free pause.

### What `cdk destroy` never removes

The pipeline agents create AWS resources at runtime, outside CloudFormation, so
no `cdk destroy` deletes them:

- **S3 Tables** table buckets, namespaces, and Iceberg tables
  (`lambda/agentic_data_ingestion_create_infrastructure/lambda_function.py:187`) —
  these hold the ingested data and bill for storage
- **Glue Catalog** databases and tables registered per pipeline (same file, :279)
- **Glue jobs**, **crawlers**, and **connections** created per pipeline
- **Secrets Manager** secrets and **IAM roles** created per pipeline
  (`lambda/agentic_data_ingestion_create_glue_pipeline/connections.py:117`,
  `iam_roles.py:112`)

Remove these from the console or CLI if you want the account clean. They carry the
`agentic-data-ingestion` / `agentic_data_ingestion` name prefixes.

## Troubleshooting

| Problem                       | Fix                                                                               |
| -------------------------------| -----------------------------------------------------------------------------------|
| Deploy fails on Docker build  | Make sure Docker Desktop is running                                               |
| Login redirect fails / blank dashboard | Create a Cognito user (step 4) and confirm the CloudFront URL is a Hosted UI callback (Auth/Hosting outputs) |
| Pipeline stuck at approval    | Check the Approvals section in the dashboard                                      |
| Glue job slow on first run    | Normal — Spark cluster provisioning takes 1-2 min                                 |
| Import errors in tests        | Re-sync the environment: `uv sync`, and run tests via `uv run` or the Make targets |
| WebSocket updates not working | Deployed app reads `wsUrl` from `/config.json` (automatic). For local dev, set `VITE_WS_URL` in `dashboard-react/.env.local` — see [docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md) |

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for more.


```