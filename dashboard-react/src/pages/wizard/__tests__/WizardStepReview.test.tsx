// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  WizardStepReview,
  TARGET_TYPE_LABELS,
  SOURCE_TYPE_LABELS,
  maskSecret,
  maskJdbcUrl,
} from '../WizardStepReview';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

const FULL_STATE: WizardState = {
  pipelineName: 'test-pipeline',
  description: 'A test pipeline for unit testing',
  // P17 Task 16.1 — added to satisfy new required fields on WizardState (defaults match
  // INITIAL_WIZARD_STATE; existing assertions in this file are unaffected).
  sourceType: 's3_upload',
  sourceConfig: {},
  schedule: null,
  targetType: 'glue_catalog',
  targetConfig: { database_name: 'mydb', table_name: 'mytable' },
  detectedSchema: null,
  schemaSource: 'detected',
  schemaFields: [
    { name: 'id', type: 'bigint', nullable: false, description: 'Primary key' },
    { name: 'name', type: 'string', nullable: true, description: 'User name' },
  ],
  existingSchemaId: null,
  aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
  approvalConfig: {
    schemaMappingEnabled: true,
    schemaMappingConfidenceThreshold: 85,
    codeGenerationEnabled: false,
    alwaysApproveSchemaChanges: true,
  },
};

describe('WizardStepReview', () => {
  it('displays pipeline name', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('test-pipeline')).toBeInTheDocument();
  });

  it('displays description', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('A test pipeline for unit testing')).toBeInTheDocument();
  });

  it('displays target type', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    // Matches the tile label in `WizardStepTarget.tsx` — Review shows the user
    // the type they picked, so the two must not disagree.
    expect(screen.getByText('Glue Catalog (existing table)')).toBeInTheDocument();
  });

  it('displays target config values', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('mydb')).toBeInTheDocument();
    expect(screen.getByText('mytable')).toBeInTheDocument();
  });

  it('displays schema field count', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('2 fields')).toBeInTheDocument();
  });

  it('displays schema fields in table', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('id')).toBeInTheDocument();
    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByText('bigint')).toBeInTheDocument();
  });

  it('displays AI model', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('anthropic.claude-3-haiku-20240307-v1:0')).toBeInTheDocument();
  });

  it('displays approval config', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('Required')).toBeInTheDocument(); // schema mapping
    expect(screen.getByText('Not required')).toBeInTheDocument(); // code generation
    expect(screen.getByText('85%')).toBeInTheDocument(); // confidence threshold
  });

  it('displays estimated cost section', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('Estimated Cost')).toBeInTheDocument();
  });

  it('handles empty state gracefully', () => {
    render(<WizardStepReview state={INITIAL_WIZARD_STATE} />);
    // Should show dashes for empty values
    const dashes = screen.getAllByText('—');
    expect(dashes.length).toBeGreaterThan(0);
  });
});

// P17 Task 20.5 — Validates: Requirement 18.1, 18.3.
describe('WizardStepReview Source section', () => {
  it('renders the Source header', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('Source')).toBeInTheDocument();
  });

  it('displays the s3_upload source-type label', () => {
    render(<WizardStepReview state={FULL_STATE} />);
    expect(screen.getByText('S3 Upload')).toBeInTheDocument();
  });

  it('displays the JDBC source-type label and engine for jdbc sources', () => {
    const jdbcState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        engine: 'postgres',
        jdbc_url: 'jdbc:postgresql://host:5432/mydb',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'full_table',
      },
      schedule: 'rate(1 hour)',
    };
    render(<WizardStepReview state={jdbcState} />);
    expect(screen.getByText('JDBC Database')).toBeInTheDocument();
    expect(screen.getByText('postgres')).toBeInTheDocument();
    expect(
      screen.getByText('jdbc:postgresql://host:5432/mydb'),
    ).toBeInTheDocument();
    expect(screen.getByText('public')).toBeInTheDocument();
    expect(screen.getByText('orders')).toBeInTheDocument();
    expect(screen.getByText('Full table')).toBeInTheDocument();
    expect(screen.getByText('rate(1 hour)')).toBeInTheDocument();
  });

  it('displays watermark fields for incremental JDBC sources', () => {
    const incrementalState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        engine: 'postgres',
        jdbc_url: 'jdbc:postgresql://host:5432/mydb',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'incremental',
        watermark_column: 'updated_at',
        watermark_type: 'timestamp',
        watermark_initial_value: '2024-01-01T00:00:00Z',
      },
      schedule: 'cron(0 2 * * ? *)',
    };
    render(<WizardStepReview state={incrementalState} />);
    expect(screen.getByText('Incremental')).toBeInTheDocument();
    expect(screen.getByText('updated_at')).toBeInTheDocument();
    expect(screen.getByText('timestamp')).toBeInTheDocument();
    expect(screen.getByText('2024-01-01T00:00:00Z')).toBeInTheDocument();
    expect(screen.getByText('cron(0 2 * * ? *)')).toBeInTheDocument();
  });

  it('hides watermark fields for full_table read mode', () => {
    const fullTableState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        engine: 'postgres',
        jdbc_url: 'jdbc:postgresql://host:5432/mydb',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'full_table',
        // Stale watermark values that should NOT be shown
        watermark_column: 'updated_at',
      },
      schedule: 'rate(1 hour)',
    };
    render(<WizardStepReview state={fullTableState} />);
    expect(screen.queryByText('Watermark Column')).not.toBeInTheDocument();
    expect(screen.queryByText('updated_at')).not.toBeInTheDocument();
  });

  it('masks the secret_arn in the Source section', () => {
    const jdbcState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        engine: 'postgres',
        jdbc_url: 'jdbc:postgresql://host:5432/mydb',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'full_table',
        secret_arn:
          'arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/mydb-AbCdEf',
      },
      schedule: 'rate(1 hour)',
    };
    render(<WizardStepReview state={jdbcState} />);
    // The full ARN must NOT appear; the masked variant must.
    expect(
      screen.queryByText(
        'arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/mydb-AbCdEf',
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByText('****CdEf')).toBeInTheDocument();
  });

  it('masks credentials embedded in a JDBC URL', () => {
    const jdbcState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        jdbc_url: 'jdbc:postgresql://user:secret@host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'full_table',
      },
      schedule: 'rate(1 hour)',
    };
    render(<WizardStepReview state={jdbcState} />);
    expect(
      screen.queryByText('jdbc:postgresql://user:secret@host:5432/db'),
    ).not.toBeInTheDocument();
    expect(screen.getByText('jdbc:postgresql://host:5432/db')).toBeInTheDocument();
  });

  it('truncates a long Initial Watermark to 30 chars', () => {
    const long = 'A'.repeat(60);
    const jdbcState: WizardState = {
      ...FULL_STATE,
      sourceType: 'jdbc',
      sourceConfig: {
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'orders',
        read_mode: 'incremental',
        watermark_column: 'wm',
        watermark_type: 'numeric',
        watermark_initial_value: long,
      },
      schedule: 'rate(1 hour)',
    };
    render(<WizardStepReview state={jdbcState} />);
    // 30-char truncation: 29 'A' chars + '…'
    expect(screen.getByText(`${'A'.repeat(29)}…`)).toBeInTheDocument();
    expect(screen.queryByText(long)).not.toBeInTheDocument();
  });
});

// P17 Task 20.5 — Validates: Requirements 14.7, 18.2, 18.3.
describe('TARGET_TYPE_LABELS / SOURCE_TYPE_LABELS', () => {
  it('TARGET_TYPE_LABELS contains the consolidated jdbc label', () => {
    expect(TARGET_TYPE_LABELS.jdbc).toBe(
      'JDBC Database (RDS, Aurora, or self-managed)',
    );
  });

  it('TARGET_TYPE_LABELS no longer carries the rds_database row', () => {
    // The temporary placeholder row added in Task 16.3 was removed in
    // Task 20.3 once `'rds_database'` was dropped from WizardState.
    expect(Object.keys(TARGET_TYPE_LABELS)).not.toContain('rds_database');
  });

  it('SOURCE_TYPE_LABELS contains both s3_upload and jdbc entries', () => {
    expect(SOURCE_TYPE_LABELS.s3_upload).toBe('S3 Upload');
    expect(SOURCE_TYPE_LABELS.jdbc).toBe('JDBC Database');
  });
});

describe('maskSecret', () => {
  it('returns "—" for empty/undefined input', () => {
    expect(maskSecret(undefined)).toBe('—');
    expect(maskSecret(null)).toBe('—');
    expect(maskSecret('')).toBe('—');
  });

  it('returns "****" for short strings (<=4 chars)', () => {
    expect(maskSecret('abc')).toBe('****');
    expect(maskSecret('abcd')).toBe('****');
  });

  it('preserves the last 4 characters', () => {
    expect(
      maskSecret(
        'arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/mydb-AbCdEf',
      ),
    ).toBe('****CdEf');
    expect(maskSecret('1234567890')).toBe('****7890');
  });
});

describe('maskJdbcUrl', () => {
  it('returns "—" for empty input', () => {
    expect(maskJdbcUrl(undefined)).toBe('—');
    expect(maskJdbcUrl(null)).toBe('—');
    expect(maskJdbcUrl('')).toBe('—');
  });

  it('strips inline credentials from a JDBC URL', () => {
    expect(maskJdbcUrl('jdbc:postgresql://user:secret@host:5432/db')).toBe(
      'jdbc:postgresql://host:5432/db',
    );
    expect(maskJdbcUrl('jdbc:mysql://admin:p%40ss@host:3306/db')).toBe(
      'jdbc:mysql://host:3306/db',
    );
  });

  it('returns the URL unchanged when no credentials are embedded', () => {
    expect(maskJdbcUrl('jdbc:postgresql://host:5432/db')).toBe(
      'jdbc:postgresql://host:5432/db',
    );
  });
});
