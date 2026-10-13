// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 4.3 — the two Schemas_Page surfaces a listing label
 * lights up.
 *
 * Requirements 1.4 and 1.6:
 *
 * > 1.4 WHEN a Display_Label write succeeds, THE Schemas_Page SHALL display the
 * > new label in its schema table without the user reloading the page.
 * >
 * > 1.6 WHEN a user opens the Schemas_Page rename control for a schema that
 * > carries a Display_Label, THE Schemas_Page SHALL prefill the control with the
 * > stored Display_Label.
 *
 * **Neither branch is new code — both simply start being taken.** Task 2.3 added
 * `_resolve_display_labels` to `get_schemas`, so a listing row now carries
 * `displayLabel`. Before it, `setRenameValue(s.display_label ?? '')`
 * (`SchemaManagementPage.tsx:488`) took its `''` arm for **every** schema, so the
 * rename modal reopened blank on a schema that had just been renamed and a rename
 * edited nothing rather than editing the stored value. That is item 1's second
 * reported symptom, and until now it was confirmed only by reading the code.
 *
 * Design §6 lists these in the non-property table: they are specific
 * interactions on hooks that already have coverage, and there is no input space
 * worth quantifying over.
 *
 * ---
 *
 * ## What this file adds over the two files beside it
 *
 * `SchemaManagementPage.write-surface.test.tsx` already asserts the static half
 * of R4.8 — a labelled row shows its label, an unlabelled one shows its id
 * (`:555-570`) — and picks the prefill up incidentally on its way to the clear
 * case (`:519`). Neither of those is R1.4 or R1.6:
 *
 * - R1.6's **other** arm — the control is empty for a schema with no stored
 *   label — is asserted nowhere. That arm is precisely the one the defect made
 *   universal, so leaving it unpinned would leave the fix unable to regress
 *   visibly.
 * - R1.4 is about the table **after a successful write**, which is the
 *   invalidate-and-refetch path through `useSetDisplayLabel`
 *   (`use-schemas.ts:313-326`). A static fixture cannot exercise it: the listing
 *   has to answer differently on the second call, the way the deployed API now
 *   does.
 *
 * ## Mocking
 *
 * `apiClient` only — never `../../hooks`. The real `useSchemas`,
 * `useSetDisplayLabel`, `fetchSchemaListing` and `normalizeSchema` all run, which
 * is what makes the wire key `displayLabel` reaching `schemaDisplayName` the
 * thing under test rather than an assumption. It also keeps clear of
 * held-assertion inventory row 5: a `vi.mock` factory **replaces** the module, so
 * any export it omits is `undefined`; the component then calls `undefined()`
 * **during render, inside the test that renders it**. The failure is a stack
 * trace naming a value that is not a function — not an assertion diff — and the
 * file still collects and still reports per test. Amend the factory before
 * adding a hook to a component the factory's tests render.
 *
 * `utils/schema-display.ts` is deliberately **not** mocked;
 * `schemaDisplayName` is part of what is under test.
 *
 * Requirements: 1.4, 1.6
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
const putMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock, post: vi.fn(), put: putMock, delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import SchemaManagementPage from '../SchemaManagementPage';

/* ------------------------------------------------------------------ */
/*  Fixtures — the post-task-2.3 listing shape                         */
/* ------------------------------------------------------------------ */

/**
 * An uploaded row with **no** `displayLabel` key at all. Absent, not `null` and
 * not `''` — Requirement 1.2, and the shape `_resolve_display_labels` emits for
 * a schema nobody has named.
 */
const UNLABELLED_ROW = {
  schemaId: 'uploaded_schema_1785844048926',
  name: 'uploaded_schema_1785844048926',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-04T11:47:29+00:00',
  version: '1.0',
  fieldCount: null,
};

/** The same row after task 2.3 resolved a stored label for it. */
const LABELLED_ROW = {
  schemaId: 'uploaded_schema_1785844048999',
  name: 'uploaded_schema_1785844048999',
  displayLabel: 'Customer master',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-05T09:12:03+00:00',
  version: '1.0',
  fieldCount: null,
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

/**
 * Answers `/schemas` from a mutable row list, so a test can make the listing
 * report a label on its **second** call — which is what the deployed API does
 * once a rename has landed, and the only way R1.4's "without reloading" is
 * observable.
 */
let listingRows: Array<Record<string, unknown>> = [];

function routeGet() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return { schemas: listingRows, sourcesFailed: [] };
    throw new Error(`unexpected GET ${path}`);
  });
}

async function renderPage(rows: Array<Record<string, unknown>>, settled: string) {
  listingRows = rows;
  routeGet();
  render(<SchemaManagementPage />, { wrapper });
  await screen.findByText(settled);
}

/** The Schema name column, one entry per body row. Column order is
 *  name, source, created, actions. */
function nameColumn(): string[] {
  return screen
    .getAllByRole('row')
    .map((row) => within(row).queryAllByRole('cell')[0]?.textContent?.trim() ?? '')
    .filter((text) => text.length > 0);
}

/** Open the rename dialog from the row whose name cell reads `shown`. */
async function openRename(shown: string) {
  const row = screen.getByText(shown).closest('tr');
  if (!row) throw new Error(`no listing row showing "${shown}"`);
  fireEvent.click(within(row as HTMLElement).getByText('Rename'));
  return screen.findByRole('dialog');
}

function renameInput(dialog: HTMLElement): HTMLInputElement {
  return within(dialog).getByTestId('rename-input').querySelector('input')!;
}

beforeEach(() => {
  getMock.mockReset();
  putMock.mockReset();
  listingRows = [];
});

/* ================================================================== */
/*  R1.6 — the rename control prefills from the listing row            */
/* ================================================================== */

describe('R1.6 — the rename control is prefilled with the stored Display_Label', () => {
  it('prefills the stored label, so a rename edits rather than retypes', async () => {
    await renderPage([UNLABELLED_ROW, LABELLED_ROW], LABELLED_ROW.displayLabel);

    const dialog = await openRename(LABELLED_ROW.displayLabel);

    // The value comes off the **listing** row's `display_label`
    // (`SchemaManagementPage.tsx:488`). Before task 2.3 the listing carried no
    // label, so this arm was unreachable and the modal reopened blank on a
    // schema that had just been renamed.
    expect(renameInput(dialog)).toHaveValue(LABELLED_ROW.displayLabel);
    // The header names the schema the same way the table does.
    expect(within(dialog).getByText(`Rename ${LABELLED_ROW.displayLabel}`)).toBeInTheDocument();
  });

  /**
   * The other arm, and the one no existing test reaches. An unlabelled row must
   * open an **empty** control — not the id — because the input's value is what a
   * Save writes: prefilling the id would store the id as a label on the first
   * Save, and the placeholder already says which schema this is.
   */
  it('opens empty for a schema with no stored label, with the id only as a placeholder', async () => {
    await renderPage([UNLABELLED_ROW, LABELLED_ROW], UNLABELLED_ROW.name);

    const dialog = await openRename(UNLABELLED_ROW.name);
    const input = renameInput(dialog);

    expect(input).toHaveValue('');
    expect(input).toHaveAttribute('placeholder', UNLABELLED_ROW.schemaId);
  });
});

/* ================================================================== */
/*  R1.4 — the table shows the new label with no reload                */
/* ================================================================== */

describe('R1.4 — a successful rename changes the name the table shows', () => {
  /**
   * The write-through R1.4 depends on: `useSetDisplayLabel` invalidates the
   * listing on success (`use-schemas.ts:319-325`), the listing refetches, and
   * task 2.3's post-pass now answers with `displayLabel` on the row. The
   * rejected alternative was a `setQueryData` write-through, which would show the
   * new label until the next refetch and then silently lose it
   * (`LGR-28b4d75-5`, **template / ratified**) — so asserting on a **refetched**
   * listing rather than on optimistic cache state is the point of the test, not
   * an incidental detail of it.
   *
   * No `rerender` and no second `render`: the component instance that showed the
   * id is the one that must end up showing the label.
   */
  it('shows the new label in the table after the write, without a re-render', async () => {
    await renderPage([UNLABELLED_ROW], UNLABELLED_ROW.name);
    expect(nameColumn()).toContain(UNLABELLED_ROW.name);

    putMock.mockImplementation(async () => {
      // The producer's own ordering: the sidecar write lands, so the next
      // listing resolves a label for this schema.
      listingRows = [{ ...UNLABELLED_ROW, displayLabel: 'Q3 orders' }];
      return {
        message: 'Display label set',
        schemaId: UNLABELLED_ROW.schemaId,
        displayLabel: 'Q3 orders',
      };
    });

    const dialog = await openRename(UNLABELLED_ROW.name);
    fireEvent.change(renameInput(dialog), { target: { value: 'Q3 orders' } });
    fireEvent.click(within(dialog).getByText('Save'));

    await waitFor(() => expect(nameColumn()).toContain('Q3 orders'));
    // R1.12 stays true on the surface the user reads: the id is no longer the
    // name shown, and it was replaced rather than appended.
    expect(nameColumn()).not.toContain(UNLABELLED_ROW.schemaId);
    // The listing was re-read rather than patched in place.
    expect(getMock.mock.calls.filter(([path]) => path === '/schemas').length).toBeGreaterThan(1);
  });
});
