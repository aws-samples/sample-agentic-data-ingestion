// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * P29 R1.2 / R1.3 — wizard dropdown honest async states.
 *
 * When a resource fetcher throws (fetchers no longer swallow errors into `[]`),
 * the consuming dropdown must render the Async_State_Triple inline: a Cloudscape
 * Select with `statusType="error"`, an error message, and a Retry recovery link
 * that re-triggers the query.
 *
 * Validates: Requirements 1.2, 1.3
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { WizardStepTarget } from '../WizardStepTarget';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

// Reconfigurable mock for the failing dropdown fetcher.
const useS3TablesBucketsMock = vi.fn();
const refetchBuckets = vi.fn();

// p66 Task 1.1 — refetch handles for the two Glue query mocks below, so a test
// can assert on their recovery wiring the way the S3 bucket cases already do.
const glueDatabasesRefetch = vi.fn();
const glueTablesRefetch = vi.fn();

vi.mock('../../../services/api-client', () => ({
  apiClient: { get: vi.fn() },
}));

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useS3TablesBuckets: () => useS3TablesBucketsMock(),
    useTablesInBucket: () => ({ data: [], isLoading: false, isError: false }),
    // p66 Task 1.1 — the two Glue query mocks carry EVERY field their consumers
    // read off a React Query result, enumerated from the readers rather than
    // guessed: `isLoading`, `isError` and `refetch` are the whole of
    // `AsyncStatusQuery` (`components/common/select-async-status.ts:24-28`) and
    // are exactly what the helper body reads (`:50`, `:52`, `:65`); `data` is
    // destructured by both `GlueCatalogForm`s (`pages/wizard/TargetForms.tsx:203`,
    // `:206`; `components/TargetConfigForm.tsx:145`, `:148`). `refetch` was the
    // missing one here. No behaviour change — with `isError: false` the helper
    // never calls it (`:65`).
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
    // JdbcTargetForm (mounted for jdbc targets) also consumes these — stub them.
    useRdsInstances: () => ({ data: [], isLoading: false, isError: false }),
    useRdsClusters: () => ({ data: [], isLoading: false, isError: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false, isError: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false, isError: false }),
    useJdbcDetectTargetSchemaStart: () => ({ mutate: vi.fn(), isPending: false }),
    useJdbcDetectTargetSchemaStatus: () => ({ data: undefined, isError: false, error: null }),
  };
});

function renderStep(overrides: Partial<WizardState> = {}) {
  const state = { ...INITIAL_WIZARD_STATE, ...overrides };
  const onChange = vi.fn();
  const { container } = render(<WizardStepTarget state={state} onChange={onChange} />);
  return { state, onChange, container };
}

describe('WizardStepTarget — dropdown honest async states (P29 R1.3)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an inline error + Retry in the S3 bucket dropdown when the fetch fails', () => {
    useS3TablesBucketsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('boom'),
      refetch: refetchBuckets,
    });

    const { container } = renderStep({ targetType: 's3_tables' });
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="s3t-bucket-select"]')!;
    expect(select).not.toBeNull();

    // Opening the dropdown surfaces the error copy + a recovery (Retry) link
    // rather than an empty option list.
    select.openDropdown();
    expect(screen.getByText(/Couldn't load buckets/)).toBeInTheDocument();

    const recovery = select.findErrorRecoveryButton();
    expect(recovery).not.toBeNull();
  });

  it('Retry recovery re-triggers the query', () => {
    useS3TablesBucketsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('boom'),
      refetch: refetchBuckets,
    });

    const { container } = renderStep({ targetType: 's3_tables' });
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="s3t-bucket-select"]')!;
    select.openDropdown();

    const recovery = select.findErrorRecoveryButton()!;
    fireEvent.click(recovery.getElement());

    expect(refetchBuckets).toHaveBeenCalled();
  });

  it('shows loaded options (statusType finished) when the fetch succeeds', () => {
    useS3TablesBucketsMock.mockReturnValue({
      data: [{ bucket_name: 'my-bucket' }],
      isLoading: false,
      isError: false,
      refetch: refetchBuckets,
    });

    const { container } = renderStep({ targetType: 's3_tables' });
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="s3t-bucket-select"]')!;
    select.openDropdown();

    // No error recovery link in the healthy path; the option is listed.
    expect(select.findErrorRecoveryButton()).toBeNull();
    const options = select.findDropdown().findOptions();
    expect(options).toHaveLength(1);
    expect(options[0].getElement().textContent).toContain('my-bucket');
  });
});
