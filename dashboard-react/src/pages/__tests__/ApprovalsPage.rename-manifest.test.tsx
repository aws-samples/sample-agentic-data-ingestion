// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 task 6.2 — the Rename_Manifest on the review modal, the second of the three
 * approval surfaces (R10.1, R10.2).
 *
 * This surface renders **three-state**, unlike the artifact panel above it in the
 * same modal, and the difference is the point of most of the assertions below: the
 * field has a LABEL, and a blank beside a label reads as a measured "no renames
 * proposed". `LGR-4854b3d-1` (**template / ratified**) is the standing ruling for
 * this exact collapse — "every hand-written `?? 0` collapses null into 'declares
 * nothing'" — and these two surfaces have already produced it twice, on
 * `mapping_count` and then on `unmapped_fields`, which is why both expressions now
 * live in one module each. `utils/rename-manifest.ts` is that module for this
 * fact, before the third divergence rather than after it.
 *
 * **Recorded, not fixed here:** `details` is `undefined` on this surface from live
 * data. Neither `get_approvals` nor `get_pending_approvals` parses
 * `approval_data` — the parse exists only in `pipelines_runs.py` and
 * `pipelines_crud.py` — so this modal's detail block is fixture-exercised, exactly
 * as `ApprovalsPage.mapping-count.test.tsx` and
 * `ApprovalsPage.populated-payload.test.tsx` already note for the two fields
 * beside this one. Widening those two handlers is a separate change with its own
 * decision; it is not made here, and the run-detail surfaces
 * (`ApprovalWorkflowSection`, and `ApprovalArtifact` inside it) do receive parsed
 * details today.
 *
 * Fixtures are the producer's shape, from design §Data Models
 * (`LGR-28b4d75-5`: a test written from the consumer's own output cannot detect
 * the producer drifting).
 *
 * **Validates: Requirements 10.1, 10.2**
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

// The same mock surface as the two sibling files: `ApprovalArtifact` pulls
// `useCodeVersion` and `useBedrockModels` in at module scope, so both must exist
// even for a branch that calls neither.
vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  // p56 — AMENDED, assertions untouched. The review modal reads `details` from
  // `GET /api/approvals/{approval_id}` now, so the fixture arrives through this
  // hook rather than on the listing row. This file's header note that the parse
  // "exists only in `pipelines_runs.py` and `pipelines_crud.py`" is still true of
  // the LISTING handlers — neither was widened; a third, keyed reader was added.
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

/** The em dash every cell on this surface renders for a fact never established. */
const NOT_ESTABLISHED = '\u2014';

/** `derive_rename_manifest`'s documented return — design §Data Models. */
const PRODUCER_MANIFEST = {
  manifest_id: 'mapping-hash-p55',
  source_schema_hash: 'sha256:p55',
  approval_id: null,
  renames: [
    { original_name: 'first name', target_name: 'first_name' },
    { original_name: '2024-total', target_name: '_2024_total' },
  ],
};

function approvalWith(details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-p55-6-2',
    pipeline_key: 'pipe-1',
    run_id: 'run-p55',
    approval_type: 'schema_mapping',
    // The Review button — the only way into this modal — renders for pending rows.
    status: 'pending',
    created_at: '2026-08-30T12:00:00Z',
    details,
  };
}

function openReview(details: Record<string, unknown>) {
  mockApprovalsQuery.data = {
    pages: [{ approvals: [approvalWith(details)], hasMore: false }],
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

describe('a payload that was read and proposes renames', () => {
  it('names both halves of every Proposed_Rename (R10.2)', () => {
    openReview({ mapping_count: 2, rename_manifest: PRODUCER_MANIFEST });
    const cell = screen.getByTestId('approvals-proposed-renames');

    expect(cell.textContent).toContain('first name → first_name');
    expect(cell.textContent).toContain('2024-total → _2024_total');
    // The failure mode both sibling fields hit on this surface before p50 fixed
    // them: a dict rendered through `String(...)`.
    expect(cell.textContent).not.toContain('[object Object]');
  });

  it('renders one list item per rename, in the order received', () => {
    openReview({ mapping_count: 2, rename_manifest: PRODUCER_MANIFEST });
    const items = screen.getByTestId('approvals-proposed-renames').querySelectorAll('li');

    expect(Array.from(items, (li) => li.textContent)).toEqual([
      'first name → first_name',
      '2024-total → _2024_total',
    ]);
  });

  it('labels the field, so the list is not a bare pair of names', () => {
    openReview({ mapping_count: 2, rename_manifest: PRODUCER_MANIFEST });

    // `getAllByText`, not `getByText`: the artifact panel is ALSO in this modal
    // and carries the same heading, so the string legitimately appears twice —
    // once as this block's key label and once as the panel's container header.
    // That duplication is the two surfaces agreeing on one name for one fact,
    // which is what sharing `utils/rename-manifest.ts` is for; asserting
    // uniqueness would be asserting the panel is absent.
    expect(screen.getAllByText('Proposed source column renames').length).toBeGreaterThanOrEqual(1);

    // The label really is this block's, read off the DOM rather than by position:
    // the value div's parent carries both.
    const block = screen.getByTestId('approvals-proposed-renames').parentElement!;
    expect(block.textContent).toContain('Proposed source column renames');
  });

  it('leaves the fields beside it working', () => {
    // The whole modal died once on a sibling field of this one, so a new block in
    // the same container is worth checking against that.
    openReview({ mapping_count: 2, rename_manifest: PRODUCER_MANIFEST });
    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe('2');
    expect(screen.getByTestId('approvals-unsourced-target-fields')).toBeInTheDocument();
  });
});

describe('the three states get three distinguishable renderings', () => {
  it('says None when the manifest was read and proposes nothing', () => {
    openReview({ mapping_count: 2, rename_manifest: { renames: [] } });
    expect(screen.getByTestId('approvals-proposed-renames').textContent).toBe('None');
  });

  it('says nothing-established for a present-but-null manifest', () => {
    // The shape `approval_handler._schema_mapping_payload` sends for every mapping
    // row written before p55, for the producer's degraded `except` arm, and for an
    // `output_data.approval` it could not read.
    openReview({ mapping_count: 2, rename_manifest: null });
    expect(screen.getByTestId('approvals-proposed-renames').textContent).toBe(NOT_ESTABLISHED);
  });

  it('says nothing-established when the key is absent entirely', () => {
    openReview({ mapping_count: 2 });
    expect(screen.getByTestId('approvals-proposed-renames').textContent).toBe(NOT_ESTABLISHED);
  });

  it('never renders blank — a blank beside a label reads as a measured None', () => {
    for (const details of [
      { rename_manifest: PRODUCER_MANIFEST },
      { rename_manifest: { renames: [] } },
      { rename_manifest: null },
      {},
    ]) {
      openReview(details);
      expect(screen.getByTestId('approvals-proposed-renames').textContent!.length).toBeGreaterThan(0);
      cleanup();
    }
  });
});

describe('an attacker-controlled original name', () => {
  it('renders markup as text rather than as markup', () => {
    const hostile = '<script>alert(1)</script>';
    openReview({
      rename_manifest: { renames: [{ original_name: hostile, target_name: 'script_alert_1_script_' }] },
    });
    const cell = screen.getByTestId('approvals-proposed-renames');

    expect(cell.textContent).toContain(hostile);
    expect(cell.querySelector('script')).toBeNull();
  });

  it('caps a pathological name so it cannot push the modal apart', () => {
    openReview({
      rename_manifest: { renames: [{ original_name: 'c'.repeat(50_000), target_name: 'ccc' }] },
    });
    const cell = screen.getByTestId('approvals-proposed-renames');

    expect(cell.textContent).toContain('truncated');
    expect(cell.textContent!.length).toBeLessThan(1_000);
  });
});
