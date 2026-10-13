// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// P29 R11.1 — the inline schema-upload flow must compute the uploaded object
// name EXACTLY ONCE and reuse it for both the /schemas/upload request and the
// fallback id sent on to /pipelines/provision.
//
// The pre-P29 bug called `Date.now()` twice, so when the upload response
// omitted a schema id the provision call referenced a DIFFERENT name than the
// object actually uploaded. This test drives the wizard to submit an inline
// schema (upload response carries no id → fallback path), with `Date.now`
// mocked to return a fresh value on every call. With the bug the two payloads
// diverge; with the fix (one captured value) they match.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';
import { INITIAL_WIZARD_STATE, type WizardState } from '../wizard/types';

vi.mock('../../hooks', () => ({
  usePipelines: () => ({ data: { pipelines: [] }, isLoading: false }),
  useBedrockModels: () => ({
    data: [{ model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' }],
    isLoading: false,
  }),
  useSchemas: () => ({ data: [], isLoading: false }),
  useS3TablesBuckets: () => ({ data: [], isLoading: false }),
  useTablesInBucket: () => ({ data: [], isLoading: false }),
  useGlueDatabases: () => ({ data: [], isLoading: false }),
  useTablesInDatabase: () => ({ data: [], isLoading: false }),
}));

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

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
}

// A complete wizard state that exercises the inline-schema upload branch:
// no existing schema id + non-empty schemaFields, S3 Tables target (no
// navigation guards), model chosen.
const seededState: WizardState = {
  ...INITIAL_WIZARD_STATE,
  pipelineName: 'inline-schema-pipeline',
  description: 'test',
  targetType: 's3_tables',
  targetConfig: { bucket_name: 'b', table_name: 't', namespace: 'default' },
  schemaSource: 'upload',
  schemaFields: [
    { name: 'id', type: 'string', nullable: false, description: '' },
    { name: 'amount', type: 'double', nullable: true, description: '' },
  ],
  existingSchemaId: null,
  aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  postMock.mockReset();
  // /schemas/upload returns NO schema id → forces the fallback-name path,
  // which is where the pre-P29 second Date.now() diverged. /pipelines/provision
  // resolves successfully.
  postMock.mockImplementation((url: string) => {
    if (url === '/schemas/upload') return Promise.resolve({});
    return Promise.resolve({ success: true });
  });

  // Deterministic, strictly-increasing clock: every Date.now() call returns a
  // distinct value, so a second computation would produce a different name.
  let tick = 1_000;
  vi.spyOn(Date, 'now').mockImplementation(() => (tick += 1));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CreatePipelinePage — inline schema upload object name (P29 R11.1)', () => {
  it('uploads and provisions against the SAME computed schema name', async () => {
    // Seed a full draft so we can restore a state with inline schema fields
    // without hand-driving the schema step UI.
    sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(seededState));
    renderPage();

    // Restore rehydrates the inline-schema state, then walk to Review.
    fireEvent.click(screen.getByTestId('wizard-draft-restore-button'));
    clickNext(); // Name → Source
    clickNext(); // Source → Target
    clickNext(); // Target → Schema
    clickNext(); // Schema → Model
    clickNext(); // Model → Review

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
    });

    const uploadCall = postMock.mock.calls.find(([url]) => url === '/schemas/upload');
    const provisionCall = postMock.mock.calls.find(([url]) => url === '/pipelines/provision');

    expect(uploadCall).toBeDefined();
    expect(provisionCall).toBeDefined();

    const uploadedName = (uploadCall![1] as { name: string }).name;
    const provisionedSchema = (provisionCall![1] as { target_schema: string }).target_schema;

    // The name uploaded under and the name referenced downstream are identical.
    expect(uploadedName).toMatch(/^uploaded_schema_\d+$/);
    expect(provisionedSchema).toBe(uploadedName);
  });
});
