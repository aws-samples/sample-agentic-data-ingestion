// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { WizardStepTarget } from '../WizardStepTarget';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

// Mock the API client
vi.mock('../../../services/api-client', () => ({
  apiClient: {
    get: vi.fn(),
  },
}));

// p66 Task 1.1 — Validates: Requirements 5.1, 5.3.
// Refetch handles for the two Glue query mocks below. Declared at module scope
// (and referenced only from inside the lazily-invoked hook arrows, so there is
// no temporal-dead-zone hazard against the hoisted `vi.mock` factory) so a test
// can assert on the recovery wiring, matching the idiom already used for the S3
// bucket dropdown in `WizardStepTarget.dropdown-error.test.tsx`.
const glueDatabasesRefetch = vi.fn();
const glueTablesRefetch = vi.fn();

// P17 Task 20.6 — Validates: Requirements 14.1, 14.2, 22.1.
// `JdbcTargetForm` (the consolidated replacement for `JdbcForm` + `RdsDatabaseForm`)
// re-uses `JdbcConnectionSelector`, which calls `useRdsInstances` /
// `useRdsClusters` / `useRdsGlueConnections` / `useRdsSecrets`. Mock them to
// the empty list so the form renders without a `QueryClient`.
vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useS3TablesBuckets: () => ({ data: [], isLoading: false }),
    useTablesInBucket: () => ({ data: [], isLoading: false }),
    // p66 Task 1.1 — the two Glue query mocks carry EVERY field their consumers
    // read off a React Query result, so no state a test means to assert can be
    // `undefined` by omission and silently falsy. The field list is enumerated
    // from the readers, not guessed:
    //   - `isLoading`, `isError`, `refetch` — the whole of `AsyncStatusQuery`
    //     (`components/common/select-async-status.ts:24-28`), and the helper
    //     body reads exactly those three (`:50`, `:52`, `:65`).
    //   - `data` — destructured by both `GlueCatalogForm`s
    //     (`pages/wizard/TargetForms.tsx:203`, `:206`;
    //      `components/TargetConfigForm.tsx:145`, `:148`).
    // `isError` was absent here before, so `statusType` resolved to `finished`
    // by accident rather than by intent; it is now stated. No behaviour change:
    // `undefined` and `false` are both falsy at `select-async-status.ts:50`.
    useGlueDatabases: () => ({
      data: [],
      isLoading: false,
      isError: false,
      refetch: glueDatabasesRefetch,
    }),
    useTablesInDatabase: () => ({
      data: [],
      isLoading: false,
      isError: false,
      refetch: glueTablesRefetch,
    }),
    // P19 Task 7.2 — the JDBC target detect-schema panel mounts these
    // hooks only when the user has chosen a JDBC target. Stub both so
    // the component renders without a QueryClientProvider in tests.
    useJdbcDetectTargetSchemaStart: () => ({
      mutate: vi.fn(),
      isPending: false,
    }),
    useJdbcDetectTargetSchemaStatus: () => ({
      data: undefined,
      isError: false,
      error: null,
    }),
  };
});

import { apiClient } from '../../../services/api-client';

function renderStep(overrides: Partial<WizardState> = {}) {
  const state = { ...INITIAL_WIZARD_STATE, ...overrides };
  const onChange = vi.fn();
  render(<WizardStepTarget state={state} onChange={onChange} />);
  return { state, onChange };
}

describe('WizardStepTarget', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders target type selector', () => {
    renderStep();
    expect(screen.getByText('Target Type')).toBeInTheDocument();
  });

  it('renders S3 Tables form fields by default', () => {
    renderStep({ targetType: 's3_tables' });
    expect(screen.getByText('Use existing')).toBeInTheDocument();
  });

  it('renders Glue Catalog form when glue_catalog is selected', () => {
    renderStep({ targetType: 'glue_catalog' });
    expect(screen.getByText('Glue Database')).toBeInTheDocument();
  });

  // P17 Task 20.1 — Validates: Requirement 14.2.
  // The legacy JdbcForm (with its plain "JDBC Connection URL" / "Username" /
  // "Password" inputs) was removed. The consolidated JdbcTargetForm renders
  // the shared connection selector + target-side database/table inputs.
  it('renders consolidated JDBC target form when jdbc is selected', () => {
    renderStep({ targetType: 'jdbc' });
    expect(screen.getByTestId('jdbc-target-database-name')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-target-table-name')).toBeInTheDocument();
    // Connection selector is mounted with the `jdbc-target-conn` test-id prefix.
    expect(screen.getByTestId('jdbc-target-conn-jdbc-url')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-target-conn-jdbc-driver')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-target-conn-port')).toBeInTheDocument();
    // Legacy field labels must NOT exist anywhere in the form.
    expect(screen.queryByText('JDBC Connection URL')).not.toBeInTheDocument();
    expect(screen.queryByText('Username')).not.toBeInTheDocument();
    expect(screen.queryByText('Password')).not.toBeInTheDocument();
  });

  it('renders S3 Parquet form when s3_parquet is selected', () => {
    renderStep({ targetType: 's3_parquet' });
    expect(screen.getByText('S3 Path')).toBeInTheDocument();
  });

  // P17 Task 20.3 — Validates: Requirement 14.1. The legacy `RDS Database`
  // option must no longer appear in the target-type selector.
  it('does not include the legacy "RDS Database" option', () => {
    renderStep({ targetType: 's3_tables' });
    expect(screen.queryByText('RDS Database')).not.toBeInTheDocument();
  });

  it('shows Detect Schema button for s3_tables (existing-mode requires bucket+table)', () => {
    renderStep({
      targetType: 's3_tables',
      targetConfig: {
        bucket_name: 'my-bucket',
        table_bucket_arn: 'arn:aws:s3tables:us-west-2:123:bucket/my-bucket',
        table_name: 'my_table',
        namespace: 'default',
      },
    });
    expect(screen.getByText('Detect Schema')).toBeInTheDocument();
  });

  it('shows Detect Schema button for glue_catalog when fully populated', () => {
    renderStep({
      targetType: 'glue_catalog',
      targetConfig: { database_name: 'mydb', table_name: 'mytable' },
    });
    expect(screen.getByText('Detect Schema')).toBeInTheDocument();
  });

  it('does not show Detect Schema button for jdbc', () => {
    renderStep({ targetType: 'jdbc' });
    expect(screen.queryByText('Detect Schema')).not.toBeInTheDocument();
  });

  it('calls API when Detect Schema is clicked', async () => {
    const mockSchema = [{ name: 'id', type: 'int', nullable: false, description: '' }];
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ schema: mockSchema });

    const { onChange } = renderStep({
      targetType: 'glue_catalog',
      targetConfig: { database_name: 'mydb', table_name: 'mytable' },
    });

    fireEvent.click(screen.getByText('Detect Schema'));

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/resources/detect-schema'),
      );
    });

    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith(
        expect.objectContaining({
          detectedSchema: mockSchema,
          schemaSource: 'detected',
          schemaFields: mockSchema,
        }),
      );
    });
  });

  it('shows error when detect schema fails', async () => {
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('API error'));

    renderStep({
      targetType: 'glue_catalog',
      targetConfig: { database_name: 'mydb', table_name: 'mytable' },
    });
    fireEvent.click(screen.getByText('Detect Schema'));

    await waitFor(() => {
      expect(screen.getByText(/API error/)).toBeInTheDocument();
    });
  });
});
