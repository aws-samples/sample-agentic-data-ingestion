// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 4.4 — the reuse and unreadable states on the Mapping Preview card.
 *
 * This is the surface where the keys actually arrive populated: `get_run_details`
 * parses `approval_data` into `details` (`pipelines_runs.py:2250-2268`), and this
 * card is what the approver looks at when the reuse-run gate fires.
 *
 * **Why the unreadable arm needs a test rather than being obviously fine.**
 * Without task 4.4 the unreadable payload rendered as *a mapping with no rows*:
 * `toMappingData` (`ApprovalArtifact.tsx:124`) yields `mappings: []` for
 * `proposed_mappings: null`, and `MappingViewer` renders that as an empty table.
 * That is `LGR-b2f79ef-14`'s *named reference to something absent* presented as a
 * thing with no fields — do-NOT 7's collapse. The fix is a **sentence**, so what
 * these tests assert is the presence of two different sentences and the absence
 * of a confident zero.
 *
 * Expectations derive from R1.4, R2.1, R2.2 and from the producer's three states
 * (`_reused_mapping_payload`), never from this card's previous output — before
 * this task it read none of these keys.
 *
 * Requirements: R1.4, R2.1, R2.2
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, within, cleanup } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import {
  REUSED_MAPPING_ID_NOT_ESTABLISHED,
  REUSED_MAPPING_STATEMENT,
  REUSED_MAPPING_UNREADABLE_STATEMENT,
} from '../../utils/approval-reused-mapping';
import type { Approval, RunDetail, SchemaMappingApprovalDetails } from '../../types';

const baseRunDetail: RunDetail = {
  run_id: 'run-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2026-08-24T13:48:26Z',
};

/** The em dash the card renders for a count that was never established. */
const COUNT_NOT_ESTABLISHED = '\u2014';

/** The producer's **resolved** state, as `_reused_mapping_payload` emits it. */
const RESOLVED: SchemaMappingApprovalDetails = {
  source_schema: { fields: [{ name: 'signup_date' }] },
  target_schema: { fields: [{ name: 'registration_date' }] },
  proposed_mappings: [
    { source_field: 'signup_date', target_field: 'registration_date', confidence: 0.96 },
  ],
  mapping_count: 1,
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  reused_from_run_id: 'run-2026-08-01-09-11-02-origin',
};

/** The producer's **unreadable** state — exactly the five keys it emits. */
const UNREADABLE: SchemaMappingApprovalDetails = {
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  mapping_unreadable: true,
  proposed_mappings: null,
  mapping_count: null,
};

function schemaMappingApproval(details: SchemaMappingApprovalDetails): Approval {
  return {
    approval_id: 'approval-p54-4-4',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-24T13:48:26Z',
    gate_name: 'Schema Review',
    details,
  };
}

function renderCard(details: SchemaMappingApprovalDetails) {
  const { container } = render(
    <ApprovalWorkflowSection approvals={[schemaMappingApproval(details)]} runDetails={baseRunDetail} />,
  );
  return within(container);
}

/** The Mapping Preview card's own text, fused as the DOM fuses it. */
function cardText(scope: ReturnType<typeof renderCard>): string {
  return scope.getByTestId('type-content-schema-mapping').textContent ?? '';
}

afterEach(() => {
  cleanup();
});

describe('ApprovalWorkflowSection reuse — a resolved reused mapping (R2.1, R2.2)', () => {
  it('states that the mapping was reused and names which one', () => {
    const scope = renderCard(RESOLVED);

    expect(cardText(scope)).toContain(REUSED_MAPPING_STATEMENT);
    expect(scope.getByTestId('workflow-reused-mapping-id').textContent).toBe('mapping-abc123');
  });

  it('does not state that the mapping could not be read', () => {
    const scope = renderCard(RESOLVED);

    expect(scope.queryByTestId('workflow-reused-mapping-unreadable')).toBeNull();
    expect(cardText(scope)).not.toContain(REUSED_MAPPING_UNREADABLE_STATEMENT);
  });

  it('renders no mapping panel on THIS surface at all, resolved or not', () => {
    // A CORRECTION to task 4.4's premise, verified by reading `rendersArtifact`
    // (`ApprovalWorkflowSection.tsx:150-152`) end to end: it returns
    // `approvalType === 'test_failure' || approvalType === 'code_generation'`, so
    // `ApprovalArtifact` — and therefore `toMappingData` and `MappingViewer` —
    // is **never** reached for a `schema_mapping` approval on this surface. The
    // narrow gate is deliberate (`LGR-69ca78b-4` rejected widening it to types
    // with no renderer). The shared mapping table is the REVIEW MODAL's
    // (`ApprovalsPage.tsx:532` renders `ApprovalArtifact` unconditionally), and
    // it is asserted there.
    //
    // The consequence for this task: on this surface the sentence is not one
    // signal beside a mapping panel, it is the ONLY thing distinguishing a
    // reused mapping from a run's own — which makes it more load-bearing here,
    // not less.
    const scope = renderCard(RESOLVED);

    expect(scope.queryByTestId('approval-artifact-mapping')).toBeNull();
    expect(scope.queryByText('signup_date')).toBeNull();
  });

  it('renders the count the payload carries, not the em dash', () => {
    expect(cardText(renderCard(RESOLVED))).toMatch(/Mapping count:\s*1\b/);
  });
});

describe('ApprovalWorkflowSection reuse — an unreadable reused mapping (R1.4, do-NOT 7)', () => {
  it('states that the mapping could not be read', () => {
    const scope = renderCard(UNREADABLE);

    expect(scope.getByTestId('workflow-reused-mapping-unreadable').textContent).toBe(
      REUSED_MAPPING_UNREADABLE_STATEMENT,
    );
  });

  it('still names which mapping the gate is about', () => {
    // R2.2 survives the failure: an approver told only "could not be read" cannot
    // go and look at the mapping themselves.
    expect(renderCard(UNREADABLE).getByTestId('workflow-reused-mapping-id').textContent).toBe(
      'mapping-abc123',
    );
  });

  it('renders no mapping rows', () => {
    // Trivially true on this surface for the reason the resolved block records —
    // `rendersArtifact` gates `schema_mapping` out — and asserted anyway so that
    // widening that gate later cannot quietly put an empty mapping panel beside
    // this payload without this file noticing. The load-bearing version of this
    // assertion is in `ApprovalsPage.reused-mapping.test.tsx`, where
    // `MappingViewer` really does render.
    const scope = renderCard(UNREADABLE);

    expect(scope.queryByText('signup_date')).toBeNull();
    expect(scope.queryByText('registration_date')).toBeNull();
  });

  it('renders the count as an em dash rather than 0', () => {
    // The do-not-regress half. `mappingCountText` already returns the em dash for
    // a null count, so this pins that the reuse arm did not reintroduce `?? 0`
    // and did not route the unreadable payload through a zero-valued count.
    const text = cardText(renderCard(UNREADABLE));

    expect(text).toContain(`Mapping count: ${COUNT_NOT_ESTABLISHED}`);
    expect(text).not.toMatch(/Mapping count:\s*0\b/);
  });

  it('renders a different card from the resolved one — two sentences, not one panel', () => {
    const unreadableText = cardText(renderCard(UNREADABLE));
    cleanup();
    const resolvedText = cardText(renderCard(RESOLVED));

    expect(unreadableText).not.toBe(resolvedText);
    expect(unreadableText).toContain(REUSED_MAPPING_UNREADABLE_STATEMENT);
    expect(resolvedText).not.toContain(REUSED_MAPPING_UNREADABLE_STATEMENT);
  });
});

describe('ApprovalWorkflowSection reuse — task 2.3s third route (do-NOT 7)', () => {
  it('states the mapping could not be read for a resolved payload with a non-list mapping list', () => {
    // Thirteen keys, `mapping_count: null`, and NO `mapping_unreadable`. Keyed on
    // the marker alone this renders as an empty mapping beside a payload
    // asserting it is readable — the collapse arriving by a route neither of
    // task 4.4's two named arms covers. The reader classifies it `unreadable`;
    // this is where that decision is visible to an approver.
    const scope = renderCard({ ...RESOLVED, proposed_mappings: {}, mapping_count: null });

    expect(scope.getByTestId('workflow-reused-mapping-unreadable').textContent).toBe(
      REUSED_MAPPING_UNREADABLE_STATEMENT,
    );
    expect(cardText(scope)).toContain(`Mapping count: ${COUNT_NOT_ESTABLISHED}`);
  });

  it('is not distinguished by an object-shaped mapping list reaching a render', () => {
    // `{}` rather than `'not-a-list'` deliberately. A string is the one non-list
    // shape with a truthy `.length`, and `ApprovalsPage`'s untouched duplicate
    // table (`LGR-e59edd7-7` recorded and LEFT it) passes that straight to
    // `Table items=`. The reader classifies the string case too — pinned in
    // `utils/__tests__/approval-reused-mapping.test.ts` — but a render fixture
    // that exercises somebody else's unfixed defect would be testing that
    // defect, not this arm.
    const scope = renderCard({ ...RESOLVED, proposed_mappings: 4, mapping_count: null });

    expect(scope.getByTestId('workflow-reused-mapping-unreadable')).toBeInTheDocument();
  });
});

describe('ApprovalWorkflowSection reuse — absence renders nothing (do-NOT 7)', () => {
  it('renders no reuse block for the empty payload the producer emits for no reuse', () => {
    const scope = renderCard({});

    expect(scope.queryByTestId('workflow-reused-mapping')).toBeNull();
    expect(cardText(scope)).not.toContain(REUSED_MAPPING_STATEMENT);
  });

  it('renders no reuse block for a run that generated its own mapping', () => {
    // Absence means not established. A block saying "not reused" would assert
    // knowledge the payload does not carry.
    const own: SchemaMappingApprovalDetails = {
      proposed_mappings: [{ source_field: 'signup_date', target_field: 'registration_date' }],
      mapping_count: 1,
    };

    expect(renderCard(own).queryByTestId('workflow-reused-mapping')).toBeNull();
  });

  it('leaves the not-established id spelling reachable but unused by a non-reuse payload', () => {
    // Guards against the block being rendered unconditionally with an em dash
    // id — which would put "Reused mapping —" on every schema-mapping approval.
    expect(cardText(renderCard({}))).not.toContain(
      `Reused mapping: ${REUSED_MAPPING_ID_NOT_ESTABLISHED}`,
    );
  });
});
