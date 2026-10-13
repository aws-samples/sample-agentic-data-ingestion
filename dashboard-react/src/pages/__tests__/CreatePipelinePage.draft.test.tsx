// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';

/**
 * P29 R5.2 / R5.3 — Create Pipeline Wizard draft persistence wired into the
 * container.
 *
 * Verifies the sessionStorage-backed Wizard_Draft end to end:
 *  - save/restore round-trip: work typed into the wizard is persisted and,
 *    on a fresh mount (as happens after the Cognito Hosted UI redirect), the
 *    Restore/Discard prompt appears and Restore rehydrates the input.
 *  - the draft is cleared on a successful submit.
 *  - the draft is cleared on explicit cancel.
 */

// Wizard step components read resource hooks; stub them like the sibling
// name-gating test so the wizard renders in jsdom.
vi.mock('../../hooks', () => ({
  usePipelines: () => ({ data: { pipelines: [] }, isLoading: false }),
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

// The container's submit posts to the API client — stub it so submit resolves.
const { postMock } = vi.hoisted(() => ({ postMock: vi.fn() }));
vi.mock('../../services/api-client', () => ({
  apiClient: { post: postMock, get: vi.fn() },
  ApiError: class ApiError extends Error {},
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

function nameInput(): HTMLInputElement {
  return screen.getByPlaceholderText('my-etl-pipeline') as HTMLInputElement;
}

function typeName(name: string) {
  fireEvent.change(nameInput(), { target: { value: name } });
}

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  postMock.mockReset();
  postMock.mockResolvedValue({ success: true });
});

describe('CreatePipelinePage — wizard draft persistence (P29 R5.2/R5.3)', () => {
  it('round-trips a draft: persisted work is offered for restore on remount', () => {
    const first = renderPage();

    // No restore prompt on a clean first visit.
    expect(screen.queryByTestId('wizard-draft-restore')).not.toBeInTheDocument();

    typeName('my-wip-pipeline');

    // Simulate the wizard being torn down mid-flow (e.g. the Cognito redirect
    // navigates the tab away). The hook flushes the latest state on unmount.
    first.unmount();
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).not.toBeNull();

    // Fresh mount (same tab, post-redirect): the draft is offered.
    renderPage();
    expect(screen.getByTestId('wizard-draft-restore')).toBeInTheDocument();
    // The wizard starts empty until the user restores.
    expect(nameInput().value).toBe('');

    // Restore rehydrates the in-progress work.
    fireEvent.click(screen.getByTestId('wizard-draft-restore-button'));
    expect(nameInput().value).toBe('my-wip-pipeline');
    // Prompt dismissed once resolved.
    expect(screen.queryByTestId('wizard-draft-restore')).not.toBeInTheDocument();
  });

  it('discards a draft when the user chooses Discard', () => {
    // Seed a prior-session draft so the prompt appears on mount.
    sessionStorage.setItem(
      WIZARD_DRAFT_KEY,
      JSON.stringify({ pipelineName: 'old-draft', description: 'x' }),
    );
    renderPage();

    expect(screen.getByTestId('wizard-draft-restore')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('wizard-draft-discard-button'));

    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
    expect(screen.queryByTestId('wizard-draft-restore')).not.toBeInTheDocument();
    // The wizard state is untouched (still empty) — discard does not restore.
    expect(nameInput().value).toBe('');
  });

  it('clears the draft on a successful submit', async () => {
    // A draft exists (e.g. saved earlier this session).
    sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify({ pipelineName: 'seed' }));
    renderPage();

    // Fill a valid name and walk the wizard to the review step (defaults use
    // the S3 Tables target, which has no navigation guards).
    typeName('brand-new-pipeline');
    clickNext(); // Name → Source
    clickNext(); // Source → Target
    clickNext(); // Target → Schema
    clickNext(); // Schema → Model
    clickNext(); // Model → Review

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
    });

    expect(postMock).toHaveBeenCalledWith('/pipelines/provision', expect.any(Object));
    // Submit succeeded → the persisted draft is gone.
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
  });

  it('clears the draft on explicit cancel', () => {
    sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify({ pipelineName: 'seed' }));
    renderPage();

    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));

    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
  });
});
