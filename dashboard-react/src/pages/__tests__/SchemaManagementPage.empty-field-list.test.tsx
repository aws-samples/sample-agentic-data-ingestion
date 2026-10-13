// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45-unmapped-target-field-visibility task 1.1 — `EmptyFieldList` characterization.
 *
 * **This file exists so that R5.7 can be checked rather than asserted.** R5.7:
 *
 * > WHERE a schema's `fields` list is empty, THE Schema_Detail_View SHALL
 * > continue to render `EmptyFieldList`'s two existing statements unchanged, and
 * > SHALL make no required/optional claim.
 *
 * "Continue to render unchanged" is an unbacked claim until something observes
 * the statements, and **nothing did**: repo-wide grep for
 * `schema-declares-no-fields` / `schema-fields-unresolved` returns only
 * `SchemaManagementPage.tsx:59` and `:65` — the component itself. CodeGraph
 * agrees, flagging `EmptyFieldList` (`:55`, one caller, `SchemaDetailModal`)
 * with no covering tests. Design C17 calls this the sharpest of the four
 * zero-coverage symbols for exactly that reason.
 *
 * So this test lands **before** task 14 adds a required/optional column to the
 * Fields table, which is what lets task 14 be read as additive: if task 14
 * disturbs either statement, this file fails and names which one.
 *
 * ---
 *
 * ## Where the expectations come from — R5.7 and the component's own docstring
 *
 * `LGR-28b4d75-5` (**template / ratified**): *"a test written from the
 * producer's output cannot detect the producer drifting, because the output is
 * its own oracle."* A snapshot of `EmptyFieldList` would pin whatever the
 * component currently emits, defects included, and would then agree with any
 * future rewrite that regenerated it. So every expectation below is traced to a
 * source **outside** the component:
 *
 * | Expectation | Authority |
 * |---|---|
 * | `field_count === 0` → *"This schema declares no fields"* | R5.7 via requirements Current state item 14 |
 * | `field_count == null` → *"No field list came back for this schema"* … *"That is not the same as a schema that declares none"* | same |
 * | both statements name the source type | the docstring at `SchemaManagementPage.tsx:44-54`: *"Both name the source type, so the reader knows which kind of schema they are looking at"* |
 * | the two cases are **different** statements under **different** testids | C4 / `LGR-4854b3d-1` (**template / ratified**) |
 *
 * **Deliberately NOT asserted: the null branch's trailing recovery clause**
 * (*"close and reopen the row to try again"*). Neither R5.7 nor the docstring
 * mentions it, so the only available expectation for it would be the component's
 * own output — which is the oracle problem above. It is left unpinned on
 * purpose; a later requirement that wants it pinned should say so, and then it
 * can be derived rather than copied.
 *
 * ## Why the three-state assertion is the load-bearing one
 *
 * `LGR-4854b3d-1` (**template / ratified**, scope `utils/schema-field-count.ts`
 * and `handlers/schemas.py`) ratifies that `field_count` is *"three-state — a
 * positive number, 0, or null — and null is never collapsed to 0"*, because
 * *"the source stated zero"* and *"no count was established"* are different
 * facts. `EmptyFieldList` is that rule already implemented on the very surface
 * task 14 touches, and the docstring at `:44-54` gives the consequence in as
 * many words: *"An empty table is indistinguishable from a failed load, which is
 * the failure class this requirement exists to remove."*
 *
 * A pair of `getByText` assertions would not catch the failure that matters —
 * two sentences merged into one hedge would still contain both fragments. So the
 * last test asserts the branch **as a branch**: different testids, and each
 * statement free of the other's distinguishing clause. That is the assertion a
 * later reader has to argue with before collapsing the two sentences.
 *
 * ## Scope: today's behaviour only
 *
 * No required/optional expectation appears here. That is task 14's ground, and
 * mixing it in would make this file's failures ambiguous between "the statements
 * changed" and "the new column is wrong".
 *
 * ## Harness
 *
 * Modelled on `SchemaManagementPage.detail-modal.test.tsx` — same page, same
 * modal, same fixture shape. `apiClient` is the only thing stubbed, so the real
 * `useSchemas` / `useSchemaDetail` / `normalizeSchema` / `normalizeFieldCount`
 * all run. That matters here: `field_count` is **derived** by
 * `normalizeFieldCount` (`use-schemas.ts:44-47`), so a fixture that stubbed the
 * hook would let the test choose the value the component branches on, and the
 * derivation — the thing `LGR-4854b3d-1` governs — would go unexercised.
 *
 * Requirements: 5.7
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: {
    get: getMock,
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import SchemaManagementPage from '../SchemaManagementPage';

/* ------------------------------------------------------------------ */
/*  Fixtures — one detail response per `field_count` state.            */
/* ------------------------------------------------------------------ */

/**
 * The three states, and how each is spelled on the wire.
 *
 * `normalizeFieldCount` (`use-schemas.ts:44-47`) reads `fieldCount ??
 * field_count` and takes it when it is a number; otherwise a present `fields`
 * **list** supplies its own count; with neither, the count was never
 * established and it returns `null`. So:
 *
 * - **stated zero** — `fieldCount: 0`. The source said "no fields".
 * - **never established** — `fieldCount: null` and **no `fields` key at all**,
 *   which is what `handlers/schemas.py:576-578` sends for an uploaded row: a
 *   `list_objects_v2` listing carries no column count, and an absent list must
 *   not be mistaken for an empty one.
 * - **stated positive** — a real `fields` list, so the Fields table renders and
 *   `EmptyFieldList` is never reached.
 *
 * Each carries a distinct `source` so the docstring's "both name the source
 * type" can be checked per branch rather than against a shared constant.
 */
const DECLARES_NONE_DETAIL = {
  schemaId: 'uploaded_schema_declares_none',
  name: 'uploaded_schema_declares_none',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-01T10:00:00+00:00',
  fieldCount: 0,
  fields: [],
};

const UNRESOLVED_DETAIL = {
  schemaId: 's3_tables_schema_unresolved',
  name: 's3_tables_schema_unresolved',
  source: 's3_table_bucket',
  targetType: 'S3 Tables',
  lastModified: '2026-08-02T10:00:00+00:00',
  fieldCount: null,
  // No `fields` key — deliberately. See the note above.
};

const RESOLVED_FIELDS = [
  { name: 'order_id', type: 'string', nullable: false, description: 'Order key', context: '' },
  { name: 'order_date', type: 'date', nullable: true, description: '', context: '' },
];

const RESOLVED_DETAIL = {
  schemaId: 'glue_salesdb_orders',
  name: 'salesdb.orders',
  source: 'glue_catalog',
  targetType: 'Glue Catalog',
  lastModified: '2026-08-03T10:00:00+00:00',
  fieldCount: RESOLVED_FIELDS.length,
  fields: RESOLVED_FIELDS,
};

const LISTING = {
  schemas: [
    {
      schemaId: DECLARES_NONE_DETAIL.schemaId,
      name: DECLARES_NONE_DETAIL.name,
      source: DECLARES_NONE_DETAIL.source,
      targetType: DECLARES_NONE_DETAIL.targetType,
      lastModified: DECLARES_NONE_DETAIL.lastModified,
      version: '1.0',
      fieldCount: 0,
    },
    {
      schemaId: UNRESOLVED_DETAIL.schemaId,
      name: UNRESOLVED_DETAIL.name,
      source: UNRESOLVED_DETAIL.source,
      targetType: UNRESOLVED_DETAIL.targetType,
      lastModified: UNRESOLVED_DETAIL.lastModified,
      version: '1.0',
      fieldCount: null,
    },
    {
      schemaId: RESOLVED_DETAIL.schemaId,
      name: RESOLVED_DETAIL.name,
      source: RESOLVED_DETAIL.source,
      targetType: RESOLVED_DETAIL.targetType,
      lastModified: RESOLVED_DETAIL.lastModified,
      version: '1.0',
      fields: RESOLVED_FIELDS,
      fieldCount: RESOLVED_FIELDS.length,
    },
  ],
  sourcesFailed: [],
};

const DETAILS: Record<string, unknown> = {
  [DECLARES_NONE_DETAIL.schemaId]: DECLARES_NONE_DETAIL,
  [UNRESOLVED_DETAIL.schemaId]: UNRESOLVED_DETAIL,
  [RESOLVED_DETAIL.schemaId]: RESOLVED_DETAIL,
};

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function routeApi() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return LISTING;
    const match = /^\/schemas\/(.+)$/.exec(path);
    if (match) return DETAILS[decodeURIComponent(match[1])];
    throw new Error(`unexpected GET ${path}`);
  });
}

/** Render the page, open `name`'s detail modal, and return the modal element. */
async function openDetail(name: string): Promise<HTMLElement> {
  routeApi();
  render(<SchemaManagementPage />, { wrapper });
  await screen.findByText(name);

  const row = screen.getByText(name).closest('tr');
  if (!row) throw new Error(`no listing row for ${name}`);
  fireEvent.click(within(row as HTMLElement).getByText('View details'));

  const dialog = await screen.findByRole('dialog');
  await waitFor(() =>
    expect(within(dialog).queryByText('Loading schema details...')).not.toBeInTheDocument(),
  );
  return dialog;
}

/**
 * The statement `EmptyFieldList` reached, as a testid plus its text.
 *
 * Read off the identified alert element, not off a rendered-HTML blob: the
 * point is *which* statement was reached and what it says, and a whole-document
 * text match cannot tell the two branches apart once both fragments exist
 * somewhere on the page.
 */
function emptyFieldStatement(dialog: HTMLElement): { testId: string; text: string } {
  const declaresNone = within(dialog).queryByTestId('schema-declares-no-fields');
  const unresolved = within(dialog).queryByTestId('schema-fields-unresolved');
  if (declaresNone && unresolved) {
    throw new Error('both EmptyFieldList branches rendered at once');
  }
  const alert = declaresNone ?? unresolved;
  if (!alert) throw new Error('neither EmptyFieldList branch rendered');
  return {
    testId: declaresNone ? 'schema-declares-no-fields' : 'schema-fields-unresolved',
    text: (alert.textContent ?? '').replace(/\s+/g, ' ').trim(),
  };
}

beforeEach(() => {
  getMock.mockReset();
});

describe('EmptyFieldList — R5.7, the two statements the Fields table must not disturb', () => {
  /**
   * `field_count === 0` is the source **stating** that the schema declares no
   * fields. R5.7 (Current state item 14) quotes the statement; the docstring at
   * `SchemaManagementPage.tsx:44-54` adds that it names the source type, *"so
   * the reader knows which kind of schema they are looking at"*.
   */
  it('states that the schema declares no fields when the source stated a count of zero', async () => {
    const dialog = await openDetail(DECLARES_NONE_DETAIL.name);

    const alert = within(dialog).getByTestId('schema-declares-no-fields');
    expect(alert).toHaveTextContent('This schema declares no fields');
    // The docstring's "both name the source type".
    expect(alert).toHaveTextContent(DECLARES_NONE_DETAIL.source);

    // The other branch is not also on screen.
    expect(within(dialog).queryByTestId('schema-fields-unresolved')).not.toBeInTheDocument();
  });

  /**
   * `field_count == null` is **no count having been established** — the uploaded
   * and S3 Tables case, where the listing carries neither a count nor a field
   * list (`handlers/schemas.py:576-578`).
   *
   * The second fragment is the one that carries the whole point: R5.7's quoted
   * *"That is not the same as a schema that declares none"* is what stops an
   * unresolved list reading as a schema with nothing in it, which is the failure
   * class the docstring says this branch exists to remove.
   */
  it('states that no field list came back, and that this differs from declaring none, when no count was established', async () => {
    const dialog = await openDetail(UNRESOLVED_DETAIL.name);

    const alert = within(dialog).getByTestId('schema-fields-unresolved');
    expect(alert).toHaveTextContent('No field list came back for this schema');
    expect(alert).toHaveTextContent('That is not the same as a schema that declares none');
    expect(alert).toHaveTextContent(UNRESOLVED_DETAIL.source);

    expect(within(dialog).queryByTestId('schema-declares-no-fields')).not.toBeInTheDocument();
  });

  /**
   * The third state. A stated positive count with a resolved list is not an
   * empty-fields case at all, so `EmptyFieldList` is never reached and **neither**
   * statement is made — the Fields table is.
   *
   * This is the control that keeps the two tests above honest: without it, a
   * component that rendered one of the statements unconditionally would still
   * pass both.
   */
  it('makes neither statement when the source stated a positive count and the fields resolved', async () => {
    const dialog = await openDetail(RESOLVED_DETAIL.name);

    expect(within(dialog).queryByTestId('schema-declares-no-fields')).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId('schema-fields-unresolved')).not.toBeInTheDocument();
    // The Fields table renders instead.
    expect(within(dialog).getByText('order_id')).toBeInTheDocument();
    expect(within(dialog).getByText('order_date')).toBeInTheDocument();
  });

  /**
   * **The branch asserted as a branch — C4 / `LGR-4854b3d-1`.**
   *
   * The three tests above would each still pass if the two sentences were merged
   * into one hedge that contained every fragment, or if both branches were given
   * the same testid. Neither would satisfy `LGR-4854b3d-1`, which ratifies that
   * *"the source stated zero"* and *"no count was established"* are different
   * facts and that the second is never collapsed into the first.
   *
   * So: different testids, and each statement free of the other's distinguishing
   * clause. A later reader who wants one sentence has to delete this assertion
   * first, with a reason — which is the practice `LGR-60c4985-6` (**template /
   * ratified**) encodes.
   */
  it('keeps the two cases as two statements under two testids', async () => {
    const declaresNone = emptyFieldStatement(await openDetail(DECLARES_NONE_DETAIL.name));
    cleanup();
    getMock.mockReset();
    const unresolved = emptyFieldStatement(await openDetail(UNRESOLVED_DETAIL.name));

    // Two testids, not one.
    expect(declaresNone.testId).toBe('schema-declares-no-fields');
    expect(unresolved.testId).toBe('schema-fields-unresolved');
    expect(declaresNone.testId).not.toBe(unresolved.testId);

    // Two statements, not one merged hedge: neither carries the other's
    // distinguishing clause.
    expect(declaresNone.text).not.toContain('No field list came back');
    expect(declaresNone.text).not.toContain('That is not the same as a schema that declares none');
    expect(unresolved.text).not.toContain('This schema declares no fields');
    expect(declaresNone.text).not.toBe(unresolved.text);
  });
});
