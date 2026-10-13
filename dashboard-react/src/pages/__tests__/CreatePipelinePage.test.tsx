// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';

// Mock hooks used by wizard steps
vi.mock('../../hooks', () => ({
  // P29 R5.1 — Step 1 duplicate-name detection reads the pipelines list.
  usePipelines: () => ({ data: { pipelines: [] }, isLoading: false }),
  useBedrockModels: () => ({
    data: [
      { model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' },
    ],
    isLoading: false,
  }),
  useSchemas: () => ({
    data: [
      { schema_id: 's1', schema_name: 'Test Schema', source_type: 'uploaded', fields: [], created_at: '2024-01-01' },
    ],
    isLoading: false,
  }),
  // WizardStepTarget renders the S3 Tables sub-form by default, which calls
  // these resource hooks.
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

// P29 R5.1 — Step 1 now gates Next on a valid pipeline name, so navigation
// tests must supply a valid name before advancing past the first step.
function fillValidName(name = 'valid-pipeline') {
  fireEvent.change(screen.getByPlaceholderText('my-etl-pipeline'), {
    target: { value: name },
  });
}

describe('CreatePipelinePage', () => {
  it('renders the wizard with 5 step titles', () => {
    renderPage();
    // Step titles appear in the wizard navigation — use getAllByText since they may appear multiple times
    expect(screen.getAllByText('Name & Description').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Target Destination').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Target Schema').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('AI Model & Options').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Review & Create').length).toBeGreaterThanOrEqual(1);
  });

  it('shows step 1 content by default', () => {
    renderPage();
    expect(screen.getByText('Pipeline Name')).toBeInTheDocument();
  });

  it('navigates to step 2 (Source) when Next is clicked', () => {
    renderPage();
    fillValidName();
    const nextButton = screen.getByRole('button', { name: /next/i });
    fireEvent.click(nextButton);
    expect(screen.getByText('Source Type')).toBeInTheDocument();
  });

  it('navigates back to step 1 when Previous is clicked from step 2', () => {
    renderPage();
    fillValidName();
    // Go to step 2 (Source)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('Source Type')).toBeInTheDocument();
    // Go back to step 1
    fireEvent.click(screen.getByRole('button', { name: /previous/i }));
    expect(screen.getByText('Pipeline Name')).toBeInTheDocument();
  });

  it('can navigate through all 6 steps', () => {
    renderPage();
    fillValidName();
    // Step 1 (Name) → 2 (Source)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('Source Type')).toBeInTheDocument();
    // Step 2 (Source) → 3 (Target)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('Target Type')).toBeInTheDocument();
    // Step 3 (Target) → 4 (Schema)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('Schema Source')).toBeInTheDocument();
    // Step 4 (Schema) → 5 (Model)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('AI Model')).toBeInTheDocument();
    // Step 5 (Model) → 6 (Review)
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    expect(screen.getByText('Pipeline Details')).toBeInTheDocument();
  });
});
