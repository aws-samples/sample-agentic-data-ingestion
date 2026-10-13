// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for TargetConfigDisplay
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 19: Target configuration display by type
 *
 * **Validates: Requirements 18.1, 18.2, 18.3, 18.4, 18.5**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import TargetConfigDisplay from '../TargetConfigDisplay';
import type { Pipeline, TargetConfig } from '../../types';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const nameArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789-_'.split('')),
  { minLength: 1, maxLength: 30 },
);

const newS3ConfigArb: fc.Arbitrary<TargetConfig> = fc.record({
  type: fc.constant('NEW_S3_TABLE_BUCKET' as const),
  bucket_name: nameArb,
});

const existingS3ConfigArb: fc.Arbitrary<TargetConfig> = fc.record({
  type: fc.constant('EXISTING_S3_TABLE_BUCKET' as const),
  bucket_name: nameArb,
  table_name: nameArb,
});

const glueConfigArb: fc.Arbitrary<TargetConfig> = fc.record({
  type: fc.constant('GLUE_CATALOG' as const),
  database: nameArb,
  table: nameArb,
  format: fc.constantFrom('parquet', 'csv', 'json', 'orc', 'avro'),
});

const jdbcConfigArb: fc.Arbitrary<TargetConfig> = fc.record({
  type: fc.constantFrom('JDBC' as const, 'RDS_DATABASE' as const),
  engine: fc.constantFrom('postgres', 'mysql', 'aurora-postgresql'),
  jdbc_url: fc.constant('jdbc:postgresql://host:5432/testdb'),
  jdbc_driver: fc.constant('org.postgresql.Driver'),
  jdbc_table: nameArb.map((t) => `public.${t}`),
  write_mode: fc.constantFrom('overwrite', 'append'),
  glue_connection_name: nameArb,
});

const unknownConfigArb: fc.Arbitrary<TargetConfig> = fc.record({
  type: fc.stringOf(
    fc.constantFrom(...'ABCDEFGHIJKLMNOPQRSTUVWXYZ_'.split('')),
    { minLength: 3, maxLength: 20 },
  ).filter(
    (t) =>
      t !== 'NEW_S3_TABLE_BUCKET' &&
      t !== 'EXISTING_S3_TABLE_BUCKET' &&
      t !== 'GLUE_CATALOG' &&
      t !== 'JDBC' &&
      t !== 'RDS_DATABASE',
  ),
});

function makePipeline(config: TargetConfig): Pipeline {
  return {
    pipeline_key: 'test-pipeline',
    pipeline_name: 'Test Pipeline',
    status: 'active',
    source_schema: 'source',
    target_schema: 'target',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-06-01T00:00:00Z',
    target_config: config,
  };
}

// ---------------------------------------------------------------------------
// Property 19: Target configuration display by type
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 19: Target configuration display by type', () => {
  /**
   * **Validates: Requirements 18.1, 18.2**
   *
   * For any Pipeline with target type NEW_S3_TABLE_BUCKET, the display
   * SHALL show the target type, bucket name, and "Created on first run".
   */
  it('renders NEW_S3_TABLE_BUCKET with bucket name and "created on first run" note', () => {
    fc.assert(
      fc.property(newS3ConfigArb, (config) => {
        cleanup();
        const { container } = render(
          <TargetConfigDisplay pipeline={makePipeline(config)} />,
        );

        const display = within(container).getByTestId('target-config-display');

        // Target type is shown
        const typeEl = within(display).getByTestId('target-type');
        expect(typeEl.textContent).toBe('NEW_S3_TABLE_BUCKET');

        // Bucket name is shown
        const bucketEl = within(display).getByTestId('target-bucket-name');
        expect(bucketEl.textContent).toBe(config.bucket_name);

        // "Created on first run" note
        const noteEl = within(display).getByTestId('target-created-note');
        expect(noteEl.textContent).toContain('Created on first run');

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 18.1, 18.3**
   *
   * For any Pipeline with target type EXISTING_S3_TABLE_BUCKET, the display
   * SHALL show the target type, bucket name, and table name.
   */
  it('renders EXISTING_S3_TABLE_BUCKET with bucket and table name', () => {
    fc.assert(
      fc.property(existingS3ConfigArb, (config) => {
        cleanup();
        const { container } = render(
          <TargetConfigDisplay pipeline={makePipeline(config)} />,
        );

        const display = within(container).getByTestId('target-config-display');

        const typeEl = within(display).getByTestId('target-type');
        expect(typeEl.textContent).toBe('EXISTING_S3_TABLE_BUCKET');

        const bucketEl = within(display).getByTestId('target-bucket-name');
        expect(bucketEl.textContent).toBe(config.bucket_name);

        const tableEl = within(display).getByTestId('target-table-name');
        expect(tableEl.textContent).toBe(config.table_name);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 18.1, 18.4**
   *
   * For any Pipeline with target type GLUE_CATALOG, the display SHALL show
   * the target type, database, table, and format.
   */
  it('renders GLUE_CATALOG with database, table, and format', () => {
    fc.assert(
      fc.property(glueConfigArb, (config) => {
        cleanup();
        const { container } = render(
          <TargetConfigDisplay pipeline={makePipeline(config)} />,
        );

        const display = within(container).getByTestId('target-config-display');

        const typeEl = within(display).getByTestId('target-type');
        expect(typeEl.textContent).toBe('GLUE_CATALOG');

        const dbEl = within(display).getByTestId('target-database');
        expect(dbEl.textContent).toBe(config.database);

        const tableEl = within(display).getByTestId('target-table');
        expect(tableEl.textContent).toBe(config.table);

        const formatEl = within(display).getByTestId('target-format');
        expect(formatEl.textContent).toBe(config.format);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * For any JDBC / RDS_DATABASE target, the display SHALL show the JDBC
   * details (table, write mode, glue connection, masked URL) — NOT the
   * generic "unknown" fallback.
   */
  it('renders JDBC / RDS_DATABASE target with connection details', () => {
    fc.assert(
      fc.property(jdbcConfigArb, (config) => {
        cleanup();
        const { container } = render(
          <TargetConfigDisplay pipeline={makePipeline(config)} />,
        );

        const display = within(container).getByTestId('target-config-display');

        const typeEl = within(display).getByTestId('target-type');
        expect(typeEl.textContent).toBe(config.type);

        // JDBC detail grid is rendered (NOT the unknown fallback).
        const jdbcEl = within(display).getByTestId('target-jdbc');
        expect(jdbcEl).toBeTruthy();
        expect(within(display).queryByTestId('target-unknown')).toBeNull();

        // Table and write mode surface the configured values.
        const tableEl = within(display).getByTestId('target-jdbc-table');
        expect(tableEl.textContent).toBe(config.jdbc_table);
        const writeModeEl = within(display).getByTestId('target-write-mode');
        expect(writeModeEl.textContent).toBe(config.write_mode);

        // JDBC URL is shown without inline credentials.
        const urlEl = within(display).getByTestId('target-jdbc-url');
        expect(urlEl.textContent).not.toContain('@');

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 18.1, 18.5**
   *
   * For any Pipeline with an unknown target type, the display SHALL show
   * the raw type string.
   */
  it('renders unknown target type as raw type string', () => {
    fc.assert(
      fc.property(unknownConfigArb, (config) => {
        cleanup();
        const { container } = render(
          <TargetConfigDisplay pipeline={makePipeline(config)} />,
        );

        const display = within(container).getByTestId('target-config-display');

        // Target type is shown
        const typeEl = within(display).getByTestId('target-type');
        expect(typeEl.textContent).toBe(config.type);

        // Unknown section is rendered with the raw type
        const unknownEl = within(display).getByTestId('target-unknown');
        expect(unknownEl.textContent).toContain(config.type);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
