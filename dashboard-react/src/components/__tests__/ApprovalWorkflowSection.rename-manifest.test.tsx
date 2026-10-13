// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 task 6.2 — the Rename_Manifest on the compact approval card, the third of
 * the three approval surfaces (R10.1, R10.2).
 *
 * This is the surface that receives parsed `details` from live data today:
 * `get_run_details` (`dashboard_api/handlers/pipelines_runs.py`) parses
 * `approval_data` and hands it through as `details`, which is why the run-detail
 * card and the `ApprovalArtifact` it contains are the live path, while the
 * approvals-list modal is fixture-exercised (recorded in
 * `ApprovalsPage.rename-manifest.test.tsx`).
 *
 * One line rather than a list, because the full table is in `ApprovalArtifact`
 * directly below on the same screen. And the line **omits itself** when the
 * payload recorded nothing — absent or `null` alike, the guard being loose `!= null`
 * exactly as the sibling line's is — which is this card's own convention: the
 * `Confidence` line below it does exactly the same, and the sibling
 * `Unsourced target fields` line was deliberately left conditional for a recorded
 * reason: making it unconditional broke two assertions in
 * `ApprovalWorkflowSection.actor-identity.characterization.test.tsx` that read
 * `/Mapping count:\s*0\b/` off this div's fused `textContent`, because an added
 * sibling removes the trailing word boundary. So the guard is load-bearing here
 * for a second, mechanical reason as well as a display one, and this file asserts
 * both the omission and the three-state text for rows that do carry the key.
 *
 * **Validates: Requirements 10.1, 10.2**
 */
import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail, SchemaMappingApprovalDetails } from '../../types';

const baseRunDetail: RunDetail = {
  run_id: 'run-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2026-08-30T12:00:00Z',
};

/** The em dash the card renders for a fact that was never established. */
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

function schemaMappingApproval(details: SchemaMappingApprovalDetails): Approval {
  return {
    approval_id: 'approval-p55-6-2',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-30T12:00:00Z',
    gate_name: 'Schema Review',
    details,
  };
}

/** Renders the card and returns the proposed-renames value, verbatim. */
function renderCardValue(details: SchemaMappingApprovalDetails): string {
  const { container } = render(
    <ApprovalWorkflowSection approvals={[schemaMappingApproval(details)]} runDetails={baseRunDetail} />,
  );
  return within(container).getByTestId('workflow-proposed-renames').textContent ?? '';
}

/** Renders the card and reports whether the line appeared at all. */
function cardLineIsRendered(details: SchemaMappingApprovalDetails): boolean {
  const { container } = render(
    <ApprovalWorkflowSection approvals={[schemaMappingApproval(details)]} runDetails={baseRunDetail} />,
  );
  return within(container).queryByTestId('workflow-proposed-renames') !== null;
}

describe('the card names both halves of every Proposed_Rename (R10.2)', () => {
  it('renders each rename as original → target', () => {
    const rendered = renderCardValue({ rename_manifest: PRODUCER_MANIFEST });

    expect(rendered).toContain('first name → first_name');
    expect(rendered).toContain('2024-total → _2024_total');
  });

  it('does not stringify the dict', () => {
    // The failure mode this card produced on its `unmapped_fields` sibling before
    // p50 task 5.3: `String(...)` on the producer's dict, which yields
    // `"[object Object]"` — wrong in a quieter way than the crash the review modal
    // got from the same payload.
    expect(renderCardValue({ rename_manifest: PRODUCER_MANIFEST })).not.toContain('[object Object]');
  });

  it('labels the line', () => {
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[schemaMappingApproval({ rename_manifest: PRODUCER_MANIFEST })]}
        runDetails={baseRunDetail}
      />,
    );
    expect(container.textContent).toContain('Proposed source column renames:');
  });
});

describe('the three states, and the omission', () => {
  it('says None when the manifest was read and proposes nothing', () => {
    expect(renderCardValue({ rename_manifest: { renames: [] } })).toBe('None');
  });

  it('omits the line for a payload that recorded nothing — absent OR null', () => {
    // The card's guard is `!= null`, loose, so it excludes `undefined` and `null`
    // alike. Both are "this row recorded nothing about renames": an approval whose
    // `approval_data` predates the projection's eleventh key omits the key, and a
    // pre-p55 mapping row arrives present-and-`null` because the projection reads
    // it with `.get`. Omitting is this card's own convention for an unrecorded
    // fact — `Confidence` below omits itself the same way, and the `unmapped_fields`
    // sibling's guard was deliberately KEPT for a recorded reason.
    //
    // The em dash lives on `ApprovalsPage` instead, and that asymmetry is
    // deliberate rather than a drift: there the field sits in a labelled grid cell
    // that would otherwise render blank, and a blank beside a label reads as a
    // measured "None". Here there is no cell to leave empty.
    expect(cardLineIsRendered({})).toBe(false);
    expect(cardLineIsRendered({ mapping_count: 3 })).toBe(false);
    expect(cardLineIsRendered({ rename_manifest: null })).toBe(false);
  });

  it('renders the line, and the em dash, for a manifest present but unreadable', () => {
    // The state that reaches the em dash on THIS surface: a value the guard admits
    // and the reader cannot use. Reachable — the projection passes through whatever
    // `output_data.approval.rename_manifest` holds, so a malformed stored payload
    // arrives as a non-dict or as a dict with no `renames`. It must not claim
    // "None", which would be a measured statement about a payload that stated
    // nothing.
    expect(renderCardValue({ rename_manifest: { manifest_id: 'm' } })).toBe(NOT_ESTABLISHED);
    expect(renderCardValue({ rename_manifest: 3 as never })).toBe(NOT_ESTABLISHED);
  });

  it('never renders blank for a state it renders at all', () => {
    for (const details of [
      { rename_manifest: PRODUCER_MANIFEST },
      { rename_manifest: { renames: [] } },
      { rename_manifest: { manifest_id: 'm' } },
    ] as SchemaMappingApprovalDetails[]) {
      expect(renderCardValue(details).length).toBeGreaterThan(0);
    }
  });
});

describe('the sibling lines are undisturbed', () => {
  it('leaves the mapping count and the unsourced line rendering as before', () => {
    // Both live in the same div and both are read off its fused `textContent` by
    // another task's characterization tests, so a new sibling is worth checking
    // against them.
    const { container } = render(
      <ApprovalWorkflowSection
        approvals={[
          schemaMappingApproval({
            mapping_count: 2,
            unmapped_fields: { source: [], target: [], target_unsourced: [], target_required: [] },
            rename_manifest: PRODUCER_MANIFEST,
          }),
        ]}
        runDetails={baseRunDetail}
      />,
    );

    expect(container.textContent).toContain('Mapping count: 2');
    expect(within(container).getByTestId('workflow-unsourced-target-fields')).toBeInTheDocument();
    expect(within(container).getByTestId('workflow-proposed-renames')).toBeInTheDocument();
  });
});

describe('an attacker-controlled original name', () => {
  it('renders markup as text and caps a pathological length', () => {
    const rendered = renderCardValue({
      rename_manifest: {
        renames: [
          { original_name: '<b>bold</b>', target_name: 'b_bold_b_' },
          { original_name: 'c'.repeat(50_000), target_name: 'ccc' },
        ],
      },
    });

    expect(rendered).toContain('<b>bold</b>');
    expect(rendered).toContain('truncated');
    expect(rendered.length).toBeLessThan(1_000);
  });
});
