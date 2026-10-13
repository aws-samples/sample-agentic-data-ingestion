// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion tests for the idle-proxy-billing fix (P28 R19.11, task 15.5).
 *
 * The four container-backed steps (create_code, test_code,
 * create_glue_pipeline, create_infrastructure) run as waitForTaskToken task
 * states: the proxy Lambda dispatches the token to the AgentCore container
 * and returns within seconds, and the CONTAINER closes the loop via
 * SendTaskSuccess/SendTaskFailure. Two stack-level contracts make that work,
 * pinned here:
 *
 *   1. Foundation — each step's AgentCore runtime role must hold states:SendTask*
 *      (without it every container callback fails and every token task
 *      times out at its TimeoutSeconds backstop).
 *   2. Compute — the proxy Lambdas are dispatch-only under the token shape,
 *      so their memorySize is pinned at 512 MB (the cost intent of R19.11).
 *      Verified at the SOURCE level following the established convention:
 *      the full ComputeStack (20+ Lambdas with Code.fromAsset) exceeds
 *      vitest worker memory during synth (see hosting-auth.test.ts) — full
 *      synth validation is done via `cdk synth` in CI.
 *
 * Token wiring on the state machine itself is asserted against the
 * synthesized Orchestration template in orchestration-stack.test.ts
 * ('Container-backed waitForTaskToken states').
 *
 * Validates: Requirements 19.11
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

// SendTask* callback actions do not support resource-level permissions, so
// the statement carries Resource '*'. SendTaskHeartbeat is NOT granted: it was
// reserved for a HeartbeatSeconds that no state sets and no code calls, and
// R-02 dropped it rather than carry an unused capability onto four roles.
const SEND_TASK_ACTIONS = ['states:SendTaskFailure', 'states:SendTaskSuccess'];

/** Since R-02 each container-backed step runs as its own runtime role. */
const STEP_RUNTIME_ROLE_NAMES = ['create-code', 'test-code', 'create-pipeline', 'create-infra'].map(
  (suffix) => `agentic-data-ingestion-${TEST_ENV}-agentcore-${suffix}-role`,
);

describe('FoundationStack — container-backed step roles SendTask* (P28 R19.11)', () => {
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

  /** Every policy statement attached (inline or via AWS::IAM::Policy) to the named role. */
  function roleStatements(roleName: string): any[] {
    const resources = json.Resources ?? {};
    const roleKey = Object.keys(resources).find(
      (k) => resources[k].Type === 'AWS::IAM::Role' && resources[k].Properties?.RoleName === roleName,
    );
    expect(roleKey, `${roleName} must exist`).toBeDefined();
    const statements: any[] = [];
    for (const inline of resources[roleKey!].Properties?.Policies ?? []) {
      statements.push(...(inline.PolicyDocument?.Statement ?? []));
    }
    // addToPolicy() statements land in AWS::IAM::Policy resources whose
    // Roles list references the role's logical id.
    for (const resource of Object.values(resources) as any[]) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      if ((resource.Properties?.Roles ?? []).some((r: any) => r?.Ref === roleKey)) {
        statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
      }
    }
    return statements;
  }

  it.each(STEP_RUNTIME_ROLE_NAMES)('%s can close its waitForTaskToken loop (SendTaskSuccess/Failure)', (roleName) => {
    const sendTaskStmt = roleStatements(roleName).find((s: any) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return SEND_TASK_ACTIONS.every((a) => actions.includes(a));
    });
    expect(sendTaskStmt, 'without it every container callback fails and the task times out').toBeDefined();
    expect(sendTaskStmt.Effect).toBe('Allow');
  });

  it.each(STEP_RUNTIME_ROLE_NAMES)('%s: the SendTask* statement uses Resource * and carries nothing else', (roleName) => {
    const sendTaskStmt = roleStatements(roleName).find((s: any) => {
      const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
      return actions.includes('states:SendTaskSuccess');
    });
    expect(sendTaskStmt).toBeDefined();
    const resources = Array.isArray(sendTaskStmt.Resource) ? sendTaskStmt.Resource : [sendTaskStmt.Resource];
    expect(resources).toEqual(['*']);
    // No other states: action rides along on the wildcard resource.
    const actions = Array.isArray(sendTaskStmt.Action) ? sendTaskStmt.Action : [sendTaskStmt.Action];
    expect(actions.filter((a: string) => a.startsWith('states:')).sort()).toEqual(SEND_TASK_ACTIONS);
  });
});

// =====================================================================
// ComputeStack — proxy Lambda memory pinned at 512 MB (source-level).
//
// Under waitForTaskToken the deployed handler only dispatches to the
// container (module imports + one boto3 call), so 512 MB is the sizing
// intent; a silent bump back to the inline-workload sizes (2048/1024)
// would undo the R19.11 cost win without breaking anything functionally —
// exactly the kind of regression only a pin catches.
// =====================================================================

describe('ComputeStack — container-backed proxy Lambdas at 512 MB (P28 R19.11)', () => {
  let source: string;

  beforeAll(async () => {
    const { readFileSync } = await import('fs');
    const { join } = await import('path');
    source = readFileSync(join(__dirname, '..', 'lib', 'compute-stack.ts'), 'utf-8');
  });

  /** The source slice defining one Lambda: from its functionName to the next Function construct. */
  function lambdaBlock(functionName: string): string {
    const start = source.indexOf(`functionName: '${functionName}'`);
    expect(start, `${functionName} definition must exist in compute-stack.ts`).toBeGreaterThan(-1);
    const end = source.indexOf('new lambda.Function(', start);
    return source.slice(start, end === -1 ? undefined : end);
  }

  const PROXY_FUNCTIONS = [
    'agentic_data_ingestion_create_code',
    'agentic_data_ingestion_test_code',
    'agentic_data_ingestion_create_glue_pipeline',
    'agentic_data_ingestion_create_infrastructure',
  ];

  it.each(PROXY_FUNCTIONS)('%s is defined with memorySize: 512', (fnName) => {
    const block = lambdaBlock(fnName);
    expect(block).toContain('memorySize: 512');
    // No stray pre-token size survives inside the definition
    expect(block).not.toContain('memorySize: 1024');
    expect(block).not.toContain('memorySize: 2048');
  });

  it('the proxy path stays enabled (USE_AGENTCORE_RUNTIME=true) for all four steps', () => {
    // The 512 MB sizing is only honest while the handler dispatches to the
    // container; each definition must carry the runtime flag.
    for (const fnName of PROXY_FUNCTIONS) {
      const block = lambdaBlock(fnName);
      expect(block, `${fnName} must dispatch to AgentCore`).toContain("USE_AGENTCORE_RUNTIME: 'true'");
    }
  });
});
