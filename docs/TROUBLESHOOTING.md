# Troubleshooting Guide

**Start from your symptom.** The nine groups below cover this whole guide — every `##` section belongs to exactly one of them.

1. **Deploy or synth failed**
   - [Deployment Issues](#deployment-issues)
2. **Dashboard Issues**
   - [Dashboard Issues](#dashboard-issues)
3. **A file was uploaded and no run appeared**
   - [Prefix Trigger / Multi-File Ingestion Troubleshooting](#prefix-trigger--multi-file-ingestion-troubleshooting)
   - [Concurrency Control Troubleshooting](#concurrency-control-troubleshooting)
4. **A run started and stopped, or is stuck**
   - [Pipeline Execution Issues](#pipeline-execution-issues)
   - [Worker Sizing Troubleshooting](#worker-sizing-troubleshooting)
   - [Checkpoint/Resume Troubleshooting](#checkpointresume-troubleshooting)
5. **The data is wrong, missing, or unreadable**
   - [Large File Handling](#large-file-handling)
   - [Parquet/Avro/ORC Troubleshooting (Phase 9)](#parquetavroorc-troubleshooting-phase-9)
   - [Write Mode / Merge Troubleshooting](#write-mode--merge-troubleshooting)
6. **The mapping or generated code is wrong**
   - [Rerun Mapping Troubleshooting](#rerun-mapping-troubleshooting)
   - [Code Interpreter Troubleshooting (Phase 12)](#code-interpreter-troubleshooting-phase-12)
   - [AgentCore Memory Troubleshooting](#agentcore-memory-troubleshooting)
7. **A Lambda or agent errored**
   - [MCP Issues](#mcp-issues)
   - [Lambda Errors](#lambda-errors)
   - [AgentCore Runtime Troubleshooting](#agentcore-runtime-troubleshooting)
8. **The UI is not updating**
   - [WebSocket Real-Time Updates](#websocket-real-time-updates)
9. **A JDBC or RDS target misbehaves**
   - [RDS Database Targets Troubleshooting](#rds-database-targets-troubleshooting)

Four sections earlier revisions of this guide carried are deliberately absent. *Schema Hash Format Change* and *S3 Select Limitations* were deleted as obsolete (p62 R22.1, R22.2); *Observability & Structured Logging* and *Monitoring* moved to [OBSERVABILITY.md](./OBSERVABILITY.md). Those four plus the 18 sections above account for all 22 sections the restructure started from.

Two headings below still carry a phase tag — *Parquet/Avro/ORC Troubleshooting (Phase 9)* and *Code Interpreter Troubleshooting (Phase 12)*. Both sit inside content-addressed fences that this restructure relocated byte-for-byte, so dropping those two tags is deferred, not done: it edits fenced bytes. The tags come out when the fences are released. Phase tags in body prose are provenance and stay.

## Deployment Issues

### CDK Deploy Fails

```bash
# Check which stack failed
npx cdk diff

# View CloudFormation events for the failed stack
aws cloudformation describe-stack-events --stack-name AgenticDataIngestion-Compute-dev \
  --query "StackEvents[?ResourceStatus=='CREATE_FAILED' || ResourceStatus=='UPDATE_FAILED']"
```

Common causes:
- Missing `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION` env vars
- CDK not bootstrapped: run `npx cdk bootstrap`
- IAM permissions: deploying user needs CloudFormation, Lambda, S3, DynamoDB, IAM, ECR, etc.
- Resource name conflicts from a previous partial deployment — if a teardown ran before this
  deploy, see [Teardown failed and the next deploy collides](#teardown-failed-and-the-next-deploy-collides)

### Lambda Layer Build Fails

The Strands layer is built via Docker bundling during `cdk deploy`. If it fails:

```bash
# Verify Docker is running
docker info

# Try building manually
docker run --rm -v $(pwd):/asset-input -v /tmp/layer:/asset-output \
  public.ecr.aws/sam/build-python3.12:latest-arm64 \
  pip install strands-agents mcp jinja2 aws-lambda-powertools \
  --target /asset-output/python --only-binary=:all:
```

### ECR Push Fails

CDK handles ECR authentication automatically. If it fails:

```bash
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
```

### Hosting Deploy Fails With a CloudFront Function Validation Error

`CREATE_FAILED | AWS::CloudFront::Function` carrying `The parameter Comment is too big` means the `SpaRouter` function's `Comment` is over CloudFront's **128-character** cap. This failed `AgenticDataIngestion-Hosting-dev` on **2026-08-31**. For the cap, why it is caught only at deploy time, and the assertion that now pins it, see [DEPLOY_HOSTING.md § The 128-character `Comment` cap](DEPLOY_HOSTING.md#the-128-character-comment-cap).

### CLOUDFRONT-scope WAF Must Be in us-east-1

The `EdgeWaf` stack creates a CLOUDFRONT-scope `WebACL`, which AWS requires to live in **us-east-1** regardless of the application region. The stack is pinned to that region (`env: { ..., region: 'us-east-1' }`) and its ARN is wired into the Hosting stack via cross-region references (`crossRegionReferences: true` on both EdgeWaf and Hosting). If synth or deploy fails with a WAF scope/region error (e.g. `WAFInvalidParameterException`, or a CLOUDFRONT WebACL rejected outside us-east-1), confirm the EdgeWaf stack still targets us-east-1 and that `crossRegionReferences` is enabled on both stacks.

### `exec format error` During Deploy

All Lambda layers and container images in this project are built for **linux/arm64**
(SAM `build-python3.12:latest-arm64` bundling images, `Platform.LINUX_ARM64` Docker
assets). On an x86_64 host, Docker can only run those build containers through
QEMU/binfmt emulation — without it, builds die with `exec format error`.

Check whether your Docker can build arm64:

```bash
docker buildx ls   # the active builder must list linux/arm64
```

If `linux/arm64` is missing (typical on x86 Linux hosts), install binfmt emulation:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

Native ARM64 hosts (Apple Silicon, AWS Graviton) need nothing — builds run natively
and faster. If you're picking an EC2 build host, choose Graviton (see
[DEPLOY_EC2.md](DEPLOY_EC2.md)).

### `No space left on device` During Deploy

A first full deploy builds 9 AgentCore container images plus several Lambda layer
bundles — expect Docker to consume tens of GB. The default 8 GB EBS volume on a
stock EC2 instance is nowhere near enough; **~100 GB free is recommended**
(`./deploy.sh` warns below 100 GB and fails below 50 GB; override the floor with
`ADI_MIN_DISK_GB`).

Reclaim space between attempts:

```bash
docker system df                    # see what's consuming space
docker system prune -af --volumes   # remove ALL unused images/containers/volumes
```

> `prune -af --volumes` deletes every cached image and build layer, so the next
> deploy rebuilds everything from scratch. It frees the most space at the cost of
> the slowest next build.

On EC2, grow the EBS volume instead (`aws ec2 modify-volume` + `growpart` +
`xfs_growfs`/`resize2fs`) — see [DEPLOY_EC2.md](DEPLOY_EC2.md).

### `permission denied` on `/var/run/docker.sock`

Common on fresh EC2 instances (especially in SSM sessions): your user isn't in the
`docker` group, or the group membership hasn't taken effect in the current session.

```bash
sudo usermod -aG docker "$USER"
```

Group membership only applies to **new** sessions: exit and reconnect (for SSM,
end the session and re-run `aws ssm start-session`), or use `newgrp docker` in the
current shell.

> **Do not** `chmod 666 /var/run/docker.sock`. A world-writable Docker socket lets
> any local process control the Docker daemon, which is equivalent to unauthenticated
> root access on the host.

### Your deployed stack is behind this repo

**One diagnosis, referenced from every symptom section that needs it.** A symptom that
reproduces on your deployment but not on someone else's — a missing Glue job argument, a
truncated task token, a field the docs say is written but isn't — is usually a deployed
stack older than the code in this working tree, not a defect.

It is stated once on purpose. Several symptom sections used to each name a *different*
historical build ("fixed in Phase 10", "old code used `--TABLE_BUCKET_ARN`", "an older
version without `detect_format()`"), which reads as concurrent diagnoses when it is one.
Which build introduced which fix stopped being useful long before the symptoms did; what
is actionable is whether **your** deployment is current.

Check, then bring it up to date:

```bash
# When was the Lambda code last published?
aws lambda get-function-configuration --function-name agentic_data_ingestion_create_code \
  --query "LastModified" --region us-west-2

cd agentic_data_ingestion/deploy-cdk
npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never --exclusively
```

`Compute` carries the Lambda code and the API router. The AI steps (`create_code`,
`test_code`, `create_glue_pipeline`, `create_infrastructure`) also run as AgentCore
Runtime containers, so a change to one of those needs **both** stacks:

```bash
npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never
```

Rows already written to DynamoDB by an older build are a separate matter — redeploying
does not rewrite them. Where an absent attribute changes behaviour, the symptom section
says what the default is.

See [GETTING-STARTED.md § Deployment Options](GETTING-STARTED.md#deployment-options) for
the full deploy sequence and stack ordering.

### Teardown failed and the next deploy collides

**One diagnosis, two symptoms.** `./destroy.sh` reported resources it could not delete, or a
later `cdk deploy` fails creating a resource that already exists. Those are the same event seen
from either end, so the chain is stated once here and referenced from
[CDK Deploy Fails](#cdk-deploy-fails) above.

For the teardown procedure itself — the stacks, the retained DynamoDB tables, and the
account-global IAM a region-scoped teardown leaves behind — see
[QUICKSTART.md § Teardown](../QUICKSTART.md#teardown). This section is about the run that
did not finish cleanly.

How a collision arises:

1. A stack delete leaves one or more resources in `DELETE_FAILED`. `destroy.sh` reports each
   one's logical id and its **full** failure reason, which is usually enough on its own — a
   non-empty bucket, a role still in use, a live dependency. None of those clears by waiting.
2. `destroy.sh` then issues **one** `delete-stack --retain-resources` retry naming those ids,
   and re-waits. It is deliberately not a loop: every attempt abandons resources, and a loop
   would abandon a growing set with no operator watching.
3. If that retry succeeds, the stack is gone and **the retained resources are still there** —
   live, no longer managed by CloudFormation, and still billed.
4. Abandoned resources keep their **physical names**. Where a name is a hardcoded literal, the
   next deploy asks CloudFormation to create something that already exists, and stops.

`destroy.sh` names them at the end of the run, under `RETAINED by a retain-resources retry`,
with the account they are live in. Read that block before doing anything else — it is the
complete list of what was abandoned, and the only place the list appears.

> A skipped stack is a different outcome with the same consequence. `--continue-past-failed-stacks`
> is opt-in; without it the run stops at the first stack that will not delete. With it, the run
> walks past that stack, names it under `SKIPPED`, and exits non-zero — nothing in that stack was
> deleted or abandoned, it is simply all still standing. Stacks delete in dependency order, so
> failures reported *after* a skipped stack may have failed **because** of it.

**AgentCore runtimes are the class most likely to collide.** Every runtime name in
`deploy-cdk/lib/agentcore-stack.ts` is a hardcoded string with **no `${environment}` suffix**.
The agent runtimes carry the literal inline at their `AgentRuntimeName` (`:520`, `:538`, `:556`,
`:574`, `:601`, `:626`, `:654`, `:679`, `:704`); the MCP server runtimes are the same story one
step removed — the literals are declared at `:67-71` and referenced from `AgentRuntimeName` at
`:348`, `:366`, `:386`, `:404`, `:422`. So the hazard applies to every runtime this system
declares, agent and MCP alike, not only the agent ones. Counts are not restated here; they live
once, with their derivation, in
[ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory).

`MappingMemory` is the counter-example, and it is the reason to read this as a naming choice
rather than a service limitation: its `Name` is `agentic_data_ingestion_mapping_memory_${environment}`
(`agentcore-stack.ts:440`), so it is scoped per environment and does not collide. Every runtime
name could have been written that way and was not.

**Clearing abandoned runtimes.** `scripts/cleanup_agentcore_runtimes.sh` is the tool for this:

```bash
cd agentic_data_ingestion

# Dry run — enumerates candidates via read-only APIs and deletes nothing:
./scripts/cleanup_agentcore_runtimes.sh --profile <name> --region us-west-2

# Delete:
./scripts/cleanup_agentcore_runtimes.sh --profile <name> --region us-west-2 --delete
```

- **Dry run is the default.** Without `--delete` it reaches no mutating call at all. `--profile`
  and `--region` are both required and have no defaults; the account is derived from the profile
  and echoed before anything is touched.
- `--prefix` defaults to `agentic_data_ingestion_`, which selects every runtime this system
  declares — agent and MCP alike. Pass `agentic_data_ingestion_agent_` for the agent runtimes
  only. The two select different sets, and the report says which one you got.
- The identifier a delete uses is the runtime **id** (`agentRuntimeId`), which is neither the
  runtime name nor the CloudFormation logical id. Three identifier spaces are in play, so read
  the id out of the report rather than composing it.
- **It refuses to delete while the AgentCore stack still exists**, even with `--delete`. That is
  not a malfunction: a runtime that stack still claims is a live resource, not an orphan. If you
  see the refusal, the stack is standing — delete the stack first. A refused `--delete` exits
  non-zero (you asked for a delete and did not get one); a dry run that would have been refused
  exits 0.
- **It does not cover the Memory resource**, which is `AWS::BedrockAgentCore::Memory` on a
  different API — `list-agent-runtimes` never returns it. Check that one by hand:

  ```bash
  aws bedrock-agentcore-control list-memories --region us-west-2 --profile <name>
  ```

It is **not** wired into `destroy.sh`, on purpose: that script deletes no Glue resources at all,
so bolting an e2e-specific tool onto it would imply the catalog is handled when it is not. The
general teardown gap is a separate finding. Run this as a separate step
after a teardown that reported retained AgentCore resources, once the stack itself is gone.

## Dashboard Issues

### API Returns 403

After the IAM → Cognito switch, direct API Gateway access is blocked by the regional WAF (`X-Origin-Verify` enforcement). **You must access the API through CloudFront.**

```bash
# Verify your CloudFront URL works (should redirect to login without a token)
curl -s -o /dev/null -w "%{http_code}" https://<CloudFrontDomain>/api/pipelines
# Expected: 401 (missing token)

# Direct execute-api access is blocked:
curl -s -o /dev/null -w "%{http_code}" https://<api-id>.execute-api.<region>.amazonaws.com/v1/api/pipelines
# Expected: 403 (WAF blocks)
```

The CloudFront `/api/*` behavior uses the managed `CachePolicy.CACHING_DISABLED` and `OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER` policies with an origin path of `/v1`. That forwards the `Authorization` header (and all viewer headers except `Host`) to API Gateway and never caches API responses. If API responses appear cached or the `Authorization` header isn't reaching the API, verify those two policies are still attached to the `/api/*` behavior.

For local development setup, see `docs/LOCAL_DEVELOPMENT.md`.

### Login Redirect Loop

If you see an infinite redirect between the app and Cognito:
- Verify `http://localhost:5173` is registered in the Cognito app client's callback URLs.
- Check that `.env.local` has the correct `VITE_USER_POOL_CLIENT_ID` and `VITE_HOSTED_UI_DOMAIN`.

### Token Expired / 401 on API Calls

The OIDC library refreshes tokens automatically. If you still get 401:
- **Verify the dashboard sends the Cognito ID token, not the access token.** The API Gateway Cognito authorizer is scope-less, so it validates the **ID token**. Send it as the raw `Authorization` header value with **no `Bearer ` prefix** — a `Bearer ` prefix or the access token is rejected with 401.
- Sign out and sign back in via the app.
- Ensure the Cognito User Pool authorizer references the correct pool.
- Check browser DevTools → Application → Session Storage for stale token data.

### No Data in Dashboard

```bash
# Check DynamoDB has data
aws dynamodb scan --table-name agentic_data_ingestion_pipelines --max-items 3 --region us-west-2

# Check API Gateway is responding
aws apigateway get-rest-apis --query "items[?contains(name, 'agentic_data_ingestion')]"
```

### `ApiError(200, 'Response body could not be read as JSON (HTTP 200)')`

An API path answered HTTP 200 with an HTML body, so the response could not be parsed as JSON. A current deployment should not produce this; if it does, the true status has been overwritten and the underlying response is an error — most often a 403.

**Do not harden the client to detect an HTML body on an API path.** That was considered and explicitly rejected when the SPA router was designed: *by then the true status is gone*, so the best such a check could do is guess 404, and it would leave the 403 case (every WAF block, every Cognito authorizer rejection) still ambiguous. Diagnose the 403 at its source instead — see [API Returns 403](#api-returns-403) above, and [DEPLOY_HOSTING.md § SPA routing and the 403 residual](DEPLOY_HOSTING.md#spa-routing-and-the-403-residual) for the full explanation.

## Prefix Trigger / Multi-File Ingestion Troubleshooting

### Empty Prefix Error

`ValueError: No files found in s3://bucket/prefix/` means the prefix has no objects. Check:

```bash
# List files in the prefix
aws s3 ls s3://your-bucket/your-prefix/ --recursive
```

Common causes:
- Trailing slash mismatch — `customer-data/2026-03-03` vs `customer-data/2026-03-03/`
- Files haven't finished uploading yet
- Wrong bucket name

### Mixed File Formats in Prefix

The prefix trigger validates the first file's format and assumes all files in the prefix share the same format. If files have mixed formats (e.g., `.csv` and `.json` in the same prefix):

- The Glue job will attempt to read all files using the first file's format, which will fail for mismatched files
- **Solution**: Organize files by format in separate prefixes (e.g., `data/csv/`, `data/json/`)

### Prefix Trigger Returns Wrong File Count

`list_s3_files()` uses paginated `list_objects_v2` and counts all objects under the prefix, including:
- Subdirectory markers (zero-byte keys ending in `/`)
- Hidden files (e.g., `_SUCCESS`, `.crc` files from Spark)

If the count seems inflated, check for non-data objects in the prefix.

### Batch Trigger API Returns 404

Ensure the route is registered:
```bash
# Check the dashboard API Lambda has the trigger-batch route
aws logs tail /aws/lambda/agentic_data_ingestion_dashboard_api --since 5m --region us-west-2 --format short \
  | grep "trigger-batch\|trigger_batch"
```

If the route isn't found, redeploy the Compute stack to pick up the new router configuration.

### Batch Trigger Starts Multiple Executions

The batch trigger should start exactly 1 Step Functions execution. If you see multiple executions:
- Check that the S3 PUT EventBridge rule isn't also firing for the same files
- The prefix trigger is initiated via the dashboard API, not via S3 events — ensure you're using `POST /api/pipelines/{id}/trigger-batch` and not uploading files to a monitored prefix

### Schema Extraction Fails for Prefix Mode

In prefix mode, schema extraction lists the first file in the prefix and extracts schema from that file. If it fails:

1. **Check the first file is valid** — the first file (alphabetically) is used as the representative sample
2. **Check the prefix ends with `/`** — schema extraction detects prefix mode by checking if `source_s3_uri` ends with `/`
3. **Check Lambda logs**:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_schema_extract --since 10m --region us-west-2 --format short
   ```


---

## Concurrency Control Troubleshooting

### Events Stuck in SQS / Not Processing

If uploaded files aren't triggering pipelines:

1. **Check the SQS queue depth**:
   ```bash
   aws sqs get-queue-attributes \
     --queue-url https://sqs.us-west-2.amazonaws.com/$ACCOUNT/agentic-data-ingestion-pipeline-triggers-dev \
     --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
     --region us-west-2
   ```

2. **Check the trigger Lambda logs**:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_pipeline_trigger --since 10m --region us-west-2 --format short
   ```

3. **Common causes**:
   - Concurrency limit reached — messages stay in queue until a running execution completes
   - Trigger Lambda throttled — `reservedConcurrentExecutions: 5` limits parallel consumers
   - SQS event source mapping disabled — check `aws lambda list-event-source-mappings --function-name agentic_data_ingestion_pipeline_trigger`

### Messages in Dead Letter Queue

Messages land in the DLQ after 3 failed processing attempts. Inspect DLQ messages:

```bash
aws sqs receive-message \
  --queue-url https://sqs.us-west-2.amazonaws.com/$ACCOUNT/agentic-data-ingestion-pipeline-triggers-dlq-dev \
  --max-number-of-messages 5 --region us-west-2
```

Common causes for DLQ messages:
- **Persistent concurrency limit**: If all 10 execution slots are occupied for longer than 3 × 120s (6 minutes), the message exhausts retries
- **Malformed EventBridge event**: The SQS body contains the EventBridge envelope — if the S3 event structure changed, parsing fails
- **Step Functions start_execution error**: IAM permission issue or state machine ARN mismatch

To reprocess DLQ messages, use the SQS DLQ redrive feature in the AWS Console or move messages back to the main queue.

### Adjusting Concurrency Limits

To change the maximum concurrent pipeline executions:

```bash
# Update the trigger Lambda environment variable
aws lambda update-function-configuration \
  --function-name agentic_data_ingestion_pipeline_trigger \
  --environment "Variables={STATE_MACHINE_ARN=arn:...,MAX_CONCURRENT_EXECUTIONS=20}" \
  --region us-west-2
```

For permanent changes, update `MAX_CONCURRENT_EXECUTIONS` in `deploy-cdk/lib/orchestration-stack.ts` and redeploy.

---

## Pipeline Execution Issues

### A JDBC Run Succeeds But Shows No Row Counts

The run is green, the data is in the target, and the dashboard still reports the load outcome as `not_established` with no `rows_read` / `rows_written`. The ETL job records those counts to DynamoDB after the target write, and a Glue job running inside a VPC needs a route to DynamoDB to do it.

Confirm it from the job's own log — the last line will be:

```
WARNING Failed to write load metrics to DynamoDB: Connect timeout on
endpoint URL: "https://dynamodb.us-west-2.amazonaws.com/"
```

```bash
# Find the job run, then read its log stream
aws glue get-job-runs --job-name agentic-data-ingestion-job-YOUR_PIPELINE_ID \
  --max-results 1 --query 'JobRuns[0].Id' --output text --region us-west-2
aws logs get-log-events --log-group-name /aws-glue/jobs/output \
  --log-stream-name JOB_RUN_ID --region us-west-2 \
  --query 'events[*].message' --output text | tail -5
```

Check whether the VPC has the endpoint. An internet gateway route does **not** substitute for one: a Glue ENI has no public IP, so `0.0.0.0/0 -> igw-...` is a blackhole for it.

```bash
aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=YOUR_VPC_ID" \
  --query 'VpcEndpoints[*].{svc:ServiceName,type:VpcEndpointType}' \
  --output table --region us-west-2
aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=YOUR_VPC_ID" \
  --query 'NatGateways[*].NatGatewayId' --region us-west-2
```

Fix: add a DynamoDB VPC Gateway Endpoint to the VPC. It has no hourly charge and needs no security group. The wizard's **Detect Schema** pre-flight checks only for the **S3** endpoint, so a subnet missing this one passes discovery and runs successfully — which is why the symptom is a silent gap rather than an error. See [JDBC_SOURCE.md — DynamoDB VPC Gateway Endpoint requirement](JDBC_SOURCE.md#dynamodb-vpc-gateway-endpoint-requirement).

The job also loses about three seconds per run to the doomed attempt. If you are looking at an older build and the job took **many minutes** longer than its work should need, that is the unbounded version of the same call — the client is now capped at one attempt with a 3-second connect timeout.

### A JDBC Run Logs `Failed to write source sample`

```
WARNING Failed to write source sample: An error occurred (AccessDenied) when
calling the PutObject operation: ... s3:PutObject on resource:
"arn:aws:s3:::adi-processed-files-.../samples/PIPELINE_ID/source-sample.json"
```

The run succeeds and the data lands; what is lost is the sample later runs use for mapping and date-format inference. The per-pipeline Glue execution role needs `s3:PutObject` on its own `samples/` prefix, which current builds grant. Confirm the deployed role:

```bash
aws iam get-role-policy --role-name AgenticDataIngestion-YOUR_PIPELINE_ID-ExecutionRole \
  --policy-name PipelineSpecificAccess --region us-west-2 \
  --query 'PolicyDocument.Statement[?contains(to_string(Action), `PutObject`)]'
```

The role is created once per pipeline and then reused, so a pipeline created by an older build keeps its old policy even after you deploy the fix — the role is not rewritten on later runs. Delete the role and let the next run recreate it, or add the statement by hand. See [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

### Pipeline Stuck at Approval

The Step Functions execution pauses at `WaitForCodeApproval` or `WaitForSchemaMappingApproval`. Check the Approvals page in the dashboard, or:

```bash
# List pending approvals
aws dynamodb scan --table-name agentic_data_ingestion_approvals \
  --filter-expression "#s = :pending" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":pending":{"S":"pending"}}' \
  --region us-west-2
```

### Schema Mapping Approval Triggers Unexpectedly

The schema mapping approval gate fires when:
1. The source schema genuinely changed (`schema_change_action` is `create_new_pipeline`, `update_existing_pipeline`, or `remap_required`) and the pipeline's `always_approve_schema_changes` is `True`
2. Mapping confidence is below the threshold (0.8 — `CONFIDENCE_AUTO_APPROVE_THRESHOLD` in `shared/config.py`)

To check a pipeline's approval config:
```bash
aws dynamodb get-item --table-name agentic_data_ingestion_pipelines \
  --key '{"pipeline_id":{"S":"YOUR_PIPELINE_ID"}}' \
  --projection-expression "approval_config" \
  --region us-west-2
```

If `approval_config` is missing, the default behavior applies: approval is enabled with `always_approve_schema_changes: True`. A pipeline row written by an older build has no `approval_config` at all — see [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

### Approval Doesn't Resume Execution

Check the approval handler logs:
```bash
aws logs tail /aws/lambda/agentic_data_ingestion_dashboard_api --since 5m --region us-west-2 \
  --format short | grep -i "task_token\|send_task\|approv"
```

Common cause: the task token was truncated, which a current build does not do — see [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

### Execution Shows Wrong Status

The dashboard cross-references three sources for execution status:
1. State table steps (what Lambda steps completed/failed)
2. Approvals table (pending approval = WAITING_APPROVAL)
3. Step Functions API (ABORTED, FAILED, SUCCEEDED)

If status is stale, the Step Functions execution may have been manually stopped. Check:
```bash
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:$ACCOUNT:stateMachine:agentic-data-ingestion-pipeline \
  --max-results 10 --region us-west-2
```

### Code Generation Fails

```bash
aws logs tail /aws/lambda/agentic_data_ingestion_create_code --since 30m --region us-west-2 --format short
```

Common causes:
- Bedrock model not enabled in your region (enable in Bedrock console)
- MCP endpoint not configured (check `AGENTCORE_ENDPOINT` env var)
- Lambda timeout (max 15 min)

### Glue Job Creation Fails

```bash
aws logs tail /aws/lambda/agentic_data_ingestion_create_glue_pipeline --since 30m --region us-west-2 --format short
```

Common causes:
- `target_config` not stored in pipelines table (check pipeline was created with target config)
- IAM role creation failed (check IAM permissions, and see the next section)
- Glue job name conflict

### Pipeline Role Creation Fails With `AccessDenied` on `iam:CreateRole`

The per-pipeline Glue role (`AgenticDataIngestion-<pipeline_id>-ExecutionRole`) must be created with the `AgenticDataIngestion-GlueJobRoleBoundary-<env>` permissions boundary. Foundation allows `iam:CreateRole` and `iam:PutRolePolicy` on these roles **only** when that boundary is attached (threat model T-03), so a create without it is refused. There is no fallback role any more: the `create_glue_pipeline` step fails.

The role builder reads the boundary's ARN from `GLUE_JOB_ROLE_BOUNDARY_ARN`. Both the `create_glue_pipeline` Lambda (Compute stack) and the AgentCore containers (AgentCore stack) must have it, so this error after an upgrade usually means Foundation was deployed but Compute or AgentCore was not:

```bash
aws lambda get-function-configuration --function-name agentic_data_ingestion_create_glue_pipeline \
  --query 'Environment.Variables.GLUE_JOB_ROLE_BOUNDARY_ARN' --region us-west-2
```

Deploy Foundation, then Compute and AgentCore. `cdk deploy --all` orders them correctly.

### A Glue Job Fails With `AccessDenied` After the Least-Privilege Role Change

Per-pipeline Glue roles no longer carry the AWS managed `AWSGlueServiceRole` policy (threat model T-02). That policy granted `glue:*` on every resource in the account, and the jobs only ever needed a small part of it. What they need is now granted inline and scoped: CloudWatch metrics in the `Glue`, `Glue Data Quality` and `AgenticDataIngestion/DataQuality` namespaces, Glue Data Quality result publishing, read access to the pipeline's own job, and catalog access to the configured target database and table.

If a job run fails with an `AccessDenied` that names a Glue or CloudWatch action, the run needs something the inline policy does not grant. Read the action and resource from the error, then add the grant in `iam_roles.py`. Add it to `GlueJobRoleBoundary` in `deploy-cdk/lib/foundation-stack.ts` as well, or the boundary silently cancels it; `tests/contracts/test_glue_job_role_boundary_covers_inline_policy.py` fails if you forget.

**Roles created before this change are not updated.** `create_pipeline_specific_role` reuses an existing role unchanged, so a pipeline created earlier keeps `AWSGlueServiceRole` and no boundary. Those roles keep working, with the old, broader permissions. The runtime roles can no longer modify them, because `iam:PutRolePolicy` now requires the boundary and `iam:AttachRolePolicy` is not granted. To move an existing pipeline onto the scoped role, delete its role while no run is in progress and let the next run recreate it. The recipe at the end of [JDBC target secret could not be resolved](#jdbc-target-secret-could-not-be-resolved) deletes the inline policy, detaches every managed policy, then deletes the role.

### Glue Job Parameter Errors (GlueArgumentError)

If a Glue job run fails with `GlueArgumentError: the following arguments are required: --source_path, --table_bucket_arn, ...`, the `DefaultArguments` don't match the template's `getResolvedOptions` names.

**Step 1: Check what the template expects**

Each template defines its required parameters in `getResolvedOptions`. See the contract table:

| Template | Required Parameters |
|----------|-------------------|
| `glue_job_s3_tables.py.j2` | `source_path`, `table_bucket_arn`, `namespace`, `table_name` |
| `glue_job_glue_catalog.py.j2` | `source_path`, `database_name`, `table_name` |
| `glue_job_jdbc.py.j2` | `source_path`, `jdbc_url`, `jdbc_table`, `jdbc_driver`, `secret_arn` |
| `glue_job_s3_parquet.py.j2` | `source_path`, `output_path` |

Note: `RDS_DATABASE` targets reuse `glue_job_jdbc.py.j2`. For every JDBC target, `secret_arn` is a Secrets Manager reference that the job resolves with `get_secret_value` at **run time**; no plaintext `jdbc_user` / `jdbc_password` argument exists. If `create_glue_pipeline` cannot find a reference it fails before creating the job; see [JDBC target secret could not be resolved](#jdbc-target-secret-could-not-be-resolved).

**Step 2: Check what the Glue job actually has**

```bash
aws glue get-job --job-name "agentic-data-ingestion-YOUR-PIPELINE-ID-TIMESTAMP" --region us-west-2 \
  --query "Job.DefaultArguments"
```

Compare the keys against the template's `getResolvedOptions` list. Every param (except `JOB_NAME`) must have a `--{param}` key with a non-empty value.

**Step 3: Check source_path resolution**

`source_path` comes from the `file_validation` step's `json_s3_uri` field in the state table:

```bash
aws dynamodb get-item --table-name agentic_data_ingestion_state \
  --key '{"run_id":{"S":"YOUR_RUN_ID"},"step_name":{"S":"file_validation"}}' \
  --projection-expression "json_s3_uri" \
  --region us-west-2
```

If `json_s3_uri` is missing, the file_validation step didn't complete successfully or didn't write the field.

**Step 4: Check warehouse_location resolution (S3 Tables only)**

`warehouse_location` is resolved from the `CreateInfrastructure` step or the S3 Tables API:

```bash
# Check infrastructure step output
aws dynamodb get-item --table-name agentic_data_ingestion_state \
  --key '{"run_id":{"S":"YOUR_RUN_ID"},"step_name":{"S":"CreateInfrastructure"}}' \
  --projection-expression "output_data" \
  --region us-west-2

# Check S3 Tables API directly
aws s3tables get-table \
  --table-bucket-arn "YOUR_TABLE_BUCKET_ARN" \
  --namespace "YOUR_NAMESPACE" \
  --name "YOUR_TABLE_NAME" \
  --query "warehouseLocation" \
  --region us-west-2
```

**Step 5: Check the create_glue_pipeline Lambda logs**

```bash
aws logs tail /aws/lambda/agentic_data_ingestion_create_glue_pipeline --since 30m --region us-west-2 --format short \
  | grep -i "parameter\|missing\|source_path\|warehouse"
```

Look for validation warnings like `Missing required parameter: --source_path` or `Empty value for --warehouse_location`.

**Common causes and fixes**:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `--source_path` missing | `file_validation` step didn't write `json_s3_uri` | Re-run pipeline; check file_validation Lambda logs |
| `--warehouse_location` empty | Infrastructure step skipped for existing bucket, S3 Tables API call failed | Check S3 Tables API permissions; verify table exists |
| `--table_bucket_arn` wrong case | An older build emitted `--TABLE_BUCKET_ARN` | [Deployed stack is behind](#your-deployed-stack-is-behind-this-repo) |
| `--namespace` missing | An older build emitted `--S3_TABLES_NAMESPACE` | [Deployed stack is behind](#your-deployed-stack-is-behind-this-repo) |
| `--database_name` missing | An older build emitted `--target-database` | [Deployed stack is behind](#your-deployed-stack-is-behind-this-repo) |
| All params missing | `parameter_builder.py` read wrong DynamoDB field | Check `required_parameters` vs `required_params` field name in generated_code table |
| `BadRequestException: table bucket ARN is not valid` | Iceberg catalog `warehouse` config set to S3 URI instead of ARN | Template should use `args['table_bucket_arn']` not `args['warehouse_location']` |
| `warehouse_location could not be resolved` | `CreateGluePipeline` ran before `CreateInfrastructure` | Check Step Functions step ordering; infrastructure must run first |
| `'dict object' has no attribute 'source_type'` | Jinja2 `StrictUndefined` + missing keys in mapping dicts | Template must use `m.get('source_type')` not `m.source_type` |
| `df_transformed = df` (passthrough, no mappings) | Field mappings not extracted from `mapping_result` JSON | Check `create_code` Lambda parses `mapping_result` string |

**Step 6: Run contract tests locally**

```bash
cd agentic_data_ingestion
uv run python -m pytest tests/contracts/api_responses/test_glue_parameter_contract.py -v
```

These tests validate that every template's `getResolvedOptions` params have matching `DefaultArguments` keys.

## Worker Sizing Troubleshooting

### Glue Job Under-Provisioned (OOM or Slow)

If a Glue job fails with `OutOfMemoryError` or runs much longer than expected:

1. **Check what sizing was applied**:
   ```bash
   aws glue get-job --job-name "agentic-data-ingestion-YOUR-PIPELINE-ID" --region us-west-2 \
     --query "Job.{WorkerType:WorkerType,NumberOfWorkers:NumberOfWorkers,Timeout:Timeout}"
   ```

2. **Check if file_size_bytes was passed**:
   ```bash
   aws dynamodb get-item --table-name agentic_data_ingestion_state \
     --key '{"run_id":{"S":"YOUR_RUN_ID"},"step_name":{"S":"file_validation"}}' \
     --projection-expression "output_data" --region us-west-2
   ```
   If `file_size_bytes` is missing, the baseline function defaults to the smallest tier (G.1X/2).

3. **Override via agent**: The Strands agent can override baseline sizing for complex transformations. Check the `create_glue_pipeline` Lambda logs for `WorkerRecommendation` entries.

### Sizing Tier Reference

| Total Data Volume | Worker Type | Workers | Timeout |
|-------------------|-------------|---------|---------|
| < 1 GB            | G.1X        | 2       | 30 min  |
| 1–10 GB           | G.1X        | 5       | 60 min  |
| 10–50 GB          | G.2X        | 10      | 90 min  |
| > 50 GB           | G.2X        | 20      | 120 min |

---

## Checkpoint/Resume Troubleshooting

### Checkpoint Not Resuming After Failure

If a re-run starts from scratch instead of resuming:

1. **Verify checkpoints are enabled** — check Glue job DefaultArguments:
   ```bash
   aws glue get-job --job-name "agentic-data-ingestion-YOUR-PIPELINE-ID" --region us-west-2 \
     --query "Job.DefaultArguments" | grep checkpoint
   ```
   Should show `--checkpoint_enabled: true` and `--checkpoint_bucket: adi-checkpoints-...`.

2. **Check checkpoint data exists in S3**:
   ```bash
   aws s3 ls s3://adi-checkpoints-$ACCOUNT-$REGION/agentic-data-ingestion-YOUR-PIPELINE-ID/ --recursive
   ```

3. **Check the 7-day lifecycle** — checkpoint data auto-expires after 7 days. If the re-run happens after expiry, there's nothing to resume from.

### Stale Checkpoint Data

If a Glue job resumes from an outdated checkpoint (e.g., schema changed since last run):

- **Delete the checkpoint manually**:
  ```bash
  aws s3 rm s3://adi-checkpoints-$ACCOUNT-$REGION/agentic-data-ingestion-YOUR-PIPELINE-ID/ --recursive
  ```
- Re-run the pipeline — it will execute the full transform and write a fresh checkpoint.

Checkpoints are keyed by `{JOB_NAME}/{run_id}/`, so different runs don't overwrite each other. Stale checkpoints only affect re-runs of the same job.

### Checkpoint Bucket Permissions

The per-pipeline Glue IAM role needs S3 access to the checkpoint bucket. If you see `AccessDenied` errors during checkpoint read/write:

```bash
# Check the Glue job's IAM role
aws glue get-job --job-name "agentic-data-ingestion-YOUR-PIPELINE-ID" --region us-west-2 \
  --query "Job.Role"

# Verify the role has checkpoint bucket access
aws iam get-role-policy --role-name ROLE_NAME --policy-name GlueJobPolicy
```

The `create_pipeline_specific_role()` function in the `create_glue_pipeline` Lambda should include the checkpoint bucket ARN in the S3 policy. If missing, redeploy the Compute stack.


---

## Large File Handling

### File Validation Passes But Glue Job Fails to Read File

Files over 50MB (or Parquet/Avro/ORC of any size) use the lightweight passthrough path — file validation does a HEAD request only and returns `source_s3_uri` pointing to the original file. The Glue job reads the file directly.

**Check `source_format` propagation:**
```bash
# Verify file_validation returned source_format
aws dynamodb get-item --table-name agentic_data_ingestion_state \
  --key '{"run_id":{"S":"YOUR_RUN_ID"},"step_name":{"S":"file_validation"}}' \
  --projection-expression "output_data" \
  --region us-west-2
```

If `source_format` is missing, the deployed `file_validation` Lambda is not writing it — see [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

### MAX_CONVERSION_SIZE_BYTES Tuning

The threshold for legacy conversion vs lightweight passthrough defaults to 50MB. To change it:

```bash
# Check current value
aws lambda get-function-configuration --function-name agentic_data_ingestion_file_validation \
  --query "Environment.Variables.MAX_CONVERSION_SIZE_BYTES" --region us-west-2

# Override in CDK: deploy-cdk/lib/compute-stack.ts
# environment: { MAX_CONVERSION_SIZE_BYTES: '104857600' }  // 100MB
```

Files under this threshold still get JSON conversion (preserves dashboard preview). Files over this threshold are passed through to Glue directly.

### Large CSV Files (>50MB) — No Dashboard Preview

When a CSV file exceeds `MAX_CONVERSION_SIZE_BYTES`, it skips JSON conversion. This means:
- No converted JSON file is written to the processed bucket
- Dashboard preview of the data is not available
- The Glue job reads the CSV directly using `spark.read.csv()`

This is expected behavior — the tradeoff is reliability over preview for large files.

## Parquet/Avro/ORC Troubleshooting (Phase 9)

### Parquet File Rejected as "Unsupported"

If you see `Unsupported file format` for a `.parquet` file, the file_validation Lambda may be an older version that doesn't have `detect_format()`. Redeploy the Compute stack.

### Schema Extraction Fails for Parquet

Parquet schema extraction uses PyArrow to read file metadata (footer only). Common issues:

1. **PyArrow layer missing**: The schema_extract Lambda needs the PyArrow layer. Check:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_schema_extract \
     --query "Layers" --region us-west-2
   ```
   Should show **exactly 2 layers — Strands and PyArrow.** Two is correct; do
   **not** add a third.

   The ADOT layer is deliberately excluded from this one function
   (`compute-stack.ts:312` — *"combined ADOT+Strands+PyArrow exceeds 250MB
   limit"*). The 250 MB combined-unzipped limit is binding here, measured in
   by unzipping both layers and the function code and summing them: Strands
   123.82 MiB + PyArrow 123.98 MiB + 0.06 MiB of function code = **247.86 MiB of
   250 MiB, leaving 2.14 MiB** — under 1% of the limit. Restoring ADOT adds
   roughly 50 MB, so `CreateFunction` refuses and the **deploy** fails; there is
   no graceful degradation. Schema extraction gets structured logging via the
   Powertools Logger instead.

   `deploy-cdk/test/p46-schema-extract-layers.test.ts` asserts the attachment is
   exactly `[strandsLayer, pyarrowLayer]`, in that order, and that ADOT is absent.
   If you see fewer than 2, or a missing PyArrow layer, redeploy the Compute
   stack — that is the failure this check is for.

2. **Parquet file is corrupted**: PyArrow will raise `ArrowInvalid` if the file footer is malformed. Check the source file:
   ```bash
   # Download and verify locally
   aws s3 cp s3://your-bucket/your-file.parquet /tmp/test.parquet
   python3 -c "import pyarrow.parquet as pq; print(pq.read_metadata('/tmp/test.parquet'))"
   ```

3. **Lambda memory**: Schema extraction Lambda is configured at 512MB. PyArrow metadata reads are lightweight, but if the Parquet file has thousands of columns, memory usage may spike. Check CloudWatch metrics for `MaxMemoryUsed`.

### Avro/ORC Schema Extraction

Avro and ORC schemas are read from **file metadata**, not sampled from records. Each format has its own extractor, and neither samples data rows:

| Format | How the schema is read |
|--------|------------------------|
| Avro | One bounded Range GET (64 KB default) of the Object Container File **header**, whose metadata map carries the writer schema. The header parser is hand-written in `lambda_function.py` — `fastavro` does not fit the layer budget (see `layer-measurement.md`). |
| ORC | The footer, via a seekable `pyarrow.fs.S3FileSystem` handle. ORC keeps its schema at the **end** of the file, so a forward-only ranged GET cannot reach it. The object is never downloaded. |

Both extractors **raise** rather than returning an empty schema, so an unreadable file fails the run at `schema_extract` — you will not get a green run declaring zero fields. Neither reads data rows, so "the file has no records" is not a cause of extraction failure.

If extraction fails:
- **Externally compressed container** (`data.avro.gz`, `data.orc.zst`): both extractors detect the gzip/bzip2/zstd magic bytes and say so. Upload the container uncompressed — Avro and ORC already compress their data blocks internally, and wrapping the whole file hides the header or footer the schema is read from. Note that format detection strips the `.gz`/`.bz2`/`.snappy`/`.zst` suffix, so renaming the file does not help.
- **Truncated or corrupt container**: Avro reports which part of the header failed; ORC surfaces PyArrow's own message (`Not an ORC file`, `Invalid ORC postscript length`).
- **PyArrow layer missing** (ORC only): raises `ImportError` naming the layer. See the layer check above.
- Check Lambda logs for parsing errors:
  ```bash
  aws logs tail /aws/lambda/agentic_data_ingestion_schema_extract --since 10m --region us-west-2 --format short
  ```

### Glue Job Fails Reading Parquet/Avro/ORC

The PySpark templates have `source_format` conditional read blocks. If the Glue job fails with a read error:

1. **Check `source_format` in DefaultArguments**: The Glue job should have the correct format. If it defaults to JSON reading, `source_format` wasn't propagated:
   ```bash
   aws glue get-job --job-name "agentic-data-ingestion-YOUR-PIPELINE-ID" --region us-west-2 \
     --query "Job.DefaultArguments"
   ```

2. **The `else` arm ran, meaning `source_format` never arrived**: all four S3-source templates carry explicit `avro` and `orc` read branches — `spark.read.format("avro").load(source_path)` and `spark.read.orc(source_path)`, at `glue_job_s3_tables.py.j2:75-78`, `glue_job_glue_catalog.py.j2:54-57`, `glue_job_s3_parquet.py.j2:56-59` and `glue_job_jdbc.py.j2:54-57`. The `{% else %}` arm is the JSON reader, and it is reached only when the value rendered into the template matched none of `csv`/`parquet`/`avro`/`orc`. So a job reading Avro or ORC bytes as JSON is a **propagation** failure, not a missing feature: check item 1 above, then regenerate the code so the template re-renders with the right `source_format`.

## Write Mode / Merge Troubleshooting

### Merge Key Column Not Found

If a Glue job fails with `AnalysisException: cannot resolve 'merge_key_name'` during `MERGE INTO`:

1. **Check the merge key exists in both source and target schemas**:
   ```bash
   # Check source schema
   aws dynamodb get-item --table-name agentic_data_ingestion_state \
     --key '{"run_id":{"S":"YOUR_RUN_ID"},"step_name":{"S":"schema_extract"}}' \
     --projection-expression "output_data" --region us-west-2
   ```

2. **Check the pipeline's merge_key config**:
   ```bash
   aws dynamodb get-item --table-name agentic_data_ingestion_pipelines \
     --key '{"pipeline_id":{"S":"YOUR_PIPELINE_ID"}}' \
     --projection-expression "target_config.merge_key" --region us-west-2
   ```

3. **Common causes**:
   - Column name is case-sensitive — `Customer_ID` ≠ `customer_id`
   - Column was renamed during schema mapping but `merge_key` still references the source name
   - Column exists in source but was dropped by the transform block

### Merge Mode on Non-Iceberg Target

`write_mode=merge` is only supported for S3 Tables (Iceberg) targets. The dashboard API rejects merge mode for other targets at pipeline creation time. If you see `ValueError: merge mode is only supported for S3 Tables (Iceberg) targets`:

- Change `write_mode` to `append` or `overwrite`
- Or change the target type to S3 Tables

### Iceberg MERGE INTO Limitations

- **Requires a unique key**: `MERGE INTO` matches on `merge_key`. If the key has duplicates in the source data, the merge may produce unexpected results (multiple matches per target row).
- **Schema evolution**: If the source has new columns not in the target Iceberg table, `UPDATE SET *` and `INSERT *` will fail. Ensure schema mapping handles new columns before merge.
- **Partition evolution**: Iceberg supports partition evolution, but `MERGE INTO` with partition changes may trigger full table rewrites for affected partitions.
- **S3 Tables catalog only**: The `MERGE INTO` SQL uses the `s3tablesbucket` Spark catalog. It won't work with the Glue Data Catalog or JDBC targets.

### Append Mode Creates Duplicates

This is expected behavior. `write_mode=append` adds all source records without deduplication. If you run the same file twice, you get 2× the records. Use `write_mode=merge` with an appropriate `merge_key` for idempotent loads.

### Write Mode Not Taking Effect

If the Glue job still uses `createOrReplace()` despite setting `write_mode=append`:

1. **Check the generated code** — `write_mode` is baked in at code generation time, not at Glue runtime:
   ```bash
   # Download the generated PySpark script
   aws s3 cp s3://adi-generated-code-ACCOUNT-REGION/PIPELINE_ID/code/latest/glue_job.py /tmp/
   grep -n "append\|merge\|overwrite\|createOrReplace" /tmp/glue_job.py
   ```

2. **Regenerate code** — if the pipeline was created before Phase 10, the generated code won't have write mode logic. Re-run the pipeline to trigger code regeneration with the updated templates.

3. **Check whether the deployed stack is current** — write-mode logic is absent from an older
   `create_code`: see [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

---

## Rerun Mapping Troubleshooting

### Rerun Mapping Times Out

The `POST /api/rerun-mapping` endpoint invokes the schema mapping Lambda asynchronously (`InvocationType: Event`) to avoid the 25-second API Gateway timeout. If the mapping doesn't appear:

1. **Check the schema mapping Lambda logs** — the async invocation runs independently:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_schema_mapping --since 10m --region us-west-2 --format short
   ```

2. **Check Lambda invoke permissions** — the dashboard API Lambda needs `lambda:InvokeFunction` on the schema mapping Lambda.

3. **Poll for results** — the dashboard polls the pipeline detail endpoint. Check that `mapping_context` is being stored on the pipeline record.

### Mapping Context Not Persisting

Mapping context (user edits from the Schema page) is stored on the pipeline record in DynamoDB. If edits are lost:

1. **Check the PUT endpoint** — `PUT /api/pipelines/{id}/mappings` updates the mapping directly on the pipeline record:
   ```bash
   aws dynamodb get-item --table-name agentic_data_ingestion_pipelines \
     --key '{"pipeline_id":{"S":"YOUR_PIPELINE_ID"}}' \
     --projection-expression "mapping_context" \
     --region us-west-2
   ```

2. **Check for `edit_pipeline` DynamoDB errors** — the `ExpressionAttributeNames` fix (empty dict guard) prevents update failures when no reserved-word attributes are present. If you see `ValidationException`, redeploy the Compute stack.

### Mapping Memory Only Stores on Approval

By design, mapping memory entries are created only when a mapping is approved, not when it's generated. This prevents low-confidence or rejected mappings from polluting the memory store. If you expect historical context but Memory returns empty results, check that previous mappings were actually approved (not just generated).

### Ghost Approvals After Force Remap

When `force_remap=true` is used (rerun mapping), approval creation is skipped. If stale approval records exist from a previous run, they are cleaned up automatically. Check for ghost approvals:

```bash
aws dynamodb scan --table-name agentic_data_ingestion_approvals \
  --filter-expression "pipeline_id = :pid AND #s = :pending" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":pid":{"S":"YOUR_PIPELINE_ID"},":pending":{"S":"pending"}}' \
  --region us-west-2
```

### A Rerun Changed an Old Run, or a Rejected Mapping Came Back

A rerun writes its mapping onto the `schema_mapping` row of whichever run last extracted a schema,
which is often a run that has already finished. Two rules keep that from rewriting the run:

- **The write is an annotation.** It stamps `annotated_at`, and the execution updater does not
  derive a run status from a row whose latest write is an annotation. A finished run stays
  finished. If an old run reads RUNNING after a rerun, the deployed Compute stack predates this
  rule.
- **The rerun keeps an undecided or refused status.** A row at `waiting_approval` stays there, and
  a row at `rejected` stays rejected when the rerun reproduces the same content (`mapping_id` is a
  content hash). New content is written as `success`, so the next run offers it, and that run
  still stops at the approval gate.

A rejection on the dashboard sets the run's own row to `rejected`. A run that reused an earlier
mapping has no row of its own, so the rejection instead stamps `reuse_revoked_at` on every
approved row of the pipeline that holds the refused mapping. Those rows keep their status, because
they record how earlier runs went. Schema compare, code generation and the rename-manifest lookup
all skip a revoked row (`shared/mapping_reuse.py`).

To see why a mapping was or was not reused, read the pipeline's mapping rows:

```bash
aws dynamodb query --table-name agentic_data_ingestion_state --index-name PipelineIndex \
  --key-condition-expression "pipeline_id = :p" \
  --filter-expression "step_name = :s" \
  --expression-attribute-values '{":p":{"S":"YOUR_PIPELINE_ID"},":s":{"S":"schema_mapping"}}' \
  --projection-expression "run_id, mapping_id, #st, reuse_revoked_at, annotated_at" \
  --expression-attribute-names '{"#st":"status"}' \
  --region us-west-2
```

A row is a reuse candidate only when `status` is `success` and `reuse_revoked_at` is absent.

### Code Generation Uses Stale Mappings

The `create_code` Lambda checks for newer mappings from reruns before generating code. If code still uses old mappings:

1. **Check the mapping timestamp** — the Lambda compares the mapping's `updated_at` with the run's start time.
2. **Check whether the deployed stack is current** — the newer-mapping check exists only in a
   current build: see [Your deployed stack is behind this repo](#your-deployed-stack-is-behind-this-repo).

---

## Code Interpreter Troubleshooting (Phase 12)

### Code Interpreter Session Fails to Start

If the test Lambda fails with `AccessDeniedException` on `start_code_interpreter_session`:

1. **Check IAM permissions** — the `codeTestingRole` needs Code Interpreter permissions:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_test_code \
     --query "Role" --region us-west-2

   # Verify the role has bedrock-agentcore:StartCodeInterpreterSession
   aws iam list-role-policies --role-name <role-name>
   ```

2. **Check region** — Code Interpreter must be available in the Lambda's region:
   ```bash
   aws bedrock-agentcore list-code-interpreter-sessions \
     --code-interpreter-identifier 'aws.codeinterpreter.v1' --region us-west-2
   ```

3. **Redeploy** if permissions are missing:
   ```bash
   cd deploy-cdk && npx cdk deploy AgenticDataIngestion-Foundation-dev --require-approval never --exclusively
   ```

### PySpark-to-Pandas Conversion Refuses

What runs in the Code Interpreter is a **proxy** for the Glue job, not the Glue job: a pandas approximation of the transform over generated sample data. A block that converts and executes cleanly has not been run against Spark.

Conversion is all-or-nothing. A construct the converter cannot express raises `ConversionError(construct, line, line_number)` from `convert()`, and the transform never runs. Nothing is ever commented out, dropped, or partially translated.

If the test report shows a `not_verified_by_proxy` check, or the transform execution fails:

1. **Read the construct and the line out of the report** — the refusal message names both:
   ```
   unconvertible PySpark construct '<construct>' on line <n>: <line>
   ```
   For a chain spread over several physical lines, `<n>` is the line the construct's own name sits on, not the line the statement starts on.

2. **Check the transform block** — download the generated code and look at that line:
   ```bash
   aws s3 cp s3://adi-generated-code-$ACCOUNT-$REGION/$PIPELINE_ID/code/latest/glue_job.py /tmp/
   grep -A 50 "AI-Generated Transform Block" /tmp/glue_job.py
   ```

3. **Constructs that refuse** — every name here is one the converter names in a refusal:
   - Aggregations and multi-frame operations: `groupBy`, `agg`, `join`, `union`
   - Window functions: `Window`, `row_number`, `rank`, `lag`, `lead`, `over`
   - User-defined functions: `udf` (including a `@udf`-decorated function defined in the block)
   - Array expansion: `explode`, `posexplode`
   - Spark-only frame methods the scaffolds use around the block: `count`, `collect`, `writeTo`, `cache`, `unpersist`, `createOrReplaceTempView`
   - Functions the generation prompt offers but the converter has no rewrite for: `initcap`, `substring`, `coalesce`, `length`, `split`, `expr`, `spark_count`
   - Functions the scaffold templates import, so the model may reach for them: `array_contains`, `spark_max`

   This is a shortlist. The full vocabulary is in [`pyspark_templates/README.md`](../pyspark_templates/README.md#what-the-pandas-proxy-converts), which lists the converter's own frozensets. Do not read an absence from either list as "converts": any bare function name the converter has no rewrite for refuses, named or not. An unnamed one refuses under its own name and is classified `not_yet_expressible`, which is the reason that says the gap is in the checker.

4. **Shapes that refuse**, independent of which constructs they use:
   - A chain that mixes frame operations, e.g. `.withColumnRenamed(...).withColumn(...)`. It refuses at the first step that cannot compose, so the line number points at that step. Splitting it into one statement per line converts
   - Column literals in single quotes. The generation prompt emits double quotes, so a block that does not is one this checker has never verified

   Chained `when(...).when(...).otherwise(...)` **does** convert, to nested `np.where` with every branch preserved in order. So does any number of chained `withColumn` calls, optionally closed by one `select`.

5. **What a refusal means for the run** — it is classified as *not verified by the pandas proxy*, not as a failed execution. `code_interpreter_runner.classify_conversion_refusal` splits two reasons apart:
   - `permanently_inexpressible` — no single-frame pandas equivalent exists (`groupBy`, `agg`, joins, window functions, UDFs). There is nothing to fix in the generated PySpark; the proxy will never verify it.
   - `not_yet_expressible` — the converter cannot express the construct yet. That is a defect in the checker, not in the generated code. `initcap` reads this way, and is the example worth knowing: it has a single-frame pandas equivalent (`.str.title()`), so it is unimplemented rather than inexpressible. It used to be listed above, which told an operator there was nothing to fix about a construct that can be fixed.

   The refusal is reported as its own `not_verified_by_proxy` check and is partitioned out of the pass/fail derivation, so a refusal alone does not fail the step. A run in which nothing was verifiable still reports `failed` — it proved nothing.

### Glue Verification Reports `source_read_failed`

**Symptom.** The test report carries a harness non-verification with reason `source_read_failed`, and the dashboard shows *"not verified — the run's source object could not be read (nothing to fix in the code; check the object and the session's read access)"*. There is no execution status and no record count.

**What it means.** The Glue executor was configured for a real-source sample (`VERIFICATION_FRAME_MODE=sample`), the frame-build statement tried to read the pipeline's own source object, and that read did not succeed. The session stops at the first failed statement, so **the transform was never submitted**. This is not a code defect and not a transform failure — there is nothing in the generated PySpark to fix.

**Likeliest cause: the Compute or AgentCore stack was deployed before Foundation.** The two halves of this feature ship in different stacks. Foundation carries the S3 `GetObject` / `ListBucket` grant on the source-files bucket for the verification session role (`deploy-cdk/lib/foundation-stack.ts`, around lines 877-885); Compute and AgentCore carry the caller that selects `sample`. Deploy a caller first and every S3-upload verification selects `sample`, gets `AccessDenied` on `HeadObject`, and abstains.

That window is **safe but visible**: the abstention is honest, so verification *coverage* degrades rather than a verdict being corrupted, and the report says "not verified" rather than showing a pass nobody earned. It **self-heals when Foundation lands** — no reprocessing, no stuck state. Every test passes in either deploy order, which is why this is worth checking first.

Check in this order:

1. **Confirm Foundation is deployed and current:**
   ```bash
   aws cloudformation describe-stacks --stack-name AgenticDataIngestion-Foundation-dev \
     --query "Stacks[0].[StackStatus,LastUpdatedTime]" --region us-west-2
   ```

2. **Check the session role's policy actually carries both buckets in both ARN forms** — `s3:ListBucket` authorizes against the bucket ARN while `s3:GetObject` authorizes against the object ARN, so four resources are expected across `adi-source-files-*` and `adi-processed-files-*`:
   ```bash
   aws iam get-role-policy --role-name AgenticDataIngestion-VerificationSession-Role \
     --policy-name <inline-policy-name> --region us-west-2
   # or list them first:
   aws iam list-role-policies --role-name AgenticDataIngestion-VerificationSession-Role
   ```

3. **Confirm the object exists and its extension resolves to a reader.** The reported reason names the URI. Recognised extensions are `csv`, `tsv`, `txt`, `json`, `jsonl`, `ndjson`, `parquet`, `pq`, `avro`, `orc`, optionally under `.gz` / `.bz2` / `.snappy` / `.zst`. An unrecognised extension or an extension-less prefix does **not** produce this reason — it falls back to a synthetic frame instead.
   ```bash
   aws s3 ls s3://adi-source-files-$ACCOUNT-$REGION/$PIPELINE_ID/ --region us-west-2
   ```

4. **Check whether the statement hit the deadline rather than a permission error.** Both doors lead to `source_read_failed`; the `error_details` sentence distinguishes them, naming either the observed Glue failure or the 120s statement deadline.

**The correct deploy order** is Foundation **first**, then Compute **and** AgentCore together:

```bash
cd deploy-cdk
npx cdk deploy AgenticDataIngestion-Foundation-dev --require-approval never
npx cdk deploy AgenticDataIngestion-Compute-dev AgenticDataIngestion-AgentCore-dev --require-approval never
```

Compute and AgentCore go together because the AgentCore container copies the `test_code` Lambda directory wholesale, and `USE_AGENTCORE_RUNTIME: 'true'` makes AgentCore's copy the live path. Deploying one leaves the two paths reading different frame modes.

**Related, and benign — do not chase it.** A **JDBC** pipeline whose report says a real-source sample was requested and could not be read (`source_uri_not_s3`) is expected and correct. A verification session holds no database credentials, so a JDBC source can never be sampled; that run built a synthetic frame, is fully verified against it, and carries **no** `not_verified_reason` at all. It is a different report line from this section's symptom.

### Code Interpreter Returns Empty Results

If the test report shows `record_count: 0` or all metrics at 0:

1. **Check session output** — look for `GENERATED:` and `VALIDATION:` markers in Lambda logs:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_test_code --since 30m --region us-west-2 --format short \
     | grep -i "GENERATED\|VALIDATION\|error\|exception"
   ```

2. **Common causes**:
   - `df_transformed` not defined — the pandas transform didn't produce the expected variable name
   - Import error in the converted code — a PySpark function wasn't properly converted
   - Session timeout (300s) — complex transforms on large test data may exceed the limit

3. **Graceful fallback**: If Code Interpreter is completely unavailable, the runner returns empty results and the pipeline continues with static analysis only.

### Code Interpreter Session Not Cleaned Up

Sessions should always be stopped in the `finally` block. If orphaned sessions accumulate:

```bash
# List active sessions
aws bedrock-agentcore list-code-interpreter-sessions \
  --code-interpreter-identifier 'aws.codeinterpreter.v1' --region us-west-2

# Manually stop a session
aws bedrock-agentcore stop-code-interpreter-session \
  --code-interpreter-identifier 'aws.codeinterpreter.v1' \
  --session-id <session-id> --region us-west-2
```

Sessions auto-expire after 300 seconds (5 minutes) even if not explicitly stopped.

---

## AgentCore Memory Troubleshooting

### Memory Not Returning Historical Mappings

If schema mapping doesn't include historical context:

1. **Check `MEMORY_ENABLED`** — must be `true`:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_schema_mapping \
     --query "Environment.Variables.MEMORY_ENABLED" --region us-west-2
   ```

2. **Check `MAPPING_MEMORY_ID`** — must be set to a valid Memory resource ID:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_schema_mapping \
     --query "Environment.Variables.MAPPING_MEMORY_ID" --region us-west-2
   ```

3. **Check Memory has data** — query for a known field:
   ```bash
   # Use the seed script to populate Memory with historical data
   python scripts/seed_mapping_memory.py --memory-id <memory-id> --region us-west-2
   ```

4. **Check Lambda logs** for Memory query results:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_schema_mapping --since 30m --region us-west-2 --format short \
     | grep -i "memory\|similar_mappings\|historical"
   ```

### Memory Approval Feedback Not Stored

If approved/rejected mappings aren't being stored in Memory:

1. **Check the approval handler logs**:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_dashboard_api --since 30m --region us-west-2 --format short \
     | grep -i "memory\|store_approval\|feedback"
   ```

2. **Check `approval_type`** — Memory feedback is only stored for `schema_mapping` approvals, not `code_generation` or `infrastructure`.

3. **Memory calls are wrapped in try/except** — failures are logged as warnings but don't block the approval flow. Look for `"Failed to store Memory"` in logs.

### Disabling Memory

Set `MEMORY_ENABLED=false` on the schema mapping Lambda and approval handler Lambda:

```bash
aws lambda update-function-configuration \
  --function-name agentic_data_ingestion_schema_mapping \
  --environment "Variables={MEMORY_ENABLED=false}" \
  --region us-west-2
```

All `MappingMemoryClient` methods return empty results when disabled — no API calls are made.

---

## MCP Issues

### AgentCore Endpoint Not Responding

```bash
# Check if endpoint is configured
aws lambda get-function-configuration --function-name agentic_data_ingestion_create_code \
  --query "Environment.Variables.AGENTCORE_ENDPOINT" --region us-west-2
```

If empty, the AgentCore stack may not have deployed successfully. Check:
```bash
aws cloudformation describe-stacks --stack-name AgenticDataIngestion-AgentCore-dev --region us-west-2
```

### MCP Fallback Mode

Lambda logs will show `AGENTCORE_ENDPOINT not configured` or `Failed to initialize Strands Agent with MCP`. The Lambda falls back to direct Bedrock calls without MCP tools. This is expected if AgentCore is not deployed.

## Lambda Errors

### Import Errors

`ModuleNotFoundError` means the Strands layer is missing or outdated. Redeploy:
```bash
cd deploy-cdk && npx cdk deploy AgenticDataIngestion-Compute-dev --require-approval never --exclusively
```

### Timeout Errors

AI-powered steps (code generation, testing) can take 5-15 minutes. Check the Lambda timeout in CDK:
- Code generation: 900s (15 min)
- Code testing: 900s (15 min)
- Other steps: 300s (5 min)

### DynamoDB Errors

`ResourceNotFoundException` means a table name doesn't match. Check Lambda env vars:
```bash
aws lambda get-function-configuration --function-name agentic_data_ingestion_dashboard_api \
  --query "Environment.Variables" --region us-west-2
```

## AgentCore Runtime Troubleshooting

### Proxy Lambda Returns Timeout

If the proxy Lambda times out waiting for the AgentCore Runtime response:

1. **Check the Runtime endpoint** — verify it's configured and reachable:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_create_code \
     --query "Environment.Variables.AGENTCORE_CREATE_CODE_ENDPOINT" --region us-west-2
   ```

2. **Check Runtime container logs**:
   ```bash
   aws logs tail /aws/bedrock-agentcore/runtime/<runtime-id> --since 10m --region us-west-2 --format short
   ```

3. **Check proxy Lambda timeout** — should be at least 120s (proxy waits for HTTP response):
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_create_code \
     --query "Timeout" --region us-west-2
   ```

### Falling Back to Inline Lambda Execution

When `USE_AGENTCORE_RUNTIME=false` or the endpoint is not configured, the Lambda executes the original inline handler code instead of proxying to AgentCore Runtime. The default is `true` in `compute-stack.ts`.

Check which mode is active:
```bash
aws lambda get-function-configuration --function-name agentic_data_ingestion_create_code \
  --query "Environment.Variables.USE_AGENTCORE_RUNTIME" --region us-west-2
```

### Runtime Container Not Starting

If the AgentCore Runtime container fails to start:

1. **Check ECR image** — verify the image was pushed successfully:
   ```bash
   aws ecr describe-images --repository-name agentic-data-ingestion-agentcore \
     --query "imageDetails[*].{tag:imageTags,pushed:imagePushedAt}" --region us-west-2
   ```

2. **Check container health** — the `BedrockAgentCoreApp` exposes a `/ping` endpoint:
   ```bash
   # From within the VPC or via port-forward
   curl http://localhost:8080/ping
   ```

3. **Check Dockerfile** — ensure `requirements.txt` includes `bedrock-agentcore`, `strands-agents`, and all shared module dependencies.

### SigV4 Authentication Errors

If the proxy Lambda gets 403 from the Runtime endpoint:

1. **Check IAM permissions** — the proxy Lambda role needs `bedrock-agentcore:InvokeRuntime`:
   ```bash
   aws iam list-role-policies --role-name <proxy-lambda-role>
   ```

2. **Check region** — SigV4 signing must use the same region as the Runtime endpoint.

3. **Check the `agentcore_proxy.py` implementation** — it uses `botocore.auth.SigV4Auth` for request signing.

## WebSocket Real-Time Updates

### Connection Failures

If the dashboard isn't receiving real-time updates:

1. **Check the WebSocket URL source** — the deployed dashboard reads the WS URL from the runtime `config.json` (`getConfig().wsUrl`); `VITE_WS_URL` only applies to local dev. For a deployed environment, confirm `config.json` (served from the CloudFront/S3 root) contains a `wsUrl` value. For local dev, verify `VITE_WS_URL` is set in `dashboard-react/.env`:
   ```bash
   grep VITE_WS_URL dashboard-react/.env
   # Should be: VITE_WS_URL=wss://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com/prod
   ```

2. **Check API Gateway deployment** — the WebSocket API must be deployed to the `prod` stage:
   ```bash
   aws apigatewayv2 get-apis --query "Items[?Name=='agentic-data-ingestion-ws-api']" --region us-west-2
   ```

3. **Check Lambda logs** for the connect handler:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_ws_connect --since 5m --region us-west-2 --format short
   ```

4. **Browser console** — open DevTools and look for WebSocket connection errors. The `useWebSocket` hook logs connection state changes.

### Stale Connections

Stale connections occur when a browser tab closes without a clean `$disconnect` (e.g., crash, network drop). These are handled automatically:

- **TTL cleanup**: Connection records have a 24-hour TTL in DynamoDB. Expired records are deleted automatically.
- **GoneException handling**: When the broadcast Lambda tries to post to a stale connection, API Gateway returns `GoneException` (410). The Lambda deletes the stale record immediately.

To manually check for stale connections:
```bash
aws dynamodb scan --table-name agentic_data_ingestion_ws_connections --max-items 20 --region us-west-2
```

### Fallback to Polling

The dashboard falls back to 10-second polling when WebSocket is unavailable. This happens when:

- No WebSocket URL is available — the deployed app finds no `wsUrl` in runtime `config.json` (or, in local dev, `VITE_WS_URL` is unset) (immediate fallback)
- WebSocket connection fails after 3 retries with exponential backoff (1s, 2s, 4s)

Check the browser console for messages like "WebSocket fallback to polling" or connection retry attempts. The `useWebSocket` hook's `isFallbackPolling` state will be `true`.

To force polling mode for debugging, temporarily remove `VITE_WS_URL` from the `.env` file and restart the dev server.

### Broadcast Lambda Not Triggering

If WebSocket connects but no updates arrive:

1. **Check DynamoDB Stream** on `agentic_data_ingestion_executions` table:
   ```bash
   aws dynamodb describe-table --table-name agentic_data_ingestion_executions \
     --query "Table.StreamSpecification" --region us-west-2
   ```
   Stream should show `StreamEnabled: true` with `StreamViewType: NEW_IMAGE`.

2. **Check broadcast Lambda logs**:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_ws_broadcast --since 10m --region us-west-2 --format short
   ```

3. **Check event source mapping**:
   ```bash
   aws lambda list-event-source-mappings \
     --function-name agentic_data_ingestion_ws_broadcast --region us-west-2
   ```

## RDS Database Targets Troubleshooting

### RDS Instance Not Appearing in Wizard

The RDS Database form in the Create Pipeline Wizard discovers instances via `GET /api/resources/rds/instances`. If no instances appear:

1. **Check IAM permissions** — the dashboard API Lambda role needs `rds:DescribeDBInstances`, `rds:DescribeDBClusters`:
   ```bash
   aws lambda get-function-configuration --function-name agentic_data_ingestion_dashboard_api \
     --query "Role" --region us-west-2
   ```

2. **Check the handler is registered** — verify the `/api/resources/rds/*` routes are in the router:
   ```bash
   aws logs tail /aws/lambda/agentic_data_ingestion_dashboard_api --since 5m --region us-west-2 --format short \
     | grep "rds"
   ```

3. **Check region** — RDS instances are region-specific. The Lambda discovers instances in its own region only.

### Glue JDBC Connection Auto-Creation Fails

When using "Select RDS Instance" mode, the dashboard API auto-creates a Glue JDBC connection from the RDS endpoint. If this fails:

1. **Check Glue permissions** — the Lambda role needs `glue:CreateConnection`, `glue:GetConnection`:
   ```bash
   aws iam list-attached-role-policies --role-name <dashboard-api-role>
   ```

2. **Check Secrets Manager permissions** — the **Glue job**, not the `create_glue_pipeline` Lambda, reads the target secret, at run time. The pipeline's execution role (`AgenticDataIngestion-<pipeline_id>-ExecutionRole`) must hold `secretsmanager:GetSecretValue` on that secret; `create_glue_pipeline` grants it when it first creates the role and never reads the secret itself. An `AccessDeniedException` on `GetSecretValue` in the job run usually means a role created before the grant was right; see [JDBC target secret could not be resolved](#jdbc-target-secret-could-not-be-resolved).

3. **Check the JDBC driver** — supported drivers: MySQL (`com.mysql.cj.jdbc.Driver`), PostgreSQL (`org.postgresql.Driver`), MariaDB (`org.mariadb.jdbc.Driver`), Oracle (`oracle.jdbc.OracleDriver`), SQL Server (`com.microsoft.sqlserver.jdbc.SQLServerDriver`).

### RDS Secret Not Found

The JDBC target's secret is read by the Glue job at run time, in its write block. If the run fails there with `ResourceNotFoundException` from Secrets Manager, or with `KeyError: 'username'` / `KeyError: 'password'`, check the secret the job was given:

```bash
# Verify the secret exists and has username/password fields (prints key NAMES only)
aws secretsmanager get-secret-value --secret-id <secret_arn> --region us-west-2 \
  --query "SecretString" | python3 -c "import sys,json; d=json.loads(json.load(sys.stdin)); print(list(d.keys()))"
```

The secret must contain `username` and `password` keys. RDS-managed secrets (created by RDS) follow this format automatically. The reference the job uses is its `--secret_arn` argument (`--target_secret_arn` for a JDBC-source pipeline).

### JDBC target secret could not be resolved

A JDBC target's credentials are referenced **only** by a Secrets Manager secret. Plaintext user/password never appear in Glue job arguments or connection properties (standing invariant). Before this was enforced on the target side, a target connection carrying inline `USERNAME` / `PASSWORD` was accepted, and `create_glue_pipeline` copied the credentials into the job's `--jdbc_user` / `--jdbc_password` arguments, where they were visible in the Glue console, CloudTrail, job-run history and the Lambda's own INFO log. That path is gone, and these are the errors that replace it.

**Symptom 1: the run fails at Glue-job creation** (the `create_glue_pipeline` step) with one of:

```
ValueError: The JDBC target's Glue connection '<name>' carries no SECRET_ID, so its credentials cannot be
referenced through Secrets Manager, and plaintext credentials are not passed to the Glue job. ...
```
```
ValueError: A JDBC target's credentials must be referenced through Secrets Manager: set secret_arn on the
target configuration, or name a Glue connection (glue_connection_name / jdbc_connection_name) whose
ConnectionProperties carry SECRET_ID. ...
```

The pipeline record has no `target_config.secret_arn`, and its connection has no `SECRET_ID` (or no connection is named). `job_arguments.resolve_jdbc_target_secret` raises; no job is created or updated.

**Symptom 2: the dashboard API returns HTTP 400** on create or edit with one of:

- `target_config.secret_arn is required, but the Glue Connection '<name>' carries no SECRET_ID property; the JDBC target job reads credentials from Secrets Manager at runtime, ...` — a `glue_connection`-mode target whose connection has no `SECRET_ID`. The source side has always refused this; the target side now does too.
- `Plaintext credentials are not accepted (target_config.jdbc_user, target_config.jdbc_password). ...` — the request carried either key, whatever its value. The message names the keys only. Remove them and send `target_config.secret_arn` (the ARN of a secret holding `username` and `password`) or use a connection whose `ConnectionProperties` carry `SECRET_ID`.

**Fix the connection.** Set `SECRET_ID` to the secret's ARN **and remove the inline `USERNAME` / `PASSWORD` in the same `UpdateConnection` call**. Glue rejects `SECRET_ID` alongside an inline pair with "Validation for connection properties failed", so adding it alone fails. `UpdateConnection` replaces the whole connection definition, so carry the existing `PhysicalConnectionRequirements` over, or the job loses its VPC routing:

```bash
CONN=<connection-name>
# Current definition, password hidden. Note the URL, SSL flag and networking.
aws glue get-connection --name "$CONN" --hide-password --region us-west-2 \
  --query 'Connection.{Props: ConnectionProperties, Net: PhysicalConnectionRequirements}'

# conn.json — Name, type, URL + SECRET_ID (no USERNAME / PASSWORD), networking copied from above:
# {
#   "Name": "<connection-name>",
#   "ConnectionType": "JDBC",
#   "ConnectionProperties": {
#     "JDBC_CONNECTION_URL": "jdbc:postgresql://host:5432/db",
#     "JDBC_ENFORCE_SSL": "true",
#     "SECRET_ID": "arn:aws:secretsmanager:us-west-2:<account>:secret:<name>-AbCdEf"
#   },
#   "PhysicalConnectionRequirements": { "SubnetId": "...", "SecurityGroupIdList": ["..."], "AvailabilityZone": "..." }
# }
aws glue update-connection --name "$CONN" --connection-input file://conn.json --region us-west-2
```

The secret must be JSON with `username` and `password` keys. Alternatively, re-create the pipeline in `select_rds` or `manual` connection mode, which provision a compliant connection.

**Then refresh the pipeline's IAM role, if it already exists.** `create_pipeline_specific_role` reuses an existing role **unchanged**: when `AgenticDataIngestion-<pipeline_id>-ExecutionRole` exists it returns it without touching its policy (very long pipeline ids get a shortened name; `naming.pipeline_execution_role_name` derives it). A role created by a run that hit Symptom 1, or created before this change for a target whose config carried no `secret_arn`, grants `GetSecretValue` only on the pipeline-scoped pattern `…:secret:agentic-data-ingestion/<pipeline_id>/*`. After the connection is fixed, the job then fails closed with `AccessDeniedException` on `secretsmanager:GetSecretValue`. Check the grant, and if it does not cover the connection's secret, delete the role (when no run of the pipeline is in progress) so the next run re-creates it with the resolved secret — or re-create the pipeline:

```bash
ROLE=AgenticDataIngestion-<pipeline_id>-ExecutionRole
aws iam get-role-policy --role-name "$ROLE" --policy-name PipelineSpecificAccess \
  --query "PolicyDocument.Statement[?contains(to_string(Action), 'secretsmanager:GetSecretValue')].Resource"

# Delete: inline policy, then each attached managed policy, then the role.
aws iam delete-role-policy --role-name "$ROLE" --policy-name PipelineSpecificAccess
aws iam list-attached-role-policies --role-name "$ROLE" --query 'AttachedPolicies[].PolicyArn' --output text
aws iam detach-role-policy --role-name "$ROLE" --policy-arn <each-arn-from-above>
aws iam delete-role --role-name "$ROLE"
```

A secret given by **name** rather than ARN is granted as `arn:aws:secretsmanager:<region>:<account>:secret:<name>-??????`, which matches that secret whatever its random suffix.

**Rotate credentials that were exposed before the change.** A Glue job created by `create_glue_pipeline` before this change still holds the target's plaintext `--jdbc_user` / `--jdbc_password` (or `--target_jdbc_user` / `--target_jdbc_password`) in its `DefaultArguments` until `create_glue_pipeline` next updates it; the next run of the pipeline does that, and re-renders the script in `CreateCode` first, so the job then carries `--secret_arn` instead. Check which jobs are affected by listing argument **names** only:

```bash
aws glue get-job --job-name "agentic-data-ingestion-job-<pipeline_id>" --region us-west-2 \
  --query 'keys(Job.DefaultArguments)'
```

Updating the job does not remove the values already recorded in CloudTrail, job-run history or the `create_glue_pipeline` Lambda's CloudWatch logs, which logged the full argument dictionary at INFO. Treat every database credential that ever went through that path as exposed and rotate it, then store the new one only in the secret the connection's `SECRET_ID` names.

---
