// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { resolveEnv } from '../lib/resolve-env';

/**
 * Unit tests for the synth-time environment sanity check (P27, Requirement 7).
 *
 * resolveEnv() takes the env source as a parameter so tests never mutate
 * process.env (which other test files read at import time).
 */

describe('resolveEnv', () => {
  // ── Req 7.1 — unset account fails with actionable message ─────────
  it('throws when CDK_DEFAULT_ACCOUNT is not set, naming the variable and the fix', () => {
    expect(() => resolveEnv({})).toThrowError(/CDK_DEFAULT_ACCOUNT is not set/);
    expect(() => resolveEnv({})).toThrowError(/aws sts get-caller-identity/);
    expect(() => resolveEnv({})).toThrowError(/README\.md/);
  });

  it('throws when CDK_DEFAULT_ACCOUNT is not a 12-digit account ID', () => {
    for (const bad of ['abc', '1234', '12345678901x', '1234567890123']) {
      expect(() => resolveEnv({ CDK_DEFAULT_ACCOUNT: bad })).toThrowError(
        /not a 12-digit AWS account ID/,
      );
    }
  });

  // ── Valid values pass through unchanged ────────────────────────────
  it('returns concrete account and region when both are set', () => {
    const env = resolveEnv({
      CDK_DEFAULT_ACCOUNT: '111111111111',
      CDK_DEFAULT_REGION: 'eu-central-1',
    });
    expect(env).toEqual({ account: '111111111111', region: 'eu-central-1' });
  });

  // ── Req 7.2 — region fallback order ────────────────────────────────
  it('falls back to AWS_REGION when CDK_DEFAULT_REGION is unset', () => {
    const env = resolveEnv({
      CDK_DEFAULT_ACCOUNT: '111111111111',
      AWS_REGION: 'ap-southeast-2',
    });
    expect(env.region).toBe('ap-southeast-2');
  });

  it('prefers CDK_DEFAULT_REGION over AWS_REGION', () => {
    const env = resolveEnv({
      CDK_DEFAULT_ACCOUNT: '111111111111',
      CDK_DEFAULT_REGION: 'us-east-2',
      AWS_REGION: 'ap-southeast-2',
    });
    expect(env.region).toBe('us-east-2');
  });

  it('defaults the region to us-west-2 when neither region variable is set', () => {
    const env = resolveEnv({ CDK_DEFAULT_ACCOUNT: '111111111111' });
    expect(env.region).toBe('us-west-2');
  });
});
