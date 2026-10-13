// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45 task 6.3 — Property 2 over `unsourcedTargetFields`' join.
 *
 * `fast-check ^3.15.0`, an existing dependency; `{ numRuns: 100 }` is this
 * repo's convention, carried from `MappingViewer.property.test.tsx:118`.
 *
 * **Expectations come from the producer, never from `unsourcedTargetFields`'
 * own output** — `LGR-28b4d75-5` (**template / ratified**): *"a test written
 * from the producer's output cannot detect the producer drifting, because the
 * output is its own oracle."* The three relations quantified below were read
 * out of `schema_mapping/lambda_function.py`, each verified against the source
 * rather than taken from the spec:
 *
 * - **The roster and its order** are `unmapped_fields.target_unsourced`
 *   (`:1395`), which is `unsourced_target_field_names(...)` computed at
 *   `:1351`. That function already ranks schema-`required` names first (`:1200`)
 *   and dedupes as it collects (`:1189`), so re-sorting downstream would be a
 *   second ordering authority (R1.5, do-NOT 13).
 * - **The detail** is `unmapped_fields.target` (`:1393`), joined **by name**,
 *   where "name" is `_unmapped_target_field_name` (`:977-995`): a bare string is
 *   trimmed, an object's `field` is trimmed when it is a string, anything else
 *   has no name. Case is **not** folded — the producer does not fold it, so
 *   `Phone` and `phone` are two fields. Both halves of that are quantified
 *   below: {@link buildDetail}'s padded spelling MUST match, its case-flipped
 *   spelling MUST NOT.
 * - **`requirement`** is `target_required` membership (`:1396`) and nothing
 *   else — not `proposed_mappings`, not `target_schema.required` (R1.3,
 *   do-NOT 2). `target_required` is
 *   `[f for f in unsourced_target if f in required_target_fields]` (`:1353`),
 *   **filtered out of the roster list two lines above**, so its members are
 *   byte-equal to roster elements. That is why {@link makeJoinDraw} draws it as
 *   a subset of the **raw** roster elements: a required name that is the
 *   *trimmed* form of a padded roster element is unreachable from the producer,
 *   and pinning an answer for it would pin an arbitrary one.
 *
 * **This deliberately does not reimplement the join and diff the two arrays.**
 * That would be the same function twice and could not fail honestly. Instead
 * the generator *decides* the pairing — which roster name has a detail entry,
 * which detail entry is a case-flipped decoy, which name is detail-only — and
 * the assertions check the relations the producer guarantees against that
 * decision. Nothing here recovers a pairing by re-running the lookup.
 *
 * Coverage note: `.kiro/steering/test-mapping.md`'s `fileMatch` is scoped to
 * Python files under `lambda/`, and it carries no frontend rows at all, so it
 * names no gate for this file. The real adjacent coverage is
 * `utils/__tests__/schema-field-count.test.ts` (the sibling util's unit model),
 * `components/__tests__/MappingViewer.property.test.tsx` (this repo's existing
 * fast-check model), and producer-side
 * `tests/unit/lambdas/schema_mapping/test_schema_mapping_approval.py:570-588`,
 * which pins `target_unsourced` / `target_required` on the Python side — the two
 * ends of the same join.
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { unsourcedTargetFields } from '../unsourced-target-fields';

// ---------------------------------------------------------------------------
// Names — distinct lowercase bases, plus the two spellings that collide with
// them. Padding collides and MUST match; case collides and MUST NOT.
// ---------------------------------------------------------------------------

/** Lowercase letters only, so {@link flipCase} always differs from the base. */
const baseNameArb = fc.stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz'.split('')), {
  minLength: 3,
  maxLength: 12,
});

/** `null` spells the name exactly; a pair pads it. At least one side is
 *  non-empty, so a padded spelling is never silently the exact one. */
const spellingArb = fc.oneof(
  fc.constant(null),
  fc
    .tuple(fc.constantFrom('', ' ', '  ', '\t'), fc.constantFrom('', ' ', '\n', ' \t'))
    .filter(([lead, trail]) => (lead + trail).length > 0),
);

type Spelling = readonly [string, string] | null;

function spell(base: string, spelling: Spelling): string {
  return spelling === null ? base : `${spelling[0]}${base}${spelling[1]}`;
}

/** A collision the producer's own identity relation refuses. Always differs
 *  from the base, because {@link baseNameArb} emits lowercase letters only. */
function flipCase(base: string): string {
  return base.toUpperCase();
}

// ---------------------------------------------------------------------------
// Detail entries — the four wire shapes `unmapped_fields.target` carries
// ---------------------------------------------------------------------------

/** `get_default_value` (`:961-974`) returns `''` for a string field, `0` for a
 *  number, `false` for a boolean, `[]` for an array, `{}` for an object, and
 *  falls through to `defaults.get(field_type)` — i.e. `None` — for a type it
 *  does not know. All six are drawn, plus arbitrary JSON. */
const defaultValueArb = fc.oneof(
  { arbitrary: fc.constant(null), weight: 3 },
  { arbitrary: fc.constant(''), weight: 2 },
  { arbitrary: fc.constant(0), weight: 1 },
  { arbitrary: fc.constant(false), weight: 1 },
  { arbitrary: fc.constant([] as unknown[]), weight: 1 },
  { arbitrary: fc.constant({} as Record<string, unknown>), weight: 1 },
  { arbitrary: fc.jsonValue(), weight: 3 },
);

/**
 * The two machine-written constants plus a model's own sentence.
 *
 * `UNSOURCED_MAPPING_REASON` (`schema_mapping/lambda_function.py:102`) and
 * `generate_simple_mapping`'s reason (`:888`) are **different facts** — the
 * producer's comment at `:96-101` says so. Whether they get different headings
 * is `reasonAttribution`'s business, not this property's; they are drawn here
 * only so the byte-equality below is exercised on them.
 *
 * **The long branch carries a `minLength`, and that is not decoration.** With a
 * single `maxLength: 400` draw, `fc.stringOf` biases small enough that a
 * `.slice(0, 80)` planted in the join passed 100 runs — measured, not supposed.
 * A branch that is *always* longer than any plausible clamp is what makes
 * byte-equality below an actual no-truncation assertion (R4.1, do-NOT 14). It is
 * the cheap half of that criterion; the observed run's real 300-character reason
 * is rendered through a surface at task 13.2.
 */
const reasonCharArb = fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz .,;—\'"'.split(''));

const reasonArb = fc.oneof(
  fc.constant('No source field in generated mapping'),
  fc.constant('No matching source field found'),
  fc.stringOf(reasonCharArb, { minLength: 1, maxLength: 60 }),
  fc.stringOf(reasonCharArb, { minLength: 200, maxLength: 400 }),
);

interface DetailSpec {
  /** A bare-string element rather than an object. It carries no
   *  `default_value` and no `reason` to look up, which is R4.6 and R3.6 rather
   *  than a gap — and `unsourcedTargetFields` indexes only object entries. */
  bare: boolean;
  spelling: Spelling;
  /** Drawn independently of the value, so a **present `null`** (R3.3) and an
   *  **absent key** (R3.6) are separate draws. `'default_value' in entry` is
   *  the only honest test of the difference; `?? null` collapses it
   *  (`LGR-44d601e-1`). */
  hasDefault: boolean;
  defaultValue: unknown;
  reason: string | null;
}

const detailSpecArb: fc.Arbitrary<DetailSpec> = fc.record({
  bare: fc.boolean(),
  spelling: spellingArb,
  hasDefault: fc.boolean(),
  defaultValue: defaultValueArb,
  reason: fc.option(reasonArb, { nil: null }),
});

/** Build one `unmapped_fields.target` element under a given spelling. */
function buildDetail(name: string, spec: DetailSpec): unknown {
  const spelled = spell(name, spec.spelling);
  if (spec.bare) return spelled;
  const entry: Record<string, unknown> = { field: spelled };
  if (spec.hasDefault) entry.default_value = spec.defaultValue;
  if (spec.reason !== null) entry.reason = spec.reason;
  return entry;
}

/** What a matched object detail entry contributes. A bare string contributes
 *  nothing, so the two shapes are distinguished here rather than at each
 *  assertion. */
function contributed(spec: DetailSpec): { hasDefault: boolean; defaultValue: unknown; reason: string | null } {
  if (spec.bare) return { hasDefault: false, defaultValue: undefined, reason: null };
  return { hasDefault: spec.hasDefault, defaultValue: spec.defaultValue, reason: spec.reason };
}

// ---------------------------------------------------------------------------
// The draw — four roles, so the generator owns the pairing
// ---------------------------------------------------------------------------

type Role =
  /** In the roster with no detail entry at all. Reachable, not hypothetical:
   *  `unsourced_target_field_names` also names source-less `mappings` entries
   *  (`:1192-1197`), so the roster is a **superset** of `target` (§4.8 step 2). */
  | 'rosterOnly'
  /** In the roster, and the only nearby detail entry is spelled in flipped
   *  case. It MUST NOT match: the producer does not fold case. */
  | 'rosterCaseDecoy'
  /** In the roster with a matching detail entry, spelled exactly or padded.
   *  Padded MUST match — the producer trims (`:990`, `:994`). */
  | 'both'
  /** In `target` only, never in the roster. MUST NOT appear in the output. */
  | 'detailOnly';

interface NameSpec {
  role: Role;
  rosterSpelling: Spelling;
  detail: DetailSpec;
  isRequired: boolean;
}

const nameSpecArb: fc.Arbitrary<NameSpec> = fc.record({
  role: fc.constantFrom<Role>('rosterOnly', 'rosterCaseDecoy', 'both', 'detailOnly'),
  rosterSpelling: spellingArb,
  detail: detailSpecArb,
  isRequired: fc.boolean(),
});

interface JoinDraw {
  payload: { target: unknown[]; target_unsourced: string[]; target_required: string[] };
  /** One per roster element, in roster order. */
  expected: {
    field: string;
    requirement: 'required' | 'optional';
    hasDefault: boolean;
    defaultValue: unknown;
    reason: string | null;
  }[];
  /** Names that exist in `target` but must never be rendered. */
  forbiddenNames: string[];
}

/**
 * Draw a roster/detail pair whose pairing is decided here, not recovered later.
 *
 * `target` may cover none, some or all of the roster and may carry extra names
 * — that is what the four roles are for. Base names are **distinct**, so no two
 * roster elements trim to the same name and every expectation below is
 * unambiguous. (Duplicate roster elements are the one asymmetry
 * `unsourcedTargetFields` documents but this property does not quantify: the
 * real producer cannot emit one, because `:1189` dedupes as it collects. The
 * *fallback* roster's dedupe is quantified separately, below.)
 */
function makeJoinDraw(): fc.Arbitrary<JoinDraw> {
  return fc
    .uniqueArray(baseNameArb, { minLength: 0, maxLength: 7 })
    .chain((names) =>
      fc
        .array(nameSpecArb, { minLength: names.length, maxLength: names.length })
        .map((specs) => ({ names, specs })),
    )
    .chain(({ names, specs }) =>
      // The detail list's order is irrelevant to this join — the roster owns
      // the order — so it is shuffled to say so rather than left in name order.
      fc.shuffledSubarray(
        names.map((_, i) => i),
        { minLength: names.length, maxLength: names.length },
      ).map((detailOrder) => {
        const roster: string[] = [];
        const required: string[] = [];
        const expected: JoinDraw['expected'] = [];
        const forbiddenNames: string[] = [];

        names.forEach((name, i) => {
          const spec = specs[i];
          if (spec.role === 'detailOnly') {
            forbiddenNames.push(name);
            return;
          }
          const rosterElement = spell(name, spec.rosterSpelling);
          roster.push(rosterElement);
          if (spec.isRequired) required.push(rosterElement);

          if (spec.role === 'rosterCaseDecoy') forbiddenNames.push(flipCase(name));

          const from = spec.role === 'both' ? contributed(spec.detail) : { hasDefault: false, defaultValue: undefined, reason: null };
          expected.push({
            field: name,
            requirement: spec.isRequired ? 'required' : 'optional',
            ...from,
          });
        });

        const target: unknown[] = [];
        for (const i of detailOrder) {
          const name = names[i];
          const spec = specs[i];
          if (spec.role === 'both' || spec.role === 'detailOnly') {
            target.push(buildDetail(name, spec.detail));
          } else if (spec.role === 'rosterCaseDecoy') {
            target.push(buildDetail(flipCase(name), spec.detail));
          }
        }

        return {
          payload: { target, target_unsourced: roster, target_required: required },
          expected,
          forbiddenNames,
        };
      }),
    );
}

// ---------------------------------------------------------------------------
// Feature: p45-unmapped-target-field-visibility, Property 2: the roster is the
// roster, and the order is the producer's
// ---------------------------------------------------------------------------

describe('Feature: p45-unmapped-target-field-visibility, Property 2: one entry per target_unsourced name, in target_unsourced order', () => {
  /**
   * **Validates: Requirements 1.5, 1.7**
   *
   * For any roster/detail pair, `unsourcedTargetFields` yields exactly one
   * entry per `target_unsourced` name, in `target_unsourced`'s order:
   *
   * - one entry per roster element, in roster order (R1.5, R1.7 — every name
   *   renders, optional ones included, so an approver is never shown a
   *   filtered list whose filter is invisible);
   * - `requirement` is `required` exactly for the `target_required` members and
   *   `optional` otherwise (R1.3);
   * - `'default_value' in entry` exactly when the matching detail entry had the
   *   key (R3.3 vs R3.6);
   * - `reason` byte-equal to the detail entry's, or absent (R4.1, R4.6);
   * - no detail-only name appears.
   */
  it('yields one entry per roster element in roster order, with the detail joined by exact name', () => {
    fc.assert(
      fc.property(makeJoinDraw(), ({ payload, expected, forbiddenNames }) => {
        const result = unsourcedTargetFields(payload);

        // One entry per roster element, in roster order. Asserted as a list
        // equality on the names rather than a count plus a membership test, so
        // a reordering fails here rather than passing a weaker check.
        expect(result.map((e) => e.field)).toEqual(expected.map((e) => e.field));

        expected.forEach((want, i) => {
          const got = result[i];

          // R1.3 — from `target_required` membership only. Nothing else is
          // consulted, and there is nothing else in the payload to consult.
          expect(got.requirement).toBe(want.requirement);

          // R3.3 vs R3.6 — the key's PRESENCE, which is the only thing that
          // tells a recorded null default from no default recorded.
          expect('default_value' in got).toBe(want.hasDefault);
          if (want.hasDefault) {
            expect(got.default_value).toStrictEqual(want.defaultValue);
          }

          // R4.1 / R4.6 — verbatim, or absent. Byte-equality is the assertion:
          // a clamp, a slice or a reword anywhere on this path fails it.
          if (want.reason === null) {
            expect('reason' in got).toBe(false);
          } else {
            expect(got.reason).toBe(want.reason);
          }
        });

        // No detail-only name appears — neither an extra name `target` carries
        // on its own, nor a case-flipped spelling of a roster name.
        const rendered = new Set(result.map((e) => e.field));
        for (const forbidden of forbiddenNames) {
          expect(rendered.has(forbidden)).toBe(false);
        }
      }),
      { numRuns: 100 },
    );
  });

  /**
   * Degenerate draw 1 — an **empty roster**.
   *
   * `[]` is a present key, so it is the roster; it does not fall through to
   * `target`. A populated `target` beside it therefore renders nothing, which
   * is the producer's own claim: a mapping with no unsourced target field names
   * none, whatever else `target` happens to hold.
   */
  it('renders nothing for an empty roster, however populated the detail list is', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(baseNameArb, { minLength: 0, maxLength: 5 }).chain((names) =>
          fc
            .array(detailSpecArb, { minLength: names.length, maxLength: names.length })
            .map((specs) => ({
              target: names.map((name, i) => buildDetail(name, specs[i])),
              // Drawn deliberately non-empty-capable: a `target_required` naming
              // a field the roster does not carry is ignored, never added.
              target_required: names,
            })),
        ),
        ({ target, target_required }) => {
          expect(unsourcedTargetFields({ target, target_unsourced: [], target_required })).toEqual([]);
        },
      ),
      { numRuns: 100 },
    );
  });

  /**
   * Degenerate draw 2 — **both Unsourced_Keys absent**.
   *
   * Per C15 this is the **common** case today, not an edge case: p37 is not
   * deployed, and `target_unsourced` / `target_required` are written by
   * `create_approval_payload:1395-1396`, which ships in that deploy. Two
   * further causes are permanent — mapping records written before p37, and C9's
   * degraded payload.
   *
   * The roster becomes `target`'s own names in `target`'s order, deduped on
   * first occurrence (the fallback roster is *constructed*, so rendering one
   * field twice would be a visible defect), and every entry is
   * `not_established` — **never `optional`** (R2.6, do-NOT 5). With no
   * `target_required` there is no authority for a requirement, and "optional"
   * would be a claim the payload does not support.
   *
   * Every occurrence of a repeated name is byte-identical here, so first-wins
   * is unambiguous and the joined keys are assertable on this path too.
   */
  it("falls back to target's own names in target's order, deduped, every entry not_established", () => {
    fc.assert(
      fc.property(
        fc
          .uniqueArray(baseNameArb, { minLength: 1, maxLength: 5 })
          .chain((names) =>
            fc
              .tuple(
                fc.array(detailSpecArb, { minLength: names.length, maxLength: names.length }),
                fc.array(fc.nat({ max: names.length - 1 }), { minLength: 1, maxLength: 10 }),
              )
              .map(([specs, sequence]) => ({ names, specs, sequence })),
          )
          .map(({ names, specs, sequence }) => {
            // One shape per NAME, so every repeat of a name is byte-identical
            // and "first object wins" cannot depend on which occurrence won.
            const target = sequence.map((i) => buildDetail(names[i], specs[i]));
            const expectedNames: string[] = [];
            const expectedFrom: ReturnType<typeof contributed>[] = [];
            for (const i of sequence) {
              if (expectedNames.includes(names[i])) continue;
              expectedNames.push(names[i]);
              expectedFrom.push(contributed(specs[i]));
            }
            return { target, expectedNames, expectedFrom };
          }),
        ({ target, expectedNames, expectedFrom }) => {
          // Neither Unsourced_Key present. Not `undefined`-valued keys —
          // absent, which is the shape every pre-p37 payload actually has.
          const result = unsourcedTargetFields({ target });

          expect(result.map((e) => e.field)).toEqual(expectedNames);

          for (const entry of result) {
            expect(entry.requirement).toBe('not_established');
            // Stated as its own assertion because it is the requirement, not a
            // consequence of the one above: nothing on this branch says
            // "optional" (R2.6, do-NOT 5).
            expect(entry.requirement).not.toBe('optional');
          }

          expectedFrom.forEach((want, i) => {
            expect('default_value' in result[i]).toBe(want.hasDefault);
            if (want.hasDefault) expect(result[i].default_value).toStrictEqual(want.defaultValue);
            if (want.reason === null) {
              expect('reason' in result[i]).toBe(false);
            } else {
              expect(result[i].reason).toBe(want.reason);
            }
          });
        },
      ),
      { numRuns: 100 },
    );
  });
});
