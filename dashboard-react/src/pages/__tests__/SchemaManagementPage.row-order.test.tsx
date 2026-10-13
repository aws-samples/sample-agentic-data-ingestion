// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The schema table lists newest first.
 *
 * The listing arrives grouped by source (uploaded, then Glue, then S3 Tables),
 * each group in its own order, so the Created column read as unsorted: on
 * 2026-10-10 it showed 7/22, 7/23, 7/24, 9/17, 9/23, 7/21, 10/6, 3/9.
 *
 * Expectations come from that requirement: newest `created_at` first, a row with
 * no date last, and equal dates ordered by the name shown.
 *
 * Mocks `apiClient` only, as the sibling files here do, so the real
 * `useSchemas` / `normalizeSchema` path turns the wire's `lastModified` into the
 * `created_at` the page sorts on.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock, post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import SchemaManagementPage from '../SchemaManagementPage';

function row(name: string, source: string, lastModified: string | undefined) {
  return {
    schemaId: name,
    name,
    source,
    targetType: 'Target Schema',
    ...(lastModified === undefined ? {} : { lastModified }),
    version: '1.0',
    fieldCount: null,
  };
}

/** The listing in the order the API groups it: uploaded, then S3 Tables. */
const LISTING = [
  row('p28b_r19_schema', 'uploaded', '2026-07-22T19:27:16+00:00'),
  row('target_schema_jdbc_customers_v2', 'uploaded', '2026-09-17T12:14:41+00:00'),
  row('verify172_schema', 'uploaded', '2026-07-21T12:02:35+00:00'),
  row('undated_schema', 'uploaded', undefined),
  row('demo.ns.tbl', 's3_table_bucket', '2026-10-06T22:54:28+00:00'),
  row('beta.ns.tbl', 's3_table_bucket', '2026-03-09T19:35:27+00:00'),
  row('alpha.ns.tbl', 's3_table_bucket', '2026-03-09T19:35:27+00:00'),
];

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

function nameColumn(): string[] {
  return screen
    .getAllByRole('row')
    .map((r) => within(r).queryAllByRole('cell')[0]?.textContent?.trim() ?? '')
    .filter((text) => text.length > 0);
}

describe('SchemaManagementPage — row order', () => {
  it('lists newest first across sources, undated last, ties by name', async () => {
    getMock.mockImplementation(async (path: string) => {
      if (path === '/schemas') return { schemas: LISTING, sourcesFailed: [] };
      throw new Error(`unexpected GET ${path}`);
    });

    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('demo.ns.tbl');

    expect(nameColumn()).toEqual([
      'demo.ns.tbl',
      'target_schema_jdbc_customers_v2',
      'p28b_r19_schema',
      'verify172_schema',
      'alpha.ns.tbl',
      'beta.ns.tbl',
      'undated_schema',
    ]);
  });
});
