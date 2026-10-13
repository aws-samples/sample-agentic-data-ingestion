// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 task 6.2 — the Rename_Manifest on the artifact panel, the first of the
 * three approval surfaces (R10.1, R10.2, design decision W4-D3).
 *
 * What this file holds that the util test cannot: that the manifest reaches the
 * screen **on the existing `schema_mapping` artifact** rather than on a new
 * approval kind, that BOTH names of every rename are rendered, and that the panel
 * says what approving does. W4-D3 rejected a new top-level approval kind — R10.1
 * says the *existing* Schema_Mapping_Approval presents the manifest, and a new
 * kind would mean a new `waitForTaskToken` state and a new Choice inheriting
 * `LGR-69ca78b-1` (standing invariant) — so the artifact still being a
 * `schema_mapping` artifact is part of the requirement and is asserted, not
 * assumed.
 *
 * **Fixtures are the producer's shape**, transcribed from
 * `derive_rename_manifest`'s documented return
 * (`schema_mapping/lambda_function.py`, design §Data Models): `manifest_id`,
 * `source_schema_hash`, `approval_id`, and an ordered `renames` list of
 * `{original_name, target_name}`. `LGR-28b4d75-5` (**template / ratified**): a
 * test written from the consumer's own output cannot detect the producer drifting.
 *
 * `MappingViewer` is doubled, as the sibling characterization file in this
 * directory does it — this file's subject is the new panel, and the mapping table
 * beside it has its own tests.
 *
 * **Validates: Requirements 10.1, 10.2**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval, MappingData } from '../../../types';

vi.mock('../../MappingViewer', () => ({
  default: () => <div data-testid="mapping-viewer-double" />,
}));

vi.mock('../../../hooks', () => ({
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

/** The producer's manifest shape — two renames, one of them a collision suffix. */
const MANIFEST = {
  manifest_id: 'mapping-hash-p55',
  source_schema_hash: 'sha256:p55',
  approval_id: null,
  renames: [
    { original_name: 'first name', target_name: 'first_name' },
    { original_name: 'first-name', target_name: 'first_name_2' },
  ],
};

function mappingApproval(details: Record<string, unknown>): Approval {
  return {
    approval_id: 'appr-p55',
    pipeline_key: 'p55-pipeline',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-30T12:00:00Z',
    details,
  };
}

/** The one mapping shape that reaches the payload without raising. */
const MAPPINGS = [{ source_field: 'first name', target_field: 'first_name', confidence: 0.95 }];

beforeEach(() => {
  cleanup();
});

describe('the manifest is a sub-payload of the EXISTING schema_mapping artifact', () => {
  it('renders on the schema_mapping artifact, which is still a schema_mapping artifact', () => {
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: MANIFEST })}
      />,
    );

    // W4-D3: the existing kind, not a new one. If a later change routed the
    // manifest to its own artifact branch, this is what would catch it.
    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.getByTestId('approval-artifact-rename-manifest')).toBeInTheDocument();
    expect(screen.getByTestId('mapping-viewer-double')).toBeInTheDocument();
  });

  it('shows BOTH names of every Proposed_Rename (R10.2)', () => {
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: MANIFEST })}
      />,
    );
    const table = screen.getByTestId('approval-artifact-rename-manifest-table');

    for (const name of ['first name', 'first_name', 'first-name', 'first_name_2']) {
      expect(within(table).getByText(name)).toBeInTheDocument();
    }
  });

  it('states what approving does, and that rejecting ends the run', () => {
    // The house pattern for a panel sitting above live approve/reject buttons:
    // a header plus an explicit statement of the consequence of approving
    // (`LGR-78f241b-1`), which `TestFailureArtifact` and `UnknownArtifact` in the
    // same file both follow. Here the consequence is the whole point — the
    // approver is authorising a change of name, not reading a fact.
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: MANIFEST })}
      />,
    );
    const panel = screen.getByTestId('approval-artifact-rename-manifest');

    expect(panel.textContent).toMatch(/Approving accepts these names/i);
    expect(panel.textContent).toMatch(/[Rr]ejecting this approval ends the run/);
  });

  it('names the columns so each cell says which name it is', () => {
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: MANIFEST })}
      />,
    );
    const table = screen.getByTestId('approval-artifact-rename-manifest-table');

    // Column headers, not colour or position, carry which name is which —
    // `LGR-99050e6-1` (template / ratified) records that colour alone fails
    // WCAG 1.4.1, so state and meaning live in text on this surface too.
    expect(within(table).getByText('Source column name')).toBeInTheDocument();
    expect(within(table).getByText('Proposed target name')).toBeInTheDocument();
  });

  it('renders a real table with an accessible name and column-scoped headers', () => {
    // Relied on rather than hand-rolled: Cloudscape `Table` emits a `<table>` with
    // `<th scope="col">` and is keyboard navigable. `ariaLabels.tableLabel` gives
    // it a name, because the visible heading is on the enclosing Container and is
    // not programmatically associated with the table.
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: MANIFEST })}
      />,
    );

    const table = screen.getByRole('table', { name: 'Proposed source column renames' });
    expect(table).toBeInTheDocument();
    const headers = within(table).getAllByRole('columnheader');
    expect(headers.length).toBeGreaterThanOrEqual(2);
  });
});

describe('the panel renders only when a rename is actually proposed (R10.1)', () => {
  it('renders nothing when the manifest proposes no rename', () => {
    // R10.1's condition is "at least one Non_Conforming_Column_Name". An empty
    // `renames` list means every source column name already conforms, which is
    // nothing to say on a panel that has no label of its own — the two summary
    // surfaces distinguish it, because there a blank sits beside a label.
    render(
      <ApprovalArtifact
        approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: { renames: [] } })}
      />,
    );

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-rename-manifest')).not.toBeInTheDocument();
  });

  it('renders nothing, and does not crash, for a payload that carries no manifest at all', () => {
    // Every mapping row written before p55 is this case, and so is
    // `create_approval_payload`'s degraded arm — task 1.2 recorded the tolerance
    // obligation for exactly this reader. The artifact must still render the rest
    // of what it knows.
    render(<ApprovalArtifact approval={mappingApproval({ proposed_mappings: MAPPINGS })} />);

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.getByTestId('mapping-viewer-double')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-rename-manifest')).not.toBeInTheDocument();
  });

  it('renders nothing for a present-but-null manifest, the shape the projection sends', () => {
    render(
      <ApprovalArtifact approval={mappingApproval({ proposed_mappings: MAPPINGS, rename_manifest: null })} />,
    );

    expect(screen.getByTestId('approval-artifact-mapping')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-rename-manifest')).not.toBeInTheDocument();
  });
});

describe('an attacker-controlled original name', () => {
  it('renders markup as text rather than as markup', () => {
    // do-NOT 17: a CSV header is attacker-controlled, and
    // `shared/identifier_utils.py` states the threat as "a hostile CSV header must
    // never become code". React escapes text children, which is why the cell is
    // the sink and no `title`, URL or `dangerouslySetInnerHTML` is used.
    const hostile = '<img src=x onerror=alert(1)>';
    render(
      <ApprovalArtifact
        approval={mappingApproval({
          proposed_mappings: MAPPINGS,
          rename_manifest: { renames: [{ original_name: hostile, target_name: 'img_src_x_onerror_alert_1_' }] },
        })}
      />,
    );
    const table = screen.getByTestId('approval-artifact-rename-manifest-table');

    expect(within(table).getByText(hostile)).toBeInTheDocument();
    expect(table.querySelector('img')).toBeNull();
  });

  it('caps a pathological name and says it truncated', () => {
    // A 2 MB column name in a table cell is the same class of problem as a 2 MB
    // error message, which is what `identifier_utils._DISPLAY_LIMIT` exists for.
    // The elision is MARKED: an approver comparing two names cannot afford one of
    // them to be quietly wrong.
    render(
      <ApprovalArtifact
        approval={mappingApproval({
          proposed_mappings: MAPPINGS,
          rename_manifest: {
            renames: [{ original_name: 'c'.repeat(20_000), target_name: 'ccc' }],
          },
        })}
      />,
    );
    const table = screen.getByTestId('approval-artifact-rename-manifest-table');

    expect(table.textContent).toContain('truncated');
    expect(table.textContent).toContain('20000');
    expect(table.textContent!.length).toBeLessThan(1_000);
  });
});

describe('other approval types are untouched', () => {
  it('does not render the panel on a code_generation approval', () => {
    const codeApproval: Approval = {
      approval_id: 'appr-code',
      pipeline_key: 'p55-pipeline',
      approval_type: 'code_generation',
      status: 'pending',
      created_at: '2026-08-30T12:00:00Z',
      details: { code_version_id: 'cv-1', rename_manifest: MANIFEST },
    };
    render(<ApprovalArtifact approval={codeApproval} />);

    expect(screen.getByTestId('approval-artifact-code-generation')).toBeInTheDocument();
    expect(screen.queryByTestId('approval-artifact-rename-manifest')).not.toBeInTheDocument();
  });
});

// Referenced so the unused-type lint does not fire on the doubled module's prop
// type, which is the shape this file deliberately does not assert on.
export type _MappingDataReference = MappingData;
