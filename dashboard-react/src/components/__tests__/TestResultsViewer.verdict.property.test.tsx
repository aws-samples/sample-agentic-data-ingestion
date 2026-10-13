// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Feature: p43-review-gate-severity-floor, Property 11: Every rendered issue states its blocking status in words
/**
 * Property-based tests for `formatStaticAnalysisVerdict` — the blocking status
 * the Test_Report_Viewer states for each rendered issue (p43 R4.3, design §4.5).
 *
 * Uses fast-check with Vitest. Minimum 100 runs required; 200 are configured.
 *
 * **Validates: Requirements 4.3**
 *
 * ## Where the expectations come from
 *
 * The four phrasings and the classification rule below are transcribed from the
 * spec — `design.md` §4.5 and `requirements.md` R4.3 / R1.5 — and NOT read back
 * from `TestResultsViewer.tsx`. `LGR-28b4d75-5` (**template / ratified**): a test
 * written from the producer's own output cannot detect the producer drifting,
 * because the output is then its own oracle. For the same reason the
 * non-blocking severity token is spelled `minor` literally here rather than
 * imported from the component's `NON_BLOCKING_SEVERITIES` set; the cross-language
 * pin against the Python frozenset is a separate structural test.
 *
 * The helper spellings (`reportWith`, `reportWithIssues`, `issueBullets`,
 * `phrasesIn`, `PHRASE_*`, `PICTOGRAPHIC_RE`) are re-derived from the sibling
 * example file `TestResultsViewer.verdict.test.tsx` rather than re-invented — a
 * third spelling of "which lines are the issue bullets" would be a third thing
 * to keep true. They are module-private there, so they are duplicated rather
 * than imported.
 *
 * ## Generator space, and what is deliberately outside it
 *
 * - **severity** — the three recognised tokens, unrecognised tokens, near-miss
 *   tokens (`minorly`, `min`, `minor.`), case variants, space/tab-padded forms,
 *   the empty string, and an absent key. The near-misses are the interesting
 *   shrink targets: they attack the exact-equality half of R1.5, where a prefix
 *   or substring comparison would silently exempt a novel severity.
 * - **attribution** — both recognised tokens, case and padding variants,
 *   unrecognised tokens, an absent key, and non-string values (`null`, a number,
 *   a boolean, an object, an array), which the viewer must treat as no
 *   attribution at all rather than throwing.
 * - **raw verdict** — both values. The verdict is not an input to the per-issue
 *   status by design (§4.5's fourth decision: every phrase describes what the
 *   issue does *by itself*), so drawing both is how that independence is tested.
 * - **category / description** — reviewer free text over a restricted ASCII
 *   alphabet that includes braces and the component's own `·` and `—`
 *   separators, and excludes `<`, `>`, `#`, newlines and pictographic
 *   characters.
 *   The exclusions are what make the "in words, not an icon" half of R4.3
 *   checkable: with the reviewer's text icon-free, markup-free and unable to
 *   spell a `#rrggbb` colour literal by construction, any icon, tag or colour
 *   in a bullet is necessarily the component's.
 *   Text carrying a literal newline is out of scope for a different reason — it
 *   splits the markdown bullet across lines, which is a `renderMarkdown`
 *   concern this property does not adjudicate.
 * - **categories map** — held fixed. It is not an input to the issue loop, and
 *   it supplies the `###` terminator that bounds the bullet region.
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { formatStaticAnalysisVerdict } from '../TestResultsViewer';

/* ------------------------------------------------------------------ */
/*  The four defined phrasings — transcribed from design §4.5.         */
/* ------------------------------------------------------------------ */

const PHRASE_UNATTRIBUTED =
  'no reviewer attribution — does not block a pass verdict, blocks a fail verdict';
const PHRASE_SCAFFOLDING = 'scaffolding — does not fail the gate by itself';
const PHRASE_NON_BLOCKING_SEVERITY = 'reported at non-blocking severity — did not fail the gate';
const PHRASE_BLOCKED = 'blocked the gate';

const ALL_PHRASES = [
  PHRASE_UNATTRIBUTED,
  PHRASE_SCAFFOLDING,
  PHRASE_NON_BLOCKING_SEVERITY,
  PHRASE_BLOCKED,
];

/** Recognised attribution tokens (R4.3, and the parser's `ALLOWED_ATTRIBUTIONS`). */
const ATTRIBUTION_TRANSFORM_BLOCK = 'transform_block';
const ATTRIBUTION_TEMPLATE_SCAFFOLDING = 'template_scaffolding';

/**
 * The non-blocking severity set, spelled from the spec (R2.4: the Python
 * `NON_BLOCKING_SEVERITIES` is `frozenset({"minor"})`), not imported from the
 * component. See the header note on `LGR-28b4d75-5`.
 */
const NON_BLOCKING_SEVERITY_TOKENS = new Set(['minor']);

/** Emoji / pictographic icons. `·` and `—` are punctuation, not icons. */
const PICTOGRAPHIC_RE = /\p{Extended_Pictographic}/u;

/* ------------------------------------------------------------------ */
/*  Report builders and extractors (re-derived from the sibling file). */
/* ------------------------------------------------------------------ */

const CATEGORIES: Record<string, string> = { Correctness: 'pass' };

function reportWith(json: string): string {
  return `# Test Report\n\nSome preamble.\n\n# Original Static Analysis Report\n\n${json}\n\nTrailing notes.`;
}

function reportWithIssues(
  verdict: string,
  issues: Array<Record<string, unknown>>,
  categories: Record<string, string> = CATEGORIES,
): string {
  return reportWith(JSON.stringify({ verdict, issues, categories }));
}

/** The issue bullet lines only — between the `### Issues (n)` heading and the next heading. */
function issueBullets(out: string): string[] {
  const lines = out.split('\n');
  const start = lines.findIndex((l) => l.startsWith('### Issues ('));
  if (start === -1) return [];
  const bullets: string[] = [];
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i].startsWith('###')) break;
    if (lines[i].startsWith('- ')) bullets.push(lines[i]);
  }
  return bullets;
}

/** Which of the four defined phrasings a bullet carries. */
function phrasesIn(bullet: string): string[] {
  return ALL_PHRASES.filter((p) => bullet.includes(p));
}

/* ------------------------------------------------------------------ */
/*  The generator space.                                              */
/* ------------------------------------------------------------------ */

/** Sentinel: the reviewer omitted the key entirely, as against supplying a value. */
const ABSENT = Symbol('absent');

interface IssueDraft {
  /** `string` in every draw, or `ABSENT` for an omitted key. */
  severity: unknown;
  category: string;
  description: string;
  /** A token, a non-string value, or `ABSENT` for an omitted key. */
  attribution: unknown;
}

const SEVERITY_TOKENS: unknown[] = [
  // Recognised (R2.6).
  'critical',
  'major',
  'minor',
  // Unrecognised — blocks, for the same reason the gate blocks (R1.2, R2.3).
  'blocker',
  'nit',
  'error',
  'info',
  'high',
  // Near misses on `minor` — the exact-equality half of R1.5.
  'minorly',
  'min',
  'minor.',
  'minors',
  // Case variants.
  'Minor',
  'MINOR',
  'MaJoR',
  'CRITICAL',
  // Space- and tab-padded forms (no newlines — see the header).
  ' minor ',
  '\tminor',
  'minor\t',
  '  MINOR  ',
  ' critical ',
  // Empty, and absent.
  '',
  ABSENT,
];

const ATTRIBUTION_TOKENS: unknown[] = [
  // Recognised.
  ATTRIBUTION_TRANSFORM_BLOCK,
  ATTRIBUTION_TEMPLATE_SCAFFOLDING,
  // Recognised, spelled differently by the reviewer.
  'TRANSFORM_BLOCK',
  ' transform_block ',
  'Template_Scaffolding',
  '\ttemplate_scaffolding ',
  // Unrecognised, including a near miss on the separator.
  'generated',
  'transform-block',
  'transform_blocks',
  'scaffolding',
  '',
  // Non-string values.
  null,
  42,
  true,
  { attribution: ATTRIBUTION_TRANSFORM_BLOCK },
  [ATTRIBUTION_TRANSFORM_BLOCK],
  // Absent key.
  ABSENT,
];

/**
 * Reviewer free text. Includes `{`/`}` so the JSON brace-walker is exercised
 * over the whole space, and the component's own `·`/`—` separators so a
 * description cannot be told from the status by punctuation alone. Excludes
 * `<`, `>`, `#`, newlines and pictographs — see the header. `#` is excluded so
 * random text cannot forge a `#rrggbb` colour literal and flake the C7
 * assertion below on text the component did not write.
 */
const SAFE_TEXT_CHARS =
  "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:!?'\"/*+-=_()[]{}·—`%$@&|~^".split(
    '',
  );

const REALISTIC_TEXT = [
  'silently drops late-arriving rows',
  'count reconciliation reads the entire table',
  'credential in a job argument',
  'boilerplate header comment',
  'use {curly} and } braces carefully',
  'severity · category — description',
  '',
];

const safeTextArb: fc.Arbitrary<string> = fc
  .oneof(
    fc.constantFrom(...REALISTIC_TEXT),
    fc.stringOf(fc.constantFrom(...SAFE_TEXT_CHARS), { maxLength: 48 }),
  )
  // A phrase inside reviewer text would make `phrasesIn` ambiguous, and the
  // property is about the status the component adds, not about text that
  // quotes it.
  .filter((s) => !ALL_PHRASES.some((p) => s.includes(p)) && !s.includes('{"verdict'));

const issueDraftArb: fc.Arbitrary<IssueDraft> = fc.record({
  severity: fc.constantFrom(...SEVERITY_TOKENS),
  category: safeTextArb,
  description: safeTextArb,
  attribution: fc.constantFrom(...ATTRIBUTION_TOKENS),
});

interface Scenario {
  verdict: string;
  drafts: IssueDraft[];
}

const scenarioArb: fc.Arbitrary<Scenario> = fc.record({
  verdict: fc.constantFrom('pass', 'fail'),
  drafts: fc.array(issueDraftArb, { minLength: 0, maxLength: 5 }),
});

/** Build the reviewer's raw issue object, omitting the keys drawn as `ABSENT`. */
function buildIssue(draft: IssueDraft): Record<string, unknown> {
  const issue: Record<string, unknown> = {
    category: draft.category,
    description: draft.description,
  };
  if (draft.severity !== ABSENT) issue.severity = draft.severity;
  if (draft.attribution !== ABSENT) issue.attribution = draft.attribution;
  return issue;
}

function renderScenario(scenario: Scenario): string {
  return formatStaticAnalysisVerdict(reportWithIssues(scenario.verdict, scenario.drafts.map(buildIssue)));
}

/**
 * The phrasing the spec requires for an issue — the classification rule from
 * design §4.5 restated, reading the drawn draft rather than the component's
 * output. Order matters: attribution decides first, severity only inside the
 * `transform_block` branch.
 */
function expectedPhrase(draft: IssueDraft): string {
  const attribution =
    typeof draft.attribution === 'string' ? draft.attribution.trim().toLowerCase() : '';
  if (attribution === ATTRIBUTION_TEMPLATE_SCAFFOLDING) return PHRASE_SCAFFOLDING;
  if (attribution !== ATTRIBUTION_TRANSFORM_BLOCK) return PHRASE_UNATTRIBUTED;
  const severity = typeof draft.severity === 'string' ? draft.severity.trim().toLowerCase() : '';
  // Exact equality against the normalised token, never a prefix or substring
  // (R1.5) — so `minorly` and `min` block.
  if (NON_BLOCKING_SEVERITY_TOKENS.has(severity)) return PHRASE_NON_BLOCKING_SEVERITY;
  return PHRASE_BLOCKED;
}

/** The near-miss severities, always executed rather than left to sampling. */
const SEEDED_SCENARIOS: Scenario[] = [
  'minorly',
  'min',
  'minor.',
  'minors',
  'Minor',
  ' minor ',
  '\tminor',
  'minor',
  'MINOR',
  '',
  ABSENT,
].map((severity) => ({
  verdict: 'pass',
  drafts: [
    {
      severity,
      category: 'Performance',
      description: 'near-miss severity token',
      attribution: ATTRIBUTION_TRANSFORM_BLOCK,
    },
  ],
}));

SEEDED_SCENARIOS.push(
  {
    // A non-string attribution must read as no attribution, not throw.
    verdict: 'fail',
    drafts: [
      { severity: 'minor', category: 'Style', description: 'non-string attribution', attribution: 42 },
    ],
  },
  {
    // Every branch at once, on a raw fail.
    verdict: 'fail',
    drafts: [
      {
        severity: 'critical',
        category: 'Correctness',
        description: 'blocks',
        attribution: ATTRIBUTION_TRANSFORM_BLOCK,
      },
      {
        severity: 'minor',
        category: 'Performance',
        description: 'retained',
        attribution: ATTRIBUTION_TRANSFORM_BLOCK,
      },
      {
        severity: 'major',
        category: 'Style',
        description: 'scaffolding',
        attribution: ATTRIBUTION_TEMPLATE_SCAFFOLDING,
      },
      { severity: ABSENT, category: 'Style', description: 'unattributed', attribution: ABSENT },
    ],
  },
);

const SEEDED_EXAMPLES: Array<[Scenario]> = SEEDED_SCENARIOS.map((s) => [s]);

const RUNS = { numRuns: 200, examples: SEEDED_EXAMPLES } as const;

/* ------------------------------------------------------------------ */
/*  Guard: `phrasesIn` is only exact if the phrasings are mutually     */
/*  non-substring. Asserted rather than assumed.                      */
/* ------------------------------------------------------------------ */

describe('the four phrasings are distinguishable', () => {
  it('no phrasing is a substring of another', () => {
    for (const a of ALL_PHRASES) {
      for (const b of ALL_PHRASES) {
        if (a !== b) expect(b.includes(a)).toBe(false);
      }
    }
  });
});

/* ------------------------------------------------------------------ */
/*  Property 11.                                                      */
/* ------------------------------------------------------------------ */

describe('Property 11: every rendered issue states its blocking status in words (p43 R4.3)', () => {
  it('gives every bullet exactly one blocking statement, as text, with the raw JSON gone', () => {
    fc.assert(
      fc.property(scenarioArb, (scenario) => {
        const out = renderScenario(scenario);

        // The raw JSON blob is gone — over the whole space, not one example.
        expect(out).not.toContain('{"verdict"');
        // ...and the splice found the right closing brace, so the report survives.
        expect(out).toContain('Trailing notes.');
        expect(out).toContain('# Original Static Analysis Report');

        const bullets = issueBullets(out);
        // Every issue is rendered, so no issue can reach the reader without a
        // status attached to it.
        expect(bullets).toHaveLength(scenario.drafts.length);

        for (const bullet of bullets) {
          // Exactly one of the four defined phrasings — not zero (a silent
          // issue) and not two (an ambiguous one).
          expect(phrasesIn(bullet)).toHaveLength(1);
          // Stated in words: no icon, and no markup or styling carrying it.
          // C7 / LGR-99050e6-1 — colour alone fails WCAG 1.4.1.
          expect(bullet).not.toMatch(PICTOGRAPHIC_RE);
          expect(bullet).not.toMatch(/<[a-z]/i);
          expect(bullet).not.toMatch(/style\s*=/);
          expect(bullet).not.toMatch(/colorText|#[0-9a-f]{6}\b/i);
        }

        // The pre-existing verdict and category icons remain the only icons in
        // the output: one on the `**Verdict:**` line, one per category check.
        expect(out.match(/[✅❌]/gu) ?? []).toHaveLength(1 + Object.keys(CATEGORIES).length);
      }),
      RUNS,
    );
  });

  it('draws the statement from the phrasing the spec assigns to that issue', () => {
    fc.assert(
      fc.property(scenarioArb, (scenario) => {
        const bullets = issueBullets(renderScenario(scenario));
        expect(bullets).toHaveLength(scenario.drafts.length);
        expect(bullets.map((b) => phrasesIn(b))).toEqual(
          scenario.drafts.map((d) => [expectedPhrase(d)]),
        );
      }),
      RUNS,
    );
  });
});
