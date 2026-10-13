# CreateInfrastructure Lambda - Target Type Detection Implementation

## Task 3.1: Add Target Type Detection Logic

### Overview
Implemented conditional infrastructure creation logic that determines whether to create new infrastructure based on the target type stored in the pipeline configuration.

### Changes Made

#### 1. New Function: `should_create_infrastructure()`
Added a new function that reads the `target_config` from the pipelines table and determines if infrastructure creation is needed:

```python
def should_create_infrastructure(target_config: Dict[str, Any]) -> tuple[bool, str]:
    """
    Determine if infrastructure creation is needed based on target configuration.
    
    Returns:
        tuple: (should_create: bool, reason: str)
    """
```

**Logic:**
- **CREATE** infrastructure for: `NEW_S3_TABLE_BUCKET` and `S3_TABLES` (both accepted, `lambda_function.py:784`)
- **SKIP** infrastructure for:
  - `EXISTING_S3_TABLE_BUCKET` (bucket already exists)
  - `GLUE_CATALOG` (table already in catalog)
  - `JDBC` (database connection already exists)
  - Empty or None target_config
  - Unknown target types

#### 2. Updated `lambda_handler()`
Modified the main handler to:
1. Read `target_config` from the pipelines DynamoDB table
2. Call `should_create_infrastructure()` to determine if creation is needed
3. Return early with skip status if infrastructure already exists
4. Store skip status in pipeline runs table for tracking
5. Pass through existing target configuration to next step

**Skip Response Format:**
```python
{
    'statusCode': 200,
    'run_id': run_id,
    'pipeline_id': pipeline_id,
    'infrastructure_status': {
        'overall_status': 'skipped',
        'reason': reason
    },
    'status': 'skipped',
    'skip_reason': reason,
    'target_config': target_config,
    'approval_required': False,
    'next_step': 'create_glue_pipeline'
}
```

### Requirements Satisfied

✅ **Requirement 3**: Read target_config from pipelines table
- Retrieves complete target configuration using `get_pipeline_metadata()`
- Accesses target_config with all required fields (type, database, table, etc.)

✅ **Requirement 4**: Determine if S3 Table Bucket creation is needed
- Checks target type to decide infrastructure creation
- Only creates for the `NEW_S3_TABLE_BUCKET` and `S3_TABLES` types
- Skips for existing resources (EXISTING_S3_TABLE_BUCKET, GLUE_CATALOG, JDBC)

✅ **Return early with skip status if infrastructure exists**
- Returns immediately with skip status when creation not needed
- Stores skip status in pipeline runs table
- Passes existing target_config to next step (CreateGluePipeline)

### Testing

Created comprehensive test suite in `tests/test_infrastructure_detection.py`:

**Test Cases:**
1. ✅ NEW_S3_TABLE_BUCKET and S3_TABLES - should create infrastructure
2. ✅ EXISTING_S3_TABLE_BUCKET - should skip
3. ✅ GLUE_CATALOG - should skip
4. ✅ JDBC - should skip
5. ✅ Empty target_config - should skip
6. ✅ None target_config - should skip
7. ✅ Unknown type - should skip with warning

**All tests passed successfully!**

### Integration with Step Functions

This implementation ensures proper workflow order:
1. **Step 7 (CreateInfrastructure)**: Conditionally creates S3 Table Bucket if needed
2. **Step 8 (CreateGluePipeline)**: Uses infrastructure info or existing target config

**Benefits:**
- Avoids duplicate resource creation
- Reduces costs by not creating unnecessary infrastructure
- Supports multiple target types (S3 Tables, Glue Catalog, JDBC)
- Provides clear skip reasons for debugging and monitoring

## Task 3.2: Integrate S3 Tables MCP for Bucket Creation (Refactored with Strands Agent)

### Overview
Implemented Strands Agent with S3 Tables MCP Server integration for intelligent Iceberg table creation. The agent reasons about optimal configurations and orchestrates MCP tools intelligently, with automatic fallback to manual creation when unavailable.

### Architecture Decision: Why Strands Agent?

**Initial Implementation (Direct MCP Calls):**
- ❌ No intelligence - mechanical tool calls
- ❌ No decision making - fixed sequence
- ❌ No reasoning - can't explain choices
- ❌ Brittle - can't adapt to variations

**Refactored Implementation (Strands Agent):**
- ✅ Intelligent orchestration of MCP tools
- ✅ Adaptive decision making based on schema analysis
- ✅ Explainable rationale for configurations
- ✅ Flexible handling of edge cases
- ✅ Aligns with design document and Requirement 13

### Changes Made

#### 1. System Prompt: `S3_TABLES_EXPERT_PROMPT`
Added expert system prompt for the agent:

```python
S3_TABLES_EXPERT_PROMPT = """You are an AWS S3 Tables and Apache Iceberg expert...
```

**Agent Expertise:**
- AWS S3 Tables architecture and best practices
- Apache Iceberg table format and optimization
- Data lake design patterns
- Performance tuning for analytical workloads

**Agent Responsibilities:**
- Create S3 Table Buckets with proper configuration
- Design optimal namespace structures
- Recommend Iceberg settings based on data characteristics
- Ensure proper metadata management
- Provide clear explanations for configuration decisions

#### 2. New Function: `initialize_s3_tables_agent()`
Initializes Strands Agent with S3 Tables MCP tools:

```python
def initialize_s3_tables_agent() -> Optional[Agent]:
    """Initialize Strands Agent with S3 Tables MCP tools."""
```

**Features:**
- Uses Claude Sonnet 4.6 (us.anthropic.claude-sonnet-4-6)
- Connects to S3 Tables MCP Server via AgentCore
- Loads all available MCP tools
- Configures agent with expert system prompt
- Max 10 iterations for complex tasks

#### 3. New Function: `create_s3_table_bucket_with_agent()`
Replaced direct MCP calls with agent orchestration:

**Agent Task:**
The agent receives a comprehensive task description including:
- Pipeline information (ID, account, region)
- Bucket configuration requirements
- Target schema with all fields
- Data characteristics (volume, record count, field count)
- Query patterns
- Expected output format

**Agent Workflow:**
```
Agent analyzes schema → Determines optimal config → 
Creates bucket → Creates namespace → 
Recommends Iceberg settings → Creates table → 
Retrieves metadata → Returns structured result with rationale
```

**Output:**
```python
{
    'bucket_name': 'agentic-data-ingestion-table-{pipeline_id}-{account_id}',
    'table_name': 'table_name',
    'table_bucket_arn': 'arn:aws:s3tables:...',
    'table_arn': 'arn:aws:s3tables:...',
    'namespace': ['agentic_data_ingestion', 'pipeline_id'],
    'metadata_location': 's3://...',
    'warehouse_location': 's3://...',
    'iceberg_config': {...},
    'rationale': 'Configured for 5GB dataset with 100K records...',
    'agent_used': True
}
```

#### 4. New Function: `create_s3_table_bucket_fallback()`
Maintains original manual creation logic as fallback:

**When Used:**
- AGENTCORE_S3TABLES_ENDPOINT not configured
- Agent initialization fails
- Agent execution fails or times out
- JSON parsing errors from agent output
- Any exception during agent creation

**Features:**
- Creates standard S3 bucket with versioning
- Generates Iceberg metadata manually
- Stores metadata files in S3
- Logs fallback usage for monitoring

#### 5. Helper Functions
Added estimation functions for agent context:

```python
def estimate_data_volume(target_schema: dict) -> float:
    """Estimate data volume in GB based on schema"""
    
def estimate_record_count(target_schema: dict) -> int:
    """Estimate record count based on schema complexity"""
```

These provide the agent with data characteristics to make informed decisions.

#### 6. Updated `create_s3_table_infrastructure()`
Enhanced to support agent integration:

**New Fields in Output:**
- `table_bucket_arn` - S3 Tables bucket ARN
- `table_arn` - Iceberg table ARN
- `namespace` - Table namespace
- `warehouse_location` - Iceberg warehouse location
- `iceberg_config` - Agent-recommended configuration
- `agent_used` - Boolean flag indicating agent usage
- `agent_rationale` - Agent's explanation of decisions
- `agent_integration` - Details about agent and model used

#### 7. Updated `lambda_handler()`
Enhanced output to pass agent information to CreateGluePipeline:

**New Output Fields:**
- `table_bucket_arn`
- `table_arn`
- `namespace`
- `warehouse_location`
- `iceberg_config`
- `agent_rationale`
- `agent_integration`

### Requirements Satisfied

✅ **Requirement 4**: Initialize MCP client connection to AgentCore
- Implemented `initialize_s3_tables_agent()` with Strands Agent
- Connects to AGENTCORE_S3TABLES_ENDPOINT via MCPClient
- Agent has access to all S3 Tables MCP tools

✅ **Requirement 11**: Use S3 Tables MCP tools to create Iceberg table
- Agent intelligently orchestrates 5+ MCP tools
- Creates proper S3 Table Bucket structure
- Generates namespace for organization
- Agent decides optimal tool sequence

✅ **Requirement 12**: Generate proper Iceberg metadata structure
- Agent analyzes schema and recommends optimal Iceberg config
- Creates Iceberg table with proper format
- Retrieves metadata and warehouse locations
- Provides rationale for configuration decisions

✅ **Requirement 13**: Strands Agent SHALL use S3 Tables MCP tools
- Uses Claude Sonnet 4.6 model
- Agent has S3 Tables expert system prompt
- Intelligently orchestrates MCP tools
- Provides explainable decisions

✅ **Store bucket info in output for CreateGluePipeline step**
- Returns all bucket details (ARN, name, locations)
- Includes agent-recommended Iceberg configuration
- Passes agent integration details and rationale

### Environment Variables

**New:**
- `AGENTCORE_S3TABLES_ENDPOINT` - AgentCore S3 Tables MCP endpoint URL

**Example:**
```
AGENTCORE_S3TABLES_ENDPOINT=https://bedrock-agentcore-runtime.us-east-1.amazonaws.com/runtime/abc123
```

### Dependencies

**New Requirements:**
- `strands-agents>=0.1.0` - Strands Agent framework with MCP support

**Updated:**
- `agentic_data_ingestion/lambda/agentic_data_ingestion_create_infrastructure/requirements.txt`

**Removed:**
- `requests` - No longer needed, Strands handles MCP communication

### Error Handling

**Robust Fallback Strategy:**
1. Check if AGENTCORE_S3TABLES_ENDPOINT is configured
2. Try to initialize Strands Agent with MCP tools
3. Execute agent task with comprehensive context
4. Parse and validate agent output
5. On any failure, automatically fall back to manual creation
6. Log all fallback events for monitoring
7. Continue pipeline execution regardless of agent availability

**Error Scenarios Handled:**
- MCP endpoint not configured
- Agent initialization failures
- Agent execution timeouts
- Invalid or unparseable agent output
- Missing required fields in agent response
- MCP tool failures within agent
- AWS API errors

### Logging

**Enhanced Logging:**
- Logs agent initialization status
- Logs agent task execution
- Logs agent output length and parsing
- Logs agent rationale for decisions
- Logs fallback usage with reasons
- Tracks agent vs manual creation
- Logs all bucket/table ARNs
- Logs Iceberg configuration recommendations

### Benefits of Agent Approach

**Intelligence:**
- Agent analyzes schema to understand data characteristics
- Makes informed decisions about Iceberg configuration
- Adapts to different data volumes and patterns

**Explainability:**
- Agent provides rationale for all configuration decisions
- Helps users understand why certain settings were chosen
- Improves debugging and optimization

**Flexibility:**
- Agent can handle edge cases not anticipated in code
- Can adapt tool sequence based on results
- Can retry with different approaches on failures

**Consistency:**
- Aligns with CreateGluePipeline Lambda (task 4) which also uses agent
- Follows design document architecture
- Satisfies Requirement 13 explicitly

### Testing Recommendations

**Integration Tests Needed:**
1. Test with agent endpoint configured and available
2. Test with agent endpoint unavailable (fallback)
3. Test with invalid agent endpoint
4. Test with agent execution failures
5. Test with various schema sizes and complexities
6. Verify agent rationale is meaningful
7. Verify output format for CreateGluePipeline

**Manual Testing:**
```bash
# Test with Strands Agent
aws lambda invoke \
  --function-name agentic_data_ingestion_create_infrastructure \
  --payload '{"run_id":"test-123","pipeline_id":"test-pipeline","target_schema":{"name":"test_table","fields":[...]}}' \
  response.json

# Check agent output
cat response.json | jq '.s3_table_bucket'
cat response.json | jq '.agent_rationale'
cat response.json | jq '.agent_integration'
```

### Comparison: Direct MCP vs Agent

| Aspect | Direct MCP Calls | Strands Agent |
|--------|------------------|---------------|
| Intelligence | ❌ None | ✅ Schema analysis |
| Decision Making | ❌ Fixed sequence | ✅ Adaptive |
| Explainability | ❌ No rationale | ✅ Provides reasoning |
| Flexibility | ❌ Brittle | ✅ Handles edge cases |
| Requirements | ❌ Violates Req 13 | ✅ Satisfies Req 13 |
| Consistency | ❌ Different from task 4 | ✅ Matches task 4 |

### Next Steps

Task 3.3 will implement:
- Additional fallback scenarios
- Enhanced error recovery
- Monitoring and metrics for agent usage
- Agent performance tracking

### Files Modified

1. `lambda/agentic_data_ingestion_create_infrastructure/lambda_function.py`
   - Added `should_create_infrastructure()` function
   - Updated `lambda_handler()` with conditional logic
   - Added skip status handling and DynamoDB updates

2. `tests/test_infrastructure_detection.py` (NEW)
   - Comprehensive test suite for target type detection
   - 7 test cases covering all scenarios
   - Validates both create and skip paths

### Logging

Added detailed logging for debugging:
- Logs target type detection decisions
- Logs skip reasons for monitoring
- Warns on unknown target types
- Tracks infrastructure creation vs skip in CloudWatch

### Error Handling

Robust error handling:
- Handles missing target_config gracefully
- Validates target type before processing
- Returns clear error messages
- Stores error state in DynamoDB for tracking
