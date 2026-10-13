// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';
import { OrchestrationStack } from '../lib/orchestration-stack';
import { HostingStack } from '../lib/hosting-stack';

/**
 * Threat model T-09, R-06: every Lambda log group, and the REST API's
 * execution log group, has a retention period.
 *
 * Covered: every AWS::Lambda::Function built from lambda.Function, which is
 * every project function plus CDK's BucketDeployment handlers. Not covered,
 * and not claimed: CDK's own raw-CloudFormation provider functions (the
 * LogRetention provider itself, S3 auto-delete), AgentCore runtime logs, and
 * Glue job logs.
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';
const ONE_MONTH_DAYS = 30;

let templates: Record<string, Record<string, any>>;

beforeAll(() => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };
  const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env, projectName: 'agentic-data-ingestion', environment: TEST_ENV,
  });
  const data = new DataStack(app, `AgenticDataIngestion-Data-${TEST_ENV}`, { env, environment: TEST_ENV });
  const auth = new AuthStack(app, `AgenticDataIngestion-Auth-${TEST_ENV}`, { env, environment: TEST_ENV });
  const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env, environment: TEST_ENV, projectName: 'agentic-data-ingestion', foundation, data,
    userPoolArn: auth.userPool.userPoolArn,
  });
  const orchestration = new OrchestrationStack(app, `AgenticDataIngestion-Orchestration-${TEST_ENV}`, {
    env, environment: TEST_ENV, compute, data, foundation,
  });
  const hosting = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
    env, environment: TEST_ENV,
    userPoolId: 'us-west-2_TestPool',
    userPoolArn: `arn:aws:cognito-idp:${TEST_REGION}:${TEST_ACCOUNT}:userpool/us-west-2_TestPool`,
  });
  templates = {
    compute: Template.fromStack(compute).toJSON(),
    orchestration: Template.fromStack(orchestration).toJSON(),
    hosting: Template.fromStack(hosting).toJSON(),
  };
}, 300_000);

const entries = (json: Record<string, any>) => Object.entries(json.Resources ?? {}) as [string, any][];
const retentions = (json: Record<string, any>) => entries(json).filter(([, r]) => r.Type === 'Custom::LogRetention').map(([, r]) => r.Properties);

/** Lambda functions built from lambda.Function: they have a Code property and are not CDK singleton providers. */
function projectFunctions(json: Record<string, any>): string[] {
  return entries(json)
    .filter(([id, r]) => r.Type === 'AWS::Lambda::Function' && !id.startsWith('LogRetention') && !id.startsWith('CustomS3AutoDeleteObjects'))
    .map(([id]) => id);
}

describe('R-06 — Lambda log groups have a retention period', () => {
  it.each(['compute', 'orchestration', 'hosting'])('every function in %s has a ONE_MONTH LogRetention on /aws/lambda/<name>', (stack) => {
    const json = templates[stack];
    const functions = projectFunctions(json);
    expect(functions.length, `${stack} has functions`).toBeGreaterThan(0);
    const covered = retentions(json).map((p) => JSON.stringify(p.LogGroupName));
    for (const fn of functions) {
      const hit = retentions(json).find((p) => JSON.stringify(p.LogGroupName).includes(`"Ref":"${fn}"`));
      expect(hit, `${stack}: no LogRetention for ${fn}; covered: ${covered.length}`).toBeDefined();
      expect(JSON.stringify(hit!.LogGroupName)).toContain('/aws/lambda/');
      expect(hit!.RetentionInDays).toBe(ONE_MONTH_DAYS);
    }
  });

  it('the project function count in Compute and Orchestration matches the documented inventory floor', () => {
    // 22 Compute and 7 Orchestration functions when this was written; a floor,
    // so adding a function never fails here, only losing coverage does.
    expect(projectFunctions(templates.compute).length).toBeGreaterThanOrEqual(22);
    expect(projectFunctions(templates.orchestration).length).toBeGreaterThanOrEqual(7);
  });
});

describe('R-06 / T-09 — the REST API execution log', () => {
  it('has a ONE_MONTH retention on API-Gateway-Execution-Logs_<api-id>/v1', () => {
    const hit = retentions(templates.compute).find((p) => JSON.stringify(p.LogGroupName).includes('API-Gateway-Execution-Logs_'));
    expect(hit).toBeDefined();
    // `API-Gateway-Execution-Logs_${restApiId}/${stageName}`: the stage name is a
    // Ref to the v1 deployment stage, which resolves to "v1" at deploy time.
    expect(JSON.stringify(hit!.LogGroupName)).toMatch(/"\/",\{"Ref":"DashboardRestApiDeploymentStagev1[0-9A-F]+"\}/);
    expect(hit!.RetentionInDays).toBe(ONE_MONTH_DAYS);
  });

  it('does not log request and response bodies (dataTraceEnabled false), but keeps INFO logging', () => {
    const stage = entries(templates.compute).find(([, r]) => r.Type === 'AWS::ApiGateway::Stage')![1];
    const settings = stage.Properties.MethodSettings;
    expect(settings.length).toBeGreaterThan(0);
    for (const s of settings) {
      expect(s.DataTraceEnabled).toBe(false);
      expect(s.LoggingLevel).toBe('INFO');
    }
  });
});

describe('T-22 residual — the Glue-callback token table is wired to both Lambdas', () => {
  it.each(['agentic_data_ingestion_run_pipeline', 'agentic_data_ingestion_job_completion'])('%s has TASK_TOKENS_TABLE', (name) => {
    const fn = entries(templates.compute).find(([, r]) => r.Type === 'AWS::Lambda::Function' && r.Properties.FunctionName === name)![1];
    expect(JSON.stringify(fn.Properties.Environment.Variables.TASK_TOKENS_TABLE)).toContain('TaskTokensTable');
  });
});
