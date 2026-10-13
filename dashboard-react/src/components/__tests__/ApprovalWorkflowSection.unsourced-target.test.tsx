// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p50 task 5.3 — the sibling `unmapped_fields` render on the approval card.
 *
 * `ApprovalWorkflowSection.tsx:241` read `String(details.unmapped_fields)` behind
 * a `!= null` guard, and the guard is the only reason this site did not throw the
 * way `ApprovalsPage`'s did. The producer writes a dict
 * (`schema_mapping/lambda_function.py:1740-1744`), so once task 2.1 pointed
 * `get_approval_data` at `output_data.approval` this card showed:
 *
 *     Unmapped fields: [object Object]
 *
 * on every corrected payload — wrong in a quieter way than the crash, and wrong
 * for the same reason. codegraph reports **no covering tests** for
 * `renderTypeSpecificContent` (`:221`), which is why no existing test saw it.
 *
 * The expression now lives in `utils/approval-unsourced-target-fields.ts` for
 * both surfaces, so this file asserts the card's three states rather than
 * re-deriving the join — that module's own tests cover the adapter, and
 * `unsourced-target-fields.property.test.ts` covers the ratified reader
 * (`LGR-9e76249-7`, `LGR-7561acb-1`, `LGR-7561acb-4`).
 *
 * Requirements: none of its own — a regression under R1.4's change set.
 */
import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail, SchemaMappingApprovalDetails } from '../../types';

const baseRunDetail: RunDetail = {
  run_id: 'run-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2026-08-24T13:48:26Z',
};

/** The em dash the card renders for a fact that was never established. */
const NOT_ESTABLISHED = '\u2014';

function schemaMappingApproval(details: SchemaMappingApprovalDetails): Approval {
  return {
    approval_id: 'approval-sm-5-3',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-24T13:48:26Z',
    gate_name: 'Schema Review',
    details,
  };
}

/** Renders the card and returns the unsourced-target value, verbatim. */
function renderCardValue(details: SchemaMappingApprovalDetails): string {
  const { container } = render(
    <ApprovalWorkflowSection approvals={[schemaMappingApproval(details)]} runDetails={baseRunDetail} />,
  );
  return (
    within(container).getByTestId('workflow-unsourced-target-fields').textContent ?? ''
  );
}

/** Renders the card and reports whether the line appeared at all. */
function cardLineIsRendered(details: SchemaMappingApprovalDetails): boolean {
  const { container } = render(
    <ApprovalWorkflowSection approvals={[schemaMappingApproval(details)]} runDetails={baseRunDetail} />,
  );
  return within(container).queryByTestId('workflow-unsourced-target-fields') !== null;
}

describe('ApprovalWorkflowSection unsourced target fields — the producer dict', () => {
  it('names the field and its requirement instead of stringifying the dict', () => {
    const rendered = renderCardValue({
      unmapped_fields: {
        source: ['legacy_notes'],
        target: [
          { field: 'mobile_phone', default_value: null, reason: 'No source field in generated mapping' },
        ],
        target_unsourced: ['mobile_phone'],
        target_required: ['mobile_phone'],
      },
    });

    expect(rendered).toBe('mobile_phone (Required)');
    // The measured pre-fix output, asserted as an absence.
    expect(rendered).not.toContain('[object Object]');
  });

  it('lists every roster name on one line, required and optional alike', () => {
    // An approver must not be shown a filtered list whose filter is invisible,
    // so an optional unsourced field appears too.
    const rendered = renderCardValue({
      unmapped_fields: {
        source: [],
        target: ['mobile_phone', 'nickname'],
        target_unsourced: ['mobile_phone', 'nickname'],
        target_required: ['mobile_phone'],
      },
    });

    expect(rendered).toBe('mobile_phone (Required), nickname (Optional)');
  });
});

describe('ApprovalWorkflowSection unsourced target fields — an unrecorded fact', () => {
  it('omits the line for the null every pre-task-2.1 row carries', () => {
    // The `!= null` guard is kept deliberately. Making the line unconditional so
    // it would render an em dash — matching `Mapping count` beside it — is a
    // display decision beyond this defect, and it broke two assertions in
    // `ApprovalWorkflowSection.actor-identity.characterization.test.tsx` that read
    // the count off this div's fused `textContent`. Omitting is also this card's
    // own convention: the `Confidence` line does the same.
    expect(cardLineIsRendered({ unmapped_fields: null })).toBe(false);
  });

  it('omits the line when the key is absent — the degraded payload', () => {
    expect(cardLineIsRendered({})).toBe(false);
  });

  it('renders None for a read payload whose roster is empty, and not the em dash', () => {
    // A present dict clears the guard, so the empty-roster fact reaches the card
    // and must read as the positive statement it is.
    const rendered = renderCardValue({ unmapped_fields: { source: [], target: [] } });

    expect(rendered).toBe('None');
    expect(rendered).not.toBe(NOT_ESTABLISHED);
  });

  it('renders not-established for a present value that is not the producer dict', () => {
    // The only route to the em dash on this card now that the guard stays: a
    // non-null value of the shape the old `as number` cast claimed.
    expect(renderCardValue({ unmapped_fields: 3 as unknown as undefined })).toBe(NOT_ESTABLISHED);
  });
});
