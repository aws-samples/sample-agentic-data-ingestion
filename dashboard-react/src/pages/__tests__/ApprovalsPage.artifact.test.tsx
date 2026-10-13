// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * P32 task 3.2 / bugfix.md 3.14 — `ApprovalArtifact`'s third call site.
 *
 * `ApprovalArtifact` is rendered from three places: `ApprovalWorkflowSection.tsx:251`
 * (pending card), `:437` (completed card) and `ApprovalsPage.tsx:357` (the review
 * modal). The first two are exercised in
 * `src/components/__tests__/ApprovalWorkflowSection.test.tsx`. This file covers
 * the third, which no existing suite reached — `ApprovalsPage.pagination.test.tsx:36-38`
 * records that its `useCodeVersion` stub is a no-op precisely because "the review
 * modal … these tests never open".
 *
 * That mattered for the Defect 1 fix: 3.14 states the fix has blast radius beyond
 * the run-detail page, and the approvals list is the other page it lands on. The
 * fixture is transcribed from the PRODUCER, not the reader —
 * `details.test_result.checks` from `approval_handler/lambda_function.py:248-267`,
 * per-check `detail` from `test_code/lambda_function.py:234`, `:244`, `:253`,
 * `:261`, `:273` — so it fails if `TestFailureArtifact` reverts to reading
 * `details.report` / `check.message` (`LGR-28b4d75-5`: a fixture built from the
 * reader's shape has the reader as its own oracle).
 *
 * Validates: Requirements 3.14
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Approval } from '../../types';

interface ApprovalsPageResult {
  approvals: Approval[];
  nextToken?: string;
  hasMore: boolean;
}

let mockApprovalsQuery: {
  data?: { pages: ApprovalsPageResult[]; pageParams: unknown[] };
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
};

// `ApprovalArtifact` imports `useCodeVersion` (via CodeViewer) and
// `useBedrockModels` (via MappingViewer) from this module at module scope, so
// both must exist on the mock even though the `test_failure` branch calls
// neither.
vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  // p56 — AMENDED, assertions untouched. `ApprovalArtifact` is now rendered by
  // `ApprovalReviewBody`, which fetches the payload for the one approval being
  // reviewed, so the fixture arrives through this hook rather than on the listing
  // row. `ApprovalArtifact` itself is unchanged and still reads
  // `approval.details` exactly as it does on the run-detail surface.
  useApprovalDetail: (id: string | undefined) => ({
    data: id
      ? {
          ...mockApprovalsQuery.data?.pages.flatMap((p) => p.approvals).find((a) => a.approval_id === id),
          details_status: 'parsed' as const,
        }
      : undefined,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
  // Placeholders — rendered only for a non-`parsed` status, which no fixture here
  // produces. Real wording asserted in `ApprovalsPage.detail-fetch.test.tsx`.
  APPROVAL_DETAILS_ABSENT_STATEMENT: 'absent-placeholder',
  APPROVAL_DETAILS_UNREADABLE_STATEMENT: 'unreadable-placeholder',
}));

import ApprovalsPage from '../ApprovalsPage';

const STATIC_DETAIL = "Static analysis category 'correctness': PASS";
const EXECUTION_DETAIL =
  "Execution failed: PySpark-to-pandas conversion failed: unconvertible PySpark construct 'withColumn' on line 5";

const testFailureApproval: Approval = {
  approval_id: 'appr-tf-page-1',
  pipeline_key: 'pipe-1',
  run_id: 'run-1',
  approval_type: 'test_failure',
  // The Review button only renders for pending rows (`ApprovalsPage.tsx:216-228`).
  status: 'pending',
  created_at: '2024-06-01T10:05:00Z',
  details: {
    test_result: {
      status: 'failed',
      checks: [
        { name: 'static_analysis:correctness', passed: true, detail: STATIC_DETAIL },
        { name: 'code_interpreter_execution', passed: false, detail: EXECUTION_DETAIL },
      ],
      report_s3_uri: 's3://test-bucket/tests/exec-1/test_report.md',
    },
    test_status: 'failed',
    failed_checks: ['code_interpreter_execution'],
  },
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ApprovalsPage />
      </ToastProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockApprovalsQuery = {
    data: { pages: [{ approvals: [testFailureApproval], hasMore: false }], pageParams: [undefined] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
});

describe('ApprovalsPage review modal — test_failure artifact (3.14)', () => {
  it('renders the per-check table from details.test_result with each check\'s detail', () => {
    renderPage();

    // The artifact only mounts once the review modal is open.
    expect(screen.queryByTestId('approval-artifact-test-failure')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));

    const artifact = screen.getByTestId('approval-artifact-test-failure');
    expect(within(artifact).getByTestId('approval-artifact-test-failure-alert').textContent).toContain(
      'Code testing failed',
    );

    // One row per check (2.1), Details cell carrying that check's `detail` (2.2).
    const table = within(artifact).getByTestId('approval-artifact-test-report');
    const rows = table.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(2);
    const detailsCell = (row: Element) => row.querySelectorAll('td')[2]?.textContent ?? '';
    expect(detailsCell(rows[0])).toContain(STATIC_DETAIL);
    expect(detailsCell(rows[1])).toContain(EXECUTION_DETAIL);

    // The Defect 1 symptom — the no-checks note standing in for the table.
    expect(within(artifact).queryByTestId('approval-artifact-empty')).not.toBeInTheDocument();
  });

  it('keeps the neutral no-checks note (no crash) when the approval carries no checks (3.13)', () => {
    mockApprovalsQuery.data = {
      pages: [{ approvals: [{ ...testFailureApproval, details: {} }], hasMore: false }],
      pageParams: [undefined],
    };

    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));

    const artifact = screen.getByTestId('approval-artifact-test-failure');
    expect(within(artifact).getByTestId('approval-artifact-test-failure-alert').textContent).toContain(
      'Code testing failed',
    );
    expect(within(artifact).getByTestId('approval-artifact-empty').textContent).toContain(
      'No per-check details were recorded',
    );
    expect(within(artifact).queryByTestId('approval-artifact-test-report')).not.toBeInTheDocument();
  });
});
