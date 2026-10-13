// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { AuthStack } from '../lib/auth-stack';

/**
 * CDK assertion tests for the Auth stack (P27, Requirement 5).
 *
 * The Cognito Hosted UI domain prefix must be account-unique: prefixes are
 * globally unique across ALL AWS accounts, so a deterministic prefix
 * collides when two accounts deploy the same environment name.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';

function synthAuth(environment: string): Template {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const stack = new AuthStack(app, `AgenticDataIngestion-Auth-${environment}`, {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    environment,
  });
  return Template.fromStack(stack);
}

describe('AuthStack — account-unique Hosted UI domain (P27 Req 5)', () => {
  // ── Req 5.1/5.6 — prefix embeds the account ID ────────────────────
  it('synthesized UserPoolDomain embeds the account ID', () => {
    const template = synthAuth('dev');
    template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
      Domain: `agentic-data-ingestion-${TEST_ACCOUNT}-dev`,
    });
  });

  // ── Req 5.2 — constraint set holds for the synthesized prefix ─────
  it('prefix satisfies Cognito constraints (charset, length, no reserved words)', () => {
    const template = synthAuth('dev');
    const domains = template.findResources('AWS::Cognito::UserPoolDomain');
    const prefixes = Object.values(domains).map(
      (r) => (r.Properties as { Domain: string }).Domain,
    );
    expect(prefixes).toHaveLength(1);
    const prefix = prefixes[0];
    expect(prefix).toMatch(/^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/); // charset, no lead/trail hyphen
    expect(prefix.length).toBeLessThanOrEqual(63);
    expect(prefix).not.toMatch(/aws|amazon|cognito/);
  });

  // ── publication-cleanup R12 — hardened password policy ─────────────
  it('enforces the hardened password policy (16 chars, all classes, 24h temp validity)', () => {
    const template = synthAuth('dev');
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      Policies: {
        PasswordPolicy: {
          MinimumLength: 16,
          RequireLowercase: true,
          RequireUppercase: true,
          RequireNumbers: true,
          RequireSymbols: true,
          TemporaryPasswordValidityDays: 1,
        },
      },
    });
  });

  // ── Security review S3 — MFA required by default, TOTP only ────────
  // Was OPTIONAL for every deployment (publication-cleanup R12) to keep the e2e
  // harness's USER_PASSWORD_AUTH sign-in working, but that harness's client only
  // exists with adi:deployE2eClient=true, so MFA now follows that flag.
  it('MFA is REQUIRED by default with TOTP (no SMS)', () => {
    const template = synthAuth('dev');
    template.hasResourceProperties('AWS::Cognito::UserPool', {
      MfaConfiguration: 'ON',
      EnabledMfas: ['SOFTWARE_TOKEN_MFA'],
    });
  });

  const synthAuthWithContext = (context: Record<string, string>) => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [], ...context } });
    const stack = new AuthStack(app, 'AgenticDataIngestion-Auth-ctx', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      environment: 'dev',
      e2eClientIdParameterName: '/agentic-data-ingestion/dev/e2e-client-id',
    });
    return Template.fromStack(stack);
  };

  it('MFA is OPTIONAL exactly when the e2e client is deployed, so the harness can sign in', () => {
    synthAuthWithContext({ 'adi:deployE2eClient': 'true' }).hasResourceProperties('AWS::Cognito::UserPool', {
      MfaConfiguration: 'OPTIONAL',
      EnabledMfas: ['SOFTWARE_TOKEN_MFA'],
    });
  });

  it('adi:requireMfa=false overrides the default to OPTIONAL', () => {
    synthAuthWithContext({ 'adi:requireMfa': 'false' }).hasResourceProperties('AWS::Cognito::UserPool', {
      MfaConfiguration: 'OPTIONAL',
    });
  });

  it('adi:requireMfa=true overrides the e2e client to REQUIRED', () => {
    synthAuthWithContext({ 'adi:deployE2eClient': 'true', 'adi:requireMfa': 'true' }).hasResourceProperties(
      'AWS::Cognito::UserPool',
      { MfaConfiguration: 'ON' },
    );
  });

  // ── e2e app client: USER_PASSWORD_AUTH only, no secret ─────────────
  it('creates no e2e app client by default (R-05)', () => {
    // A USER_PASSWORD_AUTH client is attack surface only the e2e suite needs.
    const template = synthAuth('dev');
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 0);
    template.resourceCountIs('AWS::SSM::Parameter', 0);
    expect(Object.keys(template.toJSON().Outputs ?? {})).not.toContain('E2eTestClientId');
  });

  it('adi:deployE2eClient=true creates the e2e client with USER_PASSWORD_AUTH, no secret, and publishes its id', () => {
    const app = new cdk.App({
      context: { 'aws:cdk:bundling-stacks': [], 'adi:deployE2eClient': 'true' },
    });
    const stack = new AuthStack(app, 'AgenticDataIngestion-Auth-e2e', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      environment: 'dev',
      e2eClientIdParameterName: '/agentic-data-ingestion/dev/e2e-client-id',
    });
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::Cognito::UserPoolClient', {
      ClientName: 'agentic-data-ingestion-dev-e2e',
      ExplicitAuthFlows: Match.arrayWith(['ALLOW_USER_PASSWORD_AUTH', 'ALLOW_REFRESH_TOKEN_AUTH']),
      GenerateSecret: false,
    });
    // The WebSocket authorizer reads the id from here (R-05).
    template.hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/agentic-data-ingestion/dev/e2e-client-id',
      Type: 'String',
    });
    expect(Object.keys(template.toJSON().Outputs ?? {})).toContain('E2eTestClientId');
  });

  it('adi:requireMfa=true enforces pool-level MFA', () => {
    const app = new cdk.App({
      context: { 'aws:cdk:bundling-stacks': [], 'adi:requireMfa': 'true' },
    });
    const stack = new AuthStack(app, 'AgenticDataIngestion-Auth-mfa', {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      environment: 'dev',
    });
    Template.fromStack(stack).hasResourceProperties('AWS::Cognito::UserPool', {
      MfaConfiguration: 'ON',
    });
  });

  // ── Req 5.2 — synth-time length guard ─────────────────────────────
  it('throws at synth when the environment name pushes the prefix past 63 chars', () => {
    // prefix base = 'agentic-data-ingestion-' (23) + 12 + '-' (1) = 36; 28-char env → 64 > 63
    const longEnv = 'a'.repeat(28);
    expect(() => synthAuth(longEnv)).toThrowError(/exceeds|max 63|63 chars/i);
  });

  it('accepts the longest environment name that still fits (27 chars)', () => {
    const template = synthAuth('a'.repeat(27));
    template.hasResourceProperties('AWS::Cognito::UserPoolDomain', {
      Domain: `agentic-data-ingestion-${TEST_ACCOUNT}-${'a'.repeat(27)}`,
    });
  });
});
