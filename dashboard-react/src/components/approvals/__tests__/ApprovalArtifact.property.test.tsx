// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based test for `ApprovalArtifact`'s `test_failure` branch.
 *
 * Feature: p32-test-gate-truth (bugfix)
 * - Property 1: test check details reach the screen
 *
 * Uses fast-check with Vitest and @testing-library/react, 100 iterations,
 * following the repo's `*.property.test.tsx` convention.
 *
 * **The generator is built from the producer's shape, not the reader's.** The
 * approval record is written at `approval_handler/lambda_function.py:248-267`,
 * which puts the structured test result at `details.test_result` — itself
 * `{status, checks, report_s3_uri}` from `test_code/lambda_function.py:940-944`
 * — and each check is `{name, passed, detail}`, emitted at
 * `test_code/lambda_function.py:234`, `:244`, `:253`, `:261` and `:273`. No
 * `status` key and no `message` key exist anywhere on that path.
 *
 * That direction is the point of the test. `LGR-28b4d75-5` (template): a test
 * whose expectation is transcribed from the wrong side of the wire cannot
 * detect the two sides drifting, because the output is its own oracle. The
 * fixture this test sits beside was transcribed from the reader and passed on
 * the bug for exactly that reason (`bugfix.md` 1.3).
 *
 * Expected to FAIL until the reader is corrected (p32 task 3 / C1):
 * `TestFailureArtifact` reads `details.report.checks` (`ApprovalArtifact.tsx:218-219`)
 * so it finds nothing and renders the no-checks note instead of the table, and
 * its Details column reads `check.message ?? check.details` (`:256`) so every
 * cell would render `—` even if the rows appeared.
 */
import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, screen } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval } from '../../../types';

// The test_failure branch never calls either hook, but the module graph imports
// them — keep it inert so nothing reaches the network.
vi.mock('../../../hooks', () => ({
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

// ---------------------------------------------------------------------------
// Arbitraries — constrained to what `aggregate_check_results` actually emits
// ---------------------------------------------------------------------------

/**
 * Check names the producer emits: the AI-review verdict, one
 * `static_analysis:<category>` entry per category, and the Code Interpreter
 * execution check. An index suffix keeps names distinct within one payload so
 * a row can be matched to its check unambiguously.
 */
const checkNameArb = fc.constantFrom(
  'ai_review_verdict',
  'static_analysis:correctness',
  'static_analysis:security',
  'static_analysis:schema_compliance',
  'code_interpreter_execution',
);

/**
 * `detail` is prose assembled by the producer — verdicts, category statuses,
 * record counts, and truncated error text. The alphabet covers what those
 * strings contain (including the quotes and colons a `ConversionError` message
 * carries) while staying free of leading/trailing whitespace, so a rendered
 * cell can be compared to the source string directly.
 */
const detailArb = fc
  .stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:;()\'"→'.split('')), {
    minLength: 1,
    maxLength: 120,
  })
  .map((s) => s.trim())
  .filter((s) => s.length > 0);

/** A single check as the backend writes it: `{name, passed, detail}`. */
const checkArb = fc.record({
  name: checkNameArb,
  passed: fc.boolean(),
  detail: detailArb,
});

/**
 * A `test_failure` approval whose `details.test_result.checks` is non-empty —
 * `isBugCondition_D1` from `bugfix.md`.
 */
const testFailureApprovalArb = fc
  .array(checkArb, { minLength: 1, maxLength: 4 })
  .map((rawChecks) =>
    rawChecks.map((check, index) => ({ ...check, name: `${check.name}#${index}` })),
  )
  .map(
    (checks): Approval => ({
      approval_id: 'appr-prop',
      pipeline_key: 'pipe-prop',
      approval_type: 'test_failure',
      status: 'pending',
      created_at: '2026-06-01T10:00:00Z',
      details: {
        test_result: {
          status: 'failed',
          checks,
          report_s3_uri: 's3://test-bucket/tests/exec-prop/test_report.md',
        },
        test_status: 'failed',
        failed_checks: checks.filter((c) => !c.passed).map((c) => c.name),
        report_s3_uri: 's3://test-bucket/tests/exec-prop/test_report.md',
        remediation_hint: 'retry-from-step:create_code',
      },
    }),
  );

// ---------------------------------------------------------------------------
// Property 1: test check details reach the screen
// ---------------------------------------------------------------------------

describe('Feature: p32-test-gate-truth, Property 1: test check details reach the screen', () => {
  /**
   * **Validates: Requirements 2.1, 2.2**
   *
   * For any `test_failure` approval payload whose `details.test_result.checks`
   * is non-empty, the artifact SHALL render one table row per check, and each
   * Details cell SHALL equal that check's `detail`.
   */
  it('renders one row per check with the Details cell equal to that check\u2019s detail', () => {
    fc.assert(
      fc.property(testFailureApprovalArb, (approval) => {
        cleanup();
        render(<ApprovalArtifact approval={approval} />);

        const checks = (
          (approval.details as Record<string, unknown>).test_result as {
            checks: Array<{ name: string; passed: boolean; detail: string }>;
          }
        ).checks;

        // The per-check table renders rather than the no-checks note.
        const table = screen.getByTestId('approval-artifact-test-report');
        const rows = table.querySelectorAll('tbody tr');
        expect(rows).toHaveLength(checks.length);

        // Columns are Check, Status, Details in that order (ApprovalArtifact.tsx:240-268).
        checks.forEach((check, index) => {
          const cells = rows[index].querySelectorAll('td');
          expect(cells[0].textContent).toContain(check.name);
          expect(cells[2].textContent).toBe(check.detail);
        });

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
