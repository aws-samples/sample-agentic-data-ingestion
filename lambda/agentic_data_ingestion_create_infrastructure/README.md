# Infrastructure Creation Lambda Function

## Description
This Lambda function conditionally creates AWS infrastructure for data pipelines based on target type. It uses S3 Tables MCP Server integration for intelligent Iceberg table creation with automatic fallback to manual creation.

## Purpose
- **Conditionally creates infrastructure** based on target type, for `NEW_S3_TABLE_BUCKET` **and** `S3_TABLES` (`lambda_function.py:784` accepts both)
- **Strands Agent Integration**: Uses intelligent agent with S3 Tables MCP tools for Iceberg table creation
- **Automatic Fallback**: Falls back to manual S3 bucket creation if agent is unavailable
- Creates S3 Table Buckets with Apache Iceberg format
- Generates proper Iceberg metadata structure with agent reasoning
- Provides intelligent configuration recommendations based on data characteristics
- Stores bucket info for downstream Glue job creation

## Conditional Execution Logic

### When Infrastructure is Created
- `target_type in ("NEW_S3_TABLE_BUCKET", "S3_TABLES")` - User uploaded schema to create a new S3 Table Bucket. Both tokens are accepted for compatibility; the comment at `lambda_function.py:783` records why.

> **The non-provisioning target types do not all take the same path, and two of them log a WARNING on a normal run.** `GLUE_CATALOG`, `JDBC` and `EXISTING_S3_TABLE_BUCKET` have named skip branches. `S3_PARQUET` and the legacy `RDS_DATABASE` alias have none, so they fall to the catch-all at `lambda_function.py:839`, which logs `Unknown target type: ..., skipping infrastructure creation` at WARNING and returns False. The behaviour is correct (skip), the severity is misleading. Do not read that log line as a defect.

### When Infrastructure is Skipped
- `target_type == "EXISTING_S3_TABLE_BUCKET"` - User selected existing S3 Table Bucket
- `target_type == "GLUE_CATALOG"` - Table already exists in Glue Catalog
- `target_type == "JDBC"` - Database connection already exists in Glue Catalog

## Strands Agent Integration

### Agent Architecture
- **Model**: Claude Sonnet 4.6 (us.anthropic.claude-sonnet-4-6)
- **MCP Server**: S3 Tables MCP Server via AgentCore
- **System Prompt**: S3 Tables and Apache Iceberg expert
- **Max Iterations**: 10

### Agent Capabilities
The agent intelligently:
1. Analyzes target schema to understand data characteristics
2. Creates S3 Table Bucket with optimal configuration
3. Designs namespace structure for organization
4. Recommends Iceberg settings based on data volume and query patterns
5. Creates Iceberg table with proper metadata
6. Provides rationale for all configuration decisions

### S3 Tables MCP Tools Available to Agent
1. **agentcore_create_s3_table_bucket** - Creates S3 Table Bucket
2. **agentcore_create_s3_table_namespace** - Creates namespace for table organization
3. **agentcore_recommend_iceberg_config** - Gets optimal Iceberg configuration
4. **agentcore_create_s3_table** - Creates Iceberg table in bucket
5. **agentcore_get_s3_table_metadata_location** - Retrieves metadata location

### Agent Decision Making
The agent considers:
- Number of fields in schema
- Estimated data volume
- Estimated record count
- Query patterns (analytical, operational, mixed)
- AWS best practices for data lakes
- Iceberg optimization strategies

### Fallback Behavior
If agent is unavailable or fails:
- Creates standard S3 bucket with versioning
- Generates Iceberg metadata files manually
- Stores metadata in S3 for Glue job access
- Logs fallback usage for monitoring

## Requirements Addressed
- Requirement 4: Pipeline Creation Enhancement - Capture target details during pipeline creation
- Requirement 5: S3 Table Bucket Creation - Create S3 Table Buckets with Iceberg format
- Requirement 11: Python and Glue Version Management - Use Python 3.12 runtime
- Requirement 12: S3 Tables MCP Server Deployment - Integrate with AgentCore MCP

## Input
```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "code_version_id": "string",
  "target_schema": {
    "name": "string",
    "fields": [...]
  },
  "target_type": "New S3 Bucket"
}
```

## Output

### When Infrastructure Created
```json
{
  "statusCode": 200,
  "run_id": "string",
  "pipeline_id": "string",
  "infrastructure_status": {
    "overall_status": "success",
    "s3_table_bucket": {
      "bucket_name": "agentic-data-ingestion-table-{pipeline_id}-{account_id}",
      "table_name": "string",
      "table_bucket_arn": "arn:aws:s3tables:...",
      "table_arn": "arn:aws:s3tables:...",
      "namespace": ["agentic_data_ingestion", "pipeline_id"],
      "data_location": "s3://bucket/data/table/",
      "metadata_location": "s3://bucket/_metadata/...",
      "warehouse_location": "s3://bucket/warehouse/...",
      "iceberg_config": {...},
      "agent_used": true,
      "agent_rationale": "Configured for 5GB dataset with 100K records..."
    }
  },
  "agent_integration": {
    "enabled": true,
    "model": "us.anthropic.claude-sonnet-4-6",
    "mcp_server": "S3 Tables MCP"
  }
}
```

### When Infrastructure Skipped
```json
{
  "statusCode": 200,
  "status": "skipped",
  "skip_reason": "Using existing S3 Table Bucket",
  "target_config": {...}
}
```

## Environment Variables
- `PIPELINE_RUNS_TABLE` - DynamoDB table for pipeline state (default: agentic_data_ingestion_state)
- `PIPELINES_TABLE` - DynamoDB table for pipeline metadata (default: agentic_data_ingestion_pipelines)
- `AWS_ACCOUNT_ID` - AWS account ID
- `AWS_REGION` - AWS region (default: us-east-1)
- `AGENTCORE_S3TABLES_ENDPOINT` - AgentCore S3 Tables MCP endpoint URL

## AWS Permissions Required
- **S3 Tables**: CreateTableBucket, CreateTable, GetTable, GetTableMetadataLocation
- **S3**: CreateBucket, PutObject, PutBucketVersioning
- **DynamoDB**: GetItem, PutItem, UpdateItem
- **STS**: GetCallerIdentity
- **Execute API**: Invoke AgentCore MCP endpoint

## Dependencies
- boto3>=1.28.0
- strands-agents>=0.1.0

## Implementation Status
✅ **Implemented** - Strands Agent with S3 Tables MCP integration and automatic fallback