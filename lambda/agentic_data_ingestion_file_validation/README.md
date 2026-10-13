# File Validation Lambda Function

This Lambda function processes S3 file uploads, converts supported file types to JSON, and stores metadata in DynamoDB. It serves as the first step in the Agentic Data Ingestion self-healing data ingestion pipeline.

## Function Overview

**Purpose**: Validate and convert uploaded files to JSON format while establishing pipeline keys for downstream processing.

**Trigger**: S3 ObjectCreated events

**Next Step**: Triggers schema extraction for the converted JSON files

## Input

### S3 Event Structure
```json
{
  "Records": [
    {
      "eventSource": "aws:s3",
      "s3": {
        "bucket": {
          "name": "source-bucket-name"
        },
        "object": {
          "key": "folder/subfolder/filename.csv"
        }
      }
    }
  ]
}
```

### Supported File Types
- **CSV**: Comma-separated values files
- **TSV**: Tab-separated values files  
- **JSON**: JSON files (validation and reformatting)

## Output

### Success Response
```json
{
  "statusCode": 200,
  "body": "{\"message\": \"File processing completed successfully\", \"status\": \"success\"}"
}
```

### Error Response
```json
{
  "statusCode": 500,
  "body": "{\"message\": \"File processing failed: [error details]\", \"status\": \"error\"}"
}
```

### Generated Artifacts
1. **JSON File**: Created in target S3 bucket with same path structure
2. **Pipeline Key**: Generated from S3 folder structure (`bucket/folder/subfolder`)
3. **DynamoDB Record**: Metadata stored for pipeline coordination

## DynamoDB Storage

### Table: `agentic_data_ingestion_state`
### Record Structure:
```json
{
  "pipeline_id": "source-bucket-name/folder/subfolder",
  "step_name": "file_validation",
  "status": "success|error|unsupported",
  "timestamp": "2024-01-01T12:00:00.000Z",
  "original_s3_uri": "s3://source-bucket/folder/subfolder/filename.csv",
  "json_s3_uri": "s3://target-bucket/folder/subfolder/filename.json",
  "file_type": "csv|tsv|json",
  "pipeline_key": "source-bucket-name/folder/subfolder",
  "error_message": "Error details (only if status is error)"
}
```

### Key Fields Explained:
- **pipeline_id**: Unique identifier for the pipeline (based on S3 folder structure)
- **step_name**: Always "file_validation" for this function
- **pipeline_key**: Used by downstream functions to coordinate processing
- **original_s3_uri**: Full S3 URI of the source file
- **json_s3_uri**: Full S3 URI of the converted JSON file

## Pipeline Key Generation

The pipeline key is generated from the S3 folder structure, excluding the filename:
- `s3://bucket/folder/subfolder/file.csv` → `bucket/folder/subfolder`
- `s3://bucket/file.csv` → `bucket`

Files in the same folder share the same pipeline key, enabling schema comparison and pipeline reuse.

## Environment Variables

- `DYNAMODB_TABLE`: DynamoDB table name (default: `agentic_data_ingestion_state`)
- `TARGET_BUCKET`: S3 bucket for processed JSON files

## Processing Logic

1. **Parse S3 Event**: Extract bucket name and object key
2. **Generate Pipeline Key**: Create unique identifier from folder structure
3. **Read Source File**: Download file content from S3
4. **Convert to JSON**: 
   - CSV/TSV: Parse and convert to JSON array of objects
   - JSON: Validate and reformat
5. **Upload JSON**: Store converted file in target bucket
6. **Store Metadata**: Record processing results in DynamoDB

## Error Scenarios

### Supported Errors:
- **File Read Errors**: Source file not accessible or corrupted
- **Conversion Errors**: Invalid CSV/TSV structure or malformed JSON
- **Upload Errors**: Target bucket not accessible or permissions issues
- **DynamoDB Errors**: Table not accessible or write failures

### Unsupported File Types:
Files with unsupported extensions are logged but don't cause pipeline failures. A record is still created in DynamoDB with status "unsupported".

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::source-bucket/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::target-bucket/*"
    },
    {
      "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "arn:aws:dynamodb:region:account:table/agentic_data_ingestion_state"
    }
  ]
}
```

## Monitoring

- **CloudWatch Logs**: Detailed execution logs with processing steps
- **CloudWatch Metrics**: Function duration, error rates, invocation counts
- **DynamoDB Records**: Processing status and metadata for each file

## Example Conversion

### Input CSV:
```csv
emp_id,full_name,dept,hours
E001,John Smith,Engineering,8.5
E002,Sarah Johnson,Marketing,8.0
```

### Output JSON:
```json
[
  {
    "emp_id": "E001",
    "full_name": "John Smith",
    "dept": "Engineering",
    "hours": "8.5"
  },
  {
    "emp_id": "E002",
    "full_name": "Sarah Johnson",
    "dept": "Marketing",
    "hours": "8.0"
  }
]
```