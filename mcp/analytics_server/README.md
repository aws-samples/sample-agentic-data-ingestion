# Analytics MCP Server

One of 3 focused MCP servers split from the monolithic `data_processing` server. This one handles query and analytics operations — Athena queries, data catalogs, workgroups, and EMR cluster management.

Used by the `create_glue_pipeline` Lambda function (alongside the codegen server) via `AGENTCORE_ANALYTICS_ENDPOINT`.

## Access flags (P28 R16.1)

| Flag | Deployed value | Rationale |
|------|----------------|-----------|
| `MCP_ALLOW_WRITE` | `false` (default) | The only consumer (create_glue_pipeline's worker-recommendation agent) issues read-only analysis calls — SELECT Athena queries and describe/list operations. Every actual AWS mutation is performed by Lambda code through boto3 under the Lambda's own IAM role. |
| `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | `false` (default) | No consumer needs to read customer data rows through this server. |

Both flags are read from the environment at startup (`server.py:_env_flag`) and default to `false`; only the exact string `"true"` (case-insensitive) enables them. The deployed values are pinned in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — flip them there, with a recorded rationale, if a reviewed write need appears.

## Tools

| Tool | Handler | Purpose |
|------|---------|---------|
| `manage_aws_athena_query_executions` | AthenaQueryHandler | Start/stop/get Athena queries |
| `manage_aws_athena_named_queries` | AthenaQueryHandler | Saved query management |
| `manage_aws_athena_data_catalogs` | AthenaDataCatalogHandler | Athena data catalog operations |
| `manage_aws_athena_databases_and_tables` | AthenaDataCatalogHandler | Database/table discovery via Athena |
| `manage_aws_athena_workgroups` | AthenaWorkGroupHandler | Workgroup configuration |
| `manage_aws_emr_clusters` | EMREc2ClusterHandler | Create/describe/terminate EMR clusters |
| `manage_aws_emr_ec2_steps` | EMREc2StepsHandler | Submit and monitor EMR steps |
| `manage_aws_emr_ec2_instances` | EMREc2InstanceHandler | List/describe EMR instances |

> **Note:** The design doc mentions ~15 tools including CloudWatch metrics and cost estimation, but those handlers don't exist yet. They can be added when implemented.

## Local Development

```bash
pip install -r requirements.txt
python server.py
```

The server starts on `http://0.0.0.0:8000` using FastMCP with `streamable-http` transport and `stateless_http=True`.

Requires AWS credentials in the environment (`AWS_REGION` defaults to `us-east-1`).

## Docker

```bash
docker build -t agentic-data-ingestion-mcp-analytics .
docker run -p 8000:8000 -e AWS_REGION=us-west-2 agentic-data-ingestion-mcp-analytics
```

## Deployment

Deployed as a Bedrock AgentCore Runtime resource (`agentic-data-ingestion-mcp-analytics`) via CDK in `deploy-cdk/lib/agentcore-stack.ts`.

Authentication uses AWS SigV4 (client-side transport: `shared/streamable_http_sigv4.py`, service: `bedrock-agentcore`).

## Directory Structure

```
analytics_server/
├── server.py                    # Entry point — registers handlers, starts FastMCP
├── Dockerfile                   # Container image for AgentCore
├── requirements.txt             # Python dependencies
└── (awslabs imported from ../vendor/awslabs_aws_dataprocessing_mcp_server/ — the single vendored copy; see PROVENANCE.md there)
```
