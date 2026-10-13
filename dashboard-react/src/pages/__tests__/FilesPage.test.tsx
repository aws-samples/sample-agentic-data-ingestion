// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

vi.mock('../../hooks', () => ({
  useFileDetail: () => ({ data: undefined, isLoading: false }),
}));

import FilesPage from '../FilesPage';

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipe',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
  schema_history: [],
  all_files: [
    { file_key: 'uploads/data.csv', pipeline_key: 'test-pipe', processed_at: '2024-06-01T10:00:00Z', execution_id: 'e1' },
    { file_key: 'uploads/report.xlsx', pipeline_key: 'test-pipe', processed_at: '2024-06-02T10:00:00Z' },
    { file_key: 'uploads/config.json', pipeline_key: 'test-pipe', processed_at: '2024-06-03T10:00:00Z' },
  ],
};

function renderPage(pipeline = mockPipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
        <FilesPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

describe('FilesPage', () => {
  it('renders uploaded files in the table', () => {
    renderPage();
    expect(screen.getByText('data.csv')).toBeInTheDocument();
    expect(screen.getByText('report.xlsx')).toBeInTheDocument();
  });

  it('lists JSON uploads instead of filtering them out', () => {
    renderPage();
    expect(screen.getByText('config.json')).toBeInTheDocument();
  });

  it('opens FileDetailModal on file click', () => {
    renderPage();
    fireEvent.click(screen.getByText('data.csv'));
    // Modal should be rendered (FileDetailModal with visible=true)
    expect(screen.getByText('File Details')).toBeInTheDocument();
  });

  it('shows empty state when no files', () => {
    const emptyPipeline = { ...mockPipeline, all_files: [] };
    renderPage(emptyPipeline);
    expect(screen.getByText('No files processed yet')).toBeInTheDocument();
  });
});
