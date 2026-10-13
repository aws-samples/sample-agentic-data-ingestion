// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { DataStack } from '../lib/data-stack';

/**
 * CDK assertion tests for the Processed_File_Ledger table (p30 R9.1, task 4.7).
 *
 * Each contract here fails at RUNTIME rather than at synth, which is why it is
 * pinned:
 *
 *  1. Table name and key schema. `shared/processed_file_ledger.py` does a keyed
 *     get on (pipeline_id, fingerprint). A different key schema makes every
 *     lookup a ValidationException at runtime — and because the reader FAILS
 *     OPEN by design, the visible symptom would be "dedup silently never
 *     works", not an error.
 *  2. TTL on `ttl`. Rows carry an epoch `ttl` from
 *     config.PROCESSED_FILE_LEDGER_TTL_SECONDS. Without the table-level TTL
 *     attribute configured, nothing expires and the ledger grows forever.
 *  3. NO stream. This is the reason the ledger is its own table rather than a
 *     run_id prefix on the executions table (design C8 states the opposite
 *     preference; the deviation is documented at the construct). The executions
 *     table's stream drives the WebSocket broadcast Lambda, so a ledger row
 *     there would fan out a dashboard event per successful run. If a stream is
 *     ever added here, that reasoning has silently been undone.
 *  4. Retention. Every table in data-stack.ts is RETAIN with a hardcoded
 *     physical name; C8's hazard is that renaming a construct ID makes
 *     CloudFormation try to create a duplicate physical table.
 *  5. The two consuming Lambdas are wired to the table name. Asserted at
 *     SOURCE level, following the convention in hosting-auth.test.ts and
 *     p28-idle-proxy.test.ts: synthesizing the full Compute/Orchestration
 *     stacks (20+ Lambdas with Code.fromAsset) exceeds vitest worker memory.
 *
 * Validates: Requirements 9.1
 */

const TABLE_NAME = 'agentic_data_ingestion_processed_files';

function synthDataStack(): Template {
  const app = new cdk.App();
  const stack = new DataStack(app, 'TestDataStack-P30Ledger', {
    env: { account: '123456789012', region: 'us-west-2' },
    environment: 'dev',
  });
  return Template.fromStack(stack);
}

describe('p30 R9.1 — DataStack: Processed_File_Ledger table', () => {
  let template: Template;

  beforeAll(() => {
    template = synthDataStack();
  });

  it('exists with the physical table name both Lambdas default to', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: TABLE_NAME,
    });
  });

  it('is keyed by pipeline_id (HASH) + fingerprint (RANGE), both strings', () => {
    // R9.1: "keyed by pipeline plus a content fingerprint". The pipeline is part
    // of the KEY, not a filter — the same content in two pipelines is two
    // legitimate ingestions.
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: TABLE_NAME,
      KeySchema: [
        { AttributeName: 'pipeline_id', KeyType: 'HASH' },
        { AttributeName: 'fingerprint', KeyType: 'RANGE' },
      ],
      AttributeDefinitions: Match.arrayWith([
        Match.objectLike({ AttributeName: 'pipeline_id', AttributeType: 'S' }),
        Match.objectLike({ AttributeName: 'fingerprint', AttributeType: 'S' }),
      ]),
    });
  });

  it('has TTL enabled on the `ttl` attribute (R9.4)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: TABLE_NAME,
      TimeToLiveSpecification: { AttributeName: 'ttl', Enabled: true },
    });
  });

  it('has NO DynamoDB stream — nothing should react to a ledger row appearing', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    const ledger = Object.values(tables).find(
      (t: any) => t.Properties?.TableName === TABLE_NAME
    ) as any;
    expect(ledger).toBeDefined();
    expect(ledger.Properties.StreamSpecification).toBeUndefined();
  });

  it('has NO secondary indexes — the only access pattern is a keyed get', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    const ledger = Object.values(tables).find(
      (t: any) => t.Properties?.TableName === TABLE_NAME
    ) as any;
    expect(ledger.Properties.GlobalSecondaryIndexes).toBeUndefined();
  });

  it('is RETAIN with PITR and SSE, like every other table in the stack', () => {
    const tables = template.findResources('AWS::DynamoDB::Table');
    const [logicalId, ledger] = Object.entries(tables).find(
      ([, t]: [string, any]) => t.Properties?.TableName === TABLE_NAME
    ) as [string, any];
    expect(ledger.DeletionPolicy).toBe('Retain');
    expect(ledger.Properties.BillingMode).toBe('PAY_PER_REQUEST');
    expect(ledger.Properties.PointInTimeRecoverySpecification).toEqual({
      PointInTimeRecoveryEnabled: true,
    });
    expect(ledger.Properties.SSESpecification).toEqual({ SSEEnabled: true });
    // C8: the construct ID is fixed forever. Renaming it makes CloudFormation
    // create a SECOND physical table with the same name and fail the deploy.
    expect(logicalId).toMatch(/^ProcessedFilesTable/);
  });
});

describe('p30 R9.1 — the ledger table reaches both consuming Lambdas', () => {
  const libDir = path.join(__dirname, '..', 'lib');
  const read = (f: string) => fs.readFileSync(path.join(libDir, f), 'utf8');

  it('job_completion (the WRITER) gets PROCESSED_FILES_TABLE', () => {
    // Written on the Glue SUCCEEDED callback — the only place terminal write
    // success is known.
    const src = read('compute-stack.ts');
    const block = src.slice(src.indexOf("this.jobCompletionLambda = new lambda.Function"));
    expect(block.slice(0, 2000)).toContain(
      'PROCESSED_FILES_TABLE: data.processedFilesTable.tableName'
    );
  });

  it('pipeline_trigger (the READER) gets PROCESSED_FILES_TABLE', () => {
    // Read before the pipeline lock is acquired, so a duplicate skip leaves no
    // lock to release (R10.3).
    const src = read('orchestration-stack.ts');
    expect(src).toContain('PROCESSED_FILES_TABLE: data.processedFilesTable.tableName');
  });

  it('the DataStack exposes the table for those two wirings', () => {
    expect(read('data-stack.ts')).toContain('public readonly processedFilesTable: dynamodb.Table');
  });
});
