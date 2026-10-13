// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import * as sfn from 'aws-cdk-lib/aws-stepfunctions';
import * as tasks from 'aws-cdk-lib/aws-stepfunctions-tasks';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as subscriptions from 'aws-cdk-lib/aws-sns-subscriptions';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cw_actions from 'aws-cdk-lib/aws-cloudwatch-actions';
import { Construct } from 'constructs';
import { ComputeStack, LAMBDA_CODE_ASSET_EXCLUDE } from './compute-stack';
import { DataStack } from './data-stack';
import { FoundationStack } from './foundation-stack';
import { pinnedBuildImage, SAM_PYTHON312_ARM64_BUILD_IMAGE } from './build-images';
import { retainLambdaLogs } from './log-retention';

export interface OrchestrationStackProps extends cdk.StackProps {
  environment: string;
  compute: ComputeStack;
  data: DataStack;
  foundation: FoundationStack;
  stateMachineName?: string;
  notificationEmail?: string;
}

// P28 R5.4 (task 6.3): every Glue job this project creates is named
// agentic-data-ingestion-job-<pipeline_id>[-vN] (create_glue_pipeline
// job_builders.py / schema_evolution.py) — the prefix scopes EventBridge
// rules to this project's jobs instead of every Glue job in the account.
const GLUE_JOB_NAME_PREFIX = 'agentic-data-ingestion-job-';

// P28 R5.5 (task 6.3): the RunPipeline task timeout derives from pipeline
// config via this JSONPath; when the execution input carries no usable value
// the ApplyDefaultJobTimeout Pass state injects this ceiling. 6 hours is the
// documented default — generous enough that multi-hour Glue jobs are not
// falsely failed while Glue keeps writing (the old hardcoded 3600s killed
// anything past 1h), tight enough that a stranded callback still surfaces
// within the same working day.
const MAX_JOB_DURATION_PATH = '$.pipeline_config.max_job_duration_seconds';
const DEFAULT_MAX_JOB_DURATION_SECONDS = 21600; // 6 hours

// p34 R6.6: one constant, applied to every waitForTaskToken APPROVAL state.
// 72h = long enough to cover a weekend, short enough to notice. RunPipeline is
// excluded deliberately — it carries TimeoutSecondsPath from pipeline config
// (P28 R5.5, the constants directly above), not a human-wait ceiling: its
// timeout bounds a Glue job, not a person. The four container-backed steps are
// excluded on the same ground — CONTAINER_STEP_TIMEOUT_SECONDS /
// CREATE_INFRA_TIMEOUT_SECONDS are work ceilings (P28 R19.11), so "every
// waitForTaskToken state" is deliberately NOT the scope here; every
// waitForTaskToken APPROVAL state is.
//
// This is the WORST-CASE STALL for a waitForTaskToken step, which is why it is
// worth shortening. `LGR-60c4985-3` (template / ratified) excludes the
// waitForTaskToken steps from retry-resume caching precisely because
// `step_handler` is the outermost decorator, so a cached return fires before
// the token is ever sent and the state hangs for its full TimeoutSeconds. R6
// shortens that ceiling; it does NOT remove the exclusion.
//
// 72h is a proposal, not a measured value (design §5 risk 10) — one line to
// change. Rejected: a CDK context value with 259200 as the default (design
// D6). `cdk.context.json` is untracked, so a context value would stop the
// effective ceiling from being readable from the repo.
//
// Exported so the CDK suite asserts the constant rather than re-stating the
// number (test/orchestration-stack.test.ts).
export const APPROVAL_TIMEOUT_SECONDS = 259200; // 72 hours

// The whole execution's ceiling. Without it a Standard workflow may run for a
// year, and the pipeline lock (shared/config.py LOCK_TTL_SECONDS, which must
// exceed this) would have to be sized against that. The longest legitimate
// execution passes three approval gates (schema mapping, forced test review,
// code approval: 3 x APPROVAL_TIMEOUT_SECONDS = 216 h), then the Glue job
// (DEFAULT_MAX_JOB_DURATION_SECONDS, 6 h), plus the container steps, their
// retries and the regeneration loop, which sits between gates and adds minutes,
// not gates: about 224 h. Ten days leaves 16 h of margin.
// tests/contracts/test_approval_window_cross_language.py-style readers parse
// this literal, and tests/cdk pins LOCK_TTL_SECONDS > this value.
export const EXECUTION_TIMEOUT_SECONDS = 864000; // 10 days

// P32 D2 (2.6, 2.17): the regeneration attempt counter lives in EXECUTION
// STATE, at this path, for the whole of one execution. CreateCode's and
// TestCode's payloads read it from here, so it MUST resolve on every path
// into CreateCode — an unresolvable `.$` reference in a task's Payload is a
// path failure the step's Catch turns into a failed run, and the same class
// of hazard the CheckJobTimeoutConfig guard exists for (see R5.5 above).
// CheckRegenerationAttempt is that guard: it carries a trigger-supplied
// counter through untouched (shared/rerun_contract.py may legally put one on
// a `retry`/`self_healing` input — D1) and defaults it to 0 otherwise.
//
// It is deliberately NOT a run-id prefix and NOT a row in the executions
// table: `LGR-21a1b5b-2` (a row there broadcasts a dashboard event per
// attempt, perturbs the StatusIndex GSI and renders as a phantom run) and
// `LGR-21a1b5b-4` (a new id prefix reorders run history under the
// descending sort with page limit 10). Staying in-execution also keeps one
// writer holding the pipeline lock, which `LGR-21a1b5b-1` requires.
const REGENERATION_ATTEMPT_PATH = '$.regeneration_attempt';
// Scratch path for the increment. ASL can only produce an intrinsic value
// inside a Parameters object, so the incremented number is computed into
// this holder and then landed as a scalar at REGENERATION_ATTEMPT_PATH by a
// second Pass (InputPath picks the scalar, ResultPath places it). A single
// Pass cannot do both: with Parameters the state's result IS the object.
const NEXT_REGENERATION_ATTEMPT_HOLDER = '$.regeneration_next';
const NEXT_REGENERATION_ATTEMPT_PATH = '$.regeneration_next.attempt';

export class OrchestrationStack extends cdk.Stack {
  public readonly stateMachine: sfn.StateMachine;
  public readonly pipelineTriggerQueue: sqs.Queue;
  public readonly pipelineTriggerLambda: lambda.Function;
  public readonly criticalAlertsTopic: sns.Topic;
  public readonly approvalNotificationsTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: OrchestrationStackProps) {
    super(scope, id, props);

    const { environment, compute, data, foundation } = props;
    const smName = props.stateMachineName ?? 'agentic-data-ingestion-pipeline';
    // Deliberately no default address. A hardcoded placeholder used to subscribe
    // an address at a real domain the maintainers do not own to both SNS topics
    // on every deploy, sending a confirmation request to someone else's inbox.
    // (RFC 2606 reserves example.com for this; the old value was not a reserved
    // domain.) Unset or blank now means no email subscription is created at all;
    // the topics still exist and still receive published messages, so nothing
    // else changes. Set it with `-c notificationEmail=you@example.com` to
    // receive alerts.
    const notificationEmail = props.notificationEmail?.trim() || undefined;
    const region = cdk.Stack.of(this).region;

    // ── Step Functions ─────────────────────────────────────────────
    const sfnRole = new iam.Role(this, 'StepFunctionsExecutionRole', {
      assumedBy: new iam.ServicePrincipal('states.amazonaws.com'),
    });

    const lambdaFns = [
      compute.fileValidationLambda, compute.schemaExtractionLambda,
      compute.schemaComparisonLambda, compute.schemaMappingLambda,
      compute.codeGenerationLambda, compute.codeTestingLambda,
      compute.createGluePipelineLambda, compute.createInfrastructureLambda,
      compute.createQualityChecksLambda, compute.runPipelineLambda,
      compute.approvalHandlerLambda,
    ];
    sfnRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: lambdaFns.map(fn => fn.functionArn),
    }));
    sfnRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:GetItem'],
      resources: [data.stateTable.tableArn],
    }));

    // RecordFailure Lambda (P28 R1.3/R1.4) — parses the Catch payload and
    // writes the Failure_Record (pipeline_failure state item: originating
    // step, error type, human-readable message, run/pipeline ids) before the
    // execution transitions to PipelineFailed. Real handler lives in
    // lambda/agentic_data_ingestion_record_failure/. Construct ids keep the
    // legacy "FailureRecorder" names for CFN logical-id stability (the nag
    // acknowledgments reference the hashed logical id).
    const failureRecorderRole = new iam.Role(this, 'FailureRecorderRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    failureRecorderRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem', 'dynamodb:UpdateItem'],
      resources: [data.stateTable.tableArn],
    }));
    // The failure half of the pipeline error rate (PipelineErrorRateAlarm below).
    // PutMetricData defines no resource types, so it is Resource '*' bounded by
    // the namespace, the shape jobCompletionRole uses in foundation-stack.ts.
    failureRecorderRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'AgenticDataIngestion/Pipeline' } },
    }));

    const recordFailureFn = new lambda.Function(this, 'FailureRecorderFunction', {
      functionName: `agentic-data-ingestion-record-failure-${environment}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'lambda_function.lambda_handler',
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      role: failureRecorderRole,
      // Same scoping as every other lambda/ code asset — see
      // LAMBDA_CODE_ASSET_EXCLUDE in compute-stack.ts. Without it a local
      // pytest run leaves __pycache__ in the context and moves the asset hash.
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'agentic_data_ingestion_record_failure'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: LAMBDA_CODE_ASSET_EXCLUDE,
      }),
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        // The Environment dimension of the error-rate datapoint; the alarm
        // reads the series for this environment only.
        ENVIRONMENT: environment,
      },
    });

    // Grant SFN role permission to invoke the failure recorder
    sfnRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [recordFailureFn.functionArn],
    }));

    // Lock release Lambda — deletes the pipeline lock from DynamoDB on execution completion/failure.
    //
    // The delete is conditional on the lock's stored `execution_name` matching
    // the execution being cleaned up. The acquire (pipeline_trigger
    // _acquire_pipeline_lock) records the holder for exactly this check, and the
    // trigger's own failed-start release has always used it (LGR-21a1b5b-1).
    // This function did not, and that let a stale terminal event delete a
    // NEWER execution's lock: a failed execution releases through
    // ReleaseLockFailure, a fresh trigger acquires the same key, and then the
    // first execution's FAILED status-change event arrives and the unconditional
    // delete removed the lock the second execution was holding. Two writers on
    // one pipeline follow. The name reaches this function as `execution_name`
    // from the in-machine tasks ($$.Execution.Name) and as `detail.name` on the
    // EventBridge path. A lock held by someone else, or no lock at all, is
    // `released: false` and not an error; an execution in flight at deploy time
    // sends no name and keeps the old unconditional behaviour for its one
    // remaining release.
    const lockReleaseRole = new iam.Role(this, 'LockReleaseRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    lockReleaseRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:DeleteItem'],
      resources: [data.pipelineLocksTable.tableArn],
    }));

    const lockReleaseFn = new lambda.Function(this, 'LockReleaseFunction', {
      functionName: `agentic-data-ingestion-lock-release-${environment}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      timeout: cdk.Duration.seconds(15),
      memorySize: 128,
      role: lockReleaseRole,
      code: lambda.Code.fromInline(`import boto3, os, json, logging
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource('dynamodb')
locks_table = dynamodb.Table(os.environ['LOCKS_TABLE'])
def lambda_handler(event, context):
    # Handle both direct invocation (from SFN) and EventBridge events.
    # The lock key is derived from one of two sources:
    #   - S3-upload pipelines: 'source_key' is the S3 object key, and the
    #     lock key is the prefix (everything before the trailing '/').
    #   - JDBC pipelines: no S3 key exists; the lock key is the
    #     'pipeline_id' itself (mirrors the convention in
    #     pipeline_trigger._start_jdbc_execution._acquire_pipeline_lock).
    # The holder is the execution name: 'execution_name' on a direct
    # invocation, 'detail.name' on a Step Functions status-change event.
    source_key = event.get('source_key', '')
    pipeline_id = event.get('pipeline_id', '')
    execution_name = event.get('execution_name', '')
    if 'detail' in event:
        detail = event['detail'] or {}
        execution_name = execution_name or detail.get('name', '')
        if not source_key and not pipeline_id:
            try:
                sfn_input = json.loads(detail.get('input', '{}'))
                source_key = sfn_input.get('source_key', '')
                pipeline_id = sfn_input.get('pipeline_id', '')
            except (json.JSONDecodeError, TypeError):
                pass
    if source_key:
        parts = source_key.rsplit('/', 1)
        pipeline_prefix = parts[0] if len(parts) > 1 else source_key
    elif pipeline_id:
        pipeline_prefix = pipeline_id
    else:
        logger.warning('No source_key or pipeline_id in event, skipping lock release')
        return {'released': False}
    kwargs = {'Key': {'pipeline_prefix': pipeline_prefix}}
    if execution_name:
        # Only the holder may release. A lock a later execution acquired,
        # or no lock at all, fails the condition and is left alone.
        kwargs['ConditionExpression'] = 'execution_name = :owner'
        kwargs['ExpressionAttributeValues'] = {':owner': execution_name}
    else:
        logger.warning(f'No execution name in event for pipeline_prefix={pipeline_prefix}; releasing unconditionally (pre-2026-10-11 execution)')
    logger.info(f'Releasing lock for pipeline_prefix={pipeline_prefix} held by execution_name={execution_name or "<unknown>"}')
    try:
        locks_table.delete_item(**kwargs)
        logger.info(f'Lock released for pipeline_prefix={pipeline_prefix}')
        return {'released': True, 'pipeline_prefix': pipeline_prefix, 'execution_name': execution_name}
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            logger.info(f'Lock for pipeline_prefix={pipeline_prefix} is not held by {execution_name}; nothing released')
            return {'released': False, 'pipeline_prefix': pipeline_prefix, 'execution_name': execution_name, 'reason': 'not_owner'}
        logger.error(f'Failed to release lock for {pipeline_prefix}: {e}')
        return {'released': False, 'pipeline_prefix': pipeline_prefix, 'execution_name': execution_name, 'error': str(e)}
    except Exception as e:
        logger.error(f'Failed to release lock for {pipeline_prefix}: {e}')
        return {'released': False, 'pipeline_prefix': pipeline_prefix, 'execution_name': execution_name, 'error': str(e)}
`),
      environment: {
        LOCKS_TABLE: data.pipelineLocksTable.tableName,
      },
    });

    // Grant SFN role permission to invoke the lock release Lambda
    sfnRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [lockReleaseFn.functionArn],
    }));

    // ── State Machine Definition (Type-Safe CDK Constructs) ──────
    // P28 R1.3: standard Retry policy for every task state — transient step
    // failures raised by the Step_Lambdas (shared/step_errors.py contract)
    // plus Lambda service errors, with exponential backoff.
    const stepRetry: sfn.RetryProps = {
      errors: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'],
      interval: cdk.Duration.seconds(5),
      maxAttempts: 3,
      backoffRate: 2.0,
    };

    // P28 R1.3: standard Catch — EVERY error (States.ALL) routes to
    // RecordFailure with the original error preserved at $.error. Passing the
    // whole state (no JSONPath payload plucking) is what guarantees the
    // recorded error is the ORIGINATING step's error, never a downstream
    // States.Runtime JSONPath failure (R1.4).
    const stepCatch: sfn.CatchProps = {
      errors: ['States.ALL'],
      resultPath: '$.error',
    };

    // RecordFailure — invokes the record-failure Lambda to persist the
    // Failure_Record. Receives the FULL state (including $.error) plus the
    // execution name so run/pipeline ids can be recovered no matter which
    // step failed (even FileValidation, before any result keys exist).
    const recordFailure = new tasks.LambdaInvoke(this, 'RecordFailure', {
      lambdaFunction: recordFailureFn,
      payload: sfn.TaskInput.fromObject({
        'state.$': '$',
        'execution_name.$': '$$.Execution.Name',
      }),
      resultPath: '$.failure_recorder_result',
      payloadResponseOnly: true,
    });

    // UpdateSuccessState — terminal Pass state marking pipeline completion
    const updateSuccessState = new sfn.Pass(this, 'UpdateSuccessState', {
      parameters: {
        'pipeline_id.$': '$.pipeline_id',
        'status': 'COMPLETED',
        'timestamp.$': '$$.State.EnteredTime',
        'final_result.$': '$.run_pipeline_result',
      },
    });

    // ReleaseLockSuccess — release pipeline lock on successful completion.
    // For S3 pipelines the trigger sets `pipeline_id` to the prefix it
    // used as the lock key; for JDBC pipelines the trigger sets
    // `pipeline_id` to the actual pipeline ID, also the lock key. Either
    // way `pipeline_id` is the canonical lock key — `source_key` is
    // S3-specific and is no longer threaded through.
    //
    // `execution_name` is the lock holder the trigger recorded at acquisition;
    // the release function deletes only when it matches (see LockReleaseFunction).
    const releaseLockSuccess = new tasks.LambdaInvoke(this, 'ReleaseLockSuccess', {
      lambdaFunction: lockReleaseFn,
      payload: sfn.TaskInput.fromObject({
        'pipeline_id.$': '$.pipeline_id',
        'execution_name.$': '$$.Execution.Name',
      }),
      resultPath: '$.lock_release_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
    });

    // ReleaseLockFailure — release pipeline lock on failure (best-effort, errors swallowed)
    const releaseLockFailure = new tasks.LambdaInvoke(this, 'ReleaseLockFailure', {
      lambdaFunction: lockReleaseFn,
      payload: sfn.TaskInput.fromObject({
        'pipeline_id.$': '$.pipeline_id',
        'execution_name.$': '$$.Execution.Name',
      }),
      resultPath: '$.lock_release_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: true,
    });

    // PipelineFailed — terminal Fail state so the execution reports FAILED
    // (the lock is released first via ReleaseLockFailure). Without this, the
    // failure branch ended on a Task with End:true and SFN reported SUCCEEDED
    // even on logical failure, masking all pipeline failures.
    const pipelineFailed = new sfn.Fail(this, 'PipelineFailed', {
      errorPath: '$.error.Error',
      causePath: '$.error.Cause',
    });

    // Step 1: FileValidation
    const fileValidation = new tasks.LambdaInvoke(this, 'FileValidation', {
      lambdaFunction: compute.fileValidationLambda,
      resultPath: '$.file_validation_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: false,
    });
    fileValidation.addRetry(stepRetry);
    fileValidation.addCatch(recordFailure, stepCatch);

    // ── P32 2.18: the retry resume marker ──────────────────────────
    // The step a retry-from-step execution resumes at. Only FileValidation
    // receives the whole execution state, so it is the only state that can
    // read `$.retry_from_step` without a Payload path — it echoes the marker
    // into its own result (defaulting to '', never absent), and every
    // resumable step below reads it from there. That is why this needs no new
    // ASL state and no Choice guard: the path is guaranteed-present by
    // construction, on a fresh run as much as on a retry, so it cannot raise
    // the States.ParameterPathFailure that no Retry or Catch can intercept.
    //
    // Threaded ONLY into the five tasks.LambdaInvoke steps (Decision D3,
    // shared/retry_utils.TASK_TOKEN_FIELDS). The waitForTaskToken steps are
    // deliberately excluded: `shared/step_errors.step_handler` is the
    // outermost decorator on all four container steps, so serving a cached
    // result there would return before `@step_task_token` sends the token and
    // the state would stall for its full TimeoutSeconds. Resuming at
    // CreateCode — the route `remediation_hint` names below — skips all four
    // steps that precede it, which are all in this set.
    const RESUME_MARKER_PATH = '$.file_validation_result.retry_from_step';

    // Step 2: SchemaExtract
    const schemaExtract = new tasks.LambdaInvoke(this, 'SchemaExtract', {
      lambdaFunction: compute.schemaExtractionLambda,
      payload: sfn.TaskInput.fromObject({
        'run_id.$': '$.file_validation_result.run_id',
        'original_pipeline_id.$': '$.file_validation_result.original_pipeline_id',
        'execution_arn.$': '$$.Execution.Name',
        'source_format.$': '$.file_validation_result.source_format',
        'retry_from_step.$': RESUME_MARKER_PATH,
      }),
      resultPath: '$.schema_extract_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: false,
    });
    schemaExtract.addRetry(stepRetry);
    schemaExtract.addCatch(recordFailure, stepCatch);

    // Step 3: SchemaCompare
    const schemaCompare = new tasks.LambdaInvoke(this, 'SchemaCompare', {
      lambdaFunction: compute.schemaComparisonLambda,
      payload: sfn.TaskInput.fromObject({
        'run_id.$': '$.file_validation_result.run_id',
        'execution_arn.$': '$$.Execution.Name',
        'retry_from_step.$': RESUME_MARKER_PATH,
      }),
      resultPath: '$.schema_compare_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: false,
    });
    schemaCompare.addRetry(stepRetry);
    schemaCompare.addCatch(recordFailure, stepCatch);

    // Step 4a: SchemaMapping — invoked when schema needs remapping
    const schemaMapping = new tasks.LambdaInvoke(this, 'SchemaMapping', {
      lambdaFunction: compute.schemaMappingLambda,
      payload: sfn.TaskInput.fromObject({
        'run_id.$': '$.file_validation_result.run_id',
        'source_schema_hash.$': '$.schema_extract_result.schema_hash',
        'execution_arn.$': '$$.Execution.Name',
        'schema_change_action.$': '$.schema_compare_result.action',
        'schema_differences.$': '$.schema_compare_result.comparison_result.differences',
        'retry_from_step.$': RESUME_MARKER_PATH,
      }),
      resultPath: '$.schema_mapping_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: false,
    });
    schemaMapping.addRetry(stepRetry);
    schemaMapping.addCatch(recordFailure, stepCatch);

    // Step 4b: ReuseExistingMapping — Pass state when schema is unchanged
    //
    // WHAT THIS FAST PATH SAVES, AND WHAT IT DOES NOT (p47 R4, Arm B — ratified).
    // It saves the MAPPING COMPUTATION: taking this branch instead of SchemaMapping
    // skips the Bedrock Converse call at `schema_mapping/lambda_function.py:777-778`
    // (`generate_mapping_with_bedrock`, reached only from that step's handler at
    // `:1737`), along with the whole mapping write. That is a real, per-run saving on
    // every unchanged-schema run, so this branch is NOT inert.
    //
    // It does NOT save the human approval round-trip. This Pass injects no
    // `approval_required` into `$.schema_mapping_result` — see `parameters` below —
    // and CheckSchemaMappingApproval's permissive branch fires only on
    // `And[IsPresent, IsBoolean, BooleanEquals false]` (the `checkSchemaMappingApproval`
    // chain below, currently `:831-834`). An ABSENT field fails the very first
    // conjunct, so a reused-mapping run falls to that Choice's
    // Default, which is WaitForSchemaMappingApproval — a human. Every reused-mapping
    // run therefore still passes the schema-mapping gate. (The later code gate is
    // governed independently by `$.test_code_result.approval_required`; nothing here
    // affects it either way.)
    //
    // THIS IS CORRECT, NOT A DEFECT. It is exactly what invariant `LGR-69ca78b-1`
    // (template / ratified) exists to produce: absent or non-boolean routes to the
    // human, never past them. It has been mistaken for a bug once already (p47 R4.3),
    // which is why it is written down here rather than left to be re-derived.
    //
    // DO NOT "fix" this by adding `approval_required: false` to `parameters`. That is
    // p47 R4 Arm A — it satisfies all three conjuncts without touching the Choice, so
    // it would work, and it is a REDUCTION IN HUMAN OVERSIGHT that needs a human to
    // accept it. Arm B was chosen. Reversing it is a new decision, not a cleanup.
    // Weakening the Choice instead is forbidden outright by `LGR-69ca78b-1`, which
    // names all three rejected weakenings (dropping IsBoolean, reordering the
    // conjuncts, normalising through an apply-default Pass state).
    //
    // Pinned by `deploy-cdk/test/orchestration-stack.test.ts`: this state's Parameters
    // are asserted to carry no `approval_required`, and the Choice's three ordered
    // conjuncts are asserted by deep equality ('R4.5: CheckSchemaMappingApproval
    // permits only an explicit boolean false').
    const reuseExistingMapping = new sfn.Pass(this, 'ReuseExistingMapping', {
      parameters: {
        'statusCode': 200,
        'run_id.$': '$.file_validation_result.run_id',
        'original_pipeline_id.$': '$.schema_compare_result.original_pipeline_id',
        'mapping_id.$': '$.schema_compare_result.existing_mapping_id',
        'reused_mapping': true,
      },
      resultPath: '$.schema_mapping_result',
    });

    // Step 5: CheckSchemaMappingApproval — Choice: does schema mapping need approval?
    const checkSchemaMappingApproval = new sfn.Choice(this, 'CheckSchemaMappingApproval');

    // Step 6: WaitForSchemaMappingApproval — waitForTaskToken approval gate
    //
    // `mapping_id` IS THE ONLY THING THIS GATE KNOWS ABOUT WHAT IT IS GATING (p54 R1.1).
    // On a Reuse_Run the `schema_mapping` Lambda never runs, so it writes no
    // Mapping_State_Row, so `get_approval_data`'s `if "Item" in response:` guard
    // (`approval_handler/lambda_function.py:276`) is skipped and `approval_data`
    // stays `{}` — the approver is shown nothing and clicks anyway. The handler
    // cannot resolve the mapping without being told which mapping it is, and the
    // event carried no identifier at all. This key is that identifier.
    //
    // WHY `mapping_id` AND NOT `reused_mapping`. Invariant `LGR-5c539a6-2`: every
    // key an ASL path reads off a `$.{step}_result` envelope must exist on BOTH
    // arms, because ASL has no coalesce and a path that does not resolve raises
    // `States.ParameterPathFailure` — a failure neither Retry nor Catch can
    // intercept. `$.schema_mapping_result` has exactly two producers, and
    // `mapping_id` is on both: the reuse Pass sets it from
    // `$.schema_compare_result.existing_mapping_id` (`parameters` above), and the
    // SchemaMapping Lambda returns it in its one and only success dict
    // (`schema_mapping/lambda_function.py:2342`; `@step_handler` never returns an
    // error payload, so that dict or a verbatim replay of it is the sole success
    // value). `reused_mapping` is on the Pass arm ONLY, so a path to it would fail
    // every non-reuse run. Do not add one — p54 do-NOT 1. The reuse FACT is derived
    // on the read path in `get_run_details` instead (`LGR-9e76249-2`'s shape).
    //
    // The totality is not only read off the source: CreateCode and TestCode ALREADY
    // read this exact path (`:571`, `:627`), and both sit downstream of
    // CheckSchemaMappingApproval on BOTH arms. So every run that has ever reached
    // CreateCode — by either arm, gated or not — has resolved
    // `$.schema_mapping_result.mapping_id` in production. A one-armed key there
    // would have been failing reuse runs at CreateCode all along. This gate adds a
    // third reader of an established path, not a new path.
    //
    // THIS TOUCHES THE TASK, NOT THE PASS AND NOT THE CHOICE. No `approval_required`
    // is injected anywhere, so the gate still fails closed to a human on a reused
    // mapping exactly as before: `LGR-69ca78b-1` (standing invariant) and
    // `LGR-5d68eeb-1` Arm B, whose 34 lines of rationale are at the Pass above.
    // Arm A — injecting `approval_required: false` — was REJECTED as a reduction in
    // human oversight, and this key is not a step toward it. Telling the approver
    // WHICH mapping they are approving is the opposite of approving for them.
    const waitForSchemaMappingApproval = new sfn.CustomState(this, 'WaitForSchemaMappingApproval', {
      stateJson: {
        Type: 'Task',
        Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
        Parameters: {
          FunctionName: compute.approvalHandlerLambda.functionArn,
          Payload: {
            'approval_type': 'schema_mapping',
            'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
            'run_id.$': '$.file_validation_result.run_id',
            'execution_arn.$': '$$.Execution.Name',
            'task_token.$': '$$.Task.Token',
            // Total on both arms — see the note above. p54 R1.1, `LGR-5c539a6-2`.
            'mapping_id.$': '$.schema_mapping_result.mapping_id',
          },
        },
        TimeoutSeconds: APPROVAL_TIMEOUT_SECONDS,
        Retry: [{ ErrorEquals: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'], IntervalSeconds: 5, MaxAttempts: 3, BackoffRate: 2.0 }],
        Catch: [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }],
        ResultPath: '$.schema_mapping_approval',
      },
    });

    // ── Container-backed steps: waitForTaskToken (P28 R19.11) ─────
    // The four container-backed steps (CreateCode, TestCode,
    // CreateGluePipeline, CreateInfrastructure) are waitForTaskToken task
    // states so the proxy Lambda does NOT idle-bill for the container's full
    // duration: the token travels in the payload, the AgentCore container
    // acks the dispatch immediately (the proxy returns within seconds) and
    // closes the loop with SendTaskSuccess/SendTaskFailure when the
    // background work finishes (shared/sfn_task_token.py owns the contract).
    //
    // • Output shape is preserved: SendTaskSuccess carries the exact JSON the
    //   handler returns, landing at the same ResultPath the old
    //   payloadResponseOnly LambdaInvoke used — TestResultChoice keeps
    //   reading $.test_code_result.test_result.status unchanged.
    // • Failure shape is preserved: the container sends
    //   SendTaskFailure(error=<ExceptionClassName>), and Lambda-side raises
    //   remain invocation errors — both match the existing
    //   StepFailure/TransientStepFailure ErrorEquals (composes with R1;
    //   Retry re-runs the whole dispatch, proxy + container).
    // • waitForTaskToken states MUST carry an explicit TimeoutSeconds (the
    //   ASL default is a year) — it is the backstop for a container that
    //   crashes after acking the dispatch. Values = the step's previous work
    //   ceiling (Lambda timeout / container read-timeout, 900s or 600s) plus
    //   a dispatch/callback buffer.
    const CONTAINER_STEP_RETRY = [{
      ErrorEquals: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'],
      IntervalSeconds: 5,
      MaxAttempts: 3,
      BackoffRate: 2.0,
    }];
    const CONTAINER_STEP_CATCH = [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }];
    const CONTAINER_STEP_TIMEOUT_SECONDS = 1200; // 900s work ceiling + buffer
    const CREATE_INFRA_TIMEOUT_SECONDS = 900; // 600s work ceiling + buffer

    const containerBackedTask = (
      id: string,
      functionArn: string,
      payload: Record<string, string>,
      resultPath: string,
      timeoutSeconds: number,
    ) => new sfn.CustomState(this, id, {
      stateJson: {
        Type: 'Task',
        Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
        Parameters: {
          FunctionName: functionArn,
          Payload: {
            ...payload,
            'task_token.$': '$$.Task.Token',
          },
        },
        ResultPath: resultPath,
        TimeoutSeconds: timeoutSeconds,
        Retry: CONTAINER_STEP_RETRY,
        Catch: CONTAINER_STEP_CATCH,
      },
    });

    // Step 7: CreateCode
    const createCode = containerBackedTask(
      'CreateCode',
      compute.codeGenerationLambda.functionArn,
      {
        'run_id.$': '$.file_validation_result.run_id',
        'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
        'mapping_id.$': '$.schema_mapping_result.mapping_id',
        'source_schema.$': '$.schema_extract_result.extracted_schema',
        'source_format.$': '$.file_validation_result.source_format',
        // P32 2.16: which attempt this is. 0 on a first generation, so
        // first-attempt behaviour is unchanged (3.18) — create_code already
        // defaulted the key to 0 when absent; now it is stated.
        'regeneration_attempt.$': REGENERATION_ATTEMPT_PATH,
      },
      '$.create_code_result',
      CONTAINER_STEP_TIMEOUT_SECONDS,
    );

    // Step 7 guard: CheckRegenerationAttempt / ApplyDefaultRegenerationAttempt
    // (P32 D2). Same shape and same reason as the R5.5 timeout guard: make
    // REGENERATION_ATTEMPT_PATH guaranteed-present before any state reads it
    // in a Payload. Present AND numeric → straight through, so a counter the
    // trigger contract legally put on a retry/self_healing input survives;
    // absent or malformed → 0 is injected first. Both fresh entries into
    // CreateCode go through here; the regeneration loop does not need to,
    // because it lands its own value on a path this guard already
    // established.
    const applyDefaultRegenerationAttempt = new sfn.Pass(this, 'ApplyDefaultRegenerationAttempt', {
      comment: 'Seed regeneration_attempt=0 for a first generation (P32 D2)',
      result: sfn.Result.fromNumber(0),
      resultPath: REGENERATION_ATTEMPT_PATH,
    });

    const checkRegenerationAttempt = new sfn.Choice(this, 'CheckRegenerationAttempt', {
      comment: 'Carry a trigger-supplied regeneration_attempt through; default it otherwise (P32 D2)',
    });

    // Step 8: TestCode
    const testCode = containerBackedTask(
      'TestCode',
      compute.codeTestingLambda.functionArn,
      {
        'run_id.$': '$.file_validation_result.run_id',
        'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
        'code_version_id.$': '$.create_code_result.code_version_id',
        'code_s3_uri.$': '$.create_code_result.code_s3_uri',
        // p53 task 3.3: `source_sample_uri`, not `json_s3_uri`. schema_extract
        // resolves the preference (Sample_Artifact if one exists, else the
        // canonical source URI) in Python and returns the result on BOTH arms —
        // JDBC and S3-upload — so this path is guaranteed-present by
        // construction, exactly as RESUME_MARKER_PATH above is. A bare
        // `$.schema_extract_result.sample_s3_uri` was rejected: that key exists
        // on the JDBC arm only, so every S3-upload run would raise the
        // States.ParameterPathFailure documented at :338-340 that no Retry or
        // Catch can intercept. ASL has no coalesce intrinsic, so there is no ASL
        // expression that reads a possibly-absent path safely.
        //
        // On an S3-upload run this is the same VALUE `json_s3_uri` carried
        // (schema_extract returns `canonical_uri` for both), so the source-files
        // read on `verificationSessionRole` (foundation-stack.ts:835-887) still
        // covers it.
        'source_data_uri.$': '$.schema_extract_result.source_sample_uri',
        'mapping_id.$': '$.schema_mapping_result.mapping_id',
        // P32 2.17: the bound is only reachable if test_code is told which
        // attempt it is evaluating. Guaranteed present — TestCode is only
        // reachable through CreateCode, which is only reachable through the
        // CheckRegenerationAttempt guard or the loop-back edge.
        'regeneration_attempt.$': REGENERATION_ATTEMPT_PATH,
      },
      '$.test_code_result',
      CONTAINER_STEP_TIMEOUT_SECONDS,
    );

    // Step 8b: TestResultChoice (P28 R4.1) — branch on the honest structured
    // test result produced by test_code (task 5.1 contract). With TestCode
    // using resultPath '$.test_code_result' + payloadResponseOnly, the
    // documented condition is:
    //   $.test_code_result.test_result.status == "passed" → CheckCodeApproval
    //   anything else (failed, or missing status)         → ForceTestReviewApproval
    // A failing test is a SUCCESSFUL step whose payload says failed — only
    // this Choice keeps it from ever reaching CreateInfrastructure without a
    // human decision (R4.5).
    //
    // P32 D2 REVERSES P28 R4.2's "regeneration is deliberately NOT a new SFN
    // loop" — deliberately, and only because the destination now exists. R4.2
    // removed a branch that pointed at nothing: the counter was never set, so
    // `regeneration_needed` was True on every failing run and the only
    // consumer of that verdict was the approval gate (p32 1.16). The third
    // branch below is that missing destination. The gate is NOT weakened:
    //   • a passed test still goes to CheckCodeApproval (first branch),
    //   • a failing test with nothing left to try still goes to
    //     ForceTestReviewApproval (Default), and
    //   • the regeneration branch re-enters CreateCode, so its only exits are
    //     a later passing test or the same forced review once the bound is
    //     reached — no edge from it reaches CreateInfrastructure (3.21).
    const testResultChoice = new sfn.Choice(this, 'TestResultChoice', {
      comment: 'Passed → approval check; regeneration in bounds → CreateCode; otherwise forced review',
    });

    // Step 8b-i / 8b-ii: the regeneration loop-back writer (P32 D2).
    // ComputeNext produces attempt+1 (intrinsics can only be produced into a
    // Parameters object) and Increment lands it as the scalar CreateCode's
    // and TestCode's payloads read. The source is the state's own counter,
    // not test_code's echo of it, so the loop does not depend on the Lambda
    // returning the field — and neither Pass touches any path a downstream
    // state reads, so re-entering CreateCode cannot clobber earlier results.
    const computeNextRegenerationAttempt = new sfn.Pass(this, 'ComputeNextRegenerationAttempt', {
      comment: 'attempt + 1, into a scratch holder (P32 D2)',
      parameters: {
        'attempt.$': `States.MathAdd(${REGENERATION_ATTEMPT_PATH}, 1)`,
      },
      resultPath: NEXT_REGENERATION_ATTEMPT_HOLDER,
    });

    const incrementRegenerationAttempt = new sfn.Pass(this, 'IncrementRegenerationAttempt', {
      comment: 'Land the incremented attempt as the scalar the payloads read (P32 D2)',
      inputPath: NEXT_REGENERATION_ATTEMPT_PATH,
      resultPath: REGENERATION_ATTEMPT_PATH,
    });

    // Step 8c: ForceTestReviewApproval (P28 R4.2) — forced manual approval on
    // test failure. waitForTaskToken gate of type `test_failure`; the payload
    // carries the full structured report (test_result.checks +
    // report_s3_uri) so the approval record can render it. Approver choices:
    //   approve-anyway → proceeds to CheckCodeApproval, the SAME next state
    //                    as a passed test (recorded as approved on the
    //                    test_failure approval record — approval-despite-
    //                    failure is auditable there)
    //   reject         → send_task_failure(ApprovalRejected) fails this task
    //                    → Catch → RecordFailure → PipelineFailed (identical
    //                    to WaitForCodeApproval rejection semantics)
    //   regenerate     → still not an approver action here. Under P32 D2 the
    //                    automatic attempts happen BEFORE this gate: a run
    //                    only reaches it once the bound is reached or the
    //                    failure is not a regeneration trigger, so by the
    //                    time an approver sees this screen there is nothing
    //                    left to retry automatically.
    const forceTestReviewApproval = new sfn.CustomState(this, 'ForceTestReviewApproval', {
      stateJson: {
        Type: 'Task',
        Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
        Parameters: {
          FunctionName: compute.approvalHandlerLambda.functionArn,
          Payload: {
            'approval_type': 'test_failure',
            'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
            'run_id.$': '$.file_validation_result.run_id',
            'execution_arn.$': '$$.Execution.Name',
            'task_token.$': '$$.Task.Token',
            // Full test report travels with the approval request (R4.2)
            'test_result.$': '$.test_code_result.test_result',
            // Regeneration = p16 retry-from-step, resume from CreateCode
            'remediation_hint': 'retry-from-step:create_code',
          },
        },
        TimeoutSeconds: APPROVAL_TIMEOUT_SECONDS,
        Retry: [{ ErrorEquals: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'], IntervalSeconds: 5, MaxAttempts: 3, BackoffRate: 2.0 }],
        Catch: [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }],
        ResultPath: '$.test_review_approval',
      },
    });

    // Step 9: CheckCodeApproval — Choice: does generated code need approval?
    const checkCodeApproval = new sfn.Choice(this, 'CheckCodeApproval');

    // Step 10: WaitForCodeApproval — waitForTaskToken approval gate
    const waitForCodeApproval = new sfn.CustomState(this, 'WaitForCodeApproval', {
      stateJson: {
        Type: 'Task',
        Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
        Parameters: {
          FunctionName: compute.approvalHandlerLambda.functionArn,
          Payload: {
            'approval_type': 'code_generation',
            'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
            'run_id.$': '$.file_validation_result.run_id',
            'execution_arn.$': '$$.Execution.Name',
            'task_token.$': '$$.Task.Token',
            // p34 R3.6: the structured test result travels with the approval
            // request, exactly as ForceTestReviewApproval already carries it
            // (see that state's Payload above). Without it a code_generation
            // approval reaches the reviewer with nothing to judge — the gate
            // renders a header, a line of placeholder text and two buttons.
            'test_result.$': '$.test_code_result.test_result',
          },
        },
        TimeoutSeconds: APPROVAL_TIMEOUT_SECONDS,
        Retry: [{ ErrorEquals: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'], IntervalSeconds: 5, MaxAttempts: 3, BackoffRate: 2.0 }],
        Catch: [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }],
        ResultPath: '$.code_approval',
      },
    });

    // Step 11: CreateGluePipeline (runs after CreateInfrastructure — Glue job needs warehouse_location)
    const createGluePipeline = containerBackedTask(
      'CreateGluePipeline',
      compute.createGluePipelineLambda.functionArn,
      {
        'run_id.$': '$.file_validation_result.run_id',
        'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
        'file_size_bytes.$': '$.file_validation_result.file_size_bytes',
        'file_count.$': '$.file_validation_result.file_count',
      },
      '$.create_glue_pipeline_result',
      CONTAINER_STEP_TIMEOUT_SECONDS,
    );

    // Step 12: CreateInfrastructure (runs before CreateGluePipeline so warehouse_location is available)
    const createInfrastructure = containerBackedTask(
      'CreateInfrastructure',
      compute.createInfrastructureLambda.functionArn,
      {
        'run_id.$': '$.file_validation_result.run_id',
        'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
      },
      '$.create_infrastructure_result',
      CREATE_INFRA_TIMEOUT_SECONDS,
    );

    // Step 13: CreateQualityChecks
    const createQualityChecks = new tasks.LambdaInvoke(this, 'CreateQualityChecks', {
      lambdaFunction: compute.createQualityChecksLambda,
      payload: sfn.TaskInput.fromObject({
        'run_id.$': '$.file_validation_result.run_id',
        'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
        'retry_from_step.$': RESUME_MARKER_PATH,
      }),
      resultPath: '$.create_quality_checks_result',
      payloadResponseOnly: true,
      retryOnServiceExceptions: false,
    });
    createQualityChecks.addRetry(stepRetry);
    createQualityChecks.addCatch(recordFailure, stepCatch);

    // Step 14 (guard): config-derived RunPipeline timeout (P28 R5.5, task 6.3).
    //
    // The RunPipeline task timeout is no longer a hardcoded 3600s — it reads
    // TimeoutSecondsPath from $.pipeline_config.max_job_duration_seconds so
    // per-pipeline duration config bounds how long the Glue-completion
    // callback may stay outstanding. SFN semantics make this dangerous when
    // the field is absent: TimeoutSecondsPath resolution failure is a
    // States.Runtime error that NO Retry/Catch can intercept — strictly worse
    // than the old hardcoded value. The two states below make the path
    // guaranteed-present before RunPipeline ever evaluates it:
    //   • CheckJobTimeoutConfig routes straight to RunPipeline only when the
    //     execution state already carries a usable value (present AND numeric
    //     AND > 0 — the IsPresent guard is documented ASL practice so absent
    //     fields can't kill the Choice either);
    //   • every other shape (absent — see below, this is every execution
    //     today — wrong type, or non-positive) flows through
    //     ApplyDefaultJobTimeout, which injects the documented 6-hour ceiling
    //     (deep ResultPath creates $.pipeline_config as needed).
    //
    // ── p47 R5 — RATIFIED ARM B ──────────────────────────────────────
    // This is a forward-compatibility seam with NO CURRENT PRODUCER, and that
    // is deliberate rather than unfinished.
    //
    // THE CONFIGURED ARM IS UNREACHABLE TODAY, so 100% of executions fall to
    // the Default and take ApplyDefaultJobTimeout. Nothing anywhere writes
    // max_job_duration_seconds into execution state. Re-verified at p47 C3:
    // the identifier occurs in exactly three files — this one, the vitest
    // suite, and tests/contracts/event_schemas/test_sfn_states_contract.py —
    // and ZERO times under lambda/, shared/, dashboard-react/src/, mcp/,
    // agentcore/ or pyspark_templates/. The Choice is not dead code: it is
    // the seam a producer plugs into, and when the trigger or wizard later
    // carries real per-pipeline duration config it flows through with zero
    // further ASL changes.
    //
    // DO NOT MISTAKE get_pipeline_config FOR THAT MISSING PRODUCER. Three
    // unrelated things in this repo are spelled `pipeline_config`, and
    // satisfying any one of them does NOT satisfy this Choice:
    //   1. $.pipeline_config — a path in STEP FUNCTIONS EXECUTION STATE.
    //      The only store CheckJobTimeoutConfig can read, and the only one
    //      ApplyDefaultJobTimeout writes.
    //   2. A JSON-STRING ATTRIBUTE ON A DYNAMODB STATE ROW, read by
    //      approval_handler/lambda_function.py:154 (`get_pipeline_config`,
    //      called at :391, re-echoed at :431). It carries approval settings,
    //      not job duration — and it has no producer either, which is why
    //      the per-pipeline timeout_hours override is unreachable (see
    //      tests/unit/lambdas/approval_handler/
    //      test_serialization_preservation_prop.py:133-134).
    //   3. A MAP ON THE PIPELINE RECORD, read for `source_bucket` by
    //      shared/rerun_contract.py:166 (`_resolve_source_bucket`).
    //
    // WHY ARM A (wire a producer) WAS NOT TAKEN — it is a data-model change,
    // not wiring, so do not re-propose it as a quick win. It needs: a
    // pipeline-record field that does not exist; wizard UI and validation to
    // set it; widening shared/rerun_contract.build_trigger_input, whose
    // signature has no pipeline_config parameter and whose output never emits
    // the key (the ratified P28 R9.2 single trigger contract, which raises
    // ValueError on any field it does not recognise for the flavor), plus its
    // contract tests; and covering all four start_execution producers
    // (dashboard_api/handlers/upload.py:187, handlers/pipelines_runs.py:1467
    // and :1858, pipeline_trigger/lambda_function.py:666). Underneath all of
    // that sits a product decision nobody has taken: which producer owns
    // per-pipeline duration.
    //
    // ApplyDefaultJobTimeout AND ITS 6-HOUR RESULT ARE LOAD-BEARING AND MUST
    // STAY (p47 R5.3). This is not a convenience default — it is what makes
    // TimeoutSecondsPath safe to evaluate at all, because the resolution
    // failure it prevents is uncatchable. Deleting it, or letting the Choice
    // fall through to RunPipeline, turns executions on the absent-field path
    // into States.Runtime failures; today that is EVERY execution, because
    // every one takes this branch. The IsPresent conjunct is load-bearing for
    // the same reason plus a second one: invariant LGR-69ca78b-1 rests its
    // "a Choice guarded by an IsPresent conjunct cannot raise States.Runtime"
    // claim on in-repo precedent rather than on documented short-circuit
    // semantics, and names this Choice as one of its three precedents. Weaken
    // the guard here and that invariant loses part of its evidence base.
    // Both the conjunct order and the 21600s result are already pinned by
    // test in two places — deploy-cdk/test/orchestration-stack.test.ts and
    // tests/contracts/event_schemas/test_sfn_states_contract.py — so a change
    // here has to be a deliberate test edit (LGR-5906ec6-2's mechanism).
    const applyDefaultJobTimeout = new sfn.Pass(this, 'ApplyDefaultJobTimeout', {
      comment: 'Inject the default 6h max_job_duration_seconds ceiling (P28 R5.5)',
      result: sfn.Result.fromNumber(DEFAULT_MAX_JOB_DURATION_SECONDS),
      resultPath: MAX_JOB_DURATION_PATH,
    });

    const checkJobTimeoutConfig = new sfn.Choice(this, 'CheckJobTimeoutConfig', {
      comment: 'Use pipeline-config job duration when usable; default it otherwise (P28 R5.5)',
    });

    // Step 14: RunPipeline — waitForTaskToken for Glue job completion callback.
    // TimeoutSecondsPath (not TimeoutSeconds): value guaranteed by the
    // CheckJobTimeoutConfig/ApplyDefaultJobTimeout guard directly upstream.
    const runPipeline = new sfn.CustomState(this, 'RunPipeline', {
      stateJson: {
        Type: 'Task',
        Resource: 'arn:aws:states:::lambda:invoke.waitForTaskToken',
        Parameters: {
          FunctionName: compute.runPipelineLambda.functionArn,
          Payload: {
            'run_id.$': '$.file_validation_result.run_id',
            'pipeline_id.$': '$.file_validation_result.original_pipeline_id',
            'taskToken.$': '$$.Task.Token',
          },
        },
        ResultPath: '$.run_pipeline_result',
        TimeoutSecondsPath: MAX_JOB_DURATION_PATH,
        Retry: [{ ErrorEquals: ['TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException'], IntervalSeconds: 5, MaxAttempts: 3, BackoffRate: 2.0 }],
        Catch: [{ ErrorEquals: ['States.ALL'], Next: 'RecordFailure', ResultPath: '$.error' }],
      },
    });

    // ── Chain: CheckSchemaAction branching ──────────────────────────
    // Skip remapping only when schema is unchanged AND a reusable mapping exists
    const checkSchemaAction = new sfn.Choice(this, 'CheckSchemaAction', {
      comment: 'Skip remapping only when schema is unchanged AND a reusable mapping exists',
    });

    // ── Chain: CheckSchemaAction branching ──────────────────────────
    // The complex And condition for reuse: action=use_existing_pipeline AND mapping_id present AND non-empty
    checkSchemaAction
      .when(
        sfn.Condition.and(
          sfn.Condition.stringEquals('$.schema_compare_result.action', 'use_existing_pipeline'),
          sfn.Condition.isPresent('$.schema_compare_result.existing_mapping_id'),
          sfn.Condition.not(sfn.Condition.stringEquals('$.schema_compare_result.existing_mapping_id', '')),
        ),
        reuseExistingMapping,
      )
      .otherwise(schemaMapping);

    // ── Chain: CheckSchemaMappingApproval branching ─────────────────
    // p34 R4.5: the same inversion as CheckCodeApproval below, for the same
    // reason — this gate had the identical fail-OPEN shape, so fixing one and
    // leaving its twin open would leave the finding half-addressed. See the
    // comment block on CheckCodeApproval for why the conjunction is shaped this
    // way, why the conjunct order and IsBoolean must not change, and what the
    // no-States.Runtime claim actually rests on.
    checkSchemaMappingApproval
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent('$.schema_mapping_result.approval_required'),
          sfn.Condition.isBoolean('$.schema_mapping_result.approval_required'),
          sfn.Condition.booleanEquals('$.schema_mapping_result.approval_required', false),
        ),
        // P32 D2 PRESERVED (`LGR-60c4985-2`, template / ratified): the
        // permissive arm still goes through the regeneration-counter guard and
        // never straight to CreateCode, so CreateCode's payload path always
        // resolves. CreateCode is still the next *step* either way.
        checkRegenerationAttempt,
      )
      // Fail closed: absent or non-boolean routes to the human, not past them.
      // waitForSchemaMappingApproval.next(checkRegenerationAttempt) below is
      // unchanged, so BOTH arms still reach CreateCode through the guard —
      // which is the whole of P32 D2's guarantee.
      .otherwise(waitForSchemaMappingApproval);

    // ── Chain: CheckRegenerationAttempt branching (P32 D2) ──────────
    // Present AND numeric → the value stands (a trigger-supplied counter on a
    // retry/self_healing input, D1); anything else gets 0 injected first.
    // IsPresent guards the numeric comparison — the same documented ASL
    // pattern the R5.5 guard uses, so an absent field cannot kill the Choice.
    checkRegenerationAttempt
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent(REGENERATION_ATTEMPT_PATH),
          sfn.Condition.isNumeric(REGENERATION_ATTEMPT_PATH),
        ),
        createCode,
      )
      .otherwise(applyDefaultRegenerationAttempt);
    applyDefaultRegenerationAttempt.next(createCode);

    // ── Chain: CheckJobTimeoutConfig branching (P28 R5.5) ───────────
    // Present AND numeric AND positive → the config value drives
    // TimeoutSecondsPath; anything else gets the 6h default injected first.
    // (IsPresent guards the numeric comparisons — the documented ASL pattern
    // for fields that may be absent.)
    // The first branch has NO PRODUCER, so the .otherwise() is the only path
    // any execution takes today. That is p47 R5's ratified Arm B, and neither
    // the conjuncts nor the default may be removed — full reasoning, and the
    // three unrelated `pipeline_config` stores, at the guard block where these
    // two states are declared.
    checkJobTimeoutConfig
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent(MAX_JOB_DURATION_PATH),
          sfn.Condition.isNumeric(MAX_JOB_DURATION_PATH),
          sfn.Condition.numberGreaterThan(MAX_JOB_DURATION_PATH, 0),
        ),
        runPipeline,
      )
      .otherwise(applyDefaultJobTimeout);
    applyDefaultJobTimeout.next(runPipeline);

    // ── Chain: TestResultChoice branching (P28 R4.1) ────────────────
    // The IsPresent guard makes "anything else" honest: a malformed payload
    // (missing test_result.status) routes to the forced review instead of
    // killing the Choice with an uncatchable States.Runtime error.
    testResultChoice
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent('$.test_code_result.test_result.status'),
          sfn.Condition.stringEquals('$.test_code_result.test_result.status', 'passed'),
        ),
        checkCodeApproval,
      )
      // P32 D2 / 2.17: a failing run whose failures are regeneration triggers
      // and whose attempt budget is not spent goes back to CreateCode inside
      // THIS execution, with the counter advanced. Every field read here is
      // IsPresent/IsNumeric guarded, and the bound is re-evaluated on the edge
      // itself rather than trusted from the Lambda, so a regression in
      // test_code cannot produce an unbounded loop. Ordered after the passed
      // branch and before the Default, so a passing test never regenerates and
      // an exhausted or non-regenerable failure still reaches the human gate.
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent('$.test_code_result.regeneration_needed'),
          sfn.Condition.booleanEquals('$.test_code_result.regeneration_needed', true),
          sfn.Condition.isPresent('$.test_code_result.regeneration_attempt'),
          sfn.Condition.isNumeric('$.test_code_result.regeneration_attempt'),
          sfn.Condition.isPresent('$.test_code_result.regeneration_max_attempts'),
          sfn.Condition.isNumeric('$.test_code_result.regeneration_max_attempts'),
          sfn.Condition.numberLessThanJsonPath(
            '$.test_code_result.regeneration_attempt',
            '$.test_code_result.regeneration_max_attempts',
          ),
        ),
        computeNextRegenerationAttempt,
      )
      .otherwise(forceTestReviewApproval);

    // P32 D2: the loop-back edge. ComputeNext → Increment → CreateCode, all
    // inside the same execution: no new execution, no executions-table row and
    // no new run-id prefix per attempt (`LGR-21a1b5b-2`, `LGR-21a1b5b-4`), and
    // one writer keeps holding the pipeline lock (`LGR-21a1b5b-1`).
    computeNextRegenerationAttempt.next(incrementRegenerationAttempt);
    incrementRegenerationAttempt.next(createCode);

    // Approve-anyway resumes at CheckCodeApproval — the SAME next state as a
    // passed test (R4.2). Rejection needs no wiring here: the approvals
    // handler fails the task token (ApprovalRejected) and the state's Catch
    // routes to RecordFailure like every other rejected approval gate.
    forceTestReviewApproval.next(checkCodeApproval);

    // ── Chain: CheckCodeApproval branching ──────────────────────────
    // p34 R4.1-4.4: fail CLOSED. The permissive branch fires ONLY on an
    // explicit boolean false; every other shape — absent, null, string,
    // number, object — falls to the Default, which is a human. Before this
    // change the residual branch was CreateInfrastructure, and this was the
    // only Choice in the file of which that was true.
    //
    // IsPresent gates existence, IsBoolean gates type, BooleanEquals decides.
    // Same conjunction shape as the two guards already live in this file:
    // CheckRegenerationAttempt and CheckJobTimeoutConfig (both
    // And[IsPresent, IsNumeric, ...] over a routinely-absent field), plus
    // TestResultChoice's And[IsPresent, StringEquals] over a nested path.
    // Do NOT reorder the conjuncts and do NOT drop IsBoolean: that in-repo
    // precedent is what the no-States.Runtime claim (R4.4) rests on. The
    // published Choice documentation confirms that And / IsPresent / IsBoolean
    // are supported operators and that rules are examined in listed order, but
    // it does NOT state that And short-circuits, and the live precedent covers
    // the ABSENT case rather than a wrong-type value (design D4, §5 risk 2).
    //
    // Topology is unchanged by the inversion: the same two edges, swapped ends.
    // forceTestReviewApproval.next(checkCodeApproval) above is untouched, so
    // approve-anyway still resumes here (R5.3).
    checkCodeApproval
      .when(
        sfn.Condition.and(
          sfn.Condition.isPresent('$.test_code_result.approval_required'),
          sfn.Condition.isBoolean('$.test_code_result.approval_required'),
          sfn.Condition.booleanEquals('$.test_code_result.approval_required', false),
        ),
        createInfrastructure,
      )
      .otherwise(waitForCodeApproval);

    // ── Wire up transitions (original linear chain) ────────────────
    fileValidation.next(schemaExtract);
    schemaExtract.next(schemaCompare);
    schemaCompare.next(checkSchemaAction);
    reuseExistingMapping.next(checkSchemaMappingApproval);
    schemaMapping.next(checkSchemaMappingApproval);
    // P32 D2: an approved schema mapping reaches CreateCode through the
    // regeneration-counter guard, same as the no-approval branch above.
    waitForSchemaMappingApproval.next(checkRegenerationAttempt);
    createCode.next(testCode);
    // P28 R4.1: TestCode no longer flows straight to the approval Choice —
    // TestResultChoice gates on the structured result first.
    testCode.next(testResultChoice);
    waitForCodeApproval.next(createInfrastructure);
    createInfrastructure.next(createGluePipeline);
    createGluePipeline.next(createQualityChecks);
    // P28 R5.5: the timeout guard sits between CreateQualityChecks and
    // RunPipeline so TimeoutSecondsPath always resolves (see the guard above).
    createQualityChecks.next(checkJobTimeoutConfig);

    // RunPipeline → ReleaseLockSuccess → UpdateSuccessState (terminal)
    runPipeline.next(releaseLockSuccess);
    releaseLockSuccess.next(updateSuccessState);

    // Wire failure path through lock release before terminating.
    // RecordFailure itself catches States.ALL (discarding the recorder's own
    // error so $.error keeps the ORIGINAL failure): a recorder crash must
    // never mask the original failure or skip lock release / PipelineFailed.
    recordFailure.next(releaseLockFailure);
    recordFailure.addCatch(releaseLockFailure, {
      errors: ['States.ALL'],
      resultPath: sfn.JsonPath.DISCARD,
    });
    releaseLockFailure.next(pipelineFailed);

    // ── Full chain starts at FileValidation ────────────────────────
    const definition = fileValidation;

    // Execution-history logging (AwsSolutions-SF1) + X-Ray tracing
    // (AwsSolutions-SF2) for the pipeline state machine.
    const sfnLogGroup = new cdk.aws_logs.LogGroup(this, 'StateMachineLogs', {
      logGroupName: `/aws/vendedlogs/states/${smName}`,
      retention: cdk.aws_logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.stateMachine = new sfn.StateMachine(this, 'AgenticDataIngestionStateMachine', {
      stateMachineName: smName,
      role: sfnRole,
      definitionBody: sfn.DefinitionBody.fromChainable(definition),
      // A TIMED_OUT execution fires SfnTerminalLockRelease like an ABORTED one,
      // so this is also what guarantees the lock is released by a path that
      // checks ownership rather than by the TTL alone.
      timeout: cdk.Duration.seconds(EXECUTION_TIMEOUT_SECONDS),
      tracingEnabled: true,
      logs: {
        destination: sfnLogGroup,
        // ALL (not ERROR): satisfies AwsSolutions-SF1 and gives operators the
        // full execution history for troubleshooting; retention capped at one
        // month above to bound the log-storage cost.
        level: sfn.LogLevel.ALL,
        includeExecutionData: false,
      },
    });

    // EventBridge rule: release lock on ABORTED/FAILED/TIMED_OUT executions
    // This catches cancellations which don't trigger any SFN catch blocks.
    // A dashboard cancel of a run whose Glue job is running relies on this rule
    // for its timing: the dashboard stops the Glue job and leaves the execution
    // waiting, and job_completion aborts it only once Glue reports the job
    // stopped, so the lock outlives the job (cancel_run, handlers/pipelines_runs.py).
    const sfnTerminalRule = new cdk.aws_events.Rule(this, 'SfnTerminalLockRelease', {
      ruleName: `agentic-data-ingestion-sfn-lock-release-${environment}`,
      eventPattern: {
        source: ['aws.states'],
        detailType: ['Step Functions Execution Status Change'],
        detail: {
          status: ['ABORTED', 'FAILED', 'TIMED_OUT'],
          stateMachineArn: [this.stateMachine.stateMachineArn],
        },
      },
    });
    sfnTerminalRule.addTarget(new cdk.aws_events_targets.LambdaFunction(lockReleaseFn));

    // ── EventBridge Inline Lambda Functions + Roles ─────────────────
    const inlineLambdaCode = (code: string) => lambda.Code.fromInline(code);

    // Import event bus by name to avoid circular cross-stack references
    const eventBus = events.EventBus.fromEventBusName(this, 'ImportedEventBus', `agentic-data-ingestion-pipeline-events-${environment}`);

    // P28 R8.1 (task 14.1): the former PipelineStateUpdateFunction (an
    // EventBridge-triggered mirror of 'Pipeline State Change' events into the
    // state table) was REMOVED as dead code. It wrote with a wrong key schema
    // (pipeline_id instead of run_id) so every write since inception threw a
    // ValidationException — nothing ever depended on its output. State rows
    // are written directly by the step Lambdas, which are the dashboard's
    // source of truth. The PUBLISHING side (eventbridge_publisher.py
    // publish_pipeline_state_change) stays intact for other consumers.
    // The PipelineStateChangeRule that existed solely to invoke it was
    // removed with it (see the EventBridge Rules section below).

    // Approval Status Update
    const approvalStatusUpdateRole = new iam.Role(this, 'ApprovalStatusUpdateRole', {
      roleName: `agentic-data-ingestion-approval-status-updater-role-${environment}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    approvalStatusUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem', 'dynamodb:GetItem'],
      resources: [data.approvalsTable.tableArn],
    }));

    const approvalStatusUpdateFn = new lambda.Function(this, 'ApprovalStatusUpdateFunction', {
      functionName: `agentic-data-ingestion-approval-status-updater-${environment}`,
      description: 'Update DynamoDB with approval status changes from EventBridge',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      role: approvalStatusUpdateRole,
      code: inlineLambdaCode(`import json, boto3, os, logging
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource('dynamodb')
approvals_table = dynamodb.Table(os.environ['APPROVALS_TABLE_NAME'])
def lambda_handler(event, context):
    detail = event['detail']
    approval_id = detail['approval_id']
    status = detail['status']
    timestamp = detail['timestamp']
    update_expr = "SET #status = :status, last_updated = :timestamp"
    expr_vals = {':status': status, ':timestamp': timestamp}
    expr_names = {'#status': 'status'}
    if 'approver' in detail:
        update_expr += ", approver = :approver"
        expr_vals[':approver'] = detail['approver']
    # Only mirror status onto an EXISTING approval record. Without attribute_exists,
    # update_item UPSERTS a partial "ghost" row (approval_id/status/last_updated only)
    # because these events carry a run_id-keyed approval_id (from schema_mapping /
    # create_code) that never has a canonical put_item record.
    try:
        approvals_table.update_item(Key={'approval_id': approval_id}, UpdateExpression=update_expr, ExpressionAttributeValues=expr_vals, ExpressionAttributeNames=expr_names, ConditionExpression='attribute_exists(approval_id)')
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            logger.info(f"No existing approval {approval_id}; skipping status mirror (no ghost created)")
        else:
            raise
    return {'statusCode': 200}`),
      environment: {
        APPROVALS_TABLE_NAME: data.approvalsTable.tableName,
        ENVIRONMENT: environment,
      },
    });

    // MCP Metrics Update
    const mcpMetricsUpdateRole = new iam.Role(this, 'MCPMetricsUpdateRole', {
      roleName: `agentic-data-ingestion-mcp-metrics-updater-role-${environment}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    mcpMetricsUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));

    const mcpMetricsUpdateFn = new lambda.Function(this, 'MCPMetricsUpdateFunction', {
      functionName: `agentic-data-ingestion-mcp-metrics-updater-${environment}`,
      description: 'Update CloudWatch metrics with MCP operation performance',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      role: mcpMetricsUpdateRole,
      code: inlineLambdaCode(`import json, boto3, os, logging
from datetime import datetime
logger = logging.getLogger()
logger.setLevel(logging.INFO)
cloudwatch = boto3.client('cloudwatch')
def lambda_handler(event, context):
    detail = event['detail']
    operation_name = detail['operation_name']
    status = detail['status']
    response_time_ms = detail['response_time_ms']
    metrics = [
        {'MetricName': 'MCPOperationLatency', 'Dimensions': [{'Name': 'Operation', 'Value': operation_name}, {'Name': 'Status', 'Value': status}, {'Name': 'Environment', 'Value': os.environ['ENVIRONMENT']}], 'Value': response_time_ms, 'Unit': 'Milliseconds', 'Timestamp': datetime.utcnow()},
        {'MetricName': 'MCPOperationCount', 'Dimensions': [{'Name': 'Operation', 'Value': operation_name}, {'Name': 'Status', 'Value': status}, {'Name': 'Environment', 'Value': os.environ['ENVIRONMENT']}], 'Value': 1, 'Unit': 'Count', 'Timestamp': datetime.utcnow()}
    ]
    cloudwatch.put_metric_data(Namespace='AgenticDataIngestion/MCP', MetricData=metrics)
    return {'statusCode': 200}`),
      environment: { ENVIRONMENT: environment },
    });

    // Glue Job State Update
    const glueJobStateUpdateRole = new iam.Role(this, 'GlueJobStateUpdateRole', {
      roleName: `agentic-data-ingestion-glue-job-state-updater-role-${environment}`,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    glueJobStateUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:UpdateItem', 'dynamodb:GetItem'],
      resources: [data.stateTable.tableArn, data.pipelinesTable.tableArn],
    }));
    glueJobStateUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));
    glueJobStateUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['events:PutEvents'],
      resources: [eventBus.eventBusArn],
    }));
    // The updater reads a job's `--pipeline-id` default argument to find the
    // pipeline it belongs to (see resolve_pipeline_id in the handler).
    // NAMING FENCE (p31 C3): both job-name forms, as on every project Glue grant.
    glueJobStateUpdateRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetJob'],
      resources: [
        `arn:aws:glue:${this.region}:${this.account}:job/agentic_data_ingestion*`,
        `arn:aws:glue:${this.region}:${this.account}:job/agentic-data-ingestion-*`,
      ],
    }));

    const glueJobStateUpdateFn = new lambda.Function(this, 'GlueJobStateUpdateFunction', {
      functionName: `agentic-data-ingestion-glue-job-state-updater-${environment}`,
      description: 'Update DynamoDB and publish metrics for Glue job state changes',
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.lambda_handler',
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      role: glueJobStateUpdateRole,
      // The handler below is Python inside a TypeScript template literal.
      // tests/cdk/stacks/test_glue_job_state_updater_inline.py extracts and runs
      // it, and refuses it if it contains a backslash or a dollar-brace, which
      // the template literal would rewrite before deploy.
      code: inlineLambdaCode(`import boto3, os, logging
from datetime import datetime, timezone
from botocore.exceptions import ClientError
logger = logging.getLogger()
logger.setLevel(logging.INFO)
dynamodb = boto3.resource('dynamodb')
cloudwatch = boto3.client('cloudwatch')
glue = boto3.client('glue')
pipelines_table = dynamodb.Table(os.environ.get('PIPELINES_TABLE', 'agentic_data_ingestion_pipelines'))
JOB_PREFIX = 'agentic-data-ingestion-job-'
def resolve_pipeline_id(job_name):
    # The job's --pipeline-id default argument names its pipeline. job_builders
    # sets it on every job, and a versioned job (JOB_PREFIX + id + '-v<N>',
    # schema_evolution.create_versioned_job_name) copies the same config, so it
    # resolves to the pipeline, not to '<id>-v<N>'. The name is only a fallback
    # when the job cannot be read; a wrong id there writes nothing (see below).
    try:
        args = glue.get_job(JobName=job_name)['Job'].get('DefaultArguments') or {}
        pipeline_id = args.get('--pipeline-id') or args.get('--pipeline_id')
        if pipeline_id:
            return pipeline_id
        logger.warning(f'Job {job_name} has no --pipeline-id argument; using its name')
    except Exception as e:
        logger.warning(f'Could not read job {job_name} ({e}); using its name')
    return job_name[len(JOB_PREFIX):]
def lambda_handler(event, context):
    detail = event['detail']
    job_name = detail.get('jobName', '')
    state = detail.get('state', '')
    job_run_id = detail.get('jobRunId', '')
    if not job_name.startswith(JOB_PREFIX):
        return {'statusCode': 200, 'body': 'Skipped'}
    pipeline_id = resolve_pipeline_id(job_name)
    now = datetime.now(timezone.utc)
    # Update an EXISTING pipeline record only. UpdateItem creates a missing item,
    # so without the condition a late event for a deleted pipeline (its versioned
    # jobs outlive the delete) wrote a partial record that the dashboard lists.
    # Every failure here is logged and acknowledged, deliberately: these three
    # fields are informational (no runtime reader), and a retry would publish the
    # metric below twice.
    try:
        pipelines_table.update_item(
            Key={'pipeline_id': pipeline_id},
            UpdateExpression='SET glue_job_last_run = :ts, glue_job_last_status = :st, glue_job_last_run_id = :rid',
            ConditionExpression='attribute_exists(pipeline_id)',
            ExpressionAttributeValues={':ts': now.isoformat(), ':st': state, ':rid': job_run_id},
        )
    except ClientError as e:
        if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
            logger.info(f'No pipeline record {pipeline_id} for job {job_name}; not recreating it')
        else:
            logger.error(f'Error updating pipelines table: {e}')
    except Exception as e:
        logger.error(f'Error updating pipelines table: {e}')
    env = os.environ.get('ENVIRONMENT', 'dev')
    metric_name = {'SUCCEEDED': 'GlueJob.Succeeded', 'FAILED': 'GlueJob.Failed', 'TIMEOUT': 'GlueJob.Timeout', 'RUNNING': 'GlueJob.Executed'}.get(state, 'GlueJob.StateChange')
    try:
        cloudwatch.put_metric_data(Namespace='AgenticDataIngestion/Pipeline', MetricData=[{'MetricName': metric_name, 'Dimensions': [{'Name': 'PipelineId', 'Value': pipeline_id}, {'Name': 'Environment', 'Value': env}], 'Value': 1, 'Unit': 'Count', 'Timestamp': now}])
    except Exception as e:
        logger.error(f'Error publishing metrics: {e}')
    return {'statusCode': 200}`),
      environment: {
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        STATE_TABLE_NAME: data.stateTable.tableName,
        ENVIRONMENT: environment,
      },
    });

    // ── EventBridge Rules ──────────────────────────────────────────
    // Import DLQ by ARN to avoid circular cross-stack references
    const dlq = sqs.Queue.fromQueueArn(this, 'ImportedDLQ',
      `arn:aws:sqs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:agentic-data-ingestion-eventbridge-dlq-${environment}`);
    const dlqTarget = { deadLetterQueue: dlq };

    // P28 R8.1 (task 14.1): PipelineStateChangeRule removed — its only
    // target was the deleted PipelineStateUpdateFunction (see comment above
    // the Approval Status Update section). 'Pipeline State Change' events
    // are still published; no rule in this stack needs to consume them.

    new events.Rule(this, 'ApprovalStatusChangeRule', {
      ruleName: `agentic-data-ingestion-approval-status-changes-${environment}`,
      description: 'Capture approval status change events for dashboard updates',
      eventBus,
      eventPattern: {
        source: ['agentic_data_ingestion.pipeline'],
        detailType: ['Approval Status Change'],
        detail: { status: ['pending', 'approved', 'rejected', 'timeout'] },
      },
      targets: [new targets.LambdaFunction(approvalStatusUpdateFn, {
        retryAttempts: 3,
        ...dlqTarget,
      })],
    });

    new events.Rule(this, 'MCPOperationRule', {
      ruleName: `agentic-data-ingestion-mcp-operations-${environment}`,
      description: 'Capture MCP operation events for performance monitoring',
      eventBus,
      eventPattern: {
        source: ['agentic_data_ingestion.pipeline'],
        detailType: ['MCP Operation'],
        detail: { status: ['success', 'failed', 'fallback'] },
      },
      targets: [new targets.LambdaFunction(mcpMetricsUpdateFn, {
        retryAttempts: 3,
        ...dlqTarget,
      })],
    });

    new events.Rule(this, 'GlueJobStateChangeRule', {
      ruleName: `agentic-data-ingestion-glue-job-state-changes-${environment}`,
      description: 'Capture Glue job state changes for dashboard updates and monitoring',
      eventPattern: {
        source: ['aws.glue'],
        detailType: ['Glue Job State Change'],
        detail: {
          state: ['SUCCEEDED', 'FAILED', 'TIMEOUT', 'STOPPED', 'RUNNING'],
          jobName: [{ prefix: GLUE_JOB_NAME_PREFIX }],
        },
      },
      targets: [new targets.LambdaFunction(glueJobStateUpdateFn, {
        retryAttempts: 3,
        ...dlqTarget,
      })],
    });

    // Glue Job Monitor Rule → triggers compute.jobCompletionLambda
    //
    // P28 R5.3/R5.4 (task 6.3):
    // • The event pattern is scoped to THIS project's job-name prefix — the
    //   rule no longer fires for every Glue job in the account.
    // • A lost completion event means a stranded execution, so it must never
    //   be silent. Two different failures can lose one, and both end in the
    //   dedicated DLQ below, whose depth alarm (GlueJobMonitorDlqDepthAlarm,
    //   alarms section) pages via the critical-alerts topic:
    //   1. EventBridge cannot hand the event to Lambda (throttled or
    //      unavailable). The target's retryAttempts/maxEventAge/deadLetterQueue
    //      govern this, and only this.
    //   2. The function runs and fails. EventBridge invokes Lambda
    //      asynchronously, so this is Lambda's async retry, not EventBridge's:
    //      two more attempts, then the on-failure destination set by
    //      JobCompletionAsyncInvokeConfig below. job_completion raises on
    //      every failure it cannot finish (RecordNotFoundError for the GSI
    //      race, and any AWS error), so this path carries them.
    //   Until 2026-10-10 only (1) was configured, and these comments claimed
    //   it also covered (2); a failed invocation was retried twice and then
    //   discarded, never reaching the DLQ.
    const glueJobMonitorDlq = new sqs.Queue(this, 'GlueJobMonitorDLQ', {
      enforceSSL: true, // deny non-TLS access (AwsSolutions-SQS4)
      queueName: `agentic-data-ingestion-glue-job-monitor-dlq-${environment}`,
      retentionPeriod: cdk.Duration.days(14),
    });

    new events.Rule(this, 'GlueJobMonitorRule', {
      ruleName: `agentic-data-ingestion-glue-job-monitor-${environment}`,
      description: 'Trigger Lambda when Glue jobs complete',
      eventPattern: {
        source: ['aws.glue'],
        detailType: ['Glue Job State Change'],
        detail: {
          state: ['SUCCEEDED', 'FAILED', 'STOPPED', 'TIMEOUT'],
          jobName: [{ prefix: GLUE_JOB_NAME_PREFIX }],
        },
      },
      targets: [new targets.LambdaFunction(compute.jobCompletionLambda, {
        // An hour of redelivery is orders of magnitude beyond the residual
        // completion-event race (run_pipeline persists the token BEFORE
        // start_job_run since task 6.1); anything still failing after that
        // is a real fault and belongs in the DLQ, not in an infinite retry.
        retryAttempts: 8,
        maxEventAge: cdk.Duration.hours(1),
        deadLetterQueue: glueJobMonitorDlq,
      })],
    });

    // Failure stage (2) above: the function's asynchronous invoke config.
    // Two retries is Lambda's maximum and its default; the retries come 1 and
    // 2 minutes after the first attempt, far longer than the GlueJobRunIndex
    // propagation race RecordNotFoundError covers. Six hours (the maximum) is
    // how long an event may wait in Lambda's queue while the function is
    // throttled: a late completion still closes its run, a dropped one never
    // does. Records that exhaust both reach the DLQ as Lambda destination
    // records (requestPayload + responsePayload), beside EventBridge's own
    // dead-letter messages, and the depth alarm counts both.
    //
    // L1 rather than lambda.EventInvokeConfig with SqsDestination: that
    // destination grants SendMessage on the role, and the role lives in
    // Foundation, so the grant would make Foundation depend on this stack.
    // The grant is in foundation-stack.ts, by queue name (LGR-b46a5bb-3).
    new lambda.CfnEventInvokeConfig(this, 'JobCompletionAsyncInvokeConfig', {
      functionName: compute.jobCompletionLambda.functionName,
      qualifier: '$LATEST',
      maximumRetryAttempts: 2,
      maximumEventAgeInSeconds: cdk.Duration.hours(6).toSeconds(),
      destinationConfig: { onFailure: { destination: glueJobMonitorDlq.queueArn } },
    });

    // Approval Timeout scheduled rule
    new events.Rule(this, 'ApprovalTimeoutRule', {
      ruleName: `agentic-data-ingestion-approval-timeout-${environment}`,
      description: 'Trigger approval timeout handler every 15 minutes',
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      targets: [new targets.LambdaFunction(compute.approvalTimeoutHandlerLambda)],
    });

    // ── JDBC Discovery Cleanup scheduled rule (P17/P23) ───────────
    // Daily at 03:00 UTC, invoke the dashboard API Lambda to run the
    // `cleanup_discovery_resources` janitor (reaps wizard-time JDBC
    // discovery crawlers/connections/tables older than 7 days). The
    // dashboard handler dispatches on `event["source"] == "aws.events"`
    // → cleanup_discovery_resources (lambda_function.py). Passing the
    // Lambda as a `targets.LambdaFunction` auto-synthesizes the single
    // AWS::Lambda::Permission (events.amazonaws.com → InvokeFunction,
    // SourceArn = this rule's ARN); do NOT also call addPermission, which
    // would create a duplicate permission.
    new events.Rule(this, 'DiscoveryCleanupRule', {
      ruleName: `agentic-data-ingestion-discovery-cleanup-${environment}`,
      description: 'Daily reap of stale JDBC discovery crawlers/connections/tables',
      schedule: events.Schedule.cron({ minute: '0', hour: '3' }),
      targets: [new targets.LambdaFunction(compute.dashboardApiLambda)],
    });

    // ── SQS Pipeline Trigger Queue ────────────────────────────────
    // Dead Letter Queue for messages that exhaust the redelivery budget of the
    // queue below. That is after **1000** failed receives (`maxReceiveCount`),
    // raised from 50 by p54 task 7.7 — and not 3, the count this comment
    // claimed until task 7.6 read it. Retention here is this queue's own 14
    // days, independent of the main queue's 7.
    //
    // **14 days is transport retention, not the record.** R10.3 requires a lost
    // file's record to outlive the window in which the loss happened, and this
    // queue cannot carry it: the message is gone after a fortnight. The durable
    // record is the executions-table row `QueueLossRecorderFunction` writes,
    // whose TTL is `LOST_FILE_RECORD_TTL_SECONDS` (`shared/config.py`).
    const pipelineTriggerDlq = new sqs.Queue(this, 'PipelineTriggerDLQ', {
 enforceSSL: true, // deny non-TLS access (AwsSolutions-SQS4)
      queueName: `agentic-data-ingestion-pipeline-triggers-dlq-${environment}`,
      retentionPeriod: cdk.Duration.days(14),
    });

    // Queue for buffering S3 upload events before triggering Step Functions.
    //
    // ── THE BUDGET IS THE PRODUCT, NOT THE VISIBILITY TIMEOUT ──────
    //
    // This comment used to read "visibility timeout must be long enough for a
    // full pipeline execution (~15 min)". The visibility timeout is **30 s**
    // (below); the ~15-minute figure was attributed to the wrong constant. What
    // buys the wait is `maxReceiveCount × visibilityTimeout` — since p54 task
    // 7.7, 1000 × 30 s = 30000 s = **8 h 20 min** (it was 50 × 30 s = 25 min).
    // A deferred file gets that long to find the per-pipeline lock free; past it
    // SQS moves the message to the DLQ above. `maxReceiveCount` is high for
    // exactly that reason — each "receive" is a retry after the visibility
    // timeout, so the count IS the budget's length.
    //
    // ── THE BUDGET WAS NEVER SIZED FOR A RUN WAITING ON A HUMAN ────
    //
    // The old sentence was true of the case it was written for: an UNATTENDED
    // run of ~15 min fits inside 25. It is not true of a run parked at an
    // approval gate. That gate is APPROVAL_TIMEOUT_SECONDS — 72 h, and
    // `LGR-69ca78b-2` owns the number — and `LGR-5d68eeb-4` records that
    // `timeout_behavior` is "manual" in every deployment with no producer at
    // all, so nothing releases a parked approval before the gate elapses. The
    // lock is held for that whole window.
    //
    // ── p54 R10.2 IS STILL UNSATISFIED, AND BY HOW MUCH ────────────
    //
    // R10.2: the redelivery budget MUST NOT be shorter than the lock hold. It
    // is still shorter, and these are the numbers rather than an argument:
    //
    //   covering 72 h at a 30 s visibility timeout needs   8640 receives
    //   `maxReceiveCount` is capped by SQS at              1000 receives
    //   so the budget reaches                              30000 s ≈ 8 h 20 min
    //   of                                               259200 s = 72 h
    //
    // 11.6% covered; a 63 h 40 min shortfall. It was 0.6% covered before, so
    // this is a 20x reduction in exposure and NOT a fix. R10.2 is therefore
    // recorded as UNSATISFIED rather than quietly claimed, and the residual is
    // made VISIBLE instead: `QueueLossRecorderFunction` below writes a durable
    // record naming the file, and `ExecutionsPage` renders it as "Failed (DLQ)"
    // (p54 arm (d) + arm (a), ratified). The cross-language contract test
    // `tests/contracts/test_pipeline_trigger_redelivery_budget_cross_language.py`
    // parses these two literals and `APPROVAL_TIMEOUT_SECONDS` out of this file
    // and carries the inequality as an `xfail` with the arithmetic above — so
    // the gap is asserted, not merely commented, and closing it flips that test
    // to an unexpected pass.
    //
    // **`visibilityTimeout` deliberately stays at 30 s.** The two multiply, so
    // raising it also buys budget — and it is the wrong lever: at 900 s every
    // deferral idles up to 15 minutes even when the lock clears in seconds,
    // which regresses the 6–7 minute serialisation `mynewtesting` is observed
    // doing today. It is also read by the Lambda-timeout ratio below
    // (`queueVisibilityTimeout >= lambdaTimeout * 6`), so moving it moves two
    // things. Closing R10.2 by geometry alone is impossible anyway; the arm
    // that closes it structurally is (c), a durable pending-files table, and
    // `LGR-21a1b5b-2` already says it goes in its OWN table.
    //
    // Two consequences of the raise, stated so neither is a surprise:
    //   - the `AgenticDataIngestion-SQS-Message-Age` alarm below fires at 15
    //     min of queue age, and a deferred file can now legitimately sit for
    //     8 h. The alarm is LEFT ALONE: surfacing a long deferral is the point
    //     of R9, and muting it would re-hide what this work exists to show.
    //   - a contended message is now received up to 1000 times rather than 50,
    //     each a trigger invocation that re-reads the lock and re-raises. At a
    //     30 s cadence for one message that is ~2 invocations/minute against
    //     `maximumConcurrency: 5`, so it does not crowd out real work.
    //
    // Redelivery is reported per record, not per batch (`LGR-21a1b5b-3`), whose
    // revisit condition is a `batchSize` change and NOT this budget — `batchSize`
    // stays 1. `LGR-21a1b5b-4`'s "an SQS rejection is a redelivery" reasoning
    // still stands; what p54 falsifies is the unwritten BOUND on that premise:
    // the rejection is a redelivery right up to the 1000th one, and not after.
    this.pipelineTriggerQueue = new sqs.Queue(this, 'PipelineTriggerQueue', {
 enforceSSL: true, // deny non-TLS access (AwsSolutions-SQS4)
      queueName: `agentic-data-ingestion-pipeline-triggers-${environment}`,
      visibilityTimeout: cdk.Duration.seconds(30),
      retentionPeriod: cdk.Duration.days(7),
      deadLetterQueue: {
        queue: pipelineTriggerDlq,
        // 1000 is SQS's documented ceiling for "Maximum receives" (valid range
        // 1–1000). Do not raise it further expecting more budget; there is none.
        maxReceiveCount: 1000,
      },
    });

    // ── S3 Upload Trigger → SQS → Trigger Lambda → Step Functions ──
    // When a file is uploaded to the source bucket, the event is queued in SQS.
    // The source bucket already has eventBridgeEnabled: true (foundation-stack),
    // so S3 "Object Created" events are delivered to the default EventBridge bus.
    // The trigger Lambda parses the raw EventBridge event from the SQS message body,
    // checks concurrency limits, and starts the Step Functions execution.
    const sourceBucketName = foundation.sourceBucket.bucketName;

    new events.Rule(this, 'S3UploadTriggerRule', {
      ruleName: `agentic-data-ingestion-s3-upload-trigger-${environment}`,
      description: 'Queue S3 upload events in SQS for concurrency-controlled pipeline triggering',
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [sourceBucketName] },
          object: {
            key: [{
              'anything-but': {
                'suffix': '/',
              },
            }] as any[],
          },
        } as any,
      },
      targets: [new targets.SqsQueue(this.pipelineTriggerQueue)],
    });

    // ── Pipeline Trigger Lambda (SQS → concurrency check → SFN) ──
    // Moved from compute-stack to orchestration-stack to eliminate cross-stack
    // string references. The trigger Lambda is tightly coupled to the SQS queue
    // and state machine — it belongs in the orchestration layer.
    const lambdaCodePath = (fnName: string) =>
      path.join(__dirname, '..', '..', 'lambda', fnName);

    // Lightweight Powertools-only layer for the trigger Lambda.
    // Built locally instead of importing compute.strandsLayer to avoid
    // CDK cross-stack export deadlocks when the Strands layer hash changes.
    // P28 task 8.1: the layer also carries shared/*.py (rerun_contract and
    // friends) so the trigger Lambda can import the shared trigger-input
    // contract from /opt/python — same mechanism as the Strands layer.
    const powertoolsLayer = new lambda.LayerVersion(this, 'PowertoolsTriggerLayer', {
      layerVersionName: `powertools-trigger-layer-${props.environment}`,
      description: 'aws-lambda-powertools + shared modules for pipeline trigger Lambda',
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..'), {
        // The bundling command below produces the layer via `pip install`
        // into /asset-output plus a copy of shared/*.py from /asset-input.
        // Pointing the asset at the repo root matters for fingerprinting
        // (shared/ changes must rebuild the layer), and without an exclude
        // CDK walks the entire tree (node_modules, .venv, cdk.out — tens of
        // GB), which makes synth extremely slow / OOM and hangs CDK tests.
        // Exclude the heavy, irrelevant directories so fingerprinting is cheap.
        //
        // IgnoreMode.GIT rather than the GLOB default: under GLOB the bare
        // `cdk.out` entry below matched `deploy-cdk/cdk.out` but NOT the parked
        // `deploy-cdk/cdk.out.stale-*` siblings, so this fingerprint walk was
        // still hashing tens of GiB of previous synth output on every synth.
        // GIT semantics give `cdk.out*` recursive reach, and a leading `/`
        // anchors an entry to the context root so it cannot swallow a
        // same-named directory nested under shared/.
        //
        // Bundling bind-mounts the unfiltered source at /asset-input, so
        // nothing here can starve the `cp /asset-input/shared/*.py` step — this
        // list only decides what gets fingerprinted.
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: [
          // Sibling trees. Bundling only reads shared/*.py.
          '/deploy-cdk', '/dashboard-react', '/agentcore', '/lambda', '/mcp',
          '/tests', '/docs', '/diagram', '/scripts', '/lib', '/.kiro',
          // Generated / vendored, at any depth.
          'cdk.out', 'cdk.out.*', 'node_modules', '.venv', 'venv', 'dist',
          '*.tsbuildinfo', '*.egg-info',
          // Caches, at any depth.
          '__pycache__', '*.pyc', '*.pyo', '.mypy_cache', '.ruff_cache',
          '.pytest_cache', '.hypothesis', '.coverage', '.coverage.*', 'htmlcov',
          // VCS / editor / local env.
          '.git', '.DS_Store', '*.swp', '.env', '.env.*',
        ],
        bundling: {
          image: pinnedBuildImage(SAM_PYTHON312_ARM64_BUILD_IMAGE),
          command: ['bash', '-c',
            'pip install aws-lambda-powertools' +
            ' --python-version 3.12' +
            ' --platform manylinux2014_aarch64' +
            ' --target /asset-output/python' +
            ' --only-binary=:all:' +
            ' --no-cache-dir --upgrade' +
            // Shared modules at /opt/python — the trigger Lambda imports
            // rerun_contract (→ step_names) for the SFN trigger-input
            // contract (P28 R9.2). Same copy step as the Strands layer.
            ' && cp -f /asset-input/shared/*.py /asset-output/python/' +
            ' && find /asset-output/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true',
          ],
        },
      }),
    });

    this.pipelineTriggerLambda = new lambda.Function(this, 'PipelineTrigger', {
      functionName: 'agentic_data_ingestion_pipeline_trigger',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambda.Code.fromAsset(lambdaCodePath('agentic_data_ingestion_pipeline_trigger'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: LAMBDA_CODE_ASSET_EXCLUDE,
      }),
      role: foundation.pipelineTriggerRole,
      // Lambda timeout must satisfy: queueVisibilityTimeout >= lambdaTimeout * 6
      // Queue visibility = 30s, so max Lambda timeout = 30 / 6 = 5s
      // Trigger Lambda does lightweight work (DynamoDB lock check + SFN start_execution)
      timeout: cdk.Duration.seconds(5),
      memorySize: 256,
      layers: [compute.adotLayer, powertoolsLayer],
      reservedConcurrentExecutions: 5,
      environment: {
        MAX_CONCURRENT_EXECUTIONS: '10',
        EXECUTIONS_TABLE: data.executionsTable.tableName,
        LOCKS_TABLE: data.pipelineLocksTable.tableName,
        // p30 R9.2: the trigger READS the Processed_File_Ledger before starting
        // a run (job_completion writes it). Checked before the pipeline lock is
        // acquired, so a duplicate skip leaves no lock to release (R10.3).
        PROCESSED_FILES_TABLE: data.processedFilesTable.tableName,
        STATE_MACHINE_ARN: this.stateMachine.stateMachineArn,
        SOURCE_BUCKET: foundation.sourceBucket.bucketName,
      },
    });

    // Grant trigger Lambda read/write access to pipeline locks table for dedup
    data.pipelineLocksTable.grantReadWriteData(foundation.pipelineTriggerRole);

    // Grant EventBridge permission to invoke the Pipeline_Trigger_Lambda (for scheduled triggers)
    this.pipelineTriggerLambda.addPermission('EventBridgeScheduleInvoke', {
      principal: new iam.ServicePrincipal('events.amazonaws.com'),
      action: 'lambda:InvokeFunction',
      sourceArn: `arn:aws:events:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:rule/agentic-data-ingestion-schedule-*`,
    });

    // Grant trigger Lambda S3 list permission for scheduled trigger file discovery
    foundation.sourceBucket.grantRead(this.pipelineTriggerLambda);

    // Observability: ADOT + Powertools
    this.pipelineTriggerLambda.addEnvironment('AWS_LAMBDA_EXEC_WRAPPER', '/opt/otel-instrument');
    this.pipelineTriggerLambda.addEnvironment('OTEL_RESOURCE_ATTRIBUTES', 'service.name=agentic-data-ingestion-PipelineTrigger');
    this.pipelineTriggerLambda.addEnvironment('OTEL_PYTHON_DISTRO', 'aws_distro');
    this.pipelineTriggerLambda.addEnvironment('OTEL_PYTHON_CONFIGURATOR', 'aws_configurator');
    this.pipelineTriggerLambda.addEnvironment('OTEL_EXPORTER_OTLP_PROTOCOL', 'http/protobuf');
    this.pipelineTriggerLambda.addEnvironment('OTEL_SDK_DISABLED', 'false');
    this.pipelineTriggerLambda.addEnvironment('POWERTOOLS_ENABLED', 'true');
    this.pipelineTriggerLambda.addEnvironment('POWERTOOLS_SERVICE_NAME', 'agentic_data_ingestion');
    this.pipelineTriggerLambda.addEnvironment('POWERTOOLS_LOG_LEVEL', 'INFO');

    // Wire trigger Lambda to SQS queue.
    // Use L1 CfnEventSourceMapping to avoid auto-granting SQS permissions on the
    // Foundation role (which would create a cyclic cross-stack reference).
    // The Foundation role already has wildcard SQS permissions for agentic-data-ingestion-pipeline-triggers-*.
    // Unlike the previous hardcoded function name, we use the Lambda's direct functionArn reference.
    new lambda.CfnEventSourceMapping(this, 'TriggerSqsMapping', {
      functionName: this.pipelineTriggerLambda.functionArn,
      eventSourceArn: this.pipelineTriggerQueue.queueArn,
      batchSize: 1,
      scalingConfig: { maximumConcurrency: 5 },
      enabled: true,
      // p30 R9.3: this is the half that makes the handler's
      // `batchItemFailures` return value mean anything. Without it Lambda
      // ignores the response entirely and re-delivers the WHOLE batch when the
      // handler reports any failure, so already-started records get processed
      // twice. Both halves are required; neither works alone.
      //
      // On the L1 construct the property is `functionResponseTypes` (CFN
      // FunctionResponseTypes), not the L2 SqsEventSource's
      // `reportBatchItemFailures: true`. Same feature, different spelling —
      // this mapping is L1 to avoid the cyclic cross-stack reference that
      // auto-granting SQS permissions on the Foundation role would create.
      functionResponseTypes: ['ReportBatchItemFailures'],
    });

    // ── Queue Loss Recorder (p54 R10.1/R10.3, arm (d)) ─────────────
    //
    // The DLQ's consumer. When a file's message exhausts the redelivery budget
    // above, this writes the Lost_File record that makes the loss visible:
    // `ExecutionsPage.tsx`'s `failed_dlq` branch has existed with ZERO producers
    // repo-wide, and this is the producer it was written for — `LGR-eee8ac8-6`'s
    // shape used in reverse (give a reader with no producer its producer, rather
    // than delete it). Handler and full rationale:
    // lambda/agentic_data_ingestion_queue_loss_recorder/lambda_function.py.
    //
    // The row is the Queued_Placeholder shape on the same content-addressed key,
    // with `status` still "QUEUED" (do-NOT 19 / `LGR-21a1b5b-2` refuse a fourth
    // executions status) and the lost fact on a separate `queue_outcome`
    // attribute. It carries `LOST_FILE_RECORD_TTL_SECONDS` (30 days) so it
    // outlives both the 8 h 20 min loss window and this DLQ's 14-day retention —
    // R10.3's "readable for longer than the window in which the loss occurred".
    const queueLossRecorderRole = new iam.Role(this, 'QueueLossRecorderRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
    });
    // PutItem only. It writes one whole row and never reads, updates or deletes
    // — least privilege, and it is also what makes the handler incapable of
    // annotating a row into a phantom (see its module docstring).
    queueLossRecorderRole.addToPolicy(new iam.PolicyStatement({
      actions: ['dynamodb:PutItem'],
      resources: [data.executionsTable.tableArn],
    }));
    // The event source mapping needs the poller's own permissions on the DLQ.
    queueLossRecorderRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes'],
      resources: [pipelineTriggerDlq.queueArn],
    }));

    const queueLossRecorderFn = new lambda.Function(this, 'QueueLossRecorderFunction', {
      functionName: `agentic-data-ingestion-queue-loss-recorder-${environment}`,
      description: 'Record a dead-lettered upload event as a Lost_File (p54 R10.3)',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      role: queueLossRecorderRole,
      // Carries aws-lambda-powertools AND shared/*.py at /opt/python, which is
      // how `from config import LOST_FILE_RECORD_TTL_SECONDS` resolves. The TTL
      // is deliberately NOT passed as an environment variable: that would put a
      // second copy of a behavioural value in TypeScript, which is the drift
      // `tests/contracts/test_approval_window_cross_language.py` exists to
      // prevent. One home per value — shared/config.py.
      layers: [powertoolsLayer],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..', 'lambda', 'agentic_data_ingestion_queue_loss_recorder'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: LAMBDA_CODE_ASSET_EXCLUDE,
      }),
      environment: {
        EXECUTIONS_TABLE: data.executionsTable.tableName,
        POWERTOOLS_SERVICE_NAME: 'agentic_data_ingestion',
        POWERTOOLS_LOG_LEVEL: 'INFO',
      },
    });

    // ── THIS MAPPING IS ENABLED, AND THAT IS THE DECISION ──────────
    //
    // Consuming an SQS queue DELETES its messages. That mechanical fact is the
    // whole reason `enabled` was ever a decision rather than a default, and it
    // is why the flip below has a cost attached to it.
    //
    // The cost, precisely. `PipelineTriggerDLQ` in the validation account holds **four real
    // dead-lettered uploads**, and they are two things at once: p54 task 7.1's
    // evidence, and four files an operator could still have redriven — now with
    // 8 h 20 min of budget rather than the 25 min they originally exhausted, so
    // a redrive might genuinely have succeeded. **The next deploy of the
    // Orchestration stack turns this mapping on, the poller drains those four,
    // and they are gone.**
    //
    // **The operator ratified that trade explicitly: accept losing the four, in
    // exchange for R10.1 being live.** So this is a choice on the record — not
    // an oversight, and not a default nobody examined. It is what moves R10.1
    // from satisfied-in-potential-behind-a-flag (the state task 7.7 shipped) to
    // satisfied for every real loss from that deploy forward.
    //
    // Two things this is NOT a licence to do, stated because enabling a consumer
    // and draining a queue by hand are easy to confuse:
    //   - the four are consumed **on deploy**, by the poller, not now and not by
    //     hand. p54 requirements put reading, purging and redriving the DLQ out
    //     of scope and that is unchanged.
    //   - nothing here redrives or resubmits. The handler writes the Lost_File
    //     record and stops; who presses a redrive button is still out of scope.
    //
    // Everything else — handler, role, batchSize, per-record reporting — was in
    // place and tested before the flip, so `enabled` was the whole change. The
    // CDK assertion in `deploy-cdk/test/orchestration-stack.test.ts` was amended
    // in the same change rather than deleted, per `LGR-60c4985-6`'s shape and
    // `LGR-5906ec6-2`'s rule that a deliberate posture lives in an assertion: it
    // now pins `Enabled === true`, so turning this back off means changing a
    // test on purpose and saying why.
    const queueLossRecorderDlqMapping = new lambda.CfnEventSourceMapping(this, 'QueueLossRecorderDlqMapping', {
      functionName: queueLossRecorderFn.functionArn,
      eventSourceArn: pipelineTriggerDlq.queueArn,
      // 1, matching TriggerSqsMapping. `LGR-21a1b5b-3`'s revisit condition is a
      // batchSize change, and per-record reporting below is what keeps one bad
      // record from dragging its siblings back to the queue.
      batchSize: 1,
      enabled: true,
      // Without this the return value is inert and Lambda re-delivers the whole
      // batch on any failure — the same both-halves-required rule as
      // TriggerSqsMapping above.
      functionResponseTypes: ['ReportBatchItemFailures'],
    });

    // ── WHY THIS DEPENDENCY IS NOT OPTIONAL ────────────────────────
    //
    // `CreateEventSourceMapping` for an SQS source VALIDATES the execution
    // role's `sqs:ReceiveMessage` at create time — it is not a lazy grant the
    // poller discovers later. So without an explicit ordering constraint the
    // FIRST deploy of this stack can fail with
    //
    //   InvalidParameterValueException: The provided execution role does not
    //   have permissions to call ReceiveMessage on SQS
    //
    // and roll the stack back, then succeed on retry. A rollback-then-succeed
    // shape is the worst kind of race to leave in: it reads as flakiness.
    //
    // The reason CloudFormation does not order this on its own is that the
    // implicit dependencies stop one resource short. `functionArn` renders as an
    // `Fn::GetAtt`, so the mapping depends on the FUNCTION; the function's
    // `Role` is a `GetAtt` on `QueueLossRecorderRole`, so it depends on the
    // ROLE. But the two `queueLossRecorderRole.addToPolicy` calls above
    // (`sqs:ReceiveMessage` among them) do not live on the role resource — CDK
    // emits them as a SEPARATE `AWS::IAM::Policy`, `QueueLossRecorderRole`'s
    // DefaultPolicy, which nothing in that chain references. Confirmed against
    // the synthesized template by removing this line and re-rendering: the
    // mapping comes back with NO `DependsOn` at all.
    //
    // `node.addDependency` on the ROLE construct, not on a `findChild`
    // ('DefaultPolicy') lookup: CDK expands a construct dependency to every
    // `CfnResource` in that construct's subtree, so this covers the role AND its
    // DefaultPolicy, and it keeps working if the role's children change shape.
    // Pinned by `deploy-cdk/test/orchestration-stack.test.ts`.
    //
    // ── AND WHY `TriggerSqsMapping` ABOVE DOES NOT NEED THE SAME ───
    //
    // It is safe, but by accident of stack topology rather than by design, and
    // that is worth writing down instead of leaving a reader to re-derive it.
    // Its role is `foundation.pipelineTriggerRole` — created in the FOUNDATION
    // stack, which deploys before Orchestration (`LGR-b46a5bb-3`: "Foundation
    // owns every Lambda role, so the grants deploy with it"), carrying the
    // wildcard SQS permissions its own comment cites. So by the time
    // Orchestration creates that mapping the policy has existed for a whole
    // stack. p54's recorder is the first event source mapping in this repo whose
    // role is created in the SAME stack as the mapping, which is why the race
    // appears now and not before.
    //
    // If `pipelineTriggerRole` ever moves into this stack, or its SQS grant
    // moves out of Foundation and into an `addToPolicy` here, that mapping needs
    // this same dependency and the accident stops covering it.
    queueLossRecorderDlqMapping.node.addDependency(queueLossRecorderRole);

    // ── SNS Topics ─────────────────────────────────────────────────
    this.criticalAlertsTopic = new sns.Topic(this, 'CriticalAlertsTopic', {
 enforceSSL: true, // deny non-TLS publish (AwsSolutions-SNS3)
      topicName: `agentic-data-ingestion-critical-alerts-${environment}`,
      displayName: 'AgenticDataIngestion Critical Alerts',
    });
    if (notificationEmail) {
      this.criticalAlertsTopic.addSubscription(new subscriptions.EmailSubscription(notificationEmail));
    }
    this.criticalAlertsTopic.addToResourcePolicy(new iam.PolicyStatement({
      actions: ['sns:Publish'],
      principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
      resources: [this.criticalAlertsTopic.topicArn],
    }));

    this.approvalNotificationsTopic = new sns.Topic(this, 'ApprovalNotificationsTopic', {
 enforceSSL: true, // deny non-TLS publish (AwsSolutions-SNS3)
      topicName: `agentic-data-ingestion-approval-notifications-${environment}`,
      displayName: 'AgenticDataIngestion Approval Notifications',
    });
    if (notificationEmail) {
      this.approvalNotificationsTopic.addSubscription(new subscriptions.EmailSubscription(notificationEmail));
    }

    // ── CloudWatch Alarms ──────────────────────────────────────────
    // Percentage of finished runs that failed. Each finished run publishes one
    // Pipeline.ErrorRate datapoint with dimension {Environment}: 100 from
    // record_failure for a caught failure (approval rejections excluded), 0 from
    // job_completion for a success. So the Average over a window is the failed
    // share, any value above 20 needs at least one failure, and a window with no
    // finished run has no data, which is not breaching. See RUN_ERROR_RATE_* in
    // shared/metrics_facade.py.
    //
    // Until 2026-10-11 this read the series with NO dimensions, while the only
    // publisher (which nothing called) used {PipelineId, Status}. CloudWatch
    // treats each dimension set as its own metric, so the alarm read a series
    // nothing wrote and could never fire.
    new cloudwatch.Alarm(this, 'PipelineErrorRateAlarm', {
      alarmName: `AgenticDataIngestion-Pipeline-Error-Rate-${environment}`,
      alarmDescription: 'More than 20% of finished pipeline runs failed in 15 minutes (approval rejections excluded)',
      metric: new cloudwatch.Metric({
        namespace: 'AgenticDataIngestion/Pipeline',
        metricName: 'Pipeline.ErrorRate',
        dimensionsMap: { Environment: environment },
        statistic: 'Average',
        period: cdk.Duration.minutes(15),
      }),
      evaluationPeriods: 1,
      threshold: 20,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cw_actions.SnsAction(this.criticalAlertsTopic));

    new cloudwatch.Alarm(this, 'GlueJobFailureAlarm', {
      alarmName: `AgenticDataIngestion-Job-Failures-${environment}`,
      alarmDescription: 'Alert when Glue jobs are failing',
      metric: new cloudwatch.Metric({
        namespace: 'Glue',
        metricName: 'glue.driver.aggregate.numFailedTasks',
        statistic: 'Sum',
        period: cdk.Duration.seconds(900),
      }),
      evaluationPeriods: 1,
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cw_actions.SnsAction(this.criticalAlertsTopic));

    new cloudwatch.Alarm(this, 'SqsMessageAgeAlarm', {
      alarmName: `AgenticDataIngestion-SQS-Message-Age-${environment}`,
      alarmDescription: 'Alert when messages are stuck in the pipeline trigger queue for more than 15 minutes',
      metric: this.pipelineTriggerQueue.metricApproximateAgeOfOldestMessage({
        statistic: 'Maximum',
        period: cdk.Duration.seconds(300),
      }),
      evaluationPeriods: 1,
      threshold: 900,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cw_actions.SnsAction(this.criticalAlertsTopic));

    new cloudwatch.Alarm(this, 'ApprovalTimeoutAlarm', {
      alarmName: `AgenticDataIngestion-Approval-Timeouts-${environment}`,
      alarmDescription: 'Alert when approval requests are timing out',
      metric: new cloudwatch.Metric({
        namespace: 'AgenticDataIngestion/Pipeline',
        metricName: 'ApprovalRequests.Timeout',
        statistic: 'Sum',
        period: cdk.Duration.seconds(900),
      }),
      evaluationPeriods: 1,
      threshold: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cw_actions.SnsAction(this.criticalAlertsTopic));

    // P28 R5.3 (task 6.3): any message in the Glue-job-monitor DLQ is a job
    // completion event job_completion never finished processing: either
    // EventBridge could not deliver it, or the function failed on all three
    // asynchronous attempts (JobCompletionAsyncInvokeConfig). Either way an
    // execution may be stranded waiting for its callback. Depth > 0 pages
    // immediately.
    new cloudwatch.Alarm(this, 'GlueJobMonitorDlqDepthAlarm', {
      alarmName: `AgenticDataIngestion-GlueJobMonitor-DLQ-Depth-${environment}`,
      alarmDescription: 'Alert when Glue job completion events land in the GlueJobMonitorRule dead-letter queue (job_completion never processed them — executions may be stranded)',
      metric: glueJobMonitorDlq.metricApproximateNumberOfMessagesVisible({
        statistic: 'Maximum',
        period: cdk.Duration.seconds(300),
      }),
      evaluationPeriods: 1,
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    }).addAlarmAction(new cw_actions.SnsAction(this.criticalAlertsTopic));

    // ── CloudWatch Dashboard ───────────────────────────────────────
    new cloudwatch.Dashboard(this, 'AgenticDataIngestionDashboard', {
      dashboardName: `AgenticDataIngestion-Pipeline-Monitoring-${environment}`,
      widgets: [
        [
          new cloudwatch.GraphWidget({
            title: 'Approval Operations',
            width: 12,
            left: [
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,service,ApprovalType,PipelineId} MetricName="ApprovalRequests.Created"\', \'Sum\', 300)', label: 'Created', usingMetrics: {} }),
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,service,ApprovalType} MetricName="ApprovalNotifications.unified.Success"\', \'Sum\', 300)', label: 'Processed', usingMetrics: {} }),
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,service,ApprovalType,PipelineId} MetricName="ApprovalRequests.Timeout"\', \'Sum\', 300)', label: 'Timeout', usingMetrics: {} }),
            ],
          }),
          new cloudwatch.GraphWidget({
            title: 'Pipeline Stage Executions',
            width: 12,
            left: [
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,Status,StageName,service,PipelineId} MetricName="Pipeline.StageExecution" Status="completed"\', \'Sum\', 300)', label: 'Completed', usingMetrics: {} }),
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,Status,StageName,service,PipelineId} MetricName="Pipeline.StageExecution" Status="failed"\', \'Sum\', 300)', label: 'Failed', usingMetrics: {} }),
            ],
          }),
        ],
        [
          new cloudwatch.GraphWidget({
            title: 'Glue Job Operations',
            width: 12,
            left: [
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,PipelineId,Environment} MetricName="GlueJob.Created"\', \'Sum\', 300)', label: 'Created', usingMetrics: {} }),
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,PipelineId,Environment} MetricName="GlueJob.Succeeded"\', \'Sum\', 300)', label: 'Succeeded', usingMetrics: {} }),
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,PipelineId,Environment} MetricName="GlueJob.Failed"\', \'Sum\', 300)', label: 'Failed', usingMetrics: {} }),
            ],
          }),
          new cloudwatch.GraphWidget({
            title: 'Cost Tracking',
            width: 12,
            stacked: true,
            left: [
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,WorkerType,PipelineId,Environment} MetricName="GlueJob.EstimatedCostPerRun"\', \'Sum\', 3600)', label: 'Glue Cost', usingMetrics: {} }),
            ],
          }),
        ],
        // Lambda invocation and error metrics
        [
          new cloudwatch.GraphWidget({
            title: 'Lambda Invocations',
            width: 12,
            left: [
              compute.fileValidationLambda.metricInvocations({ period: cdk.Duration.minutes(5) }),
              compute.schemaExtractionLambda.metricInvocations({ period: cdk.Duration.minutes(5) }),
              compute.codeGenerationLambda.metricInvocations({ period: cdk.Duration.minutes(5) }),
              compute.dashboardApiLambda.metricInvocations({ period: cdk.Duration.minutes(5) }),
            ],
          }),
          new cloudwatch.GraphWidget({
            title: 'Lambda Errors',
            width: 12,
            left: [
              compute.fileValidationLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
              compute.schemaExtractionLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
              compute.codeGenerationLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
              compute.dashboardApiLambda.metricErrors({ period: cdk.Duration.minutes(5) }),
            ],
          }),
        ],
        // Step Functions execution metrics
        [
          new cloudwatch.GraphWidget({
            title: 'Step Functions Executions',
            width: 12,
            left: [
              this.stateMachine.metricStarted({ period: cdk.Duration.minutes(5) }),
              this.stateMachine.metricSucceeded({ period: cdk.Duration.minutes(5) }),
              this.stateMachine.metricFailed({ period: cdk.Duration.minutes(5) }),
              this.stateMachine.metricTimedOut({ period: cdk.Duration.minutes(5) }),
            ],
          }),
          new cloudwatch.GraphWidget({
            title: 'Step Functions Duration',
            width: 12,
            left: [
              this.stateMachine.metricTime({ period: cdk.Duration.minutes(5), statistic: 'Average' }),
              this.stateMachine.metricTime({ period: cdk.Duration.minutes(5), statistic: 'p99' }),
            ],
          }),
        ],
        // Lambda duration metrics
        [
          new cloudwatch.GraphWidget({
            title: 'Lambda Duration (ms)',
            width: 12,
            left: [
              compute.fileValidationLambda.metricDuration({ period: cdk.Duration.minutes(5) }),
              compute.schemaExtractionLambda.metricDuration({ period: cdk.Duration.minutes(5) }),
              compute.codeGenerationLambda.metricDuration({ period: cdk.Duration.minutes(5) }),
              compute.codeTestingLambda.metricDuration({ period: cdk.Duration.minutes(5) }),
            ],
          }),
          new cloudwatch.GraphWidget({
            title: 'Pipeline Stage Duration',
            width: 12,
            left: [
              new cloudwatch.MathExpression({ expression: 'SEARCH(\'{AgenticDataIngestion/Pipeline,Status,StageName,service,PipelineId} MetricName="Pipeline.StageDuration" Status="completed"\', \'Average\', 300)', label: 'Stage Duration', usingMetrics: {} }),
            ],
          }),
        ],
      ],
    });

    // ── P13 Agentic Capabilities — EventBridge Rules ───────────────

    // NOTE: AgentCore Runtime endpoints are still not invocable as an
    // EventBridge target — the target list is fixed and API Destinations
    // cannot sign SigV4 — so the route to an agent is
    // Rule -> dispatch Lambda -> AgentCore.
    //
    // p30a R1.1 wires the cost-optimization schedule (Rule 3) that way: its
    // target is compute.agentDispatchLambda, which owns the long synchronous
    // InvokeAgentRuntime call inside its own 15-minute timeout. The earlier
    // claim that "the actual agent invocation happens via the dashboard API
    // endpoints" was never true for this schedule: the dashboard handlers
    // started a thread and returned, and Lambda freezes the environment on
    // return, so nothing ran.
    //
    // The two Glue-state rules below remain SNS event capture + notification.
    // Self-healing does not need this rule to reach its agent — the
    // job_completion Lambda async-invokes the self-healing dispatcher on Glue
    // failure (job_completion/lambda_function.py).

    // Rule 1: Glue Job Failure → Self-Healing notification
    const glueJobFailureRule = new events.Rule(this, 'GlueJobFailureSelfHealingRule', {
      ruleName: `agentic-data-ingestion-self-healing-trigger-${environment}`,
      description: 'Trigger self-healing agent on Glue job failures',
      eventPattern: {
        source: ['aws.glue'],
        detailType: ['Glue Job State Change'],
        detail: {
          state: ['FAILED', 'ERROR', 'TIMEOUT'],
        },
      },
    });
    glueJobFailureRule.addTarget(new targets.SnsTopic(this.criticalAlertsTopic, {
      message: events.RuleTargetInput.fromText(
        'Self-healing agent triggered for Glue job failure'
      ),
    }));

    // Rule 2: Glue Job Success → Data Quality notification
    //
    // DEFERRED BY DECISION — this target is a hold, not an oversight
    // (p30a R1.5, design §3 C4). p30a activates the cost-optimization path
    // only; automatic data-quality invocation stays off until someone turns
    // it on deliberately. Note what "on" would mean: this event pattern
    // matches EVERY successful Glue job in the account, so flipping it is a
    // load and cost decision, not just a target swap.
    //
    // The agent is unreachable meanwhile: its on-demand endpoint
    // (trigger_quality_check, p30a R2.5) was removed on 2026-10-11 because it
    // ran with no target table and no Athena grant (docs/AGENTS.md § 3). It
    // has run before, though: the state table holds
    // two data_quality rows, so the pre-p30a threaded handler has produced
    // output occasionally (a warm container resuming the killed thread is the
    // likely explanation; a direct runtime invoke is the alternative — two
    // rows is too few to attribute confidently). Do not read this SNS target
    // as "data quality has never run".
    //
    // To wire the automatic trigger later, reuse Rule 3's target verbatim:
    // targets.LambdaFunction(compute.agentDispatchLambda) with
    // `agent_endpoint_env: 'DATA_QUALITY_AGENT_ENDPOINT'` and
    // `step_name: 'data_quality'`. No IAM work is needed — the dispatch role
    // in foundation-stack.ts already grants InvokeAgentRuntime on the
    // `agentic_data_ingestion_agent_data_quality-*` runtime prefix, added for
    // the on-demand path. Which is exactly why the CDK assertion pinning this
    // SNS target and its static text is the guard: with the plumbing already
    // in place, turning DQ on is one line here plus deliberately deleting
    // that assertion.
    const glueJobSuccessRule = new events.Rule(this, 'GlueJobSuccessDataQualityRule', {
      ruleName: `agentic-data-ingestion-data-quality-trigger-${environment}`,
      description: 'Trigger data quality agent on Glue job success',
      eventPattern: {
        source: ['aws.glue'],
        detailType: ['Glue Job State Change'],
        detail: {
          state: ['SUCCEEDED'],
        },
      },
    });
    glueJobSuccessRule.addTarget(new targets.SnsTopic(this.criticalAlertsTopic, {
      message: events.RuleTargetInput.fromText(
        'Data quality agent triggered for Glue job success'
      ),
    }));

    // Rule 3: Weekly Schedule → Cost Optimization agent (p30a R1.1)
    //
    // The target is the generic dispatch Lambda, NOT the alerts topic. The
    // previous SNS target published the static text "Cost optimization agent
    // weekly run triggered" and invoked nothing, which is why no
    // step_name="cost_optimization" state row has ever existed and the
    // dashboard Cost Recommendations panel is empty rather than stale.
    const costOptimizationScheduleRule = new events.Rule(this, 'CostOptimizationScheduleRule', {
      ruleName: `agentic-data-ingestion-cost-optimization-schedule-${environment}`,
      description: 'Weekly trigger for cost optimization agent',
      schedule: events.Schedule.cron({ weekDay: 'MON', hour: '6', minute: '0' }),
    });
    // The static input is the whole contract with the dispatcher.
    // `agent_endpoint_env` carries the NAME of an environment variable, which
    // is resolved inside the dispatch Lambda's own process — compute-stack.ts
    // puts COST_OPTIMIZATION_AGENT_ENDPOINT on that function from the same
    // AgentCore prop the dashboard API reads. Passing the name rather than a
    // literal URL keeps the endpoint single-sourced instead of duplicating it
    // into this rule, where it would silently go stale on a runtime redeploy.
    costOptimizationScheduleRule.addTarget(new targets.LambdaFunction(compute.agentDispatchLambda, {
      event: events.RuleTargetInput.fromObject({
        agent_endpoint_env: 'COST_OPTIMIZATION_AGENT_ENDPOINT',
        // step_name attributes a dispatch failure to this agent rather than to
        // the shared dispatcher.
        step_name: 'cost_optimization',
        // The agent's entrypoint ignores its payload today and always runs the
        // full fleet sweep; the marker distinguishes a scheduled run from the
        // id-scoped apply path added in p30a 1.6.
        payload: { trigger: 'weekly_schedule' },
      }),
    }));

    // Log retention for every function in this stack (T-09, R-06). Last, so
    // every function exists.
    retainLambdaLogs(this);
  }
}
