// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

vi.mock('../../hooks', () => ({
  useCodeVersion: (id: string) => ({
    data: id ? { code_version_id: id, code: 'print("hello")', language: 'PySpark', created_at: '2024-01-01' } : undefined,
    isLoading: false,
    isError: false,
  }),
  useTestResults: () => ({ data: undefined, isLoading: false, isError: false }),
}));

import CodePage from '../CodePage';

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipe',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
  schema_history: [
    { version: 1, created: '2024-01-01T00:00:00Z', has_mapping: false, mapping_count: 0, runs: [{ run_id: 'r1', timestamp: '2024-01-01T00:00:00Z', code_version_id: 'cv-123' }] },
  ],
  all_files: [],
};

function renderPage(pipeline = mockPipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
        <CodePage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

describe('CodePage', () => {
  it('renders CodeViewer when code version is available', () => {
    renderPage();
    expect(screen.getByTestId('code-viewer')).toBeInTheDocument();
  });

  it('renders code content', () => {
    renderPage();
    expect(screen.getByTestId('code-content')).toHaveTextContent('print("hello")');
  });

  it('shows empty state when no code version', () => {
    const noCodePipeline = { ...mockPipeline, schema_history: [{ version: 1, created: '2024-01-01T00:00:00Z', has_mapping: false, mapping_count: 0, runs: [] }] };
    renderPage(noCodePipeline);
    expect(screen.getByText(/No code version available/)).toBeInTheDocument();
  });
});
