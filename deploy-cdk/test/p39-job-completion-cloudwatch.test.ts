// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion test for the job-completion role's CloudWatch grant (p39 R2.1).
 *
 * job_completion publishes one `GlueJob.RunCostEstimate` datapoint per terminal
 * Glue run, into namespace AgenticDataIngestion/Pipeline, and that metric is
 * the only source of the dashboard's Est. Monthly Cost tile and its trend.
 *
 * The role shipped without `cloudwatch:PutMetricData`. Every publish failed
 * with AccessDenied inside `metrics_facade.flush_direct`, which logs and
 * swallows by design, so the tile read Not established and nothing failed.
 * The unit tests could not see it: they stub either the publisher
 * (`test_run_cost_publication.py`) or the CloudWatch client
 * (`test_metrics_facade_run_cost.py`). This is the layer that can.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';
const PIPELINE_NAMESPACE = 'AgenticDataIngestion/Pipeline';

describe('FoundationStack — job-completion role CloudWatch grant (p39 R2.1)', () => {
  let json: Record<string, any>;

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    json = Template.fromStack(stack).toJSON();
  });

  /** Every policy statement attached (inline or via AWS::IAM::Policy) to the job-completion role. */
  function jobCompletionRoleStatements(): any[] {
    const resources = json.Resources ?? {};
    const roleKey = Object.keys(resources).find(
      (k) =>
        resources[k].Type === 'AWS::IAM::Role' &&
        resources[k].Properties?.RoleName === `agentic-data-ingestion-${TEST_ENV}-jobcompletion-role`,
    );
    expect(roleKey, 'job-completion role must exist').toBeDefined();

    const statements: any[] = [];
    for (const inline of resources[roleKey!].Properties?.Policies ?? []) {
      statements.push(...(inline.PolicyDocument?.Statement ?? []));
    }
    for (const resource of Object.values(resources) as any[]) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      const attached = (resource.Properties?.Roles ?? []).some((r: any) => r?.Ref === roleKey);
      if (attached) statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
    }
    return statements;
  }

  function asArray(value: any): any[] {
    return Array.isArray(value) ? value : [value];
  }

  it('finds the role through its existing grants (control)', () => {
    // If this fails, the lookup is broken and the assertions below are vacuous.
    const actions = jobCompletionRoleStatements().flatMap((s: any) => asArray(s.Action));
    expect(actions).toContain('glue:GetJobRun');
    expect(actions).toContain('states:SendTaskSuccess');
  });

  it('allows PutMetricData, bounded to the AgenticDataIngestion/Pipeline namespace', () => {
    const grants = jobCompletionRoleStatements().filter((s: any) =>
      asArray(s.Action).includes('cloudwatch:PutMetricData'),
    );
    expect(grants, 'the run-cost publish needs exactly one PutMetricData grant').toHaveLength(1);
    const [grant] = grants;
    expect(grant.Effect).toBe('Allow');
    // PutMetricData defines no resource types: Resource '*' bounded by the
    // namespace condition is the only supported scoping.
    expect(asArray(grant.Resource)).toEqual(['*']);
    expect(grant.Condition).toEqual({ StringEquals: { 'cloudwatch:namespace': PIPELINE_NAMESPACE } });
    // Nothing else rides on the condition-bounded statement.
    expect(asArray(grant.Action)).toEqual(['cloudwatch:PutMetricData']);
  });
});
