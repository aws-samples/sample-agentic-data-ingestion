# AWS Data Processing MCP Server - AgentCore Compatible

A Model Context Protocol (MCP) server that provides comprehensive AWS data processing capabilities for Amazon Bedrock AgentCore runtime. This implementation follows the AWS Labs sample structure with modular handlers and is optimized for AgentCore deployment.

> **⚠️ CRITICAL**: This server uses **FastMCP framework** as required by AgentCore deployment success criteria. Using `bedrock_agentcore.mcp.MCPServer` causes internal initialization errors on AgentCore Runtime.

**Status: legacy.** The focused `codegen_server`, `infra_server`, and `analytics_server` profiles replaced this monolith; no Lambda or agent is wired to it today. It is retained for backward compatibility and runs read-only.

## Access flags (P28 R16.1)

| Flag | Deployed value | Rationale |
|------|----------------|-----------|
| `MCP_ALLOW_WRITE` | `false` (default) | Legacy server with no consumers — nothing performs mutations through it. |
| `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | `false` (default) | No consumer needs to read customer data rows through this server. |

Both flags are read from the environment at startup (`server.py:_env_flag`) and default to `false`; only the exact string `"true"` (case-insensitive) enables them. The deployed values are pinned in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — flip them there, with a recorded rationale, if a reviewed write need appears.

The IAM-mutation tools (`add_inline_policy`, `create_data_processing_role`) are **not registered** on this server either (P28 R16.2, `register_iam_mutation_tools=False`) — IAM changes remain a CDK/deploy-time concern project-wide. Read-only IAM inspection remains available.

## Features

This MCP server provides tools for managing AWS data processing services:

### AWS Glue Interactive Sessions
- **manage_aws_glue_sessions**: Create, get, list, and delete Glue interactive sessions
- **manage_aws_glue_statements**: Run code, get results, list and cancel statements in sessions

### AWS Glue ETL Jobs
- **manage_aws_glue_jobs**: Get job details, start job runs, monitor job execution

### AWS Glue Crawlers
- **manage_aws_glue_crawlers**: Manage crawlers, start/stop crawling, get metrics

### AWS Glue Data Catalog
- **manage_aws_glue_catalog**: Manage databases, tables, and partitions in the Data Catalog

### AWS S3 Operations
- **manage_aws_s3_objects**: List, get, put, delete S3 objects with comprehensive metadata
- **manage_aws_s3_buckets**: List and get bucket information

### AWS DynamoDB Operations
- **manage_aws_dynamodb_tables**: List tables, get table details, scan and query data
- **manage_aws_dynamodb_items**: Get, put, update, delete individual items

## Deployment for Bedrock AgentCore

This server is designed to be deployed as a container in Amazon Bedrock AgentCore runtime.

### Prerequisites

- Docker
- AWS credentials configured
- Access to Amazon Bedrock AgentCore

### Building the Container

```bash
docker build -t aws-dataprocessing-mcp-server .
```

### Running Locally

```bash
docker run -it \
  -e AWS_ACCESS_KEY_ID=your_access_key \
  -e AWS_SECRET_ACCESS_KEY=your_secret_key \
  -e AWS_DEFAULT_REGION=us-east-1 \
  aws-dataprocessing-mcp-server
```

### Environment Variables

- `AWS_ACCESS_KEY_ID`: AWS access key
- `AWS_SECRET_ACCESS_KEY`: AWS secret key  
- `AWS_DEFAULT_REGION`: AWS region (default: us-east-1)
- `AWS_SESSION_TOKEN`: AWS session token (if using temporary credentials)

## Tool Usage Examples

### Glue Interactive Sessions

```python
# Create a new Glue session
{
  "operation": "create-session",
  "session_id": "my-session",
  "role": "arn:aws:iam::123456789012:role/GlueServiceRole",
  "glue_version": "4.0"
}

# Run PySpark code
{
  "operation": "run-statement", 
  "session_id": "my-session",
  "code": "df = spark.read.parquet('s3://my-bucket/data/')\ndf.show()"
}
```

### S3 Operations

```python
# List objects in bucket
{
  "operation": "list-objects",
  "bucket_name": "my-data-bucket",
  "prefix": "raw-data/",
  "max_keys": 100
}

# Get object content
{
  "operation": "get-object",
  "bucket_name": "my-data-bucket", 
  "key": "data/file.json"
}
```

### DynamoDB Operations

```python
# Scan table
{
  "operation": "scan",
  "table_name": "my-table",
  "limit": 50
}

# Get item
{
  "operation": "get-item",
  "table_name": "my-table",
  "key": {"id": {"S": "123"}}
}
```

## Error Handling

All tools return structured responses with:
- `success`: Boolean indicating operation success
- `error`: Error message if operation failed
- Tool-specific data fields

## Security

- Uses AWS IAM for authentication and authorization
- Supports AWS SigV4 request signing
- No hardcoded credentials in code
- Follows AWS security best practices

## Architecture

The server follows the AWS Labs modular structure. The `awslabs` package is
the single vendored copy at `../vendor/awslabs_aws_dataprocessing_mcp_server/`
(P28 R19.7 — see `PROVENANCE.md` there):

```
awslabs/aws_dataprocessing_mcp_server/
├── __init__.py                 # Package version
├── server.py                   # Main MCP server (AgentCore compatible)
├── utils/                      # Utility modules
│   ├── aws_helper.py          # AWS client management and helpers
│   ├── consts.py              # Constants and configuration
│   └── logging_helper.py      # Logging utilities
├── handlers/                   # Service-specific handlers
│   ├── glue/                  # AWS Glue handlers
│   ├── athena/                # Amazon Athena handlers
│   ├── emr/                   # Amazon EMR handlers
│   └── commons/               # Common resource handlers
├── models/                     # Pydantic models for responses
└── core/                      # Core functionality
```

### Technology Stack:
- **FastMCP**: Model Context Protocol framework (REQUIRED for AgentCore compatibility)
- **boto3**: AWS SDK for Python  
- **pydantic**: Data validation and serialization
- **loguru**: Advanced logging
- **streamable-http**: HTTP transport for AgentCore runtime

## Contributing

When adding new tools:
1. Follow the existing pattern with `@mcp.tool` decorator
2. Use `@handle_aws_error` for AWS API calls
3. Return structured responses with `success` field
4. Include comprehensive error handling
5. Document parameters and return values