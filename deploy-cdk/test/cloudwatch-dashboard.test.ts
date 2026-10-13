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
import { OrchestrationStack } from '../lib/orchestration-stack';

/**
 * Creates a minimal mock of the dependent stacks so we can synth
 * the OrchestrationStack without Docker bundling.
 */
function createTestStacks() {
  // Suppress Docker bundling: OrchestrationStack defines a Lambda layer via
  // `Code.fromAsset(..., { bundling: { image: DockerImage.fromRegistry(...) } })`,
  // which would run `docker run` at synth and hang in CI / without a Docker
  // daemon. Setting `aws:cdk:bundling-stacks` to an empty list makes CDK emit a
  // stub asset instead (same pattern as test-fixtures-stack.test.ts).
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: '123456789012', region: 'us-west-2' };

  const foundationStack = new cdk.Stack(app, 'MockFoundation', { env });
  const sourceBucket = new s3.Bucket(foundationStack, 'SourceBucket', {
    bucketName: 'adi-source-files-123456789012-us-west-2',
  });
  const pipelineTriggerRole = new iam.Role(foundationStack, 'PipelineTriggerRole', {
    assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
  });

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

function synthTemplate(): Template {
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

  const orchestrationStack = new OrchestrationStack(ctx.app, 'TestOrchestration', {
    env: ctx.env,
    environment: 'dev',
    compute: ctx.compute as any,
    data: mockData as any,
    foundation: mockFoundation as any,
  });
  return Template.fromStack(orchestrationStack);
}


function getDashboardBody(template: Template): any {
  const resources = template.toJSON().Resources;
  const dashKey = Object.keys(resources).find(
    (k) => resources[k].Type === 'AWS::CloudWatch::Dashboard',
  );
  expect(dashKey).toBeDefined();

  const bodyStr = resources[dashKey!].Properties.DashboardBody;
  // DashboardBody is typically a Fn::Join intrinsic
  if (typeof bodyStr === 'string') {
    return JSON.parse(bodyStr);
  }
  if (bodyStr?.['Fn::Join']) {
    const sep = bodyStr['Fn::Join'][0];
    const parts = bodyStr['Fn::Join'][1];
    // Replace non-string tokens (Ref, Fn::GetAtt, etc.) with a safe placeholder
    const joined = parts
      .map((part: any) => {
        if (typeof part === 'string') return part;
        // For Ref or GetAtt tokens that appear inside JSON string values,
        // just emit a safe placeholder string (no extra quotes — the
        // surrounding JSON already has them).
        return 'PLACEHOLDER';
      })
      .join(sep);
    return JSON.parse(joined);
  }
  throw new Error('Unexpected DashboardBody format');
}

describe('CloudWatch Dashboard — Widget Validation', () => {
  let template: Template;
  let dashboardBody: any;

  function getTemplate() {
    if (!template) template = synthTemplate();
    return template;
  }

  function getDashboard() {
    if (!dashboardBody) dashboardBody = getDashboardBody(getTemplate());
    return dashboardBody;
  }

  it('synthesizes exactly one CloudWatch Dashboard resource', () => {
    getTemplate().resourceCountIs('AWS::CloudWatch::Dashboard', 1);
  });

  it('dashboard name includes environment suffix', () => {
    const resources = getTemplate().toJSON().Resources;
    const dashKey = Object.keys(resources).find(
      (k) => resources[k].Type === 'AWS::CloudWatch::Dashboard',
    );
    const name = resources[dashKey!].Properties.DashboardName;
    expect(name).toBe('AgenticDataIngestion-Pipeline-Monitoring-dev');
  });

  it('dashboard has at least 4 rows of widgets', () => {
    const body = getDashboard();
    expect(body.widgets.length).toBeGreaterThanOrEqual(8);
  });

  it('contains Approval Operations widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Approval Operations');
  });

  it('contains Pipeline Stage Executions widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Pipeline Stage Executions');
  });

  it('contains Glue Job Operations widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Glue Job Operations');
  });

  it('contains Cost Tracking widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Cost Tracking');
  });

  it('contains Lambda Invocations widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Lambda Invocations');
  });

  it('contains Lambda Errors widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Lambda Errors');
  });

  it('contains Step Functions Executions widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Step Functions Executions');
  });

  it('contains Step Functions Duration widget', () => {
    const body = getDashboard();
    const titles = body.widgets.map((w: any) => w.properties?.title);
    expect(titles).toContain('Step Functions Duration');
  });

  it('Lambda Invocations widget references AWS/Lambda namespace', () => {
    const body = getDashboard();
    const widget = body.widgets.find((w: any) => w.properties?.title === 'Lambda Invocations');
    expect(widget).toBeDefined();
    const metrics = widget.properties.metrics;
    expect(metrics.length).toBeGreaterThanOrEqual(4);
    // Each metric array should reference AWS/Lambda namespace
    for (const m of metrics) {
      expect(m).toContain('AWS/Lambda');
    }
  });

  it('Step Functions Executions widget references AWS/States namespace', () => {
    const body = getDashboard();
    const widget = body.widgets.find((w: any) => w.properties?.title === 'Step Functions Executions');
    expect(widget).toBeDefined();
    const metrics = widget.properties.metrics;
    expect(metrics.length).toBeGreaterThanOrEqual(3);
    for (const m of metrics) {
      expect(m).toContain('AWS/States');
    }
  });
});
