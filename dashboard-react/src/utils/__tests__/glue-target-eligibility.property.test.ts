// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 task 1.3 — the **predicate half** of Property 1.
 *
 * Property 1's full statement (design §6) is: *for any list of Glue database
 * options, the wizard `GlueCatalogForm` and the edit-modal `GlueCatalogForm`
 * each offer exactly the sublist whose names begin with
 * `GLUE_TARGET_DATABASE_PREFIX`, in the source order, and the two offered sets
 * are identical.* That statement has two halves and this file owns the first:
 * **what the rule selects**. The second — that each rendered picker's option
 * list *is* that sublist, and that the two components agree — needs both
 * components and belongs to task 1.7, which asserts it against rendered output.
 * Nothing here renders.
 *
 * `fast-check ^3.15.0`, an existing dependency; `{ numRuns: 100 }` is this
 * repo's convention, carried from
 * `utils/__tests__/unsourced-target-fields.property.test.ts:388` and
 * `components/__tests__/MappingViewer.property.test.tsx:118`.
 *
 * **The oracle is the prefix rule as the Python pre-flight writes it, not
 * `partitionGlueTargetDatabases`' own output** — `LGR-28b4d75-5` (**template /
 * ratified**): "a test written from the producer's output cannot detect the
 * producer drifting, because the output is its own oracle." The generator
 * *decides* which drawn names are eligible and in what order, and the
 * assertions check the partition against that decision. It does not re-run
 * `startsWith` to recover the answer, which would be the same function twice
 * and could not fail honestly.
 *
 * **What this file is built to catch**, stated because both are live hazards
 * that a weaker property would pass:
 *
 * 1. **A dropped source order.** {@link eligibleGroupArb} draws at least two
 *    *distinct* eligible names on the ordering test, so any permutation of the
 *    eligible sublist — a `.sort()`, a `.reverse()`, a `Set` round trip —
 *    changes the array, and the assertion is a list equality rather than a
 *    membership check. A second, independent assertion requires the eligible
 *    elements' positions **in the input** to be strictly increasing, which fails
 *    on a reorder even where a name happens to repeat.
 * 2. **The two counts collapsed into one.** The `totalCount > 0 &&
 *    eligible.length === 0` cell is drawn deliberately and asserted as a pair,
 *    so an implementation that returned only the filtered list, or set
 *    `totalCount = eligible.length`, fails. That cell is exactly the R1.5 arm a
 *    filter destroys.
 *
 * Coverage note: `.kiro/steering/test-mapping.md` is scoped to Python and shell
 * files and carries no frontend rows, so it names no gate here. The gate is
 * `make test-frontend` — both legs.
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  GLUE_TARGET_DATABASE_PREFIX,
  isEligibleGlueTargetDatabase,
  partitionGlueTargetDatabases,
} from '../glue-target-eligibility';

// ---------------------------------------------------------------------------
// Names — one arbitrary per side of the rule, so the generator owns the verdict
// ---------------------------------------------------------------------------

/** Suffix characters a real Glue database name uses. */
const suffixArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'.split('')),
  { minLength: 1, maxLength: 14 },
);

/**
 * A name the rule accepts. `''` is a legal suffix boundary case in its own
 * right — the bare prefix — and is drawn separately below rather than folded in
 * here, so `eligibleNameArb` always produces distinguishable suffixes.
 */
const eligibleNameArb = suffixArb.map((s) => `${GLUE_TARGET_DATABASE_PREFIX}${s}`);

/**
 * A name the rule refuses, drawn across every near miss the real account and
 * the Python rule between them make reachable:
 *
 * - the pre-rename `agentglue_` prefix — 84 of the 93 measured databases;
 * - the prefix minus its trailing underscore, and the prefix as an infix;
 * - a **leading space** and an **upper-cased** spelling, both ineligible because
 *   `resources.py:420` neither trims nor folds case;
 * - `''`, which `fetchGlueDatabases` emits for a row carrying no name at all
 *   (`use-resources.ts:104`);
 * - arbitrary text, filtered so an accidental match cannot mislabel a draw.
 */
const ineligibleNameArb = fc
  .oneof(
    suffixArb.map((s) => `agentglue_${s}`),
    suffixArb.map((s) => `legacy_${GLUE_TARGET_DATABASE_PREFIX}${s}`),
    fc.constant('agentic_data_ingestion'),
    suffixArb.map((s) => ` ${GLUE_TARGET_DATABASE_PREFIX}${s}`),
    suffixArb.map((s) => `${GLUE_TARGET_DATABASE_PREFIX.toUpperCase()}${s}`),
    fc.constant(''),
    fc.string({ maxLength: 24 }),
  )
  .filter((name) => !name.startsWith(GLUE_TARGET_DATABASE_PREFIX));

interface DbOption {
  database_name: string;
  /** An extra key, so the partition is shown to carry the caller's shape
   *  through rather than rebuilding elements. */
  marker: number;
}

/**
 * A drawn input list plus the verdict the generator assigned to each element.
 *
 * `eligibleNames` is the answer **in source order**, built while walking the
 * list in order — it is never recovered by filtering afterwards.
 */
interface Draw {
  input: DbOption[];
  eligibleNames: string[];
  eligibleIndices: number[];
}

const drawArb: fc.Arbitrary<Draw> = fc
  .array(
    fc.record({
      isEligible: fc.boolean(),
      eligibleName: eligibleNameArb,
      ineligibleName: ineligibleNameArb,
    }),
    { minLength: 0, maxLength: 12 },
  )
  .map((specs) => {
    const input: DbOption[] = [];
    const eligibleNames: string[] = [];
    const eligibleIndices: number[] = [];
    specs.forEach((spec, i) => {
      const database_name = spec.isEligible ? spec.eligibleName : spec.ineligibleName;
      input.push({ database_name, marker: i });
      if (spec.isEligible) {
        eligibleNames.push(database_name);
        eligibleIndices.push(i);
      }
    });
    return { input, eligibleNames, eligibleIndices };
  });

// ---------------------------------------------------------------------------
// Feature: p66-glue-catalog-target-selection, Property 1 — predicate half
// ---------------------------------------------------------------------------

describe('Feature: p66-glue-catalog-target-selection, Property 1: the eligible sublist is exactly the prefixed names, in source order (predicate half)', () => {
  /**
   * **Validates: Requirements 1.1, 1.4, 4.1**
   *
   * For any list of Glue database options, `partitionGlueTargetDatabases`
   * selects exactly the elements whose `database_name` begins with
   * `GLUE_TARGET_DATABASE_PREFIX`, in the input's own order, and reports the
   * input's length as `totalCount`.
   *
   * R1.4 and R4.1 are validated *here* rather than in a component test because
   * this is the single definition both `GlueCatalogForm`s consume; task 1.7
   * closes the loop by showing each rendered picker offers this list.
   */
  it('selects exactly the prefixed elements, in source order, and counts the whole input', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames, eligibleIndices }) => {
        const { eligible, totalCount } = partitionGlueTargetDatabases(input);

        // Exactly the eligible sublist, in source order. A list equality, not a
        // set comparison: a reordering fails here rather than passing a weaker
        // membership check.
        expect(eligible.map((d) => d.database_name)).toEqual(eligibleNames);

        // The same claim stated over positions, which also holds when two
        // eligible names coincide: the chosen elements appear in strictly
        // increasing input order.
        expect(eligible.map((d) => d.marker)).toEqual(eligibleIndices);
        for (let i = 1; i < eligible.length; i += 1) {
          expect(eligible[i].marker).toBeGreaterThan(eligible[i - 1].marker);
        }

        // The whole input, not the selected part. This is the number R1.5's two
        // sentences are chosen by.
        expect(totalCount).toBe(input.length);

        // Nothing ineligible slipped through, and no element was invented: the
        // result is a subsequence of the input by identity.
        for (const chosen of eligible) {
          expect(input).toContain(chosen);
        }
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 1.1**
   *
   * The predicate and the partition are one rule, not two: an element is in
   * `eligible` **iff** `isEligibleGlueTargetDatabase` accepts its name. Stated
   * separately because the two exports are consumed separately — a component may
   * ask the predicate about a stored value while the partition drives the option
   * list — and they must not be able to disagree.
   */
  it('agrees with the predicate on every element, in both directions', () => {
    fc.assert(
      fc.property(drawArb, ({ input }) => {
        const { eligible } = partitionGlueTargetDatabases(input);
        const chosen = new Set(eligible);
        for (const option of input) {
          expect(chosen.has(option)).toBe(isEligibleGlueTargetDatabase(option.database_name));
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// The two hazards, drawn deliberately rather than left to chance
// ---------------------------------------------------------------------------

/** At least two **distinct** eligible names, so any permutation is detectable. */
const eligibleGroupArb = fc
  .uniqueArray(eligibleNameArb, { minLength: 2, maxLength: 6 })
  .chain((names) =>
    fc.array(ineligibleNameArb, { minLength: 0, maxLength: 4 }).map((noise) => ({ names, noise })),
  );

describe('Feature: p66-glue-catalog-target-selection, Property 1: source order survives interleaved ineligible names', () => {
  /**
   * **Validates: Requirements 1.1**
   *
   * The ordering claim, drawn so it cannot pass by accident. The general
   * property above may draw zero or one eligible name, where every permutation
   * is the identity; this one always draws at least two distinct ones and
   * interleaves ineligible noise between them, so a `.sort()`, a `.reverse()` or
   * a `Set` round trip anywhere on the path fails.
   */
  it('keeps at least two distinct eligible names in their drawn order', () => {
    fc.assert(
      fc.property(
        eligibleGroupArb.chain(({ names, noise }) =>
          // Where the noise lands relative to the eligible names is drawn, so
          // the eligible run is not merely a contiguous prefix of the input.
          fc
            .array(fc.nat({ max: names.length }), {
              minLength: noise.length,
              maxLength: noise.length,
            })
            .map((positions) => {
              const input: DbOption[] = [];
              let marker = 0;
              const push = (database_name: string) => {
                input.push({ database_name, marker: marker++ });
              };
              names.forEach((name, i) => {
                noise.forEach((n, j) => {
                  if (positions[j] === i) push(n);
                });
                push(name);
              });
              noise.forEach((n, j) => {
                if (positions[j] === names.length) push(n);
              });
              return { input, names };
            }),
        ),
        ({ input, names }) => {
          const { eligible } = partitionGlueTargetDatabases(input);
          expect(eligible.map((d) => d.database_name)).toEqual(names);
        },
      ),
      { numRuns: 100 },
    );
  });
});

describe('Feature: p66-glue-catalog-target-selection, Property 1: the two counts stay distinguishable (R1.5’s arm a filter destroys)', () => {
  /**
   * **Validates: Requirements 1.5**
   *
   * The cell R1.5 turns on: a non-empty input in which nothing is eligible must
   * report `totalCount > 0` alongside `eligible.length === 0`, so the Database
   * picker can say "databases exist, none is eligible" rather than "no Glue
   * databases at all".
   *
   * This is the assertion an implementation returning only a filtered array —
   * or setting `totalCount = eligible.length` — cannot satisfy. It is drawn
   * rather than exampled because "nothing eligible" has many shapes: the
   * pre-rename prefix, a near-miss spelling, a leading space, an empty name.
   */
  it('reports a positive totalCount with an empty eligible list when nothing matches', () => {
    fc.assert(
      fc.property(
        fc.array(ineligibleNameArb, { minLength: 1, maxLength: 10 }),
        (names) => {
          const input = names.map((database_name, marker) => ({ database_name, marker }));
          const { eligible, totalCount } = partitionGlueTargetDatabases(input);

          expect(eligible).toEqual([]);
          expect(totalCount).toBe(names.length);
          expect(totalCount).toBeGreaterThan(0);
          // Stated as its own assertion because it is the requirement, not a
          // consequence of the two above: the two numbers are not the same
          // number, which is what makes the two sentences selectable.
          expect(totalCount).not.toBe(eligible.length);
        },
      ),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 1.5**
   *
   * The other arm, for completeness: an empty input reports both as zero, which
   * is the `GLUE_TARGET_NO_DATABASES` cell.
   */
  it('reports zero for both on an empty input', () => {
    const { eligible, totalCount } = partitionGlueTargetDatabases([]);
    expect(eligible).toEqual([]);
    expect(totalCount).toBe(0);
  });
});
