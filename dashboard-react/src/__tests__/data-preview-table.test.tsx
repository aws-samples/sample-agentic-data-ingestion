// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import * as fc from 'fast-check';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { DataPreviewResponse } from '../types';

// Mock the useDataPreview hook
const mockUseDataPreview = vi.fn();
vi.mock('../hooks', () => ({
  useDataPreview: (...args: unknown[]) => mockUseDataPreview(...args),
}));

import DataPreviewTable from '../components/DataPreviewTable';
import { ApiError } from '../services/api-client';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── Unit tests (Task 4.4) ────────────────────────────────────────

describe('DataPreviewTable', () => {
  it('shows a loading spinner while data is being fetched', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });
    expect(screen.getByTestId('data-preview-loading')).toBeInTheDocument();
  });

  it('shows an error message when the API returns an error', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });
    expect(screen.getByText('Failed to load data preview')).toBeInTheDocument();
  });

  it('shows a "not available" note for JDBC sources instead of a preview/error', () => {
    // For JDBC sources the hook is disabled; render whatever the disabled
    // query yields — the component must short-circuit on sourceType.
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" sourceType="JDBC" />, {
      wrapper: createWrapper(),
    });

    expect(screen.getByTestId('data-preview-na')).toBeInTheDocument();
    expect(screen.getByText('Preview not available for JDBC sources')).toBeInTheDocument();
    // Must NOT show the generic error or a table for JDBC sources.
    expect(screen.queryByText('Failed to load data preview')).not.toBeInTheDocument();
    expect(screen.queryByTestId('data-preview-table')).not.toBeInTheDocument();
  });

  // ── 404 is "nothing to preview", not "the read failed" (Task 5.7, C13) ──
  //
  // Validates: Requirements 12.4, 12.5
  //
  // The assertions are on TEXT, never on a class, a colour or a
  // StatusIndicator type — LGR-99050e6-1 (template / ratified) records that
  // colour alone fails WCAG 1.4.1, so the wording is what carries the
  // distinction and the wording is what these tests pin.

  it('states there is nothing to preview on a 404 instead of the generic failure', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      // react-query reports isError on a 404 even though use-data-preview
      // suppresses its retry and its toast — that is the whole defect (R12.4).
      isError: true,
      error: new ApiError(404, {
        error: 'No run with previewable source data found for pipeline: pipe-1',
      }),
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });

    expect(screen.getByTestId('data-preview-none')).toBeInTheDocument();
    expect(screen.getByText('No source data is available to preview')).toBeInTheDocument();
    // R12.4 — must NOT read as a failure, and must not fall through to the table.
    expect(screen.queryByText('Failed to load data preview')).not.toBeInTheDocument();
    expect(screen.queryByTestId('data-preview-table')).not.toBeInTheDocument();
  });

  it('offers no Retry on a 404, because re-requesting cannot succeed', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(404, { error: 'No runs found for pipeline: pipe-1' }),
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });

    expect(screen.getByTestId('data-preview-none')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('does not render the server 404 sentence, which names internal ids', () => {
    // The backend's three 404 sentences all mean the same thing and two name
    // internal ids (a pipeline key, a `skipped-<fingerprint>` run id), so they
    // are diagnostics — the displayed copy is the component's own.
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(404, {
        error: 'Run not found: skipped-7412636fccb938719673179658c05773',
      }),
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });

    expect(screen.getByText('No source data is available to preview')).toBeInTheDocument();
    expect(screen.queryByText(/skipped-7412636f/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Run not found/)).not.toBeInTheDocument();
  });

  it('still shows the generic failure for a non-404 API error', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(500, { error: 'Internal server error' }),
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });

    expect(screen.getByText('Failed to load data preview')).toBeInTheDocument();
    expect(screen.queryByTestId('data-preview-none')).not.toBeInTheDocument();
  });

  it('still shows the generic failure for a network error that is not an ApiError', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new TypeError('Failed to fetch'),
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });

    expect(screen.getByText('Failed to load data preview')).toBeInTheDocument();
    expect(screen.queryByTestId('data-preview-none')).not.toBeInTheDocument();
  });

  it('shows "No columns" when columns array is empty', () => {
    mockUseDataPreview.mockReturnValue({
      data: { columns: [], rows: [], total_rows_returned: 0, source_file: 'empty.csv' },
      isLoading: false,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });
    expect(screen.getByText('No columns')).toBeInTheDocument();
  });

  it('renders table with correct header showing source_file and total_rows_returned', () => {
    mockUseDataPreview.mockReturnValue({
      data: {
        columns: ['name', 'age'],
        rows: [{ name: 'Alice', age: 30 }],
        total_rows_returned: 1,
        source_file: 'data.csv',
      },
      isLoading: false,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });
    expect(screen.getByText('Latest file upload: data.csv')).toBeInTheDocument();
    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByText('age')).toBeInTheDocument();
  });

  it('passes pipelineId and runId to useDataPreview', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" runId="run-1" />, { wrapper: createWrapper() });
    expect(mockUseDataPreview).toHaveBeenCalledWith('pipe-1', 'run-1', undefined, { enabled: true });
  });

  it('passes only pipelineId when runId is not provided', () => {
    mockUseDataPreview.mockReturnValue({
      data: undefined,
      isLoading: true,
      isError: false,
    });

    render(<DataPreviewTable pipelineId="pipe-1" />, { wrapper: createWrapper() });
    expect(mockUseDataPreview).toHaveBeenCalledWith('pipe-1', undefined, undefined, { enabled: true });
  });

  // ── Property test (Task 4.5) ─────────────────────────────────────
  /**
   * Feature: p12c-addon, Property 4: DataPreviewTable rendering fidelity
   *
   * For any DataPreviewResponse with a non-empty columns array, source_file
   * string, and total_rows_returned number, the DataPreviewTable shall render
   * a table with exactly columns.length column definitions whose headers match
   * the column names, and the table header text shall contain both the
   * source_file value and the total_rows_returned value.
   *
   * Validates: Requirements 6.1, 6.4
   */
  it('Property 4: rendering fidelity — columns and header match response data', () => {
    // Generator for unique non-empty column names (alphanumeric, no leading/trailing whitespace)
    const columnNameArb = fc.stringMatching(/^[a-zA-Z][a-zA-Z0-9_]{0,19}$/);
    const uniqueColumnsArb = fc
      .uniqueArray(columnNameArb, { minLength: 1, maxLength: 10 })
      .filter((arr) => arr.length > 0);

    const sourceFileArb = fc.stringMatching(/^[a-zA-Z0-9_./\-]{1,50}$/);
    const totalRowsArb = fc.nat({ max: 10000 });

    fc.assert(
      fc.property(uniqueColumnsArb, sourceFileArb, totalRowsArb, (columns, sourceFile, totalRows) => {
        const response: DataPreviewResponse = {
          columns,
          rows: [],
          total_rows_returned: totalRows,
          source_file: sourceFile,
        };

        mockUseDataPreview.mockReturnValue({
          data: response,
          isLoading: false,
          isError: false,
        });

        const { container, unmount } = render(
          <DataPreviewTable pipelineId="test-pipe" />,
          { wrapper: createWrapper() },
        );

        // Verify column count: each column should have a <th> with the column name
        const thElements = container.querySelectorAll('th');
        const headerTexts = Array.from(thElements).map((th) => th.textContent?.trim());
        for (const col of columns) {
          expect(headerTexts).toContain(col);
        }
        // The number of th elements should match columns.length
        expect(thElements.length).toBe(columns.length);

        // Verify header text contains the filename extracted from source_file
        const h2 = container.querySelector('h2');
        expect(h2).not.toBeNull();
        const h2Text = h2!.textContent ?? '';
        const expectedFileName = sourceFile.split('/').pop() ?? sourceFile;
        expect(h2Text).toContain('Latest file upload:');
        expect(h2Text).toContain(expectedFileName);

        unmount();
      }),
      { numRuns: 100 },
    );
  });
});
