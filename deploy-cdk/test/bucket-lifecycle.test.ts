// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';

/**
 * Threat model T-11, R-08: lifecycle rules on the buckets that hold customer
 * data, and the absence of rules that would destroy data the pipeline still
 * needs. The second half is the part that matters most: a whole-bucket
 * expiration on the processed bucket would delete Glue Catalog target tables,
 * and one on the generated-code bucket would delete job scripts.
 */
const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';

let json: Record<string, any>;

function rulesFor(bucketPrefix: string): any[] {
  const bucket = (Object.values(json.Resources) as any[]).find(
    (r) => r.Type === 'AWS::S3::Bucket' && r.Properties.BucketName === `${bucketPrefix}-${TEST_ACCOUNT}-${TEST_REGION}`,
  );
  expect(bucket, `${bucketPrefix} bucket`).toBeDefined();
  return bucket.Properties.LifecycleConfiguration?.Rules ?? [];
}

const wholeBucket = (rule: any) => !rule.Prefix && !rule.TagFilters;

beforeAll(() => {
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const stack = new FoundationStack(app, 'AgenticDataIngestion-Foundation-dev', {
    env: { account: TEST_ACCOUNT, region: TEST_REGION },
    projectName: 'agentic-data-ingestion',
    environment: 'dev',
  });
  json = Template.fromStack(stack).toJSON();
});

describe('R-08 — retention of customer data', () => {
  it('uploaded source files expire after 90 days, with noncurrent versions after 30', () => {
    const [rule] = rulesFor('adi-source-files');
    expect(rule).toMatchObject({
      Status: 'Enabled',
      ExpirationInDays: 90,
      NoncurrentVersionExpiration: { NoncurrentDays: 30 },
      AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
    });
    expect(wholeBucket(rule)).toBe(true);
  });

  it('the processed bucket never expires current objects bucket-wide (it holds Glue Catalog target data)', () => {
    const rules = rulesFor('adi-processed-files');
    for (const rule of rules.filter(wholeBucket)) {
      expect(rule.ExpirationInDays, 'a whole-bucket expiration would delete pipeline output').toBeUndefined();
      expect(rule.ExpirationDate).toBeUndefined();
    }
    expect(rules.find(wholeBucket)).toMatchObject({ NoncurrentVersionExpiration: { NoncurrentDays: 30 } });
  });

  it('only the processed bucket\'s dq-reports/ prefix expires', () => {
    const expiring = rulesFor('adi-processed-files').filter((r) => r.ExpirationInDays !== undefined);
    expect(expiring).toHaveLength(1);
    expect(expiring[0]).toMatchObject({ Prefix: 'dq-reports/', ExpirationInDays: 90 });
  });

  it('only the generated-code bucket\'s quarantine/ prefix expires (scripts and shared/ never do)', () => {
    const expiring = rulesFor('adi-generated-code').filter((r) => r.ExpirationInDays !== undefined);
    expect(expiring).toHaveLength(1);
    expect(expiring[0]).toMatchObject({ Prefix: 'quarantine/', ExpirationInDays: 90 });
  });

  it('target schemas, templates and dependencies never expire', () => {
    for (const bucket of ['adi-target-schemas', 'adi-templates', 'adi-dependencies']) {
      const expiring = rulesFor(bucket).filter((r) => r.ExpirationInDays !== undefined || r.ExpirationDate !== undefined);
      expect(expiring, bucket).toEqual([]);
    }
  });

  it('test outputs and checkpoints keep their short expirations and gain noncurrent-version cleanup', () => {
    expect(rulesFor('adi-test-outputs')[0]).toMatchObject({ ExpirationInDays: 30, NoncurrentVersionExpiration: { NoncurrentDays: 30 } });
    expect(rulesFor('adi-checkpoints')[0]).toMatchObject({ ExpirationInDays: 7, NoncurrentVersionExpiration: { NoncurrentDays: 30 } });
  });
});
