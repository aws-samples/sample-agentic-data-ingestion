# MCP (Model Context Protocol) Servers

This directory contains 5 focused MCP server implementations for the Agentic Data Ingestion system. Each server is deployed as a separate AgentCore Runtime container via CDK (`deploy-cdk/lib/agentcore-stack.ts`).

## Access flags — least privilege (P28 R16)

Every server reads `MCP_ALLOW_WRITE` and `MCP_ALLOW_SENSITIVE_DATA_ACCESS` from its environment at startup and **defaults both to `false`**; only the exact string `"true"` (case-insensitive) enables a flag. No server hardcodes `allow_write=True` or `allow_sensitive_data_access=True`. The deployed values are pinned per-runtime in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — that is the single place to flip a flag, with a recorded rationale.

| Server | `MCP_ALLOW_WRITE` | `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | Why |
|--------|-------------------|-----------------------------------|-----|
| codegen_server | `false` | `false` | Worker-recommendation agent reads jobs/catalog only; Glue mutations happen in Lambda boto3 code |
| analytics_server | `false` | `false` | Read-only Athena/EMR analysis for worker recommendation |
| infra_server | `false` | `false` | Not wired to any Lambda; provisioning is boto3 in `create_infrastructure` |
| s3_tables | `false` | `false` | Not wired to any Lambda; table/namespace creation is boto3; write tools unregistered when off |
| data_processing | `false` | `false` | Legacy monolith, no consumers |

IAM-mutation tools (`add_inline_policy`, `create_data_processing_role`) are **never registered** on the infra and data_processing servers (P28 R16.2) — IAM changes remain a CDK/deploy-time concern. The per-agent tool allowlist is documented in `docs/AGENTS.md`.

## Directory Structure

### `codegen_server/`
Code generation MCP server — Glue ETL jobs, data catalog, interactive sessions (8 tools).
**Consumers:** `create_glue_pipeline`'s worker-recommendation agent (via `AGENTCORE_CODEGEN_ENDPOINT`); `create_code`/`test_code` receive the endpoint env var but do their AI work through the AgentCore proxy path.

### `infra_server/`
Infrastructure MCP server — crawlers, Glue commons, workflows, catalog connections, read-only IAM, S3 (19 tools registered, of 21 sites; IAM-mutation tools are never registered).
**Consumers:** none today — `create_infrastructure` provisions via boto3 directly.

### `analytics_server/`
Analytics MCP server — Athena queries/catalogs/workgroups, EMR clusters/steps/instances (~8 tools).
**Consumers:** `create_glue_pipeline` Lambda (worker optimization, via `AGENTCORE_ANALYTICS_ENDPOINT`).

### `s3_tables/`
S3 Tables MCP server — focused S3 Tables and Iceberg operations (10 read/compute tools; 7 write tools registered only when `MCP_ALLOW_WRITE=true`).
**Consumers:** none today — S3 Tables provisioning happens via boto3 in the Lambdas.

### `data_processing/`
Legacy monolithic data processing MCP server (retained for reference).
Contains archived development files and troubleshooting resources.
**Consumers:** none.

### `vendor/awslabs_aws_dataprocessing_mcp_server/`
The **single** vendored copy of the AWS Labs DataProcessing MCP server package
(P28 R19.7). The four dataprocessing-based servers (codegen, infra, analytics,
data_processing) all import `awslabs.aws_dataprocessing_mcp_server` from here —
each `server.py` falls back to this directory on `sys.path` locally, and each
Dockerfile COPYies `vendor/.../awslabs/` into the image (build context is
`mcp/`, wired in `deploy-cdk/lib/agentcore-stack.ts`). Upstream URL, vendored
version, and local modifications are documented in
`vendor/awslabs_aws_dataprocessing_mcp_server/PROVENANCE.md`. Do not copy the
tree back into a server directory — a guard test fails on re-duplication.

## Integration Architecture

```
Lambda Functions → Strands Agents → AgentCore Runtime → MCP Server → AWS Services
```

Each Lambda's `AGENTCORE_ENDPOINT` env var points to its specific MCP server runtime.

## Deployment

All MCP servers are deployed as AgentCore Runtime containers via CDK:

```bash
cd deploy-cdk
npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never
```

## Development

For local development and testing:

```bash
cd mcp/<server_name>

# Install dependencies
pip install -r requirements.txt

# Build Docker image
docker build -t agentic-data-ingestion-mcp-<server_name> .
```

## Authentication

All MCP communication uses AWS SigV4 authentication:
- **Service**: `bedrock-agentcore`
- **Region**: Configured per deployment
- **Credentials**: Lambda execution role or configured AWS credentials

## Monitoring

Monitor MCP server operations through:
- **AgentCore Console**: Runtime status and metrics
- **CloudWatch Logs**: MCP server logs and Lambda function logs
- **Lambda Metrics**: Function execution and error rates
