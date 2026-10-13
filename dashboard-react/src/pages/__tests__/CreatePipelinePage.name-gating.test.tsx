// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';

/**
 * P29 R5.1 — Step 1 (Name) per-step validation gating.
 *
 * The Create Pipeline Wizard must block forward navigation from Step 1 when
 * the pipeline name is empty OR a case-insensitive duplicate of an existing
 * pipeline, surfacing the error inline at Step 1 (not as a submit-time toast
 * on the review step). A valid, unique name advances.
 */

// `usePipelines` return is varied per test to exercise duplicate detection.
const { mockUsePipelines } = vi.hoisted(() => ({ mockUsePipelines: vi.fn() }));

vi.mock('../../hooks', () => ({
  usePipelines: mockUsePipelines,
  useBedrockModels: () => ({
    data: [{ model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' }],
    isLoading: false,
  }),
  useSchemas: () => ({
    data: [
      { schema_id: 's1', schema_name: 'Test Schema', source_type: 'uploaded', fields: [], created_at: '2024-01-01' },
    ],
    isLoading: false,
  }),
  useS3TablesBuckets: () => ({ data: [], isLoading: false }),
  useTablesInBucket: () => ({ data: [], isLoading: false }),
  useGlueDatabases: () => ({ data: [], isLoading: false }),
  useTablesInDatabase: () => ({ data: [], isLoading: false }),
}));

import CreatePipelinePage from '../CreatePipelinePage';

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/new']}>
          <CreatePipelinePage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function typeName(name: string) {
  fireEvent.change(screen.getByPlaceholderText('my-etl-pipeline'), {
    target: { value: name },
  });
}

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
}

describe('CreatePipelinePage — Step 1 name validation gating (P29 R5.1)', () => {
  beforeEach(() => {
    mockUsePipelines.mockReset();
    mockUsePipelines.mockReturnValue({ data: { pipelines: [] }, isLoading: false });
  });

  it('blocks Next on an empty name and shows an inline required error', () => {
    renderPage();

    // No inline error before the user tries to advance.
    expect(screen.queryByText('Pipeline name is required')).not.toBeInTheDocument();

    clickNext();

    // Still on Step 1 (the name FormField label is unique to step 1 content),
    // and the Source step content has NOT rendered.
    expect(screen.getByText('Pipeline Name')).toBeInTheDocument();
    expect(screen.queryByText('Source Type')).not.toBeInTheDocument();
    // Inline error surfaced at the step — not a submit-time toast.
    expect(screen.getByText('Pipeline name is required')).toBeInTheDocument();
  });

  it('blocks Next on a duplicate name (case-insensitive) with an inline error', () => {
    mockUsePipelines.mockReturnValue({
      data: { pipelines: [{ pipeline_name: 'existing-pipeline' }] },
      isLoading: false,
    });
    renderPage();

    // Different case still counts as a duplicate.
    typeName('Existing-Pipeline');
    clickNext();

    expect(screen.getByText('Pipeline Name')).toBeInTheDocument();
    expect(screen.queryByText('Source Type')).not.toBeInTheDocument();
    expect(screen.getByText(/already exists/i)).toBeInTheDocument();
  });

  it('advances to the Source step for a valid, unique name', () => {
    mockUsePipelines.mockReturnValue({
      data: { pipelines: [{ pipeline_name: 'existing-pipeline' }] },
      isLoading: false,
    });
    renderPage();

    typeName('brand-new-pipeline');
    clickNext();

    // Advanced to Step 2 (Source) — no inline name error remains.
    expect(screen.getByText('Source Type')).toBeInTheDocument();
    expect(screen.queryByText('Pipeline name is required')).not.toBeInTheDocument();
    expect(screen.queryByText(/already exists/i)).not.toBeInTheDocument();
  });
});
