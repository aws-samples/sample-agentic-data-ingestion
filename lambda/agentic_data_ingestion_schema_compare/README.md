# Schema Compare Lambda Function

This Lambda function compares the newly extracted JSON schema against previously stored schemas for the same pipeline key to determine the appropriate pipeline action. It serves as the third step in the Agentic Data Ingestion self-healing data ingestion pipeline.

## Function Overview

**Purpose**: Compare current schema with previous schemas to determine pipeline reuse strategy.

**Trigger**: Step Functions or manual invocation after schema extraction

**Previous Step**: Schema extraction (generates schema from JSON files)

**Next Step**: Schema mapping or pipeline execution (based on comparison result)

## Input

### Step Functions Event Structure
```json
{
  "pipeline_key": "source-bucket-name/folder/subfolder"
}
```

### Required Prerequisites
- Schema extraction step must be completed successfully
- DynamoDB record with `step_name: "schema_extract"` must exist
- At least one schema must be available for the pipeline key

## Output

### First Schema Response (No Previous Schema)
```json
{
  "statusCode": 200,
  "pipeline_key": "source-bucket-name/folder/subfolder",
  "schema_hash": "-3776703701163197898",
  "action": "create_new_pipeline",
  "comparison_result": {
    "is_identical": false,
    "is_compatible": true,
    "differences": [],
    "summary": {"first_schema": true},
    "action": "create_new_pipeline"
  },
  "message": "First schema detected - new pipeline creation required"
}
```

### Schema Comparison Response
```json
{
  "statusCode": 200,
  "pipeline_key": "source-bucket-name/folder/subfolder",
  "schema_hash": "939841482601123018",
  "previous_schema_hash": "-3776703701163197898",
  "action": "update_existing_pipeline",
  "comparison_result": {
    "is_identical": false,
    "is_compatible": true,
    "differences": [
      {
        "path": "employee.team",
        "type": "field_added",
        "field": "team"
      }
    ],
    "summary": {
      "fields_added": 1,
      "fields_removed": 0,
      "type_mismatches": 0,
      "value_changes": 0
    },
    "action": "update_existing_pipeline"
  },
  "message": "Schema comparison completed - action: update_existing_pipeline"
}
```

### Error Response
```json
{
  "statusCode": 500,
  "error": "No current schema found for pipeline key: [pipeline_key]",
  "message": "Schema comparison failed"
}
```

## DynamoDB Storage

### Table: `agentic_data_ingestion_state`
### Record Structure:
```json
{
  "pipeline_id": "source-bucket-name/folder/subfolder",
  "step_name": "schema_compare",
  "status": "success|error",
  "timestamp": "2024-01-01T12:00:00.000Z",
  "current_schema_hash": "939841482601123018",
  "previous_schema_hash": "-3776703701163197898",
  "comparison_result": "{\"is_identical\":false,\"is_compatible\":true,\"differences\":[...]}",
  "schema_changed": true,
  "schema_compatible": true,
  "error_message": "Error details (only if status is error)"
}
```

### Key Fields Explained:
- **pipeline_id**: Same pipeline key from previous steps
- **step_name**: Always "schema_compare" for this function
- **current_schema_hash**: Hash of the newly extracted schema
- **previous_schema_hash**: Hash of the most recent previous schema (if exists)
- **comparison_result**: JSON string containing detailed comparison analysis
- **schema_changed**: Boolean indicating if schemas are different
- **schema_compatible**: Boolean indicating if changes are backward compatible

## Pipeline Actions

### 1. use_existing_pipeline
**When**: Schemas are identical (same hash)
**Meaning**: No changes detected, use existing processing pipeline
**Next Step**: Execute existing pipeline without modifications

### 2. update_existing_pipeline
**When**: Schemas are compatible (only new fields added)
**Meaning**: Schema evolved in backward-compatible way
**Next Step**: Update existing pipeline to handle new fields

### 3. create_new_pipeline
**When**: Schemas have breaking changes OR first schema for pipeline
**Meaning**: Incompatible changes require new processing logic
**Next Step**: Generate new pipeline with schema mapping

## Comparison Logic

### Compatible Changes (Non-Breaking)
- **New fields added**: Additional properties in objects
- **Optional field modifications**: Changes that don't affect existing data processing

### Breaking Changes (Incompatible)
- **Fields removed**: Properties that existed before are now missing
- **Type changes**: Data type modifications (string → number, object → array)
- **Required field changes**: Modifications to essential fields

### Difference Types

#### field_added
```json
{
  "path": "employee.department",
  "type": "field_added",
  "field": "department"
}
```

#### field_removed
```json
{
  "path": "employee.id",
  "type": "field_removed",
  "field": "id"
}
```

#### type_mismatch
```json
{
  "path": "employee.age",
  "type": "type_mismatch",
  "old_type": "string",
  "new_type": "number"
}
```

#### value_changed
```json
{
  "path": "metadata.version",
  "type": "value_changed",
  "old_value": "1.0",
  "new_value": "2.0"
}
```

## Processing Steps

1. **Get Current Schema**: Retrieve most recent schema extraction result
2. **Get Previous Schema**: Query for previous successful schema extraction
3. **Hash Comparison**: Quick check if schemas are identical
4. **Detailed Analysis**: Deep comparison of schema structures if different
5. **Compatibility Assessment**: Determine if changes are breaking or non-breaking
6. **Action Decision**: Choose appropriate pipeline action
7. **Store Results**: Save comparison results and recommended action

## Schema Comparison Algorithm

### Deep Comparison Process
1. **Type Validation**: Ensure both schemas have same root type
2. **Property Analysis**: Compare object properties recursively
3. **Array Item Comparison**: Analyze array item types and structures
4. **Path Tracking**: Maintain field paths for precise difference reporting
5. **Change Classification**: Categorize each difference by impact level

### Compatibility Rules
- **Adding fields**: Always compatible
- **Removing fields**: Always breaking
- **Type changes**: Always breaking
- **Nested changes**: Inherit parent compatibility rules

## Error Scenarios

### Common Errors:
- **No Current Schema**: Schema extraction step not completed
- **Schema Retrieval Failure**: DynamoDB access issues
- **Invalid Schema Format**: Corrupted schema data
- **Comparison Logic Errors**: Unexpected schema structures

## Environment Variables

- `DYNAMODB_TABLE`: DynamoDB table name (default: `agentic_data_ingestion_state`)

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["dynamodb:Query", "dynamodb:PutItem"],
      "Resource": [
        "arn:aws:dynamodb:region:account:table/agentic_data_ingestion_state",
        "arn:aws:dynamodb:region:account:table/agentic_data_ingestion_state/index/*"
      ]
    }
  ]
}
```

## Example Scenarios

### Scenario 1: First File in Pipeline
- **Input**: New pipeline key with first schema
- **Action**: `create_new_pipeline`
- **Reason**: No previous schema exists

### Scenario 2: Identical Schema
- **Input**: Same schema as previous file
- **Action**: `use_existing_pipeline`
- **Reason**: No changes detected

### Scenario 3: New Field Added
- **Input**: Schema with additional optional field
- **Action**: `update_existing_pipeline`
- **Reason**: Backward compatible change

### Scenario 4: Field Removed
- **Input**: Schema missing previously existing field
- **Action**: `create_new_pipeline`
- **Reason**: Breaking change requires new pipeline

### Scenario 5: Type Change
- **Input**: Field type changed from string to number
- **Action**: `create_new_pipeline`
- **Reason**: Type mismatch is breaking change

## Monitoring

- **CloudWatch Logs**: Detailed comparison analysis and decision logic
- **CloudWatch Metrics**: Comparison performance and action distribution
- **DynamoDB Records**: Historical comparison results for pipeline evolution tracking

## Integration with Pipeline

This function's output determines the next step in the Agentic Data Ingestion pipeline:
- **use_existing_pipeline**: Skip to pipeline execution
- **update_existing_pipeline**: Proceed to pipeline modification
- **create_new_pipeline**: Continue to schema mapping and new pipeline generation