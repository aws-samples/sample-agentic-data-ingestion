// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

vi.mock('../../hooks', () => ({
  usePipelineDetail: () => ({
    data: {
      pipeline_key: 'test-pipe',
      pipeline_name: 'Test Pipeline',
      status: 'active',
      source_schema: 'src',
      target_schema: 'tgt',
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
      schema_history: [],
      all_files: [],
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));

import PipelineLayout from '../PipelineLayout';

function renderLayout(path = '/pipelines/test-pipe') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/pipelines/:id" element={<PipelineLayout />}>
          <Route index element={<div data-testid="outlet-overview">Overview Content</div>} />
          <Route path="schema" element={<div data-testid="outlet-schema">Schema Content</div>} />
          <Route path="code" element={<div data-testid="outlet-code">Code Content</div>} />
          <Route path="executions" element={<div data-testid="outlet-executions">Executions Content</div>} />
          <Route path="files" element={<div data-testid="outlet-files">Files Content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('PipelineLayout', () => {
  it('renders 5 tabs', () => {
    renderLayout();
    expect(screen.getByText('Overview')).toBeInTheDocument();
    expect(screen.getByText('Schema')).toBeInTheDocument();
    expect(screen.getByText('Code')).toBeInTheDocument();
    expect(screen.getByText('Executions')).toBeInTheDocument();
    expect(screen.getByText('Files')).toBeInTheDocument();
  });

  it('renders pipeline name in header', () => {
    renderLayout();
    expect(screen.getByText('Test Pipeline')).toBeInTheDocument();
  });

  it('renders Outlet content for index route', () => {
    renderLayout('/pipelines/test-pipe');
    expect(screen.getByTestId('outlet-overview')).toBeInTheDocument();
  });

  it('renders Outlet content for schema route', () => {
    renderLayout('/pipelines/test-pipe/schema');
    expect(screen.getByTestId('outlet-schema')).toBeInTheDocument();
  });

  it('renders back button', () => {
    renderLayout();
    expect(screen.getByText('Back to Pipelines')).toBeInTheDocument();
  });
});
