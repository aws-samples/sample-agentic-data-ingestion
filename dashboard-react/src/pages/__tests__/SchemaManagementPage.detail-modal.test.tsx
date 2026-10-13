// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 6.1 (design B6) — Schema_Detail_Modal characterization.
 *
 * **Expectations come from R1.3, not from current output.** R1.3:
 *
 * > WHEN a user activates "View details" on a row of the Schemas_Page, THE
 * > Schema_Detail_Modal SHALL display the field list of that row's schema, and
 * > the field count it displays SHALL equal the count shown in that row's
 * > Fields column.
 *
 * R8.3 makes this test a deliverable. **This file starts red on two of its four
 * assertions, deliberately.** Task 6.3 (design B7) closes both. A reviewer who
 * sees the red and reverts has undone the only thing that would have caught the
 * defect (design §5 risk 9). The per-assertion split is stated on each test.
 *
 * ---
 *
 * ## This is filling a real void — no existing test reaches either fetcher
 *
 * `fetchSchemaDetail` (`use-schemas.ts:38-45`) and `normalizeSchema` /
 * `normalizeField` (`:7-27`) have **no test coverage at all** today, confirmed
 * by the symbol graph (design §CodeGraph table): every one of
 * `fetchSchemaDetail`, `normalizeSchema`, `normalizeField`, `useSchemaDetail`
 * and `SchemaDetailModal` is flagged "no covering tests found". The mechanism is
 * `src/__tests__/pages.test.tsx:16`, where `useSchemaDetail` is mocked as a bare
 * `vi.fn()` and **never given a return value** — so the two Schemas-page smoke
 * tests there render the listing and never open the modal, and both fetchers are
 * mocked out in every existing test in the suite.
 *
 * This file therefore mocks **`apiClient`, not `../../hooks`**: the real
 * `useSchemas` / `useSchemaDetail` / `fetchSchemaDetail` / `normalizeSchema` /
 * `normalizeField` all run, which is the point. That also sidesteps the
 * held-assertion inventory's row 5 hazard: a `vi.mock` factory **replaces** the
 * module, so any export it omits is `undefined`; the component then calls
 * `undefined()` **during render, inside the test that renders it**. The failure
 * is a stack trace naming a value that is not a function — not an assertion
 * diff — and the file still collects and still reports per test.
 * Amend the factory before adding a hook to a component the factory's tests
 * render. Task 8.7 owns that factory; nothing here touches it.
 *
 * ## The fixtures are the real wire shapes, read from the producer
 *
 * - The listing rows mirror `handlers/schemas.py` after task 4.4: a Glue row
 *   carries `fields` **and** a real `fieldCount` (`:504-507`, free from the
 *   `get_tables` page); an uploaded row carries `fieldCount: null` and **no
 *   `fields` key at all** (`:576-578`), because `list_objects_v2` returns no
 *   column count and an absent list cannot be mistaken for an empty one.
 * - The detail responses are DD9's **bare schema object** — not
 *   `{"schemas": [one]}` — built the way `_schema_detail` builds it
 *   (`schemas.py:1040-1101`): `schemaId`, `name`, `source`, `targetType`,
 *   `lastModified`, `fields[]` with `name` / `type` / `nullable` /
 *   `description` / `context`.
 *
 * ## What "the count it displays" means here
 *
 * The modal renders no explicit counter today — its Fields table's row count
 * *is* the count it displays, so that is what these tests read. If task 6.3
 * adds a `counter` to that Header the row count remains the displayed count and
 * the assertions still hold.
 *
 * Requirements: 1.3, 8.3
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';

// The API boundary is the only thing stubbed. Everything above it — the hooks,
// both fetchers and both normalizers — is the real code under test.
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
/*  Fixtures — the post-task-4.4 listing shape and DD9's detail shape. */
/* ------------------------------------------------------------------ */

/** A Glue row: `fields` stays on the listing and `fieldCount` is a real number. */
const GLUE_FIELDS = [
  { name: 'order_id', type: 'string', nullable: true, description: 'Order key', context: '' },
  { name: 'order_date', type: 'date', nullable: true, description: '', context: '' },
];

const LISTING = {
  schemas: [
    {
      // Row 1. Deliberately first, because the `[0]` unwrap this spec removes
      // shows *this* row's fields no matter which row was clicked.
      schemaId: 'uploaded_schema_alpha',
      name: 'uploaded_schema_alpha',
      source: 'uploaded',
      targetType: 'Target Schema',
      lastModified: '2026-08-01T10:00:00+00:00',
      version: '1.0',
      fieldCount: null,
    },
    {
      // Row 2. The row the user clicks in every test below.
      schemaId: 'uploaded_schema_beta',
      name: 'uploaded_schema_beta',
      source: 'uploaded',
      targetType: 'Target Schema',
      lastModified: '2026-08-02T10:00:00+00:00',
      version: '1.0',
      fieldCount: null,
    },
    {
      // Row 3, the control: the one source whose listing states a real count.
      schemaId: 'glue_salesdb_orders',
      name: 'salesdb.orders',
      source: 'glue_catalog',
      targetType: 'Glue Catalog',
      lastModified: '2026-08-03T10:00:00+00:00',
      version: '1.0',
      fields: GLUE_FIELDS,
      fieldCount: GLUE_FIELDS.length,
    },
    /* -------------------------------------------------------------- */
    /*  p45 task 14.1 — three rows added for the Required column.      */
    /*  Appended rather than inserted: every test above finds its row  */
    /*  by name (`screen.getByText(name).closest('tr')`), so position  */
    /*  is not load-bearing, but leaving rows 1–3 where they are keeps */
    /*  row 1's "deliberately first" comment true.                     */
    /* -------------------------------------------------------------- */
    {
      // Row 4 — the three-state row: one stated `false`, one stated `true`,
      // one with **no `nullable` key at all**.
      schemaId: 'uploaded_schema_mixed',
      name: 'uploaded_schema_mixed',
      source: 'uploaded',
      targetType: 'Target Schema',
      lastModified: '2026-08-04T10:00:00+00:00',
      version: '1.0',
      fieldCount: null,
    },
    {
      // Row 5 — R5.7's first branch: the source stated a count of zero.
      schemaId: 'uploaded_schema_no_fields',
      name: 'uploaded_schema_no_fields',
      source: 'uploaded',
      targetType: 'Target Schema',
      lastModified: '2026-08-05T10:00:00+00:00',
      version: '1.0',
      fieldCount: 0,
    },
    {
      // Row 6 — R5.7's second branch: no count was ever established.
      schemaId: 's3_tables_schema_unresolved',
      name: 's3_tables_schema_unresolved',
      source: 's3_table_bucket',
      targetType: 'S3 Tables',
      lastModified: '2026-08-06T10:00:00+00:00',
      version: '1.0',
      fieldCount: null,
    },
  ],
  sourcesFailed: [],
};

const ALPHA_DETAIL = {
  schemaId: 'uploaded_schema_alpha',
  name: 'uploaded_schema_alpha',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-01T10:00:00+00:00',
  fields: [
    { name: 'email', type: 'string', nullable: false, description: 'Primary contact', context: '' },
    { name: 'customer_id', type: 'string', nullable: false, description: '', context: '' },
  ],
};

const BETA_DETAIL = {
  schemaId: 'uploaded_schema_beta',
  name: 'uploaded_schema_beta',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-02T10:00:00+00:00',
  fields: [
    { name: 'region_code', type: 'string', nullable: true, description: 'ISO region', context: '' },
    { name: 'order_total', type: 'number', nullable: true, description: '', context: '' },
    { name: 'shipped_at', type: 'string', nullable: true, description: '', context: '' },
  ],
};

const GLUE_DETAIL = {
  schemaId: 'glue_salesdb_orders',
  name: 'salesdb.orders',
  source: 'glue_catalog',
  targetType: 'Glue Catalog',
  lastModified: '2026-08-03T10:00:00+00:00',
  fields: GLUE_FIELDS,
};

/* ------------------------------------------------------------------ */
/*  p45 task 14.1 — the three `nullable` states on one schema.         */
/* ------------------------------------------------------------------ */

/**
 * The three states, and how each is spelled on the wire (R5.2, R5.3).
 *
 * `nullable` is the **negation of required** at the producer
 * (`handlers/schemas.py:1394`, `field_name not in required`), so a stated `false`
 * is a stated *requirement* rather than a missing one. The third state is
 * **absence** — after p45 tasks 8 and 9 a producer that never established
 * nullability sends **no key**: `handlers/schemas.py:496` for the Glue branch,
 * whose `StorageDescriptor.Columns[]` carries no nullability at all, and `:1569`
 * for `_schema_detail`'s conditional key set.
 *
 * `postal_code` therefore carries **no `nullable` key**, deliberately, and that
 * is the case the whole column exists for. It is not spelled `nullable: null`:
 * `LGR-44d601e-1` (**template / ratified**) clears a value by *removing* the key
 * precisely because *"a stored `""` would have to be distinguished from absence
 * at every reader"*, and three spellings of two facts is the defect rather than
 * the fix (do-NOT 17).
 */
const MIXED_DETAIL = {
  schemaId: 'uploaded_schema_mixed',
  name: 'uploaded_schema_mixed',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-04T10:00:00+00:00',
  fields: [
    // Stated `false` — the source declared the field required.
    { name: 'account_ref', type: 'string', nullable: false, description: 'Account key', context: '' },
    // Stated `true` — the source declared the field optional.
    { name: 'nickname', type: 'string', nullable: true, description: '', context: '' },
    // **No `nullable` key.** The source did not say.
    { name: 'postal_code', type: 'string', description: '', context: '' },
  ],
};

/** R5.7's first branch: the source **stated** a count of zero. */
const NO_FIELDS_DETAIL = {
  schemaId: 'uploaded_schema_no_fields',
  name: 'uploaded_schema_no_fields',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-05T10:00:00+00:00',
  fieldCount: 0,
  fields: [],
};

/**
 * R5.7's second branch: **no count was established**.
 *
 * `fieldCount: null` and **no `fields` key at all**, which is what
 * `handlers/schemas.py:576-578` sends for an uploaded or S3 Tables row — a
 * `list_objects_v2` listing carries no column count, and an absent list must not
 * be mistaken for an empty one.
 */
const UNRESOLVED_DETAIL = {
  schemaId: 's3_tables_schema_unresolved',
  name: 's3_tables_schema_unresolved',
  source: 's3_table_bucket',
  targetType: 'S3 Tables',
  lastModified: '2026-08-06T10:00:00+00:00',
  fieldCount: null,
  // No `fields` key — deliberately. See above.
};

const DETAILS: Record<string, unknown> = {
  uploaded_schema_alpha: ALPHA_DETAIL,
  uploaded_schema_beta: BETA_DETAIL,
  glue_salesdb_orders: GLUE_DETAIL,
  uploaded_schema_mixed: MIXED_DETAIL,
  uploaded_schema_no_fields: NO_FIELDS_DETAIL,
  s3_tables_schema_unresolved: UNRESOLVED_DETAIL,
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

/**
 * Route `apiClient.get` the way the deployed API routes it.
 *
 * `detailFor` lets one test substitute the **collection-envelope** shape for the
 * per-id response, which is the branch DD9 deletes.
 */
function routeApi(detailFor: (id: string) => unknown = (id) => DETAILS[id]) {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return LISTING;
    const match = /^\/schemas\/(.+)$/.exec(path);
    if (match) return detailFor(decodeURIComponent(match[1]));
    throw new Error(`unexpected GET ${path}`);
  });
}

/** Open the detail modal from `name`'s row and wait for its Fields table. */
async function openDetail(name: string) {
  const row = screen.getByText(name).closest('tr');
  if (!row) throw new Error(`no listing row for ${name}`);
  fireEvent.click(within(row as HTMLElement).getByText('View details'));
  const dialog = await screen.findByRole('dialog');
  await waitFor(() =>
    expect(within(dialog).queryByText('Loading schema details...')).not.toBeInTheDocument(),
  );
  return dialog;
}

/** The field count the modal displays — its Fields table's body row count. */
function modalFieldCount(dialog: HTMLElement): number {
  return dialog.querySelectorAll('tbody tr').length;
}

/* ------------------------------------------------------------------ */
/*  p45 task 14.1 helpers — the Required column, read per field row.   */
/* ------------------------------------------------------------------ */

/** The Fields-table row for `fieldName`, inside an open detail modal. */
function fieldRow(dialog: HTMLElement, fieldName: string): HTMLElement {
  const row = within(dialog).getByText(fieldName).closest('tr');
  if (!row) throw new Error(`no Fields row for ${fieldName}`);
  return row as HTMLElement;
}

/**
 * The required/optional claim made about `fieldName`, as a testid plus its text.
 *
 * Read off the identified cell element rather than a rendered-HTML blob, and
 * scoped to the field's own row: the point is *which* of the three states was
 * claimed about *which* field, and a whole-modal text match cannot tell three
 * rows apart once all three states are on screen at once. Throws if a row makes
 * more than one claim or none, which is the shape that would let a collapsed
 * two-state renderer pass.
 *
 * The testids are the total derivation `requirement-${state.replace('_', '-')}`
 * that `utils/field-requirement.ts` writes out as a table in its own doc comment.
 * They are not an export, so this list is the derivation applied — not a copy of
 * a constant the module owns.
 */
function requirementClaim(
  dialog: HTMLElement,
  fieldName: string,
): { testId: string; text: string } {
  const row = fieldRow(dialog, fieldName);
  const found = (
    ['requirement-required', 'requirement-optional', 'requirement-not-established'] as const
  )
    .map((testId) => ({ testId, el: within(row).queryByTestId(testId) }))
    .filter((c) => c.el !== null);
  if (found.length === 0) throw new Error(`no requirement claim rendered for ${fieldName}`);
  if (found.length > 1) {
    throw new Error(
      `${fieldName} carries ${found.length} requirement claims: ${found.map((c) => c.testId).join(', ')}`,
    );
  }
  return { testId: found[0].testId, text: (found[0].el!.textContent ?? '').trim() };
}

/** Every requirement claim in an open modal, whichever state each is in. */
function allRequirementClaims(dialog: HTMLElement): HTMLElement[] {
  return [
    ...within(dialog).queryAllByTestId('requirement-required'),
    ...within(dialog).queryAllByTestId('requirement-optional'),
    ...within(dialog).queryAllByTestId('requirement-not-established'),
  ];
}

beforeEach(() => {
  getMock.mockReset();
});

describe('Schema_Detail_Modal — R1.3, the field list belongs to the row that was clicked', () => {
  /**
   * **GREEN, and the reason it is green is a finding rather than a pass.**
   *
   * Task 4.2 made `get_schema` return DD9's bare object, so
   * `fetchSchemaDetail`'s `[0]` unwrap (`use-schemas.ts:41-43`) resolves
   * correctly **by falling through**: `body.schemas` is `undefined` → falsy →
   * `Array.isArray(body)` is false → `raw = body`. The unwrap is still there and
   * still wrong; against the new backend it is simply not reached. Task 5's
   * checkpoint recorded the same thing, and warned that its apparent
   * harmlessness must not become an argument for keeping it tolerant. The next
   * test is where it still fails.
   */
  it('displays the clicked row\'s fields and not the first listing row\'s', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_beta');

    const dialog = await openDetail('uploaded_schema_beta');

    // The requested schema's fields.
    expect(within(dialog).getByText('region_code')).toBeInTheDocument();
    expect(within(dialog).getByText('order_total')).toBeInTheDocument();
    expect(within(dialog).getByText('shipped_at')).toBeInTheDocument();
    // Not the first row's.
    expect(within(dialog).queryByText('email')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('customer_id')).not.toBeInTheDocument();
    // And the modal names the schema it was given.
    expect(within(dialog).getByText(/uploaded_schema_beta/)).toBeInTheDocument();
  });

  /**
   * **RED — this is the `[0]` unwrap's live defect, and the branch DD9 deletes.**
   *
   * When the per-id response arrives in the **collection-envelope** shape
   * `{"schemas": [...]}` — the shape the pre-p35 router returned for a per-id
   * request (`router.py:329-332`), and the shape a regressed per-id branch would
   * return again — `use-schemas.ts:41-42` takes element `[0]` of it. The modal
   * then describes the **first row of the listing** under the id the user
   * clicked, which is the reported bug verbatim.
   *
   * R1.3 forbids that regardless of which shape the producer sent: the modal
   * must never present another schema's field list under the requested id. So
   * the assertion is on the *absence* of the first row's fields, not on any
   * particular replacement — task 6.3 replaces the unwrap with a plain
   * `normalizeSchema(body)`, which yields no fields from an envelope and renders
   * R1.4's empty state instead. `LGR-28b4d75-5` (**template / ratified**) is why
   * the fix is a deletion rather than `body.schemas?.[0] ?? body`: a tolerant
   * consumer is indistinguishable from the bug, so it cannot detect the producer
   * drifting.
   */
  it('never presents another schema\'s field list under the requested id, whatever shape the API sent', async () => {
    // Every per-id request answers with the whole collection.
    routeApi(() => ({ schemas: [ALPHA_DETAIL, BETA_DETAIL, GLUE_DETAIL] }));
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_beta');

    const dialog = await openDetail('uploaded_schema_beta');

    expect(within(dialog).queryByText('email')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('customer_id')).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/uploaded_schema_alpha/)).not.toBeInTheDocument();
  });
});

describe('Schema_Detail_Modal — the field count lives in the modal, not the listing', () => {
  /**
   * R1.3 used to require the modal's count to equal the listing's Fields
   * column. That column is gone: the listing states a count only for Glue
   * rows, so it read "—" on every uploaded and S3 Tables row (2026-10-10,
   * reported as "always empty"). What remains of R1.3 is its first half — the
   * modal shows that row's own field list — and that no listing cell can claim
   * a count the modal contradicts, because none is shown.
   */
  it('shows no Fields column in the listing', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_beta');

    expect(screen.queryByRole('columnheader', { name: /^Fields$/ })).not.toBeInTheDocument();
  });

  it("displays the uploaded row's own field count once opened", async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_beta');

    const dialog = await openDetail('uploaded_schema_beta');

    expect(modalFieldCount(dialog)).toBe(BETA_DETAIL.fields.length);
  });

  it("displays a Glue row's field count once opened", async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('salesdb.orders');

    const dialog = await openDetail('salesdb.orders');

    expect(modalFieldCount(dialog)).toBe(GLUE_FIELDS.length);
  });
});

/* ================================================================== */
/*  p45-unmapped-target-field-visibility task 14.1                     */
/*  The Fields table's `Required (declared by the schema)` column.      */
/* ================================================================== */

/**
 * **Where these expectations come from.** `LGR-28b4d75-5` (**template /
 * ratified**): *"a test written from the producer's output cannot detect the
 * producer drifting, because the output is its own oracle."* So nothing below is
 * read off the rendered column. Each expectation traces to a source outside the
 * component:
 *
 * | Expectation | Authority |
 * |---|---|
 * | a written status per field, beside name / type / description / context | R5.1 |
 * | the status comes from the Field_Nullable the API already emits, with no schema body parsed in the browser | R5.2 |
 * | absence of `nullable` renders Not_Established **in text** and never "optional" | R5.3, C4 / `LGR-4854b3d-1`, do-NOT 5 |
 * | `Required` / `Optional` / `Not established` as the three cell strings | design §4.6, carried verbatim in `utils/field-requirement.ts` |
 * | one expression for the vocabulary, in `utils/field-requirement.ts` | R5.5, `LGR-4854b3d-2` |
 * | the heading's `(declared by the schema)` suffix | C5 / `LGR-acca05d-3`, and the convention already on this table at `SchemaManagementPage.tsx:242-245` |
 * | an empty `fields` list makes **no** required/optional claim | R5.7 |
 *
 * **The `nullable` values these tests read were already in this file** —
 * `GLUE_FIELDS` (both `true`), `ALPHA_DETAIL.fields` (both `false`) and
 * `BETA_DETAIL.fields` (all `true`) have carried them since p35 task 6.1, and no
 * assertion read them until now. That is the point of extending this file rather
 * than adding one: the fixtures already described the wire correctly, and the
 * fact simply reached no reader (design C12).
 *
 * **The one case the fixtures could not already express** is a field with **no
 * `nullable` key**, because before p45 the producer invented one. `MIXED_DETAIL`
 * adds it, and it is the case the column exists for.
 *
 * ## Scope, and what is deliberately left to task 1.1's file
 *
 * `SchemaManagementPage.empty-field-list.test.tsx` pins `EmptyFieldList`'s two
 * statements and their two testids in full, and it is **not** edited by this
 * task — it exists precisely so this column can be read as additive. What is
 * asserted here instead is R5.7's *second* half, which task 1.1 explicitly
 * excluded: that where `fields` is empty the surface makes **no required/optional
 * claim at all**. The two halves are checked in the two places rather than
 * duplicated in one.
 *
 * That distinction is load-bearing rather than tidy: `_resolve_row_fields`'
 * docstring (`handlers/schemas.py:1501-1509`) records that `get_schema` still
 * cannot tell an unreadable body from a schema declaring none, so a
 * required/optional column must not imply the field list is complete.
 *
 * Requirements: 5.1, 5.2, 5.3, 5.5, 5.6, 5.7
 */
describe('Fields table — R5.1/R5.2/R5.3, the three requirement states', () => {
  /**
   * A stated `nullable: false` is a stated **requirement**, not a missing value:
   * the producer computes the key as the negation of required
   * (`handlers/schemas.py:1394`, `field_name not in required`).
   */
  it('renders Required for a field the source declared required', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');
    const claim = requirementClaim(dialog, 'account_ref');

    expect(claim.testId).toBe('requirement-required');
    expect(claim.text).toBe('Required');
  });

  /** A stated `nullable: true` is a stated **option**. */
  it('renders Optional for a field the source declared optional', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');
    const claim = requirementClaim(dialog, 'nickname');

    expect(claim.testId).toBe('requirement-optional');
    expect(claim.text).toBe('Optional');
  });

  /**
   * **The case the column exists for — R5.3, and the assertion a later reader has
   * to argue with before collapsing the third state.**
   *
   * `postal_code` carries **no `nullable` key**, so the source never established
   * a requirement. R5.3: *"WHERE the source did not state nullability, THE
   * Schema_Detail_View SHALL render Not_Established in text and SHALL NOT render
   * 'optional'."* Both halves are asserted, and the second is asserted as a
   * substring so a later rewording that reintroduces the word fails here
   * (do-NOT 5).
   *
   * The forbidden spelling is `nullable ?? true` at any reader (C12, do-NOT 16):
   * it reports *"the source did not say"* as *"optional"*, which is C4 /
   * `LGR-4854b3d-1`'s collapse in the permissive direction. A blank cell is
   * equally forbidden — indistinguishable from a failed load, which is the
   * confusion `EmptyFieldList` already exists to remove — so the text is asserted
   * to be non-empty as well as to say what it says.
   */
  it('renders Not established, and never "optional", for a field with no nullable key', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');
    const claim = requirementClaim(dialog, 'postal_code');

    expect(claim.testId).toBe('requirement-not-established');
    expect(claim.text).toBe('Not established');
    // Not a blank cell, and not the permissive collapse.
    expect(claim.text.length).toBeGreaterThan(0);
    expect(claim.text.toLowerCase()).not.toContain('optional');
    expect(within(fieldRow(dialog, 'postal_code')).queryByTestId('requirement-optional')).toBeNull();
  });

  /**
   * **The branch asserted as a branch — C4 / `LGR-4854b3d-1`.**
   *
   * The three tests above would each still pass if two of the states shared a
   * testid, or if every row rendered the same word. `LGR-4854b3d-1` ratifies that
   * a three-state value is *"never collapsed"*, and this is that rule on one
   * schema: three fields, three distinct states, three distinct strings, one
   * claim per row.
   */
  it('keeps the three states as three distinct claims on one schema', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');
    const required = requirementClaim(dialog, 'account_ref');
    const optional = requirementClaim(dialog, 'nickname');
    const notEstablished = requirementClaim(dialog, 'postal_code');

    // Three testids, not two.
    expect(new Set([required.testId, optional.testId, notEstablished.testId]).size).toBe(3);
    // Three strings, not two.
    expect(new Set([required.text, optional.text, notEstablished.text]).size).toBe(3);
    // Exactly one claim per field row, and no extra rows claiming anything.
    expect(allRequirementClaims(dialog)).toHaveLength(MIXED_DETAIL.fields.length);
    expect(modalFieldCount(dialog)).toBe(MIXED_DETAIL.fields.length);
  });

  /**
   * R5.2 — the status is sourced from the Field_Nullable the schemas API already
   * emits, and **no schema body is parsed in the browser**.
   *
   * The evidence available in jsdom is that the only bytes this component ever
   * sees are the `/schemas` and `/schemas/{id}` responses: `apiClient.get` is the
   * single stub, and `routeApi` throws on any other path. So a column that
   * derived the status from a fetched schema document would have to request one,
   * and the request would fail the harness rather than pass silently.
   *
   * The positive half is `ALPHA_DETAIL`, whose two fields the API states as
   * `nullable: false` — values this file has carried since p35 and never read.
   */
  it('sources the status from the API response, with no request beyond the two schema paths', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_alpha');

    const dialog = await openDetail('uploaded_schema_alpha');

    // Both of ALPHA's fields are stated `nullable: false` on the wire.
    expect(requirementClaim(dialog, 'email').testId).toBe('requirement-required');
    expect(requirementClaim(dialog, 'customer_id').testId).toBe('requirement-required');

    // Nothing was fetched but the listing and the one detail.
    const paths = getMock.mock.calls.map((c: unknown[]) => c[0] as string);
    expect(paths.every((p) => p === '/schemas' || /^\/schemas\/[^/]+$/.test(p))).toBe(true);
  });

  /**
   * The complement, on fixtures that were also already in this file: `BETA_DETAIL`
   * states `nullable: true` for all three of its fields, so all three read
   * `Optional` and none reads `Required`.
   */
  it('renders Optional for every field of a schema that states them all nullable', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_beta');

    const dialog = await openDetail('uploaded_schema_beta');

    for (const field of ['region_code', 'order_total', 'shipped_at']) {
      expect(requirementClaim(dialog, field).testId).toBe('requirement-optional');
    }
    expect(within(dialog).queryByTestId('requirement-required')).toBeNull();
    expect(within(dialog).queryByTestId('requirement-not-established')).toBeNull();
  });
});

describe('Fields table — R5.1/R5.5, the column and its heading', () => {
  /**
   * **C5 / `LGR-acca05d-3` — the heading names its authority.**
   *
   * That record's accepted cost was *"a second authority for a field's
   * description alongside the schema's own, which Requirement 1.6 keeps legible
   * by putting them under distinct headings"*, and the reason is written at
   * `SchemaManagementPage.tsx:242-245`. The new column is a **third** fact on the
   * row, so it takes the same convention rather than inventing one: `Required
   * (declared by the schema)` sits with `Description (declared by the schema)`,
   * both distinct from `Context (added by a person)`.
   *
   * Asserted as three headings present together, because the failure worth
   * catching is a heading that drops its authority suffix and leaves a reader
   * unable to tell which of the three columns a person can write to.
   */
  it('heads the column with its authority, beside the other two authorities', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');

    expect(within(dialog).getByText('Required (declared by the schema)')).toBeInTheDocument();
    expect(within(dialog).getByText('Description (declared by the schema)')).toBeInTheDocument();
    expect(within(dialog).getByText('Context (added by a person)')).toBeInTheDocument();
  });

  /**
   * R5.1 — the status is rendered *alongside* the columns the table renders
   * today, not instead of any of them. The four incumbent columns are still
   * reachable per row.
   */
  it('leaves the four existing columns rendering', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_mixed');

    const dialog = await openDetail('uploaded_schema_mixed');
    const row = fieldRow(dialog, 'account_ref');
    const text = (row.textContent ?? '').replace(/\s+/g, ' ');

    expect(text).toContain('account_ref'); // Field name
    expect(text).toContain('string'); // Type
    expect(text).toContain('Account key'); // Description (declared by the schema)
    expect(text).toContain('\u2014'); // Context (added by a person) — none written
    expect(text).toContain('Required'); // the new column
  });
});

describe('Fields table — R5.7, an empty field list makes no requirement claim', () => {
  /**
   * **R5.7's second half, which task 1.1's file deliberately does not assert.**
   *
   * `SchemaManagementPage.tsx:150` branches on `(schema.fields ?? []).length ===
   * 0` **before** the Table, so where `fields` is empty the column does not exist
   * to make a claim with. That is what makes this column additive rather than a
   * change to `EmptyFieldList`, and it is why R5.7's *"SHALL make no
   * required/optional claim"* is a structural property here rather than a copy
   * decision.
   *
   * It matters because the two branches mean different things and neither
   * supports a requirement claim: `_resolve_row_fields`' docstring
   * (`handlers/schemas.py:1501-1509`) records that `get_schema` cannot tell an
   * unreadable body from a schema declaring none. A column asserting "Not
   * established" for zero rows would be a claim about a field list that was never
   * read.
   */
  it('reaches the declares-none statement and claims nothing about requirements', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('uploaded_schema_no_fields');

    const dialog = await openDetail('uploaded_schema_no_fields');

    // The statement task 1.1 pins is still the one reached, unchanged.
    expect(within(dialog).getByTestId('schema-declares-no-fields')).toBeInTheDocument();
    expect(within(dialog).queryByTestId('schema-fields-unresolved')).toBeNull();

    // And no required/optional claim is made — not even the heading.
    expect(allRequirementClaims(dialog)).toHaveLength(0);
    expect(within(dialog).queryByText('Required (declared by the schema)')).toBeNull();
  });

  /**
   * The other branch. `field_count == null` is **no count having been
   * established** — a different fact from a stated zero (C4 /
   * `LGR-4854b3d-1`) — and it equally cannot support a requirement claim.
   */
  it('reaches the unresolved statement and claims nothing about requirements', async () => {
    routeApi();
    render(<SchemaManagementPage />, { wrapper });
    await screen.findByText('s3_tables_schema_unresolved');

    const dialog = await openDetail('s3_tables_schema_unresolved');

    expect(within(dialog).getByTestId('schema-fields-unresolved')).toBeInTheDocument();
    expect(within(dialog).queryByTestId('schema-declares-no-fields')).toBeNull();

    expect(allRequirementClaims(dialog)).toHaveLength(0);
    expect(within(dialog).queryByText('Required (declared by the schema)')).toBeNull();
  });
});
