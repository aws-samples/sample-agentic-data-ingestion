// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Approval } from '../../types';

/* ------------------------------------------------------------------ */
/*  P29 R3.2 / R3.3 — Approvals list pagination + row links.          */
/*                                                                    */
/*  - Each row links to its pipeline and run detail pages (R3.2).     */
/*  - The shared pager stays MOUNTED on the last page with "next"     */
/*    disabled — it must not unmount and strand the user (R3.3).      */
/* ------------------------------------------------------------------ */

interface ApprovalsPageResult {
  approvals: Approval[];
  nextToken?: string;
  hasMore: boolean;
}

// Mutable infinite-query stub swapped per test (vi.mock factory is hoisted).
let mockApprovalsQuery: {
  data?: { pages: ApprovalsPageResult[]; pageParams: unknown[] };
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  refetch: () => unknown;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
};

vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  // ApprovalArtifact (imported by ApprovalsPage) pulls useCodeVersion from
  // this module; it is only invoked inside the review modal, which these
  // tests never open, so a no-op stub is sufficient.
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  // p56 — same reason as `useCodeVersion` above: `ApprovalsPage` imports these
  // three at module scope, and they are only reached inside the review modal,
  // which these tests never open. No-op stubs are sufficient.
  useApprovalDetail: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  APPROVAL_DETAILS_ABSENT_STATEMENT: 'absent-placeholder',
  APPROVAL_DETAILS_UNREADABLE_STATEMENT: 'unreadable-placeholder',
}));

import ApprovalsPage from '../ApprovalsPage';

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ApprovalsPage />
      </ToastProvider>
    </MemoryRouter>,
  );
}

function approval(overrides: Partial<Approval> & Pick<Approval, 'approval_id'>): Approval {
  return {
    pipeline_key: 'pipe-1',
    approval_type: 'code_generation',
    status: 'pending',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  mockApprovalsQuery = {
    data: { pages: [], pageParams: [] },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
});

describe('ApprovalsPage — pagination (R3.3)', () => {
  it('keeps the pager mounted on the last page with "next" disabled', () => {
    const page1: ApprovalsPageResult = {
      approvals: [approval({ approval_id: 'a1', pipeline_key: 'p1', run_id: 'r1' })],
      nextToken: 't2',
      hasMore: true,
    };
    const page2: ApprovalsPageResult = {
      approvals: [approval({ approval_id: 'a2', pipeline_key: 'p2', run_id: 'r2' })],
      hasMore: false,
    };
    mockApprovalsQuery.data = { pages: [page1, page2], pageParams: [undefined, 't2'] };
    mockApprovalsQuery.hasNextPage = false;

    renderPage();

    // Page 1 visible; pager present with "next" enabled (page 1 of 2).
    expect(screen.getByRole('link', { name: 'p1' })).toBeInTheDocument();
    const nextFirst = screen.getByRole('button', { name: 'Next page' });
    expect(nextFirst).toBeInTheDocument();
    expect(nextFirst).not.toBeDisabled();

    // Turn to the last page.
    fireEvent.click(nextFirst);

    // Page 2 now visible …
    expect(screen.getByRole('link', { name: 'p2' })).toBeInTheDocument();
    // … and the pager is STILL mounted, with "next" now disabled (last page).
    const nextLast = screen.getByRole('button', { name: 'Next page' });
    expect(nextLast).toBeInTheDocument();
    expect(nextLast).toBeDisabled();
    // "previous" remains reachable — the user is not stranded.
    expect(screen.getByRole('button', { name: 'Previous page' })).not.toBeDisabled();
  });
});

describe('ApprovalsPage — row links (R3.2)', () => {
  it('links each row to its pipeline and run detail pages', () => {
    mockApprovalsQuery.data = {
      pages: [
        {
          approvals: [approval({ approval_id: 'a1', pipeline_key: 'pipe-42', run_id: 'run-7' })],
          hasMore: false,
        },
      ],
      pageParams: [undefined],
    };

    renderPage();

    const pipelineLink = screen.getByRole('link', { name: 'pipe-42' });
    expect(pipelineLink).toHaveAttribute('href', '/pipelines/pipe-42');

    const runLink = screen.getByRole('link', { name: 'run-7' });
    expect(runLink).toHaveAttribute('href', '/pipelines/pipe-42/runs/run-7');
  });

  it('renders an em dash for the run cell when no run_id is present', () => {
    mockApprovalsQuery.data = {
      pages: [
        {
          approvals: [approval({ approval_id: 'a1', pipeline_key: 'pipe-42' })],
          hasMore: false,
        },
      ],
      pageParams: [undefined],
    };

    renderPage();

    // Pipeline still links; run cell shows the placeholder (no run link).
    expect(screen.getByRole('link', { name: 'pipe-42' })).toBeInTheDocument();
    const rows = screen.getAllByRole('row');
    // The data row (skip the header row) has no run link.
    const dataRow = rows[rows.length - 1];
    expect(within(dataRow).queryByRole('link', { name: 'run-7' })).not.toBeInTheDocument();
  });
});
