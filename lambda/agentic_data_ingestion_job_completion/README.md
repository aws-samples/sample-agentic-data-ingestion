# Glue Job Completion Lambda

## Purpose
Handles Glue job completion events from EventBridge and sends callbacks to Step Functions to resume pipeline execution.

## Trigger
EventBridge rule that monitors Glue job state changes (SUCCEEDED, FAILED, STOPPED, TIMEOUT).

## Flow
1. Receives EventBridge event when Glue job completes
2. Finds the run through the `GlueJobRunIndex` GSI on the state table
3. Reads the run's task token from the task-tokens table (`approval_id = "glue_run#<run_id>"`, written by `run_pipeline`). If no row exists it falls back to a `task_token` on the state row — only runs in flight when the token moved off the state row have one; the fallback can go once none can still be waiting
4. Collects comprehensive job metrics from Glue API
5. Calculates performance vs estimates
6. Updates DynamoDB with metrics
7. Sends `send_task_success` or `send_task_failure` to Step Functions, then deletes the token row (best effort)
8. Step Functions resumes execution

A duplicate delivery that arrives after the token row is gone, for a run whose state row is already terminal, returns cleanly without a send — the same outcome as a duplicate whose send is rejected as an already-consumed token.

## Metrics Collected
- **Timing**: Execution time, start/end timestamps
- **Resources**: Worker type, count, DPU-hours
- **Cost**: Actual cost, variance from estimate
- **Performance**: Duration variance, MCP recommendation accuracy
- **Data**: Records processed (if available)

## Environment Variables
- `STATE_TABLE`: DynamoDB table for pipeline runs
- `PIPELINES_TABLE`: DynamoDB table for pipeline metadata
- `PROCESSED_FILES_TABLE`: the processed-file ledger this function writes
- `TASK_TOKENS_TABLE`: the task-tokens table holding the Glue-callback token. Outside local mode the function fails at import without it
- `AWS_REGION`: AWS region

## IAM Permissions Required
- `glue:GetJobRun` - Retrieve job metrics
- `dynamodb:Query` - Find pipeline run by job_run_id (`GlueJobRunIndex`)
- `dynamodb:UpdateItem` - Update run with metrics
- `dynamodb:GetItem` - Get pipeline metadata and the task-token row
- `dynamodb:DeleteItem` - Delete the task-token row after the callback
- `states:SendTaskSuccess` - Resume Step Functions on success
- `states:SendTaskFailure` - Resume Step Functions on failure

## EventBridge Event Format
```json
{
  "source": "aws.glue",
  "detail-type": "Glue Job State Change",
  "detail": {
    "jobName": "agentic-data-ingestion-job-1231mike-e84aa182",
    "jobRunId": "jr_abc123",
    "state": "SUCCEEDED",
    "message": "Job run succeeded"
  }
}
```

## Output (sent to Step Functions)
```json
{
  "job_name": "agentic-data-ingestion-job-1231mike-e84aa182",
  "job_run_id": "jr_abc123",
  "status": "SUCCEEDED",
  "execution_time_seconds": 930,
  "duration_minutes": 15.5,
  "worker_type": "G.1X",
  "number_of_workers": 2,
  "dpu_hours": 0.52,
  "actual_cost": 0.23,
  "estimated_cost": 0.15,
  "cost_variance_percent": 53.3,
  "mcp_recommendation_accuracy": 85.0
}
```
