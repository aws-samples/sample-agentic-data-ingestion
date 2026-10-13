// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p50 task 5.3 — the render defects this spec's own payload corrections
 * introduced, in `ApprovalsPage`'s review modal.
 *
 * Why this file exists rather than more cases in
 * `ApprovalsPage.mapping-count.test.tsx`: that file drives the same real modal,
 * but every fixture in it omits `unmapped_fields` and `quality_checks`, so it
 * could not have caught either defect. Nothing else in the tree could either —
 * these are the first tests to hand this surface a payload of the shape the
 * producer now actually sends.
 *
 * ## Both defects, measured before the fix
 *
 * **(a)** `ApprovalsPage.tsx:42` read `(details.unmapped_fields as number) ?? 0`.
 * The producer writes a dict (`schema_mapping/lambda_function.py:1740-1744`), so
 * once task 2.1 pointed `get_approval_data` at `output_data.approval` the key
 * arrived populated and clicking Review threw:
 *
 *     Objects are not valid as a React child (found: object with keys
 *     {source, target, target_unsourced, target_required})
 *
 * The **entire modal** failed, including the mapping count task 4.2 had just
 * corrected — which is why the first test below asserts that count is reachable
 * at all rather than asserting the new cell. Before 2.1 the key was `null` and
 * `?? 0` rendered a false `0` instead of crashing.
 *
 * **(b)** `ApprovalsPage.tsx:100` read `(details.quality_checks as string) ?? ''`.
 * Task 5.1 pointed that read at `pre_run_validation.quality_checks`
 * (`create_quality_checks/lambda_function.py:307-312`), a four-element list, and
 * React concatenates a string array with no separators. Measured:
 *
 *     "5 quality rules configuredData type validations enabledNull value checks
 *      configuredCompleteness validation active"
 *
 * Expectations here come from those two producers and from the ratified
 * unsourced-target vocabulary (`LGR-9e76249-7`, `LGR-7561acb-1`,
 * `LGR-7561acb-4`), never from what this surface used to render.
 *
 * Requirements: none of its own — a regression under R1.4 / R4.1's change set.
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

// Same mock surface as `ApprovalsPage.mapping-count.test.tsx`: `ApprovalArtifact`
// pulls `useCodeVersion` and `useBedrockModels` in at module scope, so both must
// exist even for a branch that calls neither. An export this factory omits is
// `undefined`, and the component then calls `undefined()` — which surfaces at
// **render** time, inside the test that renders the component, as a stack trace
// naming a value that is not a function. Amend this factory first when a new
// hook is imported by anything on this path.
vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  // p56 — AMENDED, assertions untouched. The review modal reads `details` from
  // `GET /api/approvals/{approval_id}` now, not from the listing row, so the
  // fixture arrives through this hook. It resolves the same approval the listing
  // holds and marks it `parsed`. Both defects this file pins — the dict rendered
  // as a React child, and the string array concatenated with no separators — are
  // properties of the payload's SHAPE, which is unchanged.
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

/** The em dash both cells render for a fact that was never established. */
const NOT_ESTABLISHED = '\u2014';

/**
 * `create_approval_payload`'s `unmapped_fields`, verbatim from
 * `schema_mapping/lambda_function.py:1740-1744`, on the reported run's shape:
 * `mobile_phone` in both rosters, the normaliser's own `reason` constant, and a
 * recorded `null` default.
 */
const PRODUCER_UNMAPPED_FIELDS = {
  source: ['legacy_notes'],
  target: [
    { field: 'mobile_phone', default_value: null, reason: 'No source field in generated mapping' },
  ],
  target_unsourced: ['mobile_phone'],
  target_required: ['mobile_phone'],
};

/**
 * `pre_run_validation.quality_checks`, verbatim from
 * `create_quality_checks/lambda_function.py:307-312`.
 *
 * Only the **first** element is derived from anything. The other three are
 * unconditional string literals in the producer, which is why no assertion below
 * treats them as measurements — task 5.1 recorded that as a producer-side defect
 * and deliberately left it, and pinning it as intended would be worse than
 * leaving it recorded.
 */
const PRODUCER_QUALITY_CHECKS = [
  '5 quality rules configured',
  'Data type validations enabled',
  'Null value checks configured',
  'Completeness validation active',
];

function approvalOfType(approval_type: string, details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-5-3',
    pipeline_key: 'pipe-1',
    run_id: 'run-2026-08-24-13-48-26-4fccdf03',
    approval_type,
    // The Review button — the only way into this modal — renders for pending
    // rows only (`ApprovalsPage.tsx:216-228`).
    status: 'pending',
    created_at: '2026-08-24T13:48:26Z',
    details,
  };
}

/** Mounts the page with one approval and opens its review modal. */
function openReview(approval_type: string, details: Record<string, unknown>) {
  mockApprovalsQuery.data = {
    pages: [{ approvals: [approvalOfType(approval_type, details)], hasMore: false }],
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

describe('(a) the review modal survives the producer unmapped_fields dict', () => {
  it('renders the modal at all — the crash took the whole thing down', () => {
    // The regression in one assertion. Pre-fix this throws before any of it
    // mounts, so the count task 4.2 corrected was unreachable on exactly the
    // payloads task 2.1 had just made correct.
    openReview('schema_mapping', {
      mapping_count: 1,
      unmapped_fields: PRODUCER_UNMAPPED_FIELDS,
    });

    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe('1');
  });

  it('names the unsourced target field and what the schema demands of it', () => {
    openReview('schema_mapping', {
      mapping_count: 1,
      unmapped_fields: PRODUCER_UNMAPPED_FIELDS,
    });

    const cell = screen.getByTestId('approvals-unsourced-target-fields');
    expect(cell.textContent).toBe('mobile_phone (Required)');
    // The pre-fix sibling site's output, which must not appear on either surface.
    expect(cell.textContent).not.toContain('[object Object]');
  });

  it('says Optional for a roster name the target schema does not require', () => {
    openReview('schema_mapping', {
      unmapped_fields: {
        source: [],
        target: ['mobile_phone', 'nickname'],
        target_unsourced: ['mobile_phone', 'nickname'],
        target_required: ['mobile_phone'],
      },
    });

    const rendered = screen.getByTestId('approvals-unsourced-target-fields').textContent ?? '';
    expect(rendered).toContain('mobile_phone (Required)');
    expect(rendered).toContain('nickname (Optional)');
  });

  it('says Not established for a payload written before the Unsourced_Keys', () => {
    // The common case today, not an edge: p37 is not deployed, so no payload any
    // account produces carries `target_unsourced` / `target_required`. Nothing
    // may read as "Optional" here (p45 do-NOT 5).
    openReview('schema_mapping', {
      unmapped_fields: { source: [], target: ['mobile_phone'] },
    });

    expect(screen.getByTestId('approvals-unsourced-target-fields').textContent).toBe(
      'mobile_phone (Not established)',
    );
  });
});

describe('(a) an unread payload is not an empty roster', () => {
  it('renders not-established for the null every pre-task-2.1 row carries', () => {
    openReview('schema_mapping', { unmapped_fields: null });

    expect(screen.getByTestId('approvals-unsourced-target-fields').textContent).toBe(
      NOT_ESTABLISHED,
    );
  });

  it('renders None for a read payload whose roster is empty', () => {
    // A positive fact — every target field has a source — and it must not read
    // like a payload that established nothing. Same distinction do-NOT 7 draws
    // for `mapping_count`, one column over.
    openReview('schema_mapping', {
      unmapped_fields: { source: [], target: [], target_unsourced: [], target_required: [] },
    });

    const rendered = screen.getByTestId('approvals-unsourced-target-fields').textContent;
    expect(rendered).toBe('None');
    expect(rendered).not.toBe(NOT_ESTABLISHED);
  });
});

describe('(a) confidence score states what was measured, or nothing', () => {
  it('renders not-established when no confidence was recorded', () => {
    // The state of every real payload: `get_approval_data` assembles
    // `confidence_scores` (plural, per-field) and no singular `confidence_score`,
    // so `?? 0` claimed a measured `0%` on every approval ever shown here.
    openReview('schema_mapping', { unmapped_fields: PRODUCER_UNMAPPED_FIELDS });

    const rendered = screen.getByTestId('approvals-confidence-score').textContent;
    expect(rendered).toBe(NOT_ESTABLISHED);
    expect(rendered).not.toBe('0%');
  });

  it('renders a recorded confidence as a percentage', () => {
    openReview('schema_mapping', { confidence_score: 0.96 });

    expect(screen.getByTestId('approvals-confidence-score').textContent).toBe('96%');
  });

  it('renders a recorded zero as 0% — the one value a truthiness check swallows', () => {
    openReview('schema_mapping', { confidence_score: 0 });

    expect(screen.getByTestId('approvals-confidence-score').textContent).toBe('0%');
  });
});

describe('(b) first_run quality checks render as the list they are', () => {
  it('renders one item per line, separated', () => {
    openReview('first_run', { quality_checks: PRODUCER_QUALITY_CHECKS });

    const items = Array.from(
      screen.getByTestId('approvals-quality-checks').querySelectorAll('li'),
    ).map((li) => li.textContent);

    expect(items).toEqual(PRODUCER_QUALITY_CHECKS);
  });

  it('gives every line its own element, so none is fused to its neighbour', () => {
    // Asserted through the DOM rather than through text, and the distinction is
    // the point: `textContent` concatenates across element boundaries, so it
    // reads identically for a correct list and for the pre-fix render. It cannot
    // tell them apart and a test written against it would pass on the defect.
    //
    // An exact-text query can: pre-fix all four lines were adjacent text nodes in
    // one `<div>`, whose own normalised text is the fused string
    // `"5 quality rules configuredData type validations enabled…"`, so no element
    // matched any single line and every one of these throws.
    openReview('first_run', { quality_checks: PRODUCER_QUALITY_CHECKS });

    for (const line of PRODUCER_QUALITY_CHECKS) {
      expect(screen.getByText(line).tagName).toBe('LI');
    }
  });

  it('renders nothing for a payload written before task 5.1', () => {
    // `quality_checks` was read off the row's top level, where nothing writes it,
    // so every older row carries `null`. Saying nothing is correct; the surface
    // must not invent a line.
    openReview('first_run', { quality_checks: null, infrastructure_status: null });

    expect(screen.queryByTestId('approvals-quality-checks')).toBeNull();
  });

  it('ignores a non-string element rather than stringifying it as a check', () => {
    openReview('first_run', { quality_checks: ['5 quality rules configured', { a: 1 }, null] });

    const items = Array.from(
      screen.getByTestId('approvals-quality-checks').querySelectorAll('li'),
    ).map((li) => li.textContent);

    expect(items).toEqual(['5 quality rules configured']);
  });

  it('does not throw when the key arrives as the string the old cast claimed', () => {
    // No producer sends this, and the surface must degrade rather than render a
    // character-by-character list.
    openReview('first_run', { quality_checks: 'Data type validations enabled' });

    expect(screen.queryByTestId('approvals-quality-checks')).toBeNull();
  });
});
