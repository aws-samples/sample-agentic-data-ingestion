// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { AgentCoreStack } from '../lib/agentcore-stack';

/**
 * Threat model R-02: every AgentCore runtime has its own execution role, and
 * the MCP servers share one read-only role.
 *
 * Before the split, nine agents and five MCP servers shared one role holding
 * the union of their needs, so the agent that reads chat text could create IAM
 * roles and complete Step Functions task states. These tests pin, against the
 * synthesized templates:
 *   1. which role each runtime runs as (no runtime test existed before);
 *   2. the capabilities only the runtimes that call them may hold;
 *   3. the reads the code relies on (the target-schema object, the source
 *      bucket), so a narrowing cannot remove one silently.
 *
 * What these cannot see: a call the code makes that the role lacks. That is an
 * AccessDenied at run time. The inventory behind each role is in the comment
 * block above the roles in lib/foundation-stack.ts.
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

/** runtime name (AgentRuntimeName) -> construct id of the role it must use. */
const EXPECTED_ROLE_BY_RUNTIME: Record<string, string> = {
  agentic_data_ingestion_agent_create_code: 'CreateCodeRuntimeRole',
  agentic_data_ingestion_agent_test_code: 'TestCodeRuntimeRole',
  agentic_data_ingestion_agent_create_pipeline: 'CreateGluePipelineRuntimeRole',
  agentic_data_ingestion_agent_create_infra: 'CreateInfrastructureRuntimeRole',
  agentic_data_ingestion_agent_self_healing: 'SelfHealingRuntimeRole',
  agentic_data_ingestion_agent_complex_mapping: 'ComplexMappingRuntimeRole',
  agentic_data_ingestion_agent_data_quality: 'DataQualityRuntimeRole',
  agentic_data_ingestion_agent_cost_optimization: 'CostOptimizationRuntimeRole',
  agentic_data_ingestion_agent_nl_pipeline_builder: 'NLPipelineBuilderRuntimeRole',
};
const AGENT_ROLE_IDS = Object.values(EXPECTED_ROLE_BY_RUNTIME);
const MCP_ROLE_ID = 'McpServerRuntimeRole';
const ALL_ROLE_IDS = [...AGENT_ROLE_IDS, MCP_ROLE_ID];

type Statement = { Effect: string; Action: string | string[]; Resource: any; Condition?: any; Sid?: string };

const asList = <T>(v: T | T[] | undefined): T[] => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

let foundationJson: Record<string, any>;
let agentcoreJson: Record<string, any>;

/** Logical id of the Foundation role whose construct id is `constructId`. */
function roleKey(constructId: string): string {
  const keys = Object.keys(foundationJson.Resources).filter(
    (k) => foundationJson.Resources[k].Type === 'AWS::IAM::Role' && k.startsWith(constructId) && /^[A-Z0-9]{8}$/.test(k.slice(constructId.length)),
  );
  expect(keys, `expected exactly one role for ${constructId}`).toHaveLength(1);
  return keys[0];
}

/** Every statement attached to a Foundation role: inline, default policy, managed. */
function statementsFor(constructId: string): Statement[] {
  const key = roleKey(constructId);
  const resources = foundationJson.Resources;
  const role = resources[key];
  const out: Statement[] = [];
  for (const inline of role.Properties?.Policies ?? []) out.push(...(inline.PolicyDocument?.Statement ?? []));
  for (const r of Object.values(resources) as any[]) {
    if (r.Type !== 'AWS::IAM::Policy') continue;
    if ((r.Properties?.Roles ?? []).some((ref: any) => ref?.Ref === key)) {
      out.push(...(r.Properties?.PolicyDocument?.Statement ?? []));
    }
  }
  expect(role.Properties?.ManagedPolicyArns ?? [], `${constructId} must carry no managed policy`).toEqual([]);
  return out;
}

const allowed = (constructId: string) => statementsFor(constructId).filter((s) => s.Effect === 'Allow');
const actionsOf = (constructId: string) => new Set(allowed(constructId).flatMap((s) => asList(s.Action)));
const grants = (constructId: string, action: string) => allowed(constructId).filter((s) => asList(s.Action).includes(action));
const resourceText = (s: Statement) => JSON.stringify(s.Resource);

beforeAll(() => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };
  const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env,
    projectName: 'agentic-data-ingestion',
    environment: TEST_ENV,
  });
  const agentcore = new AgentCoreStack(app, `AgenticDataIngestion-AgentCore-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    foundation,
  });
  foundationJson = Template.fromStack(foundation).toJSON();
  agentcoreJson = Template.fromStack(agentcore).toJSON();
});

describe('R-02 — each AgentCore runtime runs as its own role', () => {
  const runtimes = () =>
    (Object.values(agentcoreJson.Resources) as any[]).filter((r) => r.Type === 'AWS::BedrockAgentCore::Runtime');

  it('every agent runtime uses the role built for it', () => {
    const agents = runtimes().filter((r) => r.Properties.ProtocolConfiguration === 'HTTP');
    expect(agents.map((r) => r.Properties.AgentRuntimeName).sort()).toEqual(Object.keys(EXPECTED_ROLE_BY_RUNTIME).sort());
    for (const runtime of agents) {
      const expected = EXPECTED_ROLE_BY_RUNTIME[runtime.Properties.AgentRuntimeName];
      expect(JSON.stringify(runtime.Properties.RoleArn), runtime.Properties.AgentRuntimeName).toContain(roleKey(expected));
    }
  });

  it('every MCP server runtime uses the shared read-only MCP role', () => {
    const servers = runtimes().filter((r) => r.Properties.ProtocolConfiguration === 'MCP');
    expect(servers).toHaveLength(5);
    for (const runtime of servers) {
      expect(JSON.stringify(runtime.Properties.RoleArn), runtime.Properties.AgentRuntimeName).toContain(roleKey(MCP_ROLE_ID));
    }
  });

  it('no two agent runtimes share a role', () => {
    const keys = AGENT_ROLE_IDS.map(roleKey);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('the retired shared role grants nothing, is used by no runtime, and keeps its export for one release', () => {
    // Kept only so existing deployments' AgentCore stack can stop importing it
    // (see foundation-stack.ts). Remove with that block in the next release.
    const key = roleKey('AgentCoreRuntimeRole');
    const role = foundationJson.Resources[key];
    expect(role.Properties.RoleName).toBe(`agentic-data-ingestion-${TEST_ENV}-agentcore-runtime-role`);
    expect(role.Properties.Policies ?? []).toEqual([]);
    expect(role.Properties.ManagedPolicyArns ?? []).toEqual([]);
    const attached = (Object.values(foundationJson.Resources) as any[]).filter(
      (r) => r.Type === 'AWS::IAM::Policy' && JSON.stringify(r.Properties.Roles ?? []).includes(key),
    );
    expect(attached).toEqual([]);
    expect(JSON.stringify(agentcoreJson)).not.toContain(key);
    const exported = Object.values(foundationJson.Outputs ?? {}).some((o: any) => JSON.stringify(o.Value).includes(key));
    expect(exported, 'the export must survive this release').toBe(true);
  });

  it('every runtime role trusts only bedrock-agentcore', () => {
    for (const id of ALL_ROLE_IDS) {
      const doc = foundationJson.Resources[roleKey(id)].Properties.AssumeRolePolicyDocument;
      const principals = doc.Statement.flatMap((s: any) => asList(s.Principal?.Service));
      expect(principals, id).toEqual(['bedrock-agentcore.amazonaws.com']);
    }
  });
});

describe('R-02 — capabilities are held only by the runtimes that call them', () => {
  const holders = (action: string) => ALL_ROLE_IDS.filter((id) => actionsOf(id).has(action)).sort();

  it('only the job builder can create or modify an IAM role, and only with the boundary', () => {
    for (const action of ['iam:CreateRole', 'iam:PutRolePolicy']) {
      expect(holders(action), action).toEqual(['CreateGluePipelineRuntimeRole']);
    }
    for (const s of grants('CreateGluePipelineRuntimeRole', 'iam:CreateRole')) {
      expect(JSON.stringify(s.Condition?.ArnEquals?.['iam:PermissionsBoundary'])).toContain('GlueJobRoleBoundary');
    }
    expect(holders('iam:AttachRolePolicy')).toEqual([]);
  });

  it('PassRole is held by three runtimes, always conditioned on Glue', () => {
    expect(holders('iam:PassRole')).toEqual(['CreateGluePipelineRuntimeRole', 'SelfHealingRuntimeRole', 'TestCodeRuntimeRole']);
    for (const id of ALL_ROLE_IDS) {
      for (const s of grants(id, 'iam:PassRole')) {
        expect(s.Condition?.StringEquals?.['iam:PassedToService'], `${id}: ${resourceText(s)}`).toBe('glue.amazonaws.com');
      }
    }
    // The verifier passes the read-only verification role and nothing else.
    for (const s of grants('TestCodeRuntimeRole', 'iam:PassRole')) {
      expect(resourceText(s)).toContain('VerificationSessionRole');
    }
  });

  it('only the four pipeline-step runtimes can complete a task state', () => {
    const steps = ['CreateCodeRuntimeRole', 'CreateGluePipelineRuntimeRole', 'CreateInfrastructureRuntimeRole', 'TestCodeRuntimeRole'];
    expect(holders('states:SendTaskSuccess')).toEqual(steps);
    expect(holders('states:SendTaskFailure')).toEqual(steps);
    expect(holders('states:SendTaskHeartbeat'), 'reserved for a HeartbeatSeconds that no state sets').toEqual([]);
  });

  it('only the self-healing agent can start a pipeline execution', () => {
    expect(holders('states:StartExecution')).toEqual(['SelfHealingRuntimeRole']);
  });

  it('only the job builder holds glue:*, and its statement keeps both naming forms', () => {
    expect(holders('glue:*')).toEqual(['CreateGluePipelineRuntimeRole']);
    const [statement] = grants('CreateGluePipelineRuntimeRole', 'glue:*');
    for (const form of ['job/agentic_data_ingestion*', 'job/agentic-data-ingestion-*', 'crawler/agentic-data-ingestion-*', 'connection/agentic-data-ingestion-*']) {
      expect(resourceText(statement), form).toContain(form);
    }
  });

  it('no runtime reads a secret (T-19 removed the job-time credential lookup)', () => {
    expect(holders('secretsmanager:GetSecretValue')).toEqual([]);
  });

  it('no runtime invokes another runtime (containers run with MCP disabled)', () => {
    expect(holders('bedrock-agentcore:InvokeAgentRuntime')).toEqual([]);
  });

  it('Bedrock is held only by the runtimes that run a model', () => {
    expect(holders('bedrock:InvokeModel')).toEqual(
      ['ComplexMappingRuntimeRole', 'CreateCodeRuntimeRole', 'NLPipelineBuilderRuntimeRole', 'TestCodeRuntimeRole'],
    );
  });

  it('no runtime holds a DynamoDB table wildcard, and every one denies the task-tokens table', () => {
    for (const id of ALL_ROLE_IDS) {
      for (const s of allowed(id).filter((st) => asList(st.Action).some((a) => a.startsWith('dynamodb:')))) {
        expect(resourceText(s), id).not.toContain('table/agentic_data_ingestion*');
        expect(resourceText(s), id).not.toContain('task_tokens');
      }
      const deny = statementsFor(id).find((s) => s.Effect === 'Deny' && resourceText(s).includes('agentic_data_ingestion_task_tokens'));
      expect(deny, `${id} lacks the task-tokens Deny (T-22)`).toBeDefined();
      expect(asList(deny!.Action)).toEqual(['dynamodb:*']);
    }
  });

  it('the MCP server role is read-only', () => {
    // What every runtime needs to start and report: image pull, its own logs,
    // traces and metrics. Everything else on this role must be a read.
    const base = new Set([
      'ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability',
      'logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams', 'logs:DescribeLogGroups',
      'xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets',
      'cloudwatch:PutMetricData',
    ]);
    const writeShaped = [...actionsOf(MCP_ROLE_ID)].filter((a) => !base.has(a) && !/:(Get|List|BatchGet)/.test(a));
    expect(writeShaped).toEqual([]);
    expect([...actionsOf(MCP_ROLE_ID)].some((a) => a.startsWith('dynamodb:'))).toBe(false);
  });
});

describe('R-02 — the reads the code relies on are still granted', () => {
  it('create_code and test_code can read the target-schema object (adi-target-schemas)', () => {
    // target_schema_resolution.py reads s3://adi-target-schemas-<account>-<region>/<key>
    // from inside both containers (LGR-b2f79ef-7 pins the Lambda-role twin).
    const objectArn = `arn:aws:s3:::adi-target-schemas-${TEST_ACCOUNT}-${TEST_REGION}/schemas/p/target.json`;
    for (const id of ['CreateCodeRuntimeRole', 'TestCodeRuntimeRole']) {
      const patterns = grants(id, 's3:GetObject').flatMap((s) => asList(s.Resource)).filter((r) => typeof r === 'string');
      const reachable = patterns.some((p: string) =>
        new RegExp('^' + p.split('*').map((x) => x.replace(/[.?+^$()[\]{}|\\]/g, '\\$&')).join('.*') + '$').test(objectArn),
      );
      expect(reachable, `${id} cannot read ${objectArn}; patterns: ${patterns}`).toBe(true);
    }
  });

  it('the NL builder reads only the source bucket', () => {
    const s3 = allowed('NLPipelineBuilderRuntimeRole').filter((s) => asList(s.Action).some((a) => a.startsWith('s3:')));
    expect(s3).toHaveLength(1);
    expect(asList(s3[0].Action).sort()).toEqual(['s3:GetObject', 's3:ListBucket']);
    expect(resourceText(s3[0])).toContain('SourceFiles');
    expect(resourceText(s3[0])).not.toContain('adi-*');
  });

  it('the data-quality agent writes only under dq-reports/ in the processed bucket', () => {
    const puts = grants('DataQualityRuntimeRole', 's3:PutObject');
    expect(puts).toHaveLength(1);
    expect(resourceText(puts[0])).toContain('ProcessedFiles');
    expect(resourceText(puts[0])).toContain('/dq-reports/*');
  });
});
