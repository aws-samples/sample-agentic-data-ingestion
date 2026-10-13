// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for ApprovalArtifact — the type-switched artifact renderer (P29 R2).
 *
 * Validates: Requirements 2.1, 2.5, 2.6
 *
 * Covers:
 *  - code_generation → the generated code renders in the shared CodeViewer, and
 *    a diff renders when a previous version id is present.
 *  - schema_mapping → the fallback-provenance badge renders when `generated_by`
 *    starts with `fallback_` and is ABSENT otherwise (p28 R3.5 field guard).
 *  - test_failure → a per-check report table renders (p28 R4.2 type guard),
 *    and a missing report is safe.
 *  - unknown/absent approval types are safe (no crash, nothing rendered).
 *
 * The `../../../hooks` module is mocked so the reused CodeViewer/MappingViewer
 * do not hit the network: `useCodeVersion` returns controlled code content and
 * `useBedrockModels` returns an empty list.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval } from '../../../types';

const CURRENT_CODE = 'def transform(df):\n    return df.withColumn("new", lit(1))';
const PREVIOUS_CODE = 'def transform(df):\n    return df';

vi.mock('../../../hooks', () => ({
  useCodeVersion: (id: string | undefined) => {
    if (id === 'code-current') {
      return {
        data: { code_version_id: id, code: CURRENT_CODE, code_content: CURRENT_CODE, language: 'python', created_at: '' },
        isLoading: false,
        isError: false,
      };
    }
    if (id === 'code-previous') {
      return {
        data: { code_version_id: id, code: PREVIOUS_CODE, code_content: PREVIOUS_CODE, language: 'python', created_at: '' },
        isLoading: false,
        isError: false,
      };
    }
    // Disabled query (undefined id) or unknown id.
    return { data: undefined, isLoading: false, isError: false };
  },
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

function makeApproval(overrides: Partial<Approval>): Approval {
  return {
    approval_id: 'appr-1',
    pipeline_key: 'pipe-1',
    approval_type: 'code_generation',
    status: 'pending',
    created_at: '2026-06-01T10:00:00Z',
    ...overrides,
  };
}

describe('ApprovalArtifact — code_generation (R2.1)', () => {
  it('renders the generated code in the CodeViewer', () => {
    const approval = makeApproval({
      approval_type: 'code_generation',
      details: { code_version_id: 'code-current' },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-code')).toBeInTheDocument();
    const codeContent = screen.getByTestId('code-content');
    expect(codeContent.textContent).toContain('withColumn("new"');
  });

  it('renders a side-by-side diff when a previous version id is present', () => {
    const approval = makeApproval({
      approval_type: 'code_generation',
      details: { code_version_id: 'code-current', previous_code_version_id: 'code-previous' },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-code-diff')).toBeInTheDocument();
    expect(screen.getByTestId('agent-output-diff')).toBeInTheDocument();
    // Both versions' distinctive lines appear in the diff.
    const diff = screen.getByTestId('agent-output-diff');
    expect(diff.textContent).toContain('withColumn("new"');
  });

  it('shows a neutral note when code_version_id is missing (safe)', () => {
    const approval = makeApproval({ approval_type: 'code_generation', details: {} });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('code-content')).not.toBeInTheDocument();
  });
});

describe('ApprovalArtifact — schema_mapping fallback provenance (R2.5)', () => {
  const mappingDetails = {
    proposed_mappings: [
      { source: 'cust_id', target: 'customer_id', confidence: 0.5 },
      { source: 'fname', target: 'first_name', confidence: 0.4 },
    ],
    unmapped_fields: { source: ['extra'], target: ['unused'] },
  };

  it('renders the fallback badge when generated_by starts with "fallback_"', () => {
    const approval = makeApproval({
      approval_type: 'schema_mapping',
      details: { ...mappingDetails, generated_by: 'fallback_substring_match' },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.getByTestId('approval-artifact-fallback-badge')).toBeInTheDocument();
    // The reused mapping table still renders the fields.
    expect(screen.getByTestId('mapping-viewer').textContent).toContain('cust_id');
  });

  it('does NOT render the fallback badge when generated_by is a real model id', () => {
    const approval = makeApproval({
      approval_type: 'schema_mapping',
      details: { ...mappingDetails, generated_by: 'anthropic.claude-3-5-sonnet-20241022-v2:0' },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-fallback-badge')).not.toBeInTheDocument();
  });

  it('does NOT render the fallback badge when generated_by is absent (pre-p28)', () => {
    const approval = makeApproval({
      approval_type: 'schema_mapping',
      details: { ...mappingDetails },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-fallback-badge')).not.toBeInTheDocument();
  });
});

describe('ApprovalArtifact — test_failure report (R2.6)', () => {
  /**
   * **Validates: Requirements 2.1, 2.2, 2.3** (p32 bugfix)
   *
   * This fixture is transcribed from the PRODUCER, not from the reader.
   * `approval_handler/lambda_function.py:248-267` writes the per-check list at
   * `details.test_result.checks` (its own `test_result` is `{status, checks,
   * report_s3_uri}`, built at `test_code/lambda_function.py:940-944`), and each
   * check is `{name, passed, detail}` — emitted at
   * `test_code/lambda_function.py:234`, `:244`, `:253`, `:261` and `:273`.
   * There is no `status` key and no `message` key anywhere on that path.
   *
   * The fixture this replaced was `{ report: { checks: [{ name, status,
   * message }] } }` — the shape `TestFailureArtifact` happens to read
   * (`ApprovalArtifact.tsx:218-219`, `:256`), which is a shape the backend has
   * never written. It therefore passed while the table could not render any
   * real payload (`bugfix.md` 1.3). `LGR-28b4d75-5` (template) is the governing
   * rule: a test whose expectation is transcribed from the wrong side of the
   * wire cannot detect the two sides drifting, because the output is its own
   * oracle.
   */
  const REVIEW_DETAIL = 'Reviewer verdict: PASS; effective verdict: PASS (no issues reported)';
  const STATIC_DETAIL = "Static analysis category 'correctness': PASS";
  const EXECUTION_DETAIL =
    "Execution failed: PySpark-to-pandas conversion failed: unconvertible PySpark construct 'withColumn' on line 5";

  it('leads with the "Code testing failed" error alert and renders the per-check table', () => {
    const approval = makeApproval({
      approval_type: 'test_failure',
      details: {
        test_result: {
          status: 'failed',
          checks: [
            { name: 'ai_review_verdict', passed: true, detail: REVIEW_DETAIL },
            { name: 'static_analysis:correctness', passed: true, detail: STATIC_DETAIL },
            { name: 'code_interpreter_execution', passed: false, detail: EXECUTION_DETAIL },
          ],
          report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
        },
        test_status: 'failed',
        failed_checks: ['code_interpreter_execution'],
        report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
        remediation_hint: 'retry-from-step:create_code',
      },
    });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-test-failure')).toBeInTheDocument();
    // The failure banner is always present and explicit.
    const alert = screen.getByTestId('approval-artifact-test-failure-alert');
    expect(alert.textContent).toContain('Code testing failed');
    // One row per check, in payload order (2.1).
    const table = screen.getByTestId('approval-artifact-test-report');
    const rows = table.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(3);
    expect(table.textContent).toContain('ai_review_verdict');
    expect(table.textContent).toContain('static_analysis:correctness');
    expect(table.textContent).toContain('code_interpreter_execution');
    // The Details column carries each check's `detail` (2.2). This is the text
    // that names the construct and the line the run actually failed on, so a
    // `—` here is the difference between an informed sign-off and a blind one.
    const detailsCell = (row: Element) => row.querySelectorAll('td')[2]?.textContent ?? '';
    expect(detailsCell(rows[0])).toContain(REVIEW_DETAIL);
    expect(detailsCell(rows[1])).toContain(STATIC_DETAIL);
    expect(detailsCell(rows[2])).toContain(EXECUTION_DETAIL);
  });

  it('surfaces an overall reason string when present in the report', () => {
    const approval = makeApproval({
      approval_type: 'test_failure',
      details: {
        report: {
          summary: 'Data quality checks failed: 3 of 5 checks did not pass',
          checks: [{ name: 'null_check', status: 'failed', message: 'nulls found' }],
        },
      },
    });

    render(<ApprovalArtifact approval={approval} />);

    const reason = screen.getByTestId('approval-artifact-test-failure-reason');
    expect(reason.textContent).toContain('Data quality checks failed');
  });

  /**
   * **Validates: Requirements 2.20, 2.23** (p32 bugfix, task 6b — the reader half)
   *
   * `aggregate_check_results` emits a THIRD kind of check when the pandas proxy
   * refuses a construct (`test_code/lambda_function.py:286-320`): `passed` is
   * `false`, `verified` is `false`, and `not_verified_reason` is one of
   * `classify_conversion_refusal`'s two values
   * (`code_interpreter_runner.py:57-60`). The transform never ran, so this is a
   * non-verification, not a failure.
   *
   * These fixtures are transcribed from that producer branch, and the assertions
   * are on TEXT rather than on an indicator colour: `LGR-99050e6-1` (template)
   * is the house pattern — colour alone fails WCAG 1.4.1, so a recoloured badge
   * would leave the state invisible.
   */
  describe('a refusal is reported as a non-verification, not a failure (2.20, 2.23)', () => {
    const PERMANENT_DETAIL =
      "NOT VERIFIED by the pandas proxy: construct 'groupBy' on line 7 was not converted, so the transform " +
      'never ran. Reason (permanently_inexpressible): no single-frame pandas equivalent exists, so the proxy ' +
      'will never express it — this is not a defect in the generated PySpark and there is nothing to fix.';
    const NOT_YET_DETAIL =
      "NOT VERIFIED by the pandas proxy: construct 'withColumn' on line 5 was not converted, so the transform " +
      'never ran. Reason (not_yet_expressible): the converter cannot yet express this construct — a defect in ' +
      'the checker, not in the generated PySpark.';

    /** One not-verified check as `aggregate_check_results` writes it (`:308-318`). */
    function notVerifiedCheck(reason: string, detail: string) {
      return {
        name: 'not_verified_by_proxy',
        passed: false,
        detail,
        verified: false,
        not_verified_reason: reason,
      };
    }

    function makeNotVerifiedApproval(checks: Array<Record<string, unknown>>): Approval {
      return makeApproval({
        approval_type: 'test_failure',
        details: {
          test_result: {
            status: 'passed',
            checks,
            report_s3_uri: 's3://test-bucket/tests/exec-2/test_report.md',
          },
          test_status: 'passed',
          failed_checks: ['not_verified_by_proxy'],
          report_s3_uri: 's3://test-bucket/tests/exec-2/test_report.md',
        },
      });
    }

    /** The Status cell of the row whose Check cell names `name`. */
    function statusLabelFor(name: string): string {
      const rows = screen.getByTestId('approval-artifact-test-report').querySelectorAll('tbody tr');
      const row = Array.from(rows).find((r) => (r.querySelectorAll('td')[0]?.textContent ?? '') === name);
      expect(row, `no row rendered for check ${name}`).toBeTruthy();
      return row!.querySelectorAll('td')[1]?.textContent ?? '';
    }

    it('labels a not-verified check as neither passed nor failed, and names the construct and line', () => {
      render(
        <ApprovalArtifact
          approval={makeNotVerifiedApproval([
            { name: 'ai_review_verdict', passed: true, detail: REVIEW_DETAIL },
            notVerifiedCheck('not_yet_expressible', NOT_YET_DETAIL),
          ])}
        />,
      );

      const label = statusLabelFor('not_verified_by_proxy');
      // The deliverable is the TEXT (`LGR-99050e6-1`): distinct from both of the
      // two states the reader used to know, and not the literal 'unknown' the
      // old fall-through produced.
      // `not.toBe('failed')` is the assertion that was red before task 6b: the
      // producer writes `passed: false` on a refusal, so the two-state reader
      // matched its `passed === false` branch and labelled it "failed" — it did
      // not fall through to 'unknown'.
      expect(label).not.toBe('passed');
      expect(label).not.toBe('failed');
      expect(label).not.toBe('unknown');
      expect(label.toLowerCase()).toContain('not verified');
      // A check that DID verify something is unaffected — the third state is
      // additive, not a relabelling of the other two.
      expect(statusLabelFor('ai_review_verdict')).toBe('passed');
      // The construct and the line reach the screen, exactly as 3.7 requires of
      // the refusal itself (2.20).
      const table = screen.getByTestId('approval-artifact-test-report');
      expect(table.textContent).toContain("construct 'withColumn' on line 5");
    });

    it('keeps 2.23\u2019s two reasons distinguishable on screen', () => {
      render(
        <ApprovalArtifact
          approval={makeNotVerifiedApproval([
            {
              ...notVerifiedCheck('permanently_inexpressible', PERMANENT_DETAIL),
              name: 'not_verified_by_proxy:permanent',
            },
            {
              ...notVerifiedCheck('not_yet_expressible', NOT_YET_DETAIL),
              name: 'not_verified_by_proxy:not_yet',
            },
          ])}
        />,
      );

      const permanent = statusLabelFor('not_verified_by_proxy:permanent');
      const notYet = statusLabelFor('not_verified_by_proxy:not_yet');
      // Both read as not-verified...
      expect(permanent.toLowerCase()).toContain('not verified');
      expect(notYet.toLowerCase()).toContain('not verified');
      // ...but a reviewer can tell which one is theirs to fix. A single shared
      // label here would re-create the ambiguity the classification removed.
      expect(permanent).not.toBe(notYet);
      expect(permanent.toLowerCase()).toContain('nothing to fix');
      expect(notYet.toLowerCase()).toContain('defect');
      // And the reasons are legible in prose too, not only in the label.
      const table = screen.getByTestId('approval-artifact-test-report');
      expect(table.textContent).toContain('there is nothing to fix');
      expect(table.textContent).toContain('cannot yet express this construct');
    });

    it('does not assert a failure when every non-passing check is a refusal (2.21)', () => {
      render(
        <ApprovalArtifact
          approval={makeNotVerifiedApproval([
            { name: 'ai_review_verdict', passed: true, detail: REVIEW_DETAIL },
            notVerifiedCheck('permanently_inexpressible', PERMANENT_DETAIL),
          ])}
        />,
      );

      // The banner still exists and still states the consequence of approving
      // (`LGR-78f241b-1`), but it no longer claims an execution failed.
      const alert = screen.getByTestId('approval-artifact-test-failure-alert');
      expect(alert.textContent).not.toContain('Code testing failed');
      expect(alert.textContent).toContain('did not verify');
      expect(alert.textContent).toContain('never ran');
    });

    it('still leads with the failure banner when a real check failed alongside a refusal', () => {
      render(
        <ApprovalArtifact
          approval={makeNotVerifiedApproval([
            {
              name: 'static_analysis:correctness',
              passed: false,
              detail: "Static analysis category 'correctness': FAILED",
            },
            notVerifiedCheck('not_yet_expressible', NOT_YET_DETAIL),
          ])}
        />,
      );

      // A refusal does not soften a genuine failure (3.22's posture).
      expect(screen.getByTestId('approval-artifact-test-failure-alert').textContent).toContain(
        'Code testing failed',
      );
      expect(statusLabelFor('static_analysis:correctness')).toBe('failed');
      expect(statusLabelFor('not_verified_by_proxy').toLowerCase()).toContain('not verified');
    });
  });

  it('still shows the failure alert (no crash) when the report/checks are absent', () => {
    const approval = makeApproval({ approval_type: 'test_failure', details: {} });

    render(<ApprovalArtifact approval={approval} />);

    expect(screen.getByTestId('approval-artifact-test-failure')).toBeInTheDocument();
    // Failure banner is still present even with no per-check details.
    expect(screen.getByTestId('approval-artifact-test-failure-alert').textContent).toContain(
      'Code testing failed',
    );
    // Neutral note replaces the table; no reason string is rendered.
    const empty = screen.getByTestId('approval-artifact-empty');
    expect(empty.textContent).toContain('No per-check details were recorded');
    expect(screen.queryByTestId('approval-artifact-test-report')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-test-failure-reason')).not.toBeInTheDocument();
  });
});

describe('ApprovalArtifact — unknown/absent type is safe', () => {
  // p30 R14.5 changed this branch: it used to render nothing, which read as
  // "nothing to approve" above live approve/reject buttons. It now states that
  // the artifact could not be rendered. The full R14.5 assertions live in the
  // p30 task 2.8 component tests; these two only keep the pre-existing
  // no-crash/no-wrong-artifact guarantee honest against the new output.
  it('does not render another type\'s artifact for an unknown approval type, and does not crash', () => {
    const approval = makeApproval({ approval_type: 'first_run', details: {} });

    const { container } = render(<ApprovalArtifact approval={approval} />);

    expect(screen.queryByTestId('approval-artifact-code')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-mapping')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-test-failure')).not.toBeInTheDocument();
    expect(container.textContent).not.toBe('');
  });

  it('does not crash when the approval type is absent', () => {
    const approval = makeApproval({ approval_type: undefined as unknown as string, details: {} });

    const { container } = render(<ApprovalArtifact approval={approval} />);

    expect(container.textContent).not.toBe('');
  });
});
