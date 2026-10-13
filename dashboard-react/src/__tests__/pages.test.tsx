// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../contexts/ToastContext';

// Mock the hooks module
//
// p35-schema-surfaces held-assertion inventory **row 5**, amended by task 8.7.
//
// `SchemaManagementPage` gained `useSetDisplayLabel` in task 8.7 (D2's rename,
// R4.9) and calls it at the component's **top level** — not inside a
// conditionally-mounted child the way `useSchemaDetail` is reached. Without the
// entry below, row 5's mechanism applies: a `vi.mock` factory **replaces** the
// module, so any export it omits is `undefined`; the component then calls
// `undefined()` **during render, inside the test that renders it**. The failure
// is a stack trace naming a value that is not a function — not an assertion
// diff — and the file still collects and still reports per test.
// Amend the factory before adding a hook to a component the factory's tests
// render.
//
// Amended by **addition** and stated as deliberate, per `LGR-5906ec6-2` and
// `LGR-60c4985-6` (both **template / ratified**): every existing entry, name and
// assertion in this file is unchanged, and the subject — the Schemas page
// renders its listing with the hooks module stubbed — is untouched.
vi.mock('../hooks', () => ({
  usePipelines: vi.fn(),
  usePipelineDetail: vi.fn(),
  useRunDetail: vi.fn(),
  useApprovals: vi.fn(),
  useApprovalAction: vi.fn(),
  useSchemas: vi.fn(() => ({ data: [], isLoading: false, isError: false })),
  useSchemaDetail: vi.fn(),
  useUploadSchema: vi.fn(),
  useUpdateFieldContext: vi.fn(),
  useDeleteSchema: vi.fn(),
  // Task 8.7, inventory row 5. See the block comment above this factory.
  useSetDisplayLabel: vi.fn(),
  useStats: vi.fn(),
  useBedrockModels: vi.fn(() => ({ data: [], isLoading: false })),
  useCreatePipeline: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useEditPipeline: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useS3TablesBuckets: vi.fn(() => ({ data: [], isLoading: false })),
  useTablesInBucket: vi.fn(() => ({ data: [], isLoading: false })),
  useGlueDatabases: vi.fn(() => ({ data: [], isLoading: false })),
  useTablesInDatabase: vi.fn(() => ({ data: [], isLoading: false })),
  useBulkArchive: vi.fn(() => ({ execute: vi.fn(), progress: null, isExecuting: false })),
  useBulkDelete: vi.fn(() => ({ execute: vi.fn(), progress: null, isExecuting: false })),
  useUpdates: vi.fn(() => ({ data: { updates: [], count: 0 }, isLoading: false, isError: false })),
  pipelineKeys: { all: ['pipelines'], list: () => ['pipelines', 'list'], detail: (id: string) => ['pipelines', id] },
  approvalKeys: { all: ['approvals'], pending: () => ['approvals', 'pending'] },
  schemaKeys: { all: ['schemas'], detail: (id: string) => ['schemas', id] },
}));

import PipelineListPage from '../pages/PipelineListPage';
import ApprovalsPage from '../pages/ApprovalsPage';
import SchemaManagementPage from '../pages/SchemaManagementPage';
import * as hooks from '../hooks';

const mockedHooks = vi.mocked(hooks);

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default mock for useStats
  mockedHooks.useStats.mockReturnValue({
    data: { total_pipelines: 5, total_files: 20, schema_versions: 3, last_activity: '2025-01-01T00:00:00Z' },
    isLoading: false,
    isError: false,
  } as unknown as ReturnType<typeof hooks.useStats>);
  // Default mock for useApprovalAction (mutation hook)
  mockedHooks.useApprovalAction.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useApprovalAction>);
  // Default mock for useUploadSchema
  mockedHooks.useUploadSchema.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useUploadSchema>);
  // Default mock for useUpdateFieldContext
  mockedHooks.useUpdateFieldContext.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useUpdateFieldContext>);
  // Default mock for useDeleteSchema
  mockedHooks.useDeleteSchema.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useDeleteSchema>);
  // Default mock for useSetDisplayLabel — task 8.7, inventory row 5.
  // The factory entry above stops the export being `undefined`; this stops the
  // *call* returning `undefined`, which the page destructures for `isPending`.
  mockedHooks.useSetDisplayLabel.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useSetDisplayLabel>);
});

describe('PipelineListPage', () => {
  it('renders loading state', () => {
    mockedHooks.usePipelines.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    } as unknown as ReturnType<typeof hooks.usePipelines>);

    render(<PipelineListPage />, { wrapper });
    expect(screen.getByText('Loading pipelines')).toBeInTheDocument();
  });

  it('renders pipeline data', () => {
    mockedHooks.usePipelines.mockReturnValue({
      data: {
        pipelines: [
          { pipeline_key: 'p1', pipeline_name: 'Test Pipeline', status: 'active', source_schema: 'src', target_schema: 'tgt', created_at: '2025-01-01', updated_at: '2025-01-02' },
        ],
        total_count: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
        has_more: false,
      },
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof hooks.usePipelines>);

    render(<PipelineListPage />, { wrapper });
    expect(screen.getByText('Test Pipeline')).toBeInTheDocument();
    expect(screen.getByText('active')).toBeInTheDocument();
  });
});

describe('ApprovalsPage', () => {
  it('renders empty state', () => {
    // useApprovals is now a useInfiniteQuery — data is { pages, pageParams }.
    mockedHooks.useApprovals.mockReturnValue({
      data: { pages: [{ approvals: [], hasMore: false }], pageParams: [undefined] },
      isLoading: false,
      isError: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useApprovals>);

    render(<ApprovalsPage />, { wrapper });
    expect(screen.getByText('No pending approvals.')).toBeInTheDocument();
  });

  it('renders approval list', () => {
    mockedHooks.useApprovals.mockReturnValue({
      data: {
        pages: [
          {
            approvals: [
              { approval_id: 'a1', pipeline_key: 'p1', approval_type: 'schema_mapping', status: 'pending', created_at: '2025-01-01' },
            ],
            hasMore: false,
          },
        ],
        pageParams: [undefined],
      },
      isLoading: false,
      isError: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof hooks.useApprovals>);

    render(<ApprovalsPage />, { wrapper });
    expect(screen.getByText('schema_mapping')).toBeInTheDocument();
    expect(screen.getByText('Review')).toBeInTheDocument();
  });
});

describe('SchemaManagementPage', () => {
  it('renders empty state', () => {
    mockedHooks.useSchemas.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof hooks.useSchemas>);

    render(<SchemaManagementPage />, { wrapper });
    expect(screen.getByText('No schemas found.')).toBeInTheDocument();
  });

  it('renders schema list', () => {
    mockedHooks.useSchemas.mockReturnValue({
      data: [
        { schema_id: 's1', schema_name: 'Customer Schema', source_type: 'csv', fields: [{ field_name: 'id', field_type: 'string' }], created_at: '2025-01-01' },
      ],
      isLoading: false,
      isError: false,
    } as unknown as ReturnType<typeof hooks.useSchemas>);

    render(<SchemaManagementPage />, { wrapper });
    expect(screen.getByText('Customer Schema')).toBeInTheDocument();
    expect(screen.getByText('csv')).toBeInTheDocument();
  });
});
