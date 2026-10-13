// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for the `migrateLegacyTargetConfig` utility (P17 Task 20.4).
 *
 * Validates: P17 Requirements 14.3, 14.4, 17.7, 22.1.
 *
 * Persisted pipelines whose `target_config.type == "RDS_DATABASE"` (the
 * legacy P14 enum) must be hydrated into the wizard as `'jdbc'` with the
 * persisted `target_config.type` normalised to `'JDBC'` so the next save
 * writes the unified shape. New `JDBC` records flow through identically.
 */
import { describe, it, expect } from 'vitest';
import { migrateLegacyTargetConfig } from '../CreatePipelinePage';

describe('migrateLegacyTargetConfig', () => {
  it('migrates a legacy RDS_DATABASE pipeline to jdbc + JDBC type', () => {
    const persisted = {
      type: 'RDS_DATABASE',
      jdbc_url: 'jdbc:postgresql://host:5432/mydb',
      jdbc_driver: 'org.postgresql.Driver',
      jdbc_table: 'mydb.customers',
      database_name: 'mydb',
      table_name: 'customers',
      secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:agentic-data-ingestion/x-AbCd',
      engine: 'postgres',
    };
    const result = migrateLegacyTargetConfig(persisted);
    expect(result.targetType).toBe('jdbc');
    expect(result.targetConfig.type).toBe('JDBC');
    expect(result.targetConfig.jdbc_url).toBe('jdbc:postgresql://host:5432/mydb');
    expect(result.targetConfig.jdbc_table).toBe('mydb.customers');
    expect(result.targetConfig.engine).toBe('postgres');
  });

  it('passes through a current JDBC pipeline unchanged', () => {
    const persisted = {
      type: 'JDBC',
      jdbc_url: 'jdbc:mysql://host:3306/shop',
      jdbc_driver: 'com.mysql.cj.jdbc.Driver',
      jdbc_table: 'shop.orders',
    };
    const result = migrateLegacyTargetConfig(persisted);
    expect(result.targetType).toBe('jdbc');
    expect(result.targetConfig.type).toBe('JDBC');
    expect(result.targetConfig.jdbc_url).toBe('jdbc:mysql://host:3306/shop');
  });

  it('maps GLUE_CATALOG to glue_catalog', () => {
    const persisted = { type: 'GLUE_CATALOG', database: 'mydb', table: 'orders' };
    const result = migrateLegacyTargetConfig(persisted);
    expect(result.targetType).toBe('glue_catalog');
    expect(result.targetConfig.type).toBe('GLUE_CATALOG');
  });

  it('maps NEW_S3_TABLE_BUCKET and EXISTING_S3_TABLE_BUCKET to s3_tables', () => {
    expect(
      migrateLegacyTargetConfig({ type: 'NEW_S3_TABLE_BUCKET' }).targetType,
    ).toBe('s3_tables');
    expect(
      migrateLegacyTargetConfig({ type: 'EXISTING_S3_TABLE_BUCKET' }).targetType,
    ).toBe('s3_tables');
  });

  it('maps S3_PARQUET to s3_parquet', () => {
    const result = migrateLegacyTargetConfig({
      type: 'S3_PARQUET',
      s3_path: 's3://bucket/path/',
    });
    expect(result.targetType).toBe('s3_parquet');
    expect(result.targetConfig.s3_path).toBe('s3://bucket/path/');
  });

  it('falls back to s3_tables for unknown / missing types', () => {
    expect(migrateLegacyTargetConfig({}).targetType).toBe('s3_tables');
    expect(
      migrateLegacyTargetConfig({ type: 'something_else' }).targetType,
    ).toBe('s3_tables');
  });

  it('coerces non-string values to strings (DDB sometimes returns numbers)', () => {
    const result = migrateLegacyTargetConfig({
      type: 'JDBC',
      jdbc_url: 'jdbc:postgresql://host:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
      // simulate a numeric port persisted on an older record
      _port: 5432,
    });
    expect(result.targetConfig._port).toBe('5432');
  });

  it('drops null / undefined keys', () => {
    const result = migrateLegacyTargetConfig({
      type: 'JDBC',
      jdbc_url: 'jdbc:postgresql://host:5432/db',
      secret_arn: null,
      glue_connection_name: undefined,
    });
    expect(result.targetConfig.secret_arn).toBeUndefined();
    expect(result.targetConfig.glue_connection_name).toBeUndefined();
  });

  it('handles case-insensitive type values', () => {
    const result = migrateLegacyTargetConfig({ type: 'jdbc' });
    expect(result.targetType).toBe('jdbc');
    expect(result.targetConfig.type).toBe('JDBC');
  });
});
