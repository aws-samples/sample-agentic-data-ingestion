// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for the Glue job Configuration panel being read-only.
 *
 * Feature: p30-capability-and-control (task 5.2)
 * **Validates: Requirements 13.2**
 *
 * The panel previously offered Edit / Save, and Save fired a
 * "Configuration saved" success toast without calling any mutation — no update
 * endpoint exists for Glue job parameters. R13.2 requires the panel either call
 * a real mutation or be read-only; it must never confirm an unpersisted change.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import GlueJobDetailPage from '../GlueJobDetailPage';
import type { GlueJobDetail } from '../../types';

let mockGlueJob: GlueJobDetail | undefined;

vi.mock('../../hooks', () => ({
  useGlueJob: () => ({ data: mockGlueJob, isLoading: false, isError: false }),
  useRunGlueJob: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRollbackGlueJob: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useJobVersionHistory: () => ({ data: undefined, isLoading: false }),
}));

const JOB: GlueJobDetail = {
  job_name: 'etl-job-alpha',
  status: 'READY',
  worker_type: 'G.1X',
  number_of_workers: 5,
  timeout: 120,
  script_location: 's3://bucket/scripts/etl.py',
  created_at: '2024-06-01T00:00:00Z',
  last_modified: '2024-06-10T00:00:00Z',
  runs: [],
  configuration: { '--TempDir': 's3://tmp/' },
  script_content: 'print("hello")',
  script_versions: [{ version: 1, timestamp: '2024-06-01T00:00:00Z' }],
};

function renderConfigTab(job: GlueJobDetail = JOB) {
  mockGlueJob = job;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/pipelines/p1/glue-job/${job.job_name}`]}>
          <Routes>
            <Route path="/pipelines/:id/glue-job/:jobName" element={<GlueJobDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole('tab', { name: /Configuration/i }));
  return screen.getByTestId('tab-configuration');
}

describe('GlueJobDetailPage Configuration panel is read-only (R13.2)', () => {
  beforeEach(() => {
    mockGlueJob = undefined;
  });

  it('offers no Edit, Save, or Reset action', () => {
    const panel = renderConfigTab();

    for (const label of [/^Edit$/, /^Save$/, /^Reset$/]) {
      expect(screen.queryByRole('button', { name: label })).toBeNull();
    }
    // Nothing in the panel is an editable field.
    expect(panel.querySelectorAll('input, textarea').length).toBe(0);
  });

  it('states that the values are read-only', () => {
    const panel = renderConfigTab();

    expect(screen.getByTestId('config-read-only-note').textContent).toMatch(/read-only/i);
    expect(panel.textContent).toMatch(/read-only/i);
  });

  it('still displays the job parameters it no longer lets you edit', () => {
    const panel = renderConfigTab();

    expect(screen.getByTestId('config-worker-type').textContent).toBe(JOB.worker_type);
    expect(screen.getByTestId('config-num-workers').textContent).toBe(String(JOB.number_of_workers));
    expect(panel.textContent).toContain('--TempDir');
    expect(panel.textContent).toContain('s3://tmp/');
  });
});
