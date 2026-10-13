// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 4.4 — the reuse and unreadable states in the review modal.
 *
 * This is the surface Approve/Reject is actually clicked from, and — unlike
 * `ApprovalWorkflowSection`, whose `rendersArtifact` gate
 * (`ApprovalWorkflowSection.tsx:150-152`) excludes `schema_mapping` — it renders
 * `ApprovalArtifact` unconditionally (`ApprovalsPage.tsx:532`). So the shared
 * `toMappingData` → `MappingViewer` path is reachable **here only**, which makes
 * this the file where "renders no mapping rows" is a real assertion rather than a
 * vacuous one.
 *
 * That matters because it is the exact collapse task 4.4 exists to prevent:
 * `toMappingData` (`ApprovalArtifact.tsx:124`) yields `mappings: []` for an
 * unreadable payload, so without a sentence the state renders as **a mapping with
 * no rows** — `LGR-b2f79ef-14`'s *named reference to something absent* presented
 * as a thing with no fields, do-NOT 7.
 *
 * **`details` is `undefined` on this surface today.** Neither `get_approvals`
 * (`approvals.py:339`) nor `get_pending_approvals` (`:399`) parses
 * `approval_data`; that parse exists only in `pipelines_runs.py` and
 * `pipelines_crud.py`. So the arm is unreachable from live data and is exercised
 * by a fixture supplying `details` directly — exactly how
 * `ApprovalsPage.mapping-count.test.tsx` already tests this component. Widening
 * those two handlers is neither task 4.4's nor p54's; it is recorded, not fixed.
 *
 * Expectations derive from R1.4, R2.1, R2.2 and from the producer's three states,
 * never from this modal's previous output — before this task it read none of
 * these keys.
 *
 * Requirements: R1.4, R2.1, R2.2
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../contexts/ToastContext';
import {
  REUSED_MAPPING_STATEMENT,
  REUSED_MAPPING_UNREADABLE_STATEMENT,
} from '../../utils/approval-reused-mapping';
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

// Same mock surface as `ApprovalsPage.mapping-count.test.tsx`: `ApprovalArtifact`
// pulls `useCodeVersion` and `useBedrockModels` in at module scope, and
// `MappingViewer` calls the latter, so both must exist.
vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  // p56 — AMENDED, assertions untouched. The review modal reads `details` from
  // `GET /api/approvals/{approval_id}` now, so the fixture arrives through this
  // hook rather than on the listing row. Note the two "unreadable" notions stay
  // separate and both still hold: `mapping_unreadable` is the PRODUCER saying it
  // could not resolve a reused mapping, and it lives INSIDE a payload that parsed
  // — which is why `details_status` is `parsed` here, not `unreadable`.
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

/** The em dash the modal renders for a count that was never established. */
const COUNT_NOT_ESTABLISHED = '\u2014';

/**
 * The producer's **resolved** state. `proposed_mappings` carries the producer's
 * own element shape, `{source_field, target_field}`.
 */
const RESOLVED: Record<string, unknown> = {
  proposed_mappings: [
    { source_field: 'signup_date', target_field: 'registration_date', confidence: 0.96 },
  ],
  mapping_count: 1,
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  reused_from_run_id: 'run-2026-08-01-09-11-02-origin',
};

/** The producer's **unreadable** state — exactly the five keys it emits. */
const UNREADABLE: Record<string, unknown> = {
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  mapping_unreadable: true,
  proposed_mappings: null,
  mapping_count: null,
};

function schemaMappingApproval(details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-reuse-1',
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

/** Opens the review modal for one approval. */
function openReview(details: Record<string, unknown>) {
  mockApprovalsQuery.data = {
    pages: [{ approvals: [schemaMappingApproval(details)], hasMore: false }],
    pageParams: [undefined],
  };
  render(
    <MemoryRouter>
      <ToastProvider>
        <ApprovalsPage />
      </ToastProvider>
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByText('Review'));
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

describe('ApprovalsPage reuse — a resolved reused mapping (R2.1, R2.2)', () => {
  it('states that the mapping was reused and names which one', () => {
    openReview(RESOLVED);

    expect(screen.getByText(REUSED_MAPPING_STATEMENT)).toBeInTheDocument();
    expect(screen.getByTestId('approvals-reused-mapping-id').textContent).toBe('mapping-abc123');
  });

  it('shows the mapping, through the shared viewer this task does not change', () => {
    openReview(RESOLVED);

    // `ApprovalArtifact` → `toMappingData` → `MappingViewer`, above this
    // container in the same modal. The reuse block says the mapping is reused; it
    // does not render the mapping a second time.
    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.getByText('signup_date')).toBeInTheDocument();
    expect(screen.getByText('registration_date')).toBeInTheDocument();
  });

  it('renders the count the payload carries', () => {
    openReview(RESOLVED);

    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe('1');
  });

  it('does not state that the mapping could not be read', () => {
    openReview(RESOLVED);

    expect(screen.queryByTestId('approvals-reused-mapping-unreadable')).toBeNull();
  });
});

describe('ApprovalsPage reuse — an unreadable reused mapping (R1.4, do-NOT 7)', () => {
  it('states that the mapping could not be read', () => {
    openReview(UNREADABLE);

    expect(screen.getByTestId('approvals-reused-mapping-unreadable').textContent).toBe(
      REUSED_MAPPING_UNREADABLE_STATEMENT,
    );
  });

  it('still names which mapping the gate is about', () => {
    openReview(UNREADABLE);

    expect(screen.getByTestId('approvals-reused-mapping-id').textContent).toBe('mapping-abc123');
  });

  it('renders no mapping rows', () => {
    openReview(UNREADABLE);

    // The load-bearing version of this assertion. `MappingViewer` DOES render
    // here — the panel is present — and it has no rows, which is precisely why
    // the sentence above has to be there. Without it this is a mapping with no
    // fields, standing in for a mapping that could not be read.
    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.queryByText('signup_date')).toBeNull();
    expect(screen.queryByText('registration_date')).toBeNull();
  });

  it('renders the count as an em dash rather than 0', () => {
    openReview(UNREADABLE);

    // The do-not-regress half. This site is the one that used to read `?? 0`
    // (`LGR-e59edd7-6`, p50 task 4.2); the reuse arm must not have reintroduced
    // it, and the unreadable payload must not be routed through a zero count.
    const rendered = screen.getByTestId('approvals-mapping-count').textContent;
    expect(rendered).toBe(COUNT_NOT_ESTABLISHED);
    expect(rendered).not.toBe('0');
  });

  it('renders two different sentences across the two arms, not one empty panel', () => {
    openReview(UNREADABLE);
    const unreadableModal = screen.getByTestId('approvals-reused-mapping').textContent ?? '';
    cleanup();

    openReview(RESOLVED);
    const resolvedModal = screen.getByTestId('approvals-reused-mapping').textContent ?? '';

    expect(unreadableModal).not.toBe(resolvedModal);
    expect(unreadableModal).toContain(REUSED_MAPPING_UNREADABLE_STATEMENT);
    expect(resolvedModal).not.toContain(REUSED_MAPPING_UNREADABLE_STATEMENT);
    // Both arms still state the reuse — that fact did not fail.
    expect(unreadableModal).toContain(REUSED_MAPPING_STATEMENT);
    expect(resolvedModal).toContain(REUSED_MAPPING_STATEMENT);
  });
});

describe('ApprovalsPage reuse — task 2.3s third route (do-NOT 7)', () => {
  it('states the mapping could not be read for a resolved payload with a non-list mapping list', () => {
    // Thirteen keys, `mapping_count: null`, NO `mapping_unreadable`. Keyed on the
    // marker alone this renders here as an empty `MappingViewer` and an em dash
    // count beside a payload asserting it is readable — the collapse arriving by
    // a route neither of task 4.4's two named arms covers.
    //
    // `{}` and not `'not-a-list'`: a string is the one non-list shape with a
    // truthy `.length`, and the duplicate proposed-mappings table below
    // (`LGR-e59edd7-7` recorded and deliberately LEFT it, do NOT touch) hands
    // that straight to `Table items=`. The reader classifies the string case too;
    // that is pinned in `utils/__tests__/approval-reused-mapping.test.ts`, where
    // no render is involved.
    openReview({ ...RESOLVED, proposed_mappings: {}, mapping_count: null });

    expect(screen.getByTestId('approvals-reused-mapping-unreadable').textContent).toBe(
      REUSED_MAPPING_UNREADABLE_STATEMENT,
    );
    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe(COUNT_NOT_ESTABLISHED);
    expect(screen.queryByText('signup_date')).toBeNull();
  });
});

describe('ApprovalsPage reuse — absence renders nothing (do-NOT 7)', () => {
  it('renders no reuse block for the empty payload the producer emits for no reuse', () => {
    openReview({});

    expect(screen.queryByTestId('approvals-reused-mapping')).toBeNull();
  });

  it('renders no reuse block for a run that generated its own mapping', () => {
    // Absence means not established. A block saying "not reused" would assert
    // knowledge the payload does not carry.
    openReview({
      proposed_mappings: [{ source_field: 'signup_date', target_field: 'registration_date' }],
      mapping_count: 1,
    });

    expect(screen.queryByTestId('approvals-reused-mapping')).toBeNull();
    // …and the mapping still renders, so the guard did not suppress the panel.
    expect(screen.getByText('signup_date')).toBeInTheDocument();
  });
});

describe('ApprovalsPage reuse — the untouched duplicate table (LGR-e59edd7-7)', () => {
  it('leaves the duplicate proposed-mappings table rendering exactly as it did', () => {
    openReview(RESOLVED);

    // `LGR-e59edd7-7` item (1) recorded that this table's `{source, target}` cast
    // disagrees with the producer's `{source_field, target_field}`, so its Source
    // and Target cells render blank, and deliberately LEFT it — which of the two
    // duplicate tables survives is a display decision needing its own task. This
    // asserts the state it was left in, so task 4.4 cannot be read as having
    // quietly changed it: the row exists, and `signup_date` appears exactly once
    // in the modal, in `MappingViewer` above.
    expect(screen.getAllByText('signup_date')).toHaveLength(1);
    // And `96%` appears TWICE — once in `MappingViewer`'s confidence cell and
    // once in the duplicate table's, which is the recorded defect measured from
    // the other side: the duplicate row exists and carries only its confidence,
    // because the two field cells its cast addresses are not the keys the
    // producer sent. Asserting the count rather than presence is what makes this
    // a pin on the left-alone state rather than a passing coincidence.
    expect(screen.getAllByText('96%')).toHaveLength(2);
  });

  it('renders no duplicate table for the unreadable payload', () => {
    openReview(UNREADABLE);

    // `proposed_mappings: null` → the cast's `?? []` → `length > 0` false → the
    // table is not rendered. That is the "no mapping rows" half on this half of
    // the modal, and it needed no change.
    expect(screen.queryByText('96%')).toBeNull();
  });
});
