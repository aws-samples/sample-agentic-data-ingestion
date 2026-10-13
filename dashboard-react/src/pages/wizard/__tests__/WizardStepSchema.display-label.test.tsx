// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 4.3 — the Schema_Picker option shows the stored label.
 *
 * Requirement 1.5:
 *
 * > WHEN a Display_Label write succeeds, THE Schema_Picker SHALL display the new
 * > label.
 *
 * **No new code — the branch simply starts being taken.** The option's label is
 * `schemaDisplayName(s)` (`WizardStepSchema.tsx:175`), whose first term is
 * `display_label`; task 2.3 made the listing carry it, so the picker gains the
 * label with no frontend change. Every existing fixture in
 * `WizardStepSchema.picker.test.tsx` predates that and carries no `displayLabel`,
 * so the label term of `schemaDisplayName` has never been exercised here — it was
 * confirmed only by reading the code. Design §6 lists this in the non-property
 * table: one interaction at one state, with no input space to quantify over.
 *
 * ## Mocking
 *
 * `apiClient` only — never `../../../hooks`, the same choice
 * `WizardStepSchema.picker.test.tsx` made, so the real `useSchemas`,
 * `fetchSchemaListing` and `normalizeSchema` run and the wire key `displayLabel`
 * reaching the option label is the thing under test. It also keeps clear of
 * held-assertion inventory row 5: a `vi.mock` factory **replaces** the module, so
 * any export it omits is `undefined`; the component then calls `undefined()`
 * **during render, inside the test that renders it**. The failure is a stack
 * trace naming a value that is not a function — not an assertion diff — and the
 * file still collects and still reports per test. Amend the factory before
 * adding a hook to a component the factory's tests render.
 *
 * `utils/schema-display.ts` is deliberately **not** mocked: `schemaDisplayName`
 * is part of what is under test (`WizardStepSchema.picker.test.tsx:30-31`).
 *
 * Requirements: 1.5
 */
import { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../../contexts/ToastContext';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../../services/api-client', () => ({
  apiClient: { get: getMock, post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { WizardStepSchema } from '../WizardStepSchema';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

/* ------------------------------------------------------------------ */
/*  Fixtures — the post-task-2.3 listing shape                         */
/* ------------------------------------------------------------------ */

/** A row task 2.3 resolved a stored label for. */
const LABELLED_ROW = {
  schemaId: 'uploaded_schema_1785844048999',
  name: 'uploaded_schema_1785844048999',
  displayLabel: 'Customer master',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-05T09:12:03+00:00',
  fieldCount: null,
};

/** A row with **no** `displayLabel` key at all — Requirement 1.2's absence. */
const UNLABELLED_ROW = {
  schemaId: 'uploaded_schema_1785844048926',
  name: 'uploaded_schema_1785844048926',
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-04T11:47:29+00:00',
  fieldCount: null,
};

const LABELLED_FIELDS = [
  { name: 'customer_id', type: 'string', nullable: false, description: 'Customer key', context: '' },
];

const LISTING = { schemas: [LABELLED_ROW, UNLABELLED_ROW], sourcesFailed: [] };

const DETAILS: Record<string, unknown> = {
  [LABELLED_ROW.schemaId]: { ...LABELLED_ROW, fields: LABELLED_FIELDS },
  [UNLABELLED_ROW.schemaId]: { ...UNLABELLED_ROW, fields: LABELLED_FIELDS },
};

/* ------------------------------------------------------------------ */
/*  Harness — the picker test's, unchanged in shape                    */
/* ------------------------------------------------------------------ */

function routeApi() {
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return LISTING;
    const match = /^\/schemas\/(.+)$/.exec(path);
    if (match) return DETAILS[decodeURIComponent(match[1])] ?? {};
    return {};
  });
}

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/new']}>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

/** Real wizard state, so what the picker commits is observable rather than spied. */
function Harness() {
  const [state, setState] = useState<WizardState>({
    ...INITIAL_WIZARD_STATE,
    schemaSource: 'existing',
    schemaSourceExplicitlySet: true,
  });
  return (
    <>
      <WizardStepSchema state={state} onChange={setState} />
      <div data-testid="probe-existing-schema-id">{state.existingSchemaId ?? ''}</div>
    </>
  );
}

async function renderPicker() {
  routeApi();
  render(
    <Providers>
      <Harness />
    </Providers>,
  );
  await screen.findByText('Select an existing schema');
}

/** Cloudscape's Select opens on `mouseDown`, not on `click`. */
function openPicker() {
  fireEvent.mouseDown(screen.getByTestId('existing-schema-select').querySelector('button')!);
}

function optionLabels(): string[] {
  return screen.queryAllByRole('option').map((o) => o.textContent ?? '');
}

beforeEach(() => {
  sessionStorage.clear();
  getMock.mockReset();
});

describe('Schema_Picker — R1.5, the option shows the stored Display_Label', () => {
  it('offers the label for a labelled schema and the id for an unlabelled one', async () => {
    await renderPicker();
    openPicker();

    const labels = optionLabels();
    // The labelled row is offered under its label, and its id does not appear in
    // the option at all — `schemaDisplayName` replaces the name rather than
    // decorating it (`utils/schema-display.ts:21-23`).
    const labelled = labels.find((l) => l.includes(LABELLED_ROW.displayLabel));
    expect(
      labelled,
      `R1.5 — no option shows "${LABELLED_ROW.displayLabel}"; options: ${JSON.stringify(labels)}`,
    ).toBeDefined();
    expect(labelled).not.toContain(LABELLED_ROW.schemaId);
    // R4.8's other branch: no stored label, so the id is what is shown — not a
    // blank option.
    expect(labels.some((l) => l.includes(UNLABELLED_ROW.name))).toBe(true);
  });

  /**
   * The label is a presentation value and the option's `value` is still the
   * address (Requirement 1.12, `LGR-acca05d-2`, **template / ratified**). Asserted
   * on the same interaction because it is what makes "shows the label" safe: a
   * picker that *selected* by label would put a label into `existingSchemaId`,
   * from where it reaches `target_schema` and fails six steps later with
   * `Could not find uploaded schema`.
   */
  it('commits the schema id, not the label, when the labelled option is chosen', async () => {
    await renderPicker();
    openPicker();

    const option = screen
      .queryAllByRole('option')
      .find((o) => (o.textContent ?? '').includes(LABELLED_ROW.displayLabel))!;
    // Cloudscape commits the option on `mouseUp`.
    fireEvent.mouseUp(option);

    await waitFor(() =>
      expect(screen.getByTestId('probe-existing-schema-id').textContent).toBe(
        LABELLED_ROW.schemaId,
      ),
    );
  });
});
