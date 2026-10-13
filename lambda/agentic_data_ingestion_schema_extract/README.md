# Schema Extract Lambda Function

This Lambda function extracts JSON schema from processed files and stores the schema information in DynamoDB for pipeline processing. It serves as the second step in the Agentic Data Ingestion self-healing data ingestion pipeline.

## Function Overview

**Purpose**: Analyze JSON structure and generate comprehensive schema definitions for downstream processing.

**Trigger**: Step Functions or manual invocation after file validation

**Previous Step**: File validation (converts files to JSON)

**Next Step**: Schema comparison (determines pipeline action)

## Input

### Step Functions Event Structure
```json
{
  "pipeline_key": "source-bucket-name/folder/subfolder"
}
```

### Required Prerequisites
- File validation step must be completed successfully
- JSON file must exist in target S3 bucket
- DynamoDB record with `step_name: "file_validation"` must exist

## Output

### Success Response
```json
{
  "statusCode": 200,
  "pipeline_key": "source-bucket-name/folder/subfolder",
  "schema_hash": "-3776703701163197898",
  "schema": {
    "type": "array",
    "items": {
      "type": "object",
      "properties": {
        "emp_id": {"type": "string"},
        "full_name": {"type": "string"},
        "dept": {"type": "string"},
        "hours": {"type": "string"}
      }
    }
  },
  "json_s3_uri": "s3://target-bucket/folder/subfolder/filename.json",
  "message": "Schema extraction completed successfully"
}
```

### Error Response
```json
{
  "statusCode": 500,
  "error": "No JSON file found for pipeline key: [pipeline_key]",
  "message": "Schema extraction failed"
}
```

## DynamoDB Storage

### Table: `agentic_data_ingestion_state`
### Record Structure:
```json
{
  "pipeline_id": "source-bucket-name/folder/subfolder",
  "step_name": "schema_extract",
  "status": "success|error",
  "timestamp": "2024-01-01T12:00:00.000Z",
  "extracted_schema": "{\"type\":\"array\",\"items\":{\"type\":\"object\",\"properties\":{...}}}",
  "json_s3_uri": "s3://target-bucket/folder/subfolder/filename.json",
  "schema_hash": "-3776703701163197898",
  "error_message": "Error details (only if status is error)"
}
```

### Key Fields Explained:
- **pipeline_id**: Same pipeline key from file validation step
- **step_name**: Always "schema_extract" for this function
- **extracted_schema**: JSON string containing the complete schema definition
- **schema_hash**: Unique hash for schema comparison (used by next step)
- **json_s3_uri**: Location of the JSON file that was analyzed

## Schema Extraction Logic

### Supported Data Structures

#### 1. Array of Objects (Most Common)
**Input JSON:**
```json
[
  {"name": "John", "age": 30, "active": true},
  {"name": "Jane", "age": 25, "active": false}
]
```

**Generated Schema:**
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "name": {"type": "string"},
      "age": {"type": "integer"},
      "active": {"type": "boolean"}
    }
  }
}
```

#### 2. Single Object
**Input JSON:**
```json
{
  "employee": {"id": "E001", "name": "John"},
  "department": "Engineering"
}
```

**Generated Schema:**
```json
{
  "type": "object",
  "properties": {
    "employee": {
      "type": "object",
      "properties": {
        "id": {"type": "string"},
        "name": {"type": "string"}
      }
    },
    "department": {"type": "string"}
  }
}
```

#### 3. Nested Arrays and Objects
**Input JSON:**
```json
[
  {
    "employee": {"id": "E001", "name": "John"},
    "shifts": [
      {"date": "2024-01-01", "hours": 8.5}
    ]
  }
]
```

**Generated Schema:**
```json
{
  "type": "array",
  "items": {
    "type": "object",
    "properties": {
      "employee": {
        "type": "object",
        "properties": {
          "id": {"type": "string"},
          "name": {"type": "string"}
        }
      },
      "shifts": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "date": {"type": "string"},
            "hours": {"type": "number"}
          }
        }
      }
    }
  }
}
```

## Type Detection Rules

### Primitive Types
- **string**: Any text value
- **integer**: Whole numbers (1, 2, 100)
- **number**: Decimal numbers (1.5, 3.14)
- **boolean**: true/false values
- **null**: null values

### Complex Types
- **object**: JSON objects with properties
- **array**: JSON arrays (analyzes first item for item type)

### Type Priority
When analyzing arrays, the function examines the first item to determine the array item type. For mixed-type arrays, it defaults to the type of the first element.

## Processing Steps

1. **Retrieve Previous State**: Query DynamoDB for file validation record
2. **Extract JSON Location**: Get S3 URI of processed JSON file
3. **Read JSON Data**: Download and parse JSON content from S3
4. **Analyze Structure**: Recursively examine JSON structure
5. **Generate Schema**: Create comprehensive schema definition
6. **Calculate Hash**: Generate unique hash for schema comparison
7. **Store Results**: Save schema and metadata to DynamoDB

## Error Scenarios

### Common Errors:
- **No Previous State**: File validation step not completed
- **Missing JSON File**: JSON file not found in S3
- **Invalid JSON**: Malformed JSON content
- **S3 Access Errors**: Permissions or connectivity issues
- **DynamoDB Errors**: Table access or write failures

## Environment Variables

- `DYNAMODB_TABLE`: DynamoDB table name (default: `agentic_data_ingestion_state`)

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::target-bucket/*"
    },
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

## Schema Hash Generation

The schema hash is generated using Python's built-in `hash()` function on the JSON string representation of the schema (with sorted keys). This ensures:
- **Consistency**: Same schema always produces same hash
- **Uniqueness**: Different schemas produce different hashes
- **Comparison**: Enables fast schema comparison in next step

## Monitoring

- **CloudWatch Logs**: Detailed schema extraction process
- **CloudWatch Metrics**: Function performance and error rates
- **DynamoDB Records**: Schema storage and retrieval status

## Integration with Pipeline

This function is designed to be called by Step Functions or manually after file validation. The extracted schema is used by the schema comparison function to determine whether to:
- Use existing pipeline (identical schema)
- Update existing pipeline (compatible schema)
- Create new pipeline (incompatible schema)