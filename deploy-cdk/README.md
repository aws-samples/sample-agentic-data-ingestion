# Agentic Data Ingestion CDK — Infrastructure as Code

AWS CDK v2 (TypeScript) deployment for Agentic Data Ingestion. 8 stacks covering all infrastructure, plus an optional opt-in test-fixtures stack.

## Prerequisites

- Node.js 18+
- Docker (for Lambda layer and MCP image builds)
- AWS CLI configured with credentials
- CDK bootstrapped in **both** the app region and us-east-1 (the EdgeWaf stack deploys to us-east-1 and cross-region references require it): `npx cdk bootstrap aws://ACCOUNT/REGION` **and** `npx cdk bootstrap aws://ACCOUNT/us-east-1`

## Quick Start

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2

npm install

# Bootstrap both the app region and us-east-1 (first time only)
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION
npx cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/us-east-1

npx cdk deploy --all --require-approval never
```

## Stacks

| Stack | Resources | Depends On |
|-------|-----------|------------|
| `AgenticDataIngestion-Foundation-{env}` | IAM roles, ECR repo, 9 S3 buckets | — |
| `AgenticDataIngestion-Data-{env}` | 15 DynamoDB tables, EventBridge bus, DLQ | — |
| `AgenticDataIngestion-Auth-{env}` | Cognito User Pool (email sign-in, no self-signup, TOTP MFA required by default), Hosted UI domain | — |
| `AgenticDataIngestion-AgentCore-{env}` | 5 MCP servers, 9 agent containers, Memory resource | Foundation |
| `AgenticDataIngestion-Compute-{env}` | 21 Lambda functions, REST API Gateway (Cognito authorizer), WebSocket API (inline, Lambda authorizer on `$connect`), regional WAF (X-Origin-Verify), Strands layer | Foundation, Data, AgentCore, Auth |
| `AgenticDataIngestion-Orchestration-{env}` | Step Functions, EventBridge rules, CloudWatch alarms, SNS | Compute, Data, Foundation |
| `AgenticDataIngestion-EdgeWaf-{env}` | CLOUDFRONT-scope WAF (managed rules + rate limit) — deploys to **us-east-1** | — |
| `AgenticDataIngestion-Hosting-{env}` | Private S3 + CloudFront (OAC, SPA fallback, `/api/*` proxy), CLOUDFRONT WAF via `webAclId`, Cognito app client, `config.json` deployment | Compute, Auth, EdgeWaf |

Foundation, Data, Auth, and EdgeWaf have no cross-dependencies (EdgeWaf deploys to us-east-1). AgentCore depends only on Foundation. Compute waits for Foundation + Data + AgentCore + Auth. Orchestration follows Compute. Hosting deploys last (needs Compute + Auth + EdgeWaf) and attaches the EdgeWaf web ACL via cross-region references.

### Optional: test-fixtures stack

| Stack | Resources | Gate |
|-------|-----------|------|
| `AgenticDataIngestion-TestFixtures-{env}` | MySQL 8.0 source + Aurora Postgres 15 target, secrets, security groups, Glue Connections | `-c adi:deployTestFixtures=true` |

Not instantiated by a plain `cdk deploy --all`. Used for JDBC source/target
pipeline testing. See [QUICKSTART.md](../QUICKSTART.md#6-optional-provision-jdbc-test-databases).

## Commands

```bash
npx cdk synth          # Synthesize CloudFormation (no deploy)
npx cdk diff           # Preview changes
npx cdk deploy --all   # Deploy all stacks
npx cdk destroy --all  # Destroys 7 of 8 stacks — see the teardown caveat below

# Deploy individual stacks
npx cdk deploy AgenticDataIngestion-Foundation-dev --exclusively
npx cdk deploy AgenticDataIngestion-Data-dev --exclusively
npx cdk deploy AgenticDataIngestion-Auth-dev --exclusively
npx cdk deploy AgenticDataIngestion-AgentCore-dev --exclusively
npx cdk deploy AgenticDataIngestion-Compute-dev --exclusively
npx cdk deploy AgenticDataIngestion-Orchestration-dev --exclusively
npx cdk deploy AgenticDataIngestion-EdgeWaf-dev --exclusively
npx cdk deploy AgenticDataIngestion-Hosting-dev --exclusively

# Deploy the optional JDBC test-fixtures stack
npx cdk deploy AgenticDataIngestion-TestFixtures-dev -c adi:deployTestFixtures=true --exclusively
```

## What CDK builds automatically

- Strands Lambda layer via Docker bundling (strands-agents, mcp, jinja2, aws-lambda-powertools, shared modules)
- MCP Docker images for AgentCore Runtime
- S3 deployment of prompt templates and PySpark templates
- Cross-stack references without circular dependency issues
- Asset hashing (only redeploys changed resources)
- Bucket emptying and ECR cleanup on `cdk destroy` (all ten buckets are
  `RemovalPolicy.DESTROY` + `autoDeleteObjects`; the ECR repo is `emptyOnDelete`)

## Teardown caveat

`cdk destroy --all` does not remove everything. The Data stack sets
`terminationProtection: true` and 13 of its 17 DynamoDB tables are
`RemovalPolicy.RETAIN` (`lib/data-stack.ts:35`, `:42`), so `destroy --all`
destroys seven stacks and **fails on Data**, leaving the tables in place and
still billing for storage and PITR backups. Those protections are deliberate —
do not weaken them to make a teardown command look tidier. The explicit removal
procedure is in [QUICKSTART.md](../QUICKSTART.md#teardown).

## Environment configuration

```bash
# Dev (default)
npx cdk deploy --all

# Prod
npx cdk deploy --all -c environment=prod -c notificationEmail=ops@example.com
```

**Deploy one instance per dedicated AWS account, and use separate accounts for separate
environments.** `environment` selects configuration and labels the stacks; it does not namespace
every physical resource, so `-c environment=prod` beside an existing `dev` deployment is not a
second, independent environment:

- **Same account and region:** the DynamoDB table names (`lib/data-stack.ts`) and AgentCore runtime
  names (`lib/agentcore-stack.ts`) are fixed, and the S3 bucket names (`lib/foundation-stack.ts`,
  `lib/hosting-stack.ts`) carry the account and region but not the environment. The second deploy
  collides with the first.
- **Another region, same account:** IAM role and managed-policy names are account-wide and carry
  the environment but not the region, and `AgenticDataIngestion-VerificationSession-Role` is fixed
  outright. The EdgeWaf stack deploys to us-east-1 whichever region the app uses.
- **After teardown:** the retained tables described under [Teardown caveat](#teardown-caveat) stay
  in place under the same names, so they block a fresh deploy into that account and region until
  they are removed.

Namespacing every resource by environment would rename retained tables and needs a migration; it
is not part of this sample.

`notificationEmail` is optional. Leave it unset and no email subscription is
created — the SNS topics are still created and still receive published messages,
you just will not be emailed. Set it to receive critical alerts and approval
requests, and confirm the SNS subscription email AWS sends you.

## Tests

```bash
npm test  # Runs integration tests (requires deployed stacks)
```
