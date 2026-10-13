# Agentic Data Ingestion Approval Configuration Lambda Function

This Lambda function provides API endpoints for managing pipeline-level approval configurations in the Agentic Data Ingestion system.

## Overview

The approval configuration system allows administrators to control which approval gates are enabled for each pipeline:
- **Schema Mapping Approval**: Require approval after schema mapping generation
- **Code Generation Approval**: Require approval after code generation and testing
- **First Run Approval**: Require approval before first pipeline execution after infrastructure deployment

## API Endpoints

### GET /approval-config
List all pipelines with their approval configurations.

**Response:**
```json
{
  "pipelines": [
    {
      "pipeline_id": "customer-data-pipeline",
      "pipeline_name": "Customer Data ETL",
      "status": "active",
      "approval_config": {
        "schema_mapping_enabled": true,
        "code_generation_enabled": false,
        "first_run_enabled": true
      },
      "approval_summary": {
        "schema_mapping": true,
        "code_generation": false,
        "first_run": true,
        "total_enabled": 2
      }
    }
  ],
  "total_count": 1
}
```

### GET /approval-config/{pipeline_id}
Get approval configuration for a specific pipeline.

**Response:**
```json
{
  "pipeline_id": "customer-data-pipeline",
  "approval_config": {
    "schema_mapping_enabled": true,
    "code_generation_enabled": true,
    "first_run_enabled": true,
    "auto_approval_thresholds": {
      "schema_mapping_confidence": 0.9,
      "code_quality_score": 80,
      "max_estimated_cost": 50
    },
    "notification_settings": {
      "email_enabled": true,
      "slack_enabled": false,
      "timeout_hours": 24
    }
  },
  "last_updated": "2025-10-09T10:00:00Z",
  "updated_by": "admin@example.com"
}
```

### PUT /approval-config/{pipeline_id}
Update approval configuration for a specific pipeline.

**Request Body:**
```json
{
  "approval_config": {
    "schema_mapping_enabled": false,
    "code_generation_enabled": true,
    "first_run_enabled": true,
    "auto_approval_thresholds": {
      "schema_mapping_confidence": 0.95,
      "code_quality_score": 85,
      "max_estimated_cost": 100
    }
  },
  "updated_by": "admin@example.com"
}
```

**Response:**
```json
{
  "pipeline_id": "customer-data-pipeline",
  "approval_config": {
    "schema_mapping_enabled": false,
    "code_generation_enabled": true,
    "first_run_enabled": true
  },
  "last_updated": "2025-10-09T10:15:00Z",
  "updated_by": "admin@example.com",
  "status": "success"
}
```

### GET /approval-config/template
Get default approval configuration template.

**Response:**
```json
{
  "default_approval_config": {
    "schema_mapping_enabled": true,
    "code_generation_enabled": true,
    "first_run_enabled": true,
    "auto_approval_thresholds": {
      "schema_mapping_confidence": 0.9,
      "code_quality_score": 80,
      "max_estimated_cost": 50
    },
    "notification_settings": {
      "email_enabled": true,
      "slack_enabled": false,
      "timeout_hours": 24,
      "reminder_intervals": [4, 12, 24]
    }
  }
}
```

## Configuration Options

### Approval Gates
- `schema_mapping_enabled` (boolean): Enable/disable schema mapping approval
- `code_generation_enabled` (boolean): Enable/disable code generation approval  
- `first_run_enabled` (boolean): Enable/disable first run approval

### Auto-Approval Thresholds
- `schema_mapping_confidence` (float): Minimum confidence score to auto-approve mappings
- `code_quality_score` (integer): Minimum code quality score to auto-approve code
- `max_estimated_cost` (float): Maximum estimated monthly cost to auto-approve infrastructure

### Notification Settings
- `email_enabled` (boolean): Enable email notifications for approvals
- `slack_enabled` (boolean): Enable Slack notifications for approvals
- `timeout_hours` (integer): Hours before approval times out
- `reminder_intervals` (array): Hours at which to send reminder notifications

## Environment Variables

- `PIPELINES_TABLE`: DynamoDB table name for pipeline metadata (default: 'agentic_data_ingestion_pipelines')

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:Scan"
      ],
      "Resource": "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_pipelines"
    }
  ]
}
```

## Usage Examples

### Disable All Approvals for Trusted Pipeline
```bash
curl -X PUT https://api.example.com/approval-config/trusted-pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "approval_config": {
      "schema_mapping_enabled": false,
      "code_generation_enabled": false,
      "first_run_enabled": false
    },
    "updated_by": "admin@example.com"
  }'
```

### Enable Only Critical Approvals
```bash
curl -X PUT https://api.example.com/approval-config/critical-pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "approval_config": {
      "schema_mapping_enabled": false,
      "code_generation_enabled": true,
      "first_run_enabled": true,
      "auto_approval_thresholds": {
        "code_quality_score": 90,
        "max_estimated_cost": 25
      }
    },
    "updated_by": "admin@example.com"
  }'
```

## Integration with Pipeline Execution

The approval configuration is automatically checked by each Lambda function during pipeline execution:

1. **Schema Mapping Function**: Checks `schema_mapping_enabled` and auto-approval thresholds
2. **Code Generation Function**: Checks `code_generation_enabled` and quality thresholds  
3. **Infrastructure Function**: Checks `first_run_enabled` and cost thresholds
4. **Quality Checks Function**: Combines infrastructure and quality data for first run approval

## Error Handling

The function returns appropriate HTTP status codes:
- `200`: Success
- `400`: Bad request (invalid configuration)
- `404`: Pipeline not found
- `405`: Method not allowed
- `500`: Internal server error

All errors include descriptive error messages in the response body.