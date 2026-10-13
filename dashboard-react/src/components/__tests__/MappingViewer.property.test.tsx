// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for MappingViewer
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 7: Mapping viewer completeness
 * - Property 8: Mapping hints display
 *
 * Feature: p15-rerun-mapping
 * - Property 6: Text area pre-population
 * - Property 7: Character count accuracy
 *
 * Feature: p45-unmapped-target-field-visibility
 * - Property 1 **is** react-dashboard-parity Property 7, amended for
 *   `unmapped_target`'s new element type (p45 R4.2). It is not a new property and
 *   the existing tag is deliberately kept — see the note at the assertion.
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import MappingViewer from '../MappingViewer';
import type { FieldMapping, MappingData } from '../../types';
// p45 R4.2 — imported from the module that OWNS the type, not from `types.ts`,
// which imports it rather than re-declaring it.
import type { UnsourcedTargetField } from '../../utils/unsourced-target-fields';

// ---------------------------------------------------------------------------
// QueryClient wrapper for tests (MappingViewer uses useBedrockModels hook)
// ---------------------------------------------------------------------------

function createQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function renderWithQueryClient(ui: React.ReactElement) {
  const queryClient = createQueryClient();
  return render(
    <QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const fieldNameArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz_'.split('')),
  { minLength: 2, maxLength: 20 },
);

const fieldMappingArb: fc.Arbitrary<FieldMapping> = fc.record({
  source_field: fieldNameArb,
  target_field: fieldNameArb,
  confidence: fc.option(fc.double({ min: 0, max: 1, noNaN: true }), { nil: undefined }),
  transformation: fc.option(fc.constant('cast'), { nil: undefined }),
  hint: fc.option(
    fc.stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz '.split('')), { minLength: 1, maxLength: 30 }),
    { nil: undefined },
  ),
});

/** Prose over a small alphabet, with the length range stated by the caller. */
const proseArb = (minLength: number, maxLength: number) =>
  fc.stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz ,.'.split('')), { minLength, maxLength });

/**
 * A recorded `default_value` — three-state at the reader, so the arbitrary has to
 * reach all three.
 *
 * The six literals are `get_default_value`'s own returns
 * (`schema_mapping/lambda_function.py:961-974`): `''` for a string field, `0` for
 * a number, `false` for a boolean, `[]` for an array, `{}` for an object, and
 * `null` when the declared type is none of the six. A recorded `''` is the
 * COMMON case, not an edge one, which is why it is generated explicitly rather
 * than left to `fc.string()` to stumble on.
 */
const defaultValueArb: fc.Arbitrary<unknown> = fc.oneof(
  fc.constant(null),
  fc.constant(''),
  fc.constant(0),
  fc.constant(false),
  fc.constant([] as unknown[]),
  fc.constant({} as Record<string, unknown>),
  proseArb(1, 20),
  fc.integer(),
);

/**
 * A recorded `reason`, over all three of `reasonAttribution`'s branches: the two
 * machine-written constants byte-for-byte (`:102` and `:888`) and the mapping
 * model's own prose.
 *
 * **The long branch is deliberate and load-bearing.** `fc.stringOf` with only a
 * `maxLength` biases short, and task 6.3 measured what that costs: a planted
 * `reason.slice(0, 80)` in the implementation survived 100 runs until a
 * `{ minLength: 200 }` branch was added. Without it a verbatim-rendering
 * assertion is weaker than it looks, so the branch stays even though this
 * property asserts on the name rather than the reason.
 */
const reasonArb: fc.Arbitrary<string> = fc.oneof(
  fc.constant('No source field in generated mapping'),
  fc.constant('No matching source field found'),
  proseArb(1, 60),
  proseArb(200, 400),
);

/**
 * One `UnsourcedTargetField` — p45 R4.2's object shape.
 *
 * `requiredKeys` names only `field` and `requirement`, so `default_value` and
 * `reason` are **genuinely absent** on some draws rather than present-and-
 * `undefined`. That distinction is the whole point: the renderer decides the
 * consequence sentence with `'default_value' in entry` (`LGR-44d601e-1`,
 * **template / ratified**), so an arbitrary that always wrote the key would
 * never reach R3.6's "the mapper recorded no default value" arm, nor R4.6's
 * entry that makes no reason claim.
 */
const unsourcedTargetFieldArb: fc.Arbitrary<UnsourcedTargetField> = fc.record(
  {
    field: fieldNameArb,
    requirement: fc.constantFrom('required' as const, 'optional' as const, 'not_established' as const),
    default_value: defaultValueArb,
    reason: reasonArb,
  },
  { requiredKeys: ['field', 'requirement'] },
);

function makeMappingDataArb(): fc.Arbitrary<MappingData> {
  return fc.record({
    mappings: fc.array(fieldMappingArb, { minLength: 0, maxLength: 8 }),
    // Bare names, and deliberately still bare — do-NOT 19. Both producers write
    // names for `unmapped_source`, so an object shape here would be invented.
    unmapped_source: fc.array(fieldNameArb, { minLength: 0, maxLength: 5 }),
    // p45 R4.2 — reshaped from `fc.array(fieldNameArb)`. See the note at the
    // assertion for why this is an amendment rather than a relaxation.
    unmapped_target: fc.array(unsourcedTargetFieldArb, { minLength: 0, maxLength: 5 }),
    schema_hash: fc.hexaString({ minLength: 8, maxLength: 16 }),
    version_index: fc.integer({ min: 0, max: 10 }),
    ai_model_used: fc.option(
      fc.constantFrom('anthropic.claude-3-5-sonnet-20241022-v2:0', 'amazon.nova-pro-v1:0'),
      { nil: undefined },
    ),
    created_at: fc.option(
      fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
      { nil: undefined },
    ),
  });
}

// ---------------------------------------------------------------------------
// Property 7: Mapping viewer completeness
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 7: Mapping viewer completeness', () => {
  /**
   * **Validates: Requirements 5.1, 5.3** (react-dashboard-parity's numbering)
   *
   * **Validates: Requirements 4.2, 7.6** (p45's numbering — this same assertion
   * is p45's Property 1)
   *
   * For any MappingData, the viewer SHALL render all mappings,
   * unmapped_source, and unmapped_target entries.
   *
   * ## Amended by p45, not relaxed — R4.2
   *
   * `unmapped_target`'s element type changed from `string` to
   * `UnsourcedTargetField` (R4.2), so `makeMappingDataArb` generates records and
   * the per-member check reads `entry.field`. **The element-count assertion is
   * unchanged** — one rendered element per entry is still the property
   * (do-NOT 20), and it was the only existing pin on the surface p45 exists to
   * change.
   *
   * This is written down here rather than only in the plan because
   * `LGR-60c4985-6` and `LGR-5906ec6-2` (both **template / ratified**) make it a
   * house rule: this repo encodes a deliberate hold as a test assertion, so
   * changing one is a code change that owes a reason. `LGR-60c4985-6` amended
   * three such assertions rather than deleting them, on the ground that *"the
   * amendment must stay legible as a reversal, not read as a regression"* — and
   * recorded the reversal in the test's own docstring. Same shape here.
   *
   * The `Feature: react-dashboard-parity, Property 7` tag on the enclosing
   * `describe` is **kept on purpose.** This is that property, amended; renaming
   * it to p45 would lose the provenance of what it originally pinned.
   *
   * R7.6 rides along: the viewer is rendered with `mappingData` alone, so this
   * property fails if the new element shape ever requires a new prop of the two
   * production callers.
   */
  it('renders all mappings, unmapped source, and unmapped target entries', () => {
    fc.assert(
      fc.property(makeMappingDataArb(), (mappingData) => {
        cleanup();
        const { container } = renderWithQueryClient(
          <MappingViewer mappingData={mappingData} />,
        );

        const viewer = within(container).getByTestId('mapping-viewer');
        const text = viewer.textContent ?? '';

        // All mapped source fields should appear
        for (const m of mappingData.mappings) {
          expect(text).toContain(m.source_field);
          expect(text).toContain(m.target_field);
        }

        // All unmapped source fields should appear
        const unmappedSourceEls = within(viewer).queryAllByTestId('unmapped-source');
        expect(unmappedSourceEls.length).toBe(mappingData.unmapped_source.length);
        for (const field of mappingData.unmapped_source) {
          expect(text).toContain(field);
        }

        // All unmapped target fields should appear.
        //
        // p45 R4.2 — AMENDED, NOT RELAXED. The count below is byte-for-byte what
        // it was when the elements were bare strings (do-NOT 20); only the
        // per-member read moved from `field` to `entry.field`, because the
        // element now carries a requirement, a default and a reason alongside the
        // name. Do NOT weaken the count to a subset or a `>=`, and do NOT drop
        // the name check on the ground that the entry has several fields —
        // either removes the only pin this surface has.
        const unmappedTargetEls = within(viewer).queryAllByTestId('unmapped-target');
        expect(unmappedTargetEls.length).toBe(mappingData.unmapped_target.length);
        for (const entry of mappingData.unmapped_target) {
          expect(text).toContain(entry.field);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 8: Mapping hints display
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 8: Mapping hints display', () => {
  /**
   * **Validates: Requirements 5.5**
   *
   * For any FieldMapping with a non-empty hint, the viewer SHALL include
   * a hint indicator in the rendered output for that mapping.
   */
  it('displays hint indicator for every mapping with a non-empty hint', () => {
    // Generate mapping data where at least one mapping has a hint
    const mappingWithHintArb = fc.record({
      source_field: fieldNameArb,
      target_field: fieldNameArb,
      confidence: fc.option(fc.double({ min: 0, max: 1, noNaN: true }), { nil: undefined }),
      transformation: fc.option(fc.constant('cast'), { nil: undefined }),
      hint: fc.stringOf(
        fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz '.split('')),
        { minLength: 1, maxLength: 30 },
      ),
    });

    const mappingWithoutHintArb = fc.record({
      source_field: fieldNameArb,
      target_field: fieldNameArb,
      confidence: fc.option(fc.double({ min: 0, max: 1, noNaN: true }), { nil: undefined }),
      transformation: fc.option(fc.constant('cast'), { nil: undefined }),
      hint: fc.constant(undefined as string | undefined),
    });

    const mixedMappingsArb = fc.tuple(
      fc.array(mappingWithHintArb, { minLength: 1, maxLength: 5 }),
      fc.array(mappingWithoutHintArb, { minLength: 0, maxLength: 3 }),
    ).map(([withHints, withoutHints]) => ({
      mappings: [...withHints, ...withoutHints],
      unmapped_source: [] as string[],
      // p45 R4.2 — the `as string[]` assertion here is what deferred the type
      // error to the render site below rather than reporting it at this line.
      // This property is about hint indicators, so the list stays empty; only
      // the assertion it carries is corrected.
      unmapped_target: [] as UnsourcedTargetField[],
      schema_hash: 'abc123',
      version_index: 0,
    }));

    fc.assert(
      fc.property(mixedMappingsArb, (mappingData) => {
        cleanup();
        const { container } = renderWithQueryClient(
          <MappingViewer mappingData={mappingData} />,
        );

        const viewer = within(container).getByTestId('mapping-viewer');
        const hintIndicators = within(viewer).queryAllByTestId('hint-indicator');

        const expectedHintCount = mappingData.mappings.filter(
          (m) => m.hint && m.hint.length > 0,
        ).length;

        expect(hintIndicators.length).toBe(expectedHintCount);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Arbitraries for p15-rerun-mapping properties
// ---------------------------------------------------------------------------

/** Printable ASCII strings safe for textarea (no null bytes or control chars) */
const printableStringArb = fc.stringOf(
  fc.constantFrom(
    ...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?-_=+@#$%&*()'.split(''),
  ),
  { minLength: 0, maxLength: 200 },
);

const baseMappingData: MappingData = {
  mappings: [{ source_field: 'fname', target_field: 'first_name', confidence: 0.95 }],
  unmapped_source: [],
  /**
   * p45 R4.2 — amended for its **content**, not because anything complained.
   *
   * `[]` is assignable to `string[]` and to `UnsourcedTargetField[]` alike, so no
   * gate at any point in this spec would ever have flagged this line: it was
   * silent before the change and permanently silent after it. Task 7.2's
   * measurement is the reason it is amended anyway — the signal is per-SITE, not
   * per-file, and fixing only what `tsc` names leaves a fixture in the same file
   * still shaped for the string world.
   *
   * One real entry rather than an empty list, so the two p15 properties below
   * render the unsourced-target block instead of skipping it on the
   * `length > 0` guard. It carries all four keys deliberately: an object shape is
   * what R4.2 is about.
   */
  unmapped_target: [
    {
      field: 'last_name',
      requirement: 'required',
      default_value: null,
      reason: 'No source field in generated mapping',
    },
  ],
  schema_hash: 'abc123',
  version_index: 0,
};

// ---------------------------------------------------------------------------
// Feature: p15-rerun-mapping, Property 6: Text area pre-population
// ---------------------------------------------------------------------------

describe('Feature: p15-rerun-mapping, Property 6: Text area pre-population', () => {
  /**
   * **Validates: Requirements 4.1**
   *
   * For any initialContext string passed to MappingViewer, the text area's
   * initial value SHALL equal that string.
   */
  it('text area value equals initialContext for any string', () => {
    fc.assert(
      fc.property(printableStringArb, (contextStr) => {
        cleanup();
        renderWithQueryClient(
          <MappingViewer
            mappingData={baseMappingData}
            initialContext={contextStr}
            onRerunMapping={vi.fn()}
          />,
        );

        const textarea = screen.getByPlaceholderText(/additional context/i);
        expect(textarea).toHaveValue(contextStr);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Feature: p15-rerun-mapping, Property 7: Character count accuracy
// ---------------------------------------------------------------------------

describe('Feature: p15-rerun-mapping, Property 7: Character count accuracy', () => {
  /**
   * **Validates: Requirements 4.4**
   *
   * For any string value typed into the MappingViewer text area, the
   * displayed character count SHALL equal `{string.length}/2000`.
   */
  it('displayed character count matches typed string length', () => {
    fc.assert(
      fc.property(printableStringArb, (typedStr) => {
        cleanup();
        renderWithQueryClient(
          <MappingViewer
            mappingData={baseMappingData}
            initialContext=""
            onRerunMapping={vi.fn()}
          />,
        );

        const textarea = screen.getByPlaceholderText(/additional context/i);
        fireEvent.change(textarea, { target: { value: typedStr } });

        expect(screen.getByText(`${typedStr.length}/2000`)).toBeInTheDocument();

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
