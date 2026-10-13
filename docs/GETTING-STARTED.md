# Getting Started with Agentic Data Ingestion

This guide will help you deploy Agentic Data Ingestion and run your first AI-powered ETL pipeline.

## Prerequisites

### AWS Account Setup
- AWS account with appropriate permissions
- AWS CLI configured (`aws configure`)
- Region: us-east-1 or us-west-2 recommended (Bedrock availability)

### Required Permissions
Your IAM user/role needs permissions for:
- CloudFormation (create/update/delete stacks)
- Lambda, S3, DynamoDB, Step Functions
- ECR (for MCP server images)
- Bedrock and AgentCore Runtime
- IAM (create roles and policies)

### Local Tools
- Node.js 20+
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) — the project's Python dependency manager (`brew install uv`). Every `make` target runs through `uv run`; do not hand-build a virtualenv
- Docker (for MCP server and Lambda layer builds — must be able to build `linux/arm64` images; native on Apple Silicon/Graviton, QEMU/binfmt on x86)
- ~100 GB free disk for a first full deploy
- Bash shell
- Git

> AWS CloudShell cannot deploy this project (no Docker). Use a local machine
> or an EC2 build host — see [DEPLOY_EC2.md](DEPLOY_EC2.md).

## Quick Start

> A first full deploy takes 15-30 minutes (Docker image builds + dashboard build).
> Subsequent deploys are 3-5 minutes.

### 1. Clone and Navigate

Every command in this guide runs from the **project root**: the directory that
contains `deploy.sh`, `deploy-cdk/` and `dashboard-react/`. A block that needs
another directory changes into it and returns before it ends.

```bash
git clone <repo-url>          # this repository's HTTPS or SSH clone URL
cd <clone-directory>          # the project root: `ls deploy.sh deploy-cdk` must succeed here
uv sync                       # installs the Python toolchain and dependencies
```

### 2. Choose the target account

**There is no default account.** Two live accounts run this system, one of which is a
fresh-account validation account, so a pasted account literal deploys somewhere plausible
and wrong. Name the profile and let the account be read back from it. That prevents a repeat
of a real incident: a hardcoded account number was copied verbatim into a deploy aimed at the
other account, and updated a Data stack in the wrong place. A blank `AWS_PROFILE` fails the
`sts` call rather than defaulting, so it fails closed.

Set both variables **before** the deploy commands below, which read them. These
commands work from any directory; stay in the project root:

```bash
export AWS_PROFILE=              # REQUIRED — no default; fill in your target profile
export CDK_DEFAULT_REGION=us-west-2
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
echo "deploying to $CDK_DEFAULT_ACCOUNT / $CDK_DEFAULT_REGION"   # verify before continuing
```

Leaving `AWS_PROFILE` blank fails the `sts` call rather than defaulting — it fails closed.
Read the `echo` and confirm the account is the one you meant before going on. This matches
`deploy-cdk/README.md` and `deploy.sh`, which already derive the account this way;
`lib/resolve-env.ts` rejects anything that is not 12 digits.

### 3. Deploy Infrastructure

Recommended — one command from the project root (validates prerequisites,
bootstraps both regions, deploys all stacks, builds the dashboard automatically).
`--check` runs only the prerequisite checks and changes nothing, so run it first:

```bash
./deploy.sh --check
./deploy.sh
```

Manual equivalent: bootstrap CDK in **both** the application region and `us-east-1` (the EdgeWaf stack
deploys a CLOUDFRONT-scope WAF to `us-east-1` and is referenced cross-region by the
Hosting stack), then deploy. Run it from the project root; it returns there:

```bash
cd deploy-cdk
npm install
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1
npx cdk deploy --all --require-approval never
cd ..
```

This deploys all 8 CDK stacks including:
- S3 buckets, DynamoDB tables
- Lambda functions with Strands layer
- Step Functions state machines
- Cognito User Pool + Hosted UI (Auth stack)
- REST API Gateway with Cognito authorizer + regional WAF (Compute stack)
- WebSocket API with Lambda authorizer on `$connect` (Cognito JWT validation)
- CLOUDFRONT-scope WAF in **us-east-1** (EdgeWaf stack)
- S3 + CloudFront hosting with OAC, attached to the EdgeWaf WebACL (Hosting stack)
- MCP servers and agent containers on AgentCore Runtime

> An optional `AgenticDataIngestion-TestFixtures-dev` stack provisions MySQL/Postgres JDBC
> test databases. It is gated behind `-c adi:deployTestFixtures=true` and
> is not deployed by `cdk deploy --all` — see
> [QUICKSTART.md](../QUICKSTART.md#6-optional-provision-jdbc-test-databases).

### 4. Create a Cognito User

Since self-signup is disabled, create a user after the Auth stack deploys:

```bash
USER_POOL_ID=$(aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Auth-dev \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)

# Random temporary password (pool policy: 16+ chars); you set the real one at
# first Hosted UI sign-in, and the temporary one expires after 24 hours.
TEMP_PASSWORD=$(aws secretsmanager get-random-password \
  --password-length 24 --require-each-included-type --output text)

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD"

echo "Temporary password: $TEMP_PASSWORD"
```

### 5. Access the Dashboard

Open the CloudFront URL from the Hosting stack outputs:

```bash
aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Hosting-dev \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontUrl'].OutputValue" --output text
```

You'll be redirected to the Cognito Hosted UI to sign in. On the first sign-in you set your own
password and then enroll an authenticator app (TOTP), because MFA is required by default; have one
ready. After login the dashboard renders.

For local development with the Vite dev server, see `docs/LOCAL_DEVELOPMENT.md`.

### 6. Create Your First Pipeline

**Via Dashboard (Wizard):** the Create Pipeline Wizard is a **6-step** Cloudscape wizard.

1. Navigate to `/pipelines/new` or click "Create Pipeline" from the pipeline list
2. **Step 1 — Name & Description**: Enter pipeline name `my-first-pipeline`, add an optional description
3. **Step 2 — Source**: Choose the source type — `S3_UPLOAD`, or `JDBC` (full-table or watermark-based incremental)
4. **Step 3 — Target Destination**: Select one of the three target types — S3 Tables (Iceberg), JDBC, or S3 Parquet — and fill connection details. Click "Detect Schema" for an S3 Tables or JDBC target to auto-populate the schema
5. **Step 4 — Target Schema**: Review auto-detected schema, or upload a JSON/CSV schema file via drag-drop with inline preview
6. **Step 5 — AI Model & Options**: Select AI model (Claude Sonnet 4.6 recommended), configure approval gates
7. **Step 6 — Review & Create**: Review all settings and click "Create"

**Via the dashboard:** upload a CSV on the pipeline's **Files** tab. The S3 upload
EventBridge rule starts the pipeline on object creation — there is no separate trigger
step.

**Via CLI:** the equivalent is a plain `aws s3 cp` into the source-files bucket under the
pipeline's configured prefix. The bucket name is derived from your account and region
(`foundation-stack.ts:77`), so the variables exported in step 2 are enough to build it:

```bash
aws s3 cp tests/test-data/customer_data_1.csv \
  "s3://adi-source-files-$CDK_DEFAULT_ACCOUNT-$CDK_DEFAULT_REGION/my-first-pipeline/customer_data_1.csv"
```

### 7. Monitor Execution

Watch the 10-step pipeline execute:
1. File Validation
2. Schema Extraction
3. Schema Comparison
4. Schema Mapping (AI)
5. Code Generation (AI + MCP)
6. Code Testing (AI + Glue interactive session)
7. Infrastructure Provisioning
8. Glue Job Creation
9. Quality Checks
10. Run Pipeline (with inline Data Quality)

## Deployment Options

### Full Deployment (Recommended)
```bash
./deploy.sh    # from the project root — validates prereqs, bootstraps, deploys all stacks
```

Manual equivalent (from the project root; the subshell returns there):
```bash
(cd deploy-cdk && npm install && npx cdk deploy --all --require-approval never)
```

### Lambda Code Only (Fast Updates)
```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never --exclusively)
```

### AgentCore Runtime Containers
```bash
(cd deploy-cdk && npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never)
```

## Configuration

### Environment Parameters
All configuration is managed via CDK context. See `deploy-cdk/bin/agentic_data_ingestion.ts`
for environment settings and the stack wiring.

### AI Model Selection
Default: Claude Sonnet 4.6. Change via `BEDROCK_MODEL_ID` environment variable:
- `us.anthropic.claude-sonnet-4-6` (Default — best coding performance)
- `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (Previous generation)
- `us.amazon.nova-pro-v1:0` (Cost-effective)

### WebSocket Real-Time Updates

The **deployed** dashboard gets its WebSocket URL from `/config.json` (deployed by the
Hosting stack to S3) — no manual step is required. The WebSocket API is defined inline
in the Compute stack and its endpoint is written into `config.json` by CDK.

`VITE_WS_URL` is **only** for the local Vite dev server. For local development set it in
`dashboard-react/.env.local` (see `docs/LOCAL_DEVELOPMENT.md`); if unset, the local
dashboard falls back to polling (10-second intervals).

## Verify Deployment

### Check CDK Stacks
```bash
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
  --query "StackSummaries[?contains(StackName, 'AgenticDataIngestion')].StackName"
```

### Check Lambda Functions
```bash
aws lambda list-functions --query "Functions[?contains(FunctionName, 'agentic_data_ingestion')].FunctionName"
```

### Check MCP Endpoints

MCP endpoints are outputs of the AgentCore stack. Read them without redeploying:
```bash
aws cloudformation describe-stacks --stack-name AgenticDataIngestion-AgentCore-dev \
  --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
```

## Test the System

### Run Test Suite
```bash
make test-fast    # Unit + contract tests
make test-ci      # + integration tests (informational — carries known pre-existing failures)
```

Per-layer counts and timings are stated nowhere. [TESTING.md](TESTING.md) carries the command
that produces each figure, and the tier table for choosing which gate to run when. Note that
`make test-fast` must run alone.

### Upload Test Data
```bash
aws s3 cp tests/test-data/customer_data_1.csv \
  "s3://adi-source-files-$CDK_DEFAULT_ACCOUNT-$CDK_DEFAULT_REGION/test-pipeline/customer_data_1.csv"
```

### Check Pipeline Status
```bash
aws stepfunctions list-executions \
  --state-machine-arn $(aws stepfunctions list-state-machines \
    --query "stateMachines[?contains(name, 'agentic-data-ingestion')].stateMachineArn" --output text) \
  --max-results 5
```

## Next Steps

1. **Explore the Dashboard**: Create pipelines, upload files, monitor executions
2. **Review Architecture**: See [ARCHITECTURE.md](./ARCHITECTURE.md)
3. **Understand the Workflow**: See [WORKFLOW.md](./WORKFLOW.md)
4. **Configure Approvals**: Enable human-in-the-loop for schema mappings
5. **Customize AI Models**: Adjust model selection for cost/quality tradeoffs

## Common Issues

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) for solutions to common problems.

## Support

- Check CloudWatch Logs for Lambda errors
- Review Step Functions execution history
- Verify IAM permissions
- Ensure Bedrock model access is enabled in your region

---

This guide carries no version or "last updated" stamp. The project's release version is the one in [CHANGELOG.md](../CHANGELOG.md) and `deploy-cdk/package.json`, and a per-document date is a claim nobody re-verifies when one section changes. The `2.4` / `2026-06-09` stamp this footer used to carry predated most of the current guide. [WORKFLOW.md](WORKFLOW.md) records the same convention.
