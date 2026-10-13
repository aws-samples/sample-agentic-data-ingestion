# Glue Pipeline Creation Lambda Function

## Description
This Lambda function creates AWS Glue ETL jobs with intelligent resource allocation using Strands Agents and dual MCP servers (Data Processing + S3 Tables). It automatically configures Glue jobs for different target types including S3 Table Buckets (Iceberg), Glue Catalog tables, and JDBC databases.

## Purpose
- Initialize Strands Agent with Claude Sonnet 4.6 for intelligent Glue job configuration
- Connect to AgentCore MCP endpoint to access Data Processing and S3 Tables MCP tools
- Analyze data characteristics and recommend optimal worker types and counts
- Create Glue jobs with target-specific parameters (S3 Tables, Glue Catalog, JDBC)
- Generate pipeline-specific IAM roles with least-privilege permissions
- Configure monitoring and observability for Glue jobs

## Requirements Addressed
- Requirement 2: Intelligent Resource Allocation with Strands Agent and MCP
- Requirement 10: Python and Glue Version Management (Lambda 3.12, Glue 5.0)
- Requirement 13: Multi-MCP Agent Configuration with Claude Sonnet 4.6

## Strands Agent Integration

### Model Configuration
- **Model**: Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`)
- **Framework**: Strands Agents
- **MCP Integration**: AgentCore Runtime with dual MCP servers

### MCP Tools Available
1. **Data Processing MCP**:
   - `analyze_data_characteristics`: Analyze data volume and complexity
   - `calculate_optimal_workers`: Determine worker type and count
   - `estimate_glue_cost`: Calculate expected AWS charges
   - `validate_configuration`: Check against AWS limits

2. **S3 Tables MCP**:
   - `create_s3_table`: Create S3 Table Bucket with Iceberg metadata
   - `get_table_metadata`: Retrieve Iceberg metadata location
   - `optimize_iceberg_config`: Recommend Iceberg-specific settings
   - `validate_schema`: Validate schema compatibility

### Agent Initialization
```python
agent, mcp_tools = initialize_strands_agent_with_mcp()
# Returns: (Agent instance, list of tools) or (None, []) if unavailable
```

The agent uses AWS SigV4 authentication to connect to AgentCore and retrieves tools from both MCP servers in a single unified tool list.

## Input
```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "target_type": "S3 Table Bucket | Glue Catalog | JDBC",
  "target_schema": {
    "fields": [...]
  },
  "s3_table_bucket": {
    "bucket_name": "string",
    "table_name": "string",
    "data_location": "string",
    "iceberg_metadata_location": "string"
  },
  "table_location": "string",
  "data_volume": "string",
  "performance_requirements": {}
}
```

## Output
```json
{
  "statusCode": 200,
  "run_id": "string",
  "pipeline_id": "string",
  "database_name": "string",
  "glue_job": {
    "job_name": "string",
    "job_arn": "string",
    "role_arn": "string",
    "script_location": "string",
    "status": "created",
    "mcp_recommendations": "string",
    "mcp_enhanced": true
  },
  "target_type": "string",
  "script_location": "string",
  "next_step": "run_quality_checks"
}
```

## Environment Variables

### Required
- `PIPELINE_RUNS_TABLE`: DynamoDB table for pipeline execution state
- `PIPELINES_TABLE`: DynamoDB table for pipeline metadata
- `GENERATED_CODE_TABLE`: DynamoDB table for generated code references
- `AGENTCORE_ENDPOINT`: AgentCore MCP endpoint URL (e.g., `https://bedrock-agentcore-runtime.us-east-1.amazonaws.com/runtime/{id}`)
- `MCP_ENABLED`: Enable/disable MCP integration (`true` or `false`)

### Optional
- `AWS_ACCOUNT_ID`: AWS account ID (auto-detected if not provided)
- `AWS_REGION`: AWS region (defaults to `us-east-1`)
- `S3_TABLES_JAR_PATH`: S3 path to S3 Tables JAR for Iceberg support
- `OTEL_SDK_DISABLED`: Disable OpenTelemetry SDK (`true` to disable)

## Lambda Configuration

### Runtime Settings
- **Runtime**: Python 3.12
- **Architecture**: arm64
- **Timeout**: 900 seconds (15 minutes)
- **Memory**: 1024 MB
- **Handler**: `lambda_function.lambda_handler`

### Layers Required
- Strands Agents Lambda Layer (includes `strands-agents` and `mcp` packages)

### IAM Permissions Required
- **Glue**: `CreateJob`, `UpdateJob`, `GetJob`, `TagResource`
- **IAM**: `CreateRole`, `GetRole`, `AttachRolePolicy`, `PutRolePolicy`
- **Bedrock**: `InvokeModel` (for Claude Sonnet 4.6)
- **DynamoDB**: `GetItem`, `PutItem`, `UpdateItem`
- **S3**: Read access to generated code and JAR files
- **CloudWatch**: Logs write access

## Fallback Behavior

If MCP integration is unavailable:
1. Logs warning about MCP unavailability
2. Falls back to standard Glue job creation
3. Uses default worker configuration based on data volume
4. Sets `mcp_enhanced: false` in response
5. Includes `fallback_reason` in response

## Worker Type Recommendations

The agent uses these guidelines when recommending worker types:
- **G.025X**: Small jobs (<1 GB), 2-3 workers, ~$0.44/DPU-hour
- **G.1X**: Small to medium jobs (1-10 GB), 2-5 workers, ~$0.44/DPU-hour
- **G.2X**: Medium jobs (10-100 GB), 5-10 workers, ~$0.88/DPU-hour
- **G.4X**: Large jobs (100-500 GB), 10-25 workers, ~$1.76/DPU-hour
- **G.8X**: Very large jobs (>500 GB), 25-50 workers, ~$3.52/DPU-hour
- **Z.2X**: Memory-intensive jobs, 10-50 workers, ~$1.00/DPU-hour

## Error Handling

### Configuration Errors
- Missing `run_id` or `pipeline_id`: Returns 400 error
- Missing `s3_table_bucket` or `table_location`: Returns 400 error
- Invalid target configuration: Returns 400 error with details

### MCP Errors
- AgentCore endpoint unavailable: Falls back to standard deployment
- MCP tools fail: Falls back to standard deployment
- Agent initialization fails: Falls back to standard deployment

### AWS Service Errors
- Glue job creation fails: Returns 500 error with details
- IAM role creation fails: Returns 500 error with details
- DynamoDB write fails: Logs error but continues

## Worker Recommendation Implementation

### Agent-Powered Recommendation (Task 4.2)

The Lambda now includes intelligent worker recommendation using the Strands Agent with MCP tools:

#### Key Functions

1. **`get_data_characteristics()`**
   - Extracts data volume, record count, and complexity from pipeline metadata
   - Queries previous pipeline steps for actual data characteristics
   - Estimates transformation complexity based on schema field count
   - Returns safe defaults if data unavailable

2. **`get_worker_recommendation_with_agent()`**
   - Uses Strands Agent with MCP tools to analyze data characteristics
   - Sends detailed prompt with data volume, complexity, target type
   - Agent uses MCP tools to calculate optimal configuration
   - Parses JSON response with worker type, count, timeout, cost estimates
   - Logs agent rationale and MCP tools used
   - Returns `WorkerRecommendation` dataclass

3. **`get_default_worker_recommendation()`**
   - Fallback rule-based recommendation when MCP unavailable
   - Uses data volume and complexity multipliers
   - Calculates worker count based on data-to-worker ratios
   - Estimates duration and cost using DPU-hour calculations
   - Returns `WorkerRecommendation` with lower confidence score

#### Recommendation Flow

```
1. Extract data characteristics (volume, complexity, record count)
   ↓
2. Try agent-powered recommendation with MCP tools
   ↓
3. If agent fails → Use fallback rule-based recommendation
   ↓
4. Apply recommendation to Glue job configuration
   ↓
5. Log rationale, MCP tools used, and estimated cost
   ↓
6. Store recommendation in DynamoDB for tracking
```

#### Agent Prompt Structure

The agent receives:
- Data volume (GB)
- Record count
- Transformation complexity (simple/medium/complex)
- Field count
- Target type (S3 Tables, Glue Catalog, JDBC)
- Target configuration details

The agent returns JSON with:
- `worker_type`: G.025X, G.1X, G.2X, G.4X, G.8X, or Z.2X
- `number_of_workers`: 2-50 workers
- `timeout_minutes`: 15-2880 minutes
- `estimated_cost_per_run`: Dollar amount
- `estimated_duration_minutes`: Expected runtime
- `rationale`: Detailed explanation of recommendation
- `confidence`: 0.0-1.0 confidence score

#### Logging

All recommendations are logged with:
- Worker type and count
- Timeout configuration
- Estimated cost and duration
- Agent rationale
- MCP tools used (if any)
- Confidence score

Example log output:
```
Agent recommendation: G.2X, 8 workers
Estimated cost: $2.11, duration: 45min
Rationale: Medium complexity transformation with 50GB data requires G.2X workers...
MCP tools used: analyze_data_characteristics, calculate_optimal_workers, estimate_glue_cost
```

## Implementation Status
✅ **Task 4.1 Complete** - Strands Agent with Claude Sonnet 4.6 initialized, connected to AgentCore MCP endpoint with dual MCP servers (Data Processing + S3 Tables)

✅ **Task 4.2 Complete** - Agent-powered worker recommendation implemented with:
- Data characteristics extraction from pipeline metadata
- Strands Agent integration for intelligent configuration
- MCP tools usage for analysis and cost estimation
- Fallback rule-based recommendation when MCP unavailable
- Comprehensive logging of rationale and MCP tools used
- Worker recommendation stored in DynamoDB for tracking