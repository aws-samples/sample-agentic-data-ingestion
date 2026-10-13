# AWS S3 Tables MCP Server for AgentCore

This MCP server provides AWS S3 Tables tools for AgentCore Runtime, enabling Strands Agents to manage S3 Table Buckets and Apache Iceberg tables.

Not currently wired to any Lambda: `create_infrastructure` and `create_glue_pipeline` create table buckets, namespaces, and tables via boto3 directly under their own IAM roles. The runtime is deployed for future use and runs read-only.

## Access flags (P28 R16.1)

| Flag | Deployed value | Rationale |
|------|----------------|-----------|
| `MCP_ALLOW_WRITE` | `false` (default) | No consumer performs S3 Tables mutations through MCP — table/namespace/bucket creation happens in Lambda code via boto3. With the flag off, the mutation tools (create/rename/delete tables, namespaces, buckets, and table policies) are **not registered at all**, so they never appear in the tool list. |
| `MCP_ALLOW_SENSITIVE_DATA_ACCESS` | `false` (default) | No tool in this server reads customer data rows; the flag is accepted for parity with the awslabs handlers and currently has no effect. |

Both flags are read from the environment at startup (`agentcore_s3tables_mcp.py:_env_flag`) and default to `false`; only the exact string `"true"` (case-insensitive) enables them. The deployed values are pinned in `deploy-cdk/lib/agentcore-stack.ts` (`mcpServerEnv`) — flip them there, with a recorded rationale, if a reviewed write need appears.

## Features

### S3 Table Bucket Management
- Create and manage S3 Table Buckets
- List and get bucket details
- Bucket policy management

### Namespace Operations
- Create and list namespaces
- Namespace-based table organization

### Table Operations
- Create, list, and get S3 tables
- Rename and delete tables
- Get table metadata locations
- Table policy management

### Iceberg Optimization
- Recommend optimal Iceberg configurations
- Estimate S3 Tables costs
- Partition strategy recommendations
- Compression and file size optimization

## Architecture

This server is designed to run in AgentCore Runtime alongside the Data Processing MCP server, providing a unified set of tools for AWS data operations.

```
┌─────────────────────────────────────────┐
│      AgentCore Runtime Container        │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │  Data Processing MCP Server      │  │
│  │  (Port 8000)                     │  │
│  └──────────────────────────────────┘  │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │  S3 Tables MCP Server            │  │
│  │  (Port 8001)                     │  │
│  └──────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

## Deployment

### Prerequisites
- AWS Account with S3 Tables enabled
- AgentCore Runtime deployed
- ECR repository for container images
- IAM role with S3 Tables permissions

### Build and Deploy

1. Build the Docker image:
```bash
cd agentic_data_ingestion/mcp/s3_tables
docker build -t agentcore-s3tables-mcp .
```

2. Push to ECR:
```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com
docker tag agentcore-s3tables-mcp:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/agentic-data-ingestion-mcp:s3tables
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/agentic-data-ingestion-mcp:s3tables
```

3. Deploy to AgentCore Runtime:
```bash
cd ../../deploy
./deploy-s3tables-mcp.sh
```

## IAM Permissions Required

The AgentCore Runtime role needs the following permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3tables:*"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "glue:GetTable",
        "glue:GetDatabase"
      ],
      "Resource": "*"
    }
  ]
}
```

## Available Tools

Always registered (read-only / pure computation):

### Connectivity
- `test_s3tables_connection` - Verify S3 Tables service connectivity and permissions

### Bucket Discovery
- `agentcore_list_s3_table_buckets` - List all table buckets
- `agentcore_get_s3_table_bucket` - Get bucket details

### Namespace Discovery
- `agentcore_list_s3_table_namespaces` - List namespaces

### Table Discovery
- `agentcore_list_s3_tables` - List tables in a namespace
- `agentcore_get_s3_table` - Get table details
- `agentcore_get_s3_table_metadata_location` - Get metadata location
- `agentcore_get_s3_table_policy` - Get resource policy

### Optimization Tools
- `agentcore_recommend_iceberg_config` - Get Iceberg configuration recommendations
- `agentcore_estimate_s3_tables_cost` - Estimate monthly costs

Registered only when `MCP_ALLOW_WRITE=true` (P28 R16.1):

### Bucket Management
- `agentcore_create_s3_table_bucket` - Create a new S3 Table Bucket

### Namespace Management
- `agentcore_create_s3_table_namespace` - Create a namespace

### Table Management
- `agentcore_create_s3_table` - Create a new table
- `agentcore_rename_s3_table` - Rename a table
- `agentcore_delete_s3_table` - Delete a table

### Policy Management
- `agentcore_put_s3_table_policy` - Set resource policy
- `agentcore_delete_s3_table_policy` - Delete resource policy

## Usage Example

```python
from strands import Agent
from strands.tools.mcp import MCPClient

# Initialize MCP client
mcp_client = MCPClient(
    lambda: streamablehttp_client(
        url=f"{agentcore_endpoint}/mcp",
        headers={"Authorization": f"Bearer {session_token}"}
    )
)

# Get tools from both MCP servers
with mcp_client:
    tools = mcp_client.list_tools_sync()
    
    # Create agent with all MCP tools
    agent = Agent(
        model="us.anthropic.claude-sonnet-4-6",
        tools=tools,
        system_prompt="You are an AWS data engineer..."
    )
    
    # Agent can now use both Data Processing and S3 Tables tools
    result = agent.run("Create an S3 Table Bucket for my pipeline")
```

## Testing

Test the MCP server locally:

```bash
# Run the server
python agentcore_s3tables_mcp.py

# In another terminal, test with curl
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"method": "tools/list"}'
```

## Monitoring

The server includes:
- Health check endpoint at `/health`
- Structured logging with loguru
- AWS error handling and retry logic
- CloudWatch metrics integration

## Troubleshooting

### Common Issues

1. **S3 Tables not available in region**
   - S3 Tables is only available in specific regions
   - Check AWS documentation for regional availability

2. **Permission denied errors**
   - Verify IAM role has s3tables:* permissions
   - Check resource policies on table buckets

3. **Container fails to start**
   - Check CloudWatch logs for the AgentCore Runtime
   - Verify ECR image is accessible
   - Ensure health check endpoint is responding

## File Structure

```
s3_tables/
├── agentcore_s3tables_mcp.py    # Main production MCP server
├── Dockerfile                   # Container build configuration
├── requirements.txt             # Python dependencies
├── README.md                    # This documentation
└── archive/                     # Experimental and debug versions
    ├── README.md                # Archive documentation
    └── *.py                     # Development versions
```

The `archive/` folder contains experimental versions created during development and debugging. See `archive/README.md` for details about the development process and lessons learned.

## References

- [AWS S3 Tables Documentation](https://docs.aws.amazon.com/s3/latest/userguide/s3-tables.html)
- [Apache Iceberg Documentation](https://iceberg.apache.org/)
- [AgentCore Runtime Documentation](https://docs.aws.amazon.com/bedrock-agentcore/)
- [MCP Protocol Specification](https://modelcontextprotocol.io/)
- [MCP Integration](../../docs/MCP-INTEGRATION.md) — server profiles, tool inventory, and which agents reach this server
