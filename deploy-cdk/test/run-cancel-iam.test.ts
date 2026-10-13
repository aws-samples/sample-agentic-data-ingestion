// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * The grants the dashboard's run cancel needs, on the two roles that act on it.
 *
 * - The dashboard API (infrastructure role) describes and stops a run's
 *   execution, and stops its Glue job when the job is still running.
 * - job_completion stops the execution once Glue reports a cancelled job
 *   stopped, which is what releases the pipeline lock.
 *
 * Step Functions evaluates DescribeExecution and StopExecution against the
 * EXECUTION ARN. The dashboard role held both only on the stateMachine ARN,
 * which does not match an execution, so the old cancel's stop was denied
 * (inferred from the policy text; it was also swallowed). The Glue grant carries
 * both job/ name forms (NAMING FENCE, p31 C3).
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const EXECUTION_ARN = `arn:aws:states:${TEST_REGION}:${TEST_ACCOUNT}:execution:agentic-data-ingestion-*:*`;
const KEBAB_JOB = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:job/agentic-data-ingestion-*`;
const SNAKE_JOB = `arn:aws:glue:${TEST_REGION}:${TEST_ACCOUNT}:job/agentic_data_ingestion*`;

type Statement = { Action: string | string[]; Resource: string | string[]; Effect: string };

const asList = (v: string | string[]): string[] => (Array.isArray(v) ? v : [v]);

function statementsForRole(json: Record<string, any>, roleName: string): Statement[] {
  const resources = Object.entries(json.Resources ?? {}) as [string, any][];
  const role = resources.find(([, r]) => r.Type === 'AWS::IAM::Role' && r.Properties?.RoleName === roleName);
  expect(role, `role ${roleName} not found`).toBeDefined();
  const roleKey = role![0];
  const statements: Statement[] = [];
  for (const [, r] of resources) {
    if (r.Type !== 'AWS::IAM::Policy') continue;
    const roles = (r.Properties?.Roles ?? []) as any[];
    if (roles.some((ref) => ref?.Ref === roleKey)) {
      statements.push(...(r.Properties.PolicyDocument.Statement as Statement[]));
    }
  }
  expect(statements.length, `no inline statements found for ${roleName}`).toBeGreaterThan(0);
  return statements;
}

function grants(statements: Statement[], action: string): string[] {
  return statements
    .filter((s) => s.Effect === 'Allow' && asList(s.Action).includes(action))
    .flatMap((s) => asList(s.Resource));
}

describe('run cancel IAM', () => {
  let json: Record<string, any>;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, 'AgenticDataIngestion-Foundation-dev-RunCancel', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: 'dev',
    });
    json = Template.fromStack(stack).toJSON();
  });

  it('the dashboard API can describe and stop a pipeline execution by its execution ARN', () => {
    const statements = statementsForRole(json, 'agentic-data-ingestion-dev-infrastructure-role');
    expect(grants(statements, 'states:StopExecution')).toContain(EXECUTION_ARN);
    expect(grants(statements, 'states:DescribeExecution')).toContain(EXECUTION_ARN);
  });

  it('the dashboard API can stop a pipeline Glue job run, under both job name forms and nothing wider', () => {
    const resources = grants(statementsForRole(json, 'agentic-data-ingestion-dev-infrastructure-role'), 'glue:BatchStopJobRun');
    expect(resources).toEqual(expect.arrayContaining([KEBAB_JOB, SNAKE_JOB]));
    expect(resources).not.toContain('*');
  });

  it('job_completion can describe and stop the execution a cancel left waiting', () => {
    const statements = statementsForRole(json, 'agentic-data-ingestion-dev-jobcompletion-role');
    expect(grants(statements, 'states:StopExecution')).toEqual([EXECUTION_ARN]);
    expect(grants(statements, 'states:DescribeExecution')).toEqual([EXECUTION_ARN]);
  });
});
