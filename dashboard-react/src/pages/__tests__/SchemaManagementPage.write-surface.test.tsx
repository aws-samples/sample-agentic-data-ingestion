// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 8.7 (design C4) — the write surface's frontend.
 *
 * Expectations come from **R2.1, R2.2, R3.4, R3.6, R3.9, R4.1, R4.2, R4.8 and
 * R4.9**, and the fixtures come from the response shapes tasks 8.4–8.6 actually
 * landed, read out of `handlers/schemas.py` rather than invented:
 *
 * | Route | Outcome | Shape |
 * |---|---|---|
 * | `DELETE /api/schemas/{id}` | `409` | `{error, reason: "schema_in_use", schemaId, checked, referencingPipelines: [{pipelineId, pipelineName}]}` (`:2069-2072`) |
 * | | `400` | `{error, reason: "catalog_derived_schema", schemaId, source}` — **no `checked`** (`:2029-2031`) |
 * | | `503` | `{error, reason: "reference_check_incomplete", retryable: true, checked, schemaId, details}` (`:2049-2053`) |
 * | `PUT /api/schemas/{id}/display-label` | `200` | `{message, schemaId, displayLabel}` or, cleared, `{message, schemaId, cleared: true}` with **no `displayLabel` key** (`:2486-2502`) |
 * | `POST /api/schemas/upload` | `200` | `{message, schemaId, location, displayLabel?}`, plus `displayLabelError` when the body stored and the label write did not (`:1626-1643`) |
 *
 * ## Why the three delete failures get three tests rather than one parametrised one
 *
 * Design §5 risk 6: `useDeleteSchema` had no `onError` at all, so every failure
 * was one fixed toast — and of the three refusals the `503` is **the branch with
 * no user story behind it**, therefore the one most likely to be collapsed into a
 * generic error during implementation. Collapsing it puts the page back to
 * "always toasts Failed to delete schema" for the one case where the correct
 * answer is *retry*. So each branch is asserted on the thing that distinguishes
 * it: the `409` on the **pipeline names** (R3.9 wants the pipelines, not a
 * count), the `400` on the catalog source, and the `503` on the words "try
 * again" plus a retry control that actually re-issues the request.
 *
 * ## What this file mocks, and why not `../../hooks`
 *
 * `apiClient` only — so the real `useDeleteSchema`, `useUpdateFieldContext`,
 * `useSetDisplayLabel`, `useUploadSchema` and the real `ApiError` classification
 * all run. That is the point: the symbol graph flags every one of those hooks
 * "no covering tests found", because `src/__tests__/pages.test.tsx` replaces the
 * whole hooks module with `vi.fn()` stubs. It also keeps this file clear of
 * held-assertion inventory row 5's hazard — a `vi.mock('../../hooks')` factory
 * makes any hook it omits `undefined`, and the render then throws a `TypeError`
 * instead of failing an assertion. Row 5's own amendment lives in that file and
 * was made **before** the rename hook was added.
 *
 * `ApiError` is imported from the real module rather than stubbed, because
 * `classifySchemaDeleteFailure` reads `status` and `body` off it and a stub class
 * carries neither.
 *
 * Requirements: 2.1, 2.2, 3.4, 3.6, 3.9, 4.1, 4.2, 4.8, 4.9
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
const putMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
const deleteMock = vi.hoisted(() => vi.fn());

// The real module is kept and only `apiClient` is replaced, so `ApiError` below
// is the genuine class with `status` and `body` on it.
vi.mock('../../services/api-client', async () => {
  const actual = await vi.importActual<typeof import('../../services/api-client')>(
    '../../services/api-client',
  );
  return {
    ...actual,
    apiClient: { get: getMock, post: postMock, put: putMock, delete: deleteMock },
  };
});

import { ApiError } from '../../services/api-client';
import SchemaManagementPage from '../SchemaManagementPage';

// jsdom's File does not implement `.text()`, and `uploadSchema` awaits it before
// it can POST — without this the upload cases fail with the mutation rejecting
// and `apiClient.post` never called, which looks like a missing request rather
// than a missing polyfill. Same shim as
// `SchemaManagementPage.upload-filename.test.tsx` and
// `components/__tests__/SchemaUploadWizard.test.tsx`.
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

/* ------------------------------------------------------------------ */
/*  Fixtures — the post-task-4.4 listing shape                         */
/* ------------------------------------------------------------------ */

/** An uploaded row: deletable and renameable (R3.4). */
const UPLOADED_ROW = {
  schemaId: 'uploaded_schema_1785844048926',
  name: 'uploaded_schema_1785844048926',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-04T11:47:29+00:00',
  version: '1.0',
  fieldCount: null,
};

/** A second uploaded row, this one already carrying a Display_Label (R4.8). */
const LABELLED_ROW = {
  schemaId: 'uploaded_schema_1785844048999',
  name: 'uploaded_schema_1785844048999',
  displayLabel: 'Customer master',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-05T09:00:00+00:00',
  version: '1.0',
  fieldCount: null,
};

/** A catalog-derived row: the API refuses every write route for it. */
const GLUE_ROW = {
  schemaId: 'glue_salesdb_orders',
  name: 'salesdb.orders',
  source: 'glue_catalog',
  targetType: 'Glue Catalog',
  lastModified: '2026-08-03T10:00:00+00:00',
  version: '1.0',
  fields: [{ name: 'order_id', type: 'string', nullable: true, description: '', context: '' }],
  fieldCount: 1,
};

const S3_TABLES_ROW = {
  schemaId: 's3_table_testingbucket_ns_tn',
  name: 'testingbucket.ns.tn',
  source: 's3_table_bucket',
  targetType: 'S3 Tables',
  lastModified: '2026-08-03T11:00:00+00:00',
  version: '1.0',
  fieldCount: null,
};

const LISTING = {
  schemas: [UPLOADED_ROW, LABELLED_ROW, GLUE_ROW, S3_TABLES_ROW],
  sourcesFailed: [],
};

/** DD9's bare per-id object for the uploaded row, with one annotatable field. */
const UPLOADED_DETAIL = {
  schemaId: UPLOADED_ROW.schemaId,
  name: UPLOADED_ROW.name,
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: UPLOADED_ROW.lastModified,
  fields: [{ name: 'email', type: 'string', nullable: false, description: 'Contact', context: '' }],
};

const GLUE_DETAIL = {
  schemaId: GLUE_ROW.schemaId,
  name: GLUE_ROW.name,
  source: 'glue_catalog',
  targetType: 'Glue Catalog',
  lastModified: GLUE_ROW.lastModified,
  fields: GLUE_ROW.fields,
};

const DETAILS: Record<string, unknown> = {
  [UPLOADED_ROW.schemaId]: UPLOADED_DETAIL,
  [LABELLED_ROW.schemaId]: { ...UPLOADED_DETAIL, schemaId: LABELLED_ROW.schemaId, name: LABELLED_ROW.name, displayLabel: LABELLED_ROW.displayLabel },
  [GLUE_ROW.schemaId]: GLUE_DETAIL,
};

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function routeGet() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return LISTING;
    const match = /^\/schemas\/(.+)$/.exec(path);
    if (match) {
      const detail = DETAILS[decodeURIComponent(match[1])];
      if (detail) return detail;
    }
    throw new ApiError(404, { error: `no such schema: ${path}` });
  });
}

/** The row whose Schema name cell reads `name`. */
function rowFor(name: string): HTMLElement {
  const cell = screen.getByText(name);
  const row = cell.closest('tr');
  if (!row) throw new Error(`no listing row showing "${name}"`);
  return row as HTMLElement;
}

/** Open the delete dialog for the row showing `name`. */
async function openDeleteDialog(name: string) {
  fireEvent.click(within(rowFor(name)).getByRole('button', { name: `Delete schema ${name}` }));
  return screen.findByRole('dialog');
}

/** Confirm the delete and wait for the dialog to settle. */
async function confirmDelete(dialog: HTMLElement) {
  fireEvent.click(within(dialog).getByText('Delete'));
  await waitFor(() => expect(deleteMock).toHaveBeenCalled());
}

async function renderPage() {
  routeGet();
  render(<SchemaManagementPage />, { wrapper });
  await screen.findByText(UPLOADED_ROW.name);
}

beforeEach(() => {
  getMock.mockReset();
  putMock.mockReset();
  postMock.mockReset();
  deleteMock.mockReset();
});

/* ================================================================== */
/*  R3.6 / R3.9 — three distinct delete failures, not one              */
/* ================================================================== */

describe('Delete refusals — R3.6 and R3.9, the reason the API gave and not a fixed message', () => {
  /**
   * R3.9: "WHEN a delete is refused under 3.7, THE Schemas_Page SHALL present the
   * referencing pipelines the Schemas_API named, so that the user can act on them
   * rather than only being told no."
   *
   * So the assertion is on the pipeline **names**, not on a count and not merely
   * on the presence of an error. A count is what tells the user there is an
   * obstacle without telling them which one.
   */
  it('renders the referencing pipelines the 409 named, by name and id', async () => {
    await renderPage();
    deleteMock.mockRejectedValue(
      new ApiError(409, {
        error: `Schema ${UPLOADED_ROW.schemaId} is referenced by 2 pipeline(s) and was not deleted. Update or archive them first.`,
        reason: 'schema_in_use',
        schemaId: UPLOADED_ROW.schemaId,
        checked: ['target_schema_name-index query'],
        referencingPipelines: [
          { pipelineId: 'testing-39eddb17', pipelineName: 'nightly orders' },
          { pipelineId: 'hello123-c4827272', pipelineName: 'hello123-c4827272' },
        ],
      }),
    );

    const dialog = await openDeleteDialog(UPLOADED_ROW.name);
    await confirmDelete(dialog);

    const refusal = await screen.findByTestId('delete-refused-in-use');
    // The pipelines themselves, which is what the user has to act on.
    expect(within(refusal).getByText('nightly orders')).toBeInTheDocument();
    expect(within(refusal).getByText(/testing-39eddb17/)).toBeInTheDocument();
    expect(within(refusal).getByText('hello123-c4827272')).toBeInTheDocument();
    // The API's own sentence, not one composed here (R3.6).
    expect(within(refusal).getByText(/Update or archive them first/)).toBeInTheDocument();
    // Distinct from the other two branches.
    expect(screen.queryByTestId('delete-refused-catalog-derived')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-check-incomplete')).not.toBeInTheDocument();
    // Retrying cannot change the answer, so Delete is not re-offered.
    expect(screen.queryByTestId('delete-retry')).not.toBeInTheDocument();
  });

  /**
   * R3.3's refusal as the page renders it. Reachable even with the row action
   * gated on `source_type` (R3.4, asserted separately below) — a hand-issued
   * request or a row whose source type was unknown still has to read correctly.
   */
  it('renders the 400 for a catalog-derived schema, naming the catalog that owns it', async () => {
    await renderPage();
    deleteMock.mockRejectedValue(
      new ApiError(400, {
        error: `Schema ${GLUE_ROW.schemaId} is a view of a catalog object (glue_catalog) and is not deletable through this endpoint.`,
        reason: 'catalog_derived_schema',
        schemaId: GLUE_ROW.schemaId,
        source: 'glue_catalog',
      }),
    );

    // The row offers no delete control (R3.4), so drive the same classification
    // through the dialog opened for an uploaded row. The branch under test is the
    // rendering of the response, which is what R3.6 constrains.
    const dialog = await openDeleteDialog(UPLOADED_ROW.name);
    await confirmDelete(dialog);

    const refusal = await screen.findByTestId('delete-refused-catalog-derived');
    // The header and the API's own sentence both say it, which is the point —
    // `getAllByText` rather than a narrower query that would pin which of the two
    // carries the phrase.
    expect(within(refusal).getAllByText(/is a view of a catalog object/).length).toBeGreaterThan(0);
    expect(within(refusal).getByText(/not deletable through this endpoint/)).toBeInTheDocument();
    // The catalog that owns it, which is the actionable part.
    expect(within(refusal).getByText('glue_catalog')).toBeInTheDocument();
    expect(within(refusal).getByText(/would destroy a pipeline's target/)).toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-in-use')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-check-incomplete')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-retry')).not.toBeInTheDocument();
  });

  /**
   * **Design §5 risk 6's branch.** The `503` has no user story behind it and is
   * therefore the one a generic-error collapse loses first — which would leave the
   * page saying "failed" for the one case where nothing failed, nothing was
   * deleted, and the answer is *retry*.
   *
   * Three separate things are asserted, because a collapse can lose any one of
   * them independently: that the branch is reachable and distinct, that it says
   * to try again, and that the retry control **re-issues the request**.
   */
  it('renders the 503 as its own branch, says to try again, and offers a retry that re-issues the delete', async () => {
    await renderPage();
    deleteMock.mockRejectedValue(
      new ApiError(503, {
        error: `Could not establish whether any pipeline references schema ${UPLOADED_ROW.schemaId}, so the delete was not attempted. Please try again.`,
        reason: 'reference_check_incomplete',
        retryable: true,
        checked: ['target_schema_name-index query'],
        schemaId: UPLOADED_ROW.schemaId,
        details: 'reference scan budget exhausted',
      }),
    );

    const dialog = await openDeleteDialog(UPLOADED_ROW.name);
    await confirmDelete(dialog);

    const refusal = await screen.findByTestId('delete-refused-check-incomplete');
    // "Try again" is said by the API's own sentence **and** by the page's own
    // guidance, and both matter — the point of design §5 risk 6 is that this
    // branch must not read as a generic failure.
    expect(within(refusal).getAllByText(/try again/i).length).toBeGreaterThan(0);
    // It must not read as a deletion that failed — nothing was deleted.
    expect(within(refusal).getByText(/The schema is untouched/)).toBeInTheDocument();
    // And the header says so before any of the prose does.
    expect(within(refusal).getByText(/Nothing was deleted/)).toBeInTheDocument();
    // Only the lookups that completed, which is what distinguishes "could not
    // check" from "checked and found nothing".
    expect(screen.getByTestId('delete-checked-lookups')).toHaveTextContent(
      'target_schema_name-index query',
    );
    // Not either of the other two.
    expect(screen.queryByTestId('delete-refused-in-use')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-catalog-derived')).not.toBeInTheDocument();

    // And the retry actually retries.
    const callsBefore = deleteMock.mock.calls.length;
    deleteMock.mockResolvedValue({
      message: 'Schema deleted',
      schemaId: UPLOADED_ROW.schemaId,
      checked: ['target_schema_name-index query', 'target_schema_s3_key scan'],
      referencingPipelines: [],
    });
    fireEvent.click(screen.getByTestId('delete-retry'));
    await waitFor(() => expect(deleteMock.mock.calls.length).toBe(callsBefore + 1));
    await waitFor(() =>
      expect(screen.queryByTestId('delete-refused-check-incomplete')).not.toBeInTheDocument(),
    );
  });

  /**
   * An unclassified failure is **not** folded into one of the three. The three
   * refusals all guarantee the object still exists — R3.10 orders the reference
   * check before the delete, so a refusal and a deletion are never both
   * observable — and a `500` guarantees nothing, which is worth saying rather than
   * dressing up as a refusal.
   */
  it('keeps an unclassified failure separate from the three refusals', async () => {
    await renderPage();
    deleteMock.mockRejectedValue(
      new ApiError(500, { error: 'Failed to delete schema', details: 'AccessDenied' }),
    );

    const dialog = await openDeleteDialog(UPLOADED_ROW.name);
    await confirmDelete(dialog);

    const failure = await screen.findByTestId('delete-failed');
    expect(within(failure).getByText('AccessDenied')).toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-in-use')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-catalog-derived')).not.toBeInTheDocument();
    expect(screen.queryByTestId('delete-refused-check-incomplete')).not.toBeInTheDocument();
  });

  it('reports a successful delete naming the schema, and closes the dialog', async () => {
    await renderPage();
    deleteMock.mockResolvedValue({
      message: `Schema ${UPLOADED_ROW.schemaId} deleted`,
      schemaId: UPLOADED_ROW.schemaId,
      checked: ['target_schema_name-index query', 'target_schema_s3_key scan'],
      referencingPipelines: [],
    });

    const dialog = await openDeleteDialog(UPLOADED_ROW.name);
    await confirmDelete(dialog);

    await waitFor(() =>
      expect(screen.getByText(new RegExp(`"${UPLOADED_ROW.name}" deleted successfully`))).toBeInTheDocument(),
    );
  });
});

/* ================================================================== */
/*  R3.4 — the delete action is offered only where the API will delete */
/* ================================================================== */

describe('R3.4 — the delete and rename controls are offered only for uploaded schemas', () => {
  it('offers delete and rename on an uploaded row and on neither catalog-derived row', async () => {
    await renderPage();

    // Uploaded: both.
    expect(
      within(rowFor(UPLOADED_ROW.name)).getByRole('button', {
        name: `Delete schema ${UPLOADED_ROW.name}`,
      }),
    ).toBeInTheDocument();
    expect(within(rowFor(UPLOADED_ROW.name)).getByText('Rename')).toBeInTheDocument();

    // glue_catalog: neither. Deleting it would destroy a pipeline's target, and
    // the API answers 400 on all three write routes.
    expect(within(rowFor(GLUE_ROW.name)).queryByText('Rename')).not.toBeInTheDocument();
    expect(
      within(rowFor(GLUE_ROW.name)).queryByRole('button', {
        name: `Delete schema ${GLUE_ROW.name}`,
      }),
    ).not.toBeInTheDocument();

    // s3_table_bucket: neither, same reason.
    expect(within(rowFor(S3_TABLES_ROW.name)).queryByText('Rename')).not.toBeInTheDocument();
    expect(
      within(rowFor(S3_TABLES_ROW.name)).queryByRole('button', {
        name: `Delete schema ${S3_TABLES_ROW.name}`,
      }),
    ).not.toBeInTheDocument();

    // Every row still offers the read action, which R3.4 does not restrict.
    expect(within(rowFor(GLUE_ROW.name)).getByText('View details')).toBeInTheDocument();
  });
});

/* ================================================================== */
/*  R4.9 / R4.8 — the rename control                                   */
/* ================================================================== */

describe('R4.9 — renaming a schema from the Schemas_Page', () => {
  it('sends the typed label to the display-label route and nowhere else', async () => {
    await renderPage();
    putMock.mockResolvedValue({
      message: 'Display label set',
      schemaId: UPLOADED_ROW.schemaId,
      displayLabel: 'Orders v2',
    });

    fireEvent.click(within(rowFor(UPLOADED_ROW.name)).getByText('Rename'));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByTestId('rename-input').querySelector('input')!, {
      target: { value: 'Orders v2' },
    });
    fireEvent.click(within(dialog).getByText('Save'));

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    // The route and the body the handler reads (`_string_field(event,
    // "displayLabel")`, `schemas.py:2466`).
    expect(putMock).toHaveBeenCalledWith(
      `/schemas/${UPLOADED_ROW.schemaId}/display-label`,
      { displayLabel: 'Orders v2' },
    );
    // R4.10 / R4.11 — a rename is one attribute write. Nothing else was issued:
    // no delete, no upload, and in particular no second `PUT` that could carry a
    // label into an addressing position.
    expect(deleteMock).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalled();
  });

  /**
   * R4.10 said on screen. The id is what every pipeline record's
   * `target_schema_name` and every S3 key carry (R4.11), so the dialog states
   * that it is unchanged — a label that hid the id would make the two impossible
   * to correlate, and the failure mode of a label reaching that attribute is
   * silent and lands six steps away.
   */
  it('states that the schema id and the backing object are untouched', async () => {
    await renderPage();
    fireEvent.click(within(rowFor(UPLOADED_ROW.name)).getByText('Rename'));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByText(/The schema id stays/)).toBeInTheDocument();
    expect(within(dialog).getByText(UPLOADED_ROW.schemaId)).toBeInTheDocument();
    expect(
      within(dialog).getByText(/no object is moved, copied or deleted, and no pipeline is changed/),
    ).toBeInTheDocument();
  });

  /** An empty label clears it, which is a `200` carrying `cleared: true`. */
  it('clears the label when the input is emptied, and reports it as cleared', async () => {
    await renderPage();
    putMock.mockResolvedValue({
      message: `Display label cleared for ${LABELLED_ROW.schemaId}`,
      schemaId: LABELLED_ROW.schemaId,
      cleared: true,
    });

    // The labelled row shows its label, not its id (R4.8).
    fireEvent.click(within(rowFor(LABELLED_ROW.displayLabel)).getByText('Rename'));
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByTestId('rename-input').querySelector('input')!;
    // Prefilled with the stored label, so a rename edits rather than retypes.
    expect(input).toHaveValue(LABELLED_ROW.displayLabel);
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(within(dialog).getByText('Save'));

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    expect(putMock).toHaveBeenCalledWith(
      `/schemas/${LABELLED_ROW.schemaId}/display-label`,
      { displayLabel: '' },
    );
    await waitFor(() =>
      expect(screen.getByText(/Display label cleared/)).toBeInTheDocument(),
    );
  });

  /** Same rule as the context editor: the reason, and the text still recoverable. */
  it('reports a failed rename and leaves the typed label in the input', async () => {
    await renderPage();
    putMock.mockRejectedValue(
      new ApiError(400, {
        error: 'Only uploaded schemas carry an editable display label',
        reason: 'not_an_uploaded_schema',
      }),
    );

    fireEvent.click(within(rowFor(UPLOADED_ROW.name)).getByText('Rename'));
    const dialog = await screen.findByRole('dialog');
    const input = within(dialog).getByTestId('rename-input').querySelector('input')!;
    fireEvent.change(input, { target: { value: 'Orders v2' } });
    fireEvent.click(within(dialog).getByText('Save'));

    const error = await screen.findByTestId('rename-error');
    expect(error).toHaveTextContent('Only uploaded schemas carry an editable display label');
    expect(input).toHaveValue('Orders v2');
  });
});

/* ================================================================== */
/*  R4.8 — the same name for the same schema                           */
/* ================================================================== */

describe('R4.8 — the Display_Label where one is stored, the schema id otherwise', () => {
  it('shows the label for a labelled row and the id for an unlabelled one', async () => {
    await renderPage();

    // Stored label wins.
    expect(screen.getByText(LABELLED_ROW.displayLabel)).toBeInTheDocument();
    expect(screen.queryByText(LABELLED_ROW.schemaId)).not.toBeInTheDocument();
    // No label: the id, not a blank cell.
    expect(screen.getByText(UPLOADED_ROW.schemaId)).toBeInTheDocument();
  });
});

/* ================================================================== */
/*  R2.1 / R2.2 — the Context column                                   */
/* ================================================================== */

describe('Field context — R2.1 reports the failure and keeps the text, R2.2 offers no editor it cannot use', () => {
  async function openDetail(name: string) {
    fireEvent.click(within(rowFor(name)).getByText('View details'));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() =>
      expect(within(dialog).queryByText('Loading schema details...')).not.toBeInTheDocument(),
    );
    return dialog;
  }

  /**
   * R2.1: "IF a Field_Context edit is submitted and the write fails, THEN THE
   * Schemas_Page SHALL report the failure to the user and SHALL leave the entered
   * text recoverable."
   *
   * Both halves. Before this the mutation had no `onError` at any level, so Save
   * neither said anything nor closed the editor, and the row stayed in edit mode
   * with no way to tell a stored annotation from a discarded one.
   */
  it('reports a failed context write and leaves the entered text in the editor', async () => {
    await renderPage();
    putMock.mockRejectedValue(
      new ApiError(500, { error: 'Failed to update field context', details: 'AccessDenied' }),
    );

    const dialog = await openDetail(UPLOADED_ROW.name);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit context for email' }));
    const input = within(dialog).getByLabelText('Context for email');
    fireEvent.change(input, { target: { value: 'PII — hashed downstream' } });
    fireEvent.click(within(dialog).getByText('Save'));

    const error = await screen.findByTestId('context-save-error');
    expect(error).toHaveTextContent('Failed to update field context');
    // Recoverable: the editor is still open and still holds what was typed.
    expect(input).toHaveValue('PII — hashed downstream');
  });

  it('sends the context to the per-field route and closes the editor on success', async () => {
    await renderPage();
    putMock.mockResolvedValue({
      message: 'Context updated',
      schemaId: UPLOADED_ROW.schemaId,
      fieldName: 'email',
      context: 'PII',
    });

    const dialog = await openDetail(UPLOADED_ROW.name);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit context for email' }));
    fireEvent.change(within(dialog).getByLabelText('Context for email'), {
      target: { value: 'PII' },
    });
    fireEvent.click(within(dialog).getByText('Save'));

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    expect(putMock).toHaveBeenCalledWith(
      `/schemas/${UPLOADED_ROW.schemaId}/fields/email/context`,
      { context: 'PII' },
    );
    await waitFor(() =>
      expect(within(dialog).queryByLabelText('Context for email')).not.toBeInTheDocument(),
    );
  });

  /**
   * R2.2: "THE Schemas_Page SHALL NOT present an editing control for a value it
   * cannot persist." R2.5 is why — the context `PUT` answers `400` for a
   * catalog-derived id, because only an Uploaded_Schema has a Context_Sidecar.
   */
  it('offers no context editor on a catalog-derived schema, whose context cannot be stored', async () => {
    await renderPage();
    const dialog = await openDetail(GLUE_ROW.name);

    expect(
      within(dialog).queryByRole('button', { name: 'Edit context for order_id' }),
    ).not.toBeInTheDocument();
    // The value is still displayed — it is the editor that is withheld, not the
    // column.
    expect(within(dialog).getByText('order_id')).toBeInTheDocument();
  });
});

/* ================================================================== */
/*  R4.1 / R4.2 — the optional Display_Label at upload                 */
/* ================================================================== */

describe('Upload — R4.1 offers an optional Display_Label, R4.2 works without one', () => {
  /**
   * Choose a file in the page's **plain** "Upload schema" input — the hidden one
   * in the Table header's actions, which is first in DOM order and drives
   * `handleUpload`. That is the path under test here: it is the one R4.1's label
   * field has to reach as well as the preview wizard's, and
   * `SchemaManagementPage.upload-filename.test.tsx` covers the wizard path.
   */
  async function chooseFile(contents = '{"properties":{"id":{"type":"string"}}}') {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File([contents], 'orders.json', { type: 'application/json' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    await act(async () => {
      fireEvent.change(input);
    });
  }

  it('sends the supplied Display_Label with the upload', async () => {
    await renderPage();
    postMock.mockResolvedValue({
      message: 'Schema uploaded successfully',
      schemaId: 'orders_1785844049000',
      location: 's3://bucket/shared/orders_1785844049000.json',
      displayLabel: 'Orders extract',
    });

    fireEvent.change(screen.getByTestId('upload-display-label').querySelector('input')!, {
      target: { value: 'Orders extract' },
    });
    await chooseFile();

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0][0]).toBe('/schemas/upload');
    expect(postMock.mock.calls[0][1]).toMatchObject({ displayLabel: 'Orders extract' });
  });

  /**
   * R4.2 — "the schema SHALL be usable with no Display_Label at all". So the key
   * is **absent**, not sent as `""`: an empty label is the absence of one at the
   * producer too, and sending it would make the request say something the user
   * did not.
   */
  it('omits the key entirely when no Display_Label was typed', async () => {
    await renderPage();
    postMock.mockResolvedValue({
      message: 'Schema uploaded successfully',
      schemaId: 'orders_1785844049000',
      location: 's3://bucket/shared/orders_1785844049000.json',
    });

    await chooseFile();

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0][1]).not.toHaveProperty('displayLabel');
  });

  /** Whitespace-only is no label, matching the producer. */
  it('treats a whitespace-only Display_Label as no label', async () => {
    await renderPage();
    postMock.mockResolvedValue({ message: 'ok', schemaId: 'x', location: 's3://b/shared/x.json' });

    fireEvent.change(screen.getByTestId('upload-display-label').querySelector('input')!, {
      target: { value: '   ' },
    });
    await chooseFile();

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    expect(postMock.mock.calls[0][1]).not.toHaveProperty('displayLabel');
  });

  /**
   * `displayLabelError` arrives on a **`200`**. The schema is stored and usable
   * (R4.2) and only the sidecar write failed, so reporting it as an upload
   * failure would make the user retry an upload that succeeded and produce a
   * second object.
   */
  it('reports displayLabelError as a stored schema with an unsaved label, not as a failed upload', async () => {
    await renderPage();
    postMock.mockResolvedValue({
      message: 'Schema uploaded successfully',
      schemaId: 'orders_1785844049000',
      location: 's3://bucket/shared/orders_1785844049000.json',
      displayLabelError: 'AccessDenied on the sidecar',
    });

    fireEvent.change(screen.getByTestId('upload-display-label').querySelector('input')!, {
      target: { value: 'Orders extract' },
    });
    await chooseFile();

    await waitFor(() => expect(postMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByText(/display label was not saved/)).toBeInTheDocument(),
    );
    // Not reported as an upload failure.
    expect(screen.queryByText(/could not be uploaded/)).not.toBeInTheDocument();
  });
});
