// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization test — `toMappingData` / `toStringList` over the four
 * reachable `Unmapped_Entry` shapes (p45 task 2.1).
 *
 * **Validates: Requirements 1.4, 3.3, 3.6, 4.3, 4.6, 4.7**
 *
 * ## Why this file exists before anything changes
 *
 * `toMappingData` (`ApprovalArtifact.tsx:86-133`) and `toStringList` (`:72-79`)
 * are named by **no** test in the tree. CodeGraph reports "no covering tests
 * found" for both, and reading confirms it: `ApprovalArtifact.test.tsx:105`
 * feeds `unmapped_fields: { source: ['extra'], target: ['unused'] }` through the
 * `schema_mapping` branch and asserts only that a mapped field's *name* appears,
 * so the flattening these two functions perform is exercised but never observed.
 * R4.3 changes what they return. Until the current behaviour is written down,
 * "we changed the flattening" and "we broke the flattening" look identical.
 *
 * **It was green before task 15 and it is green after it.** Task 15 amended the
 * marked assertions to the post-change expectations; the unmarked ones survived
 * untouched, which is the whole point of writing them as facts rather than as
 * shapes (see "How to read the assertions" below). Each reversal is recorded
 * **at its assertion** rather than only in the plan — `LGR-60c4985-6`
 * (**template / ratified**), because a deliberate hold encoded as a test
 * assertion makes changing one a code change with a reason attached, and the
 * next reader meets the reason where they meet the change.
 *
 * ## Expectations come from the Python producers, never from current output
 *
 * `LGR-28b4d75-5` (**template / ratified**): *"a test written from the
 * producer's output cannot detect the producer drifting, because the output is
 * its own oracle."* `toStringList` is the thing being changed, so its current
 * return cannot be its own oracle either. Every fixture below is transcribed
 * from the code that writes the shape:
 *
 *  - `_normalise_mapping_structure` (`schema_mapping/lambda_function.py:1122-1128`)
 *    writes `{field, default_value, reason}` with `reason` =
 *    `UNSOURCED_MAPPING_REASON`.
 *  - `generate_simple_mapping` (`:884-890`) writes `{field, default_value,
 *    reason}` with `reason` = "No matching source field found", and its
 *    `default_value` comes from `get_default_value` (`:961-975`), which returns
 *    `""` for a `string`-typed property (`:966`).
 *  - `_unmapped_target_field_name` (`:977-995`) tolerates a **bare string**,
 *    because a Bedrock response is model output parsed as returned. That
 *    tolerance is pinned Python-side at
 *    `test_schema_mapping_approval.py:523-536`.
 *  - `create_approval_payload` (`:1391-1397`) assembles `unmapped_fields` and
 *    (`:1435-1439`) returns `{approval_type, error, generated_by}` — no
 *    `unmapped_fields` key at all — when its blanket `except` fires.
 *  - `unsourced_target_field_names` (`:1144-1200`) builds `target_unsourced`
 *    from **both** `unmapped_target_fields` **and** source-less `mappings`
 *    entries, which is why the roster can be a superset of the detail list.
 *
 * ## How to read the assertions
 *
 * Facts the producer establishes are read through the three shape-agnostic
 * helpers below — `entryName`, `hasDefaultValueKey`, `entryReason`. Those
 * assertions are about **what survives the trip to the component** and hold
 * before and after task 15. Assertions about the *container* shape — that
 * `unmapped_target` was a list of bare strings and is now a list of
 * `UnsourcedTargetField` objects — carry an explicit `AMENDED AT TASK 15`
 * marker, and the task-15 diff touched only marked lines plus this docstring.
 *
 * ## What is deliberately NOT pinned
 *
 * The degraded payload of shape 4 is **not** pinned as intended producer
 * behaviour (do-NOT 10). `LGR-b2f79ef-13` (**template / ratified**, the live
 * twin of the dead `LGR-78ce0b7-13` that the source comments still cite) is the
 * standing reason: a blanket `except Exception` that re-labels an
 * already-classified exception is a clause to be **narrowed**, not behaviour to
 * be enshrined. Fixing it is `backlog.md` item 2. What shape 4 pins is only the
 * reader's side — the frontend does not throw on a payload with no
 * `unmapped_fields`, and falls to R1.4's branch.
 *
 * ## Harness
 *
 * `MappingViewer` is doubled so the `MappingData` object `toMappingData`
 * returns can be read directly. A rendered-DOM assertion cannot tell
 * `default_value: null` from no `default_value` key, and that distinction is
 * exactly what R3.3 and R3.6 are about. The double follows
 * `auth/__tests__/AuthProvider.test.tsx:44-56`'s `vi.hoisted` prop-capture
 * pattern. `../../../hooks` is mocked inert by the same convention as
 * `ApprovalArtifact.test.tsx:27-48`: the branch under test calls neither hook,
 * but the module graph imports them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval, MappingData } from '../../../types';

// ---------------------------------------------------------------------------
// The two machine-written reason constants, copied from Python.
//
// Source of truth: `agentic_data_ingestion/lambda/agentic_data_ingestion_schema_mapping/lambda_function.py`
//   UNSOURCED_MAPPING_REASON      — `:102`, written by `_normalise_mapping_structure:1126`
//   NAME_SIMILARITY_NO_MATCH      — `:888`, written by `generate_simple_mapping`
//
// They are **different facts**, and the comment at `:96-101` says so in as many
// words: the first records that the generated mapping claimed a target field
// without naming a source; the second records that name-similarity matching
// looked and found no candidate. R4.7 plus C14 make that three attributions
// with a model-authored reason, not two.
// ---------------------------------------------------------------------------
const UNSOURCED_MAPPING_REASON = 'No source field in generated mapping';
const NAME_SIMILARITY_NO_MATCH = 'No matching source field found';

/**
 * The reason the mapper recorded on run `run-2026-08-20-17-52-21-248afa7a`,
 * verbatim (requirements.md:24-27), with the blockquote's line wrapping joined
 * back into the one paragraph the mapper wrote. R4.1 requires it to reach the
 * approver unwrapped and untruncated. Design §6.4 calls it "the observed run's
 * 300-character reason"; joined faithfully it measures **271**, and the exact
 * figure is asserted below so a later edit cannot quietly shorten the fixture
 * this file's verbatim guard depends on.
 */
const OBSERVED_MODEL_REASON =
  "No source field semantically corresponds to 'mobile_phone'. The source 'phone' " +
  'field represents a general phone number and should not be duplicated here, as ' +
  'mobile and landline are distinct contact types. This field is not in the ' +
  "target's required list, so NULL is valid.";

// ---------------------------------------------------------------------------
// Capture the MappingData handed to the shared MappingViewer.
// ---------------------------------------------------------------------------

const mocks = vi.hoisted(() => ({
  captured: null as MappingData | null,
}));

vi.mock('../../MappingViewer', () => ({
  default: (props: { mappingData: MappingData }) => {
    mocks.captured = props.mappingData;
    return <div data-testid="mapping-viewer-double" />;
  },
}));

vi.mock('../../../hooks', () => ({
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

// ---------------------------------------------------------------------------
// Shape-agnostic fact readers. These three questions are the ones the producer
// answers, so asking them this way keeps the assertions valid across task 15.
// ---------------------------------------------------------------------------

/** The field name an entry carries, in either the string or the object shape. */
function entryName(entry: unknown): string | undefined {
  if (typeof entry === 'string') return entry;
  if (typeof entry === 'object' && entry !== null) {
    const field = (entry as Record<string, unknown>).field;
    return typeof field === 'string' ? field : undefined;
  }
  return undefined;
}

/**
 * Whether the entry carries a `default_value` **key**, tested with `in` and not
 * with `?? null` — `LGR-44d601e-1`'s rule. A present `null` is a recorded null
 * default (R3.3); an absent key is no default recorded (R3.6). `??` collapses
 * the two, and this test exists partly to keep them apart.
 */
function hasDefaultValueKey(entry: unknown): boolean {
  return typeof entry === 'object' && entry !== null && 'default_value' in entry;
}

/** The `reason` the entry carries, or `undefined` when it carries none. */
function entryReason(entry: unknown): unknown {
  if (typeof entry === 'object' && entry !== null) {
    return (entry as Record<string, unknown>).reason;
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/**
 * A `schema_mapping` approval whose `details` is the given payload. `details`
 * is `Record<string, unknown>` on the wire and is narrowed at the point of use
 * inside `toMappingData` (C8, `LGR-69ca78b-4`), so the fixture is written as
 * the payload actually arrives rather than through a narrowed type.
 */
function mappingApproval(details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-p45',
    pipeline_key: 'latesttesting-19ca826a',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-20T17:52:21Z',
    details,
  };
}

/**
 * The `phone → phone` mapping the observed run produced, carrying both keys —
 * the shape that reaches the payload without raising (requirements C9).
 */
const OBSERVED_MAPPINGS = [
  { source_field: 'phone', target_field: 'phone', confidence: 0.97, transformation: 'direct' },
];

/** Render the mapping branch and return the `MappingData` it built. */
function renderAndCapture(details: Record<string, unknown>): MappingData {
  render(<ApprovalArtifact approval={mappingApproval(details)} />);
  // The branch was taken — otherwise the capture below would be a stale read.
  expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
  expect(mocks.captured).not.toBeNull();
  return mocks.captured as MappingData;
}

beforeEach(() => {
  cleanup();
  mocks.captured = null;
});

describe('Unmapped_Entry shape 1 — a bare string (Bedrock output parsed as returned)', () => {
  /**
   * Producer facts (`_unmapped_target_field_name:977-995`, pinned at
   * `test_schema_mapping_approval.py:523-536`): the entry carries a name and
   * nothing else. There is no `default_value` key and no `reason`, so R4.6 says
   * render the name and make no reason claim, and R3.6 says state the column
   * will be empty without naming a value.
   */
  it('carries the name, and neither a default_value key nor a reason', () => {
    const data = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_fields: { source: [], target: ['mobile_phone'] },
    });

    expect(data.unmapped_target).toHaveLength(1);
    const entry = data.unmapped_target[0];

    // Facts — held before task 15 and still hold after it.
    expect(entryName(entry)).toBe('mobile_phone');
    expect(hasDefaultValueKey(entry)).toBe(false); // R3.6: no default recorded
    expect(entryReason(entry)).toBeUndefined(); // R4.6: no reason claim

    // AMENDED AT TASK 15 — **R4.3**. The entry was the bare name, because
    // `toStringList` mapped every element to a string; `toMappingData` now hands
    // the payload to `unsourcedTargetFields` and the normalised object arrives.
    // `requirement` is `not_established` and **not** `optional`: neither
    // Unsourced_Key is present on this payload, so no authority established
    // required-ness (R1.4, R2.6, do-NOT 5).
    expect(entry).toEqual({ field: 'mobile_phone', requirement: 'not_established' });
  });
});

describe("Unmapped_Entry shape 2 — `{field}` only (a model's own unnormalised JSON)", () => {
  /**
   * The Bedrock prompt's declared response shape (`:740-746`) is a dict, but a
   * model may return only `field`. `_unmapped_target_field_name:991-993` reads
   * `entry.get("field", "")` and tolerates the missing siblings, so this shape
   * reaches the payload with the same two absences as shape 1 — and must reach
   * the approver with the same two absences.
   */
  it('is indistinguishable from the bare string in the facts it carries', () => {
    const data = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_fields: { source: [], target: [{ field: 'mobile_phone' }] },
    });

    expect(data.unmapped_target).toHaveLength(1);
    const entry = data.unmapped_target[0];

    expect(entryName(entry)).toBe('mobile_phone');
    expect(hasDefaultValueKey(entry)).toBe(false);
    expect(entryReason(entry)).toBeUndefined();

    // AMENDED AT TASK 15 — **R4.3**. `toStringList:76-77` read `rec.field` and
    // discarded the object; the join now keeps it. This shape and shape 1 still
    // arrive **identical**, which is the point of both tests: the model's
    // unnormalised `{field}` records exactly as much as a bare name does.
    expect(entry).toEqual({ field: 'mobile_phone', requirement: 'not_established' });
  });
});

describe('Unmapped_Entry shape 3 — `{field, default_value: null, reason}` (the observed run)', () => {
  const observedDetails = {
    proposed_mappings: OBSERVED_MAPPINGS,
    unmapped_fields: {
      source: [],
      target: [{ field: 'mobile_phone', default_value: null, reason: OBSERVED_MODEL_REASON }],
    },
  };

  /**
   * The producer recorded two facts about this field that no other shape
   * carries: a **present** `default_value` of `null` (R3.3 — distinguishable
   * from shape 1's absent key, R3.6) and the model's own account of the
   * decision (R4.1 — verbatim, untruncated).
   *
   * AMENDED AT TASK 15 — the whole test. Both facts were discarded at
   * `toStringList`, which is R4.3's subject; both now arrive.
   */
  it('AMENDED AT TASK 15 — both recorded facts now arrive (R3.3, R4.1, R4.3)', () => {
    const data = renderAndCapture(observedDetails);

    expect(data.unmapped_target).toHaveLength(1);
    const entry = data.unmapped_target[0];
    expect(entryName(entry)).toBe('mobile_phone');

    // AMENDED AT TASK 15 — **R3.3**. The key is PRESENT and its value is null,
    // which is a *recorded* null default and not the same fact as shape 1's
    // absent key (R3.6). `hasDefaultValueKey` tests with `in` rather than
    // `?? null` precisely because `??` collapses the two — `LGR-44d601e-1`
    // (**template / ratified**), whose own revisit condition is *"a reader needs
    // to tell 'cleared' apart from 'never set'"*. This is that reader.
    expect(hasDefaultValueKey(entry)).toBe(true);
    expect(entry.default_value).toBeNull();

    // AMENDED AT TASK 15 — **R4.1**. The model's own account, verbatim.
    expect(entryReason(entry)).toBe(OBSERVED_MODEL_REASON);
  });

  /**
   * The consequence of that discard, stated on its own so the reason for task
   * 15 is legible without reading task 15: the shape the mapper filled in and
   * the shape it left bare arrive at the component identical. An approver
   * therefore cannot be shown the difference between "the mapper recorded a
   * null default and wrote down why" and "nothing was recorded at all".
   *
   * AMENDED AT TASK 15 — after §4.1 these two must NOT be equal, and they are
   * not. This is the assertion that makes R4.3 observable rather than asserted:
   * the shape the mapper filled in and the shape it left bare are now
   * distinguishable at the component boundary, so an approver can be shown the
   * difference.
   */
  it('AMENDED AT TASK 15 — no longer collapses onto the bare-string shape (R3.3, R4.3)', () => {
    const detailed = renderAndCapture(observedDetails);
    const detailedEntries = detailed.unmapped_target;

    cleanup();
    mocks.captured = null;

    const bare = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_fields: { source: [], target: ['mobile_phone'] },
    });

    // AMENDED AT TASK 15 — was `toEqual`. The name is still the same on both
    // sides, so the inequality is carried by the recorded facts and by nothing
    // else — which is what stops this from passing for an unrelated reason.
    expect(detailedEntries.map(entryName)).toEqual(bare.unmapped_target.map(entryName));
    expect(detailedEntries).not.toEqual(bare.unmapped_target);
  });

  /**
   * R4.1's verbatim rule, asserted for the one input we have measured. Nothing
   * on this path may `slice`, `substring` or clamp the reason (do-NOT 14).
   *
   * AMENDED AT TASK 15 — the string now arrives, so this guard is load-bearing
   * rather than a check on the fixture's own integrity: a later truncation
   * anywhere between the payload and the component fails here first.
   */
  it('AMENDED AT TASK 15 — the model sentence now reaches the component verbatim (R4.1)', () => {
    expect(OBSERVED_MODEL_REASON).toHaveLength(271);

    const data = renderAndCapture(observedDetails);
    const reason = data.unmapped_target[0].reason;

    // Byte-equal, not merely containing: `toBe` is what catches a rewording,
    // and the length is what catches a truncation that keeps the opening clause.
    expect(reason).toBe(OBSERVED_MODEL_REASON);
    expect(reason).toContain('semantically corresponds');
    expect(reason).toHaveLength(271);
  });
});

describe('Unmapped_Entry shape 4 — an Approval_Payload with no `unmapped_fields`', () => {
  /**
   * Two live producers of this payload, and neither is a hypothetical:
   *
   *  - `create_approval_payload:1435-1439` — the degraded return of its blanket
   *    `except Exception`, which carries `approval_type`, `error` and
   *    `generated_by` and nothing else. Reachable today on the mirror shape
   *    named in `backlog.md` item 2.
   *  - every mapping record written before p37, which had no
   *    `target_unsourced` / `target_required` to write. p37 is not deployed
   *    (C15), so this is currently the only branch any account takes.
   *
   * **The degradation itself is not pinned as intended behaviour** (do-NOT 10,
   * `LGR-b2f79ef-13`). What is pinned is the reader: the frontend must not
   * throw, and must land on R1.4's branch with an empty list rather than
   * inventing entries.
   */
  it('renders without throwing and yields empty lists (R1.4)', () => {
    const data = renderAndCapture({
      error: 'Failed to create approval payload: KeyError: \'phone\'',
      generated_by: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
    });

    expect(data.unmapped_target).toEqual([]);
    expect(data.unmapped_source).toEqual([]);
    // The panel still renders — an approve/reject pair sits below it either way.
    expect(screen.getByTestId('mapping-viewer-double')).toBeInTheDocument();
  });

  /**
   * The same absence via the pre-p37 record: `unmapped_fields` present but
   * carrying only `source` and `target`, with neither Unsourced_Key. R1.4's
   * branch is reached by key absence, not by payload absence, so both routes
   * are covered.
   */
  it('reaches the same branch when only the two Unsourced_Keys are absent', () => {
    const data = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_fields: { source: [], target: [] },
    });

    expect(data.unmapped_target).toEqual([]);
  });
});

describe('The three reason attributions (R4.7, C14)', () => {
  /**
   * Three reasons reach this list and they mean three different things:
   * `UNSOURCED_MAPPING_REASON` is a structural note from the normaliser,
   * `NAME_SIMILARITY_NO_MATCH` is a structural note from the fallback matcher,
   * and the observed sentence is a model's judgement. R4.7 asks that the
   * surface distinguish them; C14 records that there are three, because the
   * middle one is not named in the requirements.
   *
   * `default_value` differs per producer and is transcribed, not invented:
   * `generate_simple_mapping:887` calls `get_default_value`, which returns `""`
   * for a `string`-typed property (`:966`), while
   * `_normalise_mapping_structure:1125` calls `_default_value_or_none`, which
   * returns `None` when the schema is unusable (`:1053-1059`).
   *
   * AMENDED AT TASK 15 — all three arrived as the same bare name, so the panel
   * could attribute none of them. All three are now distinguishable, which is
   * the precondition R4.7 needs: `reasonAttribution` cannot choose a heading for
   * a reason that never arrived.
   */
  it('AMENDED AT TASK 15 — all three now arrive distinguishable (R4.7, C14)', () => {
    const reasons = [
      { field: 'mobile_phone', default_value: null, reason: UNSOURCED_MAPPING_REASON },
      { field: 'mobile_phone', default_value: '', reason: NAME_SIMILARITY_NO_MATCH },
      { field: 'mobile_phone', default_value: null, reason: OBSERVED_MODEL_REASON },
    ];

    const captured = reasons.map((entry) => {
      cleanup();
      mocks.captured = null;
      const data = renderAndCapture({
        proposed_mappings: OBSERVED_MAPPINGS,
        unmapped_fields: { source: [], target: [entry] },
      });
      return data.unmapped_target;
    });

    // AMENDED AT TASK 15 — was three equalities collapsing onto
    // `['mobile_phone']`. Each entry now carries the reason it was written with,
    // verbatim, plus the `default_value` its own producer recorded (R3.3, R4.1).
    // `requirement` is `not_established` on all three because these payloads
    // carry neither Unsourced_Key (R1.4, R2.6) — the attribution and the
    // requirement are independent facts.
    expect(captured[0][0]).toEqual({
      field: 'mobile_phone',
      requirement: 'not_established',
      default_value: null,
      reason: UNSOURCED_MAPPING_REASON,
    });
    expect(captured[1][0]).toEqual({
      field: 'mobile_phone',
      requirement: 'not_established',
      default_value: '',
      reason: NAME_SIMILARITY_NO_MATCH,
    });
    expect(captured[2][0]).toEqual({
      field: 'mobile_phone',
      requirement: 'not_established',
      default_value: null,
      reason: OBSERVED_MODEL_REASON,
    });

    // Pairwise distinct — the three-attribution premise, stated as three
    // inequalities so a future collapse of any single pair fails here.
    expect(captured[1]).not.toEqual(captured[0]);
    expect(captured[2]).not.toEqual(captured[0]);
    expect(captured[2]).not.toEqual(captured[1]);
  });

  /** The two constants are distinct strings — the premise C14 rests on. */
  it('keeps the two machine-written constants distinct', () => {
    expect(UNSOURCED_MAPPING_REASON).not.toBe(NAME_SIMILARITY_NO_MATCH);
  });
});

describe('The roster superset — a `target_unsourced` name `target` does not carry', () => {
  /**
   * `unsourced_target_field_names:1192-1197` adds every `mappings` entry that
   * has a `target_field` and no usable `source_field`, on top of the names it
   * read from `unmapped_target_fields` (`:1187-1190`). So `target_unsourced` is
   * a **superset** of `unmapped_fields.target`'s own names, and design §4.8
   * step 2 says such a name carries neither `default_value` nor `reason`. The
   * docstring at `:1162-1164` records why this is permanent: C9 normalises at
   * generation time and cannot reach a record already written.
   *
   * AMENDED AT TASK 15 — `toMappingData` read only `u.source` and `u.target`, so
   * `nickname` never reached the approver at all and neither Unsourced_Key was
   * consulted. That absence was R1.1's gap in executable form; this test is now
   * the executable form of it being closed.
   */
  it('AMENDED AT TASK 15 — the roster is read and the roster-only name arrives (R1.1, R1.2, R1.5, R1.7)', () => {
    const data = renderAndCapture({
      // `nickname` sits in `mappings` with no usable `source_field` — the
      // pre-C9 shape `unsourced_target_field_names` list 2 exists to read.
      proposed_mappings: [
        ...OBSERVED_MAPPINGS,
        { target_field: 'nickname', source_field: null, confidence: 0.5 },
      ],
      unmapped_fields: {
        source: [],
        target: [{ field: 'mobile_phone', default_value: null, reason: OBSERVED_MODEL_REASON }],
        // Producer order: schema-`required` names first (`:1199-1200`). Neither
        // of these is required on the observed schema, so insertion order holds.
        target_unsourced: ['mobile_phone', 'nickname'],
        target_required: [],
      },
    });

    // AMENDED AT TASK 15 — was `toEqual(['mobile_phone'])` plus a
    // `not.toContain('nickname')`.
    //
    // **R1.1 + R1.5** — the roster and its order come from `target_unsourced`,
    // taken as received. Nothing here re-sorts (do-NOT 13).
    // **R1.7** — the roster-only name renders too. An approver must not be shown
    // a filtered list whose filter is invisible.
    expect(data.unmapped_target.map(entryName)).toEqual(['mobile_phone', 'nickname']);

    // **R1.2 + R1.3** — `target_required` was read: it is present and empty, so
    // both names are `optional` rather than `not_established`. Required-ness
    // comes from that key alone; nothing consults `proposed_mappings`, which is
    // where `nickname` came from (do-NOT 2).
    expect(data.unmapped_target.map((entry) => entry.requirement)).toEqual([
      'optional',
      'optional',
    ]);

    // §4.8 step 2 — the roster-only name has no matching `target` entry, so it
    // carries neither key: R3.6's sentence and no reason claim (R4.6).
    const nickname = data.unmapped_target[1];
    expect(hasDefaultValueKey(nickname)).toBe(false);
    expect(entryReason(nickname)).toBeUndefined();

    // …while the name the detail list does carry keeps both facts (R3.3, R4.1).
    const mobilePhone = data.unmapped_target[0];
    expect(hasDefaultValueKey(mobilePhone)).toBe(true);
    expect(entryReason(mobilePhone)).toBe(OBSERVED_MODEL_REASON);
  });
});

describe('`unmapped_source` is unchanged (do-NOT 19)', () => {
  /**
   * **This block must survive task 15 with no amendment.** `toStringList` stays
   * and keeps serving `unmapped_source` as bare strings, because both producers
   * write names there and nothing else (design Current state item 6):
   * `generate_simple_mapping:893` assigns `unmapped_source = source_fields`, a
   * list of source column names, and `create_approval_payload:1349` reads
   * `unmapped_source_fields` and passes it into the payload at `:1392`
   * untouched. An object shape on this key would be invented by the frontend,
   * which is what do-NOT 19 forbids.
   */
  it('keeps source names as bare strings, in the order received', () => {
    const data = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_fields: { source: ['middle_name', 'fax'], target: ['mobile_phone'] },
    });

    expect(data.unmapped_source).toEqual(['middle_name', 'fax']);
    for (const entry of data.unmapped_source) {
      expect(typeof entry).toBe('string');
    }
  });

  /**
   * `toStringList:73` returns `[]` for a non-array, and `toMappingData:111-113`
   * then retries the flat `unmapped_source_fields` key. Both halves are load
   * bearing for the pre-p37 records that use the flat spelling, so both are
   * pinned before the function is touched.
   */
  it('falls back to the flat `unmapped_source_fields` spelling', () => {
    const data = renderAndCapture({
      proposed_mappings: OBSERVED_MAPPINGS,
      unmapped_source_fields: ['middle_name'],
      unmapped_target_fields: ['mobile_phone'],
    });

    expect(data.unmapped_source).toEqual(['middle_name']);
    expect(data.unmapped_target.map(entryName)).toEqual(['mobile_phone']);
  });
});
