# Agentic Data Ingestion Approval Timeout Handler

This Lambda function handles approval timeouts for the Agentic Data Ingestion pipeline system.

## Functionality

- Scans for expired approval requests
- Escalates each one for manual intervention: the approval stays `pending`,
  `timeout_occurred` is set, and a CloudWatch alarm is raised
- Sends timeout notifications via SNS
- Creates CloudWatch alarms for manual escalations
- Records comprehensive metrics for monitoring

**Manual escalation is the only outcome.** The handler also carries
`auto_approve` / `auto_reject` branches keyed on the approval record's
`timeout_behavior`, and the `auto_approved` / `auto_rejected` counters in the
report below come from them — but they are **not configurable and do not fire**.
Nothing can set `timeout_behavior` to anything but `manual`, and even if
something could, the Step Functions gate abandons the execution at exactly 72 h
while this sweep runs every 15 minutes, so it always arrives to find a consumed
token. Both blockers, and why the branch code is deliberately retained rather
than deleted, are recorded at the dispatch in `lambda_function.py`
(p47 R6, `LGR-69ca78b-2`). Expect those two counters to be `0` in every report.

## Environment Variables

- `APPROVALS_TABLE`: DynamoDB table for approval requests. CDK sets it to the Approvals table
  (`compute-stack.ts:1176`), so the code default at `lambda_function.py:85`,
  `agentic_data_ingestion_approvals`, applies only in local mode. Outside local mode
  `require_env` raises rather than falling back.
- `SNS_TOPIC_ARN`: SNS topic for timeout notifications
- `CLOUDWATCH_ALARM_TOPIC`: SNS topic for urgent escalations

## Trigger

Typically triggered by CloudWatch Events on a schedule (e.g., every 15 minutes):

```json
{
  "Rules": [
    {
      "Name": "ApprovalTimeoutCheck",
      "ScheduleExpression": "rate(15 minutes)",
      "Targets": [
        {
          "Id": "ApprovalTimeoutHandler",
          "Arn": "arn:aws:lambda:region:account:function:agentic_data_ingestion_approval_timeout_handler"
        }
      ]
    }
  ]
}
```

## Metrics Published

- `ApprovalRequests.Timeout`: Count of timed out approvals by type and behavior
- `ApprovalNotifications.sns_timeout.Success/Failure`: Notification delivery status
- `ManualTimeoutEscalation`: Custom metric for CloudWatch alarms

## Response Format

```json
{
  "statusCode": 200,
  "body": {
    "message": "Timeout processing completed",
    "report": {
      "timestamp": "2025-10-09T10:00:00Z",
      "total_timeouts_processed": 3,
      "auto_approved": 0,
      "auto_rejected": 0,
      "manual_escalations": 3,
      "processing_errors": 0,
      "timeout_details": [...]
    }
  }
}
```