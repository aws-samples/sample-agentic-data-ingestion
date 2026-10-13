# Agentic Data Ingestion — AI-Powered ETL Pipeline Automation

Agentic Data Ingestion is an intelligent ETL pipeline automation system that uses AI to generate, test, and deploy data transformation code. It combines AWS Bedrock, Strands Agents, and Model Context Protocol (MCP) to create a fully automated, human-supervised ETL pipeline generation system.

> ## ⚠️ Sample code — not for production use
>
> This repository is a **reference architecture published as sample code**. It is
> provided **as-is, without warranty of any kind**, to be read, run in a test
> account, and adapted. It is **not an AWS product or service**, it is **not
> covered by AWS Support**, and it carries **no service-level agreement**. See
> [SUPPORT.md](SUPPORT.md).
>
> Deploying it creates billable AWS resources, including AWS Glue jobs, Amazon
> Bedrock model invocations, Amazon DynamoDB tables, AWS Lambda functions and an
> Amazon CloudFront distribution. Review [docs/COST.md](docs/COST.md) first, and
> use [destroy.sh](destroy.sh) to tear everything down when you are finished.
>
> The defaults are chosen to keep the demo path short, not to be secure for real
> data. Before pointing this at anything sensitive, work through
> [Hardening for real data](#hardening-for-real-data) — and treat that section as
> a starting point rather than a complete list.

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Node.js | 20+ | `brew install node` or [nvm](https://github.com/nvm-sh/nvm) |
| Python | 3.12+ | `brew install python@3.12` or [uv](https://docs.astral.sh/uv/) |
| Docker | Latest | [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| AWS CLI | 2.x | `brew install awscli` |
| AWS CDK | 2.x | Installed via `npm install` in deploy-cdk |

**Build host requirements:**

- **ARM64 builds.** All Lambda layers and container images target `linux/arm64`.
  You need either an ARM64 host (Apple Silicon, AWS Graviton) or Docker with
  QEMU/binfmt cross-building. Verify with `docker buildx ls` (must list
  `linux/arm64`); on x86 Linux enable it with
  `docker run --privileged --rm tonistiigi/binfmt --install arm64`.
- **Disk space.** A first full deploy builds 9 container images plus Lambda
  layers — **~100 GB free is recommended** (`./deploy.sh` warns below that and
  fails below 50 GB).
- **AWS CloudShell cannot deploy this project** (no Docker daemon). Use a local
  machine with Docker, or an EC2 build host — see
  [docs/DEPLOY_EC2.md](docs/DEPLOY_EC2.md) for a known-good recipe.

## Supported regions & AWS prerequisites

The verified deployment region is **us-west-2**. Deploying elsewhere requires
that all of the following are available in your target region:

- **Amazon Bedrock model access** for the configured models (Claude Sonnet and
  Amazon Nova cross-region inference profiles). Many models are
  [available by default](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html),
  but a third-party model is enabled for the account on its first invocation,
  and that needs AWS Marketplace permissions (`aws-marketplace:Subscribe` and
  related actions) that none of this solution's roles carry. So **before the
  first pipeline run**, invoke each configured model once as a principal that
  has them, for example from the Bedrock console playground. Anthropic models
  also need the one-time first-use form (use case details) for the account or
  its organization. The deployment succeeds without any of this; the AI steps
  then fail at runtime with `AccessDeniedException`.
- **Amazon Bedrock AgentCore** (Runtime, Memory, Code Interpreter).
- **Amazon S3 Tables** (only if you use S3 Tables / Iceberg targets).
- **AWS Glue 5.0**.

Regardless of the app region, `us-east-1` must also be bootstrapped: the
CloudFront-scope WAF stack deploys there (see Setup below).

## Project Structure

```
agentic_data_ingestion/
├── deploy-cdk/              # CDK infrastructure (8 stacks + optional test-fixtures)
├── dashboard-react/         # React + Cloudscape dashboard (CloudFront + Cognito Hosted UI)
│   ├── src/                 # TypeScript source
├── lambda/                  # Lambda functions
├── shared/                  # Shared Python modules (Lambda layer)
│   └── prompt_templates/    # Jinja2 prompt templates
├── mcp/                     # MCP servers (codegen, infra, analytics, s3_tables, data_processing)
├── agentcore/               # AgentCore Runtime containers (pipeline agents + autonomous agents)
├── pyspark_templates/       # PySpark code generation templates (Jinja2)
├── tests/                   # Layered test suite
│   ├── unit/                # isolated Lambda + agent tests (moto)
│   ├── contracts/           # API & event schema validation
│   ├── integration/         # multi-Lambda flow tests
│   ├── e2e/                 # real-AWS tests (need deployed stack, excluded by default)
│   ├── fixtures/            # Shared data, moto/real-AWS toggle
│   └── cdk/                 # CDK assertions (Python)
├── docs/                    # Architecture and design docs
├── diagram/                 # Architecture diagram (diagram.svg)
├── lib/                     # JAR dependencies (S3 Tables Iceberg)
└── scripts/                 # Utility scripts
```

> **Counts live in two places, and neither is here.** Inventory figures — Lambda directories, shared
> modules, MCP servers, agent containers, PySpark templates, DynamoDB tables, GSIs, buckets, wizard
> steps, approval gates — are stated once in
> [docs/ARCHITECTURE.md § System Inventory](docs/ARCHITECTURE.md#system-inventory), each with the rule
> that produces it and a derivation a test executes. Test counts, file counts and wall clocks are
> deliberately stated nowhere: [docs/TESTING.md](docs/TESTING.md) carries the command that produces
> each instead. Re-measure rather than quote: every such figure measures one tree at one moment, and a
> literal copied into a second file is drift waiting to happen.

## Setup

### Recommended: one-command deploy

With AWS credentials configured (profile, SSO, or env vars):

```bash
./deploy.sh
```

The script validates prerequisites (Node 20+, Python 3.12+, Docker, ARM64 build
capability, disk space, credentials), resolves your account and region, bootstraps
CDK in both required regions, and deploys all stacks. The React dashboard is built
automatically during deploy. Use `./deploy.sh --dry-run` to only validate the
environment, and `./deploy.sh --environment staging` for a non-default environment.

### Manual deployment

**[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) is the step-by-step walkthrough** — the
single explained path, covering what `deploy.sh` automates: choosing the target account,
bootstrapping both regions, deploying the stacks, creating a Cognito user, and opening the
dashboard.

Two constraints from it are worth stating here, because skipping either fails the deploy:

- **`AWS_PROFILE` has no default.** Two accounts run this system, so deriving the account from
  a named profile and reading the result back is the procedure, not a formality. A pasted
  account literal once updated a stack in the wrong account.
- **Bootstrap both regions.** The EdgeWaf stack deploys a CLOUDFRONT-scope WAF to `us-east-1`
  and the Hosting stack consumes its ARN cross-region, so the app region alone is not enough.

The dashboard is built automatically as part of the Hosting stack's asset bundling. There is no
separate frontend build step, which is why the first deploy is slower.

Either path deploys 8 stacks in dependency order:

| Stack | Resources |
|-------|-----------|
| `AgenticDataIngestion-Foundation-dev` | IAM roles, ECR, S3 buckets, RDS/Glue/SecretsManager discovery permissions |
| `AgenticDataIngestion-Data-dev` | DynamoDB tables, EventBridge bus, DLQ |
| `AgenticDataIngestion-Auth-dev` | Cognito User Pool (email sign-in, no self-signup, TOTP MFA required by default), Hosted UI domain |
| `AgenticDataIngestion-AgentCore-dev` | AgentCore Runtime containers, MCP runtimes, Memory resource |
| `AgenticDataIngestion-Compute-dev` | Lambda functions, REST API Gateway (Cognito authorizer), WebSocket API (defined inline, Lambda authorizer on `$connect`), regional WAF (X-Origin-Verify), Strands layer (auto-built) |
| `AgenticDataIngestion-Orchestration-dev` | Step Functions, EventBridge rules, CloudWatch alarms |
| `AgenticDataIngestion-EdgeWaf-dev` | CLOUDFRONT-scope WAF (managed rules + rate limit) — **deploys to us-east-1** |
| `AgenticDataIngestion-Hosting-dev` | Private S3 + CloudFront (OAC, SPA fallback, `/api/*` proxy), CLOUDFRONT WAF via `webAclId`, Cognito app client, `config.json` deployment |

An optional `AgenticDataIngestion-TestFixtures-dev` stack provisions MySQL/Postgres JDBC
test databases. It is gated behind `-c adi:deployTestFixtures=true` and is
not deployed by `cdk deploy --all` — see [QUICKSTART.md](QUICKSTART.md#6-optional-provision-jdbc-test-databases).

> **Upgrade note — globally-unique names now account-scoped (breaking for pre-P27 deployments):**
> two resources had deterministic globally-unique names that the first deploying
> account claimed permanently, blocking everyone else. Both now embed the account ID:
>
> - **Cognito Hosted UI domain** → `agentic-data-ingestion-<account-id>-<env>`.
>   Updating an **existing** environment replaces the domain, and because a user
>   pool allows only one domain (created before the old one is deleted), the Auth
>   stack update fails unless you delete the old domain first:
>
>   ```bash
>   aws cognito-idp delete-user-pool-domain \
>     --domain agentic-data-ingestion-<env> --user-pool-id <user-pool-id>
>   ```
>
>   The sign-in URL changes; the dashboard picks the new domain up automatically
>   via `config.json` on the next Hosting deploy.
> - **Dashboard S3 bucket** → `adi-dashboard-<account-id>-<region>` (matching the
>   other `adi-*` buckets). Updating an existing environment replaces the bucket;
>   the old one is emptied and deleted automatically (`autoDeleteObjects`) and the
>   same deploy repopulates the new bucket and repoints CloudFront.
>
> Fresh accounts are unaffected by either change.

> **Upgrade note — `projectName` default (breaking for deployments created before July 2026):**
> the `cdk.json` context default changed from `sample-agentic_data_ingestion` to
> `agentic-data-ingestion`. The value is embedded in IAM role and managed-policy
> physical names (e.g. `agentic-data-ingestion-dev-codegeneration-role`), so the
> change **replaces those roles** — and because dependent stacks import the role ARNs
> via CloudFormation exports, an in-place update fails with an "export ... is in use"
> error. Existing deployments must either (a) destroy and redeploy
> (`npx cdk destroy --all`, then `./deploy.sh` — but `destroy --all` leaves the
> Data stack and its retained DynamoDB tables behind, and the retained tables
> block a redeploy until they are deleted, see
> [QUICKSTART.md](QUICKSTART.md#teardown)), or (b) pin the old name on every
> deploy: `-c projectName=sample-agentic_data_ingestion`. Fresh accounts are unaffected.

### Access the dashboard

The dashboard is served from a private S3 bucket via CloudFront (OAC) and protected by Cognito Hosted UI — it is not an S3 website and there is no local signing proxy. After `cdk deploy --all`, open the CloudFront URL from the Hosting stack output and sign in through the Cognito Hosted UI:

```bash
aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Hosting-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" --output text
```

User accounts are admin-created (self-signup is disabled): create users in the Cognito User Pool, then sign in at the CloudFront URL.

#### Local development

```bash
cd dashboard-react
npm install
npm run dev          # Vite dev server on http://localhost:5173
```

Vite proxies `/api/*` to the deployed CloudFront distribution — set `VITE_API_PROXY_TARGET` to the CloudFront URL in `.env.local` (the regional WAF blocks the raw `execute-api` URL, so CloudFront must be the proxy target). Signing in from `localhost` needs the stacks deployed with `-c adi:allowLocalhost=true`, which adds the dev server to the Cognito callback URLs; it is off by default. See [docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md) for the full setup.

## Testing

```bash
make test-fast    # Unit + contract tests — must run alone
make test-ci      # + integration tests (informational: carries known pre-existing failures)
make test-all     # All test layers
make test-e2e     # Real AWS (sets AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true) — billable
```

Test layers run in separate pytest processes, and that is deliberate. No counts or wall clocks
are stated here or anywhere else. [Testing Guide](docs/TESTING.md) carries the command that
produces each figure, the tier to run for a given kind of change, and what each layer covers.

## Usage

1. Create a pipeline via the dashboard (set name, target type, target config)
   - Target types: S3 Tables (Iceberg), JDBC, S3 Parquet. `RDS_DATABASE` is a legacy alias normalised onto `jdbc`, not a fourth type. A fourth type, Glue Catalog (write into an existing catalog table), is no longer offered for new pipelines; existing Glue Catalog pipelines keep running
2. Upload a source file (CSV, JSON, Parquet, Avro, ORC)
3. The Step Functions pipeline runs 10 steps automatically:
   - File Validation → Schema Extraction → Schema Comparison → Schema Mapping
   - Code Generation → Code Testing → Infrastructure Provisioning → Glue Job Creation
   - Quality Checks → Run Pipeline (with inline Data Quality)
4. Approve schema mappings and generated code when prompted (or approve inline on the Schema page)
5. Monitor execution progress in real-time on the dashboard
6. Use Mapping Memory to track and reuse approved field mappings across runs

## Updating Lambda code

After editing Lambda source in `lambda/` or `shared/`, redeploy:

```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never --exclusively)
```

The Strands Lambda layer (shared modules, MCP client, Jinja2, aws-lambda-powertools) is rebuilt automatically via Docker bundling.

**Important:** The AI agent Lambdas (`create_code`, `test_code`, `create_glue_pipeline`, `create_infrastructure`) run on AgentCore Runtime containers. Changes to these also require deploying the AgentCore stack:

```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-AgentCore-dev AgenticDataIngestion-Compute-dev --require-approval never)
```

## Updating the Step Functions state machine

```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-Orchestration-dev --require-approval never --exclusively)
```

## Environment configuration

The `.env` file in the project root sets `AWS_PROFILE` and `AWS_REGION` for the CLI/CDK tooling. CDK reads `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION` from the shell environment. The deployed dashboard reads its runtime settings (region, Cognito User Pool ID + client ID, Hosted UI domain, `apiBaseUrl`, `wsUrl`) from `/config.json`, which CDK generates and uploads to S3 — no build-time env vars are required in production. For local dev only, set `VITE_API_PROXY_TARGET` (the CloudFront URL) and optional `VITE_*` fallbacks in `dashboard-react/.env.local`.

To deploy with a different environment label:

```bash
npx cdk deploy --all -c environment=prod -c notificationEmail=ops@example.com
```

`notificationEmail` is optional; omit it and no email subscription is created.

**Deploy one instance per dedicated AWS account, and use separate accounts for separate
environments.** The `environment` setting selects configuration and labels the stacks; it does not
namespace every physical resource. The DynamoDB tables and AgentCore runtimes have fixed names, and
the S3 bucket names carry the account and region but not the environment, so a second deployment in
the same account and region collides with the first. Another region in the same account is not
isolated either, because IAM names are account-wide and do not carry the region. Teardown also
leaves the retained DynamoDB tables in place, and they block a later deployment into the same
account and region until they are removed. [deploy-cdk/README.md](deploy-cdk/README.md#environment-configuration)
lists which resources collide and where they are defined.

There is also a security reason, and it applies even to a single deployment: several of the
sample's roles hold account-wide Glue permissions and broad S3 access, and each pipeline's Glue
job runs model-written code. Use an account that holds no unrelated workloads whose Glue catalog,
connections or buckets matter to you.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design, and
[diagram/diagram.svg](diagram/diagram.svg) for the rendered architecture diagram.

```
User → Cognito Hosted UI login
     → CloudFront (private S3 SPA + /api/* proxy) → API Gateway (Cognito authorizer) → Lambda (Dashboard API)
                                                  ↓
                                          Step Functions
                                    ┌─────────────────────┐
                                    │ 10 Processing Steps  │
                                    │ (AI steps use MCP)   │
                                    └─────────────────────┘
                                          ↓           ↓
                                    Bedrock +     AgentCore
                                    Strands       (Dual MCP)
                                          ↓
                                    AWS Glue Job
```

## Documentation

**[docs/README.md](docs/README.md) is the full index**, grouped by Core, User Guides,
Deployment, Reference and Archive. The shortlist below is where most readers start.

| Start here | For |
|---|---|
| [Getting Started](docs/GETTING-STARTED.md) | Deploy it and run a first pipeline. The single deploy walkthrough |
| [Architecture](docs/ARCHITECTURE.md) | System design, and the System Inventory every other doc's counts point at |
| [Workflow](docs/WORKFLOW.md) | What the 10 pipeline steps actually do |
| [Troubleshooting](docs/TROUBLESHOOTING.md) | Symptom-first index when a run fails |
| [Testing Guide](docs/TESTING.md) | Which gate to run for a given change, and how to measure it |

Also frequently wanted: [Dashboard](docs/DASHBOARD.md) (the React app),
[Agents](docs/AGENTS.md) (the five autonomous agents, and which are actually auto-triggered),
[JDBC Source](docs/JDBC_SOURCE.md) (database sources and targets),
[Lambda Functions](docs/LAMBDA-FUNCTIONS.md), [DynamoDB Tables](docs/DYNAMODB-TABLES.md),
[MCP Integration](docs/MCP-INTEGRATION.md), [Cost](docs/COST.md),
[Well-Architected](docs/WELL-ARCHITECTED.md) and [cdk-nag](docs/CDK-NAG.md).

## Data sent to Amazon Bedrock

The AI steps send parts of your data to a foundation model. With the default
configuration the model is Claude Sonnet 4.6, called through the `us.`
geographic cross-Region inference profile, so a request can be processed in any
US commercial Region the profile covers
([cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)).
A pipeline can override the model ID; a `global.` profile routes requests to
any commercial Region worldwide.

| Step | What the prompt contains |
|---|---|
| Schema mapping | Source and target field names and types, and up to five sample values per source field. For a JDBC source, from the second run onward the samples come from rows the Glue job read. |
| Complex mapping agent | The same, for the fields the first pass could not map with enough confidence |
| Code generation | The source and target schemas and the mapping. No sample values. |
| Code testing (review) | The generated PySpark code and the verification result, including any error text, which can quote a value |
| Natural-language pipeline builder (`POST /chat`) | Your chat messages, and the field names and types it reads from a file in the source bucket |

Model providers have no access to prompts or completions
([Bedrock data protection](https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html)).
Whether AWS retains them depends on the model and on your account's data-retention
setting; read [Bedrock data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html)
before switching to a different model. The agent trace records this sample
stores in DynamoDB replace each list of sample values with a count. There is no switch
that withholds sample values from the mapping prompts; mapping quality depends
on them.

## Hardening for real data

The sample ships with demo-friendly auth defaults. The changes below are the ones
the maintainers know about, and they are a **starting point, not a sufficient
condition** — working through this list does not make the sample production-ready,
and nothing here alters the as-is, unsupported status set out in
[SUPPORT.md](SUPPORT.md). Assess it against your own security and compliance
requirements before it touches data you care about.

- **MFA is required by default** — pool-level `mfa: REQUIRED` with TOTP (SMS
  stays disabled). Users enroll an authenticator at their first Hosted UI
  sign-in. It is optional only when the e2e test client is deployed
  (`-c adi:deployE2eClient=true`), whose password sign-in cannot answer an MFA
  challenge. `-c adi:requireMfa=true` or `=false` overrides that either way;
  keep it required anywhere real data is involved.
- **Password policy** — 16-character minimum with all character classes and a
  24-hour temporary-password window is enforced by default.
- **Federate with your IdP** — Cognito User Pools support SAML/OIDC identity
  providers (Okta, Entra ID, etc.) through the Hosted UI, giving you your
  organization's MFA, session policies, and audit trail. Add an
  `IdentityProvider` + `supportedIdentityProviders` entry to `auth-stack.ts`.
- **Role-based access** — pre-create Cognito groups (e.g. `admin`, `viewer`,
  `pipeline-operator`) and scope API routes by group claims in the REST
  authorizer if different personas need different privileges; the sample grants
  every authenticated user full dashboard access.
- **User provisioning** — create users via your IdP or an automated pipeline
  rather than the CLI; the QUICKSTART's random-temporary-password flow is the
  minimum bar (no plaintext literals, forced rotation at first sign-in).
- **Cognito Plus tier** — enables threat protection (compromised-credential
  detection, adaptive auth) at additional cost; the sample uses Essentials.
- **DynamoDB point-in-time recovery** is on for the 13 retained tables (the
  shared `tableDefaults` in `deploy-cdk/lib/data-stack.ts`). The four
  TTL-backed ephemeral tables (task tokens, WebSocket connections, errors and
  the model-call budget) have it off and are deleted with the stack; that
  finding (AwsSolutions-DDB3) is acknowledged with a justification.
  **CloudFront geo restrictions** are deferred by default with a written
  justification. See [docs/CDK-NAG.md](docs/CDK-NAG.md) for the full
  inventory and rationale.
- **Retention of customer data** — by default uploaded source files expire 90
  days after upload, data-quality reports (`dq-reports/`) and quarantined rows
  (`quarantine/`) after 90 days, and overwritten or deleted object versions
  after 30 days. Lambda logs, API Gateway execution and REST access logs, and
  CloudFront access logs are kept for one month.
  The processed-files bucket never expires current objects, because Glue
  Catalog target tables usually live there. Change `SOURCE_RETENTION_DAYS` and
  `NONCURRENT_VERSION_DAYS` in `deploy-cdk/lib/foundation-stack.ts` to match
  your policy.
- **Keep code approval on** for any pipeline whose source data you do not
  control. A model writes the transform, and field names and values reach its
  prompt. Turning the gate off (`code_generation_enabled: false`) lets a
  transform that passes its checks run without review. See
  [SECURITY.md](SECURITY.md) for what the checks do and do not cover.
- **Review IAM changes before you redeploy.** `./deploy.sh` and the `cdk deploy`
  commands in this README pass `--require-approval never`, so they apply IAM and
  security-group changes without showing them. Run `npx cdk diff` in
  `deploy-cdk/` first, or run `npx cdk deploy` without that flag to be prompted
  for every security-relevant change. `cdk.json` does not set it, so a bare
  `cdk deploy` prompts by default.
- **Development-only allowances are off by default.** `-c adi:allowLocalhost=true`
  adds `http://localhost:5173` to the Cognito callback and logout URLs and to the
  `/api/*` CORS origins, for local dashboard development.
  `-c adi:deployE2eClient=true` creates the `USER_PASSWORD_AUTH` app client the
  e2e suite uses. Leave both off anywhere real data is involved.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more
information on reporting security issues.

## License

This sample is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.

It redistributes one third-party component under the Apache License 2.0; see
[NOTICE](NOTICE) and [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES).
