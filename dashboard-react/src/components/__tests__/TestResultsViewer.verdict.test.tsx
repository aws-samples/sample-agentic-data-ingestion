// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for formatStaticAnalysisVerdict — the pure helper that turns the
 * AI reviewer's raw JSON verdict (embedded in the "Original Static Analysis
 * Report" section) into readable markdown at render time.
 *
 * Frontend-only formatting change; no requirement/design spec attached.
 */

import { describe, it, expect } from 'vitest';
import { render, within, cleanup } from '@testing-library/react';
import { formatStaticAnalysisVerdict, TestResultsDisplay } from '../TestResultsViewer';

// Real example verdict object from the reviewer (used as a fixture).
const EXAMPLE_JSON =
  '{"verdict":"pass","issues":[{"severity":"minor","category":"Correctness","description":"The count reconciliation after append reads the entire table, not just the rows written in this run, so the comparison between `transformed_count` and `output_count` will almost always show a mismatch on non-empty tables and produces misleading warnings.","attribution":"template_scaffolding"},{"severity":"minor","category":"Performance","description":"Three separate `df.count()` actions are called (source_count, _dq_rows_quarantined, transformed_count) plus a post-write table scan, causing multiple full data scans; caching `df_transformed` before the DQ split would reduce redundant recomputation.","attribution":"template_scaffolding"}],"categories":{"Correctness":"pass","Schema Compliance":"pass","Error Handling":"pass","AWS Best Practices":"pass","Security":"pass","Performance":"pass"}}';

function reportWith(json: string): string {
  return `# Test Report\n\nSome preamble.\n\n# Original Static Analysis Report\n\n${json}\n\nTrailing notes.`;
}

describe('formatStaticAnalysisVerdict', () => {
  it('(a) formats the example verdict JSON and removes the raw blob', () => {
    const input = reportWith(EXAMPLE_JSON);
    const out = formatStaticAnalysisVerdict(input);

    // Human-readable content is present
    expect(out).toContain('Verdict');
    expect(out).toContain('Pass');
    // Category names surface
    expect(out).toContain('Correctness');
    expect(out).toContain('Security');
    expect(out).toContain('Performance');
    expect(out).toContain('Schema Compliance');
    // A snippet of an issue description survives
    expect(out).toContain('count reconciliation after append');
    // Attribution is rendered
    expect(out).toContain('template_scaffolding');
    // Issues heading with count
    expect(out).toContain('### Issues (2)');
    // Raw JSON blob is gone
    expect(out).not.toContain('{"verdict"');
    // Surrounding report text is preserved
    expect(out).toContain('# Original Static Analysis Report');
    expect(out).toContain('Trailing notes.');
  });

  it('(b) renders ❌ for a failing verdict and a non-pass category', () => {
    const json =
      '{"verdict":"fail","issues":[{"severity":"major","category":"Security","description":"hardcoded secret","attribution":"generated"}],"categories":{"Correctness":"pass","Security":"fail"}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));

    expect(out).toContain('**Verdict:** ❌ Fail');
    // Passing category shows check, failing category shows cross
    expect(out).toContain('- ✅ Correctness');
    expect(out).toContain('- ❌ Security');
    expect(out).not.toContain('{"verdict"');
  });

  it('(c) returns the input unchanged when there is no verdict JSON', () => {
    const input = '# Original Static Analysis Report\n\nNo structured verdict here, just prose.';
    expect(formatStaticAnalysisVerdict(input)).toBe(input);
  });

  it('(d) returns the input unchanged for malformed/partial JSON (no throw)', () => {
    const input = reportWith('{"verdict":"pass"');
    let out: string;
    expect(() => {
      out = formatStaticAnalysisVerdict(input);
    }).not.toThrow();
    expect(formatStaticAnalysisVerdict(input)).toBe(input);
  });

  it('(e) renders "No issues reported." when the issues array is empty', () => {
    const json = '{"verdict":"pass","issues":[],"categories":{"Correctness":"pass"}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));

    expect(out).toContain('No issues reported.');
    expect(out).not.toContain('### Issues');
    expect(out).toContain('**Verdict:** ✅ Pass');
    expect(out).not.toContain('{"verdict"');
  });

  it('handles an absent issues array (issues key missing)', () => {
    const json = '{"verdict":"pass","categories":{"Correctness":"pass"}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));

    expect(out).toContain('No issues reported.');
    expect(out).not.toContain('### Issues');
  });

  it('handles verdict object with whitespace after the opening brace', () => {
    const json = '{ "verdict":"pass","categories":{"Security":"pass"}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));

    expect(out).toContain('**Verdict:** ✅ Pass');
    expect(out).toContain('- ✅ Security');
    expect(out).not.toContain('{ "verdict"');
  });

  it('is case-insensitive for the verdict value', () => {
    const json = '{"verdict":"PASS","categories":{}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));
    expect(out).toContain('**Verdict:** ✅ Pass');
  });

  it('correctly skips braces embedded inside string literals', () => {
    // Description contains a literal `}` and `{` inside the string — the walker
    // must not treat those as the closing brace.
    const json =
      '{"verdict":"pass","issues":[{"severity":"minor","category":"Style","description":"use {curly} and } braces carefully"}],"categories":{"Correctness":"pass"}}';
    const out = formatStaticAnalysisVerdict(reportWith(json));

    expect(out).toContain('use {curly} and } braces carefully');
    expect(out).toContain('### Issues (1)');
    expect(out).not.toContain('{"verdict"');
    // Trailing report text must survive (proves the correct closing brace was found)
    expect(out).toContain('Trailing notes.');
  });
});

describe('TestResultsDisplay (component) — verdict formatting is wired in the main viewer', () => {
  // Note: TestResultsDisplay is the hook-free pure component; it renders raw
  // output via formatOutput and is intentionally NOT changed by this task.
  // The formatting is applied in the main TestResultsViewer render path. Here
  // we assert the helper produces DOM-safe markdown when rendered directly.
  it('produces content free of the raw verdict blob', () => {
    cleanup();
    const formatted = formatStaticAnalysisVerdict(reportWith(EXAMPLE_JSON));
    const { container } = render(
      <TestResultsDisplay testResult={{ test_status: 'passed', test_output: formatted }} />,
    );
    const viewer = within(container).getByTestId('test-results-viewer');
    expect(viewer.textContent ?? '').not.toContain('{"verdict"');
    expect(viewer.textContent ?? '').toContain('Verdict');
    cleanup();
  });
});

/* ------------------------------------------------------------------ */
/*  p43 R4.3 — test 4.3b: one named example per blocking phrasing.     */
/*                                                                    */
/*  Named examples, not only a property: a failure here names which    */
/*  phrasing broke, where a shrunk counterexample would not.           */
/*                                                                    */
/*  The four phrasings below are transcribed from the spec             */
/*  (design.md §4.5, requirements R4.3), NOT read back from the        */
/*  component. LGR-28b4d75-5 (template / ratified): a test written     */
/*  from the producer's own output cannot detect the producer          */
/*  drifting, because the output is then its own oracle. For the same  */
/*  reason the non-blocking severity token is spelled `minor`          */
/*  literally here rather than imported from the component's           */
/*  NON_BLOCKING_SEVERITIES set — the cross-language pin against the   */
/*  Python frozenset is task 13.4, not this file.                      */
/* ------------------------------------------------------------------ */

const PHRASE_UNATTRIBUTED =
  'no reviewer attribution — does not block a pass verdict, blocks a fail verdict';
const PHRASE_SCAFFOLDING = 'scaffolding — does not fail the gate by itself';
const PHRASE_NON_BLOCKING_SEVERITY = 'reported at non-blocking severity — did not fail the gate';
const PHRASE_BLOCKED = 'blocked the gate';

/** The four defined phrasings. Mutually non-substring, so `includes` is exact enough. */
const ALL_PHRASES = [
  PHRASE_UNATTRIBUTED,
  PHRASE_SCAFFOLDING,
  PHRASE_NON_BLOCKING_SEVERITY,
  PHRASE_BLOCKED,
];

/** Build a report whose embedded verdict carries the given issues. */
function reportWithIssues(
  verdict: string,
  issues: Array<Record<string, unknown>>,
  categories: Record<string, string> = { Correctness: 'pass' },
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

/** One issue per phrasing, in phrasing order 4, 3, 2, 1. */
const FOUR_PHRASING_ISSUES: Array<Record<string, unknown>> = [
  {
    severity: 'critical',
    category: 'Correctness',
    description: 'silently drops late-arriving rows',
    attribution: 'transform_block',
  },
  {
    severity: 'minor',
    category: 'Performance',
    description: 'one redundant count action',
    attribution: 'transform_block',
  },
  {
    severity: 'major',
    category: 'Style',
    description: 'boilerplate header comment',
    attribution: 'template_scaffolding',
  },
  { severity: 'minor', category: 'Style', description: 'reviewer supplied no attribution' },
];

describe('formatStaticAnalysisVerdict — blocking status per issue (p43 R4.3, test 4.3b)', () => {
  it('blocked: a reviewer transform_block finding at critical', () => {
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'critical',
          category: 'Correctness',
          description: 'drops late rows',
          attribution: 'transform_block',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_BLOCKED]);
    expect(bullets[0]).toContain(`_(transform_block · ${PHRASE_BLOCKED})_`);
    expect(out).not.toContain('{"verdict"');
  });

  it('blocked: a reviewer transform_block finding at major', () => {
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'major',
          category: 'Security',
          description: 'credential in a job argument',
          attribution: 'transform_block',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_BLOCKED]);
    expect(bullets[0]).toContain(`_(transform_block · ${PHRASE_BLOCKED})_`);
  });

  it('non-blocking severity: the same finding at minor is retained, not blocking', () => {
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'minor',
          category: 'Performance',
          description: 'one redundant count action',
          attribution: 'transform_block',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_NON_BLOCKING_SEVERITY]);
    expect(bullets[0]).toContain(`_(transform_block · ${PHRASE_NON_BLOCKING_SEVERITY})_`);
  });

  it('non-blocking severity: comparison is exact, so a near-miss token blocks', () => {
    // `minorly` is not `minor`. The gate classifies by exact equality against
    // the already-normalised token (R1.5), never by prefix or substring, and an
    // unrecognised token blocks — so the viewer must say so too.
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'minorly',
          category: 'Performance',
          description: 'novel severity token',
          attribution: 'transform_block',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_BLOCKED]);
  });

  it('scaffolding: a template_scaffolding finding does not fail the gate by itself', () => {
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'major',
          category: 'Style',
          description: 'boilerplate header comment',
          attribution: 'template_scaffolding',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    // Severity is irrelevant on this branch — `major` still reads as scaffolding.
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_SCAFFOLDING]);
    expect(bullets[0]).toContain(`_(template_scaffolding · ${PHRASE_SCAFFOLDING})_`);
  });

  it('unattributed: an issue with no attribution key states the rule, not a per-run outcome', () => {
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        { severity: 'minor', category: 'Style', description: 'no attribution supplied' },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_UNATTRIBUTED]);
    // No token to show, so the suffix carries the phrase alone — no ` · ` separator.
    expect(bullets[0]).toContain(`_(${PHRASE_UNATTRIBUTED})_`);
    expect(bullets[0]).not.toContain(` · ${PHRASE_UNATTRIBUTED}`);
  });

  it('unattributed: an unrecognised attribution token is shown verbatim and labelled unattributed', () => {
    // Two facts, both carried: what the reviewer said, and what the gate does
    // with it. `generated` is not a recognised attribution, so it reaches the
    // unattributed rule — but it is still displayed as the reviewer wrote it.
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('fail', [
        {
          severity: 'major',
          category: 'Security',
          description: 'hardcoded secret',
          attribution: 'generated',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(1);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_UNATTRIBUTED]);
    expect(bullets[0]).toContain(`_(generated · ${PHRASE_UNATTRIBUTED})_`);
  });

  it('classifies case-insensitively while displaying the reviewer’s own spelling', () => {
    // The one place display and comparison deliberately diverge: the gate reads
    // an already-lowercased token, so the viewer folds case to classify — and
    // still renders what the reviewer wrote, unchanged.
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: 'MINOR',
          category: 'Performance',
          description: 'uppercase reviewer output',
          attribution: 'TRANSFORM_BLOCK',
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_NON_BLOCKING_SEVERITY]);
    expect(bullets[0]).toContain('**MINOR · Performance**');
    expect(bullets[0]).toContain(`_(TRANSFORM_BLOCK · ${PHRASE_NON_BLOCKING_SEVERITY})_`);
    // Display is not normalised on the way through.
    expect(bullets[0]).not.toContain('**minor ·');
    expect(bullets[0]).not.toContain('_(transform_block ·');
  });

  it('tolerates padded tokens while displaying them verbatim', () => {
    const paddedSeverity = ' minor ';
    const paddedAttribution = ' transform_block ';
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', [
        {
          severity: paddedSeverity,
          category: 'Performance',
          description: 'padded reviewer output',
          attribution: paddedAttribution,
        },
      ]),
    );
    const bullets = issueBullets(out);
    expect(phrasesIn(bullets[0])).toEqual([PHRASE_NON_BLOCKING_SEVERITY]);
    expect(bullets[0]).toContain(`_(${paddedAttribution} · ${PHRASE_NON_BLOCKING_SEVERITY})_`);
  });

  it('gives every issue in a mixed list exactly one of the four phrasings', () => {
    const out = formatStaticAnalysisVerdict(reportWithIssues('pass', FOUR_PHRASING_ISSUES));
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(4);
    expect(bullets.map((b) => phrasesIn(b))).toEqual([
      [PHRASE_BLOCKED],
      [PHRASE_NON_BLOCKING_SEVERITY],
      [PHRASE_SCAFFOLDING],
      [PHRASE_UNATTRIBUTED],
    ]);
  });
});

/* ------------------------------------------------------------------ */
/*  p43 R4.3 — the status is stated in WORDS.                          */
/*                                                                    */
/*  LGR-99050e6-1 (template / ratified), constraint C7: "Colour alone  */
/*  fails WCAG 1.4.1 and makes the inversion invisible". The           */
/*  executable form of that is (i) the phrase is present as text and   */
/*  (ii) no new icon or styling was introduced to carry the status.    */
/* ------------------------------------------------------------------ */

/** Emoji / pictographic icons. `·` and `—` are punctuation, not icons. */
const PICTOGRAPHIC_RE = /\p{Extended_Pictographic}/u;

describe('formatStaticAnalysisVerdict — status in words, not icon or colour (p43 R4.3, C7)', () => {
  it('introduces no emoji or icon into the issue bullets', () => {
    const out = formatStaticAnalysisVerdict(reportWithIssues('pass', FOUR_PHRASING_ISSUES));
    const bullets = issueBullets(out);
    expect(bullets).toHaveLength(4);
    for (const bullet of bullets) {
      expect(bullet).not.toMatch(PICTOGRAPHIC_RE);
      expect(phrasesIn(bullet)).toHaveLength(1);
    }
  });

  it('introduces no styling or markup to carry the status', () => {
    const out = formatStaticAnalysisVerdict(reportWithIssues('fail', FOUR_PHRASING_ISSUES));
    for (const bullet of issueBullets(out)) {
      expect(bullet).not.toMatch(/style\s*=/);
      expect(bullet).not.toMatch(/<[a-z]/i);
      expect(bullet).not.toMatch(/colorText|#[0-9a-f]{3,6}\b/i);
    }
  });

  it('leaves the pre-existing verdict and category icons as the only icons in the output', () => {
    const categories = { Correctness: 'pass', Security: 'fail', Performance: 'pass' };
    const out = formatStaticAnalysisVerdict(
      reportWithIssues('pass', FOUR_PHRASING_ISSUES, categories),
    );
    // One on the `**Verdict:**` line, one per category check, none in the bullets.
    const icons = out.match(/[✅❌]/gu) ?? [];
    expect(icons).toHaveLength(1 + Object.keys(categories).length);
    expect(issueBullets(out)).toHaveLength(4);
  });

  it('carries all four phrasings into the rendered DOM as text', () => {
    cleanup();
    const formatted = formatStaticAnalysisVerdict(
      reportWithIssues('pass', FOUR_PHRASING_ISSUES),
    );
    const { container } = render(
      <TestResultsDisplay testResult={{ test_status: 'passed', test_output: formatted }} />,
    );
    const text = within(container).getByTestId('test-results-viewer').textContent ?? '';
    for (const phrase of ALL_PHRASES) {
      expect(text).toContain(phrase);
    }
    cleanup();
  });
});
