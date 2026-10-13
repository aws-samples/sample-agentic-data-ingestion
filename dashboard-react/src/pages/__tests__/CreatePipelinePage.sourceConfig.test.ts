// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for `buildCreateSourceConfig` — the source_config transform
 * applied to the create/provision request body.
 *
 * Regression guard: the wizard stores the source database namespace + table
 * under the friendly keys `schema` / `table`, but the backend's
 * `_validate_jdbc_source_config` requires the canonical `jdbc_schema` /
 * `jdbc_table` keys. Before this transform existed, JDBC pipeline creation
 * failed server-side with HTTP 400 ("source_config.jdbc_schema is required")
 * even though Detect Schema (which aliases the keys) succeeded.
 */
import { describe, it, expect } from 'vitest';
import { buildCreateSourceConfig } from '../CreatePipelinePage';

describe('buildCreateSourceConfig', () => {
  it('aliases schema/table to jdbc_schema/jdbc_table for JDBC sources', () => {
    const cfg = {
      _connection_mode: 'select_rds',
      jdbc_url: 'jdbc:mysql://host:3306/appdb',
      jdbc_driver: 'com.mysql.cj.jdbc.Driver',
      secret_arn: 'arn:aws:secretsmanager:us-west-2:123456789012:secret:db-Abc',
      engine: 'mysql',
      read_mode: 'full_table',
      schema: 'appdb',
      table: 'customers',
    };

    const out = buildCreateSourceConfig('jdbc', cfg);

    expect(out.jdbc_schema).toBe('appdb');
    expect(out.jdbc_table).toBe('customers');
    // Canonical keys the backend requires must all survive the transform.
    expect(out.jdbc_url).toBe('jdbc:mysql://host:3306/appdb');
    expect(out.jdbc_driver).toBe('com.mysql.cj.jdbc.Driver');
    expect(out.secret_arn).toBe(
      'arn:aws:secretsmanager:us-west-2:123456789012:secret:db-Abc',
    );
    expect(out.engine).toBe('mysql');
    expect(out.read_mode).toBe('full_table');
    expect(out._connection_mode).toBe('select_rds');
    // The friendly keys are dropped in favour of the canonical aliases.
    expect(out.schema).toBeUndefined();
    expect(out.table).toBeUndefined();
  });

  it('defaults read_mode to full_table when the user never set it', () => {
    // The Read Mode toggle defaults to "Full table" for DISPLAY only — the
    // key is not persisted into state unless the user changes it. The
    // create payload must still carry read_mode or the backend rejects the
    // create with "source_config.read_mode is required".
    const out = buildCreateSourceConfig('jdbc', {
      _connection_mode: 'select_rds',
      jdbc_url: 'jdbc:mysql://host:3306/appdb',
      schema: 'appdb',
      table: 'customers',
    });
    expect(out.read_mode).toBe('full_table');
  });

  it('preserves an explicit incremental read_mode (does not overwrite)', () => {
    const out = buildCreateSourceConfig('jdbc', {
      read_mode: 'incremental',
      watermark_column: 'updated_at',
      watermark_type: 'timestamp',
      schema: 'appdb',
      table: 'customers',
    });
    expect(out.read_mode).toBe('incremental');
    expect(out.watermark_column).toBe('updated_at');
  });

  it('omits jdbc_schema/jdbc_table when the friendly keys are absent', () => {
    const out = buildCreateSourceConfig('jdbc', {
      _connection_mode: 'select_rds',
      jdbc_url: 'jdbc:mysql://host:3306/appdb',
    });
    expect(out.jdbc_schema).toBeUndefined();
    expect(out.jdbc_table).toBeUndefined();
    expect(out.jdbc_url).toBe('jdbc:mysql://host:3306/appdb');
  });

  it('preserves glue_connection mode keys while aliasing schema/table', () => {
    const out = buildCreateSourceConfig('jdbc', {
      _connection_mode: 'glue_connection',
      glue_connection_name: 'my-existing-conn',
      jdbc_url: 'jdbc:postgresql://host:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
      schema: 'public',
      table: 'orders',
    });
    expect(out.glue_connection_name).toBe('my-existing-conn');
    expect(out.jdbc_schema).toBe('public');
    expect(out.jdbc_table).toBe('orders');
  });

  it('passes S3_UPLOAD source config through unchanged (no aliasing)', () => {
    const cfg = { some_key: 'value', schema: 'ignored', table: 'ignored' };
    const out = buildCreateSourceConfig('s3_upload', cfg);
    expect(out).toEqual(cfg);
    // schema/table are NOT aliased for non-JDBC sources.
    expect(out.jdbc_schema).toBeUndefined();
    expect(out.jdbc_table).toBeUndefined();
  });

  it('returns a copy, not the original reference', () => {
    const cfg = { schema: 'appdb', table: 'customers' };
    const out = buildCreateSourceConfig('jdbc', cfg);
    expect(out).not.toBe(cfg);
  });
});
