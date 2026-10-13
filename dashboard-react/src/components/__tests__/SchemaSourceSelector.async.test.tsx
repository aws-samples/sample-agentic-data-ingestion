// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import type { Schema } from '../../types';

/* ------------------------------------------------------------------ */
/*  P29 R1.4 — Async_State_Triple on the schema-source wizard picker. */
/*                                                                    */
/*  A schema load failure MUST render an inline error + Retry rather  */
/*  than an empty dropdown that masquerades as "no schemas".          */
/* ------------------------------------------------------------------ */

/*
 * p35-schema-surfaces task 6.3 — held-assertion inventory row 7, amended **by
 * addition**, with all three P29 test names kept verbatim.
 *
 * Row 6 puts `SchemaSourceSelector` in this spec's change set: DD4 moved field
 * resolution off the listing, so an uploaded or S3 Tables row arrives with
 * `fieldCount: null` and **no `fields` key**, and the component's three
 * `${s.fields?.length ?? 0} fields` sites offered every one of them as
 * "0 fields". Row 7 is this file: its fixtures gain `field_count`, or the
 * amended component renders "fields not loaded" against fixtures that never
 * declared a count and the existing assertions read as regressions.
 *
 * Two amendments, both additive:
 *
 * 1. The success-case fixture is now two real listing rows carrying `field_count`
 *    (one `null`, one stated) instead of `data: []`. The P29 assertions on that
 *    test — no inline error, the picker rendered — are unchanged and still hold.
 * 2. `useSchemaDetail` joins the `vi.mock` factory. The component resolves the
 *    selected schema from `GET /api/schemas/{id}` before committing it, because
 *    the listing row it used to hand straight to `onSchemaSelect` goes into
 *    `PipelineFormModal`'s pipeline write (`PipelineFormModal.tsx:185`). The
 *    entry is added here deliberately rather than discovered, because of
 *    inventory row 5's mechanism: a `vi.mock` factory **replaces** the module,
 *    so any export it omits is `undefined`; the component then calls
 *    `undefined()` **during render, inside the test that renders it**. The
 *    failure is a stack trace naming a value that is not a function — not an
 *    assertion diff — and the file still collects and still reports per test.
 *    Amend the factory before adding a hook to a component the factory's tests
 *    render.
 */

let mockSchemasQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
let mockDetailQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  useSchemas: () => mockSchemasQuery,
  useSchemaDetail: () => mockDetailQuery,
}));

import SchemaSourceSelector from '../SchemaSourceSelector';

/* ------------------------------------------------------------------ */
/*  Fixtures — DD4's post-task-4.4 listing shapes.                     */
/* ------------------------------------------------------------------ */

/** `field_count: null` — no count established. Every uploaded row after 4.4. */
const UPLOADED_ROW: Schema = {
  schema_id: 'uploaded_schema_beta',
  schema_name: 'uploaded_schema_beta',
  source_type: 'uploaded',
  fields: [],
  field_count: null,
  created_at: '2026-08-03T10:00:00+00:00',
};

/** Glue: the one source whose listing states a real count, free from the
 *  `get_tables` page. */
const GLUE_ROW: Schema = {
  schema_id: 'glue_salesdb_orders',
  schema_name: 'salesdb.orders',
  source_type: 'glue_catalog',
  fields: [
    { field_name: 'order_id', field_type: 'string', description: 'Order key' },
    { field_name: 'order_total', field_type: 'double' },
  ],
  field_count: 2,
  created_at: '2026-08-01T10:00:00+00:00',
};

/** What `GET /api/schemas/{id}` returns for the uploaded row — the field list
 *  the listing no longer carries. */
const UPLOADED_DETAIL: Schema = {
  ...UPLOADED_ROW,
  fields: [
    { field_name: 'region_code', field_type: 'string', description: 'ISO region' },
    { field_name: 'customer_id', field_type: 'string' },
  ],
  field_count: 2,
};

const SCHEMAS = [UPLOADED_ROW, GLUE_ROW];

function renderSelector(onSchemaSelect = vi.fn()) {
  render(<SchemaSourceSelector onSchemaSelect={onSchemaSelect} />);
  return onSchemaSelect;
}

/** Cloudscape's Select opens on `mouseDown`, not on `click`. */
function openPicker() {
  fireEvent.mouseDown(screen.getByRole('button', { name: /Choose an uploaded schema/ }));
}

function optionFor(label: string): HTMLElement {
  const option = screen.queryAllByRole('option').find((o) => (o.textContent ?? '').includes(label));
  if (!option) throw new Error(`no option for ${label}`);
  return option;
}

beforeEach(() => {
  mockRefetch.mockReset();
  mockDetailQuery = { data: undefined, isLoading: false, isError: false, refetch: vi.fn() };
});

describe('SchemaSourceSelector — Async_State_Triple (R1.4)', () => {
  it('renders an inline error + Retry on schema load failure', () => {
    mockSchemasQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderSelector();
    expect(screen.getByTestId('schema-source-error')).toBeInTheDocument();
    expect(screen.getByTestId('schema-source-retry')).toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked', () => {
    mockSchemasQuery = { data: undefined, isLoading: false, isError: true, refetch: mockRefetch };
    renderSelector();
    fireEvent.click(screen.getByTestId('schema-source-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the schema picker when the query succeeds', () => {
    mockSchemasQuery = { data: SCHEMAS, isLoading: false, isError: false, refetch: mockRefetch };
    renderSelector();
    expect(screen.queryByTestId('schema-source-error')).not.toBeInTheDocument();
    expect(screen.getByText('Select schema')).toBeInTheDocument();
  });
});

describe('SchemaSourceSelector — p35 DD4, the field count the listing actually states', () => {
  /**
   * Inventory row 6's three count sites. `field_count: null` is not `0`: the
   * source established no count, so the option says so instead of claiming a
   * number the schema contradicts.
   */
  it('offers a schema whose count was never established as "fields not loaded"', () => {
    mockSchemasQuery = { data: SCHEMAS, isLoading: false, isError: false, refetch: mockRefetch };
    renderSelector();
    openPicker();

    expect(optionFor('uploaded_schema_beta').textContent).toMatch(/fields not loaded/);
    expect(optionFor('uploaded_schema_beta').textContent).not.toMatch(/0 fields/);
  });
});

describe('SchemaSourceSelector — p35 row 6, the detail is resolved before the schema is committed', () => {
  /**
   * The select handler used to hand `{ ...schema }` from the listing row
   * straight to `onSchemaSelect`, and that object goes into
   * `PipelineFormModal`'s pipeline write (`PipelineFormModal.tsx:185`). After
   * DD4 the row has no fields, so the pipeline would be built against a
   * field-less schema — R5.1's defect class on a surface the requirements never
   * named.
   */
  it('commits the resolved field list rather than the field-less listing row', async () => {
    mockSchemasQuery = { data: SCHEMAS, isLoading: false, isError: false, refetch: mockRefetch };
    mockDetailQuery = { data: UPLOADED_DETAIL, isLoading: false, isError: false, refetch: vi.fn() };
    const onSchemaSelect = renderSelector();

    openPicker();
    fireEvent.mouseUp(optionFor('uploaded_schema_beta'));

    await waitFor(() => expect(onSchemaSelect).toHaveBeenCalled());
    const [id, committed] = onSchemaSelect.mock.calls[0] as [string, Schema];
    expect(id).toBe('uploaded_schema_beta');
    // The listing row's `fields` is `[]`; these are the detail's.
    expect(committed.fields.map((f) => f.field_name)).toEqual(['region_code', 'customer_id']);
  });

  /** While the detail is in flight nothing is committed, and the preview does
   *  not claim the schema has no fields. */
  it('commits nothing while the field list is still being resolved', () => {
    mockSchemasQuery = { data: SCHEMAS, isLoading: false, isError: false, refetch: mockRefetch };
    mockDetailQuery = { data: undefined, isLoading: true, isError: false, refetch: vi.fn() };
    const onSchemaSelect = renderSelector();

    openPicker();
    fireEvent.mouseUp(optionFor('uploaded_schema_beta'));

    expect(onSchemaSelect).not.toHaveBeenCalled();
    expect(screen.queryByTestId('schema-preview-declares-no-fields')).not.toBeInTheDocument();
  });
});
