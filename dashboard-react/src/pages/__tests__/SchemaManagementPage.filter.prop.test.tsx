// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 4.2 — **Property 6: a schema is findable by the name
 * the table shows.**
 *
 * **Validates: Requirements 1.7**
 *
 * > WHEN a user types text that a schema's Display_Label contains into the
 * > Schemas_Page filter, THE Schemas_Page SHALL include that schema in the
 * > filtered list.
 *
 * Feature: p36-schema-naming, Property 6: A schema is findable by the name the
 * table shows — for any set of listing rows and any substring drawn from a row's
 * Display_Label or its id, in any casing, the Schemas_Page filter includes that
 * row; and a needle contained in neither excludes it.
 *
 * ## Why this renders the page instead of calling the predicate
 *
 * The filter is **inline in the component body** (`SchemaManagementPage.tsx:326-334`,
 * `const filtered = schemas.filter(...)`) and the module exports only the default
 * component — there is no exported predicate to call. Extracting one would be a
 * production change made to suit a test, which task 4.2 forbids, and re-typing the
 * expression here would make the test its own oracle. So the row set arrives
 * through `useSchemas` and the needle arrives through the rendered `TextFilter`,
 * which is the path a user takes.
 *
 * The rows are still **pure in-memory data** — no fetch, no timers, no
 * `waitFor` — which is what keeps 120 iterations affordable.
 *
 * ## What this file mocks, and the row-5 hazard it avoids
 *
 * `useSchemas` only, over `vi.importActual('../../hooks')`, so every other hook
 * the page calls at its top level — `useUploadSchema`, `useDeleteSchema`,
 * `useSetDisplayLabel`, `useUpdateFieldContext` — stays real. A factory that
 * *replaced* the hooks module would make each omitted export `undefined`, and the
 * page would throw a `TypeError` **during render, inside the test that renders
 * it** (held-assertion inventory row 5, as p36 item 3 restates it) rather than
 * failing an assertion.
 *
 * ## Row presence is asserted on the rename control, not on the name cell
 *
 * `data-testid="rename-schema-{schema_id}"` is unique per row and stable for any
 * label — including a whitespace-only one, where a text query on the name cell
 * would collapse to the empty string and match nothing. Every generated row is
 * `source_type: 'uploaded'`, which is the only source that carries a
 * Display_Label at all and the gate that control sits behind (`isUploadedSchema`).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Schema } from '../../types';

const useSchemasMock = vi.hoisted(() => vi.fn());

vi.mock('../../hooks', async () => {
  const actual = await vi.importActual<typeof import('../../hooks')>('../../hooks');
  return { ...actual, useSchemas: () => useSchemasMock() };
});

import SchemaManagementPage from '../SchemaManagementPage';

/* ------------------------------------------------------------------ */
/*  Generators                                                         */
/* ------------------------------------------------------------------ */

/**
 * The label alphabet, constrained on purpose.
 *
 * Requirement 1.7 is a case-insensitive substring rule, and the filter spells it
 * `String.prototype.toLowerCase()` on both sides. A handful of characters make
 * *JavaScript's own* case folding non-positional — 'ß' uppercases to two
 * characters, 'ı' round-trips to 'i', and 'Σ' lowercases to 'ς' at the end of a
 * word but 'σ' elsewhere — so a substring drawn from a label containing one is
 * not guaranteed to survive `toLowerCase()` as a substring. Generating those
 * would produce counterexamples about ECMA-262's case-mapping tables rather than
 * about this filter, so the alphabet is length-preserving under both mappings and
 * still spans mixed ASCII case, digits, separators, accented Latin, Greek, CJK
 * and Hangul.
 */
const LABEL_CHARS = [
  ...'abcdefghijklmnopqrstuvwxyz',
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  ...'0123456789',
  ...' _-.()',
  'é', 'É', 'ü', 'Ü', 'ñ', 'Ñ', 'ç', 'Ç', 'ø', 'Ø', 'å', 'Å',
  'ω', 'Ω', '日', '本', '語', '데', '이', '터', '€', '—',
];

const labelArb: fc.Arbitrary<string> = fc
  .array(fc.constantFrom(...LABEL_CHARS), { minLength: 1, maxLength: 16 })
  .map((cs) => cs.join(''));

/** One generated row: an opaque uploaded id, and a Display_Label or none. */
const rowSeedArb = fc.record({
  // The shape `upload_schema` actually emits: `uploaded_schema_{epoch_ms}`, all
  // digits after the prefix — which is what makes "a label substring that does
  // not appear in the id" the ordinary case rather than a special one.
  suffix: fc.integer({ min: 1_700_000_000_000, max: 1_899_999_999_999 }),
  label: fc.option(labelArb, { nil: undefined }),
});

/** How each character of the drawn needle is re-cased: keep / upper / lower. */
const caseBitsArb = fc.array(fc.integer({ min: 0, max: 2 }), { minLength: 20, maxLength: 20 });

const worldArb = fc.record({
  seeds: fc.uniqueArray(rowSeedArb, { minLength: 1, maxLength: 4, selector: (s) => s.suffix }),
  /** Which row the needle is drawn from, and asserted about. */
  targetIndex: fc.nat(),
  /** Draw from the Display_Label where there is one, else from the id. */
  fromLabel: fc.boolean(),
  start: fc.nat(),
  length: fc.integer({ min: 1, max: 20 }),
  caseBits: caseBitsArb,
});

interface Row extends Schema {
  schema_id: string;
}

function buildRows(seeds: ReadonlyArray<{ suffix: number; label?: string }>): Row[] {
  return seeds.map(({ suffix, label }) => {
    const id = `uploaded_schema_${suffix}`;
    return {
      schema_id: id,
      // The listing emits the id as both `schemaId` and `name` for an uploaded
      // schema, so "its id" and `schema_name` are the same string here.
      schema_name: id,
      source_type: 'uploaded',
      display_label: label,
      fields: [],
      field_count: null,
      created_at: '2026-08-04T11:47:29+00:00',
    };
  });
}

/**
 * Re-case a drawn substring, one character at a time.
 *
 * The `length !== 1` guard is a belt-and-braces check on the alphabet above: if a
 * character ever creeps in whose mapping changes length, it is left alone rather
 * than silently producing a needle that is not a substring of the label in any
 * casing.
 */
function recase(text: string, bits: readonly number[]): string {
  return [...text]
    .map((ch, i) => {
      const bit = bits[i % bits.length];
      const mapped = bit === 1 ? ch.toUpperCase() : bit === 2 ? ch.toLowerCase() : ch;
      return mapped.length === 1 ? mapped : ch;
    })
    .join('');
}

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

/** Render the page over `rows`, type `needle`, and report whether `id` survived. */
function rowSurvivesFilter(rows: Row[], needle: string, id: string): boolean {
  useSchemasMock.mockReturnValue({
    data: rows,
    isLoading: false,
    isError: false,
    sourcesFailed: [],
  });
  render(<SchemaManagementPage />, { wrapper });
  fireEvent.change(screen.getByPlaceholderText('Search schemas'), {
    target: { value: needle },
  });
  return screen.queryByTestId(`rename-schema-${id}`) !== null;
}

beforeEach(() => {
  useSchemasMock.mockReset();
});

afterEach(() => {
  cleanup();
});

/* ================================================================== */
/*  Property 6                                                         */
/* ================================================================== */

// Feature: p36-schema-naming, Property 6: A schema is findable by the name the
// table shows
describe('Property 6 — a schema is findable by the name the table shows (R1.7)', () => {
  it('includes a row for any substring of its Display_Label or its id, in any casing', () => {
    fc.assert(
      fc.property(worldArb, ({ seeds, targetIndex, fromLabel, start, length, caseBits }) => {
        cleanup();
        const rows = buildRows(seeds);
        const target = rows[targetIndex % rows.length];

        // The label where the property asks for one and there is one, else the
        // id. Both are "the name the table shows" cases: `schemaDisplayName`
        // renders the label when stored and the id otherwise.
        const source =
          fromLabel && target.display_label ? target.display_label : target.schema_name;
        const from = start % source.length;
        const drawn = source.slice(from, from + Math.min(length, source.length - from));
        // A drawn slice is non-empty by construction (`from < source.length`),
        // which matters: the filter treats `''` as "no filter", so an empty
        // needle would assert nothing.
        const needle = recase(drawn, caseBits);

        expect(rowSurvivesFilter(rows, needle, target.schema_id)).toBe(true);
      }),
      {
        numRuns: 120,
        examples: [
          // A label whose substring appears **nowhere** in the id — the case where
          // the label is doing all the work. Before task 2.3 the listing carried
          // no label at all, so `display_label || schema_name` collapsed to the id
          // for every row and this needle matched nothing.
          [
            {
              seeds: [{ suffix: 1_785_844_048_926, label: 'Q3 orders' }],
              targetIndex: 0,
              fromLabel: true,
              start: 0,
              length: 2,
              caseBits: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            },
          ],
          // Mixed casing on both sides: a mixed-case label, and every character of
          // the needle upper-cased.
          [
            {
              seeds: [{ suffix: 1_785_844_048_927, label: 'Customer MASTER' }],
              targetIndex: 0,
              fromLabel: true,
              start: 9,
              length: 6,
              caseBits: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
            },
          ],
          // ...and the same label with every character lower-cased.
          [
            {
              seeds: [{ suffix: 1_785_844_048_928, label: 'Customer MASTER' }],
              targetIndex: 0,
              fromLabel: true,
              start: 9,
              length: 6,
              caseBits: [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2],
            },
          ],
          // A unicode label, drawn from mid-string.
          [
            {
              seeds: [{ suffix: 1_785_844_048_929, label: '売上 données 데이터' }],
              targetIndex: 0,
              fromLabel: true,
              start: 3,
              length: 7,
              caseBits: [1, 0, 2, 0, 1, 0, 2, 0, 1, 0, 2, 0, 1, 0, 2, 0, 1, 0, 2, 0],
            },
          ],
          // A labelled row beside an unlabelled one: the id half of the rule still
          // holds for the row the table is showing an id for.
          [
            {
              seeds: [
                { suffix: 1_785_844_048_930, label: 'Q3 orders' },
                { suffix: 1_785_844_048_931, label: undefined },
              ],
              targetIndex: 1,
              fromLabel: false,
              start: 0,
              length: 16,
              caseBits: [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0],
            },
          ],
        ],
      },
    );
  }, 180_000);

  it('excludes a row for a needle contained in neither its Display_Label nor its id', () => {
    fc.assert(
      fc.property(
        fc.record({
          seeds: fc.uniqueArray(rowSeedArb, {
            minLength: 1,
            maxLength: 4,
            selector: (s) => s.suffix,
          }),
          targetIndex: fc.nat(),
          needle: fc
            .array(fc.constantFrom(...LABEL_CHARS), { minLength: 3, maxLength: 10 })
            .map((cs) => cs.join('')),
        }),
        ({ seeds, targetIndex, needle }) => {
          cleanup();
          const rows = buildRows(seeds);
          const target = rows[targetIndex % rows.length];

          // "Contained in neither" is the precondition, not an assumption about
          // the generator: the needle is drawn from the same alphabet as the
          // labels, so a collision is possible and is discarded rather than
          // asserted about. The filter also ignores a blank needle, so a
          // whitespace-only draw is discarded too.
          const lowered = needle.toLowerCase();
          const shown = (target.display_label || target.schema_name).toLowerCase();
          fc.pre(needle.trim().length > 0);
          fc.pre(!shown.includes(lowered));
          fc.pre(!target.schema_name.toLowerCase().includes(lowered));

          expect(rowSurvivesFilter(rows, needle, target.schema_id)).toBe(false);
        },
      ),
      {
        numRuns: 120,
        examples: [
          // The concrete "needle in neither field" case: a label and an id that
          // share nothing with the text typed.
          [
            {
              seeds: [{ suffix: 1_785_844_048_926, label: 'Q3 orders' }],
              targetIndex: 0,
              needle: 'Ωμέγα',
            },
          ],
        ],
      },
    );
  }, 180_000);
});
