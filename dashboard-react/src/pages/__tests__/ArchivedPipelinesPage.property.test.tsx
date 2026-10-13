// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for ArchivedPipelinesPage
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 23: Archived pipelines listing
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import ArchivedPipelinesPage from '../ArchivedPipelinesPage';
import type { Pipeline } from '../../types';

// Mock hooks
let mockPipelines: Pipeline[] = [];
vi.mock('../../hooks', () => ({
  useArchivedPipelines: () => ({
    data: mockPipelines,
    isLoading: false,
    isError: false,
  }),
  useRestorePipeline: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useDeletePipeline: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
}));

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const archivedPipelineArb: fc.Arbitrary<Pipeline> = fc.record({
  pipeline_key: fc.uuid(),
  pipeline_name: fc.stringOf(
    fc.constantFrom(...'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_'.split('')),
    { minLength: 1, maxLength: 25 },
  ),
  status: fc.constant('archived'),
  source_schema: fc.constant('source'),
  target_schema: fc.constant('target'),
  created_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  updated_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
});

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderPage(pipelines: Pipeline[]) {
  mockPipelines = pipelines;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <ArchivedPipelinesPage />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Property 23: Archived pipelines listing
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 23: Archived pipelines listing', () => {
  /**
   * **Validates: Requirements 12.2**
   *
   * For any set of archived Pipeline objects, the ArchivedPipelinesPage SHALL
   * render each pipeline with restore and delete action buttons.
   */
  it('renders each archived pipeline with restore and delete buttons', () => {
    fc.assert(
      fc.property(
        fc.array(archivedPipelineArb, { minLength: 1, maxLength: 6 }),
        (pipelines) => {
          cleanup();
          renderPage(pipelines);

          const list = screen.getByTestId('archived-pipelines-list');
          const text = list.textContent ?? '';

          // Each pipeline name should appear
          for (const p of pipelines) {
            expect(text).toContain(p.pipeline_name || p.pipeline_key);
          }

          // Restore and delete buttons for each pipeline
          const restoreButtons = within(list).getAllByTestId('restore-button');
          const deleteButtons = within(list).getAllByTestId('delete-button');

          expect(restoreButtons).toHaveLength(pipelines.length);
          expect(deleteButtons).toHaveLength(pipelines.length);

          cleanup();
        },
      ),
      { numRuns: 100 },
    );
  });
});
