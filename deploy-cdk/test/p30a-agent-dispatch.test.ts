// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * CDK assertion tests for the generic Agent_Dispatch_Lambda registration
 * (p30a R1.2, task 1.2).
 *
 * Three stack-level contracts are pinned here, each because breaking it
 * fails at RUNTIME rather than at synth:
 *
 *   1. Compute — the function is registered with a >= 900s timeout. The
 *      handler pins AGENT_READ_TIMEOUT_SECONDS = 870 and its docstring names
 *      compute-stack.ts as the owner of the enclosing budget. If the Lambda
 *      timeout drops below 870s, boto3's read timeout can never fire and a
 *      long agent run surfaces as an opaque Lambda timeout instead of the
 *      handled failure the handler is written to produce.
 *   2. Compute — the agent endpoint env vars live on THIS function. Callers
 *      pass `agent_endpoint_env` (the NAME of an env var), resolved inside
 *      this process — having the value only on the dashboard API's
 *      environment would resolve to "agent not configured".
 *   3. Foundation — the dispatch role holds bedrock-agentcore:InvokeAgentRuntime
 *      scoped to the agent runtime prefixes it actually dispatches to, and the
 *      dashboard API role can invoke the dispatch function.
 *
 * Compute assertions are SOURCE-level, following the established convention:
 * the full ComputeStack (20+ Lambdas with Code.fromAsset) exceeds vitest
 * worker memory during synth (see hosting-auth.test.ts and p28-idle-proxy.test.ts).
 * Full-template validation is `cdk synth` in CI.
 *
 * The EventBridge rule -> dispatch Lambda target assertions are task 1.8 and
 * live in orchestration-stack.test.ts — deliberately not pre-empted here.
 *
 * Validates: Requirements 1.2
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const DISPATCH_FN_NAME = 'agentic_data_ingestion_agent_dispatch';
const DISPATCH_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-agentdispatch-role`;
const DASHBOARD_API_ROLE_NAME = `agentic-data-ingestion-${TEST_ENV}-infrastructure-role`;

const COST_RUNTIME_ARN =
  `arn:aws:bedrock-agentcore:${TEST_REGION}:${TEST_ACCOUNT}:runtime/agentic_data_ingestion_agent_cost_optimization-*`;
const DISPATCH_FN_ARN =
  `arn:aws:lambda:${TEST_REGION}:${TEST_ACCOUNT}:function:${DISPATCH_FN_NAME}`;

// =====================================================================
// Foundation — the dispatch role's AgentCore grant and the dashboard
// API's invoke grant. The Foundation stack OWNS every Lambda role, so
// these grants deploy with Foundation, not with Compute.
// =====================================================================

describe('FoundationStack — agent dispatch IAM (p30a R1.2)', () => {
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

  /** Every policy statement attached (inline or via AWS::IAM::Policy) to a named role. */
  function statementsForRole(roleName: string): any[] {
    const resources = json.Resources ?? {};
    const roleKey = Object.keys(resources).find(
      (k) =>
        resources[k].Type === 'AWS::IAM::Role' &&
        resources[k].Properties?.RoleName === roleName,
    );
    expect(roleKey, `${roleName} must exist`).toBeDefined();

    const statements: any[] = [];
    for (const inline of resources[roleKey!].Properties?.Policies ?? []) {
      statements.push(...(inline.PolicyDocument?.Statement ?? []));
    }
    // addToPolicy() statements land in AWS::IAM::Policy resources (DefaultPolicy
    // plus any OverflowPolicy CDK splits out) whose Roles reference the role.
    for (const resource of Object.values(resources) as any[]) {
      if (resource.Type !== 'AWS::IAM::Policy') continue;
      const attached = (resource.Properties?.Roles ?? []).some((r: any) => r?.Ref === roleKey);
      if (attached) statements.push(...(resource.Properties?.PolicyDocument?.Statement ?? []));
    }
    return statements;
  }

  function asList(v: any): any[] {
    return Array.isArray(v) ? v : [v];
  }

  it('grants bedrock-agentcore:InvokeAgentRuntime to the dispatch role', () => {
    const invokeStmts = statementsForRole(DISPATCH_ROLE_NAME).filter((s) =>
      asList(s.Action).includes('bedrock-agentcore:InvokeAgentRuntime'),
    );
    expect(
      invokeStmts.length,
      'the dispatch Lambda cannot reach any agent without InvokeAgentRuntime',
    ).toBeGreaterThanOrEqual(1);
    for (const s of invokeStmts) expect(s.Effect).toBe('Allow');
  });

  it('scopes the InvokeAgentRuntime grant to the cost-optimization runtime prefix, never Resource *', () => {
    // Least privilege, matching the self-healing dispatch grant: CloudFormation
    // appends a unique suffix to the runtime id, so a name-prefix wildcard is
    // required — but `runtime/*` is not.
    const invokeStmts = statementsForRole(DISPATCH_ROLE_NAME).filter((s) =>
      asList(s.Action).includes('bedrock-agentcore:InvokeAgentRuntime'),
    );
    const arns = invokeStmts.flatMap((s) => asList(s.Resource));
    expect(arns).toContain(COST_RUNTIME_ARN);
    expect(arns, 'InvokeAgentRuntime must not be granted on all resources').not.toContain('*');
    for (const arn of arns) {
      expect(arn).toContain(':runtime/agentic_data_ingestion_agent_');
    }
  });

  it('the dispatch role carries no InvokeAgentRuntime grant beyond enumerated agent runtimes', () => {
    // The role is deliberately built with NO managed policies (no
    // agentCorePolicy, which grants InvokeAgentRuntime on Resource *).
    const resources = json.Resources ?? {};
    const roleKey = Object.keys(resources).find(
      (k) =>
        resources[k].Type === 'AWS::IAM::Role' &&
        resources[k].Properties?.RoleName === DISPATCH_ROLE_NAME,
    );
    const managed = (resources[roleKey!].Properties?.ManagedPolicyArns ?? []) as any[];
    const refsAgentCorePolicy = managed.some(
      (m) => typeof m === 'object' && JSON.stringify(m).includes('AgentCorePolicy'),
    );
    expect(
      refsAgentCorePolicy,
      'attaching AgentCorePolicy would silently widen InvokeAgentRuntime to Resource *',
    ).toBe(false);
  });

  it('grants the dashboard API role lambda:InvokeFunction on the dispatch function', () => {
    const invokeStmts = statementsForRole(DASHBOARD_API_ROLE_NAME).filter((s) =>
      asList(s.Action).includes('lambda:InvokeFunction'),
    );
    expect(invokeStmts.length).toBeGreaterThanOrEqual(1);
    const arns = invokeStmts.flatMap((s) => asList(s.Resource));
    // The function-specific grant must be present in its own right: the
    // project-wide agentic_data_ingestion_* wildcard also covers it today, and
    // if that wildcard is ever tightened the agent endpoints must not silently
    // start accepting work they cannot dispatch.
    expect(
      arns,
      'dashboard API must be able to invoke the dispatch function by name',
    ).toContain(DISPATCH_FN_ARN);
  });
});

// =====================================================================
// Compute — registration shape, source-level (full synth exceeds vitest
// worker memory; `cdk synth` covers the template).
// =====================================================================

describe('ComputeStack — agent dispatch registration (p30a R1.2)', () => {
  let source: string;
  let handlerSource: string;

  beforeAll(async () => {
    const { readFileSync } = await import('fs');
    const { join } = await import('path');
    source = readFileSync(join(__dirname, '..', 'lib', 'compute-stack.ts'), 'utf-8');
    handlerSource = readFileSync(
      join(__dirname, '..', '..', 'lambda', DISPATCH_FN_NAME, 'lambda_function.py'),
      'utf-8',
    );
  });

  /** The source slice defining one Lambda: from its functionName to the next Function construct. */
  function lambdaBlock(functionName: string): string {
    const start = source.indexOf(`functionName: '${functionName}'`);
    expect(start, `${functionName} definition must exist in compute-stack.ts`).toBeGreaterThan(-1);
    const end = source.indexOf('new lambda.Function(', start);
    return source.slice(start, end === -1 ? undefined : end);
  }

  it('registers the dispatch Lambda with the handler entry point and the dedicated role', () => {
    const block = lambdaBlock(DISPATCH_FN_NAME);
    expect(block).toContain("handler: 'lambda_function.lambda_handler'");
    // Code comes from lambda/<fnName>/ via the `lambdaCode` helper. The helper
    // wraps `lambdaCodePath` and adds the generated-file exclusions that keep
    // the asset hash stable, so assert both halves: the call site names this
    // function, and the helper still resolves to lambda/<fnName>/.
    expect(block).toContain(`code: lambdaCode('${DISPATCH_FN_NAME}')`);
    expect(source).toMatch(
      /const lambdaCode = \(fnName: string\) =>\s*\n\s*lambda\.Code\.fromAsset\(lambdaCodePath\(fnName\)/,
    );
    expect(block).toContain('role: foundation.agentDispatchRole');
  });

  it('sets a 900s timeout — at least the 15 minutes R1.2 requires', () => {
    const block = lambdaBlock(DISPATCH_FN_NAME);
    const match = block.match(/timeout:\s*cdk\.Duration\.seconds\((\d+)\)/);
    expect(match, 'the dispatch Lambda must set an explicit timeout').not.toBeNull();
    expect(Number(match![1])).toBeGreaterThanOrEqual(900);
  });

  it('keeps the Lambda timeout above the handler read timeout', () => {
    // R1.2 asks for a boto3 read timeout no longer than the enclosing Lambda
    // timeout. The two numbers live in different files and languages, so
    // nothing but this test keeps them consistent.
    const block = lambdaBlock(DISPATCH_FN_NAME);
    const lambdaTimeout = Number(block.match(/timeout:\s*cdk\.Duration\.seconds\((\d+)\)/)![1]);
    const readTimeoutMatch = handlerSource.match(/AGENT_READ_TIMEOUT_SECONDS\s*=\s*(\d+)/);
    expect(readTimeoutMatch, 'handler must pin AGENT_READ_TIMEOUT_SECONDS').not.toBeNull();
    const readTimeout = Number(readTimeoutMatch![1]);
    expect(readTimeout).toBeLessThan(lambdaTimeout);
  });

  it('carries the agent endpoint env vars the handler resolves agent_endpoint_env against', () => {
    const block = lambdaBlock(DISPATCH_FN_NAME);
    expect(block).toContain('COST_OPTIMIZATION_AGENT_ENDPOINT');
    expect(block).toContain('DATA_QUALITY_AGENT_ENDPOINT');
  });

  it('attaches the shared Lambda layer and ADOT observability layer, like its siblings', () => {
    const block = lambdaBlock(DISPATCH_FN_NAME);
    expect(block).toContain('layers: [adotLayer, strandsLayer]');
    expect(block).toContain('architecture: lambda.Architecture.ARM_64');
  });

  // p30a R2.2 (task 1.5): the dashboard API needs the dispatch function's NAME
  // on its environment. Without it handlers/agents.py has nothing to invoke and
  // the lambda:InvokeFunction grant in Foundation is inert — both agent
  // endpoints then report themselves unconfigured. The env var name is the
  // contract between compute-stack.ts and the Python handler, and nothing but
  // this test keeps the two spellings in step.
  it('puts AGENT_DISPATCH_FN on the dashboard API, resolved from the dispatch function', () => {
    const block = lambdaBlock('agentic_data_ingestion_dashboard_api');
    expect(block).toContain('AGENT_DISPATCH_FN: this.agentDispatchLambda.functionName');
  });

  it('spells the dispatch env var the same way the dashboard API handler reads it', async () => {
    const { readFileSync } = await import('fs');
    const { join } = await import('path');
    const agentsHandler = readFileSync(
      join(__dirname, '..', '..', 'lambda', 'agentic_data_ingestion_dashboard_api', 'handlers', 'agents.py'),
      'utf-8',
    );
    const match = agentsHandler.match(/DISPATCH_FN_ENV\s*=\s*"([A-Z_]+)"/);
    expect(match, 'handlers/agents.py must pin DISPATCH_FN_ENV').not.toBeNull();
    expect(lambdaBlock('agentic_data_ingestion_dashboard_api')).toContain(`${match![1]}:`);
  });
});
