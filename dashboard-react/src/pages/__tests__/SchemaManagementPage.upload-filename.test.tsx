// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 8.6 — R4.3: "Upload with preview" keeps the user's filename.
 *
 * **Expectations come from R4.3, not from current output.** R4.3:
 *
 * > THE Schemas_Page SHALL NOT substitute a fixed filename for the file a user
 * > chose.
 *
 * and R4.2:
 *
 * > WHEN a user uploads a schema through the Schemas_Page without supplying a
 * > Display_Label, THE Schemas_Page SHALL derive the schema's name from the
 * > selected file's basename, exactly as it does today
 *
 * ## The defect
 *
 * `handleSchemaReady` rebuilt the parsed schema as
 * `new File([schemaBlob], 'schema.json')` (`SchemaManagementPage.tsx:112`),
 * discarding the filename entirely. `uploadSchema` then derives the schema name
 * from `file.name` (`use-schemas.ts:111`), so **every** upload through "Upload
 * with preview" landed as `shared/schema_<suffix>.json` — distinct schemas
 * collapsed into one indistinguishable family, which is the "schema names are
 * confusing" complaint this spec starts from. R4.2's "exactly as it does today"
 * was true of the plain Upload button and false of this path.
 *
 * ## Why this asserts on `apiClient.post` rather than on a mocked hook
 *
 * The same reason `SchemaManagementPage.detail-modal.test.tsx` does: the API
 * boundary is the only thing stubbed, so the real `useUploadSchema`,
 * `uploadSchema` and its `FormData` → `{name, content}` conversion all run. A
 * `vi.mock('../../hooks')` factory would have asserted on the FormData this
 * component happens to build and proved nothing about what reaches the wire —
 * and it would have put this file in the way of held-assertion inventory row 5,
 * which task 8.7 owns.
 *
 * Requirements: 4.2, 4.3
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: {
    get: getMock,
    post: postMock,
    put: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import SchemaManagementPage from '../SchemaManagementPage';

// jsdom's File doesn't implement .text() — polyfill it, as
// components/__tests__/SchemaUploadWizard.test.tsx does.
beforeAll(() => {
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

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

const JSON_SCHEMA = JSON.stringify({
  properties: {
    customer_id: { type: 'integer', description: 'Primary key' },
    email: { type: 'string', description: 'Contact' },
  },
  required: ['customer_id'],
});

/**
 * Open the preview wizard and choose `file` in **its** Cloudscape FileUpload.
 *
 * The query is scoped to the wizard's own `schema-file-upload` container on
 * purpose. `document.querySelector('input[type="file"]')` finds the page's
 * *hidden* input first — the one behind the plain "Upload schema" button
 * (`SchemaManagementPage.tsx`, in the Table header's actions), which is earlier
 * in DOM order and drives `handleUpload`, a different code path that already
 * preserved the filename. The first draft of this file did exactly that and
 * three of its four cases passed against the unfixed component.
 */
async function uploadWithPreview(file: File) {
  fireEvent.click(screen.getByTestId('upload-with-preview-button'));
  const uploadArea = await screen.findByTestId('schema-file-upload');
  const input = uploadArea.querySelector('input[type="file"]') as HTMLInputElement;
  expect(input).toBeTruthy();
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    fireEvent.change(input);
  });
}

/** The body of the one `POST /schemas/upload` call, once it has been made. */
async function uploadedBody(): Promise<{ name: string; content: string }> {
  await waitFor(() => {
    expect(postMock.mock.calls.find(([url]) => url === '/schemas/upload')).toBeDefined();
  });
  const call = postMock.mock.calls.find(([url]) => url === '/schemas/upload')!;
  return call[1] as { name: string; content: string };
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return { schemas: [] };
    throw new Error(`unexpected GET ${path}`);
  });
  postMock.mockResolvedValue({
    message: 'Schema uploaded successfully',
    schemaId: 'customers_1785844048926',
    location: 's3://bucket/shared/customers_1785844048926.json',
  });
});

describe('Schemas_Page "Upload with preview" — R4.3, the chosen filename survives', () => {
  it("uploads under the chosen file's basename rather than a fixed 'schema.json'", async () => {
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('Schema Management');

    await uploadWithPreview(new File([JSON_SCHEMA], 'customers.json', { type: 'application/json' }));

    const body = await uploadedBody();
    expect(body.name).toBe('customers');
    expect(body.name).not.toBe('schema');
  });

  it('preserves the basename of a CSV the wizard parsed into fields', async () => {
    // The wizard accepts CSV and rebuilds it as a JSON Schema document, so the
    // extension legitimately changes. The *basename* is the part R4.3 is about.
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('Schema Management');

    await uploadWithPreview(
      new File(['customer_id,email\n1,a@example.com'], 'orders_2026.csv', { type: 'text/csv' }),
    );

    const body = await uploadedBody();
    expect(body.name).toBe('orders_2026');
  });

  it('uploads the parsed schema document, not the raw file', async () => {
    // Guards the other half of `handleSchemaReady`: preserving the filename must
    // not turn this into a pass-through of the user's bytes, because the wizard's
    // inline type and description edits live in the rebuilt document.
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('Schema Management');

    await uploadWithPreview(new File([JSON_SCHEMA], 'customers.json', { type: 'application/json' }));

    const parsed = JSON.parse((await uploadedBody()).content);
    expect(Object.keys(parsed.properties)).toEqual(['customer_id', 'email']);
    expect(parsed.required).toEqual(['customer_id']);
  });

  it('falls back to a stable name when no filename reached the page', async () => {
    // The "Use This Schema" button can commit fields the parent has already been
    // told about; a name is still owed. `schema` is that fallback, and it is the
    // only case where the pre-p35 behaviour is still correct.
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('Schema Management');

    await uploadWithPreview(new File([JSON_SCHEMA], '.json', { type: 'application/json' }));

    expect((await uploadedBody()).name).toBe('schema');
  });
});
