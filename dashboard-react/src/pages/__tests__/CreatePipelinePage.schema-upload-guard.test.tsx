// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Step 4 (Target Schema) upload-mode gating, driven through the real upload UI.
//
// Companion to CreatePipelinePage.schema-upload.test.tsx, which seeds
// `schemaFields` from a sessionStorage draft and therefore never touches the
// upload widget. That entry point is still covered there; this file covers the
// one a user actually walks: pick the Upload tile, choose a file, continue.
//
// The defect these tests pin: a parsed schema lived only in
// SchemaUploadWizard's local state, so Review read "Schema Source: Uploaded /
// Fields: 0 fields" and Create returned 400 "target_schema is required".
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';

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

const BLOCKED_MESSAGE = 'Upload a schema file with at least one field before continuing.';

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

/**
 * Name → Source → Target → Schema. S3 Tables target has no step-2 guard.
 * Step titles appear twice (nav pane + step header), so the schema step is
 * identified by its content instead: the schema-source tiles.
 */
function walkToSchemaStep() {
  fireEvent.change(screen.getByPlaceholderText('my-etl-pipeline'), {
    target: { value: 'upload-schema-pipeline' },
  });
  clickNext(); // Name → Source
  clickNext(); // Source → Target
  clickNext(); // Target → Schema
  expect(screen.getByTestId('schema-source-tiles')).toBeInTheDocument();
}

/** True while the Target Schema step is still the active step. */
function onSchemaStep(): boolean {
  return screen.queryByTestId('schema-source-tiles') !== null;
}

function selectUploadTile() {
  const tiles = screen.getByTestId('schema-source-tiles');
  fireEvent.click(within(tiles).getByRole('radio', { name: /^Upload/ }));
  return screen.getByTestId('schema-file-upload');
}

async function uploadFile(file: File) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    fireEvent.change(input);
  });
}

function jsonFile(content: unknown, name = 'schema.json'): File {
  return new File([typeof content === 'string' ? content : JSON.stringify(content)], name, {
    type: 'application/json',
  });
}

const VALID_SCHEMA = {
  properties: {
    order_id: { type: 'integer', description: 'Order id' },
    total: { type: 'number', description: '' },
  },
  required: ['order_id'],
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  postMock.mockReset();
  postMock.mockResolvedValue({ success: true });
  // jsdom's File doesn't implement .text().
  if (!File.prototype.text) {
    File.prototype.text = function () {
      return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.readAsText(this);
      });
    };
  }
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CreatePipelinePage — Step 4 upload-mode schema gating', () => {
  it('carries an uploaded schema through to Review with a non-zero field count', async () => {
    renderPage();
    walkToSchemaStep();
    selectUploadTile();

    await uploadFile(jsonFile(VALID_SCHEMA));
    await waitFor(() => {
      expect(screen.getByTestId('use-schema-button')).toBeInTheDocument();
    });

    clickNext(); // Schema → Model
    clickNext(); // Model → Review

    expect(onSchemaStep()).toBe(false);
    expect(screen.getByText('Uploaded')).toBeInTheDocument();
    expect(screen.getByText('2 fields')).toBeInTheDocument();
    expect(screen.getByTestId('review-schema-table')).toBeInTheDocument();
    expect(screen.getByText('order_id')).toBeInTheDocument();
  });

  it('blocks Next in upload mode with zero fields and shows the reason as text', () => {
    renderPage();
    walkToSchemaStep();
    selectUploadTile();

    // Nothing uploaded yet — the reason is not shown until the mode itself
    // makes Next impossible, which is immediately on entering upload mode.
    clickNext();

    // Still on the schema step, with the upload widget and the reason visible.
    expect(onSchemaStep()).toBe(true);
    expect(screen.getByTestId('schema-file-upload')).toBeInTheDocument();
    expect(screen.getByText(BLOCKED_MESSAGE)).toBeInTheDocument();
  });

  it('does not advance or populate fields when the parse fails', async () => {
    renderPage();
    walkToSchemaStep();
    selectUploadTile();

    await uploadFile(jsonFile('not valid json {{{'));
    await waitFor(() => {
      expect(screen.getByTestId('schema-parse-error')).toBeInTheDocument();
    });
    // A failed parse leaves nothing to commit.
    expect(screen.queryByTestId('use-schema-button')).not.toBeInTheDocument();

    clickNext();

    expect(onSchemaStep()).toBe(true);
    expect(screen.getByTestId('schema-file-upload')).toBeInTheDocument();
    expect(screen.getByText(BLOCKED_MESSAGE)).toBeInTheDocument();
  });

  it('reaching Review after an upload sends a non-empty target_schema', async () => {
    renderPage();
    walkToSchemaStep();
    selectUploadTile();

    await uploadFile(jsonFile(VALID_SCHEMA));
    await waitFor(() => {
      expect(screen.getByTestId('use-schema-button')).toBeInTheDocument();
    });

    clickNext(); // Schema → Model
    clickNext(); // Model → Review

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
    });

    const provisionCall = postMock.mock.calls.find(([url]) => url === '/pipelines/provision');
    expect(provisionCall).toBeDefined();
    const body = provisionCall![1] as { target_schema: string; schema_source: string };
    // The 400 "target_schema is required" case was an empty string here.
    expect(body.target_schema).not.toBe('');
    expect(body.schema_source).toBe('upload');
  });
});
