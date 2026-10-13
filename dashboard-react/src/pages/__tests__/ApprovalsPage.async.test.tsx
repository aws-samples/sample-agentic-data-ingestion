// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../contexts/ToastContext';

/* ------------------------------------------------------------------ */
/*  P29 R1.4 — Async_State_Triple on ApprovalsPage.                   */
/*                                                                    */
/*  A load failure MUST render an error + Retry, NOT the             */
/*  "No pending approvals." empty-state copy.                        */
/* ------------------------------------------------------------------ */

let mockApprovalsQuery: {
  data?: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => unknown;
};
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  // p56 — `ApprovalsPage` imports these three at module scope, so the mock has to
  // carry them or the import fails. These tests never open the review modal, so
  // none is ever invoked or rendered; the review modal's own Async_State_Triple
  // (a payload read that fails must not look like an approval with no mappings)
  // is `ApprovalsPage.detail-fetch.test.tsx`'s subject, not this file's.
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

beforeEach(() => {
  mockRefetch.mockReset();
});

describe('ApprovalsPage — Async_State_Triple (R1.4)', () => {
  it('renders an error with a Retry button on API failure (not the empty copy)', () => {
    mockApprovalsQuery = { data: [], isLoading: false, isError: true, refetch: mockRefetch };
    renderPage();
    expect(screen.getByText('Failed to load approvals.')).toBeInTheDocument();
    expect(screen.getByTestId('approvals-retry')).toBeInTheDocument();
    expect(screen.queryByText('No pending approvals.')).not.toBeInTheDocument();
  });

  it('invokes refetch when Retry is clicked on the error state', () => {
    mockApprovalsQuery = { data: [], isLoading: false, isError: true, refetch: mockRefetch };
    renderPage();
    fireEvent.click(screen.getByTestId('approvals-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });

  it('renders the "No pending approvals." empty copy only when genuinely empty', () => {
    mockApprovalsQuery = { data: [], isLoading: false, isError: false, refetch: mockRefetch };
    renderPage();
    expect(screen.getByText('No pending approvals.')).toBeInTheDocument();
    expect(screen.queryByTestId('approvals-retry')).not.toBeInTheDocument();
  });
});
