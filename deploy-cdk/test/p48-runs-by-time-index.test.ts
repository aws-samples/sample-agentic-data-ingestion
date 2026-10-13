// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { DataStack } from '../lib/data-stack';

/**
 * CDK assertion tests for RunsByTimeIndex on the executions table
 * (p48 R5.2 / R5.3, task 2.1).
 *
 * Why each contract here is pinned rather than left to review:
 *
 *  1. The index exists, keyed (pipeline_id, started_at). This is the whole
 *     mechanism p48 C1 adopts. Without it the run-history read has no
 *     chronological sort key available and orders by `run_id` — under which
 *     three id prefixes share the key (`queued-` < `run-` < `skipped-`), so a
 *     descending sort groups BY KIND. A wrong key schema here does not fail at
 *     synth and does not fail loudly at runtime: it re-orders a list a human
 *     reads.
 *
 *  2. ProjectionType.ALL. Required, not preferred — the read consumes eight
 *     attributes (status, file_name, source_key, run_id, schema_hash,
 *     pending_approval_id, pending_approval_type, current_step). A narrower
 *     projection still satisfies every query, silently, by forcing a second
 *     read per row — reintroducing the N+1 the enrichment design avoids. That
 *     is a performance regression no functional test can see, which is why it
 *     is asserted at the template.
 *
 *  3. The other two indexes are untouched. StatusIndex (status, started_at)
 *     backs the metrics cost fallback; run_id-index (PK-only) is cancel_run's
 *     recovery path and was "the last remaining dashboard scan" (P28 R17.2).
 *     DynamoDB permits only ONE GSI creation per table per stack update, so a
 *     change that altered either alongside this one would fail the deploy —
 *     after the diff had already been read and approved.
 *
 *  4. Exactly three GSIs, and no other executions-table change. The count is
 *     the machine-checkable form of the deploy-note fence in data-stack.ts: a
 *     fourth index added here would be a deploy that cannot succeed.
 *
 *  5. The base key schema is unchanged. p48 R5.1/R5.3: adding a GSI must write
 *     nothing and must not touch `run_id`. Changing a key attribute would
 *     orphan every existing row until its TTL expired.
 *
 * Validates: Requirements 5.2, 5.3
 */

const TABLE_NAME = 'agentic_data_ingestion_executions';
const INDEX_NAME = 'RunsByTimeIndex';

function synthDataStack(): Template {
  const app = new cdk.App();
  const stack = new DataStack(app, 'TestDataStack-P48RunsByTime', {
    env: { account: '123456789012', region: 'us-west-2' },
    environment: 'dev',
  });
  return Template.fromStack(stack);
}

function executionsTable(template: Template): any {
  const tables = template.findResources('AWS::DynamoDB::Table');
  const table = Object.values(tables).find(
    (t: any) => t.Properties?.TableName === TABLE_NAME
  ) as any;
  expect(table).toBeDefined();
  return table;
}

function gsiByName(template: Template, name: string): any {
  const indexes = executionsTable(template).Properties.GlobalSecondaryIndexes ?? [];
  return indexes.find((i: any) => i.IndexName === name);
}

describe('p48 R5.2 — executions table: RunsByTimeIndex', () => {
  let template: Template;

  beforeAll(() => {
    template = synthDataStack();
  });

  it('is defined on the executions table', () => {
    expect(gsiByName(template, INDEX_NAME)).toBeDefined();
  });

  it('is keyed by pipeline_id (HASH) + started_at (RANGE)', () => {
    // The partition key is the pipeline because run history is always read for
    // one pipeline; the sort key is started_at because a GSI sorts on exactly
    // one attribute, so the fallback chain an in-memory sort could have
    // expressed is not available (R1.4).
    expect(gsiByName(template, INDEX_NAME).KeySchema).toEqual([
      { AttributeName: 'pipeline_id', KeyType: 'HASH' },
      { AttributeName: 'started_at', KeyType: 'RANGE' },
    ]);
  });

  it('projects ALL attributes — a narrower projection is an N+1, not an error', () => {
    expect(gsiByName(template, INDEX_NAME).Projection).toEqual({ ProjectionType: 'ALL' });
  });

  it('declares both key attributes as STRING', () => {
    // ISO-8601 strings sort lexicographically in chronological order, which is
    // what makes ScanIndexForward=False mean "newest first". A non-STRING
    // started_at would break that silently.
    // Asserted by lookup rather than Match.arrayWith, which is order-sensitive
    // and would couple this test to the position CDK happens to emit.
    const defs: Array<{ AttributeName: string; AttributeType: string }> =
      executionsTable(template).Properties.AttributeDefinitions;
    const typeOf = (name: string) => defs.find((d) => d.AttributeName === name)?.AttributeType;
    expect(typeOf('pipeline_id')).toBe('S');
    expect(typeOf('started_at')).toBe('S');
  });
});

describe('p48 R5.3 — adding the index disturbs nothing else', () => {
  let template: Template;

  beforeAll(() => {
    template = synthDataStack();
  });

  it('leaves the base key schema at (pipeline_id, run_id)', () => {
    // R5.1: the three run_id shapes are not rewritten and the key does not
    // move. Changing a key attribute orphans every existing row until TTL.
    expect(executionsTable(template).Properties.KeySchema).toEqual([
      { AttributeName: 'pipeline_id', KeyType: 'HASH' },
      { AttributeName: 'run_id', KeyType: 'RANGE' },
    ]);
  });

  it('leaves StatusIndex at (status, started_at) with ALL', () => {
    const status = gsiByName(template, 'StatusIndex');
    expect(status).toBeDefined();
    expect(status.KeySchema).toEqual([
      { AttributeName: 'status', KeyType: 'HASH' },
      { AttributeName: 'started_at', KeyType: 'RANGE' },
    ]);
    expect(status.Projection).toEqual({ ProjectionType: 'ALL' });
  });

  it('leaves run_id-index PK-only with ALL', () => {
    const runId = gsiByName(template, 'run_id-index');
    expect(runId).toBeDefined();
    expect(runId.KeySchema).toEqual([{ AttributeName: 'run_id', KeyType: 'HASH' }]);
    expect(runId.Projection).toEqual({ ProjectionType: 'ALL' });
  });

  it('carries exactly three GSIs — the deploy-note fence, machine-checked', () => {
    // DynamoDB allows only ONE GSI creation per table per stack update. A
    // fourth index appearing here is a deploy that cannot succeed, so the
    // count is pinned rather than left to the in-code comment alone.
    const indexes = executionsTable(template).Properties.GlobalSecondaryIndexes;
    expect(indexes.map((i: any) => i.IndexName).sort()).toEqual([
      'RunsByTimeIndex',
      'StatusIndex',
      'run_id-index',
    ]);
  });

  it('keeps the NEW_IMAGE stream and the ttl attribute', () => {
    // The stream drives the WebSocket broadcast Lambda; the TTL is what ages
    // rows out. Neither is affected by an index add, and both are asserted so
    // an incidental edit to this block cannot remove them quietly.
    const props = executionsTable(template).Properties;
    expect(props.StreamSpecification).toEqual({ StreamViewType: 'NEW_IMAGE' });
    expect(props.TimeToLiveSpecification).toEqual({ AttributeName: 'ttl', Enabled: true });
  });
});
