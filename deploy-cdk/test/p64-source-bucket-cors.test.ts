// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * The source bucket's CORS block, pinned on the synthesized template — p64 R4.7.
 *
 * WHY THIS EXISTS
 * ---------------
 * R4.7's "nothing pins it" means **no test asserts it**, not that CDK omits it.
 * CDK does declare the block, at `foundation-stack.ts:73-84`, and the deployed
 * config in the validation account matches that declaration field for field
 * (p64 task 0.2). What was missing was any guard: `CorsConfiguration` — the
 * CloudFormation property name an S3 CORS assertion has to read — appeared in
 * ZERO files under `deploy-cdk/test/` or `tests/cdk/` before this one, so the
 * block could be edited or deleted with every test still green.
 *
 * The block is no longer bookkeeping, and that is what earns this file its
 * place. p64 task 1d.5 (R4.4) made upload success conditional on a
 * **client-side `fetch(..., { method: 'HEAD' })`** from the browser straight at
 * the source bucket — `FileUploadModal.tsx:153-157`, via `confirmObjectExists`.
 * That request is cross-origin and it works ONLY because this CORS block allows
 * `HEAD` from `*`. Drop `HEAD` here and the confirmation fetch fails preflight,
 * so every upload reports an error — the browser's CORS failure is
 * indistinguishable, to that code, from a missing object. Nothing in the React
 * tree can catch that: the frontend tests stub `fetch`, so they pass against a
 * bucket that would reject the real request.
 *
 * The one CORS-shaped test that already existed,
 * `p47-cors-posture.test.ts:283-370`, asserts CloudFront's
 * `ResponseHeadersPolicy` `CorsConfig` on `/api/*` — a different resource in a
 * different stack. It would not notice this block disappearing.
 *
 * WHY THE SYNTHESIZED TEMPLATE
 * ----------------------------
 * `LGR-1166391-1` settled this for CDK-shape guards: assert on what
 * CloudFormation is actually handed, because a source-text scan passes on input
 * that still fails at deploy. The same record notes the Python `tests/cdk/`
 * suite cannot render a template, which is why this is vitest — and the CORS
 * and source-bucket precedent both live in this suite anyway.
 *
 * WHAT IS DELIBERATELY *NOT* PINNED
 * ---------------------------------
 * `AllowedHeaders`, `MaxAge`, and the exact `AllowedMethods` set. R4.7 requires
 * no change to the CORS posture, so this file pins the fields something else
 * depends on — `HEAD` (the confirmation fetch), `POST` (the presigned-POST
 * upload itself), the wildcard origin, and `ETag` in `ExposedHeaders` — and
 * leaves the rest free to move. Pinning `AllowedMethods` exactly would turn a
 * harmless widening into a test failure.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';
const SOURCE_BUCKET_NAME = `adi-source-files-${TEST_ACCOUNT}-${TEST_REGION}`;

type CorsRule = {
  AllowedHeaders?: string[];
  AllowedMethods?: string[];
  AllowedOrigins?: string[];
  ExposedHeaders?: string[];
  MaxAge?: number;
};

describe('FoundationStack — source bucket CORS (p64 R4.7)', () => {
  let corsRules: CorsRule[];

  beforeAll(() => {
    const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
    const stack = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}-P64`, {
      env: { account: TEST_ACCOUNT, region: TEST_REGION },
      projectName: 'agentic-data-ingestion',
      environment: TEST_ENV,
    });
    const json = Template.fromStack(stack).toJSON();

    // Located by physical bucket name, the way foundation-iam.test.ts:319-322
    // already does it: the logical id carries a CDK-generated suffix, so a
    // hardcoded logical id would silently stop matching.
    const hits = (Object.entries(json.Resources ?? {}) as [string, any][]).filter(
      ([, r]) => r.Type === 'AWS::S3::Bucket' && r.Properties?.BucketName === SOURCE_BUCKET_NAME,
    );
    expect(hits.length, `expected exactly one AWS::S3::Bucket named ${SOURCE_BUCKET_NAME}`).toBe(1);

    corsRules = hits[0][1].Properties?.CorsConfiguration?.CorsRules ?? [];
  });

  // ── The anti-vacuity guard, asserted first ──────────────────────────
  //
  // Every assertion below reads `corsRules`. If the CORS block is deleted the
  // synthesized bucket carries no `CorsConfiguration` at all, `corsRules` is
  // `[]`, and a `.some()`-shaped assertion over an empty array would pass
  // silently. This is the assertion that fails in that case.

  it('the source bucket carries a CORS configuration at all (R4.7)', () => {
    expect(
      corsRules.length,
      `${SOURCE_BUCKET_NAME} must declare CorsConfiguration.CorsRules — without it the browser ` +
        'cannot POST an upload to this bucket and cannot HEAD it to confirm the object landed',
    ).toBeGreaterThan(0);
  });

  // ── The four fields something else depends on ───────────────────────

  it('allows HEAD, which is what makes the R4.4 upload confirmation work', () => {
    // `confirmObjectExists` in FileUploadModal.tsx:153-157 issues a
    // cross-origin HEAD at this bucket after the 2xx. No HEAD here means that
    // fetch fails preflight and every upload reports failure.
    const methods = corsRules.flatMap((r) => r.AllowedMethods ?? []);
    expect(
      methods,
      'HEAD is load-bearing: the post-upload confirmation fetch (R4.4) is a browser HEAD',
    ).toContain('HEAD');
  });

  it('allows POST, which is the presigned-POST upload itself', () => {
    const methods = corsRules.flatMap((r) => r.AllowedMethods ?? []);
    expect(
      methods,
      'the dashboard uploads via presigned POST — see handlers/upload.py presign_upload',
    ).toContain('POST');
  });

  it('allows the wildcard origin, so both localhost and CloudFront reach it', () => {
    // Unlike the API path, this one is genuinely cross-origin in every
    // deployment: the browser talks to `<bucket>.s3.amazonaws.com` directly,
    // never through the distribution that served the SPA.
    const origins = corsRules.flatMap((r) => r.AllowedOrigins ?? []);
    expect(origins, 'the S3 upload/confirm path is cross-origin under every deployment').toContain(
      '*',
    );
  });

  it('exposes ETag to the browser', () => {
    // The upload response's ETag is only readable by client JS if S3 says so;
    // without this the header is present on the wire and invisible in the page.
    const exposed = corsRules.flatMap((r) => r.ExposedHeaders ?? []);
    expect(exposed, 'ETag must be readable by the browser after an upload').toContain('ETag');
  });
});
