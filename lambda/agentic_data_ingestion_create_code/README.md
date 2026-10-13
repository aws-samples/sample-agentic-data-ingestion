# Agentic Data Ingestion Code Generator - Strands Agent

## Description
This Strands Agent generates production-quality PySpark transformation code from approved field mappings using Bedrock AgentCore.

## Overview

The Code Generator Agent is a Bedrock AgentCore-deployed Strands agent that:
- Loads PySpark templates from S3
- Generates transformation code using Claude Sonnet 4.6
- Validates syntax automatically
- Fixes validation errors autonomously
- Supports MCP tools for enhanced capabilities
- Writes to S3 Tables (Iceberg format) by default

## Features

✅ **Template-based Generation** - Uses proven PySpark patterns
✅ **Autonomous Validation** - Checks and fixes syntax errors
✅ **MCP Tool Support** - Can access AWS Glue documentation
✅ **DynamoDB Integration** - Stores generated code for tracking
✅ **Step Functions Ready** - Designed for orchestration
✅ **S3 Tables Support** - Configurable S3 table bucket name

## Requirements Addressed
- Requirement 5.1: Create ingestion code based on mapping when mapping is available
- Requirement 5.2: Store generated code in DynamoDB for testing and deployment
- Requirement 5.3: Provide specific error details when code generation fails
- **NEW**: Manual approval required before code generation
- **NEW**: Configurable S3 table bucket name in pipeline UI

## Configuration

### Environment Variables

```bash
TEMPLATES_BUCKET=adi-templates-dev
PIPELINES_TABLE=AgenticDataIngestion-Pipelines-dev
CODE_TABLE=AgenticDataIngestion-GeneratedCode-dev
TESTING_LAMBDA=AgenticDataIngestion-CodeTester-dev
MCP_CONFIG_PATH=/tmp/mcp_config.json
```

### Agent Configuration

See `agent_config.json` for Bedrock AgentCore configuration.

## Input Event

```json
{
  "pipeline_id": "pipe-123",
  "mapping_id": "map-456",
  "source_config": {
    "type": "S3",
    "format": "parquet",
    "location": "s3://bucket/data/",
    "schema": {...}
  },
  "target_config": {
    "type": "S3_TABLES",
    "s3_table_bucket": "my-data-bucket",
    "table_name": "customer_master",
    "namespace": "default",
    "format": "iceberg",
    "schema": {...}
  },
  "mappings": [
    {
      "source_field": "customer_id",
      "target_field": "cust_id",
      "transformation": "DIRECT"
    }
  ],
  "approved_by": "user@example.com"
}
```

## Output

```json
{
  "statusCode": 200,
  "body": {
    "message": "Code generated successfully by Strands Agent",
    "code_id": "code-20250210123456",
    "pipeline_id": "pipe-123",
    "validation": {
      "valid": true,
      "errors": [],
      "warnings": []
    },
    "test_triggered": true,
    "agent_used": true
  }
}
```

## Deployment

```bash
# Deploy with CloudFormation
cd ../deploy
./deploy-code-pipeline.sh dev us-west-2 your-email@example.com

# Or manually package
pip install -r requirements.txt -t package/
cp agent_function.py package/
cd package && zip -r ../function.zip .
```

## AWS Permissions Required
- S3: Read access for templates, Write access for generated code
- DynamoDB: Read/Write access to GeneratedCode table
- Lambda: Invoke permission for Code Tester agent
- Bedrock: InvokeModel permission for Claude Sonnet 4.6
- CloudWatch: Logs write access

## Implementation Status
✅ **Implemented** - Strands Agent with MCP support, deployed via Bedrock AgentCore
