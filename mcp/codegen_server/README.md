# Codegen MCP Server

One of 3 focused MCP servers split from the monolithic `data_processing` server. This one handles code generation — Glue ETL job CRUD, interactive sessions for testing PySpark, and Data Catalog reads for schema discovery.

Exposed to the `create_code`, `test_code`, and `create_glue_pipeline` Lambdas via `AGENTCORE_ENDPOINT` / `AGENTCORE_CODEGEN_ENDPOINT`. In practice only `create_glue_pipeline`'s worker-recommendation agent opens an MCP connection today; `create_code` and `test_code` do their AI work through the AgentCore proxy path and perform AWS calls via boto3.

## Access flags (P28 R16.1)

| Flag | Deployed value | Rationale |
|------|----------------|-----------|
| `MCP_ALLOW_WRITE` | `false` (default) | The only active consumer (create_glue_pipeline's worker-recommendation agent) issues read-only analysis calls — listing jobs, databases, and tables to inform configuration. Glue job creation/starts are performed by Lambda code through boto3 under the Lambda's own IAM role, never through MCP. |
| `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | `false` (default) | No consumer needs to read customer data rows through this server. |

Both flags are read from the environment at startup (`server.py:_env_flag`) and default to `false`; only the exact string `"true"` (case-insensitive) enables them. The deployed values are pinned in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — flip them there, with a recorded rationale, if a reviewed write need appears.

## Tools

| Tool | Handler | Purpose |
|------|---------|---------|
| `manage_aws_glue_jobs` | GlueEtlJobsHandler | Create, update, get, start ETL jobs; fetch run logs |
| `manage_aws_glue_sessions` | GlueInteractiveSessionsHandler | Create/manage Glue interactive sessions |
| `manage_aws_glue_statements` | GlueInteractiveSessionsHandler | Run statements in interactive sessions |
| `manage_aws_glue_databases` | GlueDataCatalogHandler | List and describe Glue databases |
| `manage_aws_glue_tables` | GlueDataCatalogHandler | List and describe Glue tables |
| `manage_aws_glue_catalog` | GlueDataCatalogHandler | General catalog operations |

## Local Development

```bash
pip install -r requirements.txt
python server.py
```

The server starts on `http://0.0.0.0:8000` using FastMCP with `streamable-http` transport and `stateless_http=True`.

Requires AWS credentials in the environment (`AWS_REGION` defaults to `us-east-1`).

## Docker

```bash
docker build -t agentic-data-ingestion-mcp-codegen .
docker run -p 8000:8000 -e AWS_REGION=us-west-2 agentic-data-ingestion-mcp-codegen
```

## Deployment

Deployed as a Bedrock AgentCore Runtime resource (`agentic-data-ingestion-mcp-codegen`) via CDK in `deploy-cdk/lib/agentcore-stack.ts`.

Authentication uses AWS SigV4 (client-side transport: `shared/streamable_http_sigv4.py`, service: `bedrock-agentcore`).

## Directory Structure

```
codegen_server/
├── server.py                    # Entry point — registers handlers, starts FastMCP
├── Dockerfile                   # Container image for AgentCore
├── requirements.txt             # Python dependencies
└── (awslabs imported from ../vendor/awslabs_aws_dataprocessing_mcp_server/ — the single vendored copy; see PROVENANCE.md there)
```
