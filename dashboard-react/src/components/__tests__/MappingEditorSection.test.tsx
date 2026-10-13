// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `MappingEditorSection` — the component's **first** test (p45 task 18.1).
 *
 * **Validates: Requirements 7.5, 7.6**
 *
 * ## Why this file exists
 *
 * C17: CodeGraph reports "no covering tests found" for `MappingEditorSection`,
 * and here that verdict is **correct** — `find src -name '*MappingEditorSection*'`
 * returns the component and nothing else, and `src/components/__tests__/` held no
 * file naming it before this one. (CodeGraph's coverage flag is not reliable in
 * this area in general: it reports the same for `MappingViewer`, which
 * `MappingViewer.property.test.tsx` and `MappingViewer.context.test.tsx` both
 * render. It was checked by reading here rather than trusted.)
 *
 * Task 18.1 changes one expression in this component — `MappingEditorSection.tsx:82`
 * now passes `displayed.unmapped_target.map((e) => e.field)` instead of the list
 * itself. `tsc` catches the *type* mismatch that made the change necessary; it
 * cannot catch a later "simplification" back to the whole entry once
 * `DragDropMappingEditorProps.unmappedTarget` is someday widened, nor can it say
 * anything about the two facts R7.5 requires to be left alone. So both are
 * pinned here.
 *
 * ## What is being pinned, and where each expectation comes from
 *
 * **1. The editor receives bare strings** (R7.6, C11, do-NOT 15). The
 * expectation is read off the *consumer's* contract, not off this component's
 * current output: `DragDropMappingEditorProps.unmappedTarget` is `string[]`
 * (`DragDropMappingEditor.tsx:20`), the editor spreads it into a `Set<string>`
 * (`:64-70`), and each member becomes a Cloudscape `Select` option's `label`
 * **and** `value` (`:183-187`) — so a non-string member renders
 * `[object Object]` as a selectable target field and `handleSave` (`:105-115`)
 * would save that string into `FieldMapping.target_field`. The `[object Object]`
 * assertion below is that failure named directly, because it is the one a reader
 * would otherwise have to reconstruct from three files.
 *
 * **2. An entry carrying `default_value` and `reason` yields only its `field`.**
 * The fixture shapes are transcribed from the Python producers rather than from
 * this component's output (`LGR-28b4d75-5`, **template / ratified**: *"a test
 * written from the producer's output cannot detect the producer drifting"*).
 * `_normalise_mapping_structure` (`schema_mapping/lambda_function.py:1122-1128`)
 * and `generate_simple_mapping` (`:884-890`) both write `{field, default_value,
 * reason}` together, which is why an entry carrying those keys is the case worth
 * pinning: they are the keys that must **not** travel into a dropdown of field
 * names.
 *
 * **3. The withheld-reason branch renders its existing text unchanged**
 * (R7.5, C7, do-NOT 9). `LGR-99050e6-3` (**template / ratified**, and the one
 * ledger record whose `scope` names this file) offers the run-detail editor only
 * when the run's mapping is the pipeline's current one, because
 * `PUT /pipelines/{id}/mappings` silently falls back to the latest mapping when
 * the id is absent — so an unconditional editor has two wrong outcomes that both
 * report "Mappings saved". The withheld affordance carries an explanation rather
 * than vanishing silently. Both reason strings below are transcribed verbatim
 * from their author, `run-detail/SchemaTab.tsx:66-72`; this component renders
 * what it is given, and these assertions are what makes "unchanged" checkable.
 *
 * p45 changes **no** affordance on this surface. It changes a prop *value*.
 *
 * ## Harness
 *
 * Both children are doubled, following
 * `approvals/__tests__/ApprovalArtifact.unmapped-entry.characterization.test.tsx:118-129`'s
 * `vi.hoisted` prop-capture pattern (itself following
 * `auth/__tests__/AuthProvider.test.tsx`). The doubles are what let the props
 * handed to `DragDropMappingEditor` be read **as objects** — a rendered-DOM
 * assertion could not tell a bare `'mobile_phone'` from an entry that happens to
 * stringify to something containing it, and that distinction is the whole
 * subject of this file. The `MappingViewer` double also carries the button that
 * invokes `onEditMode`, because the edit branch is reachable no other way.
 *
 * `useUpdateMappings` and `useToast` are overridden inert: the mutation is never
 * fired here, but `useToast` throws outside a `ToastProvider` and
 * `useUpdateMappings` would need a `QueryClientProvider`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import MappingEditorSection from '../MappingEditorSection';
import type { MappingData } from '../../types';
import type { UnsourcedTargetField } from '../../utils/unsourced-target-fields';

// ---------------------------------------------------------------------------
// Doubles
// ---------------------------------------------------------------------------

const mocks = vi.hoisted(() => ({
  /** Every prop `DragDropMappingEditor` was handed, as objects. */
  editorProps: null as Record<string, unknown> | null,
  /** Whether `MappingViewer` was offered an `onEditMode` callback. */
  viewerWasOfferedEditMode: null as boolean | null,
}));

vi.mock('../DragDropMappingEditor', () => ({
  default: (props: Record<string, unknown>) => {
    mocks.editorProps = props;
    return <div data-testid="drag-drop-editor-double" />;
  },
}));

vi.mock('../MappingViewer', () => ({
  default: (props: { onEditMode?: () => void }) => {
    mocks.viewerWasOfferedEditMode = typeof props.onEditMode === 'function';
    return (
      <div data-testid="mapping-viewer-double">
        <button
          type="button"
          data-testid="enter-edit-mode"
          onClick={() => props.onEditMode?.()}
        >
          Edit Mappings
        </button>
      </div>
    );
  },
}));

vi.mock('../../hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../hooks')>();
  return { ...actual, useUpdateMappings: () => ({ mutate: vi.fn() }) };
});

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: vi.fn() }) };
});

// ---------------------------------------------------------------------------
// Fixtures — transcribed from the producers, not from current output
// ---------------------------------------------------------------------------

/**
 * The reason `_normalise_mapping_structure` writes, verbatim
 * (`schema_mapping/lambda_function.py:102`, written at `:1126`).
 */
const UNSOURCED_MAPPING_REASON = 'No source field in generated mapping';

/**
 * An entry in the producer's full object shape: all three of `field`,
 * `default_value` and `reason` present, because both object producers write them
 * together (`:885-889`, `:1123-1127`). `default_value: null` is a **recorded**
 * null default (R3.3), which is precisely the value that must not be smuggled
 * into a `Select` option.
 */
const FULL_ENTRY: UnsourcedTargetField = {
  field: 'mobile_phone',
  requirement: 'required',
  default_value: null,
  reason: UNSOURCED_MAPPING_REASON,
};

/**
 * An entry with no `default_value` key and no `reason` — the bare-string
 * Unmapped_Entry after normalisation (`_unmapped_target_field_name:977-995`,
 * R4.6, R3.6), whose requirement could not be established because the
 * Unsourced_Keys were absent (R1.4, R2.6).
 */
const BARE_ENTRY: UnsourcedTargetField = {
  field: 'legacy_id',
  requirement: 'not_established',
};

function mappingData(overrides: Partial<MappingData> = {}): MappingData {
  return {
    mappings: [{ source_field: 'phone', target_field: 'phone', confidence: 0.97 }],
    unmapped_source: ['nickname'],
    unmapped_target: [FULL_ENTRY, BARE_ENTRY],
    source_schema_fields: ['phone', 'nickname'],
    target_schema_fields: ['phone', 'mobile_phone', 'legacy_id'],
    schema_hash: 'abc123',
    version_index: 0,
    ai_model_used: 'amazon.nova-lite-v1:0',
    ...overrides,
  };
}

/**
 * The two withheld-reason strings, transcribed verbatim from their author,
 * `run-detail/SchemaTab.tsx:66-72`.
 */
const SUPERSEDED_MAPPING_REASON =
  'These are the mappings this run proposed. The pipeline has since moved to a newer mapping, so they are read-only here — edit the current mapping from the pipeline\'s Schema History.';
const NO_MAPPING_ID_REASON =
  'This run did not record which mapping it used, so its mappings are read-only here — edit them from the pipeline\'s Schema History.';

/** Render, enter the edit branch, and return the props the editor was handed. */
function renderAndEnterEditMode(data: MappingData = mappingData()): Record<string, unknown> {
  render(<MappingEditorSection mappingData={data} pipelineKey="latesttesting-19ca826a" />);
  fireEvent.click(screen.getByTestId('enter-edit-mode'));
  // The branch was taken — otherwise the capture below would be a stale read.
  expect(screen.getByTestId('drag-drop-editor-double')).toBeInTheDocument();
  expect(mocks.editorProps).not.toBeNull();
  return mocks.editorProps as Record<string, unknown>;
}

beforeEach(() => {
  cleanup();
  mocks.editorProps = null;
  mocks.viewerWasOfferedEditMode = null;
});

// ---------------------------------------------------------------------------
// 1. The editor receives bare strings
// ---------------------------------------------------------------------------

describe('the names handed to DragDropMappingEditor (R7.6, C11, do-NOT 15)', () => {
  it('passes unmappedTarget as bare strings, in the producer order received', () => {
    const props = renderAndEnterEditMode();

    expect(props.unmappedTarget).toEqual(['mobile_phone', 'legacy_id']);
    for (const member of props.unmappedTarget as unknown[]) {
      expect(typeof member).toBe('string');
    }
  });

  it('never hands the editor a value that renders as [object Object]', () => {
    // The concrete failure C11 names: `unmappedTarget` members become a `Select`
    // option's `label` and `value` (`DragDropMappingEditor.tsx:183-187`), and a
    // chosen option's `value` is saved into `FieldMapping.target_field`
    // (`handleSave:105-115`). Stated as the rendered outcome so a reader does
    // not have to reassemble it from three files.
    const props = renderAndEnterEditMode();

    const handed = props.unmappedTarget as unknown[];
    expect(handed.length).toBeGreaterThan(0);
    for (const member of handed) {
      expect(String(member)).not.toContain('[object Object]');
    }
  });

  it('leaves unmapped_source alone — both producers write bare names (do-NOT 19)', () => {
    const props = renderAndEnterEditMode();

    expect(props.unmappedSource).toEqual(['nickname']);
  });

  it('passes targetSchemaFields straight through — a different prop, untouched', () => {
    // `MappingEditorSection.tsx:84`. Named here because task 13.1 concurrently
    // deletes a prop *also* called `targetSchemaFields` from `MappingViewer` — a
    // `SchemaField[]` one on a different component. This is
    // `DragDropMappingEditorProps.targetSchemaFields` (`:22`, `string[]`), and
    // p45 does not touch it.
    const props = renderAndEnterEditMode();

    expect(props.targetSchemaFields).toEqual(['phone', 'mobile_phone', 'legacy_id']);
    expect(props.sourceSchemaFields).toEqual(['phone', 'nickname']);
  });
});

// ---------------------------------------------------------------------------
// 2. An entry carrying default_value and reason yields only its field
// ---------------------------------------------------------------------------

describe('an entry carrying default_value and reason (R7.6)', () => {
  it('yields only its field — requirement, default_value and reason do not travel', () => {
    const props = renderAndEnterEditMode(
      mappingData({ unmapped_target: [FULL_ENTRY] }),
    );

    const handed = props.unmappedTarget as unknown[];
    expect(handed).toEqual(['mobile_phone']);
    expect(handed).toHaveLength(1);

    // Every fact the entry carries beyond its name is absent from what the
    // editor sees. Asserted on the serialised list because the editor's own
    // failure mode is a serialisation (`String(member)` as a `Select` label).
    const serialised = JSON.stringify(handed);
    expect(serialised).not.toContain('requirement');
    expect(serialised).not.toContain('default_value');
    expect(serialised).not.toContain('reason');
    expect(serialised).not.toContain(UNSOURCED_MAPPING_REASON);
  });

  it('yields the same shape for an entry with no default_value key', () => {
    // R3.6's case. `'default_value' in entry` is false here, and the extraction
    // is indifferent to that — one bare name either way, so no reader
    // downstream has to distinguish the two (`LGR-44d601e-1`).
    const props = renderAndEnterEditMode(
      mappingData({ unmapped_target: [BARE_ENTRY] }),
    );

    expect(props.unmappedTarget).toEqual(['legacy_id']);
  });

  it('hands the editor an empty list when nothing is unsourced', () => {
    const props = renderAndEnterEditMode(mappingData({ unmapped_target: [] }));

    expect(props.unmappedTarget).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 3. The withheld-reason branch renders its existing text unchanged
// ---------------------------------------------------------------------------

describe('the editor availability gate (R7.5, C7, do-NOT 9, LGR-99050e6-3)', () => {
  it('renders the superseded-mapping reason verbatim and withholds the affordance', () => {
    render(
      <MappingEditorSection
        mappingData={mappingData()}
        pipelineKey="latesttesting-19ca826a"
        editable={false}
        notEditableReason={SUPERSEDED_MAPPING_REASON}
      />,
    );

    expect(screen.getByTestId('mapping-edit-unavailable')).toHaveTextContent(
      SUPERSEDED_MAPPING_REASON,
    );
    // The affordance is withheld, not merely hidden: `MappingViewer` is handed
    // no `onEditMode` at all.
    expect(mocks.viewerWasOfferedEditMode).toBe(false);
    expect(screen.queryByTestId('drag-drop-editor-double')).not.toBeInTheDocument();
  });

  it('renders the missing-mapping-id reason verbatim — missing ids fail closed', () => {
    render(
      <MappingEditorSection
        mappingData={mappingData()}
        pipelineKey="latesttesting-19ca826a"
        editable={false}
        notEditableReason={NO_MAPPING_ID_REASON}
      />,
    );

    expect(screen.getByTestId('mapping-edit-unavailable')).toHaveTextContent(
      NO_MAPPING_ID_REASON,
    );
    expect(mocks.viewerWasOfferedEditMode).toBe(false);
  });

  it('offers the affordance and no explanation when editing is appropriate', () => {
    render(
      <MappingEditorSection mappingData={mappingData()} pipelineKey="latesttesting-19ca826a" />,
    );

    expect(mocks.viewerWasOfferedEditMode).toBe(true);
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
  });

  it('withholds the affordance silently when no reason was supplied', () => {
    // `editable={false}` with no `notEditableReason` renders nothing — the
    // component states the withholding only when its caller gave it words to
    // state it with (`MappingEditorSection.tsx:107-111`).
    render(
      <MappingEditorSection
        mappingData={mappingData()}
        pipelineKey="latesttesting-19ca826a"
        editable={false}
      />,
    );

    expect(mocks.viewerWasOfferedEditMode).toBe(false);
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
  });
});
