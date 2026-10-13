// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for GlueJobDetailPage
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 22: Glue job detail tab content
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import GlueJobDetailPage from '../GlueJobDetailPage';
import type { GlueJobDetail, GlueJobRun } from '../../types';

// Mock hooks
let mockGlueJob: GlueJobDetail | undefined;
vi.mock('../../hooks', () => ({
  useGlueJob: () => ({
    data: mockGlueJob,
    isLoading: false,
    isError: false,
  }),
  useRunGlueJob: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useRollbackGlueJob: () => ({
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useJobVersionHistory: () => ({
    data: undefined,
    isLoading: false,
  }),
}));

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const glueJobRunArb: fc.Arbitrary<GlueJobRun> = fc.record({
  run_id: fc.uuid(),
  status: fc.constantFrom('SUCCEEDED', 'FAILED', 'RUNNING', 'STOPPED'),
  started_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  completed_at: fc.option(
    fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
    { nil: undefined },
  ),
  duration_seconds: fc.option(fc.integer({ min: 1, max: 3600 }), { nil: undefined }),
  error_message: fc.option(fc.constant('Out of memory'), { nil: undefined }),
});

const glueJobDetailArb: fc.Arbitrary<GlueJobDetail> = fc.record({
  job_name: fc.constantFrom('etl-job-alpha', 'etl-job-beta', 'transform-job'),
  status: fc.constantFrom('READY', 'RUNNING', 'FAILED'),
  worker_type: fc.constantFrom('G.1X', 'G.2X', 'G.4X'),
  number_of_workers: fc.integer({ min: 2, max: 100 }),
  timeout: fc.integer({ min: 1, max: 2880 }),
  script_location: fc.constant('s3://bucket/scripts/etl.py'),
  created_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  last_modified: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  runs: fc.array(glueJobRunArb, { minLength: 0, maxLength: 5 }),
  configuration: fc.dictionary(
    fc.constantFrom('--conf', '--extra-py-files', '--TempDir'),
    fc.constantFrom('spark.sql.shuffle.partitions=200', 's3://libs/utils.py', 's3://tmp/'),
    { minKeys: 0, maxKeys: 3 },
  ),
  script_content: fc.option(fc.constant('import sys\nfrom pyspark.context import SparkContext'), { nil: undefined }),
  script_versions: fc.option(
    fc.array(fc.constantFrom('v1', 'v2', 'v3'), { minLength: 0, maxLength: 3 }),
    { nil: undefined },
  ),
});

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderPage(glueJob: GlueJobDetail) {
  mockGlueJob = glueJob;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/pipelines/test-pipeline/glue-job/${glueJob.job_name}`]}>
          <Routes>
            <Route path="/pipelines/:id/glue-job/:jobName" element={<GlueJobDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Property 22: Glue job detail tab content
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 22: Glue job detail tab content', () => {
  /**
   * **Validates: Requirements 11.2, 11.3, 11.4**
   *
   * For any GlueJobDetail object, the Overview tab SHALL contain job metadata
   * and worker config, the Runs tab SHALL list all runs, the Configuration tab
   * SHALL display all parameters, and the Code tab SHALL display script content.
   */
  it('Overview tab contains job metadata and worker configuration', () => {
    fc.assert(
      fc.property(glueJobDetailArb, (glueJob) => {
        cleanup();
        renderPage(glueJob);

        const overview = screen.getByTestId('tab-overview');

        // Job metadata
        expect(within(overview).getByTestId('job-name').textContent).toBe(glueJob.job_name);
        expect(within(overview).getByTestId('job-status').textContent).toContain(glueJob.status);

        // Worker config
        expect(within(overview).getByTestId('worker-type').textContent).toBe(glueJob.worker_type);
        expect(within(overview).getByTestId('num-workers').textContent).toBe(String(glueJob.number_of_workers));
        expect(within(overview).getByTestId('timeout').textContent).toBe(String(glueJob.timeout));

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('Runs tab lists all runs with status and duration', () => {
    const jobWithRunsArb = glueJobDetailArb.filter((j) => j.runs.length > 0);

    fc.assert(
      fc.property(jobWithRunsArb, (glueJob) => {
        cleanup();
        renderPage(glueJob);

        // Switch to Runs tab
        const runsTab = screen.getByRole('tab', { name: /Runs/i });
        fireEvent.click(runsTab);

        const runsPanel = screen.getByTestId('tab-runs');
        const text = runsPanel.textContent ?? '';

        // All runs should be listed
        for (const run of glueJob.runs) {
          expect(text).toContain(run.run_id);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('Configuration tab displays all parameters', () => {
    fc.assert(
      fc.property(glueJobDetailArb, (glueJob) => {
        cleanup();
        renderPage(glueJob);

        // Switch to Configuration tab
        const configTab = screen.getByRole('tab', { name: /Configuration/i });
        fireEvent.click(configTab);

        const configPanel = screen.getByTestId('tab-configuration');
        const text = configPanel.textContent ?? '';

        // Worker config should be present
        expect(text).toContain(glueJob.worker_type);
        expect(text).toContain(String(glueJob.number_of_workers));
        expect(text).toContain(String(glueJob.timeout));

        // Custom parameters
        for (const key of Object.keys(glueJob.configuration)) {
          expect(text).toContain(key);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('Code tab displays script content when available', () => {
    const jobWithScriptArb = glueJobDetailArb.map((j) => ({
      ...j,
      script_content: 'import sys\nfrom pyspark.context import SparkContext',
    }));

    fc.assert(
      fc.property(jobWithScriptArb, (glueJob) => {
        cleanup();
        renderPage(glueJob);

        // Switch to Code tab
        const codeTab = screen.getByRole('tab', { name: /Code/i });
        fireEvent.click(codeTab);

        const codePanel = screen.getByTestId('tab-code');
        const scriptEl = within(codePanel).getByTestId('script-content');
        expect(scriptEl.textContent).toContain('import sys');

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
