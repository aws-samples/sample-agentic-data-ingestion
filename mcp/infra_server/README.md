# Infrastructure MCP Server

One of 3 focused MCP servers split from the monolithic `data_processing` server. This one handles infrastructure provisioning — crawlers, security configs, workflows, catalog connections/partitions, read-only IAM inspection, and S3 operations.

Not currently wired to any Lambda: `create_infrastructure` provisions S3 Tables/Glue resources via boto3 directly under its own IAM role. The runtime is deployed for future use and runs read-only.

## Access flags (P28 R16.1)

| Flag | Deployed value | Rationale |
|------|----------------|-----------|
| `MCP_ALLOW_WRITE` | `false` (default) | No consumer performs infrastructure mutations through MCP — `create_infrastructure` uses boto3 under its own IAM role. |
| `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | `false` (default) | No consumer needs to read customer data rows through this server. |

Both flags are read from the environment at startup (`server.py:_env_flag`) and default to `false`; only the exact string `"true"` (case-insensitive) enables them. The deployed values are pinned in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — flip them there, with a recorded rationale, if a reviewed write need appears.

### IAM-mutation tools removed (P28 R16.2)

`add_inline_policy` and `create_data_processing_role` are **not registered** on this server (`CommonResourceHandler(..., register_iam_mutation_tools=False)`). Agents reachable through MCP process user-supplied data (CSV headers), so IAM mutation must not be reachable even behind `allow_write` — IAM changes remain a CDK/deploy-time concern. Read-only IAM inspection (`get_policies_for_role`, `get_roles_for_service`) remains available.

## Tools

| Tool | Handler | Purpose |
|------|---------|---------|
| `manage_aws_glue_crawlers` | CrawlerHandler | Create, start, get crawlers |
| `manage_aws_glue_classifiers` | CrawlerHandler | Manage Glue classifiers |
| `manage_aws_glue_crawler_management` | CrawlerHandler | Crawler lifecycle operations |
| `manage_aws_glue_usage_profiles` | GlueCommonsHandler | Glue usage profiles |
| `manage_aws_glue_security_configurations` | GlueCommonsHandler | Security configurations |
| `manage_aws_glue_encryption` | GlueCommonsHandler | Encryption settings |
| `manage_aws_glue_resource_policies` | GlueCommonsHandler | Resource-based policies |
| `manage_aws_glue_workflows` | GlueWorkflowAndTriggerHandler | Create/manage workflows |
| `manage_aws_glue_triggers` | GlueWorkflowAndTriggerHandler | Create/manage triggers |
| `manage_aws_glue_connections` | GlueDataCatalogHandler | JDBC/network connections |
| `manage_aws_glue_partitions` | GlueDataCatalogHandler | Table partition management |
| `get_policies_for_role` | CommonResourceHandler | List policies on a role (read-only) |
| `get_roles_for_service` | CommonResourceHandler | List roles for a service (read-only) |
| `list_s3_buckets` | CommonResourceHandler | List S3 buckets |
| `upload_to_s3` | CommonResourceHandler | Upload objects to S3 (write-gated) |
| `analyze_s3_usage_for_data_processing` | CommonResourceHandler | Analyze S3 for ETL planning |

Mutating operations within the `manage_aws_glue_*` tools are additionally runtime-gated on `allow_write` inside the awslabs handlers (read-only operations pass, mutations are refused when the flag is off).

## Local Development

```bash
pip install -r requirements.txt
python server.py
```

The server starts on `http://0.0.0.0:8000` using FastMCP with `streamable-http` transport and `stateless_http=True`.

Requires AWS credentials in the environment (`AWS_REGION` defaults to `us-east-1`).

## Docker

```bash
docker build -t agentic-data-ingestion-mcp-infra .
docker run -p 8000:8000 -e AWS_REGION=us-west-2 agentic-data-ingestion-mcp-infra
```

## Deployment

Deployed as a Bedrock AgentCore Runtime resource (`agentic-data-ingestion-mcp-infra`) via CDK in `deploy-cdk/lib/agentcore-stack.ts`.

Authentication uses AWS SigV4 (client-side transport: `shared/streamable_http_sigv4.py`, service: `bedrock-agentcore`).

## Directory Structure

```
infra_server/
├── server.py                    # Entry point — registers handlers, starts FastMCP
├── Dockerfile                   # Container image for AgentCore
├── requirements.txt             # Python dependencies
└── (awslabs imported from ../vendor/awslabs_aws_dataprocessing_mcp_server/ — the single vendored copy; see PROVENANCE.md there)
```
