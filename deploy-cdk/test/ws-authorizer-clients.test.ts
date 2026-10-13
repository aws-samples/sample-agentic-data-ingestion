// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';

/**
 * Threat model T-06, R-05: the WebSocket $connect authorizer accepts only
 * tokens issued to this deployment's app clients.
 *
 * The dashboard client is created in Hosting, which deploys after Compute, so
 * the authorizer cannot take its ID as an environment value. Hosting and Auth
 * write the IDs to SSM parameters, and Compute tells the authorizer their
 * names and grants it GetParameters on exactly those. The bin/ entry point
 * owns the names; this checks that Compute wires what it is given.
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';
const PARAMETERS = ['/agentic-data-ingestion/dev/dashboard-client-id', '/agentic-data-ingestion/dev/e2e-client-id'];

let compute: Record<string, any>;
let foundation: Record<string, any>;

beforeAll(() => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };
  const foundationStack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env, projectName: 'agentic-data-ingestion', environment: TEST_ENV,
  });
  const data = new DataStack(app, `AgenticDataIngestion-Data-${TEST_ENV}`, { env, environment: TEST_ENV });
  const auth = new AuthStack(app, `AgenticDataIngestion-Auth-${TEST_ENV}`, { env, environment: TEST_ENV });
  const computeStack = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env, environment: TEST_ENV, projectName: 'agentic-data-ingestion', foundation: foundationStack, data,
    userPoolArn: auth.userPool.userPoolArn,
    wsClientIdParameterNames: PARAMETERS,
  });
  compute = Template.fromStack(computeStack).toJSON();
  foundation = Template.fromStack(foundationStack).toJSON();
}, 300_000);

const authorizer = () =>
  (Object.values(compute.Resources) as any[]).find(
    (r) => r.Type === 'AWS::Lambda::Function' && r.Properties.FunctionName === 'agentic_data_ingestion_ws_authorizer',
  );

describe('R-05 — the WebSocket authorizer knows where its allowed clients are', () => {
  it('names both client-id parameters, and no longer carries the empty USER_POOL_CLIENT_ID', () => {
    const vars = authorizer().Properties.Environment.Variables;
    expect(vars.USER_POOL_CLIENT_ID_PARAMETERS).toBe(PARAMETERS.join(','));
    expect(vars).not.toHaveProperty('USER_POOL_CLIENT_ID');
    expect(JSON.stringify(vars.USER_POOL_ID)).toContain('userpool/');
  });

  it('can read exactly those parameters and nothing else in SSM', () => {
    const all = [compute, foundation].flatMap((t) =>
      (Object.values(t.Resources) as any[])
        .filter((r) => r.Type === 'AWS::IAM::Policy')
        .flatMap((r) => r.Properties.PolicyDocument.Statement),
    );
    const ssm = all.filter((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]).some((a: string) => a.startsWith('ssm:')));
    expect(ssm).toHaveLength(1);
    expect(ssm[0].Action).toBe('ssm:GetParameters');
    const resources = JSON.stringify(ssm[0].Resource);
    for (const name of PARAMETERS) expect(resources).toContain(`parameter${name}`);
    expect(resources).not.toContain('parameter/*');
  });
});

// Security review S2: the authorizer parses unauthenticated input on the one
// edge with no WAF, so it runs on its own role holding only what it calls.
// It used to share foundation.infrastructureRole, which carries
// states:SendTaskSuccess/SendTaskFailure on '*'.
describe('S2 — the WebSocket authorizer runs on its own least-privilege role', () => {
  const roleLogicalId = () => authorizer().Properties.Role['Fn::GetAtt'][0];
  const roleStatements = () =>
    (Object.values(compute.Resources) as any[])
      .filter(
        (r) =>
          r.Type === 'AWS::IAM::Policy' &&
          (r.Properties.Roles ?? []).some((ref: any) => ref.Ref === roleLogicalId()),
      )
      .flatMap((r) => r.Properties.PolicyDocument.Statement);

  it('is a role in the Compute stack, not a Foundation role', () => {
    // An Fn::GetAtt to a local resource; a Foundation role would arrive as an import.
    expect(compute.Resources[roleLogicalId()].Type).toBe('AWS::IAM::Role');
    expect(JSON.stringify(authorizer().Properties.Role)).not.toContain('Fn::ImportValue');
  });

  it('trusts only Lambda and carries only the basic execution managed policy', () => {
    const role = compute.Resources[roleLogicalId()].Properties;
    expect(JSON.stringify(role.AssumeRolePolicyDocument)).toContain('lambda.amazonaws.com');
    expect(role.ManagedPolicyArns).toHaveLength(1);
    expect(JSON.stringify(role.ManagedPolicyArns[0])).toContain('service-role/AWSLambdaBasicExecutionRole');
  });

  it('grants ssm:GetParameters on the named parameters and no other action', () => {
    const actions = roleStatements().flatMap((s: any) => (Array.isArray(s.Action) ? s.Action : [s.Action]));
    expect(actions).toEqual(['ssm:GetParameters']);
    expect(JSON.stringify(roleStatements())).not.toContain('states:');
  });
});

// Security review S8: the only rate limit on the WebSocket edge, which cannot
// be associated with WAF.
describe('S8 — the WebSocket stage is throttled', () => {
  it('sets a default route throttle on the stage', () => {
    const stage = (Object.values(compute.Resources) as any[]).find((r) => r.Type === 'AWS::ApiGatewayV2::Stage');
    expect(stage.Properties.DefaultRouteSettings).toMatchObject({
      ThrottlingRateLimit: 50,
      ThrottlingBurstLimit: 100,
    });
  });
});

// Security review S9 and S10, on the REST stage built by the same synth.
describe('S9/S10 — the REST stage access log, and one CORS knob', () => {
  const restStage = () =>
    (Object.values(compute.Resources) as any[]).find((r) => r.Type === 'AWS::ApiGateway::Stage');

  it('writes an access log to a one-month log group, with no header or query-string field', () => {
    const settings = restStage().Properties.AccessLogSetting;
    expect(settings).toBeDefined();
    const groupLogicalId = settings.DestinationArn['Fn::GetAtt'][0];
    const group = compute.Resources[groupLogicalId];
    expect(group.Type).toBe('AWS::Logs::LogGroup');
    expect(group.Properties.RetentionInDays).toBe(30);
    expect(group.Properties.LogGroupName).toBe('/aws/apigateway/agentic-data-ingestion-dev-dashboard-api-access');
    const format = settings.Format as string;
    for (const field of ['$context.requestId', '$context.identity.sourceIp', '$context.status', '$context.authorizer.claims.sub']) {
      expect(format).toContain(field);
    }
    // Tokens travel in the Authorization header; nothing here may echo a header
    // value or the query string.
    expect(format).not.toMatch(/\$input|\$context\.requestOverride|querystring|header/i);
  });

  it('declares no allowedOrigin prop: origin narrowing lives only at the edge (LGR-f34fe5c-2)', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const source = fs.readFileSync(path.join(__dirname, '..', 'lib', 'compute-stack.ts'), 'utf8');
    // The comment that records the removal may name it; a declaration or a read may not.
    expect(source).not.toMatch(/allowedOrigin\?\s*:|props\.allowedOrigin/);
  });
});
