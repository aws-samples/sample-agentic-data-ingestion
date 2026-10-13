// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Template } from 'aws-cdk-lib/assertions';
import { APPROVAL_TIMEOUT_SECONDS, EXECUTION_TIMEOUT_SECONDS, OrchestrationStack } from '../lib/orchestration-stack';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * Creates a minimal mock of the dependent stacks so we can synth
 * the OrchestrationStack without Docker bundling.
 */
function createTestStacks() {
  const app = new cdk.App();
  const env = { account: '123456789012', region: 'us-west-2' };

  // ── Mock Foundation Stack ──────────────────────────────────────
  const foundationStack = new cdk.Stack(app, 'MockFoundation', { env });
  const sourceBucket = new s3.Bucket(foundationStack, 'SourceBucket', {
    bucketName: 'adi-source-files-123456789012-us-west-2',
  });
  const pipelineTriggerRole = new iam.Role(foundationStack, 'PipelineTriggerRole', {
    assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  });

  // ── Mock Data Stack ────────────────────────────────────────────
  const dataStack = new cdk.Stack(app, 'MockData', { env });
  const stateTable = new dynamodb.Table(dataStack, 'StateTable', {
    tableName: 'agentic_data_ingestion_state',
    partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'step_name', type: dynamodb.AttributeType.STRING },
    stream: dynamodb.StreamViewType.NEW_IMAGE,
  });
  const pipelinesTable = new dynamodb.Table(dataStack, 'PipelinesTable', {
    tableName: 'agentic_data_ingestion_pipelines',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
  });
  const approvalsTable = new dynamodb.Table(dataStack, 'ApprovalsTable', {
    tableName: 'agentic_data_ingestion_approvals',
    partitionKey: { name: 'approval_id', type: dynamodb.AttributeType.STRING },
  });
  const pipelineLocksTable = new dynamodb.Table(dataStack, 'PipelineLocksTable', {
    tableName: 'agentic_data_ingestion_pipeline_locks',
    partitionKey: { name: 'pipeline_prefix', type: dynamodb.AttributeType.STRING },
  });
  const executionsTable = new dynamodb.Table(dataStack, 'ExecutionsTable', {
    tableName: 'agentic_data_ingestion_executions',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
  });
  // p30 R9.1: the trigger's PROCESSED_FILES_TABLE env var reads this, so the
  // mock data stack must carry it or OrchestrationStack cannot synth.
  const processedFilesTable = new dynamodb.Table(dataStack, 'ProcessedFilesTable', {
    tableName: 'agentic_data_ingestion_processed_files',
    partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'fingerprint', type: dynamodb.AttributeType.STRING },
  });
  const eventBus = new events.EventBus(dataStack, 'EventBus', {
    eventBusName: 'agentic-data-ingestion-pipeline-events-dev',
  });
  new sqs.Queue(dataStack, 'DLQ', {
    queueName: 'agentic-data-ingestion-eventbridge-dlq-dev',
  });

  // ── Mock Compute Stack (inline Lambda functions — no Docker) ───
  const computeStack = new cdk.Stack(app, 'MockCompute', { env });
  const mockLambda = (id: string, fnName: string) =>
    new lambda.Function(computeStack, id, {
      functionName: fnName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(e,c): return {}'),
    });

  const compute = {
    fileValidationLambda: mockLambda('FV', 'agentic_data_ingestion_file_validation'),
    schemaExtractionLambda: mockLambda('SE', 'agentic_data_ingestion_schema_extract'),
    schemaComparisonLambda: mockLambda('SC', 'agentic_data_ingestion_schema_compare'),
    schemaMappingLambda: mockLambda('SM', 'agentic_data_ingestion_schema_mapping'),
    codeGenerationLambda: mockLambda('CG', 'agentic_data_ingestion_create_code'),
    codeTestingLambda: mockLambda('CT', 'agentic_data_ingestion_test_code'),
    createGluePipelineLambda: mockLambda('CGP', 'agentic_data_ingestion_create_glue_pipeline'),
    createInfrastructureLambda: mockLambda('CI', 'agentic_data_ingestion_create_infrastructure'),
    createQualityChecksLambda: mockLambda('CQC', 'agentic_data_ingestion_create_quality_checks'),
    runPipelineLambda: mockLambda('RP', 'agentic_data_ingestion_run_pipeline'),
    approvalHandlerLambda: mockLambda('AH', 'agentic_data_ingestion_approval_handler'),
    jobCompletionLambda: mockLambda('JC', 'agentic_data_ingestion_job_completion'),
    approvalTimeoutHandlerLambda: mockLambda('ATH', 'agentic_data_ingestion_approval_timeout_handler'),
    dashboardApiLambda: mockLambda('DA', 'agentic_data_ingestion_dashboard_api'),
    // p30a R1.1: CostOptimizationScheduleRule targets this function, so the
    // mock compute stack has to carry it or the Orchestration stack cannot synth.
    agentDispatchLambda: mockLambda('AD', 'agentic_data_ingestion_agent_dispatch'),
    strandsLayer: new lambda.LayerVersion(computeStack, 'MockStrandsLayer', {
      code: lambda.Code.fromAsset(path.join(__dirname, '.')),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
    }),
    adotLayer: lambda.LayerVersion.fromLayerVersionArn(computeStack, 'MockAdotLayer',
      'arn:aws:lambda:us-west-2:901920570463:layer:aws-otel-python-arm64-ver-1-32-0:2'),
  };

  return { app, env, foundationStack, sourceBucket, pipelineTriggerRole, dataStack, stateTable, pipelinesTable, approvalsTable, pipelineLocksTable, executionsTable, processedFilesTable, eventBus, computeStack, compute };
}

type SynthResult = {
  template: Template;
  stack: OrchestrationStack;
  compute: ReturnType<typeof createTestStacks>['compute'];
};

let synthCache: SynthResult | undefined;

/**
 * Synthesizes the OrchestrationStack once and caches the result.
 *
 * The stack and the mock compute constructs are returned alongside the
 * template because asserting the *identity* of an EventBridge target means
 * comparing the rendered `Arn` against `stack.resolve(<construct>.someArn)`.
 * That is how the R1.6 assertions below name the dispatch Lambda and the
 * alerts topic without hard-coding CDK's logical-id hashes, which shift
 * whenever a construct is renamed or moved.
 */
function synthOrchestration(): SynthResult {
  if (synthCache) return synthCache;

  const ctx = createTestStacks();

  // Build mock data/foundation objects matching the interfaces OrchestrationStack expects
  const mockData = Object.assign(ctx.dataStack, {
    stateTable: ctx.stateTable,
    pipelinesTable: ctx.pipelinesTable,
    approvalsTable: ctx.approvalsTable,
    pipelineLocksTable: ctx.pipelineLocksTable,
    executionsTable: ctx.executionsTable,
    processedFilesTable: ctx.processedFilesTable,
    eventBus: ctx.eventBus,
  });

  const mockFoundation = Object.assign(ctx.foundationStack, {
    sourceBucket: ctx.sourceBucket,
    pipelineTriggerRole: ctx.pipelineTriggerRole,
  });

  const orchestrationStack = new OrchestrationStack(ctx.app, 'TestOrchestration', {
    env: ctx.env,
    environment: 'dev',
    compute: ctx.compute as any,
    data: mockData as any,
    foundation: mockFoundation as any,
  });
  synthCache = {
    template: Template.fromStack(orchestrationStack),
    stack: orchestrationStack,
    compute: ctx.compute,
  };
  return synthCache;
}

function synthOrchestrationStack(): Template {
  return synthOrchestration().template;
}

// ── All 16 expected state names ──────────────────────────────────
const EXPECTED_STATES = [
  'FileValidation',
  'SchemaExtract',
  'SchemaCompare',
  'CheckSchemaAction',
  'ReuseExistingMapping',
  'SchemaMapping',
  'CheckSchemaMappingApproval',
  'WaitForSchemaMappingApproval',
  'CreateCode',
  'TestCode',
  'TestResultChoice',
  'ForceTestReviewApproval',
  'CheckCodeApproval',
  'WaitForCodeApproval',
  'CreateGluePipeline',
  'CreateInfrastructure',
  'CreateQualityChecks',
  'CheckJobTimeoutConfig',
  'ApplyDefaultJobTimeout',
  // P32 D2 — the in-execution regeneration loop
  'CheckRegenerationAttempt',
  'ApplyDefaultRegenerationAttempt',
  'ComputeNextRegenerationAttempt',
  'IncrementRegenerationAttempt',
  'RunPipeline',
  'ReleaseLockSuccess',
  'ReleaseLockFailure',
  'UpdateSuccessState',
  'RecordFailure',
  'PipelineFailed',
];

// P28 R1.3: every task state in the pipeline (9 Lambda invokes + 3
// waitForTaskToken states) must carry both Retry and Catch.
const LAMBDA_TASK_STATES = [
  'FileValidation', 'SchemaExtract', 'SchemaCompare', 'SchemaMapping',
  'CreateCode', 'TestCode', 'CreateGluePipeline', 'CreateInfrastructure',
  'CreateQualityChecks',
];
const TOKEN_TASK_STATES = ['WaitForSchemaMappingApproval', 'WaitForCodeApproval', 'ForceTestReviewApproval', 'RunPipeline'];
const ALL_TASK_STATES = [...LAMBDA_TASK_STATES, ...TOKEN_TASK_STATES];

describe('OrchestrationStack — Type-Safe Step Functions', () => {
  let template: Template;
  let smDefinition: Record<string, any>;

  // Synth once, reuse across tests
  function getTemplate() {
    if (!template) template = synthOrchestrationStack();
    return template;
  }

  function getStateMachineDefinition(): Record<string, any> {
    if (smDefinition) return smDefinition;
    const t = getTemplate();
    const resources = t.toJSON().Resources;
    // Find the StateMachine resource
    const smKey = Object.keys(resources).find(
      (k) => resources[k].Type === 'AWS::StepFunctions::StateMachine',
    );
    expect(smKey).toBeDefined();
    const defStr =
      resources[smKey!].Properties.DefinitionString?.['Fn::Join']?.[1] ??
      resources[smKey!].Properties.DefinitionString;
    // DefinitionString is either a plain string or a Fn::Join intrinsic
    let parsed: any;
    if (typeof defStr === 'string') {
      parsed = JSON.parse(defStr);
    } else if (Array.isArray(defStr)) {
      // Fn::Join produces an array of strings and Ref/GetAtt tokens
      // Replace tokens with placeholder strings so we can parse the JSON
      const joined = defStr
        .map((part: any) => (typeof part === 'string' ? part : '__TOKEN__'))
        .join('');
      parsed = JSON.parse(joined);
    } else {
      throw new Error('Unexpected DefinitionString format');
    }
    smDefinition = parsed;
    return smDefinition;
  }

  it('synthesizes a StateMachine resource', () => {
    const t = getTemplate();
    t.resourceCountIs('AWS::StepFunctions::StateMachine', 1);
  });

  it('state machine contains all 16+ expected states', () => {
    const def = getStateMachineDefinition();
    const stateNames = Object.keys(def.States);
    for (const expected of EXPECTED_STATES) {
      expect(stateNames).toContain(expected);
    }
  });

  it('starts at FileValidation', () => {
    const def = getStateMachineDefinition();
    expect(def.StartAt).toBe('FileValidation');
  });

  // ── Transition chain tests ─────────────────────────────────────
  it('FileValidation → SchemaExtract', () => {
    const def = getStateMachineDefinition();
    expect(def.States.FileValidation.Next).toBe('SchemaExtract');
  });

  it('SchemaExtract → SchemaCompare', () => {
    const def = getStateMachineDefinition();
    expect(def.States.SchemaExtract.Next).toBe('SchemaCompare');
  });

  it('SchemaCompare → CheckSchemaAction', () => {
    const def = getStateMachineDefinition();
    expect(def.States.SchemaCompare.Next).toBe('CheckSchemaAction');
  });

  it('CheckSchemaAction branches to ReuseExistingMapping or SchemaMapping', () => {
    const def = getStateMachineDefinition();
    const state = def.States.CheckSchemaAction;
    expect(state.Type).toBe('Choice');
    expect(state.Default).toBe('SchemaMapping');
    const reuseChoice = state.Choices.find(
      (c: any) => c.Next === 'ReuseExistingMapping',
    );
    expect(reuseChoice).toBeDefined();
  });

  it('ReuseExistingMapping → CheckSchemaMappingApproval', () => {
    const def = getStateMachineDefinition();
    expect(def.States.ReuseExistingMapping.Next).toBe('CheckSchemaMappingApproval');
  });

  it('SchemaMapping → CheckSchemaMappingApproval', () => {
    const def = getStateMachineDefinition();
    expect(def.States.SchemaMapping.Next).toBe('CheckSchemaMappingApproval');
  });

  // P32 D2: both fresh routes to CreateCode pass through the
  // regeneration-counter guard, so CreateCode's Payload path always resolves.
  // CreateCode is still the next *step* on both routes — asserted here rather
  // than assumed, since the guard is what makes the loop safe on attempt 1.
  //
  // AMENDED — p34 R4.5, a deliberate reversal, not a regression
  // (`LGR-5906ec6-2`, `LGR-60c4985-6`, both template / ratified). The two ends
  // of this Choice are SWAPPED: the Default was `CheckRegenerationAttempt` (the
  // permissive outcome) and is now `WaitForSchemaMappingApproval` (a human), so
  // an absent or non-boolean flag can no longer route past the reviewer.
  //
  // `.not.toBe('CreateCode')` moves WITH the permissive arm rather than staying
  // on the Default. That assertion is P32 D2's proof (`LGR-60c4985-2`, template
  // / ratified): both fresh routes into CreateCode pass the counter guard. Left
  // on the Default it would still pass and prove nothing, because the Default is
  // no longer the arm that reaches CreateCode.
  it('CheckSchemaMappingApproval branches to WaitForSchemaMappingApproval or the regeneration guard', () => {
    const def = getStateMachineDefinition();
    const state = def.States.CheckSchemaMappingApproval;
    expect(state.Type).toBe('Choice');
    // Fail closed: the residual branch is the human (p34 R4.5).
    expect(state.Default).toBe('WaitForSchemaMappingApproval');
    // The permissive arm — the only non-Default Choice — is the false arm.
    expect(state.Choices).toHaveLength(1);
    const falseArm = state.Choices[0];
    expect(falseArm.Next).toBe('CheckRegenerationAttempt');
    expect(falseArm.Next).not.toBe('CreateCode');
    // Both arms still reach CreateCode through the guard — the whole of P32 D2.
    expect(def.States.WaitForSchemaMappingApproval.Next).toBe('CheckRegenerationAttempt');
  });

  it('WaitForSchemaMappingApproval → CheckRegenerationAttempt → CreateCode', () => {
    const def = getStateMachineDefinition();
    expect(def.States.WaitForSchemaMappingApproval.Next).toBe('CheckRegenerationAttempt');
    const guard = def.States.CheckRegenerationAttempt;
    expect(guard.Choices.some((c: any) => c.Next === 'CreateCode')).toBe(true);
    expect(def.States.ApplyDefaultRegenerationAttempt.Next).toBe('CreateCode');
  });

  it('CreateCode → TestCode', () => {
    const def = getStateMachineDefinition();
    expect(def.States.CreateCode.Next).toBe('TestCode');
  });

  it('TestCode → TestResultChoice', () => {
    const def = getStateMachineDefinition();
    expect(def.States.TestCode.Next).toBe('TestResultChoice');
  });

  // AMENDED — p34 R8.5 / R4.1-4.3, a deliberate reversal, not a regression
  // (`LGR-5906ec6-2`, `LGR-60c4985-6`, both template / ratified). This is the
  // gate the spec exists for: its Default was `CreateInfrastructure`, the only
  // Choice in orchestration-stack.ts whose residual branch was the permissive
  // outcome, so an absent or malformed flag reached infrastructure creation
  // unreviewed. The Default is now the approval state.
  //
  // The two edges are the same two edges as before, with their ends swapped —
  // the approval destination moved from a named Choice onto the Default, so the
  // `Choices.find(... 'WaitForCodeApproval')` lookup is replaced by a Default
  // assertion plus the shape of the single permissive arm. The test name is kept
  // verbatim and still describes the state honestly.
  //
  // Validates: Requirements 4.1, 4.2, 4.3, 8.5
  it('CheckCodeApproval branches to WaitForCodeApproval or CreateInfrastructure', () => {
    const def = getStateMachineDefinition();
    const state = def.States.CheckCodeApproval;
    expect(state.Type).toBe('Choice');
    // R8.5: the default branch is the approval state, asserted against the
    // synthesized definition rather than the TypeScript source text.
    expect(state.Default).toBe('WaitForCodeApproval');
    // Exactly one non-Default Choice, and it is the three-conjunct And. Ordered
    // deep-equality: dropping IsBoolean or reordering the conjuncts fails here,
    // which is what design D4's no-States.Runtime claim rests on.
    expect(state.Choices).toHaveLength(1);
    expect(state.Choices[0]).toEqual({
      And: [
        { Variable: '$.test_code_result.approval_required', IsPresent: true },
        { Variable: '$.test_code_result.approval_required', IsBoolean: true },
        { Variable: '$.test_code_result.approval_required', BooleanEquals: false },
      ],
      Next: 'CreateInfrastructure',
    });
  });

  it('WaitForCodeApproval → CreateInfrastructure', () => {
    const def = getStateMachineDefinition();
    expect(def.States.WaitForCodeApproval.Next).toBe('CreateInfrastructure');
  });

  // p34 R3.6: a code_generation approval request carries the structured test
  // result, so the reviewer sees the checks alongside the code rather than a
  // header and two buttons. This is the producer half of R3.6 — the same key
  // ForceTestReviewApproval has carried since P28 R4.2, asserted below in the
  // `TestResultChoice / ForceTestReviewApproval` block.
  //
  // Validates: Requirements 3.6
  it('WaitForCodeApproval carries the structured test result, like ForceTestReviewApproval', () => {
    const def = getStateMachineDefinition();
    const state = def.States.WaitForCodeApproval;
    expect(state.Resource).toBe('arn:aws:states:::lambda:invoke.waitForTaskToken');
    const payload = state.Parameters.Payload;
    expect(payload['approval_type']).toBe('code_generation');
    expect(payload['test_result.$']).toBe('$.test_code_result.test_result');
    // One producer path, one key: the two approval gates read the test result
    // from the same place, so a reviewer sees the same evidence either way.
    expect(payload['test_result.$']).toBe(
      def.States.ForceTestReviewApproval.Parameters.Payload['test_result.$'],
    );
  });

  it('CreateInfrastructure → CreateGluePipeline', () => {
    const def = getStateMachineDefinition();
    expect(def.States.CreateInfrastructure.Next).toBe('CreateGluePipeline');
  });

  it('CreateGluePipeline → CreateQualityChecks', () => {
    const def = getStateMachineDefinition();
    expect(def.States.CreateGluePipeline.Next).toBe('CreateQualityChecks');
  });

  it('CreateQualityChecks → CheckJobTimeoutConfig (P28 R5.5 guard)', () => {
    const def = getStateMachineDefinition();
    expect(def.States.CreateQualityChecks.Next).toBe('CheckJobTimeoutConfig');
  });

  it('CheckJobTimeoutConfig defaults through ApplyDefaultJobTimeout → RunPipeline', () => {
    const def = getStateMachineDefinition();
    expect(def.States.CheckJobTimeoutConfig.Type).toBe('Choice');
    expect(def.States.ApplyDefaultJobTimeout.Next).toBe('RunPipeline');
  });

  it('RunPipeline → ReleaseLockSuccess', () => {
    const def = getStateMachineDefinition();
    expect(def.States.RunPipeline.Next).toBe('ReleaseLockSuccess');
  });

  it('ReleaseLockSuccess → UpdateSuccessState', () => {
    const def = getStateMachineDefinition();
    expect(def.States.ReleaseLockSuccess.Next).toBe('UpdateSuccessState');
  });

  it('UpdateSuccessState is terminal', () => {
    const def = getStateMachineDefinition();
    expect(def.States.UpdateSuccessState.End).toBe(true);
  });

  it('RecordFailure → ReleaseLockFailure', () => {
    const def = getStateMachineDefinition();
    expect(def.States.RecordFailure.Next).toBe('ReleaseLockFailure');
  });

  it('ReleaseLockFailure routes to PipelineFailed', () => {
    const def = getStateMachineDefinition();
    expect(def.States.ReleaseLockFailure.Next).toBe('PipelineFailed');
  });

  // Lock ownership (2026-10-11, assessment F3). Both in-machine releases name
  // the holder, so the inline release function can refuse to delete a lock a
  // later execution acquired. tests/cdk/stacks/test_lock_release_inline.py
  // runs that function against the stale-event sequence; this pins that the
  // name actually reaches it.
  it.each(['ReleaseLockSuccess', 'ReleaseLockFailure'])(
    '%s passes the execution name as the lock holder',
    (stateName) => {
      const def = getStateMachineDefinition();
      // payloadResponseOnly invokes the function directly, so the payload IS
      // the Parameters object (no `.Payload` wrapper).
      const payload = def.States[stateName].Parameters;
      expect(payload['execution_name.$']).toBe('$$.Execution.Name');
      expect(payload['pipeline_id.$']).toBe('$.pipeline_id');
    },
  );

  // Execution lifetime (2026-10-11, assessment F4). The lock TTL in
  // shared/config.py is sized above this; tests/cdk/stacks/
  // test_lock_ttl_exceeds_execution_timeout.py pins the ordering, and this pins
  // that the timeout is actually on the deployed definition rather than only
  // exported from the module.
  it('the state machine has a 10-day execution timeout', () => {
    const def = getStateMachineDefinition();
    expect(def.TimeoutSeconds).toBe(EXECUTION_TIMEOUT_SECONDS);
    expect(EXECUTION_TIMEOUT_SECONDS).toBe(864000);
    // Three approval gates and a six-hour Glue job must fit, with margin.
    expect(EXECUTION_TIMEOUT_SECONDS).toBeGreaterThan(3 * APPROVAL_TIMEOUT_SECONDS + 6 * 3600);
  });

  it('PipelineFailed is a terminal Fail state', () => {
    const def = getStateMachineDefinition();
    expect(def.States.PipelineFailed.Type).toBe('Fail');
  });

  // ── State type tests ───────────────────────────────────────────
  it('Lambda invoke states are Task type', () => {
    const def = getStateMachineDefinition();
    const lambdaStates = [
      'FileValidation', 'SchemaExtract', 'SchemaCompare', 'SchemaMapping',
      'CreateCode', 'TestCode', 'CreateGluePipeline', 'CreateInfrastructure',
      'CreateQualityChecks',
    ];
    for (const name of lambdaStates) {
      expect(def.States[name].Type).toBe('Task');
    }
  });

  it('Choice states are Choice type', () => {
    const def = getStateMachineDefinition();
    const choiceStates = [
      'CheckSchemaAction', 'CheckSchemaMappingApproval', 'TestResultChoice', 'CheckCodeApproval',
      'CheckRegenerationAttempt',
    ];
    for (const name of choiceStates) {
      expect(def.States[name].Type).toBe('Choice');
    }
  });

  // p47 R4 Arm B (ratified): the reuse fast path saves the mapping computation —
  // including the Bedrock Converse call — and NOT the approval round-trip. The
  // mechanism is precisely that this Pass injects no `approval_required`, so
  // CheckSchemaMappingApproval's IsPresent conjunct fails and the run falls to its
  // Default, which is the human (invariant `LGR-69ca78b-1`).
  //
  // Asserted here because the posture was otherwise only a comment: the sibling
  // contract test pins that this state is an `sfn.Pass` and says nothing about its
  // parameters, so Arm A — injecting `approval_required: false` — would have landed
  // green. Adding that key is a governance change and must fail a test, not pass one.
  it('R4 Arm B: ReuseExistingMapping injects no approval_required', () => {
    const def = getStateMachineDefinition();
    const state = def.States.ReuseExistingMapping;

    expect(state.Type).toBe('Pass');
    expect(state.Parameters).toBeDefined();
    // Neither the plain key nor the JSONPath spelling ('approval_required.$'),
    // so the assertion cannot be satisfied by injecting the same field by reference.
    const paramKeys = Object.keys(state.Parameters);
    expect(paramKeys.filter((k) => k.replace(/\.\$$/, '') === 'approval_required')).toEqual([]);
    // The parameters this state DOES carry, so an addition of any kind is visible
    // here rather than silently accreting.
    expect(paramKeys.sort()).toEqual(
      ['mapping_id.$', 'original_pipeline_id.$', 'reused_mapping', 'run_id.$', 'statusCode'],
    );
    expect(state.ResultPath).toBe('$.schema_mapping_result');
  });

  it('Pass states are Pass type', () => {
    const def = getStateMachineDefinition();
    expect(def.States.ReuseExistingMapping.Type).toBe('Pass');
    expect(def.States.UpdateSuccessState.Type).toBe('Pass');
    // P32 D2 — the counter states carry no side effects beyond state shaping
    expect(def.States.ApplyDefaultRegenerationAttempt.Type).toBe('Pass');
    expect(def.States.ComputeNextRegenerationAttempt.Type).toBe('Pass');
    expect(def.States.IncrementRegenerationAttempt.Type).toBe('Pass');
  });

  // ── waitForTaskToken tests ─────────────────────────────────────
  it('WaitForSchemaMappingApproval uses waitForTaskToken', () => {
    const def = getStateMachineDefinition();
    expect(def.States.WaitForSchemaMappingApproval.Resource).toBe(
      'arn:aws:states:::lambda:invoke.waitForTaskToken',
    );
  });

  // p54 R1.1: the schema-mapping gate is told WHICH mapping it is gating.
  //
  // Without `mapping_id` the event named no mapping at all, so on a Reuse_Run —
  // where the `schema_mapping` Lambda never runs and writes no Mapping_State_Row —
  // `get_approval_data` had nothing to resolve from and returned `{}`. The approver
  // was shown an empty payload and clicked anyway.
  //
  // Both halves are asserted, because the risk here is not the key's absence but
  // the WRONG key: invariant `LGR-5c539a6-2` requires that every key an ASL path
  // reads off a `$.{step}_result` envelope exists on BOTH producers of it, since a
  // path that does not resolve raises `States.ParameterPathFailure` — uncatchable by
  // Retry or Catch. `$.schema_mapping_result` has two producers and `mapping_id` is
  // on both. `reused_mapping` is on the reuse Pass ONLY, so a path to it would fail
  // every non-reuse run (p54 do-NOT 1); the negative assertion below is what keeps
  // that from being added later as an apparently harmless convenience.
  it('p54 R1.1: the schema-mapping gate carries the mapping_id, and no one-armed key', () => {
    const def = getStateMachineDefinition();
    const payload = def.States.WaitForSchemaMappingApproval.Parameters.Payload;

    expect(payload['mapping_id.$']).toBe('$.schema_mapping_result.mapping_id');

    // Additive, not a replacement: the five keys the gate already carried are
    // still there. `run_id` and `pipeline_id` come off `$.file_validation_result`
    // deliberately — that envelope is written before either mapping arm runs.
    expect(payload['approval_type']).toBe('schema_mapping');
    expect(payload['pipeline_id.$']).toBe('$.file_validation_result.original_pipeline_id');
    expect(payload['run_id.$']).toBe('$.file_validation_result.run_id');
    expect(payload['execution_arn.$']).toBe('$$.Execution.Name');
    expect(payload['task_token.$']).toBe('$$.Task.Token');

    // The exact key set, in the house style of the ReuseExistingMapping assertion
    // above, so a sixth addition is visible here rather than silently accreting.
    expect(Object.keys(payload).sort()).toEqual([
      'approval_type',
      'execution_arn.$',
      'mapping_id.$',
      'pipeline_id.$',
      'run_id.$',
      'task_token.$',
    ]);

    // do-NOT 1: no path anywhere in this Payload may read a key that exists on one
    // arm of `$.schema_mapping_result` only. `reused_mapping` is the live example —
    // the Pass sets it, the Lambda's response dict does not.
    const referencedPaths = Object.entries(payload)
      .filter(([key]) => key.endsWith('.$'))
      .map(([, value]) => String(value));
    expect(
      referencedPaths.filter((p) => p.includes('reused_mapping')),
      'a path to $.schema_mapping_result.reused_mapping would raise ' +
        'States.ParameterPathFailure on every non-reuse run (LGR-5c539a6-2)',
    ).toEqual([]);
  });

  // The other half of `LGR-5c539a6-2` for the key added above: the reuse arm must
  // keep producing `mapping_id`, or the gate's path stops resolving on every
  // unchanged-schema run. The Pass's full parameter set is pinned separately by the
  // 'R4 Arm B' assertion above; this one records WHY that particular key is
  // load-bearing, so removing it fails with the runtime consequence named.
  //
  // Note the path was already total before p54 and is provable from topology rather
  // than from reading the Lambda: CreateCode and TestCode read the same path (pinned
  // by 'the payloads keep their per-step input bindings alongside the token' below),
  // and both are downstream of this Choice on both arms. The gate is a third reader.
  it('p54 R1.1: the reuse arm produces the mapping_id the gate reads', () => {
    const def = getStateMachineDefinition();
    expect(
      def.States.ReuseExistingMapping.Parameters['mapping_id.$'],
      'WaitForSchemaMappingApproval reads $.schema_mapping_result.mapping_id, so the ' +
        'reuse arm must produce it — otherwise every reused-mapping run fails at the gate',
    ).toBe('$.schema_compare_result.existing_mapping_id');
    expect(def.States.ReuseExistingMapping.ResultPath).toBe('$.schema_mapping_result');
    // Nothing was added to the Pass by p54: this task touches the Task only, and
    // the governance posture lives in the Pass's `parameters` (`LGR-5d68eeb-1`).
    expect(
      Object.keys(def.States.ReuseExistingMapping.Parameters).filter(
        (k) => k.replace(/\.\$$/, '') === 'reused_mapping',
      ),
    ).toEqual(['reused_mapping']);
  });

  it('WaitForCodeApproval uses waitForTaskToken', () => {
    const def = getStateMachineDefinition();
    expect(def.States.WaitForCodeApproval.Resource).toBe(
      'arn:aws:states:::lambda:invoke.waitForTaskToken',
    );
  });

  it('RunPipeline uses waitForTaskToken', () => {
    const def = getStateMachineDefinition();
    expect(def.States.RunPipeline.Resource).toBe(
      'arn:aws:states:::lambda:invoke.waitForTaskToken',
    );
  });

  // ── Error handling tests (P28 R1.3 / R1.4) ─────────────────────
  it('EVERY task state has a Catch: States.ALL → RecordFailure with ResultPath $.error', () => {
    const def = getStateMachineDefinition();
    for (const name of ALL_TASK_STATES) {
      const catches = def.States[name].Catch;
      expect(catches, `${name} must have Catch`).toBeDefined();
      expect(catches.length).toBeGreaterThan(0);
      expect(catches[0].ErrorEquals, `${name} Catch must be States.ALL`).toEqual(['States.ALL']);
      expect(catches[0].Next, `${name} Catch must target RecordFailure`).toBe('RecordFailure');
      expect(catches[0].ResultPath, `${name} Catch must preserve error at $.error`).toBe('$.error');
    }
  });

  it('EVERY task state has a Retry for TransientStepFailure + Lambda service errors', () => {
    const def = getStateMachineDefinition();
    for (const name of ALL_TASK_STATES) {
      const retries = def.States[name].Retry;
      expect(retries, `${name} must have Retry`).toBeDefined();
      expect(retries.length).toBeGreaterThan(0);
      expect(retries[0].ErrorEquals).toContain('TransientStepFailure');
      expect(retries[0].ErrorEquals).toContain('Lambda.ServiceException');
      expect(retries[0].ErrorEquals).toContain('Lambda.TooManyRequestsException');
    }
  });

  it('Retry policies use exponential backoff (5s interval, 3 attempts, 2.0 rate)', () => {
    const def = getStateMachineDefinition();
    for (const name of ALL_TASK_STATES) {
      const retry = def.States[name].Retry[0];
      expect(retry.IntervalSeconds).toBe(5);
      expect(retry.MaxAttempts).toBe(3);
      expect(retry.BackoffRate).toBe(2.0);
    }
  });

  it('RecordFailure receives the full state plus the execution name', () => {
    const def = getStateMachineDefinition();
    const params = def.States.RecordFailure.Parameters?.Payload ?? def.States.RecordFailure.Parameters;
    // Full state (no JSONPath plucking that could raise States.Runtime — R1.4)
    expect(params['state.$']).toBe('$');
    expect(params['execution_name.$']).toBe('$$.Execution.Name');
  });

  it('RecordFailure routes to PipelineFailed via ReleaseLockFailure', () => {
    const def = getStateMachineDefinition();
    expect(def.States.RecordFailure.Next).toBe('ReleaseLockFailure');
    expect(def.States.ReleaseLockFailure.Next).toBe('PipelineFailed');
    expect(def.States.PipelineFailed.Type).toBe('Fail');
  });

  it('a RecordFailure crash cannot mask the original failure (own Catch, error discarded)', () => {
    const def = getStateMachineDefinition();
    const catches = def.States.RecordFailure.Catch;
    expect(catches).toBeDefined();
    expect(catches[0].ErrorEquals).toEqual(['States.ALL']);
    expect(catches[0].Next).toBe('ReleaseLockFailure');
    // ResultPath null discards the recorder's own error, keeping $.error
    expect(catches[0].ResultPath ?? null).toBeNull();
  });

  it('RecordFailure Lambda is a real asset-based function with STATE_TABLE env', () => {
    const t = getTemplate();
    const resources = t.toJSON().Resources;
    const fnKey = Object.keys(resources).find(
      (k) =>
        resources[k].Type === 'AWS::Lambda::Function' &&
        resources[k].Properties.FunctionName === 'agentic-data-ingestion-record-failure-dev',
    );
    expect(fnKey, 'record-failure Lambda must exist').toBeDefined();
    const props = resources[fnKey!].Properties;
    expect(props.Handler).toBe('lambda_function.lambda_handler');
    // Asset-based code (S3), not inline ZipFile
    expect(props.Code.ZipFile).toBeUndefined();
    expect(props.Code.S3Key).toBeDefined();
    expect(props.Environment.Variables.STATE_TABLE).toBeDefined();
  });

  it('RecordFailure Lambda role can write to the state table', () => {
    const t = getTemplate();
    const resources = t.toJSON().Resources;
    // Locate the policy attached to the failure recorder role
    const policyKey = Object.keys(resources).find((k) => {
      if (resources[k].Type !== 'AWS::IAM::Policy') return false;
      const roles = resources[k].Properties.Roles ?? [];
      return roles.some((r: any) => typeof r?.Ref === 'string' && r.Ref.startsWith('FailureRecorderRole'));
    });
    expect(policyKey, 'failure recorder role policy must exist').toBeDefined();
    const statements = resources[policyKey!].Properties.PolicyDocument.Statement;
    const writeStmt = statements.find((s: any) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.includes('dynamodb:PutItem') && actions.includes('dynamodb:UpdateItem');
    });
    expect(writeStmt, 'must grant dynamodb:PutItem + UpdateItem').toBeDefined();
  });

  // ── ResultPath tests ───────────────────────────────────────────
  it('each Lambda invoke state has a unique ResultPath', () => {
    const def = getStateMachineDefinition();
    const resultPaths = new Set<string>();
    const lambdaStates = [
      'FileValidation', 'SchemaExtract', 'SchemaCompare', 'SchemaMapping',
      'CreateCode', 'TestCode', 'CreateGluePipeline', 'CreateInfrastructure',
      'CreateQualityChecks',
    ];
    for (const name of lambdaStates) {
      const rp = def.States[name].ResultPath;
      expect(rp).toBeDefined();
      expect(resultPaths.has(rp)).toBe(false);
      resultPaths.add(rp);
    }
  });

  // ── Non-state-machine resources preserved ──────────────────────
  it('preserves CloudWatch Dashboard', () => {
    const t = getTemplate();
    t.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
  });

  it('preserves CloudWatch Alarms', () => {
    const t = getTemplate();
    // 5 alarms: PipelineErrorRate, GlueJobFailure, SqsMessageAge,
    // ApprovalTimeout, GlueJobMonitorDlqDepth (P28 R5.3, task 6.3)
    t.resourceCountIs('AWS::CloudWatch::Alarm', 5);
  });

  it('preserves SNS Topics', () => {
    const t = getTemplate();
    t.resourceCountIs('AWS::SNS::Topic', 2);
  });

  it('preserves EventBridge Rules', () => {
    const t = getTemplate();
    // Multiple rules: pipeline state, approval status, MCP, Glue job, S3 upload, etc.
    const resources = t.toJSON().Resources;
    const ruleCount = Object.values(resources).filter(
      (r: any) => r.Type === 'AWS::Events::Rule',
    ).length;
    expect(ruleCount).toBeGreaterThanOrEqual(5);
  });

  // ── CheckSchemaAction complex And condition ────────────────────
  it('CheckSchemaAction uses And condition with 3 sub-conditions', () => {
    const def = getStateMachineDefinition();
    const choice = def.States.CheckSchemaAction.Choices[0];
    expect(choice.And).toBeDefined();
    expect(choice.And.length).toBe(3);
    // First: action == use_existing_pipeline
    expect(choice.And[0].Variable).toBe('$.schema_compare_result.action');
    expect(choice.And[0].StringEquals).toBe('use_existing_pipeline');
    // Second: existing_mapping_id is present
    expect(choice.And[1].Variable).toBe('$.schema_compare_result.existing_mapping_id');
    expect(choice.And[1].IsPresent).toBe(true);
    // Third: existing_mapping_id is not empty (Not + StringEquals)
    expect(choice.And[2].Not).toBeDefined();
  });

  // ── P28 R4 — test-failure gating (task 5.2) ────────────────────
  // Validates: Requirements 4.1, 4.2, 4.5
  describe('TestResultChoice / ForceTestReviewApproval (P28 R4)', () => {
    it('TestResultChoice branches on $.test_code_result.test_result.status == "passed" → CheckCodeApproval', () => {
      const def = getStateMachineDefinition();
      const state = def.States.TestResultChoice;
      expect(state.Type).toBe('Choice');
      const passedChoice = state.Choices.find((c: any) => c.Next === 'CheckCodeApproval');
      expect(passedChoice, 'passed branch must target CheckCodeApproval').toBeDefined();
      // Documented condition, IsPresent-guarded so a malformed payload can
      // never kill the Choice with an uncatchable States.Runtime error
      expect(passedChoice.And).toBeDefined();
      expect(passedChoice.And.length).toBe(2);
      expect(passedChoice.And[0].Variable).toBe('$.test_code_result.test_result.status');
      expect(passedChoice.And[0].IsPresent).toBe(true);
      expect(passedChoice.And[1].Variable).toBe('$.test_code_result.test_result.status');
      expect(passedChoice.And[1].StringEquals).toBe('passed');
    });

    it('anything-but-passed (failed or missing status) defaults to ForceTestReviewApproval', () => {
      const def = getStateMachineDefinition();
      const state = def.States.TestResultChoice;
      expect(state.Default).toBe('ForceTestReviewApproval');
      // P32 D2 amends this assertion deliberately (it used to pin
      // Choices.length === 1, i.e. the ABSENCE of the regeneration route —
      // `LGR-5906ec6-2`'s pattern). There are now exactly two non-default
      // branches: passed, and regeneration-in-bounds. Anything else — a
      // failure that is not a regeneration trigger, a spent attempt budget, a
      // malformed payload — still falls through to the human gate.
      expect(state.Choices.length).toBe(2);
      const targets = state.Choices.map((c: any) => c.Next);
      expect(targets).toEqual(['CheckCodeApproval', 'ComputeNextRegenerationAttempt']);
    });

    it('ForceTestReviewApproval is a waitForTaskToken approval of type test_failure carrying the report', () => {
      const def = getStateMachineDefinition();
      const state = def.States.ForceTestReviewApproval;
      expect(state.Type).toBe('Task');
      expect(state.Resource).toBe('arn:aws:states:::lambda:invoke.waitForTaskToken');
      const payload = state.Parameters.Payload;
      expect(payload['approval_type']).toBe('test_failure');
      expect(payload['task_token.$']).toBe('$$.Task.Token');
      expect(payload['run_id.$']).toBe('$.file_validation_result.run_id');
      expect(payload['pipeline_id.$']).toBe('$.file_validation_result.original_pipeline_id');
      // Full structured report travels with the approval request (R4.2)
      expect(payload['test_result.$']).toBe('$.test_code_result.test_result');
      // Regeneration is p16 retry-from-step — documented in the payload
      expect(payload['remediation_hint']).toBe('retry-from-step:create_code');
      // p34 R6.1/6.2/6.6 amends this assertion DELIBERATELY (`LGR-5906ec6-2`,
      // `LGR-60c4985-6`, both template / ratified — an amendment must read as a
      // reversal, not as a regression). It used to pin the literal 604800, i.e.
      // a 7-day window; the window is now 72h and, more to the point, is
      // expressed ONCE as APPROVAL_TIMEOUT_SECONDS and shared by all three
      // approval gates. Asserting the constant rather than a number is what
      // makes R6.6 checkable here. The subject — this gate carries the same
      // approval window as the other human gates — is unchanged and still
      // asserted.
      expect(state.TimeoutSeconds).toBe(APPROVAL_TIMEOUT_SECONDS);
      expect(state.ResultPath).toBe('$.test_review_approval');
    });

    it('ForceTestReviewApproval has Retry + Catch → RecordFailure (task-1.4 pattern)', () => {
      const def = getStateMachineDefinition();
      const state = def.States.ForceTestReviewApproval;
      expect(state.Retry[0].ErrorEquals).toContain('TransientStepFailure');
      expect(state.Catch[0].ErrorEquals).toEqual(['States.ALL']);
      expect(state.Catch[0].Next).toBe('RecordFailure');
      expect(state.Catch[0].ResultPath).toBe('$.error');
    });

    it('approve-anyway resumes at CheckCodeApproval — the same next state as a passed test', () => {
      const def = getStateMachineDefinition();
      expect(def.States.ForceTestReviewApproval.Next).toBe('CheckCodeApproval');
    });

    it('a failed test can NEVER reach CreateInfrastructure without an approval decision', () => {
      const def = getStateMachineDefinition();
      // The failed branch parks on a waitForTaskToken task: the execution
      // cannot move until an approver calls send_task_success (approve-anyway
      // → CheckCodeApproval) or send_task_failure (reject → Catch →
      // RecordFailure → PipelineFailed). Walk every non-approval edge out of
      // TestResultChoice's default target and assert none reaches
      // CreateInfrastructure directly.
      const failedTarget = def.States.TestResultChoice.Default;
      expect(failedTarget).toBe('ForceTestReviewApproval');
      const gate = def.States[failedTarget];
      // Token task — no output path exists other than the approval callback
      expect(gate.Resource).toBe('arn:aws:states:::lambda:invoke.waitForTaskToken');
      // Its only success edge goes to the approval Choice, not the infra step
      expect(gate.Next).not.toBe('CreateInfrastructure');
      expect(gate.Next).toBe('CheckCodeApproval');
      // Its only failure edge goes to the failure chain
      expect(gate.Catch[0].Next).toBe('RecordFailure');
    });
  });

  // ═══════════════════════════════════════════════════════════════════════
  // p32 D2 — the regeneration loop lives inside one execution
  // ═══════════════════════════════════════════════════════════════════════
  //
  // Defect 4 was a conclusion with no destination: test_code decided
  // "regenerate" on every failing run and the only consumer of that verdict
  // was the approval gate. D2 gives it a destination — an edge from
  // TestResultChoice back to CreateCode, carrying an incremented counter in
  // execution state.
  //
  // What these assertions exist to prevent, beyond the happy path:
  //   • an attempt becoming a separate execution or a separate run id
  //     (`LGR-21a1b5b-2`, `LGR-21a1b5b-4`) — pinned by asserting the edge
  //     target is a state in THIS machine and that nothing on the loop starts
  //     an execution;
  //   • an unbounded loop — the bound is re-evaluated on the edge itself;
  //   • a first generation breaking because the counter path does not resolve
  //     (3.18) — pinned by the guard's default;
  //   • a failing run reaching CreateInfrastructure without either a pass or a
  //     human decision (3.21) — pinned by walking every edge out of the loop.
  //
  // Validates: Requirements 2.6, 2.17; 3.18, 3.21
  describe('regeneration loop-back edge (p32 D2)', () => {
    const ATTEMPT_PATH = '$.regeneration_attempt';
    const HOLDER_PATH = '$.regeneration_next';

    it('the counter path is guaranteed present before CreateCode reads it', () => {
      const def = getStateMachineDefinition();
      const guard = def.States.CheckRegenerationAttempt;
      expect(guard.Type).toBe('Choice');
      // Exactly one non-default branch: present AND numeric → straight through
      expect(guard.Choices).toHaveLength(1);
      const usable = guard.Choices[0];
      expect(usable.Next).toBe('CreateCode');
      expect(usable.And).toHaveLength(2);
      expect(usable.And[0]).toEqual({ Variable: ATTEMPT_PATH, IsPresent: true });
      expect(usable.And[1]).toEqual({ Variable: ATTEMPT_PATH, IsNumeric: true });
      // Absent or malformed → 0 is injected first, so a first generation can
      // never fail on an unresolvable payload path (3.18)
      expect(guard.Default).toBe('ApplyDefaultRegenerationAttempt');
      const seed = def.States.ApplyDefaultRegenerationAttempt;
      expect(seed.Result).toBe(0);
      expect(seed.ResultPath).toBe(ATTEMPT_PATH);
      expect(seed.Next).toBe('CreateCode');
    });

    it('a trigger-supplied counter survives the guard rather than being reset', () => {
      // D1 makes regeneration_attempt legal on a retry/self_healing trigger
      // input. The guard must not clobber it — a field that may be present but
      // has no effect is the shape that produced Defect 4 in the first place.
      const def = getStateMachineDefinition();
      const guard = def.States.CheckRegenerationAttempt;
      const passthrough = guard.Choices.find((c: any) => c.Next === 'CreateCode');
      expect(passthrough, 'a present numeric counter goes straight to CreateCode').toBeDefined();
      // and nothing on that branch writes the path
      expect(passthrough.ResultPath).toBeUndefined();
    });

    it('the regeneration branch is bounded on the edge, not just in the Lambda', () => {
      const def = getStateMachineDefinition();
      const branch = def.States.TestResultChoice.Choices.find(
        (c: any) => c.Next === 'ComputeNextRegenerationAttempt',
      );
      expect(branch, 'TestResultChoice must have a regeneration branch').toBeDefined();
      // Every field is IsPresent/IsNumeric guarded so a malformed payload
      // falls through to the forced review instead of killing the Choice
      expect(branch.And).toEqual([
        { Variable: '$.test_code_result.regeneration_needed', IsPresent: true },
        { Variable: '$.test_code_result.regeneration_needed', BooleanEquals: true },
        { Variable: '$.test_code_result.regeneration_attempt', IsPresent: true },
        { Variable: '$.test_code_result.regeneration_attempt', IsNumeric: true },
        { Variable: '$.test_code_result.regeneration_max_attempts', IsPresent: true },
        { Variable: '$.test_code_result.regeneration_max_attempts', IsNumeric: true },
        {
          Variable: '$.test_code_result.regeneration_attempt',
          NumericLessThanPath: '$.test_code_result.regeneration_max_attempts',
        },
      ]);
    });

    it('the loop advances the counter in state and re-enters CreateCode', () => {
      const def = getStateMachineDefinition();
      const compute = def.States.ComputeNextRegenerationAttempt;
      expect(compute.Parameters).toEqual({
        'attempt.$': `States.MathAdd(${ATTEMPT_PATH}, 1)`,
      });
      expect(compute.ResultPath).toBe(HOLDER_PATH);
      expect(compute.Next).toBe('IncrementRegenerationAttempt');

      const increment = def.States.IncrementRegenerationAttempt;
      expect(increment.InputPath).toBe(`${HOLDER_PATH}.attempt`);
      expect(increment.ResultPath).toBe(ATTEMPT_PATH);
      expect(increment.Next).toBe('CreateCode');
    });

    it('neither loop state clobbers a path a downstream state reads', () => {
      const def = getStateMachineDefinition();
      // The V1 live finding: a re-executed step overwrites its own
      // {step}_result, so anything the loop writes must be its own key.
      const written = [
        def.States.ApplyDefaultRegenerationAttempt.ResultPath,
        def.States.ComputeNextRegenerationAttempt.ResultPath,
        def.States.IncrementRegenerationAttempt.ResultPath,
      ];
      for (const path of written) {
        expect(path === ATTEMPT_PATH || path === HOLDER_PATH).toBe(true);
      }
    });

    it('CreateCode and TestCode are both told which attempt they are on', () => {
      const def = getStateMachineDefinition();
      expect(def.States.CreateCode.Parameters.Payload['regeneration_attempt.$']).toBe(ATTEMPT_PATH);
      expect(def.States.TestCode.Parameters.Payload['regeneration_attempt.$']).toBe(ATTEMPT_PATH);
    });

    it('an attempt is not a new execution, a new run id, or a state row', () => {
      const def = getStateMachineDefinition();
      // `LGR-21a1b5b-2` / `LGR-21a1b5b-4`: no state on the loop starts an
      // execution or writes a row — both loop states are plain Passes.
      for (const name of ['ComputeNextRegenerationAttempt', 'IncrementRegenerationAttempt']) {
        const state = def.States[name];
        expect(state.Type).toBe('Pass');
        expect(state.Resource).toBeUndefined();
      }
      const definitionText = JSON.stringify(def);
      expect(definitionText).not.toContain('states:::states:startExecution');
    });

    it('3.21 holds through the loop: no edge out of it reaches CreateInfrastructure', () => {
      const def = getStateMachineDefinition();
      // Walk the loop: regeneration → CreateCode → TestCode → TestResultChoice.
      // The only ways out of that cycle are the passed branch (CheckCodeApproval,
      // the same state a passed test reaches) and the Default (the human gate).
      expect(def.States.ComputeNextRegenerationAttempt.Next).toBe('IncrementRegenerationAttempt');
      expect(def.States.IncrementRegenerationAttempt.Next).toBe('CreateCode');
      expect(def.States.CreateCode.Next).toBe('TestCode');
      expect(def.States.TestCode.Next).toBe('TestResultChoice');
      const exits = [
        ...def.States.TestResultChoice.Choices.map((c: any) => c.Next),
        def.States.TestResultChoice.Default,
      ];
      expect(exits).not.toContain('CreateInfrastructure');
      expect(exits).toContain('ForceTestReviewApproval');
    });
  });

  // ═══════════════════════════════════════════════════════════════════════
  // p34 R3.3 / R3.4 / R4 / R5 — the two governance gates, and what the
  // inversion did NOT change
  // ═══════════════════════════════════════════════════════════════════════
  //
  // Task 8.1 swapped the ends of two Choice states and claimed the topology was
  // otherwise identical: same states, same edges, same waitForTaskToken set.
  // That claim was verified once, by hand, against a definition dump. These
  // assertions make it checkable by the suite instead, so the next change to
  // either gate cannot quietly add a state, add an edge, or move the reviewer.
  //
  // Expressed against the SYNTHESIZED definition throughout. R8.5 and
  // requirements.md risk 8 both record source-text assertions as the weaker
  // form: a refactor of the TypeScript can satisfy a `grep` while changing what
  // the state machine does. The two source-text tests in
  // tests/contracts/event_schemas/test_sfn_states_contract.py are held
  // assertions and stay, amended; these are the ones that carry the guarantee.
  //
  // Validates: Requirements 3.3, 3.4, 4.1, 4.2, 4.3, 4.5, 5.1, 5.2, 5.3, 5.4
  describe('p34 governance gates — fail closed, same topology', () => {
    const TOKEN_RESOURCE = 'arn:aws:states:::lambda:invoke.waitForTaskToken';

    // The complete waitForTaskToken set as it stands, named as an EXPECTATION
    // so R5.2 is a set equality rather than a count. Eight states: the three
    // human approval gates, the four container-backed steps stamped by the
    // containerBackedTask helper (P28 R19.11), and RunPipeline's Glue callback.
    //
    // Reconciles with test_sfn_states_contract.py's `count == 5`: that test
    // counts RESOURCE LITERALS in the TypeScript source, and one of the five —
    // the literal inside the containerBackedTask helper — stamps four states.
    // 5 literals, 8 synthesized states; the helper's own docstring says so.
    // A count cannot tell a swap from a no-op, so this is the set.
    const EXPECTED_TOKEN_STATES = [
      'CreateCode',
      'CreateGluePipeline',
      'CreateInfrastructure',
      'ForceTestReviewApproval',
      'RunPipeline',
      'TestCode',
      'WaitForCodeApproval',
      'WaitForSchemaMappingApproval',
    ].sort();

    const APPROVAL_PATH = '$.test_code_result.approval_required';
    const SCHEMA_APPROVAL_PATH = '$.schema_mapping_result.approval_required';
    /** The field R3.4 deletes — no state may reference it. */
    const DEAD_APPROVAL_PATH = '$.create_code_result.approval_required';

    /** Every state this state can transition to, by any edge. */
    function outEdges(def: Record<string, any>, name: string): string[] {
      const s = def.States[name];
      const edges: string[] = [];
      if (s.Next) edges.push(s.Next);
      if (s.Default) edges.push(s.Default);
      for (const c of s.Choices ?? []) if (c.Next) edges.push(c.Next);
      for (const c of s.Catch ?? []) if (c.Next) edges.push(c.Next);
      return edges;
    }

    /** Every `Variable` a Choice state compares, across all its rules. */
    function choiceVariables(def: Record<string, any>, name: string): string[] {
      const vars: string[] = [];
      const walk = (rule: any): void => {
        if (rule == null || typeof rule !== 'object') return;
        if (typeof rule.Variable === 'string') vars.push(rule.Variable);
        for (const key of ['And', 'Or', 'Not']) {
          const nested = rule[key];
          if (Array.isArray(nested)) nested.forEach(walk);
          else if (nested) walk(nested);
        }
      };
      (def.States[name].Choices ?? []).forEach(walk);
      return vars;
    }

    it('R4.5: CheckSchemaMappingApproval permits only an explicit boolean false', () => {
      const def = getStateMachineDefinition();
      const state = def.States.CheckSchemaMappingApproval;
      expect(state.Choices).toHaveLength(1);
      // The same ordered three-conjunct shape as its sibling. IsPresent gates
      // existence, IsBoolean gates type, BooleanEquals decides — so absent,
      // null, "false", 0, {} and [] all fall to the Default.
      expect(state.Choices[0]).toEqual({
        And: [
          { Variable: SCHEMA_APPROVAL_PATH, IsPresent: true },
          { Variable: SCHEMA_APPROVAL_PATH, IsBoolean: true },
          { Variable: SCHEMA_APPROVAL_PATH, BooleanEquals: false },
        ],
        Next: 'CheckRegenerationAttempt',
      });
      expect(state.Default).toBe('WaitForSchemaMappingApproval');
      // P32 D2 (`LGR-60c4985-2`): every route into CreateCode passes the counter
      // guard. Asserted on BOTH arms, since the inversion swapped which arm is
      // which and only one of them used to be checked.
      expect(def.States.WaitForSchemaMappingApproval.Next).toBe('CheckRegenerationAttempt');
      expect(outEdges(def, 'CheckSchemaMappingApproval')).not.toContain('CreateCode');
    });

    it('R3.3: CheckCodeApproval reads the merged field and no other approval field', () => {
      const def = getStateMachineDefinition();
      const vars = choiceVariables(def, 'CheckCodeApproval');
      // Every Variable this gate compares — all three conjuncts — is the merged
      // field. Not "contains": a second approval field added anywhere in the
      // rule tree fails here, which is what "and SHALL read no other approval
      // field" means.
      expect(vars).toEqual([APPROVAL_PATH, APPROVAL_PATH, APPROVAL_PATH]);
      expect(new Set(vars)).toEqual(new Set([APPROVAL_PATH]));
    });

    it('R3.4: no state in the definition references create_code_result.approval_required', () => {
      const def = getStateMachineDefinition();
      // Whole-definition search, not just the Choice states: the field is gone
      // from the producer (design D2), so no Payload, ResultSelector, Parameters
      // entry or Choice rule may name it either. A revival would break the ASL
      // at runtime with an unresolvable path rather than fail visibly here, so
      // the assertion is worth the breadth.
      expect(JSON.stringify(def)).not.toContain(DEAD_APPROVAL_PATH);
      for (const name of Object.keys(def.States)) {
        expect(
          JSON.stringify(def.States[name]),
          `${name} must not reference the deleted create_code approval field`,
        ).not.toContain(DEAD_APPROVAL_PATH);
      }
    });

    it('R5.1 / R5.4: the gate stays where the reviewer has context', () => {
      const def = getStateMachineDefinition();
      // R5.1: CheckCodeApproval is still the state immediately after
      // TestResultChoice on the passing route — nothing was inserted between
      // the test result and the gate, so the reviewer still sees both.
      const passed = def.States.TestResultChoice.Choices.find(
        (c: any) => c.Next === 'CheckCodeApproval',
      );
      expect(passed, 'the passed branch must target CheckCodeApproval').toBeDefined();
      expect(def.States.TestCode.Next).toBe('TestResultChoice');
      // R5.4: CreateCode → TestCode with no Choice between them.
      expect(def.States.CreateCode.Next).toBe('TestCode');
      expect(def.States.TestCode.Type).not.toBe('Choice');
      // R5.3: approve-anyway still resumes AT the gate, not past it.
      expect(def.States.ForceTestReviewApproval.Next).toBe('CheckCodeApproval');
    });

    it('R5.2: the waitForTaskToken state set is unchanged — no gate added, none swapped', () => {
      const def = getStateMachineDefinition();
      const tokenStates = Object.keys(def.States)
        .filter((n) => def.States[n].Resource === TOKEN_RESOURCE)
        .sort();
      // Set equality, deliberately not a count: R5.2 says the inversion adds no
      // waitForTaskToken state, and a count of 8 would also pass if a gate were
      // renamed or replaced. `LGR-60c4985-3` (template / ratified) is why this
      // matters beyond tidiness — token states are excluded from retry-resume,
      // so a new one silently inherits a full-TimeoutSeconds stall on resume.
      expect(tokenStates).toEqual(EXPECTED_TOKEN_STATES);
      // The three human gates specifically: an approval_type in the payload is
      // what makes a token state a human gate rather than a work ceiling.
      const approvalGates = tokenStates.filter(
        (n) => def.States[n].Parameters?.Payload?.['approval_type'] !== undefined,
      );
      expect(approvalGates).toEqual([
        'ForceTestReviewApproval',
        'WaitForCodeApproval',
        'WaitForSchemaMappingApproval',
      ]);
    });

    it('R5.2: the inversion swapped edge ends without adding or removing an edge', () => {
      const def = getStateMachineDefinition();
      // Both gates still have exactly the two out-edges they had before task
      // 8.1 — the same destinations, reached through the other arm. This is the
      // honest form of "topology unchanged" at the two states that changed: it
      // would fail if the inversion had introduced an apply-default Pass state
      // (design D4 rejected exactly that on R5.2) or dropped an arm.
      expect(outEdges(def, 'CheckCodeApproval').sort()).toEqual(
        ['CreateInfrastructure', 'WaitForCodeApproval'],
      );
      expect(outEdges(def, 'CheckSchemaMappingApproval').sort()).toEqual(
        ['CheckRegenerationAttempt', 'WaitForSchemaMappingApproval'],
      );
      // And the two gates are still Choice states with no Catch and no Retry —
      // a Choice cannot fail, which is the other half of R4.4.
      for (const name of ['CheckCodeApproval', 'CheckSchemaMappingApproval']) {
        expect(def.States[name].Type).toBe('Choice');
        expect(def.States[name].Catch).toBeUndefined();
        expect(def.States[name].Retry).toBeUndefined();
      }
    });
  });

  // ── P28 R5 — GlueJobMonitorRule DLQ + prefix + config timeout (task 6.3) ──
  // Validates: Requirements 5.3, 5.4, 5.5
  describe('GlueJobMonitorRule DLQ / prefix filter / RunPipeline timeout (P28 R5)', () => {
    const DLQ_QUEUE_NAME = 'agentic-data-ingestion-glue-job-monitor-dlq-dev';
    const MONITOR_RULE_NAME = 'agentic-data-ingestion-glue-job-monitor-dev';
    const TIMEOUT_PATH = '$.pipeline_config.max_job_duration_seconds';

    function findResource(type: string, predicate: (props: any) => boolean) {
      const resources = getTemplate().toJSON().Resources;
      const key = Object.keys(resources).find(
        (k) => resources[k].Type === type && predicate(resources[k].Properties ?? {}),
      );
      return key ? { key, resource: resources[key] } : undefined;
    }

    function getMonitorRule() {
      const found = findResource('AWS::Events::Rule', (p) => p.Name === MONITOR_RULE_NAME);
      expect(found, 'GlueJobMonitorRule must exist').toBeDefined();
      return found!.resource.Properties;
    }

    function getDlq() {
      const found = findResource('AWS::SQS::Queue', (p) => p.QueueName === DLQ_QUEUE_NAME);
      expect(found, 'GlueJobMonitorDLQ queue must exist').toBeDefined();
      return found!;
    }

    // ── R5.3: DLQ wired to the rule target + depth alarm ─────────
    it('a dedicated DLQ queue exists with 14-day retention', () => {
      const { resource } = getDlq();
      expect(resource.Properties.MessageRetentionPeriod).toBe(14 * 24 * 3600);
    });

    it('the rule target dead-letters to the DLQ with a bounded retry policy', () => {
      const rule = getMonitorRule();
      expect(rule.Targets).toHaveLength(1);
      const target = rule.Targets[0];
      // DeadLetterConfig.Arn resolves to the DLQ's ARN (GetAtt on its logical id)
      const { key: dlqKey } = getDlq();
      expect(target.DeadLetterConfig?.Arn?.['Fn::GetAtt']?.[0]).toBe(dlqKey);
      // Bounded redelivery: exhaust retries → DLQ (not infinite retry, not drop)
      expect(target.RetryPolicy?.MaximumRetryAttempts).toBe(8);
      expect(target.RetryPolicy?.MaximumEventAgeInSeconds).toBe(3600);
    });

    it('EventBridge is allowed to send to the DLQ (queue policy)', () => {
      const { key: dlqKey } = getDlq();
      const policy = findResource('AWS::SQS::QueuePolicy', (p) =>
        (p.Queues ?? []).some((q: any) => q?.Ref === dlqKey),
      );
      expect(policy, 'DLQ must have a queue policy').toBeDefined();
      const statements = policy!.resource.Properties.PolicyDocument.Statement;
      const sendStmt = statements.find((s: any) => {
        const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
        return s.Effect === 'Allow' && s.Principal?.Service === 'events.amazonaws.com'
          && actions.includes('sqs:SendMessage');
      });
      expect(sendStmt, 'events.amazonaws.com must be granted sqs:SendMessage').toBeDefined();
    });

    it('an alarm fires on ANY visible message in the DLQ (depth > 0) and pages critical alerts', () => {
      const alarm = findResource('AWS::CloudWatch::Alarm', (p) =>
        p.AlarmName === 'AgenticDataIngestion-GlueJobMonitor-DLQ-Depth-dev');
      expect(alarm, 'DLQ depth alarm must exist').toBeDefined();
      const props = alarm!.resource.Properties;
      expect(props.MetricName).toBe('ApproximateNumberOfMessagesVisible');
      expect(props.Namespace).toBe('AWS/SQS');
      // Dimension binds the alarm to THIS DLQ
      const { key: dlqKey } = getDlq();
      const dim = (props.Dimensions ?? []).find((d: any) => d.Name === 'QueueName');
      expect(dim?.Value?.['Fn::GetAtt']?.[0]).toBe(dlqKey);
      // > 0 — any dead-lettered completion event pages
      expect(props.Threshold).toBe(0);
      expect(props.ComparisonOperator).toBe('GreaterThanThreshold');
      // Wired to the critical-alerts SNS topic like every other alarm here
      expect(props.AlarmActions).toHaveLength(1);
      expect(props.AlarmActions[0].Ref).toMatch(/CriticalAlertsTopic/);
    });

    // ── R5.4: event pattern scoped to this project's jobs ────────
    it('the event pattern carries the project jobName prefix (no account-wide firing)', () => {
      const rule = getMonitorRule();
      expect(rule.EventPattern.source).toEqual(['aws.glue']);
      expect(rule.EventPattern['detail-type']).toEqual(['Glue Job State Change']);
      expect(rule.EventPattern.detail.jobName).toEqual([
        { prefix: 'agentic-data-ingestion-job-' },
      ]);
      expect(rule.EventPattern.detail.state).toEqual(['SUCCEEDED', 'FAILED', 'STOPPED', 'TIMEOUT']);
    });

    // ── R5.5: RunPipeline timeout derives from pipeline config ───
    it('RunPipeline uses TimeoutSecondsPath (config-derived), not a hardcoded TimeoutSeconds', () => {
      const def = getStateMachineDefinition();
      const state = def.States.RunPipeline;
      expect(state.TimeoutSecondsPath).toBe(TIMEOUT_PATH);
      expect(state.TimeoutSeconds).toBeUndefined();
    });

    it('CheckJobTimeoutConfig guards the path: usable value → RunPipeline, anything else → default', () => {
      const def = getStateMachineDefinition();
      const choice = def.States.CheckJobTimeoutConfig;
      expect(choice.Type).toBe('Choice');
      // Exactly one non-default branch: present AND numeric AND > 0
      expect(choice.Choices).toHaveLength(1);
      const usable = choice.Choices[0];
      expect(usable.Next).toBe('RunPipeline');
      expect(usable.And).toHaveLength(3);
      expect(usable.And[0]).toEqual({ Variable: TIMEOUT_PATH, IsPresent: true });
      expect(usable.And[1]).toEqual({ Variable: TIMEOUT_PATH, IsNumeric: true });
      expect(usable.And[2]).toEqual({ Variable: TIMEOUT_PATH, NumericGreaterThan: 0 });
      // Absent/malformed values can never reach TimeoutSecondsPath unresolved
      expect(choice.Default).toBe('ApplyDefaultJobTimeout');
    });

    it('ApplyDefaultJobTimeout injects the documented 6h (21600s) default at the timeout path', () => {
      const def = getStateMachineDefinition();
      const pass = def.States.ApplyDefaultJobTimeout;
      expect(pass.Type).toBe('Pass');
      expect(pass.Result).toBe(21600);
      expect(pass.ResultPath).toBe(TIMEOUT_PATH);
      expect(pass.Next).toBe('RunPipeline');
    });

    // p47 R5 Arm B (ratified): CheckJobTimeoutConfig's configured branch is a
    // forward-compatibility seam with NO PRODUCER, so 100% of executions take
    // ApplyDefaultJobTimeout. The two assertions above already pin the guard's
    // shape (R5.3); this one pins the POSTURE — that the arm is still unfed —
    // which was otherwise only a comment.
    //
    // Scoped to the ASL on purpose. Arm A's producer would be a trigger Lambda
    // writing max_job_duration_seconds into the execution input, which this
    // assertion would NOT see, and correctly so: that is Arm A implemented as
    // designed, and the design docs are the place it gets recorded. What this
    // catches is a producer appearing *inside the state machine*, where it would
    // silently make the documented posture false.
    it('R5 Arm B: ApplyDefaultJobTimeout is the only state that writes the timeout path', () => {
      const def = getStateMachineDefinition();

      // ResultPath is the ASL surface by which a state writes into $.pipeline_config.
      const writers = Object.entries(def.States)
        .filter(([, s]: [string, any]) => typeof s.ResultPath === 'string' && s.ResultPath.startsWith('$.pipeline_config'))
        .map(([name]) => name);
      expect(writers).toEqual(['ApplyDefaultJobTimeout']);

      // Belt and braces against any other write surface (ResultSelector, a
      // Parameters injection, a second Pass): exactly three states may mention
      // the identifier at all, each in a known role — the writer above, the
      // Choice that reads it, and RunPipeline's TimeoutSecondsPath.
      const mentions = Object.entries(def.States)
        .filter(([, s]) => JSON.stringify(s).includes('max_job_duration_seconds'))
        .map(([name]) => name)
        .sort();
      expect(mentions).toEqual(['ApplyDefaultJobTimeout', 'CheckJobTimeoutConfig', 'RunPipeline']);
    });
  });

  // ── P28 R8.1 — pipelineStateUpdateFn removed (task 14.1) ──────
  // Validates: Requirements 8.1
  // The EventBridge-triggered state mirror wrote with the wrong key schema
  // (pipeline_id instead of run_id) — a 100%-ValidationException write since
  // inception. It was deleted as dead code; these assertions pin its absence
  // so it cannot silently return.
  describe('pipelineStateUpdateFn removal (P28 R8.1)', () => {
    function resourcesOfType(type: string): any[] {
      const resources = getTemplate().toJSON().Resources;
      return Object.values(resources)
        .filter((r: any) => r.Type === type)
        .map((r: any) => r.Properties ?? {});
    }

    it('no pipeline-state-updater Lambda is synthesized', () => {
      const fnNames = resourcesOfType('AWS::Lambda::Function')
        .map((p) => p.FunctionName)
        .filter((n): n is string => typeof n === 'string');
      expect(fnNames.some((n) => n.startsWith('agentic-data-ingestion-pipeline-state-updater'))).toBe(false);
    });

    it('no pipeline-state-updater IAM role is synthesized', () => {
      const roleNames = resourcesOfType('AWS::IAM::Role')
        .map((p) => p.RoleName)
        .filter((n): n is string => typeof n === 'string');
      expect(roleNames.some((n) => n.startsWith('agentic-data-ingestion-pipeline-state-updater-role'))).toBe(false);
    });

    it('no rule consumes Pipeline State Change events (PipelineStateChangeRule deleted)', () => {
      const rules = resourcesOfType('AWS::Events::Rule');
      const stateChangeRule = rules.find((p) =>
        p.Name === 'agentic-data-ingestion-pipeline-state-changes-dev'
        || (p.EventPattern?.['detail-type'] ?? []).includes('Pipeline State Change'));
      expect(stateChangeRule, 'PipelineStateChangeRule must not exist').toBeUndefined();
    });

    it('the sibling approval-status updater and its rule are untouched', () => {
      const fnNames = resourcesOfType('AWS::Lambda::Function')
        .map((p) => p.FunctionName)
        .filter((n): n is string => typeof n === 'string');
      expect(fnNames).toContain('agentic-data-ingestion-approval-status-updater-dev');
      const rules = resourcesOfType('AWS::Events::Rule');
      const approvalRule = rules.find((p) => p.Name === 'agentic-data-ingestion-approval-status-changes-dev');
      expect(approvalRule, 'ApprovalStatusChangeRule must remain').toBeDefined();
    });
  });

  // ── P28 R19.11 — container-backed steps use waitForTaskToken (task 15.5) ──
  // Validates: Requirements 19.11
  // The four container-backed steps dispatch the token to the AgentCore
  // container and the proxy Lambda returns within seconds (no idle billing);
  // the container closes the loop via SendTaskSuccess/SendTaskFailure. These
  // assertions run against the SYNTHESIZED state-machine definition so a
  // regression back to plain LambdaInvoke (which re-introduces idle-proxy
  // billing for the container's full duration) fails loudly.
  describe('Container-backed waitForTaskToken states (P28 R19.11)', () => {
    // state → [ResultPath, TimeoutSeconds]. Timeouts are the step's previous
    // work ceiling (Lambda timeout / container read-timeout) + a
    // dispatch/callback buffer: 900s + 300 = 1200; create_infrastructure's
    // 600s ceiling + 300 = 900.
    const CONTAINER_STATES: Record<string, [string, number]> = {
      CreateCode: ['$.create_code_result', 1200],
      TestCode: ['$.test_code_result', 1200],
      CreateGluePipeline: ['$.create_glue_pipeline_result', 1200],
      CreateInfrastructure: ['$.create_infrastructure_result', 900],
    };

    it.each(Object.keys(CONTAINER_STATES))(
      '%s is a waitForTaskToken task carrying $$.Task.Token in the payload',
      (name) => {
        const def = getStateMachineDefinition();
        const state = def.States[name];
        expect(state.Type).toBe('Task');
        expect(state.Resource).toBe('arn:aws:states:::lambda:invoke.waitForTaskToken');
        // The token travels IN the dispatch payload — the container (or the
        // Lambda-side step_task_token relay) closes the loop with it.
        expect(state.Parameters.Payload['task_token.$']).toBe('$$.Task.Token');
      },
    );

    it.each(Object.entries(CONTAINER_STATES))(
      '%s preserves its pre-token ResultPath and carries an explicit TimeoutSeconds backstop',
      (name, [resultPath, timeoutSeconds]) => {
        const def = getStateMachineDefinition();
        const state = def.States[name];
        // SendTaskSuccess output lands at the SAME ResultPath the old
        // payloadResponseOnly LambdaInvoke used — downstream JSONPath
        // consumers (TestResultChoice, RecordFailure PROGRESS_ORDER) are
        // unchanged.
        expect(state.ResultPath).toBe(resultPath);
        // waitForTaskToken defaults to a ~1-year timeout: the explicit value
        // is the backstop for a container that crashes after acking the
        // dispatch (no HeartbeatSeconds in use).
        expect(state.TimeoutSeconds).toBe(timeoutSeconds);
        expect(state.HeartbeatSeconds).toBeUndefined();
      },
    );

    it.each(Object.keys(CONTAINER_STATES))(
      '%s keeps the R1 Retry (TransientStepFailure + Lambda service errors) and Catch → RecordFailure',
      (name) => {
        const def = getStateMachineDefinition();
        const state = def.States[name];
        // SendTaskFailure(error=<ExceptionClassName>) and Lambda invocation
        // errors surface the SAME error names — one Retry/Catch matches both
        // the dispatch path and the container callback path.
        expect(state.Retry[0].ErrorEquals).toEqual([
          'TransientStepFailure', 'Lambda.ServiceException', 'Lambda.TooManyRequestsException',
        ]);
        expect(state.Catch[0].ErrorEquals).toEqual(['States.ALL']);
        expect(state.Catch[0].Next).toBe('RecordFailure');
        expect(state.Catch[0].ResultPath).toBe('$.error');
      },
    );

    it('the payloads keep their per-step input bindings alongside the token', () => {
      const def = getStateMachineDefinition();
      const payloadKeys = (name: string) => Object.keys(def.States[name].Parameters.Payload);
      expect(payloadKeys('CreateCode')).toEqual(expect.arrayContaining([
        'run_id.$', 'pipeline_id.$', 'mapping_id.$', 'source_schema.$', 'source_format.$', 'task_token.$',
      ]));
      expect(payloadKeys('TestCode')).toEqual(expect.arrayContaining([
        'run_id.$', 'pipeline_id.$', 'code_version_id.$', 'code_s3_uri.$', 'source_data_uri.$', 'mapping_id.$', 'task_token.$',
      ]));
      expect(payloadKeys('CreateGluePipeline')).toEqual(expect.arrayContaining([
        'run_id.$', 'pipeline_id.$', 'file_size_bytes.$', 'file_count.$', 'task_token.$',
      ]));
      expect(payloadKeys('CreateInfrastructure')).toEqual(expect.arrayContaining([
        'run_id.$', 'pipeline_id.$', 'task_token.$',
      ]));
    });

    it('transition chain through the token states is unchanged (output shape preserved)', () => {
      const def = getStateMachineDefinition();
      // SendTaskSuccess carries the exact handler JSON, so the pre-token
      // transitions keep reading the same payloads at the same paths.
      expect(def.States.CreateCode.Next).toBe('TestCode');
      expect(def.States.TestCode.Next).toBe('TestResultChoice');
      expect(def.States.CreateInfrastructure.Next).toBe('CreateGluePipeline');
      expect(def.States.CreateGluePipeline.Next).toBe('CreateQualityChecks');
    });
  });

  // ═══════════════════════════════════════════════════════════════════════
  // p34 R6 — the approval window, read off the synthesized definition
  // ═══════════════════════════════════════════════════════════════════════
  //
  // R6 bounds how long a pipeline can sit waiting for a human. The window used
  // to be 604800s (7 days) at three separate literals; it is now one exported
  // constant, APPROVAL_TIMEOUT_SECONDS (72h). Two things make that worth
  // asserting rather than reading:
  //
  //   • `LGR-60c4985-3` (template / ratified) excludes the waitForTaskToken
  //     steps from retry-resume caching, because `step_handler` is the
  //     outermost decorator and a cached return fires before the token is
  //     sent — so TimeoutSeconds IS the worst-case stall for these states, not
  //     a theoretical backstop. R6 shortens the stall; it does not remove the
  //     exclusion.
  //   • The scope is approval token states, NOT all token states. Four
  //     container-backed steps are also waitForTaskToken but carry WORK
  //     ceilings (P28 R19.11) and RunPipeline carries TimeoutSecondsPath from
  //     pipeline config (P28 R5.5). Asserting over "every token state" would
  //     be wrong, so the partition is derived and asserted explicitly below.
  //
  // Everything here reads the SYNTHESIZED definition and derives the state set
  // from it, rather than grepping the TypeScript or restating a state list —
  // `requirements.md` risk 8 is that a source-text check can pass while the
  // behaviour changes.
  //
  // Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.6
  describe('approval window: TimeoutSeconds (p34 R6)', () => {
    const TOKEN_RESOURCE = 'arn:aws:states:::lambda:invoke.waitForTaskToken';

    // The three human gates, and the approval_type each publishes. Named as an
    // EXPECTATION — the tests below derive the actual set from the definition
    // and compare, so a fourth approval gate added without a window fails here
    // rather than shipping on the ASL default (~1 year).
    const EXPECTED_APPROVAL_GATES: Record<string, string> = {
      WaitForSchemaMappingApproval: 'schema_mapping',
      ForceTestReviewApproval: 'test_failure',
      WaitForCodeApproval: 'code_generation',
    };

    // RunPipeline's config-derived ceiling (P28 R5.5). Same literal the P28 R5
    // block pins as TIMEOUT_PATH; MAX_JOB_DURATION_PATH is module-local to the
    // stack and not exported, so it is restated rather than imported.
    const JOB_TIMEOUT_PATH = '$.pipeline_config.max_job_duration_seconds';

    // Token states that are deliberately NOT on the approval window: work
    // ceilings (P28 R19.11), asserted in the container block above.
    const WORK_CEILING_TOKEN_STATES: Record<string, number> = {
      CreateCode: 1200,
      TestCode: 1200,
      CreateGluePipeline: 1200,
      CreateInfrastructure: 900,
    };

    /** Every state in the definition whose Resource is the token integration. */
    function tokenStateNames(def: Record<string, any>): string[] {
      return Object.keys(def.States)
        .filter((n) => def.States[n].Resource === TOKEN_RESOURCE)
        .sort();
    }

    /** A token state is an APPROVAL gate iff its payload names an approval_type. */
    function approvalGateNames(def: Record<string, any>): string[] {
      return tokenStateNames(def).filter(
        (n) => def.States[n].Parameters?.Payload?.['approval_type'] !== undefined,
      );
    }

    /** Every state this state can transition to, by any edge. */
    function outEdges(def: Record<string, any>, name: string): string[] {
      const s = def.States[name];
      const edges: string[] = [];
      if (s.Next) edges.push(s.Next);
      if (s.Default) edges.push(s.Default);
      for (const c of s.Choices ?? []) if (c.Next) edges.push(c.Next);
      for (const c of s.Catch ?? []) if (c.Next) edges.push(c.Next);
      return edges;
    }

    /** Forward reachability closure from a set of states. */
    function reachableFrom(def: Record<string, any>, seeds: string[]): Set<string> {
      const seen = new Set<string>();
      const queue = [...seeds];
      while (queue.length) {
        const name = queue.shift()!;
        if (seen.has(name) || !def.States[name]) continue;
        seen.add(name);
        queue.push(...outEdges(def, name));
      }
      return seen;
    }

    it('the approval gates are exactly the three human gates, derived from the definition', () => {
      const def = getStateMachineDefinition();
      // Derived, not restated: the set comes from Resource + approval_type in
      // the synthesized ASL. A new approval gate lands in this set
      // automatically and then has to satisfy the window assertions below.
      expect(approvalGateNames(def).sort()).toEqual(Object.keys(EXPECTED_APPROVAL_GATES).sort());
      for (const [name, approvalType] of Object.entries(EXPECTED_APPROVAL_GATES)) {
        expect(def.States[name].Parameters.Payload['approval_type']).toBe(approvalType);
        expect(def.States[name].Parameters.Payload['task_token.$']).toBe('$$.Task.Token');
      }
    });

    it('all three approval gates carry APPROVAL_TIMEOUT_SECONDS (R6.1, R6.2, R6.6)', () => {
      const def = getStateMachineDefinition();
      for (const name of approvalGateNames(def)) {
        const state = def.States[name];
        // R6.6: the constant, not a number. Asserting the import is what makes
        // "expressed once" checkable — a fourth literal would fail this.
        expect(state.TimeoutSeconds, `${name} must carry the approval window`)
          .toBe(APPROVAL_TIMEOUT_SECONDS);
        // R6.1: the stated ceiling. Independent of the constant's current value,
        // so raising the constant past 72h fails here rather than silently.
        expect(state.TimeoutSeconds).toBeLessThanOrEqual(259200);
        // A human gate's window is static: a config-derived path would put the
        // ceiling back outside the repo, which is what design D6 rejected.
        expect(state.TimeoutSecondsPath, `${name} must not derive its window from state`)
          .toBeUndefined();
        expect(state.HeartbeatSeconds).toBeUndefined();
      }
    });

    it('no approval gate carries a different window (R6.2, R6.6)', () => {
      const def = getStateMachineDefinition();
      const windows = new Set(approvalGateNames(def).map((n) => def.States[n].TimeoutSeconds));
      // One distinct value across all approval gates — this is R6.2 ("the same
      // TimeoutSeconds value as WaitForCodeApproval") stated as a set property
      // rather than three pairwise comparisons, so it holds for a fourth gate.
      expect(windows.size).toBe(1);
      expect([...windows]).toEqual([APPROVAL_TIMEOUT_SECONDS]);
    });

    it('the work-ceiling token states are deliberately off the approval window', () => {
      const def = getStateMachineDefinition();
      // The partition is asserted, not assumed: every token state is either an
      // approval gate, a container step on a work ceiling, or RunPipeline. A
      // new token state that is none of these fails here, which is the point —
      // it forces a decision about which ceiling it belongs on.
      expect(tokenStateNames(def)).toEqual(
        [
          ...Object.keys(EXPECTED_APPROVAL_GATES),
          ...Object.keys(WORK_CEILING_TOKEN_STATES),
          'RunPipeline',
        ].sort(),
      );
      for (const [name, ceiling] of Object.entries(WORK_CEILING_TOKEN_STATES)) {
        // P28 R19.11: a work ceiling bounds a container, not a person.
        expect(def.States[name].TimeoutSeconds).toBe(ceiling);
        expect(def.States[name].TimeoutSeconds).not.toBe(APPROVAL_TIMEOUT_SECONDS);
      }
      // P28 R5.5: RunPipeline's ceiling comes from pipeline config, guaranteed
      // resolvable by the CheckJobTimeoutConfig guard directly upstream. Also
      // asserted in the P28 R5 block above ('RunPipeline uses
      // TimeoutSecondsPath (config-derived), not a hardcoded TimeoutSeconds');
      // repeated here so R6's scope exclusion is legible where R6 is asserted.
      expect(def.States.RunPipeline.TimeoutSecondsPath).toBe(JOB_TIMEOUT_PATH);
      expect(def.States.RunPipeline.TimeoutSeconds).toBeUndefined();
    });

    it('a timed-out approval routes to RecordFailure (R6.3)', () => {
      const def = getStateMachineDefinition();
      for (const name of approvalGateNames(def)) {
        const catches = def.States[name].Catch;
        expect(catches, `${name} must have Catch`).toBeDefined();
        expect(catches.length, `${name} Catch must not be empty`).toBeGreaterThan(0);
        // States.Timeout is INSIDE States.ALL, so no dedicated ErrorEquals
        // entry is needed — R6.3 already holds today and this asserts it
        // rather than implementing it. What has to stay true is that no Catch
        // entry sends a timeout anywhere other than the failure chain, whatever
        // the ordering: hence every entry, not just the first.
        expect(catches.map((c: any) => c.Next)).toEqual(catches.map(() => 'RecordFailure'));
        const catchAll = catches.find((c: any) => (c.ErrorEquals ?? []).includes('States.ALL'));
        expect(catchAll, `${name} must catch States.ALL (which covers States.Timeout)`).toBeDefined();
        expect(catchAll.ResultPath).toBe('$.error');
      }
      // The failure chain is terminal: RecordFailure → ReleaseLockFailure →
      // PipelineFailed, a Fail state. Asserted above; restated as the reason a
      // timeout cannot rejoin the happy path.
      expect(def.States.PipelineFailed.Type).toBe('Fail');
    });

    it('R6.4: WaitForCodeApproval reaches CreateInfrastructure only on a granted approval', () => {
      const def = getStateMachineDefinition();
      const gate = def.States.WaitForCodeApproval;
      // waitForTaskToken: the ONLY way to the Next edge is SendTaskSuccess —
      // i.e. an approver granting the approval. There is no timer, no polling
      // exit and no default transition out of a token task.
      expect(gate.Resource).toBe(TOKEN_RESOURCE);
      // Exactly two edges out: the success edge and the failure edge.
      expect(outEdges(def, 'WaitForCodeApproval').sort()).toEqual(
        ['CreateInfrastructure', 'RecordFailure'],
      );
      expect(gate.Next).toBe('CreateInfrastructure');
      // Every NON-success edge — today just the States.ALL Catch, which is the
      // route a timeout and a rejection both take — must be unable to reach
      // CreateInfrastructure, however far you walk.
      const nonSuccessTargets = outEdges(def, 'WaitForCodeApproval').filter(
        (t) => t !== gate.Next,
      );
      expect(nonSuccessTargets).toEqual(['RecordFailure']);
      const closure = reachableFrom(def, nonSuccessTargets);
      // Non-vacuous: the walk really did traverse the failure chain to its
      // terminal Fail state before concluding CreateInfrastructure is absent.
      expect([...closure]).toEqual(expect.arrayContaining([
        'RecordFailure', 'ReleaseLockFailure', 'PipelineFailed',
      ]));
      expect([...closure].sort()).not.toContain('CreateInfrastructure');
      //
      // Scope note, deliberate: this walk starts at WaitForCodeApproval, not at
      // CheckCodeApproval. CheckCodeApproval.Default was 'CreateInfrastructure'
      // when this test was written — that permissive residual branch is the
      // defect task 8.1 has since inverted, and the Default is now
      // 'WaitForCodeApproval'. It is asserted (and amended) by the R4/R8.5 tests
      // that own it, not here. Writing R6.4 against the gate state is what let
      // this assertion hold, unchanged and still passing, across that inversion
      // — so task 8.2's Choice amendments read as the deliberate routing fix
      // they are rather than as a repair to this test.
    });
  });
});

// ═════════════════════════════════════════════════════════════════════════
// p30a R1.6 — which agent each EventBridge rule actually invokes
// ═════════════════════════════════════════════════════════════════════════
//
// Two claims are pinned here, and the SECOND one is the point of the block.
//
// R1.1 repointed the weekly cost-optimization schedule from the alerts topic
// to the generic dispatch Lambda. R1.5 deliberately did NOT do the same for
// data quality. Nothing in the types, the synth, or the shape of the code
// distinguishes those two states: both rules are a `new events.Rule` plus one
// `addTarget`, and a well-meaning "finish wiring up the agents" edit would
// flip both without anyone noticing that only one of them was decided.
//
// So the data-quality assertion is a lock on a decision, not a description of
// current output. It is expected to be deleted one day — deliberately, in the
// same change that turns automatic data-quality invocation on, by someone who
// has read the comment above `GlueJobSuccessDataQualityRule` in
// lib/orchestration-stack.ts and accepted what it says about scope and cost.
// Deleting it to turn a red suite green is the one use that defeats its
// purpose. If it is failing, the wiring changed; go read that comment.
//
// Validates: Requirements 1.6
describe('p30a R1.6 — EventBridge agent-trigger rule targets', () => {
  const COST_RULE = 'agentic-data-ingestion-cost-optimization-schedule-dev';
  const DQ_RULE = 'agentic-data-ingestion-data-quality-trigger-dev';
  const SELF_HEALING_RULE = 'agentic-data-ingestion-self-healing-trigger-dev';
  const CRITICAL_ALERTS_TOPIC = 'agentic-data-ingestion-critical-alerts-dev';
  const DISPATCH_FN = 'agentic_data_ingestion_agent_dispatch';

  // Static texts captured LIVE from the deployed rules in 444455556666 /
  // us-west-2 via `events list-targets-by-rule` (p30a Phase 0.3), not copied
  // out of the source. So these are a pin on what is actually running, not a
  // snapshot of what this file happens to say today.
  const DQ_STATIC_TEXT = 'Data quality agent triggered for Glue job success';
  const SELF_HEALING_STATIC_TEXT = 'Self-healing agent triggered for Glue job failure';

  function resources(): Record<string, any> {
    return synthOrchestration().template.toJSON().Resources ?? {};
  }

  function ruleEntry(ruleName: string): { logicalId: string; props: any } {
    const all = resources();
    const logicalId = Object.keys(all).find(
      (k) => all[k].Type === 'AWS::Events::Rule' && all[k].Properties?.Name === ruleName,
    );
    expect(logicalId, `rule ${ruleName} must exist`).toBeDefined();
    return { logicalId: logicalId!, props: all[logicalId!].Properties };
  }

  /**
   * The rendered ARN of the dispatch Lambda as the Orchestration stack sees
   * it. Resolved through the stack rather than written out, because the
   * cross-stack import name embeds the construct's logical-id hash
   * (`...FnGetAttAD98A6BDCFArn...`). A literal would break on any rename that
   * changed nothing about the wiring, and a test that fails for a reason its
   * message cannot explain teaches the next reader to delete it.
   */
  function dispatchLambdaArn(): any {
    const { stack, compute } = synthOrchestration();
    const arn = stack.resolve(compute.agentDispatchLambda.functionArn);
    const exportName = arn?.['Fn::ImportValue'];
    expect(
      exportName,
      'the dispatch Lambda ARN must render as a cross-stack import — if it does not, nothing in the Orchestration stack references the function at all, which means the schedule target was removed',
    ).toBeTypeOf('string');

    // Walk the import back to the function it names. Without this the helper
    // would only restate whatever the harness passed in as agentDispatchLambda;
    // with it, the assertion is that the schedule reaches the function actually
    // called agentic_data_ingestion_agent_dispatch.
    const computeJson = Template.fromStack(cdk.Stack.of(compute.agentDispatchLambda)).toJSON();
    const output = (Object.values(computeJson.Outputs ?? {}) as any[]).find(
      (o) => o?.Export?.Name === exportName,
    );
    expect(output, `compute stack must export ${exportName}`).toBeDefined();
    const fnLogicalId = output.Value['Fn::GetAtt'][0];
    expect(
      computeJson.Resources[fnLogicalId]?.Properties?.FunctionName,
      'the schedule target must be the agent dispatch function, not some other Lambda',
    ).toBe(DISPATCH_FN);
    return arn;
  }

  /**
   * The rendered ARN of the critical-alerts topic. `Ref` on an SNS topic
   * yields its ARN, so this resolves to `{ Ref: <logicalId> }`; the logical id
   * is then read back out of the template to confirm it is the critical-alerts
   * topic and not the approvals topic the stack also creates. Same reason as
   * dispatchLambdaArn: identity by construct, never by hash literal.
   */
  function criticalAlertsTopicArn(): any {
    const { stack } = synthOrchestration();
    const arn = stack.resolve(stack.criticalAlertsTopic.topicArn);
    const logicalId = arn?.Ref;
    expect(logicalId, 'the topic ARN must render as a Ref to a topic in this stack').toBeTypeOf(
      'string',
    );
    expect(
      resources()[logicalId]?.Properties?.TopicName,
      'the pinned SNS target must be the critical-alerts topic, not the approvals topic',
    ).toBe(CRITICAL_ALERTS_TOPIC);
    return arn;
  }

  /** Every AWS::Lambda::Permission whose SourceArn is this rule. */
  function lambdaPermissionsForRule(ruleLogicalId: string): any[] {
    return Object.values(resources()).filter(
      (r: any) =>
        r.Type === 'AWS::Lambda::Permission' &&
        r.Properties?.SourceArn?.['Fn::GetAtt']?.[0] === ruleLogicalId,
    );
  }

  // ── Assertion 1 — the cost schedule reaches the agent ────────────────
  // R1.1. The pre-p30a target was the alerts topic with the static text
  // "Cost optimization agent weekly run triggered", which invoked nothing —
  // which is why no step_name="cost_optimization" state row has ever existed.

  it('the cost-optimization schedule targets the agent dispatch Lambda, not the alerts topic', () => {
    const { props } = ruleEntry(COST_RULE);
    expect(props.Targets, 'the schedule must have exactly one target').toHaveLength(1);
    // The SNS check runs FIRST on purpose. On a revert to the alerts topic the
    // dispatch Lambda stops being referenced by this stack at all, so
    // dispatchLambdaArn() would fail first — accurately, but with a message
    // about cross-stack imports that reads like a broken harness rather than
    // "the weekly agent run is switched off". Order the assertions so the
    // diagnosis a contributor sees is the consequence, not the mechanism.
    expect(
      props.Targets[0].Arn,
      'a regression to the alerts topic means the cost agent silently stops running — SNS publishes static text and invokes nothing, which is the pre-p30a state that left the Cost Recommendations panel permanently empty',
    ).not.toEqual(criticalAlertsTopicArn());
    expect(
      props.Targets[0].Arn,
      'the weekly schedule must reach the dispatch Lambda — no other target type can invoke an AgentCore runtime',
    ).toEqual(dispatchLambdaArn());
  });

  it('carries the static input the dispatcher reads: endpoint env var NAME, step_name, payload', () => {
    // The whole contract with the dispatch Lambda is this JSON blob. It passes
    // the NAME of an environment variable rather than a URL so the endpoint
    // stays single-sourced on the dispatch function; a literal here would go
    // stale on an AgentCore redeploy without failing anything.
    const { props } = ruleEntry(COST_RULE);
    expect(typeof props.Targets[0].Input).toBe('string');
    expect(JSON.parse(props.Targets[0].Input)).toEqual({
      agent_endpoint_env: 'COST_OPTIMIZATION_AGENT_ENDPOINT',
      step_name: 'cost_optimization',
      payload: { trigger: 'weekly_schedule' },
    });
  });

  it('EventBridge is granted invoke on the dispatch function, scoped to this rule alone', () => {
    // Without the permission the rule fires and the invoke is denied — a
    // failure visible only in the rule's FailedInvocations metric.
    const { logicalId } = ruleEntry(COST_RULE);
    const perms = lambdaPermissionsForRule(logicalId);
    expect(perms, 'exactly one events.amazonaws.com invoke grant for this rule').toHaveLength(1);
    expect(perms[0].Properties.Action).toBe('lambda:InvokeFunction');
    expect(perms[0].Properties.Principal).toBe('events.amazonaws.com');
    expect(perms[0].Properties.FunctionName).toEqual(dispatchLambdaArn());
  });

  it('still fires weekly on Monday 06:00 UTC', () => {
    // A correct target on a broken schedule is still an agent that never runs.
    expect(ruleEntry(COST_RULE).props.ScheduleExpression).toBe('cron(0 6 ? * MON *)');
  });

  // ── Assertion 2 — the data-quality rule is HELD, on purpose ──────────
  // This is the mechanism that keeps the deferral deliberate. See the block
  // comment above, and the comment on the rule itself in
  // lib/orchestration-stack.ts.

  it('the data-quality rule target is UNCHANGED — SNS notification, not the dispatch Lambda', () => {
    const { props } = ruleEntry(DQ_RULE);
    expect(
      props.Targets,
      [
        'p30a R1.5/R1.6: the data-quality rule target is a deliberate hold, not a stale expectation.',
        'Read the comment above GlueJobSuccessDataQualityRule in deploy-cdk/lib/orchestration-stack.ts',
        'before changing this — it records what turning DQ on actually commits to, and that the IAM',
        'plumbing is already in place, so wiring it is one line there plus deleting this test.',
        'If that is what you are doing: delete this test in the same change and say so in the MR.',
        'If it is not: something re-wired data quality without deciding to.',
      ].join(' '),
    ).toEqual([
      {
        Arn: criticalAlertsTopicArn(),
        Id: 'Target0',
        Input: JSON.stringify(DQ_STATIC_TEXT),
      },
    ]);
  });

  it('no Lambda invoke grant exists for the data-quality rule', () => {
    // The target assertion above could be satisfied while a second target or a
    // leftover grant quietly wired the agent. Absence of the grant is the
    // independent check: EventBridge cannot invoke any function from this rule.
    const { logicalId } = ruleEntry(DQ_RULE);
    expect(
      lambdaPermissionsForRule(logicalId),
      'a Lambda invoke grant on the data-quality rule means DQ was wired — see the rule comment in orchestration-stack.ts',
    ).toHaveLength(0);
  });

  // ── Rule 1 — self-healing, pinned for a different reason ─────────────
  // R1.6 does not ask for this and it is NOT a hold: self-healing IS wired,
  // just not through this rule — job_completion async-invokes the self-healing
  // dispatcher on Glue failure. It is pinned because it sits directly above
  // the DQ rule and is structurally identical, so it is collateral in exactly
  // the bulk edit the DQ assertion exists to catch. Deleting THIS test blocks
  // nothing; it only removes the record that the rule is notification-only.
  it('the self-healing rule is notification-only — self-healing reaches its agent via job_completion', () => {
    const { logicalId, props } = ruleEntry(SELF_HEALING_RULE);
    expect(props.Targets).toEqual([
      {
        Arn: criticalAlertsTopicArn(),
        Id: 'Target0',
        Input: JSON.stringify(SELF_HEALING_STATIC_TEXT),
      },
    ]);
    expect(lambdaPermissionsForRule(logicalId)).toHaveLength(0);
  });
});

// ── p30 R9.3 — SQS partial batch failure reporting ───────────────────────
//
// Added here rather than in a new file because this file already synthesizes the
// OrchestrationStack once and caches it; a second file means a second synth of a
// 20-Lambda stack, which is the memory pressure that pushed
// p30-processed-file-ledger.test.ts to source-level string assertions. Here the
// real synthesized resource is available for free, so it is asserted directly.
describe('p30 R9.3 — TriggerSqsMapping reports batch item failures', () => {
  const QUEUE_NAME = 'agentic-data-ingestion-pipeline-triggers-dev';

  // Which construct id the mapping's EventSourceArn must GetAtt, and the id it
  // must NOT — spelled as prefixes because CDK appends a hash to each.
  const TRIGGER_QUEUE_ID = 'PipelineTriggerQueue';
  const DLQ_ID = 'PipelineTriggerDLQ';

  function mappingsFor(queueConstructIdPrefix: string): any[] {
    const mappings = synthOrchestrationStack().findResources(
      'AWS::Lambda::EventSourceMapping',
    );
    return (Object.values(mappings) as any[]).filter((entry) => {
      const getAtt = entry.Properties?.EventSourceArn?.['Fn::GetAtt'];
      return Array.isArray(getAtt) && String(getAtt[0]).startsWith(queueConstructIdPrefix);
    });
  }

  function triggerMapping(): any {
    // This used to assert the stack held exactly ONE event source mapping, on
    // the grounds that the trigger queue was the only event source and that a
    // second mapping should fail every test in this block rather than let one
    // quietly check the wrong resource. p54 task 7.7 added a second mapping —
    // the DLQ's, feeding QueueLossRecorderFunction — so the premise is gone but
    // the GUARANTEE it bought is not, and that is what is preserved here:
    // exactly one mapping may name the trigger queue, so this still cannot
    // return the wrong resource, and a third mapping on the same queue still
    // fails every test in the block. The DLQ mapping is asserted separately
    // below so the count growing stays a deliberate, visible fact.
    //
    // Note `PipelineTriggerQueue` is a prefix of nothing else, while
    // `PipelineTriggerDLQ` is a *different* prefix — the filter cannot confuse
    // them, and `does not also match the DLQ` below is the check on that.
    const entries = mappingsFor(TRIGGER_QUEUE_ID);
    expect(
      entries,
      'exactly one event source mapping should name the pipeline trigger queue',
    ).toHaveLength(1);
    return entries[0];
  }

  function dlqMapping(): any {
    const entries = mappingsFor(DLQ_ID);
    expect(
      entries,
      'exactly one event source mapping should name the pipeline trigger DLQ',
    ).toHaveLength(1);
    return entries[0];
  }

  it('declares ReportBatchItemFailures', () => {
    // Without this the handler's `batchItemFailures` return value is IGNORED and
    // Lambda re-delivers the whole batch on any reported failure — so records
    // that already started an execution are processed a second time. The two
    // halves of R9.3 are independent and neither works alone; this is the half
    // that lives in infrastructure and cannot be caught by a Python test.
    expect(triggerMapping().Properties.FunctionResponseTypes).toEqual([
      'ReportBatchItemFailures',
    ]);
  });

  it('points at a queue by reference rather than a hard-coded ARN', () => {
    const arn = triggerMapping().Properties.EventSourceArn;
    expect(JSON.stringify(arn)).toContain('Arn');
  });

  it('still has batchSize 1 — so partial reporting is latent today, not live', () => {
    // Recorded rather than changed. With batchSize 1 a batch never holds two
    // records, so R9.3's own scenario ("record 2 conflicts, record 1 stays
    // processed") cannot arise in this deployment: the response is equivalent to
    // the old raise. The handler contract is still the correct one and becomes
    // load-bearing the moment batchSize is raised — which is a throughput
    // decision R9.3 does not make. If this assertion is updated, the two-record
    // integration case (task 4.12) stops being hypothetical.
    expect(triggerMapping().Properties.BatchSize).toBe(1);
  });

  it('keeps maximumConcurrency, which is what actually bounds fan-out', () => {
    expect(triggerMapping().Properties.ScalingConfig).toEqual({ MaximumConcurrency: 5 });
  });

  it('references the pipeline-triggers queue by name somewhere in the stack', () => {
    // The mapping's EventSourceArn is a Ref/GetAtt, so the physical name is
    // asserted on the queue resource instead.
    synthOrchestrationStack().hasResourceProperties('AWS::SQS::Queue', {
      QueueName: QUEUE_NAME,
    });
  });

  it('does not also match the DLQ — the two mappings are distinguishable', () => {
    // The selector above is a prefix match, so this is the check that the prefix
    // actually discriminates. If `PipelineTriggerQueue` ever became a prefix of
    // the DLQ's id (or vice versa) every assertion in this block would silently
    // start reading two resources as one.
    expect(triggerMapping()).not.toBe(dlqMapping());
    expect(mappingsFor(TRIGGER_QUEUE_ID).length + mappingsFor(DLQ_ID).length).toBe(
      Object.keys(
        synthOrchestrationStack().findResources('AWS::Lambda::EventSourceMapping'),
      ).length,
    );
  });
});

// ── p54 R10.1/R10.3 — the DLQ has a consumer, and it is enabled ──────────
//
// The first assertions of any kind on this DLQ. Before p54 nothing in either CDK
// suite mentioned `PipelineTriggerDLQ`, `RedrivePolicy`, `maxReceiveCount` or
// `visibilityTimeout`, which is how a 25-minute redelivery budget sat behind a
// 72-hour approval gate unremarked. The arithmetic itself is pinned across the
// language boundary by
// tests/contracts/test_pipeline_trigger_redelivery_budget_cross_language.py;
// this block pins the WIRING, which that test cannot see.
describe('p54 R10 — QueueLossRecorder wiring', () => {
  function mappings(): any[] {
    return Object.values(
      synthOrchestrationStack().findResources('AWS::Lambda::EventSourceMapping'),
    ) as any[];
  }

  function dlqMapping(): any {
    const entries = mappings().filter((entry) => {
      const getAtt = entry.Properties?.EventSourceArn?.['Fn::GetAtt'];
      return Array.isArray(getAtt) && String(getAtt[0]).startsWith('PipelineTriggerDLQ');
    });
    expect(entries, 'one event source mapping should name the DLQ').toHaveLength(1);
    return entries[0];
  }

  it('the DLQ mapping is ENABLED — a ratified trade that costs four dead-lettered files', () => {
    // AMENDED, not deleted, and the reversal is recorded here rather than in a
    // rename that hides it — `LGR-60c4985-6`'s shape, under `LGR-5906ec6-2`'s
    // rule that a deliberate posture in this repo lives in an assertion, so
    // changing the posture means changing a test on purpose. This assertion read
    // `false` when task 7.7 shipped, with a note asking whoever flipped the flag
    // to update it in the same change. This is that change.
    //
    // Still not a placeholder and still not an oversight: deliberate in this
    // state exactly as it was deliberate in the other. Consuming an SQS queue
    // DELETES its messages, and this DLQ holds four real dead-lettered uploads in
    // the validation account that were simultaneously p54 task 7.1's evidence and four files
    // an operator could still have redriven — with 8 h 20 min of budget instead
    // of the 25 min they originally exhausted, so a redrive might have succeeded.
    // The operator accepted losing all four in exchange for R10.1 being live
    // rather than satisfied-in-potential behind a flag. The next Orchestration
    // deploy consumes them; nothing drains them by hand, which p54 requirements
    // still put out of scope.
    //
    // The guarantee this test carries is unchanged in either direction: if the
    // mapping is ever turned back off, amend this line in the same commit and
    // record why, the way the flip on is recorded here.
    expect(dlqMapping().Properties.Enabled).toBe(true);
  });

  it('depends on the recorder role AND its DefaultPolicy, so the first deploy cannot race', () => {
    // `CreateEventSourceMapping` for an SQS source VALIDATES the execution role's
    // `sqs:ReceiveMessage` at create time. Without an explicit ordering
    // constraint the FIRST deploy of this stack can fail with
    //
    //   InvalidParameterValueException: The provided execution role does not
    //   have permissions to call ReceiveMessage on SQS
    //
    // and roll back, then succeed on retry — which reads as flakiness rather
    // than as a missing dependency, and is why this is pinned rather than left
    // to a comment.
    //
    // The implicit dependencies stop one resource short. `functionArn` renders as
    // an `Fn::GetAtt`, so the mapping depends on the FUNCTION; the function's
    // `Role` is a `GetAtt`, so it depends on the ROLE. But the two
    // `addToPolicy` calls — `sqs:ReceiveMessage` among them — are emitted as a
    // SEPARATE `AWS::IAM::Policy` (the role's DefaultPolicy) that nothing in that
    // chain references.
    //
    // PROVED TO FAIL, NOT ASSUMED TO. Removing the `addDependency` line and
    // re-running this test renders the mapping with no `DependsOn` at all, and
    // this assertion fails naming the missing policy.
    //
    // Both names are required. The role alone is not enough: the role can exist
    // with no policy attached, which is exactly the state that fails validation.
    const dependsOn = dlqMapping().DependsOn ?? [];
    const names = (Array.isArray(dependsOn) ? dependsOn : [dependsOn]).map(String);

    expect(
      names.some((n) => n.startsWith('QueueLossRecorderRoleDefaultPolicy')),
      `the DLQ mapping does not depend on QueueLossRecorderRole's DefaultPolicy (DependsOn: ` +
        `${JSON.stringify(dependsOn)}). That policy carries sqs:ReceiveMessage on the DLQ, and ` +
        'CreateEventSourceMapping checks it at create time — without the dependency the first ' +
        'deploy of this stack can roll back with InvalidParameterValueException and then succeed ' +
        'on retry. Restore `node.addDependency(queueLossRecorderRole)` on the mapping; CDK ' +
        "expands a construct dependency to every CfnResource in that construct's subtree, which " +
        'is why depending on the role covers its DefaultPolicy too.',
    ).toBe(true);

    expect(
      names.some((n) => n.startsWith('QueueLossRecorderRole') && !n.includes('DefaultPolicy')),
      `the DLQ mapping does not depend on QueueLossRecorderRole itself (DependsOn: ` +
        `${JSON.stringify(dependsOn)})`,
    ).toBe(true);
  });

  it('TriggerSqsMapping needs no such dependency — and the reason is topology, not design', () => {
    // Asserted rather than trusted, because the difference between the two
    // mappings is the whole reason this race appeared at p54 and not earlier.
    //
    // `TriggerSqsMapping`'s role is `foundation.pipelineTriggerRole`, created in
    // the FOUNDATION stack, which deploys before Orchestration (`LGR-b46a5bb-3`:
    // "Foundation owns every Lambda role, so the grants deploy with it") and
    // carries the wildcard SQS permissions its own comment cites. So its policy
    // has existed for a whole stack by the time this mapping is created. p54's
    // recorder is the first event source mapping in this repo whose role is
    // created in the SAME stack as the mapping.
    //
    // This test pins the PREMISE, not the absence of a DependsOn: that the
    // trigger mapping's role is an imported/cross-stack value rather than a role
    // constructed here. If that ever stops being true the premise fails loudly
    // and the mapping needs the same dependency the recorder's got.
    const triggerMappings = mappings().filter((entry) => {
      const getAtt = entry.Properties?.EventSourceArn?.['Fn::GetAtt'];
      return Array.isArray(getAtt) && String(getAtt[0]).startsWith('PipelineTriggerQueue');
    });
    expect(triggerMappings, 'one event source mapping should name the trigger queue').toHaveLength(1);

    const roles = Object.values(
      synthOrchestrationStack().findResources('AWS::IAM::Role'),
    ) as any[];
    const localRoleNames = roles.map((r) => JSON.stringify(r.Properties?.RoleName ?? ''));
    expect(
      localRoleNames.some((n) => n.includes('pipeline-trigger') || n.includes('PipelineTrigger')),
      'a role that looks like the pipeline-trigger role is now constructed inside the ' +
        'Orchestration stack. TriggerSqsMapping was only ever safe because that role lives in ' +
        'Foundation and is already deployed — if it has moved here, that mapping needs the same ' +
        'explicit dependency on its DefaultPolicy that QueueLossRecorderDlqMapping carries, or ' +
        'the first deploy can fail on ReceiveMessage validation.',
    ).toBe(false);
  });

  it('declares ReportBatchItemFailures, so a failed write returns the message', () => {
    // Same both-halves rule as TriggerSqsMapping: without this the handler's
    // `batchItemFailures` return value is ignored. It matters more here than
    // there — an unreported DynamoDB failure means the message is deleted and
    // the Lost_File record is never written, which is silent loss reintroduced
    // by the mechanism built to remove it.
    expect(dlqMapping().Properties.FunctionResponseTypes).toEqual([
      'ReportBatchItemFailures',
    ]);
  });

  it('the recorder can only PutItem on the executions table — never update or delete', () => {
    // Least privilege, and load-bearing rather than hygiene: the handler writes a
    // WHOLE row because the Queued_Placeholder has usually expired by DLQ time
    // (1 h TTL against an 8 h 20 min budget), and an `update_item` against a
    // missing item would create `{pipeline_id, run_id, queue_outcome}` with no
    // `started_at` — a row RunsByTimeIndex cannot index (LGR-7d19eca-2) and so an
    // invisible record. Withholding UpdateItem makes that mistake unmakeable.
    const policies = Object.values(
      synthOrchestrationStack().findResources('AWS::IAM::Policy'),
    ) as any[];
    const recorderStatements = policies
      .filter((p) => JSON.stringify(p.Properties?.Roles ?? '').includes('QueueLossRecorderRole'))
      .flatMap((p) => p.Properties?.PolicyDocument?.Statement ?? []);
    expect(recorderStatements.length, 'the recorder role has no inline policy').toBeGreaterThan(0);

    const dynamoActions = recorderStatements
      .flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]))
      .filter((a: string) => typeof a === 'string' && a.startsWith('dynamodb:'));
    expect(dynamoActions).toEqual(['dynamodb:PutItem']);
  });

  it('the trigger queue keeps a 30 s visibility timeout and a 1000 receive budget', () => {
    // The two numbers whose PRODUCT is the redelivery budget. Asserted here as
    // well as in the contract test because this reads the synthesized template
    // (what deploys) while the contract test reads the source literal (what a
    // reader sees) — and the contract test's job is the cross-language
    // inequality, which it carries as an xfail. This one must stay green.
    //
    // `visibilityTimeout` is 30 s deliberately and is NOT the lever for more
    // budget: raising it idles every deferral for up to its whole value even
    // when the lock clears in seconds, and it is also constrained by the
    // trigger Lambda's timeout ratio. See the comment on the queue.
    synthOrchestrationStack().hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'agentic-data-ingestion-pipeline-triggers-dev',
      VisibilityTimeout: 30,
      RedrivePolicy: {
        maxReceiveCount: 1000,
      },
    });
  });
});

// ── Optional alarm email (notificationEmail) ──────────────────────
//
// `notificationEmail` is optional by decision: unset or blank creates NO email
// subscription, rather than requiring an address or defaulting to a placeholder.
// A placeholder was the original defect — it subscribed an address at a real
// domain nobody here owned to both topics on every deploy, sending a
// confirmation request to a stranger's inbox, and the subscription could never
// be confirmed. The topics themselves must still exist when no email is set,
// because every alarm publishes to one of them regardless.
//
// None of that had a synthesis test. The shared `synthOrchestration()` above
// caches one synth with the prop unset, so it can only ever observe one arm.
// These build an uncached stack per case.
//
// Scope, stated so it does not overclaim: this pins the STACK's behaviour given
// the prop. It does not pin that `bin/agentic_data_ingestion.ts` reads the
// `-c notificationEmail=` context into that prop, which was a separate defect
// once (the documented override was silently ignored). Testing that needs the
// whole app synthesized, which bundles assets with Docker.

function synthOrchestrationWithEmail(notificationEmail: string | undefined): Template {
  const ctx = createTestStacks();
  const mockData = Object.assign(ctx.dataStack, {
    stateTable: ctx.stateTable,
    pipelinesTable: ctx.pipelinesTable,
    approvalsTable: ctx.approvalsTable,
    pipelineLocksTable: ctx.pipelineLocksTable,
    executionsTable: ctx.executionsTable,
    processedFilesTable: ctx.processedFilesTable,
    eventBus: ctx.eventBus,
  });
  const mockFoundation = Object.assign(ctx.foundationStack, {
    sourceBucket: ctx.sourceBucket,
    pipelineTriggerRole: ctx.pipelineTriggerRole,
  });
  const stack = new OrchestrationStack(ctx.app, 'TestOrchestrationEmail', {
    env: ctx.env,
    environment: 'dev',
    compute: ctx.compute as any,
    data: mockData as any,
    foundation: mockFoundation as any,
    notificationEmail,
  });
  return Template.fromStack(stack);
}

const ALERT_TOPIC_NAMES = [
  'agentic-data-ingestion-critical-alerts-dev',
  'agentic-data-ingestion-approval-notifications-dev',
];

/** Logical ids of the two alert topics, resolved by their topicName. */
function alertTopicIds(template: Template): string[] {
  const topics = template.findResources('AWS::SNS::Topic');
  return Object.entries(topics)
    .filter(([, r]: [string, any]) => ALERT_TOPIC_NAMES.includes(r.Properties?.TopicName))
    .map(([id]) => id);
}

describe('OrchestrationStack — notificationEmail is optional', () => {
  it('creates no email subscription when the prop is unset', () => {
    const template = synthOrchestrationWithEmail(undefined);
    template.resourceCountIs('AWS::SNS::Subscription', 0);
  });

  it.each([
    ['an empty string', ''],
    ['whitespace only', '   '],
  ])('treats %s as unset, because the value is trimmed', (_label, value) => {
    // A blank `-c notificationEmail=` must not produce a subscription with an
    // empty endpoint, which SNS rejects at deploy time.
    const template = synthOrchestrationWithEmail(value);
    template.resourceCountIs('AWS::SNS::Subscription', 0);
  });

  it('still creates both alert topics when no email is set', () => {
    // The alarms publish to these whether or not anyone is subscribed.
    // Dropping the topics along with the subscription would break every
    // alarm action rather than merely silencing delivery.
    const template = synthOrchestrationWithEmail(undefined);
    expect(alertTopicIds(template)).toHaveLength(2);
  });

  it('subscribes the address to both alert topics when it is set', () => {
    const template = synthOrchestrationWithEmail('ops@example.com');
    const subs = Object.values(template.findResources('AWS::SNS::Subscription'));
    expect(subs).toHaveLength(2);

    const topicIds = alertTopicIds(template);
    const subscribedTopics = subs.map((s: any) => s.Properties?.TopicArn?.Ref).sort();
    expect(subscribedTopics).toEqual([...topicIds].sort());
    for (const s of subs as any[]) {
      expect(s.Properties.Protocol).toBe('email');
      expect(s.Properties.Endpoint).toBe('ops@example.com');
    }
  });

  it('trims surrounding whitespace from a set address', () => {
    const template = synthOrchestrationWithEmail('  ops@example.com  ');
    const subs = Object.values(template.findResources('AWS::SNS::Subscription')) as any[];
    expect(subs).toHaveLength(2);
    for (const s of subs) expect(s.Properties.Endpoint).toBe('ops@example.com');
  });
});

// ── job_completion failures reach the Glue job monitor DLQ ───────────
//
// EventBridge invokes job_completion asynchronously, so two different
// failures can lose a completion event: EventBridge failing to hand it to
// Lambda (the rule target's retry policy and DLQ), and the function failing
// once Lambda runs it (Lambda's async retry, then the function's on-failure
// destination). Only the first was configured, while comments claimed it also
// covered the second; a failed invocation was retried twice and discarded,
// and the DLQ depth alarm could not fire for it. These tests pin both stages
// to the same queue, and the Foundation grant that lets Lambda send to it.
describe('OrchestrationStack — job_completion failures reach the Glue job monitor DLQ', () => {
  const DLQ_NAME = 'agentic-data-ingestion-glue-job-monitor-dlq-dev';

  function dlqLogicalId(): string {
    const resources = synthOrchestration().template.toJSON().Resources ?? {};
    const ids = Object.keys(resources).filter(
      (id) => resources[id].Type === 'AWS::SQS::Queue' && resources[id].Properties?.QueueName === DLQ_NAME,
    );
    expect(ids, `exactly one queue named ${DLQ_NAME}`).toHaveLength(1);
    return ids[0];
  }

  it('sends a failed asynchronous invocation to the DLQ after two retries', () => {
    const { template, stack, compute } = synthOrchestration();
    const configs = Object.values(template.findResources('AWS::Lambda::EventInvokeConfig')) as any[];
    const forJobCompletion = configs.filter(
      (c) => JSON.stringify(c.Properties.FunctionName) === JSON.stringify(stack.resolve(compute.jobCompletionLambda.functionName)),
    );
    expect(forJobCompletion, 'job_completion needs exactly one async invoke config').toHaveLength(1);
    const [config] = forJobCompletion;

    expect(config.Properties.Qualifier).toBe('$LATEST');
    expect(config.Properties.MaximumRetryAttempts).toBe(2);
    expect(config.Properties.MaximumEventAgeInSeconds).toBe(6 * 60 * 60);
    expect(config.Properties.DestinationConfig).toEqual({
      OnFailure: { Destination: { 'Fn::GetAtt': [dlqLogicalId(), 'Arn'] } },
    });
  });

  it('keeps the delivery-failure DLQ on the EventBridge target, pointing at the same queue', () => {
    const rules = Object.values(synthOrchestration().template.findResources('AWS::Events::Rule')) as any[];
    const monitor = rules.find((r) => r.Properties?.Name === 'agentic-data-ingestion-glue-job-monitor-dev');
    expect(monitor, 'GlueJobMonitorRule must exist').toBeDefined();
    const [target] = monitor.Properties.Targets;
    expect(target.DeadLetterConfig).toEqual({ Arn: { 'Fn::GetAtt': [dlqLogicalId(), 'Arn'] } });
    expect(target.RetryPolicy).toEqual({ MaximumRetryAttempts: 8, MaximumEventAgeInSeconds: 3600 });
  });

  it('lets the job-completion role send to that queue, by the name Orchestration gives it', () => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const foundation = new FoundationStack(app, 'AgenticDataIngestion-Foundation-dev', {
      env: { account: '123456789012', region: 'us-west-2' },
      projectName: 'agentic-data-ingestion',
      environment: 'dev',
    });
    const resources = Template.fromStack(foundation).toJSON().Resources ?? {};
    const roleId = Object.keys(resources).find(
      (id) =>
        resources[id].Type === 'AWS::IAM::Role' &&
        resources[id].Properties?.RoleName === 'agentic-data-ingestion-dev-jobcompletion-role',
    );
    expect(roleId, 'job-completion role must exist').toBeDefined();
    const statements = (Object.values(resources) as any[])
      .filter((r) => r.Type === 'AWS::IAM::Policy' && (r.Properties?.Roles ?? []).some((ref: any) => ref?.Ref === roleId))
      .flatMap((r) => r.Properties.PolicyDocument.Statement);
    const sends = statements.filter((s: any) => [s.Action].flat().includes('sqs:SendMessage'));

    expect(sends, 'one SendMessage grant, for the destination').toHaveLength(1);
    expect(sends[0].Effect).toBe('Allow');
    // Same account and region as the Orchestration harness, so the name is the
    // whole agreement between the two stacks.
    expect(sends[0].Resource).toBe(`arn:aws:sqs:us-west-2:123456789012:${DLQ_NAME}`);
    expect(dlqLogicalId()).toBeTypeOf('string');
  });
});

// ── The Glue job state updater reads the job's own pipeline id ───────
//
// GlueJobStateUpdateFunction resolves a job to its pipeline through the job's
// `--pipeline-id` default argument (glue:GetJob), because a versioned job name
// (`...-job-<id>-v<N>`) does not strip to the pipeline id. Without the grant
// every lookup falls back to the name. The handler's behaviour is tested in
// tests/cdk/stacks/test_glue_job_state_updater_inline.py.
describe('OrchestrationStack — Glue job state updater can read job definitions', () => {
  it('grants the updater role glue:GetJob on both project job-name forms', () => {
    const resources = synthOrchestration().template.toJSON().Resources ?? {};
    const roleId = Object.keys(resources).find(
      (id) =>
        resources[id].Type === 'AWS::IAM::Role' &&
        resources[id].Properties?.RoleName === 'agentic-data-ingestion-glue-job-state-updater-role-dev',
    );
    expect(roleId, 'the updater role must exist').toBeDefined();

    const grantsFor = (id: string) =>
      (Object.values(resources) as any[])
        .filter((r) => r.Type === 'AWS::IAM::Policy' && (r.Properties?.Roles ?? []).some((ref: any) => ref?.Ref === id))
        .flatMap((r) => r.Properties.PolicyDocument.Statement)
        .filter((s: any) => [s.Action].flat().includes('glue:GetJob'));

    const grants = grantsFor(roleId!);
    expect(grants).toHaveLength(1);
    const arns = JSON.stringify(grants[0].Resource);
    expect(arns).toContain(':job/agentic-data-ingestion-*');
    expect(arns).toContain(':job/agentic_data_ingestion*');
  });
});

// ── The pipeline error-rate alarm reads a series that is written ─────
//
// One datapoint per finished run on AgenticDataIngestion/Pipeline /
// Pipeline.ErrorRate with dimension {Environment}: 100 from record_failure for
// a failure, 0 from job_completion for a success. The alarm averages it, so it
// must name exactly that dimension set: a CloudWatch metric is identified by
// its dimensions, and the dimensionless alarm this replaced read a series no
// producer wrote. The producers' side of the contract is
// tests/contracts/test_pipeline_error_rate_cross_language.py.
describe('OrchestrationStack — pipeline error-rate alarm', () => {
  const resources = () => synthOrchestration().template.toJSON().Resources ?? {};

  it('averages the {Environment} series over 15 minutes and fires above 20%', () => {
    const alarms = Object.values(resources()).filter(
      (r: any) => r.Type === 'AWS::CloudWatch::Alarm' && r.Properties?.AlarmName === 'AgenticDataIngestion-Pipeline-Error-Rate-dev',
    ) as any[];
    expect(alarms).toHaveLength(1);
    const p = alarms[0].Properties;
    expect(p.Namespace).toBe('AgenticDataIngestion/Pipeline');
    expect(p.MetricName).toBe('Pipeline.ErrorRate');
    expect(p.Dimensions).toEqual([{ Name: 'Environment', Value: 'dev' }]);
    expect(p.Statistic).toBe('Average');
    expect(p.Period).toBe(900);
    expect(p.EvaluationPeriods).toBe(1);
    expect(p.Threshold).toBe(20);
    expect(p.ComparisonOperator).toBe('GreaterThanThreshold');
    expect(p.TreatMissingData).toBe('notBreaching');
  });

  it('lets record_failure publish into that namespace only, with the environment set', () => {
    const all = resources();
    const fnEntry = Object.entries(all).find(
      ([, r]: [string, any]) =>
        r.Type === 'AWS::Lambda::Function' && r.Properties?.FunctionName === 'agentic-data-ingestion-record-failure-dev',
    ) as [string, any] | undefined;
    expect(fnEntry, 'the record-failure function must exist').toBeDefined();
    const fn = fnEntry![1];
    expect(fn.Properties.Environment.Variables.ENVIRONMENT).toBe('dev');

    const roleId = fn.Properties.Role['Fn::GetAtt'][0];
    const statements = (Object.values(all) as any[])
      .filter((r) => r.Type === 'AWS::IAM::Policy' && (r.Properties?.Roles ?? []).some((ref: any) => ref?.Ref === roleId))
      .flatMap((r) => r.Properties.PolicyDocument.Statement);
    const puts = statements.filter((s: any) => [s.Action].flat().includes('cloudwatch:PutMetricData'));
    expect(puts).toHaveLength(1);
    expect(puts[0].Condition).toEqual({ StringEquals: { 'cloudwatch:namespace': 'AgenticDataIngestion/Pipeline' } });
  });
});
