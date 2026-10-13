# Agentic Data Ingestion Schema Mapping Function

## Description
Lambda function that generates intelligent field mappings from source schema to target schema using Amazon Bedrock LLM. This function analyzes the source data schema and creates optimal mappings to a predefined target schema, handling field transformations, default values, and unmapped fields.

## Functionality

### Core Features
- **Intelligent Mapping**: Uses Amazon Bedrock (Claude) to generate semantic field mappings
- **Fallback Logic**: Simple name-based matching when Bedrock is unavailable
- **Transformation Detection**: Identifies required data type conversions and transformations
- **Unmapped Field Handling**: Manages fields that don't have direct mappings
- **Default Value Assignment**: Provides sensible defaults for unmapped target fields

### Input
Receives event from schema comparison step:
```json
{
    "pipeline_key": "bucket/folder",
    "source_schema_hash": "hash123",
    "trigger": "schema_compare"
}
```

### Processing Steps
1. **Retrieve Source Schema**: Gets the latest extracted schema from DynamoDB
2. **Load Target Schema**: Fetches target schema from S3 or uses default employee schema
3. **Generate Mapping**: Uses Bedrock LLM for intelligent field mapping analysis
4. **Create Transformations**: Identifies required data type conversions
5. **Handle Unmapped Fields**: Assigns default values and documents unmapped fields
6. **Store Results**: Saves mapping configuration to DynamoDB

### Output
Returns mapping configuration and triggers next pipeline step:
```json
{
    "statusCode": 200,
    "pipeline_key": "bucket/folder",
    "mapping_generated": true,
    "mapping_count": 8,
    "unmapped_source_fields": 2,
    "unmapped_target_fields": 1,
    "mapping_summary": "Generated 8 field mappings with high confidence",
    "next_step": "create_code"
}
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DYNAMODB_TABLE` | DynamoDB state table name | `agentic_data_ingestion_state` |
| `TARGET_SCHEMA_BUCKET` | S3 bucket for target schemas | `agentic-data-ingestion-target-schemas` |
| `BEDROCK_MODEL_ID` | Bedrock model for mapping generation | `anthropic.claude-3-haiku-20240307-v1:0` |

## Permissions Required

### AWS Services
- **DynamoDB**: Read/Write access to state table
- **S3**: Read access to target schema bucket
- **Bedrock**: Invoke model permissions for Claude

### IAM Policy Example
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_state"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject"
            ],
            "Resource": "arn:aws:s3:::adi-target-schemas/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel"
            ],
            "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
        }
    ]
}
```

## Mapping Strategy

### Bedrock LLM Analysis
The function uses Claude to perform sophisticated field mapping by:
- **Semantic Analysis**: Understanding field meanings beyond just names
- **Pattern Recognition**: Identifying common data patterns and relationships
- **Transformation Logic**: Determining required data type conversions
- **Confidence Scoring**: Providing confidence levels for each mapping

### Fallback Mapping
When Bedrock is unavailable, uses rule-based matching:
- **Exact Name Match**: Direct field name correspondence
- **Substring Match**: Partial name matching with high confidence
- **Pattern Matching**: Common field patterns (id, name, email, date, etc.)
- **Type-Based Defaults**: Appropriate default values by data type

## Target Schema

### Default Employee Schema
```json
{
    "type": "object",
    "properties": {
        "employee_id": {"type": "string", "description": "Unique employee identifier"},
        "first_name": {"type": "string", "description": "Employee first name"},
        "last_name": {"type": "string", "description": "Employee last name"},
        "email": {"type": "string", "description": "Employee email address"},
        "department": {"type": "string", "description": "Department or division"},
        "position": {"type": "string", "description": "Job title or position"},
        "hire_date": {"type": "string", "format": "date", "description": "Date of hire"},
        "salary": {"type": "number", "description": "Annual salary"},
        "manager_id": {"type": "string", "description": "Manager's employee ID"},
        "location": {"type": "string", "description": "Work location or office"},
        "status": {"type": "string", "description": "Employment status"}
    },
    "required": ["employee_id", "first_name", "last_name"]
}
```

### Custom Target Schemas
Place custom target schemas in S3:
- **Bucket**: `agentic-data-ingestion-target-schemas`
- **Key**: `target_schema.json`
- **Format**: JSON Schema specification

## Stored Data Structure

### DynamoDB Record
```json
{
    "pipeline_id": "bucket/folder",
    "step_name": "schema_mapping",
    "status": "success",
    "timestamp": "2024-01-20T15:30:00.000Z",
    "pipeline_key": "bucket/folder",
    "mapping_result": "{\"mappings\": [...], \"unmapped_source_fields\": [...]}",
    "source_schema_hash": "hash123",
    "target_schema_hash": "hash456",
    "mapping_count": 8,
    "unmapped_source_count": 2,
    "unmapped_target_count": 1
}
```

### Mapping Result Format
```json
{
    "mappings": [
        {
            "target_field": "employee_id",
            "source_field": "emp_id",
            "transformation": "string conversion",
            "confidence": 0.95,
            "notes": "Direct mapping with type conversion"
        }
    ],
    "unmapped_source_fields": ["legacy_field", "temp_data"],
    "unmapped_target_fields": [
        {
            "field": "status",
            "default_value": "active",
            "reason": "Not present in source, using default"
        }
    ],
    "mapping_summary": "Generated 8 high-confidence field mappings"
}
```

## Error Handling

### Common Errors
- **Missing Source Schema**: No schema found in previous pipeline step
- **Bedrock Access Issues**: Model invocation failures or permission errors
- **Invalid Schema Format**: Malformed JSON schema structures
- **DynamoDB Errors**: Table access or write permission issues

### Error Recovery
- **Graceful Degradation**: Falls back to simple mapping when Bedrock fails
- **State Persistence**: Stores error states in DynamoDB for debugging
- **Detailed Logging**: Comprehensive error messages for troubleshooting

## Testing

### Unit Tests
```bash
# Run local tests
python -m pytest tests/

# Test with sample schemas
python test_mapping.py
```

### Integration Tests
```bash
# Test with real Bedrock
python integration_test.py --use-bedrock

# Test fallback logic
python integration_test.py --no-bedrock
```

## Monitoring

### CloudWatch Metrics
- **Invocation Count**: Function execution frequency
- **Error Rate**: Mapping generation failures
- **Duration**: Processing time per mapping
- **Bedrock Usage**: Model invocation costs

### Custom Metrics
- **Mapping Quality**: Confidence scores and field coverage
- **Fallback Usage**: Frequency of simple vs. LLM mapping
- **Schema Complexity**: Source/target field counts

## Next Steps

After successful mapping generation:
1. **Code Generation**: Create ETL transformation code
2. **Pipeline Creation**: Generate Glue job definitions
3. **Quality Checks**: Add data validation rules
4. **Execution**: Run the data ingestion pipeline

## Dependencies

### Python Packages
```txt
boto3>=1.28.0
json
logging
datetime
typing
```

### AWS Services
- Amazon Bedrock (Claude models)
- DynamoDB (state management)
- S3 (schema storage)
- CloudWatch (monitoring)