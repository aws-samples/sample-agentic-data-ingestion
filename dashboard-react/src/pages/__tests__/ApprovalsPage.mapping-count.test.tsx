// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p50 task 4.2 — the mapping count in `ApprovalsPage`'s review modal.
 *
 * Why this file exists: codegraph reports **no covering tests** for
 * `ApprovalTypeDetails` (`ApprovalsPage.tsx:26`), and this is the site that was
 * wrong. It read `(details.mapping_count as number) ?? 0`, so an approval whose
 * payload could not be read rendered a confident `Mapping count 0` — the
 * collapse `LGR-4854b3d-1` (**template / ratified**) refused for `field_count`
 * ("every hand-written `?? 0` collapses null into 'declares nothing'") and the
 * wrong answer `LGR-9a35b2d-2` (**template / ratified**) measured on the sibling
 * listing surface: `mapping_count: 0` for a pipeline whose `schema_mapping` row
 * carried 7.
 *
 * The sibling render in `ApprovalWorkflowSection` was already correct, and
 * `ApprovalWorkflowSection.actor-identity.characterization.test.tsx` pins it
 * (its header says so in as many words, and hands this site over by name). So
 * the two sites had diverged on exactly the distinction R1.5 draws. This file is
 * the other half of the pair: the same three states, asserted here, so neither
 * surface can collapse them again.
 *
 * Expectations come from R1.4/R1.5 and from the producer's derivation at
 * `approval_handler/lambda_function.py:343`, never from this site's previous
 * output — that output was the defect.
 *
 * Requirements: R1.4, R1.5 — Design: C2, CP2
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
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

// Same mock surface as `ApprovalsPage.artifact.test.tsx`: `ApprovalArtifact`
// pulls `useCodeVersion` and `useBedrockModels` in at module scope, so both must
// exist even for a branch that calls neither.
vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  // p56 — AMENDED, and the assertions below are untouched. The review modal now
  // reads `details` from `GET /api/approvals/{approval_id}` instead of from the
  // listing row, because neither listing handler parses `approval_data`. So the
  // fixture arrives through this hook rather than on the row; it resolves the
  // same approval the listing holds and marks it `parsed`. What these tests
  // assert — the three states of the mapping count — is unchanged, and this
  // file's own header note that the arm was "exercised by a fixture supplying
  // `details` directly" is now describing the transport, not a stand-in for
  // unreachable code.
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
  // Placeholders: these two sentences render only for a non-`parsed` status,
  // which no fixture here produces. Their real wording is asserted in
  // `ApprovalsPage.detail-fetch.test.tsx`, which owns those states.
  APPROVAL_DETAILS_ABSENT_STATEMENT: 'absent-placeholder',
  APPROVAL_DETAILS_UNREADABLE_STATEMENT: 'unreadable-placeholder',
}));

import ApprovalsPage from '../ApprovalsPage';

/** The em dash the surface renders for a count that was never established. */
const NOT_ESTABLISHED = '\u2014';

function schemaMappingApproval(details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-mapping-1',
    pipeline_key: 'pipe-1',
    run_id: 'run-2026-08-24-13-48-26-4fccdf03',
    approval_type: 'schema_mapping',
    // The Review button — the only way into this modal — renders for pending
    // rows only (`ApprovalsPage.tsx:216-228`).
    status: 'pending',
    created_at: '2026-08-24T13:48:26Z',
    details,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ApprovalsPage />
      </ToastProvider>
    </MemoryRouter>,
  );
}

/** Opens the review modal and returns the rendered mapping count, verbatim. */
function openReviewAndReadCount(details: Record<string, unknown>): string {
  mockApprovalsQuery.data = {
    pages: [{ approvals: [schemaMappingApproval(details)], hasMore: false }],
    pageParams: [undefined],
  };
  renderPage();
  fireEvent.click(screen.getByText('Review'));
  return screen.getByTestId('approvals-mapping-count').textContent ?? '';
}

beforeEach(() => {
  mockApprovalsQuery = {
    data: { pages: [{ approvals: [], hasMore: false }], pageParams: [undefined] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
});

describe('ApprovalsPage mapping count — an established count is rendered (R1.4)', () => {
  it('renders a positive count', () => {
    expect(openReviewAndReadCount({ mapping_count: 7 })).toBe('7');
  });

  it('renders a real zero as 0 — a payload was read and it carries no mappings', () => {
    // R1.5's other direction, and the reason `?? 0` could not simply be replaced
    // by a truthiness check: a readable zero must still read as zero.
    expect(openReviewAndReadCount({ mapping_count: 0 })).toBe('0');
  });
});

describe('ApprovalsPage mapping count — not established is not zero (R1.5, do-NOT 7)', () => {
  it('renders not-established for the null the producer sends for an unreadable payload', () => {
    const rendered = openReviewAndReadCount({ mapping_count: null });
    expect(rendered).toBe(NOT_ESTABLISHED);
    expect(rendered).not.toBe('0');
  });

  it('renders not-established when the key is absent entirely', () => {
    // The reported record's shape: `mapping_count` was not a key at all, and
    // eight of the nine keys that were present were `null` (`LGR-86698d4-1`).
    const rendered = openReviewAndReadCount({
      generated_by: 'us.anthropic.claude-sonnet-4-6',
    });
    expect(rendered).toBe(NOT_ESTABLISHED);
    expect(rendered).not.toBe('0');
  });

  it('distinguishes an unreadable payload from an empty mapping (CP2)', () => {
    // The whole requirement in one assertion: these two must not render alike.
    // Two mounts, because the modal holds one approval at a time.
    const unreadable = openReviewAndReadCount({ mapping_count: null });
    cleanup();
    const empty = openReviewAndReadCount({ mapping_count: 0 });

    expect(unreadable).not.toBe(empty);
  });
});
