// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { HostingStack } from '../lib/hosting-stack';

/**
 * Threat model T-16, R-12: CloudFront adds browser security headers to every
 * dashboard and API response.
 *
 * The SPA policy carries a Content-Security-Policy listing exactly the origins
 * the dashboard talks to (see the comment above `spaContentSecurityPolicy` in
 * lib/hosting-stack.ts for why each is there). What these tests cannot show is
 * that the dashboard works under that policy in a browser: a source it needs
 * and the CSP omits shows up only as a console violation after deploy.
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';
const HOSTED_UI = 'https://agentic-data-ingestion-111111111111-dev.auth.us-west-2.amazoncognito.com';

let json: Record<string, any>;

function policy(prefix: string): Record<string, any> {
  const hits = (Object.entries(json.Resources) as [string, any][]).filter(
    ([id, r]) => r.Type === 'AWS::CloudFront::ResponseHeadersPolicy' && id.startsWith(prefix),
  );
  expect(hits, `expected exactly one ${prefix} policy`).toHaveLength(1);
  return hits[0][1].Properties.ResponseHeadersPolicyConfig;
}

/** Flatten an Fn::Join / Fn::Select tree to greppable text. */
function flatten(value: any): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(flatten).join('');
  if (value && typeof value === 'object') {
    if (value['Fn::Join']) return (value['Fn::Join'][1] as any[]).map(flatten).join(value['Fn::Join'][0]);
    if (value.Ref) return `\${${value.Ref}}`;
    return Object.values(value).map(flatten).join('');
  }
  return '';
}

const directives = (csp: string) =>
  Object.fromEntries(
    csp.split(';').map((d) => d.trim()).filter(Boolean).map((d) => {
      const [name, ...sources] = d.split(/\s+/);
      return [name, sources];
    }),
  ) as Record<string, string[]>;

beforeAll(() => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const stack = new HostingStack(app, `AgenticDataIngestion-Hosting-${TEST_ENV}`, {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    environment: TEST_ENV,
    restApiHost: 'abc123.execute-api.us-west-2.amazonaws.com',
    userPoolId: 'us-west-2_TestPool',
    userPoolArn: `arn:aws:cognito-idp:${TEST_REGION}:${TEST_ACCOUNT}:userpool/us-west-2_TestPool`,
    hostedUiDomain: HOSTED_UI,
    wsUrl: 'wss://ws123.execute-api.us-west-2.amazonaws.com/prod',
    dashboardClientIdParameterName: '/agentic-data-ingestion/dev/dashboard-client-id',
  });
  json = Template.fromStack(stack).toJSON();
});

describe('R-12 — security headers on both behaviours', () => {
  it('the default (SPA) behaviour and /api/* each carry a policy with security headers', () => {
    const distribution = (Object.values(json.Resources) as any[]).find((r) => r.Type === 'AWS::CloudFront::Distribution');
    const config = distribution.Properties.DistributionConfig;
    expect(JSON.stringify(config.DefaultCacheBehavior.ResponseHeadersPolicyId)).toContain('SpaSecurityHeaders');
    const api = config.CacheBehaviors.find((b: any) => b.PathPattern === '/api/*');
    expect(JSON.stringify(api.ResponseHeadersPolicyId)).toContain('ApiCorsHeaders');
  });

  it.each(['SpaSecurityHeaders', 'ApiCorsHeaders'])('%s sets HSTS, nosniff, frame DENY and a referrer policy, overriding the origin', (id) => {
    const headers = policy(id).SecurityHeadersConfig;
    expect(headers.StrictTransportSecurity).toMatchObject({ AccessControlMaxAgeSec: 31536000, IncludeSubdomains: true, Override: true });
    expect(headers.ContentTypeOptions).toEqual({ Override: true });
    expect(headers.FrameOptions).toEqual({ FrameOption: 'DENY', Override: true });
    expect(headers.ReferrerPolicy).toEqual({ ReferrerPolicy: 'strict-origin-when-cross-origin', Override: true });
    expect(headers.ContentSecurityPolicy.Override).toBe(true);
  });

  it('the API CSP allows nothing (JSON responses are never rendered)', () => {
    expect(policy('ApiCorsHeaders').SecurityHeadersConfig.ContentSecurityPolicy.ContentSecurityPolicy).toBe(
      "default-src 'none'; frame-ancestors 'none'",
    );
  });
});

describe('R-12 — the dashboard Content-Security-Policy', () => {
  let csp: Record<string, string[]>;
  let raw: string;
  beforeAll(() => {
    raw = flatten(policy('SpaSecurityHeaders').SecurityHeadersConfig.ContentSecurityPolicy.ContentSecurityPolicy);
    csp = directives(raw);
  });

  it('stays under CloudFront\'s 1783-character limit with realistic values', () => {
    expect(raw.length).toBeLessThan(1783);
  });

  it('runs only same-origin script: no inline script, no eval', () => {
    expect(csp['script-src']).toEqual(["'self'"]);
    expect(raw).not.toContain("'unsafe-eval'");
    expect(csp['script-src']).not.toContain("'unsafe-inline'");
  });

  it('forbids framing, plugins, base rewrites and cross-origin form posts', () => {
    expect(csp['frame-ancestors']).toEqual(["'none'"]);
    expect(csp['object-src']).toEqual(["'none'"]);
    expect(csp['base-uri']).toEqual(["'self'"]);
    expect(csp['form-action']).toEqual(["'self'"]);
    expect(csp['default-src']).toEqual(["'self'"]);
  });

  it('connects only to the API, Cognito, the WebSocket API and the source bucket', () => {
    const connect = csp['connect-src'];
    expect(connect).toContain("'self'");
    expect(connect).toContain(`https://cognito-idp.${TEST_REGION}.amazonaws.com`);
    expect(connect).toContain(HOSTED_UI);
    expect(connect.some((s) => s.startsWith('wss://') && !s.endsWith('/prod'))).toBe(true);
    const bucket = `adi-source-files-${TEST_ACCOUNT}-${TEST_REGION}`;
    expect(connect).toContain(`https://${bucket}.s3.amazonaws.com`);
    expect(connect).toContain(`https://${bucket}.s3.${TEST_REGION}.amazonaws.com`);
    expect(connect.filter((s) => s.includes('*'))).toEqual([]);
  });

  it('frames only the Cognito Hosted UI (oidc-client-ts silent renew)', () => {
    expect(csp['frame-src']).toEqual([HOSTED_UI]);
  });
});

describe('R-05 — Hosting publishes the dashboard client id for the WebSocket authorizer', () => {
  it('writes the app client id to the named SSM parameter', () => {
    const params = (Object.values(json.Resources) as any[]).filter((r) => r.Type === 'AWS::SSM::Parameter');
    expect(params).toHaveLength(1);
    expect(params[0].Properties.Name).toBe('/agentic-data-ingestion/dev/dashboard-client-id');
    expect(JSON.stringify(params[0].Properties.Value)).toContain('UserPoolClient');
  });
});
