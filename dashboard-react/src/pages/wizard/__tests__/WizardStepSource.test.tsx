// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for WizardStepSource (P17 Task 19.8).
 *
 * Coverage:
 *  - Source-type Tiles selector branches (s3_upload Alert vs jdbc form)
 *  - Schedule cron(...) / rate(...) regex validation (P17 Req 5.2)
 *  - Identifier validation for Schema, Table, and Watermark Column
 *    (P17 Req 3.5, 3.6)
 *  - Schema-detection happy path (RUNNING → READY persists schema +
 *    detected_glue_* fields)
 *  - Schema-detection failure path (FAILED → error displayed, no schema)
 *  - validateScheduleExpression / validateIdentifier exported helpers
 *
 * Validates: P17 Requirements 1.1-1.6, 2.1, 3.5, 3.6, 4.1, 4.6, 4.7,
 *            5.1, 5.2, 22.1.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  WizardStepSource,
  validateScheduleExpression,
  validateIdentifier,
  EB_CRON_RE,
  EB_RATE_RE,
  IDENTIFIER_RE,
} from '../WizardStepSource';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

// ─── Mocks ───────────────────────────────────────────────────────

// JdbcConnectionSelector hooks — flat data, no loading.
// We mock the entire `../../../hooks` barrel so the imports inside both
// JdbcConnectionSelector AND WizardStepSource (which both pull from
// '../../hooks') resolve to the same mocked module.
const mockMutateFn = vi.fn();
let mockStartHookValue: any = {
  mutate: mockMutateFn,
  isPending: false,
};
let mockStatusHookValue: any = {
  data: undefined,
  isError: false,
  error: null,
};

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useJdbcDetectSchemaStart: () => mockStartHookValue,
    useJdbcDetectSchemaStatus: (_crawlerName: string | null | undefined) =>
      mockStatusHookValue,
  };
});

// ─── Stateful test harness ─────────────────────────────────────

/**
 * Wraps WizardStepSource in a stateful container so tests can drive the
 * component through real onChange transitions (the WizardStepTarget test
 * pattern — component re-renders with the updated state on each onChange).
 */
function StatefulHarness({
  initial,
  onState,
}: {
  initial: Partial<WizardState>;
  onState?: (s: WizardState) => void;
}) {
  const [state, setState] = useState<WizardState>({
    ...INITIAL_WIZARD_STATE,
    ...initial,
  });
  return (
    <WizardStepSource
      state={state}
      onChange={(next) => {
        setState(next);
        onState?.(next);
      }}
    />
  );
}

function renderHarness(initial: Partial<WizardState> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onState = vi.fn();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <StatefulHarness initial={initial} onState={onState} />
    </QueryClientProvider>,
  );
  return { onState, ...utils };
}

/** Cloudscape Button forwards `data-testid` to the rendered <button>; this is a
 *  defensive helper so the same code works regardless of which element carries the id. */
function detectButton(): HTMLButtonElement {
  const node = screen.getByTestId('jdbc-source-detect-schema-button');
  return (node.tagName.toLowerCase() === 'button'
    ? (node as HTMLButtonElement)
    : (node.querySelector('button') as HTMLButtonElement));
}

// ─── Default mock state ─────────────────────────────────────────

beforeEach(() => {
  mockMutateFn.mockReset();
  mockStartHookValue = {
    mutate: mockMutateFn,
    isPending: false,
  };
  mockStatusHookValue = {
    data: undefined,
    isError: false,
    error: null,
  };
});

afterEach(() => {
  vi.useRealTimers();
});

// ─── Source-type branches (P17 Req 1.1-1.6) ────────────────────

describe('WizardStepSource — source-type branches', () => {
  it('renders the source-type Tiles selector with both options', () => {
    renderHarness();
    expect(screen.getByText('Source Type')).toBeInTheDocument();
    expect(screen.getByText('S3 Upload')).toBeInTheDocument();
    expect(screen.getByText('JDBC Database')).toBeInTheDocument();
  });

  it('shows the S3-upload Alert and no JDBC form when sourceType is s3_upload', () => {
    renderHarness({ sourceType: 's3_upload' });
    expect(screen.getByTestId('s3-upload-alert')).toBeInTheDocument();
    expect(screen.queryByTestId('jdbc-source-schema')).not.toBeInTheDocument();
  });

  it('shows the JDBC form (connection selector + schema/table + read-mode + schedule) when sourceType is jdbc', () => {
    renderHarness({ sourceType: 'jdbc' });
    expect(screen.queryByTestId('s3-upload-alert')).not.toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-conn-mode-tiles')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-schema')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-table')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-read-mode')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-schedule')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-schedule-preset-hourly')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-schedule-preset-daily')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-schedule-preset-weekly')).toBeInTheDocument();
  });

  it('hides watermark fields in full_table read mode', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: { read_mode: 'full_table' },
    });
    expect(screen.queryByTestId('jdbc-source-watermark-column')).not.toBeInTheDocument();
  });

  it('shows watermark fields in incremental read mode', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: { read_mode: 'incremental' },
    });
    expect(screen.getByTestId('jdbc-source-watermark-column')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-watermark-type')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-source-watermark-initial')).toBeInTheDocument();
  });
});

// ─── Schedule validation (P17 Req 5.2) ─────────────────────────

describe('WizardStepSource — schedule validation', () => {
  it('EB_CRON_RE accepts 6-field cron expressions', () => {
    expect(EB_CRON_RE.test('cron(0 2 * * ? *)')).toBe(true);
    expect(EB_CRON_RE.test('cron(0 2 ? * MON *)')).toBe(true);
    expect(EB_CRON_RE.test('cron( 0 2 * * ? * )')).toBe(true);
  });

  it('EB_CRON_RE rejects 5-field crontabs and missing parens', () => {
    expect(EB_CRON_RE.test('cron(0 2 * * ?)')).toBe(false); // 5 fields
    expect(EB_CRON_RE.test('0 2 * * ? *')).toBe(false); // missing wrapper
    expect(EB_CRON_RE.test('cron 0 2 * * ? *')).toBe(false); // no parens
  });

  it('EB_RATE_RE accepts rate expressions with valid units', () => {
    expect(EB_RATE_RE.test('rate(1 hour)')).toBe(true);
    expect(EB_RATE_RE.test('rate(5 minutes)')).toBe(true);
    expect(EB_RATE_RE.test('rate(2 days)')).toBe(true);
    expect(EB_RATE_RE.test('rate( 30 minute )')).toBe(true);
  });

  it('EB_RATE_RE rejects invalid units and bad shapes', () => {
    expect(EB_RATE_RE.test('rate(1 second)')).toBe(false);
    expect(EB_RATE_RE.test('rate(week)')).toBe(false);
    expect(EB_RATE_RE.test('1 hour')).toBe(false);
  });

  it('validateScheduleExpression accepts both cron and rate', () => {
    expect(validateScheduleExpression('rate(1 hour)')).toBeUndefined();
    expect(validateScheduleExpression('cron(0 2 * * ? *)')).toBeUndefined();
  });

  it('validateScheduleExpression returns an error for empty / invalid input', () => {
    expect(validateScheduleExpression('')).toBe('Schedule is required for JDBC sources');
    expect(validateScheduleExpression('every hour')).toMatch(/EventBridge/);
    expect(validateScheduleExpression('cron(0 2 * * ?)')).toMatch(/EventBridge/);
  });

  it('renders an inline error in the Schedule FormField when the expression is invalid', () => {
    renderHarness({
      sourceType: 'jdbc',
      schedule: 'every hour',
    });
    expect(
      screen.getByText(/Must be an EventBridge cron\(\.\.\.\) or rate\(\.\.\.\) expression/),
    ).toBeInTheDocument();
  });

  it('clears the error once a preset button populates a valid schedule', () => {
    const { onState } = renderHarness({
      sourceType: 'jdbc',
      schedule: 'bad',
    });
    expect(
      screen.getByText(/Must be an EventBridge cron\(\.\.\.\) or rate\(\.\.\.\) expression/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('jdbc-source-schedule-preset-hourly'));

    expect(onState).toHaveBeenCalledWith(
      expect.objectContaining({ schedule: 'rate(1 hour)' }),
    );
    expect(
      screen.queryByText(/Must be an EventBridge cron\(\.\.\.\) or rate\(\.\.\.\) expression/),
    ).not.toBeInTheDocument();
  });
});

// ─── Identifier validation (P17 Req 3.5, 3.6) ──────────────────

describe('WizardStepSource — identifier validation', () => {
  it('IDENTIFIER_RE accepts standard SQL identifiers', () => {
    expect(IDENTIFIER_RE.test('users')).toBe(true);
    expect(IDENTIFIER_RE.test('_users')).toBe(true);
    expect(IDENTIFIER_RE.test('user_table_2')).toBe(true);
  });

  it('IDENTIFIER_RE rejects empty, leading-digit, hyphen, dot, and quoted', () => {
    expect(IDENTIFIER_RE.test('')).toBe(false);
    expect(IDENTIFIER_RE.test('1users')).toBe(false);
    expect(IDENTIFIER_RE.test('user-table')).toBe(false);
    expect(IDENTIFIER_RE.test('schema.table')).toBe(false);
    expect(IDENTIFIER_RE.test('"users"')).toBe(false);
  });

  it('validateIdentifier accepts valid identifiers and rejects invalid', () => {
    expect(validateIdentifier('users')).toBeUndefined();
    expect(validateIdentifier('')).toBe('Required');
    expect(validateIdentifier('1bad')).toMatch(/Letters, digits, and underscores/);
    expect(validateIdentifier('schema.table')).toMatch(/Letters, digits, and underscores/);
  });

  it('renders inline error on Schema input when value is invalid', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: { schema: 'bad-schema' },
    });
    expect(
      screen.getAllByText(/Letters, digits, and underscores/).length,
    ).toBeGreaterThan(0);
  });

  it('renders inline error on Watermark Column input when invalid (incremental mode)', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: {
        read_mode: 'incremental',
        watermark_column: '1bad',
      },
    });
    expect(
      screen.getAllByText(/Letters, digits, and underscores/).length,
    ).toBeGreaterThan(0);
  });
});

// ─── Detect Schema button gating (P17 Req 4.1) ─────────────────

describe('WizardStepSource — Detect Schema button gating', () => {
  it('is disabled when required fields are missing', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: { schema: 'public', table: 'users' },
      // jdbc_url, jdbc_driver, secret_arn missing
    });
    expect(detectButton()).toBeDisabled();
  });

  it('is enabled when connection + secret + schema + table are populated (select_rds mode)', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        schema: 'public',
        table: 'users',
      },
    });
    expect(detectButton()).not.toBeDisabled();
  });

  it('is enabled in glue_connection mode without a secret (the connection carries credentials)', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: {
        _connection_mode: 'glue_connection',
        glue_connection_name: 'my-conn',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        schema: 'public',
        table: 'users',
      },
    });
    expect(detectButton()).not.toBeDisabled();
  });

  it('is disabled when schema or table fail identifier validation', () => {
    renderHarness({
      sourceType: 'jdbc',
      sourceConfig: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        schema: 'bad-schema',
        table: 'users',
      },
    });
    expect(detectButton()).toBeDisabled();
  });
});

// ─── Schema-detection happy path (P17 Req 4.6 / 4.7) ────────────

describe('WizardStepSource — schema-detection happy path', () => {
  function jdbcConfigForDetection() {
    return {
      sourceType: 'jdbc' as const,
      sourceConfig: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        schema: 'public',
        table: 'users',
      },
    };
  }

  it('clicks Detect Schema, polls RUNNING, then persists schema + detected_glue_* on READY', async () => {
    // Capture mutate options so we can simulate onSuccess after a click.
    let mutateOptions: any = null;
    mockMutateFn.mockImplementation((_payload: any, options: any) => {
      mutateOptions = options;
    });

    const { onState } = renderHarness(jdbcConfigForDetection());

    fireEvent.click(detectButton());

    expect(mockMutateFn).toHaveBeenCalledTimes(1);
    expect(mockMutateFn.mock.calls[0][0]).toEqual({
      // schema/table aliased to jdbc_schema/jdbc_table for the API call
      source_config: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        jdbc_schema: 'public',
        jdbc_table: 'users',
      },
      mode: 'select_rds',
    });

    // Simulate the mutation onSuccess callback (the real hook would invoke
    // this on a 202 from the backend) and flip the status query mock to RUNNING.
    mockStatusHookValue = {
      data: {
        status: 'RUNNING',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_abc123_',
        crawler_name: 'agentic-data-ingestion-discover-abc123',
      },
      isError: false,
      error: null,
    };
    act(() => {
      mutateOptions.onSuccess({
        crawler_name: 'agentic-data-ingestion-discover-abc123',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_abc123_',
        status: 'RUNNING',
      });
    });

    // Progress bar should be visible while RUNNING.
    await waitFor(() => {
      expect(screen.getByTestId('jdbc-source-detect-progress')).toBeInTheDocument();
    });

    // Now simulate the polling result transitioning to READY.
    mockStatusHookValue = {
      data: {
        status: 'READY',
        schema: [
          { name: 'id', type: 'bigint', nullable: false, description: 'PK' },
          { name: 'email', type: 'string', nullable: true },
        ],
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        glue_table: 'agentic_data_ingestion_abc123_users',
        table_prefix: 'agentic_data_ingestion_abc123_',
      },
      isError: false,
      error: null,
    };

    // Force a re-render so DetectionPanel re-evaluates the now-READY status.
    // Typing into the schedule input triggers an onChange and a re-render.
    fireEvent.change(
      screen.getByTestId('jdbc-source-schedule').querySelector('input')!,
      { target: { value: 'rate(1 hour)' } },
    );

    // Wait for the success Alert + detected schema table to appear.
    await waitFor(() => {
      expect(screen.getByTestId('jdbc-source-detect-success')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-source-detected-schema-table')).toBeInTheDocument();
    });

    // Confirm that detected_glue_* fields and detectedSourceSchema were
    // persisted. The source-side write now goes to `detectedSourceSchema`
    // (canonical) rather than the deprecated `detectedSchema` alias so
    // editing the target form cannot wipe the source schema.
    const persistedCalls = onState.mock.calls.map((c) => c[0] as WizardState);
    const lastWithDetected = [...persistedCalls]
      .reverse()
      .find((s) => s.detectedSourceSchema && s.detectedSourceSchema.length > 0);
    expect(lastWithDetected).toBeDefined();
    expect(lastWithDetected!.detectedSourceSchema).toEqual([
      { name: 'id', type: 'bigint', nullable: false, description: 'PK' },
      { name: 'email', type: 'string', nullable: true, description: '' },
    ]);
    expect(lastWithDetected!.sourceConfig.detected_glue_database).toBe(
      'agentic_data_ingestion_jdbc_discovery_dev',
    );
    expect(lastWithDetected!.sourceConfig.detected_glue_table).toBe(
      'agentic_data_ingestion_abc123_users',
    );
    expect(lastWithDetected!.sourceConfig.detected_table_prefix).toBe(
      'agentic_data_ingestion_abc123_',
    );
  });
});

// ─── Schema-detection failure paths ────────────────────────────

describe('WizardStepSource — schema-detection failure paths', () => {
  it('shows an Alert with the error message when status flips to FAILED', async () => {
    let mutateOptions: any = null;
    mockMutateFn.mockImplementation((_payload: any, options: any) => {
      mutateOptions = options;
    });

    const { onState } = renderHarness({
      sourceType: 'jdbc' as const,
      sourceConfig: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        schema: 'public',
        table: 'users',
      },
    });

    fireEvent.click(detectButton());
    expect(mockMutateFn).toHaveBeenCalledTimes(1);

    // Simulate the backend returning the crawler name and the polling result
    // arriving as FAILED.
    mockStatusHookValue = {
      data: {
        status: 'FAILED',
        crawler_name: 'agentic-data-ingestion-discover-fail',
        error_message: 'security-group misconfiguration',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
      },
      isError: false,
      error: null,
    };
    act(() => {
      mutateOptions.onSuccess({
        crawler_name: 'agentic-data-ingestion-discover-fail',
        glue_database: 'agentic_data_ingestion_jdbc_discovery_dev',
        table_prefix: 'agentic_data_ingestion_fail_',
        status: 'RUNNING',
      });
    });

    // Force a re-render so DetectionPanel re-evaluates the now-FAILED status.
    fireEvent.change(
      screen.getByTestId('jdbc-source-schedule').querySelector('input')!,
      { target: { value: 'rate(1 hour)' } },
    );

    await waitFor(() => {
      expect(screen.getByTestId('jdbc-source-detect-error')).toBeInTheDocument();
      expect(screen.getByText(/security-group misconfiguration/)).toBeInTheDocument();
    });

    // Detected schema MUST NOT be persisted on failure.
    const calls = onState.mock.calls.map((c) => c[0] as WizardState);
    const anyWithDetected = calls.some(
      (s) => s.detectedSourceSchema && s.detectedSourceSchema.length > 0,
    );
    expect(anyWithDetected).toBe(false);
  });

  it('surfaces a mutation onError as a terminal error', async () => {
    let mutateOptions: any = null;
    mockMutateFn.mockImplementation((_payload: any, options: any) => {
      mutateOptions = options;
    });

    renderHarness({
      sourceType: 'jdbc' as const,
      sourceConfig: {
        _connection_mode: 'select_rds',
        jdbc_url: 'jdbc:postgresql://host:5432/db',
        jdbc_driver: 'org.postgresql.Driver',
        secret_arn: 'arn:aws:secretsmanager:us-west-2:123:secret:db-Abc',
        schema: 'public',
        table: 'users',
      },
    });

    fireEvent.click(detectButton());
    expect(mockMutateFn).toHaveBeenCalledTimes(1);

    act(() => {
      mutateOptions.onError(new Error('S3 endpoint missing'));
    });

    await waitFor(() => {
      expect(screen.getByTestId('jdbc-source-detect-error')).toBeInTheDocument();
      expect(screen.getByText(/S3 endpoint missing/)).toBeInTheDocument();
    });
  });
});
