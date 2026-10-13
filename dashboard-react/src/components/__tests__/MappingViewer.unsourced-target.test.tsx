// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45 task 13.2 — `MappingViewer` renders the three facts about a target field
 * no source column supplies.
 *
 * **What was on screen before this.** One `Badge` per entry carrying the field
 * name and nothing else. `default_value` and `reason` were computed by the
 * producer, carried on the wire, and thrown away by three separate flattening
 * sites on the way to this component; required-ness was never read at all. An
 * approver saw a list of names and had to open the schema to learn which of them
 * the target demanded.
 *
 * **Where the expectations below come from.** Design §4.6 writes every string
 * out in full, and this file transcribes them from there as literals — it does
 * **not** call `requirementLabel`, `consequenceStatement` or `reasonAttribution`
 * to build an expectation. `LGR-28b4d75-5` (**template / ratified**): *"a test
 * written from the producer's output cannot detect the producer drifting, because
 * the output is its own oracle."* Calling the label functions here would assert
 * that the component renders whatever those functions return, which is true of a
 * reworded string too. The three consequence cases and the three attributions are
 * design §4.6's tables; the wire shapes they are reached from are the producers'
 * (`_normalise_mapping_structure:1122-1128`, `generate_simple_mapping:884-890`).
 *
 * **Fixtures are built from the wire, through the real boundary reader.** The
 * entries handed to `MappingViewer` come from `unsourcedTargetFields` wherever the
 * fact under test is *about the wire shape* — the bare-string case especially,
 * because "a bare string carries no reason" (R4.6) is a fact about the producer's
 * two shapes, and hand-writing the normalised object would assert it about a
 * literal I wrote rather than about a shape the producer emits. Where the fact
 * under test is about the *render* of a given entry, the entry is a literal, so
 * the case is unambiguous on the page.
 *
 * **What no test here can establish.** Whether any of this copy *reads* well.
 * `backlog.md` item 15 records that this environment has *"no driver, no browser
 * binary, no config, no Python binding"*, and that *"nothing observes layout,
 * focus order, or whether the `not_established` blank reads as absence rather than
 * a broken cell"*. jsdom pins that the strings are in the DOM. Design §4.6 exists
 * so a human reviewer can judge the wording; if it reads as hedging, the fix is
 * copy, not structure.
 *
 * **Validates: Requirements 1.7, 2.1, 2.2, 2.3, 2.4, 2.6, 3.1, 3.2, 3.3, 3.4,
 * 3.5, 3.6, 4.1, 4.6, 4.7, 4.8**
 */

import { describe, it, expect, vi } from 'vitest';
import ts from 'typescript';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import MappingViewer from '../MappingViewer';
import { unsourcedTargetFields } from '../../utils/unsourced-target-fields';
import type { UnsourcedTargetField } from '../../utils/unsourced-target-fields';
import type { MappingData } from '../../types';

vi.mock('../../hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../hooks')>();
  return {
    ...actual,
    useBedrockModels: () => ({ data: [], isLoading: false, error: null }),
  };
});

// ---------------------------------------------------------------------------
// Design §4.6's strings, transcribed rather than computed
// ---------------------------------------------------------------------------

const LABEL_REQUIRED = 'Required by the target schema';
const LABEL_OPTIONAL = 'Optional in the target schema';
const LABEL_NOT_ESTABLISHED =
  "The target schema's requirement for this field was not established";

/** R2.4 — the rule, on `required` only. */
const RULE_REQUIRED =
  'A required target field that no source column supplies sends the mapping to a human for approval.';

/** R1.4 / R2.6 — rendered once above the list, never per entry. */
const FALLBACK_STATEMENT =
  "The target schema's requirement for these fields was not established, so none of them is shown as required or optional.";

const CONSEQUENCE_NULL_DEFAULT =
  'No source column supplies this field. The target column is written as null on every row — that is the default the mapper recorded.';
const CONSEQUENCE_NO_DEFAULT =
  'No source column supplies this field. The target column is written empty on every row; the mapper recorded no default value.';
const consequenceWithValue = (rendered: string) =>
  `No source column supplies this field. The target column is written with the default the mapper recorded, ${rendered}, on every row.`;

const ATTRIBUTION_NORMALISER =
  'Reason (structural — the generated mapping named no source column)';
const ATTRIBUTION_NAME_SIMILARITY =
  'Reason (structural — name-similarity matching found no candidate)';
const ATTRIBUTION_MODEL = "Reason (the mapping model's own account)";

/** The two machine-written reason constants (C14, R4.7). */
const UNSOURCED_MAPPING_REASON = 'No source field in generated mapping';
const NO_MATCHING_SOURCE_FIELD_REASON = 'No matching source field found';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function mappingData(unmapped_target: UnsourcedTargetField[]): MappingData {
  return {
    mappings: [{ source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 }],
    unmapped_source: [],
    unmapped_target,
    schema_hash: 'abc123',
    version_index: 0,
  };
}

function renderViewer(unmapped_target: UnsourcedTargetField[]) {
  const Wrapper = createWrapper();
  return render(
    <Wrapper>
      <MappingViewer mappingData={mappingData(unmapped_target)} />
    </Wrapper>,
  );
}

/** The text of every rendered unmapped-target entry, joined. */
function entriesText(): string {
  return screen
    .getAllByTestId('unmapped-target')
    .map((el) => el.textContent ?? '')
    .join(' ');
}

// ---------------------------------------------------------------------------
// R3.1-R3.6 — the three consequence cases
// ---------------------------------------------------------------------------

describe('MappingViewer — the Consequence_Statement (p45 R3)', () => {
  /**
   * `default_value` is three-state and the third state is **absence**, not
   * `null` — `LGR-44d601e-1` (**template / ratified**) clears a value by
   * *removing* the key precisely because *"a stored `""` would have to be
   * distinguished from absence at every reader"*. These three cases are that
   * distinction on screen: a recorded value, a recorded `null`, and no record.
   */
  it('names the recorded default when one was recorded and it is not null', () => {
    renderViewer([{ field: 'region', requirement: 'required', default_value: 'N/A' }]);

    // `JSON.stringify` quotes the value, so a recorded empty string is legible
    // as a value rather than as a gap — `get_default_value:961-974` returns `''`
    // for a string field, which makes that the common case, not a corner.
    expect(screen.getByText(consequenceWithValue('"N/A"'))).toBeInTheDocument();
  });

  it('says "as null" in words when the recorded default is null (R3.3)', () => {
    renderViewer([{ field: 'mobile_phone', requirement: 'required', default_value: null }]);

    // The whole of R3.3: "the default is null" and "no default was recorded"
    // have to be distinguishable on screen, not just in the payload. A rendered
    // empty string would collapse them.
    expect(screen.getByText(CONSEQUENCE_NULL_DEFAULT)).toBeInTheDocument();
    expect(screen.queryByText(CONSEQUENCE_NO_DEFAULT)).not.toBeInTheDocument();
  });

  it('names no value when no default was recorded (R3.6)', () => {
    // Reached from the wire, not hand-written: a bare-string Unmapped_Entry is
    // one of the producer's two shapes and carries no `default_value` key at
    // all. `unsourcedTargetFields` is the one reader of that shape (do-NOT 2).
    const entries = unsourcedTargetFields({ target: ['legacy_code'] });
    renderViewer(entries);

    expect(screen.getByText(CONSEQUENCE_NO_DEFAULT)).toBeInTheDocument();
    expect(screen.queryByText(CONSEQUENCE_NULL_DEFAULT)).not.toBeInTheDocument();
  });

  it('never claims the column will be absent, and never mentions a run (R3.4, R3.5)', () => {
    renderViewer([
      { field: 'region', requirement: 'required', default_value: 'N/A' },
      { field: 'mobile_phone', requirement: 'optional', default_value: null },
      { field: 'legacy_code', requirement: 'optional' },
    ]);

    const consequences = screen
      .getAllByTestId('unmapped-target-consequence')
      .map((el) => el.textContent ?? '');
    expect(consequences).toHaveLength(3);

    for (const sentence of consequences) {
      // R3.4 — the generated code writes the column, as `lit(None).cast(…)` on
      // the observed run, so it exists and is empty. Nothing may say otherwise.
      expect(sentence).toContain('is written');
      expect(sentence).not.toMatch(/missing|absent|will not (be )?(exist|appear)|dropped/i);
      // R3.5 — a sentence that never names a run cannot claim a row count, a
      // completeness percentage or a data-quality outcome. Those are measured
      // after the load and a pre-load panel restating them would be a second
      // authority for them. "on every row" is a statement about the mapping;
      // "rows loaded" would be a statement about an execution.
      expect(sentence).not.toMatch(/\d+\s*%|\brows? (were|loaded|written|affected)\b|quality/i);
    }
  });
});

// ---------------------------------------------------------------------------
// R4.7, C14 — the three reason attributions
// ---------------------------------------------------------------------------

describe('MappingViewer — the reason and its attribution (p45 R4)', () => {
  /**
   * Three attributions, not two. The two machine-written constants mean
   * **different** things and the producer says so at
   * `schema_mapping/lambda_function.py:96-101`: `:102`'s constant records that
   * the generated mapping claimed the field without naming a source, `:888`'s
   * records that name-similarity matching looked and found nothing. R4.7 names
   * one of them; C14 is why there are three headings.
   */
  it('attributes the normaliser constant to the generated mapping', () => {
    renderViewer([
      { field: 'mobile_phone', requirement: 'required', default_value: null, reason: UNSOURCED_MAPPING_REASON },
    ]);

    expect(screen.getByText(ATTRIBUTION_NORMALISER)).toBeInTheDocument();
    expect(screen.queryByText(ATTRIBUTION_NAME_SIMILARITY)).not.toBeInTheDocument();
    expect(screen.queryByText(ATTRIBUTION_MODEL)).not.toBeInTheDocument();
  });

  it('attributes the simple-mapping constant to name-similarity matching', () => {
    renderViewer([
      { field: 'region', requirement: 'optional', default_value: 'N/A', reason: NO_MATCHING_SOURCE_FIELD_REASON },
    ]);

    expect(screen.getByText(ATTRIBUTION_NAME_SIMILARITY)).toBeInTheDocument();
    expect(screen.queryByText(ATTRIBUTION_NORMALISER)).not.toBeInTheDocument();
    expect(screen.queryByText(ATTRIBUTION_MODEL)).not.toBeInTheDocument();
  });

  it("attributes anything else to the model's own account", () => {
    const modelReason =
      'The source has no column resembling a loyalty tier; inventing one from purchase history would be a guess.';
    renderViewer([{ field: 'loyalty_tier', requirement: 'required', reason: modelReason }]);

    expect(screen.getByText(ATTRIBUTION_MODEL)).toBeInTheDocument();
    // The body is the model's words, unedited. The attribution lives in the
    // heading exactly so the sentence never has to carry it.
    expect(screen.getByText(modelReason)).toBeInTheDocument();
  });

  it('renders the reason under a heading of its own, distinct from description and context (R4.8)', () => {
    renderViewer([{ field: 'mobile_phone', requirement: 'required', reason: UNSOURCED_MAPPING_REASON }]);

    // C5 / `LGR-acca05d-3` (**template / ratified**) accepted a *second*
    // authority for a field's prose — the schema's own `description` and the
    // Context_Sidecar's `context` — *"which Requirement 1.6 keeps legible by
    // putting them under distinct headings"*. The mapper's `reason` is the
    // third. The heading is what keeps it separable, so it is asserted to exist
    // as its own element rather than as a substring of one blob.
    const heading = screen.getByTestId('unmapped-target-reason-heading');
    expect(heading.textContent).toBe(ATTRIBUTION_NORMALISER);
    expect(heading.textContent).not.toMatch(/description|context/i);

    const body = screen.getByTestId('unmapped-target-reason');
    expect(body.textContent).toBe(UNSOURCED_MAPPING_REASON);
    // The heading is not folded into the body, and the body is not folded into
    // the heading. Two elements, two authorities' worth of separation.
    expect(body).not.toBe(heading);
  });

  it('makes no reason claim for a bare-string entry (R4.6)', () => {
    // Both wire shapes are reachable and the producer tolerates both
    // (`_unmapped_target_field_name:977-995`). A bare string carries a name and
    // nothing else, so there is no reason to attribute — and inventing a
    // heading for an absent sentence would be a claim about provenance.
    renderViewer(unsourcedTargetFields({ target: ['legacy_code'] }));

    expect(screen.getByText('legacy_code')).toBeInTheDocument();
    expect(screen.queryByTestId('unmapped-target-reason')).not.toBeInTheDocument();
    expect(screen.queryByTestId('unmapped-target-reason-heading')).not.toBeInTheDocument();
    expect(screen.queryByText(/^Reason \(/)).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// R4.1, do-NOT 14 — verbatim, untruncated, and no clamp on the path
// ---------------------------------------------------------------------------

describe('MappingViewer — the reason is rendered verbatim (p45 R4.1)', () => {
  it('renders a long reason whole, with no ellipsis and no shortening', () => {
    // Longer than the 300-character reason on the observed run, so a clamp set
    // for "a sentence or two" would bite here.
    const longReason = `${'The target schema demands a mobile number and the source carries only a landline column, '.repeat(
      6,
    )}so no mapping was proposed.`;
    expect(longReason.length).toBeGreaterThan(500);

    renderViewer([{ field: 'mobile_phone', requirement: 'required', default_value: null, reason: longReason }]);

    const body = screen.getByTestId('unmapped-target-reason');
    // `toBe`, not `toContain`: a truncation would still contain a prefix.
    expect(body.textContent).toBe(longReason);
    expect(body.textContent).not.toContain('…');
    expect(body.textContent).not.toContain('...');
  });

  /**
   * do-NOT 14's **only mechanical check**, and it parses rather than greps.
   *
   * A substring sweep cannot tell source from prose, which is the ground
   * `LGR-b46a5bb-4` rejected substring matching on for a repo-wide invariant —
   * and it would fail immediately here, because
   * `utils/unsourced-target-fields.ts:60-61` names `slice` and `substring` in its
   * own doc comment in order to forbid them. So the two files on the reason's
   * path are parsed and walked for a real truncating **call**, leaving the prose
   * free to keep naming what it forbids.
   *
   * Task 6.1 confirmed zero such calls in `unsourced-target-fields.ts`; this
   * asserts the renderer added none, and keeps asserting it.
   *
   * Unfalsifiable in general — a future clamp could arrive in a stylesheet this
   * walk never reads — so the style properties that express one in JSX are
   * checked as identifiers and strings too. Design §6.4 records R4.1 as one of
   * the four criteria with no complete machine check.
   */
  it('has no truncating call or line clamp on the render path', () => {
    // `node:path` off `fileURLToPath(import.meta.url)`, **not**
    // `new URL(relative, import.meta.url)`. Under the jsdom environment the
    // global `URL` is jsdom's, and it resolves a relative specifier against the
    // document origin rather than the file base: the expression returns
    // `http://localhost:3000/src/components/MappingViewer.tsx`, and
    // `fileURLToPath` then throws `ERR_INVALID_URL_SCHEME`. Measured, not
    // guessed. Same class of harness surprise as task 16.1's prop-capture
    // finding — the environment, not the code under test.
    const here = dirname(fileURLToPath(import.meta.url));
    const paths = {
      'components/MappingViewer.tsx': resolve(here, '../MappingViewer.tsx'),
      'utils/unsourced-target-fields.ts': resolve(here, '../../utils/unsourced-target-fields.ts'),
    };

    const truncatingMethods = new Set(['slice', 'substring', 'substr']);
    const clampIdentifiers = new Set(['WebkitLineClamp', 'lineClamp', 'textOverflow']);
    const clampStrings = [/line-clamp/i, /text-overflow/i, /ellipsis/i];

    const findings: string[] = [];

    for (const [label, path] of Object.entries(paths)) {
      const source = readFileSync(path, 'utf-8');
      const sf = ts.createSourceFile(
        label,
        source,
        ts.ScriptTarget.Latest,
        true,
        label.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
      );

      const at = (node: ts.Node) =>
        `${label}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`;

      const visit = (node: ts.Node): void => {
        if (
          ts.isCallExpression(node) &&
          ts.isPropertyAccessExpression(node.expression) &&
          truncatingMethods.has(node.expression.name.text)
        ) {
          findings.push(`${at(node)} calls .${node.expression.name.text}()`);
        }
        if (ts.isIdentifier(node) && clampIdentifiers.has(node.text)) {
          findings.push(`${at(node)} names ${node.text}`);
        }
        if (ts.isStringLiteralLike(node) && clampStrings.some((re) => re.test(node.text))) {
          findings.push(`${at(node)} carries the clamp string ${JSON.stringify(node.text)}`);
        }
        ts.forEachChild(node, visit);
      };
      ts.forEachChild(sf, visit);
    }

    expect(findings).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// R2.1-R2.4, R2.6 — the requirement, in words
// ---------------------------------------------------------------------------

describe('MappingViewer — required versus optional, in text (p45 R2)', () => {
  it('states required-ness in text, with its testid, for each state', () => {
    renderViewer([
      { field: 'mobile_phone', requirement: 'required', default_value: null },
      { field: 'nickname', requirement: 'optional' },
    ]);

    // R2.1 / R2.2 — text, not colour. The `Badge` on the name may be grey; the
    // status is a sentence either way (C1, `LGR-99050e6-1`, do-NOT 3).
    expect(screen.getByText(LABEL_REQUIRED)).toBeInTheDocument();
    expect(screen.getByText(LABEL_OPTIONAL)).toBeInTheDocument();
    expect(screen.getByTestId('requirement-required')).toBeInTheDocument();
    expect(screen.getByTestId('requirement-optional')).toBeInTheDocument();
  });

  it('states the approval rule for a required field, and only for a required field (R2.4)', () => {
    renderViewer([
      { field: 'mobile_phone', requirement: 'required', default_value: null },
      { field: 'nickname', requirement: 'optional' },
    ]);

    // The **rule**, not a per-run derivation. Re-deriving the routing decision
    // in the frontend would be a second implementation of
    // `determine_approval_requirement:1290-1296` in a second language with no
    // test able to see the two drift (do-NOT 2, p43 task 13.1's reasoning). One
    // occurrence, on the one required entry.
    const rules = screen.getAllByTestId('unmapped-target-requirement-rule');
    expect(rules).toHaveLength(1);
    expect(rules[0].textContent).toBe(RULE_REQUIRED);
    // Nothing on screen reports whether *this* run was gated.
    expect(screen.queryByText(/approval_required|was sent for approval|this run/i)).not.toBeInTheDocument();
  });

  it('renders every roster name, optional ones included (R1.7)', () => {
    // The producer orders schema-required names first (`:1199-1200`) and that
    // ordering is the only authority (R1.5, do-NOT 13). An approver must not be
    // shown a filtered list whose filter is invisible, so the optional entries
    // are on screen too — with their own label, not silently.
    const roster: UnsourcedTargetField[] = [
      { field: 'mobile_phone', requirement: 'required', default_value: null },
      { field: 'nickname', requirement: 'optional' },
      { field: 'legacy_code', requirement: 'optional' },
    ];
    renderViewer(roster);

    expect(screen.getAllByTestId('unmapped-target')).toHaveLength(3);
    for (const entry of roster) {
      expect(entriesText()).toContain(entry.field);
    }
    expect(screen.getAllByTestId('requirement-optional')).toHaveLength(2);
  });

  it('makes no required/optional claim when the requirement authority was absent (R2.6)', () => {
    // Reached from the wire: a payload with a `target` detail list and **no**
    // `target_required`, which is C15's common case — p37 is not deployed, so
    // every payload every account produces today takes this branch.
    const entries = unsourcedTargetFields({
      target: [
        { field: 'mobile_phone', default_value: null, reason: UNSOURCED_MAPPING_REASON },
        'legacy_code',
      ],
    });
    expect(entries.map((e) => e.requirement)).toEqual(['not_established', 'not_established']);

    renderViewer(entries);

    // The statement, once above the list — not repeated per entry.
    expect(screen.getAllByTestId('unmapped-target-requirement-not-established')).toHaveLength(1);
    expect(screen.getByText(FALLBACK_STATEMENT)).toBeInTheDocument();

    // And the per-entry cell still renders the `not_established` label, so a
    // reader who scans one row rather than the header still finds the fact.
    expect(screen.getAllByTestId('requirement-not-established')).toHaveLength(2);
    expect(screen.getAllByText(LABEL_NOT_ESTABLISHED)).toHaveLength(2);
  });

  it('presents no field as optional on the not-established branch (do-NOT 5)', () => {
    const entries = unsourcedTargetFields({ target: ['legacy_code', 'mobile_phone'] });
    renderViewer(entries);

    // do-NOT 5 / C4 / `LGR-4854b3d-1` (**template / ratified**): reporting *"the
    // source did not say"* as *"optional"* is the permissive collapse this spec
    // exists to remove. Two checks, because the failure has two shapes — a
    // per-entry label that says the word, and a state resolved to `optional`.
    //
    // Scoped to the entries rather than swept over the document, and that is
    // deliberate rather than a relaxation. The fallback statement above the list
    // is design §4.6's verbatim copy and it contains the word *in a negation*:
    // "so none of them is shown as required or optional". A document-wide sweep
    // for /optional/i would fail on the sentence whose whole purpose is to deny
    // the claim. What do-NOT 5 forbids is presenting a **field** as optional, so
    // that is what is asserted here — per entry, plus the two testids globally.
    expect(entriesText()).not.toMatch(/optional/i);
    expect(screen.queryByTestId('requirement-optional')).not.toBeInTheDocument();
    expect(screen.queryByTestId('requirement-required')).not.toBeInTheDocument();
    // No rule sentence either: a field whose requirement was never established
    // cannot be said to force approval or not to (R2.6).
    expect(screen.queryByTestId('unmapped-target-requirement-rule')).not.toBeInTheDocument();
  });

  it('does not render the fallback statement when the authority was present', () => {
    renderViewer([{ field: 'mobile_phone', requirement: 'required', default_value: null }]);

    expect(
      screen.queryByTestId('unmapped-target-requirement-not-established'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(FALLBACK_STATEMENT)).not.toBeInTheDocument();
  });

  it('renders no unmapped-target section at all when the list is empty', () => {
    // `every` on an empty array is `true`, so the fallback statement needs the
    // length guard the component carries — this is that guard's observer. The
    // statement is about "these fields" and there are none to be about.
    renderViewer([]);

    expect(screen.queryByTestId('unmapped-target')).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('unmapped-target-requirement-not-established'),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Unmapped Target Fields/)).not.toBeInTheDocument();
  });
});
