// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for the run-detail `SchemaTab` mapping editor
 * (p30 task 2.8, R14.2).
 *
 * **Validates: Requirements 14.6**
 *
 * R14.2 adds the mapping editor to the run-detail Schema tab, but deliberately
 * **conditionally**: mappings are pipeline-level state keyed by `mapping_id`,
 * while this tab renders what a particular run proposed. `canEditMappings`
 * requires `pipelineKey` present *and* `runMappingId === currentMappingId`.
 *
 * The gate is the substance of the change, so both polarities are asserted. An
 * unconditional editor is not a smaller version of this feature — it is a
 * different defect: saving against the pipeline's current mapping silently
 * reverts it to an old run's proposal, and saving against a superseded record
 * changes nothing. Both report success.
 *
 * `MappingEditorSection` is the shared component both this tab and
 * `SchemaHistoryPage` now render, so a test that only covered the run surface
 * would not notice the extraction changing the pipeline-scoped one. That
 * assertion lives in `pages/__tests__/SchemaHistoryPage.test.tsx`, next to the
 * page it guards.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
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

import SchemaTab from '../SchemaTab';

/* ------------------------------------------------------------------ */
/*  Fixtures                                                           */
/* ------------------------------------------------------------------ */

const CURRENT_MAPPING_ID = 'map-current';
const OLD_MAPPING_ID = 'map-superseded';

const runMappingData: MappingData = {
  mappings: [
    { source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 },
    { source_field: 'fname', target_field: 'first_name', confidence: 0.88 },
  ],
  // Bare names — both producers write names there, so this half is not widened
  // (p45 do-NOT 19).
  unmapped_source: [],
  /**
   * p45 R4.2 — `unmapped_target` carries the Unmapped_Entry's object shape, so
   * `requirement`, `default_value` and `reason` survive the trip to the
   * component. An empty list was assignable to the old `string[]` **and** to
   * `UnsourcedTargetField[]`, so nothing would have complained about leaving it
   * — it was simply no longer describing the wire. `requirement` here is
   * `'required'` because the field is in the payload's `target_required`
   * (`schema_mapping/lambda_function.py:1396`); a present-and-null
   * `default_value` is a recorded null default (R3.3), not a missing one (R3.6).
   *
   * This also exercises `MappingEditorSection.tsx:97`'s name extraction: the
   * editor's `unmappedTarget` prop is `string[]` and an object reaching it would
   * render `[object Object]` as a selectable target field (C11, do-NOT 15).
   */
  unmapped_target: [
    {
      field: 'mobile_phone',
      requirement: 'required',
      default_value: null,
      reason: 'No source field in generated mapping',
    },
  ],
  schema_hash: 'sha-1',
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
}

function renderTab(overrides: Overrides = {}) {
  return render(
    <SchemaTab
      mappingData={overrides.mappingData === undefined ? runMappingData : overrides.mappingData}
      extractedSchema={EXTRACTED_SCHEMA}
      pipelineKey={'pipelineKey' in overrides ? overrides.pipelineKey : 'pipe-1'}
      runMappingId={overrides.runMappingId}
      currentMappingId={overrides.currentMappingId}
      isRegenerating={false}
      onSaveContext={vi.fn()}
      onRerunMapping={vi.fn()}
    />,
  );
}

function editButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: /edit mappings/i });
}

beforeEach(() => {
  updateMappingsMutate.mockReset();
  showToastMock.mockReset();
});

/* ------------------------------------------------------------------ */
/*  Tests                                                             */
/* ------------------------------------------------------------------ */

describe("SchemaTab — editor offered when the run's mapping is the pipeline's current one (R14.2)", () => {
  it('renders Edit Mappings and no unavailable explanation', () => {
    renderTab({ runMappingId: CURRENT_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(editButton()).toBeInTheDocument();
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
  });

  it('opens the drag-drop editor on click', () => {
    renderTab({ runMappingId: CURRENT_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    fireEvent.click(editButton()!);

    expect(screen.getAllByTestId('editable-mapping-row').length).toBe(runMappingData.mappings.length);
    // The read-only viewer is replaced, not shown alongside.
    expect(screen.queryByTestId('mapping-viewer')).not.toBeInTheDocument();
  });

  it("saves against the run's own mapping_id, which is also the pipeline's current one", () => {
    renderTab({ runMappingId: CURRENT_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    fireEvent.click(editButton()!);
    fireEvent.click(screen.getByRole('button', { name: /save mappings/i }));

    expect(updateMappingsMutate).toHaveBeenCalledTimes(1);
    expect(updateMappingsMutate.mock.calls[0][0]).toEqual(
      expect.objectContaining({ pipelineKey: 'pipe-1', mappingId: CURRENT_MAPPING_ID }),
    );
  });
});

describe('SchemaTab — editor withheld with a reason when the run has been superseded (R14.2)', () => {
  it('renders no Edit Mappings button', () => {
    renderTab({ runMappingId: OLD_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(editButton()).not.toBeInTheDocument();
  });

  it('explains why, and points at the pipeline Schema History', () => {
    renderTab({ runMappingId: OLD_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    const note = screen.getByTestId('mapping-edit-unavailable');
    expect(note.textContent).toContain('mappings this run proposed');
    expect(note.textContent).toContain('newer mapping');
    expect(note.textContent).toContain('Schema History');
  });
});

describe('SchemaTab — legacy run that recorded no mapping id fails closed (R14.2)', () => {
  it('renders no Edit Mappings button', () => {
    renderTab({ runMappingId: undefined, currentMappingId: CURRENT_MAPPING_ID });

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(editButton()).not.toBeInTheDocument();
  });

  it('says the run did not record which mapping it used', () => {
    renderTab({ runMappingId: undefined, currentMappingId: CURRENT_MAPPING_ID });

    const note = screen.getByTestId('mapping-edit-unavailable');
    expect(note.textContent).toContain('did not record which mapping it used');
    expect(note.textContent).toContain('Schema History');
  });
});

describe('SchemaTab — nothing flashes while ids are still loading (R14.2)', () => {
  it('withholds the editor and the explanation when the pipeline detail has not arrived', () => {
    renderTab({ runMappingId: CURRENT_MAPPING_ID, currentMappingId: undefined });

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(editButton()).not.toBeInTheDocument();
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
  });

  it('withholds the editor when the route param has not resolved', () => {
    renderTab({
      pipelineKey: undefined,
      runMappingId: CURRENT_MAPPING_ID,
      currentMappingId: CURRENT_MAPPING_ID,
    });

    expect(editButton()).not.toBeInTheDocument();
  });
});

describe('SchemaTab — no mapping data (R14.2)', () => {
  it('renders neither the editor nor the explanation, only the empty note', () => {
    renderTab({ mappingData: null, runMappingId: CURRENT_MAPPING_ID, currentMappingId: CURRENT_MAPPING_ID });

    expect(screen.queryByTestId('mapping-viewer')).not.toBeInTheDocument();
    expect(editButton()).not.toBeInTheDocument();
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
    expect(screen.getByText('No mapping data available for this run.')).toBeInTheDocument();
  });
});
