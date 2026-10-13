# Pipeline Execution Lambda Function

## Description
This Lambda function starts the pipeline's Glue ETL job and hands the run back to Step Functions, which waits for the job's completion callback rather than for this function to return.

## Purpose
- Starts the Glue job for a pipeline whose infrastructure and Glue job already exist
- Persists the Step Functions task token so the run can be resumed when the job finishes
- Makes the run findable from a Glue job-state-change event by recording the Glue job run id
- Fails the Step Functions task honestly when the job cannot be started, rather than stranding the run

## Requirements Addressed
- Requirement 10.1: Execute complete ingestion process when all infrastructure is ready
- Requirement 10.2: Process data according to generated mappings and quality rules
- Requirement 10.3: Provide status and results information when pipeline execution completes

Data processing and quality evaluation happen inside the Glue job, not here. This function's part of 10.2 and 10.3 is starting that job and recording where its state lives.

## Input
The `Payload` of the Step Functions `RunPipeline` state, an `arn:aws:states:::lambda:invoke.waitForTaskToken` task:

```json
{
  "run_id": "run-20241112-abc123",
  "pipeline_id": "pipeline-customer-data",
  "taskToken": "AAAAKgAAAAIAAAAAAAAAAQ..."
}
```

All three are required. A missing `pipeline_id` or `run_id` raises before anything is written; a missing `taskToken` raises too, because the callback pattern has no other way to resume.

## Output
**Nothing is returned on the success path, and that is deliberate.** Step Functions is holding the task token, so this function returns `None` and the `job_completion` Lambda calls `send_task_success` or `send_task_failure` when the Glue job reaches a terminal state.

What it writes instead is two rows:

- **The task token, in the task-tokens table** (`agentic_data_ingestion_task_tokens`), keyed `approval_id = "glue_run#<run_id>"` and carrying `task_token`, `run_id`, `pipeline_id` and a `ttl` of `TASK_TOKEN_TTL_SECONDS` (7 days, the approval path's backstop, `shared/config.py`). It is deliberately **not** on the state row: AgentCore agents read the state table and the dashboard returns state rows to the browser, while the AgentCore runtime role is denied every action on the task-tokens table (`DenyApprovalTaskTokens`, LGR-8f59603-3). It is keyed by `run_id` because it must exist before `start_job_run` returns a job run id.
- **A state-table row** keyed `(run_id, step_name="run_pipeline")` carrying `status: "RUNNING"`, the Glue job name and `started_at` — no token — followed by an `update_item` that attaches `glue_job_run_id`. The job run id is what `job_completion` matches a Glue state-change event against, via the `GlueJobRunIndex` GSI; it then reads the token row by `run_id` and deletes it once the callback is delivered.

The event is logged with `taskToken` removed, because the dashboard reads this function's log group back to the browser.

### Ordering and failure behaviour
- **Both rows are written before `start_job_run`, the token row first** (P28 R5.1). The old order let a fast-failing job emit its completion event before the token existed, which stranded the run.
- **A failure after the pre-start writes cleans up the rows it wrote** — the state row and the token row — sends `send_task_failure` with the event's token, writes a `failed` state row, and then raises.
- **Retryable Glue errors raise `TransientStepFailure`** so the state machine's Retry policy can back off: concurrency, throttling, and internal service errors (`:37-45`, raised at `:325`). Everything else raises `StepFailure` (`:326`).

## AWS Permissions Required
- Glue: the shared Glue admin statement on `runPipelineRole` (`foundation-stack.ts:659`), which is
  `glue:*` scoped to project-named catalogs, databases, tables, jobs, crawlers and connections
  (`foundation-stack.ts:248-254`), not account-wide
- Step Functions: `states:SendTaskSuccess`, `states:SendTaskFailure`, `states:SendTaskHeartbeat` (`foundation-stack.ts:665`)
- DynamoDB: read/write on the state table, read on the pipelines table, `PutItem`/`DeleteItem` on the task-tokens table (all inside the shared `table/agentic_data_ingestion*` grant)
- EventBridge: `events:PutEvents` on the project event buses (`foundation-stack.ts:660`)
- CloudWatch: Logs write access

## Environment Variables
- `STATE_TABLE`: pipeline state table. CDK sets it (`compute-stack.ts:876-878`); the code default in `lambda_function.py` applies only in local mode
- `PIPELINES_TABLE`: pipelines table, same treatment
- `TASK_TOKENS_TABLE`: the task-tokens table the Glue-callback token is stored in, same treatment. Outside local mode the function fails at import without it

## Configuration
Declared in `deploy-cdk/lib/compute-stack.ts:866-880`: Python 3.12 on arm64, 300 s timeout (`:873`), 512 MB (`:874`), `[adotLayer, strandsLayer]` (`:875`).

## Implementation Status
✅ **Implemented.** Handler at `lambda_function.py`, wrapped in `@step_handler("run_pipeline")` (`:86`). Unit tests in `tests/unit/lambdas/run_pipeline/` cover the pre-start token ordering, where the token is stored, cleanup of both rows on failure, and the raise-on-failure contract.
