// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p56 — the review modal fetches its payload, and says which of three things
 * happened.
 *
 * ## Why this file exists
 *
 * `ApprovalTypeDetails` reads `approval.details ?? {}`, and neither listing
 * handler parsed the stored `approval_data` — so every reader in this modal
 * correctly reported "not established": the mapping count, the unsourced target
 * fields, the confidence, the proposed renames, and `MappingViewer`'s whole panel
 * ("No mappings available", Total Mappings 0, AI Model Unknown). The five sibling
 * `ApprovalsPage.*` files put `details` on the listing row by hand, which is why
 * they passed while the live surface showed nothing; two comments in
 * `ApprovalsPage.tsx` said as much in writing. `GET /api/approvals/{approval_id}`
 * is the payload's transport, and this file is the part none of those five can
 * cover: what the surface does when the fetch is in flight, when it fails, and
 * when it succeeds but the payload was not established.
 *
 * ## The three states, and why the pairs are asserted rather than the values
 *
 * | `details_status` | fact |
 * |---|---|
 * | `'parsed'` | the attribute decoded; `details` carries it, and may be `{}` or all-`null` |
 * | `'absent'` | no attribute; the **producer** stored nothing |
 * | `'unreadable'` | something was stored and could not be interpreted |
 *
 * All three make every field reader answer *not established*, because that is the
 * honest answer for all three — a reader cannot tell them apart and should not
 * have to. So per-state value assertions would pass even if the states were
 * merged, and a **merge is the defect**: `{}` for an unreadable payload reads as
 * "the producer stored nothing" when the truth is "we could not read what it
 * stored". `LGR-4854b3d-1` (**template / ratified**) forbids collapsing a
 * three-state value onto one of its ends, and p54 drew the same line for a single
 * field via `REUSED_MAPPING_UNREADABLE_STATEMENT`, per `LGR-b2f79ef-14`: a named
 * reference to something absent is not a thing with no fields. The load-bearing
 * tests here therefore compare states to each other.
 *
 * ## The fourth input state that is not a fourth output state
 *
 * `LIVE_ALL_NULL_PAYLOAD` is the verbatim `approval_data` of the pending approval
 * this work was reported against — `newtest-ab2a08a0_schema_mapping_1787402782`,
 * created 2026-08-22, read read-only from `agentic_data_ingestion_approvals` in
 * account 777788889999 / us-west-2. It is present, valid JSON, structurally
 * complete, and every substantive value is `null`. That is `'parsed'`: the
 * producer stored a payload and it was read. The nulls are then each reader's
 * business, and they already report them. Pinned here because "all-null" invites
 * being treated as a read failure, and it is not one.
 *
 * ## Async_State_Triple (p29 R1)
 *
 * Loading, error-with-retry and established-nothing are three distinct states,
 * following `TestResultsViewer` and `StatsBar`. The one that matters on THIS
 * surface: **a failed read must not render as an approval with no mappings.** An
 * approver who sees an empty Mapping Preview because a fetch failed would sign off
 * believing they had been shown the truth — `LGR-b2f79ef-14`'s "could not check
 * versus checked and passed", where the cost is a wrong approval.
 *
 * ## Where the expectations come from
 *
 * From the live payload above, from the producer's key names, and from the
 * ratified vocabulary the readers already own (`LGR-4854b3d-1`, `LGR-e59edd7-6`,
 * `LGR-9e76249-7`, `LGR-7561acb-4`). Never from what this surface used to render —
 * what it used to render is the defect.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Approval, ApprovalDetail, ApprovalDetailsStatus } from '../../types';

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

let mockDetailQuery: {
  data?: ApprovalDetail;
  isLoading: boolean;
  isError: boolean;
  refetch: ReturnType<typeof vi.fn>;
};

/** Every id the hook was called with, so the keying can be asserted. */
let detailQueryCalls: Array<string | undefined>;

/**
 * The real sentences, so this file asserts wording rather than a placeholder.
 *
 * `vi.hoisted` because the `vi.mock` factory below is hoisted above these
 * declarations, and a plain `const` would be in its temporal dead zone when the
 * factory runs (`ReferenceError: Cannot access 'ABSENT_STATEMENT' before
 * initialization`). The `let` mock-state variables are fine unhoisted — the
 * factory only reads them when the functions it returns are *invoked*, at render
 * time — but these two are read by the factory body itself.
 */
const { ABSENT_STATEMENT, UNREADABLE_STATEMENT } = vi.hoisted(() => ({
  ABSENT_STATEMENT:
    'No approval payload was recorded for this approval, so there is nothing to review below.',
  UNREADABLE_STATEMENT:
    'The recorded approval payload could not be read, so the fields below state nothing about it. ' +
    'This is not the same as an approval with no mappings.',
}));

vi.mock('../../hooks', () => ({
  useApprovals: () => mockApprovalsQuery,
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
  useApprovalDetail: (id: string | undefined) => {
    detailQueryCalls.push(id);
    return mockDetailQuery;
  },
  APPROVAL_DETAILS_ABSENT_STATEMENT: ABSENT_STATEMENT,
  APPROVAL_DETAILS_UNREADABLE_STATEMENT: UNREADABLE_STATEMENT,
}));

import ApprovalsPage from '../ApprovalsPage';

/** The em dash every reader on this surface renders for a fact never established. */
const NOT_ESTABLISHED = '\u2014';

const APPROVAL_ID = 'newtest-ab2a08a0_schema_mapping_1787402782';
const PIPELINE_KEY = 'newtest-ab2a08a0';

/**
 * The reported approval's stored payload, parsed — verbatim from the live row.
 * Nine keys, eight `null`, one populated.
 */
const LIVE_ALL_NULL_PAYLOAD: Record<string, unknown> = {
  source_schema: null,
  target_schema: null,
  proposed_mappings: null,
  confidence_scores: null,
  unmapped_fields: null,
  mapping_summary: null,
  evidence: null,
  questions: null,
  generated_by: 'us.anthropic.claude-sonnet-4-6',
};

/**
 * A payload from the same table after the producer began populating them
 * (2026-08-31 rows), reduced to the keys this surface renders. Element shape is
 * the producer's own `{source_field, target_field}`.
 */
const POPULATED_PAYLOAD: Record<string, unknown> = {
  mapping_count: 7,
  generated_by: 'us.anthropic.claude-sonnet-4-6',
  proposed_mappings: [
    { source_field: 'signup_date', target_field: 'registration_date', confidence: 0.96 },
  ],
  unmapped_fields: {
    source: [],
    target: ['loyalty_tier'],
    target_unsourced: ['loyalty_tier'],
    target_required: ['loyalty_tier'],
  },
  // `target_name`, which is the producer's key — NOT `proposed_name`. Worth
  // stating because the first draft of this fixture used `proposed_name` and
  // `proposedRenames` correctly skipped the entry, rendering `None`: the reader
  // requires both names to be non-empty strings under the producer's own key
  // names, and it declines to guess. That is the reader working, and it is why
  // the fixture is written from `utils/rename-manifest.ts`'s contract rather
  // than from a plausible-looking shape.
  rename_manifest: {
    manifest_id: 'mapping-hash-p56',
    renames: [{ original_name: 'Customer Name!', target_name: 'customer_name' }],
  },
};

function listingRow(): Approval {
  return {
    approval_id: APPROVAL_ID,
    pipeline_key: PIPELINE_KEY,
    run_id: 'run-2026-08-22-12-46-00-8032490a',
    approval_type: 'schema_mapping',
    // The Review button — the only way into this modal — renders for pending rows
    // only (`ApprovalsPage.tsx`).
    status: 'pending',
    created_at: '2026-08-22T12:46:23.029231+00:00',
    // DELIBERATELY NO `details`. That is the whole defect: the listing row does
    // not carry one, so a fixture that put one here would be testing a shape the
    // surface never receives.
  };
}

function detailResponse(
  status: ApprovalDetailsStatus,
  details?: Record<string, unknown>,
): ApprovalDetail {
  const response: ApprovalDetail = { ...listingRow(), details_status: status };
  // `details` is OMITTED on the two non-parsed states, exactly as the handler
  // omits it — not sent as `{}`.
  if (details !== undefined) response.details = details;
  return response;
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

/** Renders the page with one pending approval and clicks Review. */
function openReview() {
  mockApprovalsQuery.data = {
    pages: [{ approvals: [listingRow()], hasMore: false }],
    pageParams: [undefined],
  };
  renderPage();
  fireEvent.click(screen.getByRole('button', { name: 'Review' }));
}

beforeEach(() => {
  detailQueryCalls = [];
  mockApprovalsQuery = {
    data: { pages: [{ approvals: [], hasMore: false }], pageParams: [undefined] },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
  mockDetailQuery = {
    data: detailResponse('parsed', POPULATED_PAYLOAD),
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
});

// ===========================================================================
// The fetch itself
// ===========================================================================

describe('the payload is fetched for the approval being reviewed', () => {
  it('fires no request until a row is opened', () => {
    renderPage();

    // The hook is called on every render (React rules), but with `undefined`,
    // which is what `enabled: !!approvalId` gates on. Asserting the ARGUMENT
    // rather than the call count is what distinguishes "no request" from "the
    // hook was not reached".
    expect(detailQueryCalls.every((id) => id === undefined)).toBe(true);
  });

  it('is keyed on the opened approval id', () => {
    openReview();

    expect(detailQueryCalls).toContain(APPROVAL_ID);
  });
});

// ===========================================================================
// Async_State_Triple — loading
// ===========================================================================

describe('while the payload is loading', () => {
  beforeEach(() => {
    mockDetailQuery = { data: undefined, isLoading: true, isError: false, refetch: vi.fn() };
  });

  it('says it is loading', () => {
    openReview();

    expect(screen.getByTestId('approval-detail-loading')).toBeTruthy();
  });

  it('renders no field readers at all — an in-flight read states nothing', () => {
    openReview();

    // Not "renders an em dash": the cells must not be on the screen. An em dash
    // beside a label is a claim that the payload was consulted, and it has not
    // been yet.
    expect(screen.queryByTestId('approvals-mapping-count')).toBeNull();
    expect(screen.queryByTestId('approvals-unsourced-target-fields')).toBeNull();
    expect(screen.queryByTestId('approvals-proposed-renames')).toBeNull();
  });

  it('still shows the four fields the listing row already carries', () => {
    openReview();

    // The header fields are deliberately NOT gated on the fetch: the modal opens
    // populated with what is already in hand, and only the payload arrives late.
    expect(screen.getAllByText(PIPELINE_KEY).length).toBeGreaterThan(0);
    expect(screen.getAllByText('schema_mapping').length).toBeGreaterThan(0);
  });
});

// ===========================================================================
// Async_State_Triple — error. The state that must not read as "no mappings".
// ===========================================================================

describe('when the payload could not be loaded', () => {
  beforeEach(() => {
    mockDetailQuery = { data: undefined, isLoading: false, isError: true, refetch: vi.fn() };
  });

  it('says the load failed', () => {
    openReview();

    expect(screen.getByTestId('approval-detail-error')).toBeTruthy();
  });

  it('offers a Retry wired to the query own refetch', () => {
    openReview();
    fireEvent.click(screen.getByTestId('approval-detail-retry'));

    expect(mockDetailQuery.refetch).toHaveBeenCalledTimes(1);
  });

  it('does NOT render as an approval with no mappings', () => {
    openReview();

    // The requirement, stated as an absence. Without the error branch, `details`
    // is `undefined` and every reader below reports "not established" — which is
    // indistinguishable from a payload the producer never wrote, and would let an
    // approver sign off believing they had been shown the mapping.
    expect(screen.queryByTestId('approvals-mapping-count')).toBeNull();
    expect(screen.queryByText('No mappings available')).toBeNull();
  });

  it('does not claim the producer stored nothing', () => {
    openReview();

    // A read failure is not a producer fact. Neither payload-state sentence may
    // appear, because neither has been established.
    expect(screen.queryByTestId('approval-detail-absent')).toBeNull();
    expect(screen.queryByTestId('approval-detail-unreadable')).toBeNull();
  });

  it('treats a resolved-but-empty response as a failure rather than as data', () => {
    // `isError` false with no `data` — the shape React Query can briefly hold.
    // Falling through would hand `undefined` to every reader.
    mockDetailQuery = { data: undefined, isLoading: false, isError: false, refetch: vi.fn() };
    openReview();

    expect(screen.getByTestId('approval-detail-error')).toBeTruthy();
  });
});

// ===========================================================================
// The parsed state — the payload reaches the modal's fields
// ===========================================================================

describe('a populated payload reaches the modal fields', () => {
  it('renders the mapping count from the fetched payload', () => {
    openReview();

    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe('7');
  });

  it('renders the unsourced target fields with the ratified requirement word', () => {
    openReview();

    const cell = screen.getByTestId('approvals-unsourced-target-fields').textContent ?? '';
    expect(cell).toContain('loyalty_tier');
    // The word comes from `field-requirement.ts` (`LGR-7561acb-4`), not from here.
    expect(cell).toContain('Required');
  });

  it('renders both halves of every proposed rename', () => {
    openReview();

    const cell = screen.getByTestId('approvals-proposed-renames').textContent ?? '';
    expect(cell).toContain('Customer Name!');
    expect(cell).toContain('customer_name');
  });

  it('renders no payload-state banner — the third state is silence', () => {
    openReview();

    expect(screen.queryByTestId('approval-detail-absent')).toBeNull();
    expect(screen.queryByTestId('approval-detail-unreadable')).toBeNull();
    expect(screen.queryByTestId('approval-detail-error')).toBeNull();
  });

  it('reaches `MappingViewer` in the artifact panel, not only the preview cells', () => {
    openReview();

    // `ApprovalArtifact` is passed the same fetched approval, so the panel above
    // the Mapping Preview lights up too. Asserted because it is the reader the
    // reported screen showed as "No mappings available" / Total Mappings 0.
    expect(screen.queryByText('No mappings available')).toBeNull();
    expect(screen.getAllByText('signup_date').length).toBeGreaterThan(0);
  });
});

describe('the reported approval — parsed, and every value null', () => {
  beforeEach(() => {
    mockDetailQuery = {
      data: detailResponse('parsed', LIVE_ALL_NULL_PAYLOAD),
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    };
  });

  it('is not treated as a failed read', () => {
    openReview();

    // The payload WAS stored and WAS read. Rendering the error or the unreadable
    // banner here would blame the read for a producer fact.
    expect(screen.queryByTestId('approval-detail-error')).toBeNull();
    expect(screen.queryByTestId('approval-detail-unreadable')).toBeNull();
    expect(screen.queryByTestId('approval-detail-absent')).toBeNull();
  });

  it('reports every null field as not established, not as zero', () => {
    openReview();

    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe(NOT_ESTABLISHED);
    expect(screen.getByTestId('approvals-mapping-count').textContent).not.toBe('0');
    expect(screen.getByTestId('approvals-unsourced-target-fields').textContent).toBe(NOT_ESTABLISHED);
    expect(screen.getByTestId('approvals-proposed-renames').textContent).toBe(NOT_ESTABLISHED);
  });

  it('renders the one populated key — the AI model stops reading Unknown', () => {
    openReview();

    // `generated_by` is the only non-null value on the reported row, and
    // `toMappingData` falls `ai_model_used` back to it, so `MappingViewer`'s AI
    // Model cell goes from `Unknown` to the model's display name. This is the
    // whole of the user-visible change for THAT approval, which is why it is
    // asserted rather than assumed.
    expect(screen.getAllByText('Claude Sonnet 4.6').length).toBeGreaterThan(0);
    expect(screen.queryByText('Unknown')).toBeNull();
  });
});

// ===========================================================================
// The two non-parsed states, and the pairwise distinctions
// ===========================================================================

describe('a payload the producer never stored', () => {
  beforeEach(() => {
    mockDetailQuery = {
      data: detailResponse('absent'),
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    };
  });

  it('says the producer recorded nothing', () => {
    openReview();

    expect(screen.getByTestId('approval-detail-absent').textContent).toContain(ABSENT_STATEMENT);
  });

  it('does not also claim the payload was unreadable', () => {
    openReview();

    expect(screen.queryByTestId('approval-detail-unreadable')).toBeNull();
  });

  it('reports every field as not established rather than as zero', () => {
    openReview();

    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe(NOT_ESTABLISHED);
  });
});

describe('a payload that could not be read', () => {
  beforeEach(() => {
    mockDetailQuery = {
      data: detailResponse('unreadable'),
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    };
  });

  it('says the stored payload could not be read', () => {
    openReview();

    expect(screen.getByTestId('approval-detail-unreadable').textContent).toContain(UNREADABLE_STATEMENT);
  });

  it('says in words that this is not an approval with no mappings', () => {
    openReview();

    // The sentence carries the distinction in TEXT, not in the alert colour —
    // `LGR-99050e6-1` requires it and colour alone fails WCAG 1.4.1.
    expect(screen.getByTestId('approval-detail-unreadable').textContent).toContain(
      'not the same as an approval with no mappings',
    );
  });

  it('renders not-established rather than zero for every field', () => {
    openReview();

    // The requirement in the prompt's own words: an unreadable payload renders as
    // not-established rather than as zero.
    expect(screen.getByTestId('approvals-mapping-count').textContent).toBe(NOT_ESTABLISHED);
    expect(screen.getByTestId('approvals-mapping-count').textContent).not.toBe('0');
    expect(screen.getByTestId('approvals-unsourced-target-fields').textContent).toBe(NOT_ESTABLISHED);
    expect(screen.getByTestId('approvals-proposed-renames').textContent).toBe(NOT_ESTABLISHED);
  });

  it('does not also claim the producer stored nothing', () => {
    openReview();

    expect(screen.queryByTestId('approval-detail-absent')).toBeNull();
  });
});

describe('the three states are distinguishable on the surface', () => {
  /** Renders one state and returns which banners the surface showed. */
  function bannersFor(status: ApprovalDetailsStatus, details?: Record<string, unknown>): string {
    mockDetailQuery = {
      data: detailResponse(status, details),
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    };
    openReview();
    const absent = screen.queryByTestId('approval-detail-absent') !== null;
    const unreadable = screen.queryByTestId('approval-detail-unreadable') !== null;
    const error = screen.queryByTestId('approval-detail-error') !== null;
    return `absent=${absent} unreadable=${unreadable} error=${error}`;
  }

  it('an unreadable payload does not look like an absent one', () => {
    const unreadable = bannersFor('unreadable');
    cleanup();
    const absent = bannersFor('absent');

    expect(unreadable).not.toBe(absent);
  });

  it('an unreadable payload does not look like an empty one', () => {
    // The headline case, and the one `{}` on the wire would have merged: `{}` says
    // the producer stored an empty payload; unreadable says we could not read what
    // it stored. Both make every field read not-established, so the BANNER is the
    // only thing that can tell them apart — which is why it exists.
    const unreadable = bannersFor('unreadable');
    cleanup();
    const empty = bannersFor('parsed', {});

    expect(unreadable).not.toBe(empty);
  });

  it('an absent payload does not look like an empty one', () => {
    const absent = bannersFor('absent');
    cleanup();
    const empty = bannersFor('parsed', {});

    expect(absent).not.toBe(empty);
  });

  it('a parsed-but-empty payload is silent — it is not an error', () => {
    expect(bannersFor('parsed', {})).toBe('absent=false unreadable=false error=false');
  });
});
