// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 6.2 (design B6) — Schema_Picker characterization.
 *
 * **Expectations come from R5.1, R5.2, R6.1 and R6.4 — never from current
 * output, which contains the defects.** R8.4 makes this file a deliverable:
 * selection of a populated schema, selection of a zero-field schema, and
 * narrowing by typed text, plus R6.4's keyboard-only path.
 *
 * **Six of its ten tests start RED, deliberately.** Task 6.3 (design B7) closes
 * all six. A reviewer who sees the red and reverts has undone the only thing
 * that would have caught the defect (design §5 risk 9). Each test states its
 * expected colour and the mechanism behind it. Two of the four green ones are
 * **tripwires against the wrong fix** and must stay green.
 *
 * ---
 *
 * ## This is filling a real void
 *
 * The symbol graph flags `ExistingSchemaSelector` (`WizardStepSchema.tsx:53`),
 * `SchemaPreviewTable` (`:17`), `useSchemas` and `fetchSchemas` as having **no
 * covering tests**. `WizardStepSchema.test.tsx:68`/`:99` render the selector but
 * assert only its `FormField` label, and both that file and
 * `WizardStepSchema.upload-commit.test.tsx` replace `../../../hooks` with a
 * factory returning a canned `useSchemas`, so neither `fetchSchemas` nor
 * `normalizeSchema` has ever run in a test.
 *
 * This file therefore mocks **`apiClient`, not `../../../hooks`** — the same
 * choice task 6.1 made in `SchemaManagementPage.detail-modal.test.tsx`, for the
 * same two reasons: the real `useSchemas` / `fetchSchemas` / `normalizeSchema` /
 * `normalizeField` are part of what is under test, and held-assertion inventory
 * row 5 applies: a `vi.mock` factory **replaces** the module, so any export it
 * omits is `undefined`; the component then calls `undefined()`
 * **during render, inside the test that renders it**. The failure is a stack
 * trace naming a value that is not a function — not an assertion diff — and the
 * file still collects and still reports per test. Amend the factory before
 * adding a hook to a component the factory's tests render. Nothing here touches
 * that factory; task 8.7 owns it.
 *
 * ## Held-assertion inventory row 12 is a constraint this file leans on
 *
 * `WizardStepSchema.test.tsx:68` and `:99` assert
 * `getByText('Select an existing schema')` — the `FormField` label at
 * `WizardStepSchema.tsx:69`. They survive task 6.3 **by construction**, and
 * that is a constraint on it: R6's search must land inside this same labelled
 * control, not in a differently-labelled one. This file uses the same label as
 * its "listing has loaded" signal, so it is a third dependency on it rather
 * than an amendment. Neither row-12 assertion is touched.
 *
 * ## The three-way `fieldCount` distinction, which task 4.4 introduced
 *
 * DD4 moved field resolution off the listing, so "zero-field schema" is now
 * ambiguous in a listing row and the three states are **not** interchangeable:
 *
 * | Listing row | Meaning | R5.1 / R5.2 |
 * |---|---|---|
 * | `fieldCount: 0` | the source stated a count and it is zero | **refuse this one** |
 * | `fieldCount: null` | no count was established — every uploaded (`schemas.py:578`) and every S3 Tables (`:641`) row | **must NOT be refused** |
 * | a positive number | Glue, free from the `get_tables` page (`:504-507`) | fine |
 *
 * A guard keyed on `fields.length === 0` instead of on `fieldCount === 0` would
 * refuse **every** uploaded and S3 Tables schema — task 4.4's notes call that
 * breaking R5.1's guard in the worst direction. Two tests below exist purely as
 * the tripwire for it and are **green now and must stay green**.
 *
 * Fixtures are the post-4.4 wire shapes read from the producer: a Glue row
 * carries `fields` **and** a real `fieldCount` (`schemas.py:507`); an uploaded
 * or S3 Tables row carries `fieldCount: null` and **no `fields` key at all**.
 * Detail responses are DD9's bare schema object as `_schema_detail` builds it
 * (`schemas.py:1040-1101`).
 *
 * Requirements: 5.1, 5.2, 6.1, 6.4, 8.4
 *
 * ## Amended: the picker offers uploaded schemas only
 *
 * The Existing tile now lists `isUploadedSchema` rows alone (see the comment on
 * `ExistingSchemaSelector`). A Glue or S3 Tables pick got no target-schema
 * object, so code generation and code testing ran without a target schema, and
 * the Glue list was mostly this app's own tables. This file was written when
 * every source was offered, so its cases moved deliberately rather than being
 * deleted:
 *
 * - The Glue and S3 Tables rows stay in the listing fixture, and a new test
 *   asserts they are NOT offered.
 * - The populated and zero-field selections now use uploaded rows. An uploaded
 *   row carries `fieldCount: null`, so both resolve through the detail
 *   endpoint, and the zero-field refusal fires once the detail reports no
 *   fields rather than from the listing's stated zero, which only Glue rows
 *   could carry.
 * - The `fieldCount: null` tripwire keeps its uploaded case and drops the S3
 *   Tables one, which is no longer selectable.
 * - The two filter tests narrow by name, since every option is now the same
 *   source type and the option no longer prints it.
 */
import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../../contexts/ToastContext';
import type { ReactNode } from 'react';

// The API boundary is the only thing stubbed. The hooks, the fetchers and both
// normalizers above it are the code under test.
const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
vi.mock('../../../services/api-client', () => ({
  apiClient: { get: getMock, post: postMock, put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { WizardStepSchema } from '../WizardStepSchema';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';
import CreatePipelinePage from '../../CreatePipelinePage';

/* ------------------------------------------------------------------ */
/*  Fixtures — the post-task-4.4 listing shape and DD9's detail shape.  */
/* ------------------------------------------------------------------ */

const POPULATED_FIELDS = [
  { name: 'order_id', type: 'string', nullable: false, description: 'Order key', context: '' },
  { name: 'order_total', type: 'double', nullable: true, description: '', context: '' },
  { name: 'ordered_at', type: 'timestamp', nullable: true, description: '', context: '' },
];

/** Glue: listed by `/schemas`, never offered by the picker. */
const GLUE_ROW = {
  schemaId: 'glue_salesdb_orders',
  name: 'salesdb.orders',
  source: 'glue_catalog',
  targetType: 'Glue Catalog',
  lastModified: '2026-08-01T10:00:00+00:00',
  fields: POPULATED_FIELDS,
  fieldCount: POPULATED_FIELDS.length,
};

/** An uploaded schema whose detail declares fields: the populated selection. */
const POPULATED_ROW = {
  schemaId: 'orders_target_v2',
  name: 'orders_target_v2',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-01T10:00:00+00:00',
  fieldCount: null,
};

/** An uploaded schema whose detail declares NO fields: the schema R5.1 and
 *  R5.2 refuse, on the only source the picker offers. */
const ZERO_FIELD_ROW = {
  schemaId: 'empty_upload',
  name: 'empty_upload',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-02T10:00:00+00:00',
  fieldCount: null,
};

/** `fieldCount: null`, no `fields` key — every uploaded row after task 4.4.
 *  Must NOT be refused. */
const LAZY_UPLOADED_ROW = {
  schemaId: 'uploaded_schema_beta',
  name: 'uploaded_schema_beta',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-03T10:00:00+00:00',
  fieldCount: null,
};

/** S3 Tables: listed by `/schemas`, never offered by the picker. */
const LAZY_S3_ROW = {
  schemaId: 's3table_testingbucket_ns_customers',
  name: 'testingbucket.ns.customers',
  source: 's3_table_bucket',
  targetType: 'S3 Tables (Iceberg)',
  lastModified: '2026-08-04T10:00:00+00:00',
  fieldCount: null,
};

const LISTING = {
  schemas: [GLUE_ROW, POPULATED_ROW, ZERO_FIELD_ROW, LAZY_UPLOADED_ROW, LAZY_S3_ROW],
  sourcesFailed: [],
};

const UPLOADED_FIELDS = [
  { name: 'region_code', type: 'string', nullable: true, description: 'ISO region', context: '' },
  { name: 'customer_id', type: 'string', nullable: false, description: '', context: '' },
];

const S3_FIELDS = [
  { name: 'customer_key', type: 'string', nullable: false, description: 'Warehouse key', context: '' },
];

/** DD9's bare schema object, per source. */
const DETAILS: Record<string, unknown> = {
  glue_salesdb_orders: { ...GLUE_ROW, fields: POPULATED_FIELDS },
  orders_target_v2: { ...POPULATED_ROW, fields: POPULATED_FIELDS },
  empty_upload: { ...ZERO_FIELD_ROW, fields: [] },
  uploaded_schema_beta: { ...LAZY_UPLOADED_ROW, fields: UPLOADED_FIELDS },
  s3table_testingbucket_ns_customers: { ...LAZY_S3_ROW, fields: S3_FIELDS },
};

/** Field names the *detail* endpoint reports, keyed by schema id. */
const DETAIL_FIELD_NAMES: Record<string, string[]> = {
  orders_target_v2: POPULATED_FIELDS.map((f) => f.name),
  uploaded_schema_beta: UPLOADED_FIELDS.map((f) => f.name),
  s3table_testingbucket_ns_customers: S3_FIELDS.map((f) => f.name),
};

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

/**
 * Routes `apiClient.get` the way the API routes it. Everything the wizard's
 * other steps ask for resolves to `{}`, which every sibling fetcher normalizes
 * to an empty list — this file is about the schema step.
 */
function routeApi() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return LISTING;
    const match = /^\/schemas\/(.+)$/.exec(path);
    if (match) return DETAILS[decodeURIComponent(match[1])] ?? {};
    if (path.startsWith('/pipelines')) return { pipelines: [] };
    return {};
  });
}

function queryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Providers({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={queryClient()}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/new']}>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

/**
 * Holds real wizard state so a commit (or the absence of one) is observable —
 * a `vi.fn()` spy would show the call but not prove the value lands. Same shape
 * as `WizardStepSchema.upload-commit.test.tsx`'s harness.
 */
function Harness() {
  const [state, setState] = useState<WizardState>({
    ...INITIAL_WIZARD_STATE,
    schemaSource: 'existing',
    // Keeps the pre-selection effect from resetting the mode to 'detected'.
    schemaSourceExplicitlySet: true,
  });
  return (
    <>
      <WizardStepSchema state={state} onChange={setState} />
      <div data-testid="probe-schema-fields">{JSON.stringify(state.schemaFields)}</div>
      <div data-testid="probe-existing-schema-id">{state.existingSchemaId ?? ''}</div>
    </>
  );
}

/** Renders the schema step in `existing` mode and waits for the listing. */
async function renderPicker() {
  routeApi();
  render(
    <Providers>
      <Harness />
    </Providers>,
  );
  // Inventory row 12's label, used here as the loaded signal (see the header).
  await screen.findByText('Select an existing schema');
}

function committedFields(): WizardState['schemaFields'] {
  return JSON.parse(screen.getByTestId('probe-schema-fields').textContent || '[]');
}

function committedFieldNames(): string[] {
  return committedFields().map((f) => f.name);
}

function trigger(): HTMLElement {
  return screen.getByTestId('existing-schema-select').querySelector('button')!;
}

/** Cloudscape's Select opens on `mouseDown`, not on `click`. */
function openPicker() {
  fireEvent.mouseDown(trigger());
}

function optionLabels(): string[] {
  return screen.queryAllByRole('option').map((o) => o.textContent ?? '');
}

function optionFor(label: string): HTMLElement {
  const option = screen.queryAllByRole('option').find((o) => (o.textContent ?? '').includes(label));
  if (!option) throw new Error(`no option for ${label}; options: ${JSON.stringify(optionLabels())}`);
  return option;
}

/** Selects by visible label. Cloudscape commits the option on `mouseUp`. */
function selectByLabel(label: string) {
  openPicker();
  fireEvent.mouseUp(optionFor(label));
}

/**
 * R5.2's inline statement, matched by meaning rather than by wording: any text
 * saying the selected schema has (or declares) no fields.
 *
 * `SchemaPreviewTable`'s existing placeholder "No schema fields available"
 * (`WizardStepSchema.tsx:18`) deliberately does not match, and that is not a
 * regex accident: the placeholder renders identically for a field list that is
 * empty and for one that was never resolved, so it cannot carry R5.2's
 * statement. Telling those two apart is the whole point of DD4's
 * `fieldCount: null` and of R1.4's empty state.
 */
function zeroFieldStatements(): HTMLElement[] {
  return screen.queryAllByText(/(declares|has|with)?\s*no fields/i);
}

beforeEach(() => {
  sessionStorage.clear();
  getMock.mockReset();
  postMock.mockReset();
  postMock.mockResolvedValue({ success: true });
});

/* ================================================================== */
/*  R5.1 / R5.2 — what the picker may and may not commit               */
/* ================================================================== */

describe('Schema_Picker — which schemas are offered', () => {
  /**
   * A Glue or S3 Tables pick got no target-schema object, so code generation
   * and code testing ran without a target schema. Both rows are in the listing
   * fixture; neither may be offered.
   */
  it('offers uploaded schemas only', async () => {
    await renderPicker();
    openPicker();

    const labels = optionLabels();
    expect(labels.some((l) => l.includes('salesdb.orders')), 'a Glue Data Catalog table was offered').toBe(false);
    expect(labels.some((l) => l.includes('testingbucket.ns.customers')), 'an S3 Tables table was offered').toBe(false);
    for (const id of ['orders_target_v2', 'empty_upload', 'uploaded_schema_beta']) {
      expect(labels.some((l) => l.includes(id)), `uploaded schema ${id} was not offered`).toBe(true);
    }
  });
});

describe('Schema_Picker — selecting a populated schema (R8.4)', () => {
  it('commits the selected schema and previews the fields it declares', async () => {
    await renderPicker();

    selectByLabel('orders_target_v2');

    expect(screen.getByTestId('probe-existing-schema-id').textContent).toBe('orders_target_v2');
    await waitFor(() => {
      expect(committedFieldNames()).toEqual(DETAIL_FIELD_NAMES.orders_target_v2);
    });
    const preview = screen.getByTestId('schema-preview-table');
    for (const name of DETAIL_FIELD_NAMES.orders_target_v2) {
      expect(within(preview).getByText(name)).toBeInTheDocument();
    }
  });
});

describe('Schema_Picker — selecting a zero-field schema (R5.1, R5.2)', () => {
  /**
   * **RED at p35 — R5.2's inline statement did not exist.** (The case then was
   * a Glue row stating `fieldCount: 0`; it is now an uploaded row whose detail
   * reports no fields, since only uploaded schemas are offered.)
   *
   * Selecting a schema that declares no fields rendered
   * `SchemaPreviewTable`'s generic "No schema fields available" placeholder and
   * nothing else: no statement that this schema declares no fields, and no
   * indication that it cannot be used. Task 6.3 adds R5.2's inline guard.
   *
   * Why this matters past the dashboard: `WizardStepSchema.tsx:76-81` maps
   * `schema?.fields ?? []` unconditionally, and `LGR-b304401-1`
   * (**template / ratified**) scopes the existing zero-field guard to
   * `schemaSource === 'upload'` **by decision**, so the `existing` path is
   * unguarded and a pipeline created this way reaches p33's `no_target_schema`
   * non-verification. The upload branch's auto-commit and the `mirror_source`
   * branch are that record's subject and are untouched here.
   */
  it('states inline that the schema declares no fields', async () => {
    await renderPicker();

    selectByLabel('empty_upload');

    // An uploaded row states no count, so the refusal waits for the detail.
    await waitFor(() => {
      expect(
        zeroFieldStatements().length,
        'R5.2 requires an inline statement that the selected schema declares no fields; ' +
          'SchemaPreviewTable\'s "No schema fields available" placeholder cannot serve as one, ' +
          'because it renders the same for an unresolved field list',
      ).toBeGreaterThan(0);
    });
  });
});

describe('Schema_Picker — a schema whose field count was never established (R5.1)', () => {
  /**
   * **GREEN, and it must stay green — this is the worst-direction tripwire.**
   *
   * After task 4.4 an uploaded or S3 Tables row carries `fieldCount: null` and
   * **no `fields` key**, so `normalizeSchema` resolves `fields` to `[]`
   * (`use-schemas.ts:18`) for every one of them. A zero-field guard written
   * against `fields.length === 0` rather than against `fieldCount === 0` would
   * therefore refuse **every** uploaded and every S3 Tables schema — the guard
   * broken in the worst direction. R5.1 refuses a schema that *declares* zero
   * fields; "not established" is not "zero".
   */
  it.each([['uploaded_schema_beta', 'uploaded_schema_beta']])('does not refuse %s, whose source reports fieldCount: null', async (label, id) => {
    await renderPicker();

    selectByLabel(label);

    expect(screen.getByTestId('probe-existing-schema-id').textContent).toBe(id);
    expect(
      zeroFieldStatements(),
      `${label} reports fieldCount: null, not 0 — refusing it would reject every uploaded ` +
        'and S3 Tables schema',
    ).toHaveLength(0);
  });

  /**
   * **RED — and this red is broader than the design anticipated.**
   *
   * Task 4.4 removed the per-row `s3:GetObject` from the listing, so an
   * uploaded or S3 Tables row arrives with no `fields` key and the field list
   * lives only behind `GET /api/schemas/{id}`. `WizardStepSchema.tsx:76-81`
   * still maps `schema?.fields ?? []` off the **listing** row, so it now
   * commits an **empty field list for every uploaded and every S3 Tables
   * schema** and the preview claims there are no fields — for schemas that have
   * them. The design read this site as R5.2's zero-field case only; after 4.4 it
   * is also every lazily-resolved schema.
   *
   * Task 6.3 closes it by resolving the detail before committing — the same move
   * held-assertion inventory row 6 prescribes for the sibling site at
   * `SchemaSourceSelector.tsx:128`, which hands `{ ...schema }` straight into
   * `PipelineFormModal`'s pipeline write.
   *
   * The option's own description has the same root: `WizardStepSchema.tsx:62`
   * writes `${s.fields?.length ?? 0} fields`, so these two rows are offered as
   * "0 fields" while declaring some. That line is design §5 risk 3's sixth
   * `?? 0` consumer and task 6.3 owns it; it is left unasserted here so this
   * file does not pin an option's wording.
   */
  it.each([['uploaded_schema_beta', 'uploaded_schema_beta']])('surfaces the fields %s actually declares', async (label, id) => {
    await renderPicker();

    selectByLabel(label);

    await waitFor(() => {
      expect(committedFieldNames()).toEqual(DETAIL_FIELD_NAMES[id]);
    });
    const preview = screen.getByTestId('schema-preview-table');
    for (const name of DETAIL_FIELD_NAMES[id]) {
      expect(within(preview).getByText(name)).toBeInTheDocument();
    }
  });
});

/* ================================================================== */
/*  R6.1 / R6.4 — narrowing by typed text, from the keyboard alone     */
/* ================================================================== */

describe('Schema_Picker — narrowing by typed text (R6.1)', () => {
  /**
   * **RED — the picker has no search at all.**
   *
   * The `Select` at `WizardStepSchema.tsx:70-86` carries no `filteringType`, so
   * it defaults to `'none'` and renders no filter input; the option list is
   * every schema, unpaginated. Task 6.3 adds `filteringType="auto"`, the form
   * already used at `DragDropMappingEditor.tsx:168` and `:188` (R6.6).
   */
  it('accepts typed text and narrows the offered options', async () => {
    await renderPicker();
    openPicker();

    const filter = screen.queryByRole('combobox');
    expect(
      filter,
      'R6.1 requires the picker to accept typed text; the Select renders no filter input',
    ).not.toBeNull();

    fireEvent.change(filter as HTMLInputElement, { target: { value: 'orders' } });

    const labels = optionLabels();
    expect(labels).toHaveLength(1);
    expect(labels[0]).toContain('orders_target_v2');
  });
});

describe('Schema_Picker — the search is keyboard-operable (R6.4)', () => {
  /**
   * **RED — for the same missing control.**
   *
   * No mouse event is used: focus lands on the trigger the way a Tab would
   * leave it, `ArrowDown` opens the dropdown, the text goes into whatever the
   * open dropdown focused, and `ArrowDown` + `Enter` commits. Everything except
   * the filter input already works today, which is why R6.4 is about the search
   * rather than about selection.
   */
  it('narrows and selects without a pointer', async () => {
    await renderPicker();

    (trigger() as HTMLButtonElement).focus();
    // Cloudscape reads `keyCode`, so a `key`-only event is ignored.
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowDown', keyCode: 40 });

    const focused = document.activeElement as HTMLElement;
    expect(
      focused?.getAttribute('role'),
      'R6.4 requires the search to be operable from the keyboard alone, so opening the ' +
        'picker must put the caret in its filter input',
    ).toBe('combobox');

    fireEvent.change(focused, { target: { value: 'beta' } });
    expect(optionLabels()).toHaveLength(1);

    fireEvent.keyDown(focused, { key: 'ArrowDown', keyCode: 40 });
    fireEvent.keyDown(focused, { key: 'Enter', keyCode: 13 });

    expect(screen.getByTestId('probe-existing-schema-id').textContent).toBe('uploaded_schema_beta');
  });
});

/* ================================================================== */
/*  R5.2 in the wizard — the advance is what reaches the pipeline      */
/* ================================================================== */

/**
 * The step-level half of R5.2 needs the real `<Wizard>`, because "cannot
 * advance" is `CreatePipelinePage`'s `onNavigate` guard
 * (`CreatePipelinePage.tsx:487-495`), not the step's. Today that guard covers
 * `mirror_source` and `upload` only, so the `existing` path advances with an
 * empty committed field list and `handleSubmit` sends the selected id as
 * `target_schema` (`:283`).
 */
describe('CreatePipelinePage — the wizard cannot advance past a zero-field schema (R5.2, R5.1)', () => {
  function renderPage() {
    routeApi();
    render(
      <Providers>
        <CreatePipelinePage />
      </Providers>,
    );
  }

  function clickNext() {
    fireEvent.click(screen.getByRole('button', { name: /next/i }));
  }

  /** True while the Target Schema step is the active step. */
  function onSchemaStep(): boolean {
    return screen.queryByTestId('schema-source-tiles') !== null;
  }

  /** Name → Source → Target → Schema. An S3 Tables target has no step-2 guard. */
  async function walkToSchemaStep() {
    fireEvent.change(screen.getByPlaceholderText('my-etl-pipeline'), {
      target: { value: 'picker-characterization' },
    });
    clickNext(); // Name → Source
    clickNext(); // Source → Target
    clickNext(); // Target → Schema
    expect(onSchemaStep()).toBe(true);
    const tiles = screen.getByTestId('schema-source-tiles');
    fireEvent.click(within(tiles).getByRole('radio', { name: /^Existing/ }));
    await screen.findByText('Select an existing schema');
  }

  /**
   * **RED — nothing stops the advance today.**
   *
   * R5.2: selecting a schema that declares zero fields must prevent the wizard
   * advancing past the schema step. R5.1: such a schema must not be committed
   * as the pipeline's target schema — and the advance is where that becomes
   * observable, because `target_schema` is sent from `state.existingSchemaId`.
   */
  it('refuses to leave the schema step, and no pipeline is created', async () => {
    renderPage();
    await walkToSchemaStep();

    selectByLabel('empty_upload');
    // The refusal lands once the detail reports no fields; advance after it.
    await waitFor(() => expect(zeroFieldStatements().length).toBeGreaterThan(0));
    clickNext();

    expect(onSchemaStep()).toBe(true);
    expect(
      postMock.mock.calls.filter(([url]) => url === '/pipelines/provision'),
      'R5.1 — a schema declaring zero fields must not reach a pipeline as its target schema',
    ).toHaveLength(0);
  });

  /**
   * **GREEN, and it must stay green — the wizard-level half of the tripwire.**
   *
   * The guard R5.2 asks for must key on `fieldCount === 0`. Keyed on
   * `fields.length` it would block the schema step for every uploaded and every
   * S3 Tables schema, which is every schema a user uploads.
   */
  it('still advances for a schema whose field count was never established', async () => {
    renderPage();
    await walkToSchemaStep();

    selectByLabel('uploaded_schema_beta');
    clickNext();

    expect(onSchemaStep()).toBe(false);
  });
});
