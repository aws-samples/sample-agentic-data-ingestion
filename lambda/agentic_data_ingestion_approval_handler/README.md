# Agentic Data Ingestion Approval Handler Lambda Function

This Lambda function handles Human-in-the-Loop (HITL) approval workflows for Agentic Data Ingestion pipelines. It supports creating approval requests from Step Functions and processing approval decisions from the dashboard API.

## Functionality

### Approval Request Creation
- Validates and sanitizes approval request data
- Retrieves pipeline configuration and approval settings
- Collects relevant data based on approval type (schema mapping, code generation, first run)
- Stores approval request in DynamoDB with TTL for automatic cleanup
- Sends notifications via SNS and EventBridge

### Approval Decision Processing
- Processes approve/reject decisions with audit logging
- Updates approval status in DynamoDB
- Continues or fails Step Functions workflow using task tokens
- Sends decision notifications via EventBridge

## Supported Approval Types

1. **schema_mapping**: Approves Bedrock LLM-generated schema mappings
2. **code_generation**: Approves Strands Agent-generated PySpark code and test results
3. **first_run**: Approves first pipeline execution after infrastructure deployment

## Environment Variables

- `APPROVALS_TABLE`: DynamoDB table for approval requests. CDK sets it to the Approvals table
  (`compute-stack.ts:1143`), so the code default at `lambda_function.py:102`,
  `agentic_data_ingestion_approvals`, applies only in local mode. Outside local mode
  `require_env` raises rather than falling back, so a missing variable fails loudly instead of
  reading the wrong table.
- `STATE_TABLE`: DynamoDB table for pipeline state (default: agentic_data_ingestion_state)
- `SNS_TOPIC_ARN`: SNS topic for approval notifications (optional)
- `DEFAULT_TIMEOUT_HOURS`: Default approval timeout in hours (default: 72, from `shared/config.py`'s `APPROVAL_WINDOW_HOURS`, which mirrors the CDK gate's `APPROVAL_TIMEOUT_SECONDS`). CDK does not set this variable — the code default applies unless an operator sets it.

## Event Formats

### Create Approval Request (from Step Functions)
```json
{
  "approval_type": "schema_mapping|code_generation|first_run",
  "pipeline_id": "run123.03",
  "run_id": "run123",
  "task_token": "step-functions-task-token",
  "execution_arn": "arn:aws:states:region:account:execution:name:id"
}
```

### Process Approval Decision (from API/Dashboard)
```json
{
  "approval_id": "run123.03_schema_mapping_1633024800",
  "decision": "approved|rejected",
  "comment": "Optional approval comment",
  "user": "user@example.com"
}
```

## Response Format

### Success Response
```json
{
  "statusCode": 200,
  "body": {
    "message": "Operation completed successfully",
    "approval_id": "approval_id",
    "status": "pending|approved|rejected"
  }
}
```

### Error Response
```json
{
  "statusCode": 400|500,
  "body": {
    "error": "ValidationError|InternalError",
    "message": "Error description"
  }
}
```

## DynamoDB Schema

### Approval Record Structure
```json
{
  "approval_id": "run123.03_schema_mapping_1633024800",
  "pipeline_id": "run123.03",
  "run_id": "run123",
  "approval_type": "schema_mapping",
  "status": "pending|approved|rejected|timeout",
  "created_at": "2025-10-09T10:00:00Z",
  "timeout_at": "2025-10-16T10:00:00Z",
  "timeout_behavior": "manual",
  "task_token": "step-functions-task-token",
  "execution_arn": "arn:aws:states:...",
  "approval_data": "{...}",
  "pipeline_config": "{...}",
  "processed_at": "2025-10-09T11:00:00Z",
  "processed_by": "user@example.com",
  "comment": "Approval comment",
  "ttl": 1635552000
}
```

**`timeout_behavior` has one value: `manual`.** It is not a configurable
setting. This field used to be documented as `manual|auto_reject|auto_approve`,
but the two auto values were never settable and never could fire — see the p47
R6 note in `get_pipeline_config` (`lambda_function.py`) for both blockers and
for what making them real would require. Treat this field as a constant on the
record, not as a knob.

## Integration with Step Functions

The function integrates with Step Functions using the `waitForTaskToken` pattern:

1. Step Functions calls this Lambda with a task token
2. Lambda stores the approval request and task token
3. Lambda returns immediately (Step Functions waits)
4. When approval decision is made, Lambda calls `send_task_success` or `send_task_failure`
5. Step Functions continues or terminates based on the decision

## Notifications

### SNS Notifications
- Sent when approval requests are created
- Includes pipeline details and dashboard link
- Configurable via pipeline settings

### EventBridge Events
- `Approval Request Created`: When new approval is needed
- `Approval Decision Made`: When approval is processed
- Used for real-time dashboard updates

## Error Handling

- Input validation with detailed error messages
- Graceful handling of missing pipeline configuration
- Audit logging for all approval decisions
- Automatic cleanup via DynamoDB TTL
- Circuit breaker pattern for external service calls

## Security

- Input sanitization to prevent injection attacks
- Field length limits to prevent abuse
- IAM role with least privilege access
- Audit trail for all approval decisions
- Encrypted storage in DynamoDB

## Monitoring

- CloudWatch logs for all operations
- EventBridge events for dashboard integration
- DynamoDB metrics for performance monitoring
- SNS delivery status tracking