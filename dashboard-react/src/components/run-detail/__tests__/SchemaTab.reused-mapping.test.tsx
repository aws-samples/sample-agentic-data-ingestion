// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 4.3 — `SchemaTab`'s reuse arm, and the `notEditableReason` trap.
 *
 * A Reuse_Run takes the `ReuseExistingMapping` Pass instead of the
 * `schema_mapping` Lambda. A Pass writes nothing to DynamoDB, so the run has no
 * `(run_id, "schema_mapping")` state row, `RunDetailPage` builds no
 * `runMappingData` from it, and this tab used to render *"No mapping data
 * available for this run."* — a true sentence that hid the reason.
 *
 * **THREE ARMS, and the third is what this file adds.** The two existing absence
 * states are untouched and are asserted here as non-regression, because R2's
 * whole point is that a reuse and a genuine absence are different facts.
 *
 * **THE TRAP.** `notEditableReason`'s third branch emits *"This run did not
 * record which mapping it used"* whenever `runMappingId` is empty and
 * `currentMappingId` is set — and on a Reuse_Run `runMappingId` is `''`. So
 * populating `mappingData` without overriding that sentence puts a **false**
 * statement beside a populated mapping: the run did record which mapping it
 * used, and `reusedMappingId` names it. The route taken is the reuse arm's own
 * reason; `runMappingId` is deliberately NOT given `reused_mapping_id`, which
 * would flip `mappingIsCurrent` and offer the editor on a surface no R2
 * criterion asks to make editable (`LGR-99050e6-3`, **template / ratified** —
 * the editor is offered only when the run's mapping is the pipeline's current
 * one, and missing ids fail closed).
 *
 * **Expectations derive from the requirements and from the exported copy
 * constants, never from rendered output.** `LGR-28b4d75-5` (**template /
 * ratified**): a test written from the thing under test cannot detect it
 * drifting, because the output is its own oracle. Each assertion below names the
 * criterion it carries.
 *
 * **Words, not colour** — do-NOT 8, `LGR-99050e6-1` (**template / ratified**,
 * colour alone fails WCAG 1.4.1) and `LGR-1f7892b-2`. Every assertion here is on
 * text; nothing asserts a `StatusIndicator` type or a `Badge`.
 *
 * _Requirements: 1.3, 2.1, 2.2, 2.3_
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { MappingData } from '../../../types';

const updateMappingsMutate = vi.fn();
const showToastMock = vi.fn();

vi.mock('../../../hooks', () => ({
  useUpdateMappings: () => ({ mutate: updateMappingsMutate, isPending: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, error: null }),
}));

vi.mock('../../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

import SchemaTab, {
  REUSED_MAPPING_STATEMENT,
  SCHEMA_MAPPING_STEP_ABSENT_STATEMENT,
  REUSED_MAPPING_READ_ONLY,
  REUSED_MAPPING_FIELDS_UNAVAILABLE,
  describeReusedMappingId,
} from '../SchemaTab';

/* ------------------------------------------------------------------ */
/*  Fixtures                                                           */
/* ------------------------------------------------------------------ */

const CURRENT_MAPPING_ID = 'map-current';
const REUSED_MAPPING_ID = 'map-reused-from-earlier-run';

/**
 * What the page hands over on the reuse path: the reused mapping's own fields,
 * projected from the origin run's approval payload (p54 task 2.2 returns that
 * run's `output_data.approval` verbatim, so these are the bytes the approver
 * saw). `schema_hash` is `''` and `ai_model_used` is absent because the payload
 * carries neither — see the memo in `RunDetailPage.tsx`.
 */
const reusedMappingData: MappingData = {
  mappings: [
    { source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 },
    { source_field: 'fname', target_field: 'first_name', confidence: 0.88 },
  ],
  unmapped_source: [],
  unmapped_target: [],
  schema_hash: '',
  version_index: 0,
};

const EXTRACTED_SCHEMA = JSON.stringify({
  type: 'object',
  properties: { cust_id: { type: 'string' } },
});

interface Overrides {
  mappingData?: MappingData | null;
  pipelineKey?: string;
  runMappingId?: string;
  currentMappingId?: string;
  reusedMapping?: boolean;
  reusedMappingId?: string;
  extractedSchema?: string;
}

/**
 * Defaults describe a Reuse_Run as the wire actually presents one:
 * `runMappingId` is `''` (no `schema_mapping` row to read it off),
 * `currentMappingId` is set (pipeline detail has arrived), and the reuse pair is
 * `true` + a non-blank id. That combination is exactly what arms the trap.
 */
function renderTab(overrides: Overrides = {}) {
  return render(
    <SchemaTab
      mappingData={overrides.mappingData === undefined ? reusedMappingData : overrides.mappingData}
      extractedSchema={'extractedSchema' in overrides ? overrides.extractedSchema! : EXTRACTED_SCHEMA}
      pipelineKey={'pipelineKey' in overrides ? overrides.pipelineKey : 'pipe-1'}
      runMappingId={'runMappingId' in overrides ? overrides.runMappingId : ''}
      currentMappingId={
        'currentMappingId' in overrides ? overrides.currentMappingId : CURRENT_MAPPING_ID
      }
      reusedMapping={'reusedMapping' in overrides ? overrides.reusedMapping : true}
      reusedMappingId={'reusedMappingId' in overrides ? overrides.reusedMappingId : REUSED_MAPPING_ID}
      isRegenerating={false}
      onSaveContext={vi.fn()}
      onRerunMapping={vi.fn()}
    />,
  );
}

function reuseNote(): HTMLElement | null {
  return screen.queryByTestId('schema-reused-mapping');
}

function editButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: /edit mappings/i });
}

beforeEach(() => {
  updateMappingsMutate.mockReset();
  showToastMock.mockReset();
});

/* ------------------------------------------------------------------ */
/*  R2.1, R2.2, R2.3 — three sentences, one per criterion              */
/* ------------------------------------------------------------------ */

describe('SchemaTab — a reused mapping is visible as reused (p54 R2.1, R2.2, R2.3)', () => {
  it('states that the mapping was reused (R2.1)', () => {
    renderTab();

    expect(screen.getByText(REUSED_MAPPING_STATEMENT)).toBeInTheDocument();
  });

  it('identifies which mapping was reused (R2.2)', () => {
    renderTab();

    const note = reuseNote();
    expect(note).not.toBeNull();
    expect(note!.textContent).toContain(REUSED_MAPPING_ID);
  });

  it('states that the schema-mapping step did not run for this run (R2.3)', () => {
    renderTab();

    expect(screen.getByText(SCHEMA_MAPPING_STEP_ABSENT_STATEMENT)).toBeInTheDocument();
  });

  it('carries the three facts as three separate statements, not one combined sentence', () => {
    // R2.3 exists because R2.1 alone is ambiguous, so the three must be
    // separately readable rather than fused. Asserted structurally: each string
    // is its own text node, which `getByText` on the exact string only finds
    // when it has not been concatenated into a neighbour.
    renderTab();

    expect(screen.getByText(REUSED_MAPPING_STATEMENT)).toBeInTheDocument();
    expect(screen.getByText(describeReusedMappingId(REUSED_MAPPING_ID))).toBeInTheDocument();
    expect(screen.getByText(SCHEMA_MAPPING_STEP_ABSENT_STATEMENT)).toBeInTheDocument();
  });

  it("renders the reused mapping's own fields (R1.3)", () => {
    renderTab();

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/*  The notEditableReason trap                                        */
/* ------------------------------------------------------------------ */

describe('SchemaTab — the reuse arm does not claim the run recorded no mapping (p54 R2.2)', () => {
  it('does NOT render the no-mapping-id sentence beside a populated mapping', () => {
    renderTab();

    const note = screen.getByTestId('mapping-edit-unavailable');
    // The false sentence. `runMappingId` is '' and `currentMappingId` is set, so
    // the pre-p54 chain would have produced exactly this.
    expect(note.textContent).not.toContain('did not record which mapping it used');
  });

  it('says instead that the mapping was reused rather than produced, and points at Schema History', () => {
    renderTab();

    const note = screen.getByTestId('mapping-edit-unavailable');
    expect(note.textContent).toBe(REUSED_MAPPING_READ_ONLY);
    expect(note.textContent).toContain('reused an existing mapping');
    expect(note.textContent).toContain('Schema History');
  });

  it('still withholds the editor, because the run produced no mapping of its own (LGR-99050e6-3)', () => {
    renderTab();

    expect(editButton()).not.toBeInTheDocument();
  });

  it('withholds the editor even when the reused id happens to be the pipeline\'s current one', () => {
    // The rejected route would have forwarded `reused_mapping_id` as
    // `runMappingId`, at which point THIS fixture would offer the editor — the
    // reused mapping is generally the pipeline's current one, so the coincidence
    // is the common case rather than a corner. `mappingIsCurrent` must stay
    // keyed on the run's own id.
    renderTab({ reusedMappingId: CURRENT_MAPPING_ID });

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(editButton()).not.toBeInTheDocument();
    expect(updateMappingsMutate).not.toHaveBeenCalled();
  });

  it('names no mapping when the reuse flag arrived without an id, rather than an empty identity', () => {
    renderTab({ reusedMappingId: undefined });

    const note = reuseNote();
    expect(note!.textContent).toContain('not recorded');
    expect(note!.textContent).toContain(REUSED_MAPPING_STATEMENT);
  });
});

/* ------------------------------------------------------------------ */
/*  Unreadable reused mapping — an absence, not an empty mapping       */
/* ------------------------------------------------------------------ */

describe('SchemaTab — a reused mapping whose fields could not be read (p54 R1.4 shape, do-NOT 7)', () => {
  it('states that the fields could not be read and renders no mapping table', () => {
    // `_reused_mapping_payload` spells an unreadable mapping as
    // `proposed_mappings: None`, so the page builds no `MappingData`. Rendering
    // an empty viewer would present a named reference to something absent as a
    // mapping with no fields (`LGR-b2f79ef-14`).
    renderTab({ mappingData: null });

    expect(screen.getByText(REUSED_MAPPING_FIELDS_UNAVAILABLE)).toBeInTheDocument();
    expect(screen.queryByTestId('mapping-viewer')).not.toBeInTheDocument();
  });

  it('still states the reuse, so the absence is explained rather than bare', () => {
    renderTab({ mappingData: null });

    expect(screen.getByText(REUSED_MAPPING_STATEMENT)).toBeInTheDocument();
    expect(screen.getByText(SCHEMA_MAPPING_STEP_ABSENT_STATEMENT)).toBeInTheDocument();
  });

  it('does NOT fall through to the generic absence sentence', () => {
    renderTab({ mappingData: null });

    expect(screen.queryByText('No mapping data available for this run.')).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/*  Non-regression — the existing two-state absence rendering         */
/* ------------------------------------------------------------------ */

describe('SchemaTab — the existing absence rendering survives (p54 R2.3 boundary)', () => {
  it('renders the generic sentence for a run with no mapping data and no reuse', () => {
    renderTab({ mappingData: null, reusedMapping: undefined, reusedMappingId: undefined });

    expect(screen.getByText('No mapping data available for this run.')).toBeInTheDocument();
    expect(reuseNote()).not.toBeInTheDocument();
  });

  it('renders the no-schema sentence when nothing was extracted either', () => {
    renderTab({
      mappingData: null,
      extractedSchema: '',
      reusedMapping: undefined,
      reusedMappingId: undefined,
    });

    expect(screen.getByText('No schema was extracted in this run.')).toBeInTheDocument();
  });

  it('says nothing about reuse when the fact is NOT ESTABLISHED, even though the step is absent', () => {
    // Both keys absent is a third state, not `false`. R2.1's and R2.2's
    // sentences are unsupported there — nothing says the mapping was reused and
    // there is no id to name — and R2.3's cannot be asserted either, because
    // this component cannot tell "the step did not run" from "the step ran and
    // proposed nothing": `mappingData` is null in both cases. Claiming R2.3
    // here would be p54 do-NOT 7's collapse.
    renderTab({ mappingData: null, reusedMapping: undefined, reusedMappingId: undefined });

    expect(screen.queryByText(REUSED_MAPPING_STATEMENT)).not.toBeInTheDocument();
    expect(screen.queryByText(SCHEMA_MAPPING_STEP_ABSENT_STATEMENT)).not.toBeInTheDocument();
    expect(screen.queryByText(REUSED_MAPPING_FIELDS_UNAVAILABLE)).not.toBeInTheDocument();
  });

  it('says nothing about reuse when the run generated its own mapping (reusedMapping false)', () => {
    renderTab({ runMappingId: CURRENT_MAPPING_ID, reusedMapping: false, reusedMappingId: undefined });

    expect(reuseNote()).not.toBeInTheDocument();
    // And the unchanged editor rule still applies: own mapping === current one.
    expect(editButton()).toBeInTheDocument();
  });

  it('leaves the superseded-run reason unchanged for a non-reuse run', () => {
    renderTab({
      runMappingId: 'map-superseded',
      reusedMapping: false,
      reusedMappingId: undefined,
    });

    const note = screen.getByTestId('mapping-edit-unavailable');
    expect(note.textContent).toContain('mappings this run proposed');
    expect(note.textContent).toContain('newer mapping');
  });

  it('leaves the no-mapping-id reason reachable for a non-reuse run that recorded none', () => {
    // The sentence the reuse arm overrides is NOT deleted — a run that genuinely
    // did not record a mapping id and did not reuse one still gets it.
    renderTab({ runMappingId: '', reusedMapping: undefined, reusedMappingId: undefined });

    const note = screen.getByTestId('mapping-edit-unavailable');
    expect(note.textContent).toContain('did not record which mapping it used');
  });
});
