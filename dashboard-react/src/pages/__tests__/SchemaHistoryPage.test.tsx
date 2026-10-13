// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

vi.mock('../../hooks', () => ({
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovals: () => ({ data: undefined }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useMappingRegenPolling: () => {},
  useBedrockModels: () => ({ data: [], isLoading: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useTestResults: () => ({ data: undefined, isLoading: false, isError: false }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

import SchemaHistoryPage from '../SchemaHistoryPage';

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipe',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
  schema_history: [
    { version: 1, created: '2024-01-01T00:00:00Z', has_mapping: false, mapping_count: 0, runs: [] },
    { version: 2, created: '2024-02-01T00:00:00Z', has_mapping: true, mapping_count: 5, runs: [{ run_id: 'r1', timestamp: '2024-02-01T00:00:00Z', code_version_id: 'cv1' }] },
  ],
  all_files: [],
};

function renderPage(pipeline = mockPipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
        <SchemaHistoryPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

describe('SchemaHistoryPage', () => {
  it('renders version timeline dots', () => {
    renderPage();
    const dots = screen.getAllByTestId('version-dot');
    expect(dots).toHaveLength(2);
    expect(dots[0]).toHaveTextContent('v1');
    expect(dots[1]).toHaveTextContent('v2');
  });

  it('shows mapping count for the latest version by default', () => {
    renderPage();
    expect(screen.getByTestId('version-mapping-count')).toHaveTextContent('5');
  });

  it('renders inner tabs for Mappings, Code, Tests', () => {
    renderPage();
    expect(screen.getAllByText('Mappings').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Tests')).toBeInTheDocument();
  });

  it('shows empty state when no schema versions', () => {
    const emptyPipeline = { ...mockPipeline, schema_history: [] };
    renderPage(emptyPipeline);
    expect(screen.getByText('No schema versions available')).toBeInTheDocument();
  });
});

/**
 * p30 R14.2 extracted this page's mapping view/edit wiring into the shared
 * `MappingEditorSection`, which the run-detail Schema tab now also renders. The
 * run surface is deliberately conditional (see
 * `components/run-detail/__tests__/SchemaTab.test.tsx`); this page is not — it is
 * pipeline-scoped, so its `mapping_id` is by definition the current one and it
 * passes no `editable` prop, relying on the component's `true` default.
 *
 * This guards against the shared extraction silently narrowing the surface it
 * was extracted from. The pre-existing tests above never reached it: `mockPipeline`
 * v2 has `has_mapping: true` but no `mapping_details`, so the empty-mapping branch
 * renders instead.
 *
 * **Validates: Requirements 14.6**
 */
describe('SchemaHistoryPage — mapping editing stays unconditional (p30 R14.2)', () => {
  const pipelineWithMappings: Pipeline = {
    ...mockPipeline,
    mapping_id: 'map-current',
    schema_history: [
      mockPipeline.schema_history![0],
      {
        ...mockPipeline.schema_history![1],
        mapping_details: {
          mappings: [
            { source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 },
            { source_field: 'fname', target_field: 'first_name', confidence: 0.88 },
          ],
          unmapped_source_fields: [],
          // p45 task 17.1 — amended for the new element shape. `[]` was
          // assignable to both the old `string[]` and the new `unknown[]`
          // declaration, so **no gate would ever have flagged it** (task 7.2's
          // artefact §4, the not-gate-enumerable class): `tsconfig.json` excludes
          // `src/**/__tests__/**` from `tsc`, and vitest was green because an
          // empty list feeds every reader identically. It is amended because its
          // content was wrong for the wire this page now reads, not because
          // anything caught it.
          //
          // `unmapped_source_fields` stays bare names (do-NOT 19) — both
          // producers write names there, so an object shape would be invented.
          unmapped_target_fields: [
            { field: 'mobile_phone', default_value: null, reason: 'No source field in generated mapping' },
          ],
        },
      },
    ],
  };

  it('offers Edit Mappings with no unavailable explanation', () => {
    renderPage(pipelineWithMappings);

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /edit mappings/i })).toBeInTheDocument();
    expect(screen.queryByTestId('mapping-edit-unavailable')).not.toBeInTheDocument();
  });

  it('opens the drag-drop editor on click', () => {
    renderPage(pipelineWithMappings);

    fireEvent.click(screen.getByRole('button', { name: /edit mappings/i }));

    expect(screen.getAllByTestId('editable-mapping-row')).toHaveLength(2);
  });
});

/**
 * p45 R4.5 — this page stops discarding the Unmapped_Entry object, and makes no
 * required/optional claim while doing it.
 *
 * `SchemaHistoryPage`'s `mappingData` construction used to flatten
 * `mapping_details.unmapped_target_fields` with its own
 * `typeof f === 'string' ? f : (f as Record<string, unknown>).field` test, which
 * threw away `default_value` and `reason` on the way to the component. It now
 * routes the list through `utils/unsourced-target-fields.ts` —
 * the one tolerant reader of that wire shape (do-NOT 2) — so the entries reach
 * `MappingViewer` whole.
 *
 * **Why every entry here is `not_established`, and why that is correct rather
 * than broken.** `activeVersion.mapping_details` is a **mapping record, not an
 * approval payload**: `target_unsourced` and `target_required` are computed in
 * `create_approval_payload` (`schema_mapping/lambda_function.py:1395-1396`) and
 * stored on the approval, while the mapping record carries
 * `unmapped_target_fields` plus `target_schema_hash` — not the schema and not
 * the derived name lists. The keys are therefore *structurally* absent, and
 * R2.6 is the criterion that makes making no claim the right answer. The two
 * negative assertions below are that criterion in executable form: they hold
 * now, and they stay the R2.6 pin once task 13.2 gives `MappingViewer` its
 * requirement/consequence/reason renderer.
 *
 * **Scope note, and the three assertions task 13.2 added.** Task 17.1 owns the
 * *construction*; task 13.2 owns the *render*. When 17.1 landed, `MappingViewer`
 * rendered each unmapped-target entry as its name in a `Badge` and nothing else,
 * so `requirementLabel('not_established')`'s sentence, `consequenceStatement`'s
 * sentence and `reasonAttribution`'s heading were not in the DOM from any surface
 * and asserting them here would have been asserting another task's unlanded work.
 * 17.1's docstring asked 13.2 to add them once the renderer existed; the
 * `states the three facts …` case below is that request discharged. It matters
 * that it lives *here* and not only in
 * `components/__tests__/MappingViewer.unsourced-target.test.tsx`: that file hands
 * the component entries directly, while this one drives the whole page, so it is
 * the only observer that the three facts survive **this page's** construction —
 * a mapping record with no Unsourced_Keys — rather than a hand-built list.
 *
 * Expectations derive from R4.5, R2.6 and R1.4 plus the producer's entry shapes
 * (`_normalise_mapping_structure:1122-1128`, `generate_simple_mapping:884-890`),
 * never from the page's current output — `LGR-28b4d75-5` (**template /
 * ratified**): a test written from the producer's output cannot detect the
 * producer drifting, because the output is its own oracle.
 *
 * **Validates: Requirements 4.5, 2.6, 1.4**
 */
describe('SchemaHistoryPage — unsourced target fields (p45 R4.5)', () => {
  /**
   * Three reachable element shapes on one record, all three from the producers:
   * a bare string (R4.6 — no `default_value`, no `reason` to claim), the
   * normaliser's object with a **present null** default (R3.3), and
   * `generate_simple_mapping`'s object with a non-null default and its own
   * reason constant.
   */
  const pipelineWithUnsourcedTargets: Pipeline = {
    ...mockPipeline,
    mapping_id: 'map-current',
    schema_history: [
      mockPipeline.schema_history![0],
      {
        ...mockPipeline.schema_history![1],
        mapping_details: {
          mappings: [{ source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 }],
          unmapped_source_fields: [],
          unmapped_target_fields: [
            'legacy_code',
            { field: 'mobile_phone', default_value: null, reason: 'No source field in generated mapping' },
            { field: 'region', default_value: 'N/A', reason: 'No matching source field found' },
          ],
        },
      },
    ],
  };

  it('renders one element per entry, and every name, whatever the wire shape', () => {
    renderPage(pipelineWithUnsourcedTargets);

    const rendered = screen.getAllByTestId('unmapped-target');
    expect(rendered).toHaveLength(3);

    const text = rendered.map((el) => el.textContent ?? '').join(' ');
    expect(text).toContain('legacy_code');
    expect(text).toContain('mobile_phone');
    expect(text).toContain('region');
  });

  it('keeps the object entry legible instead of stringifying it', () => {
    renderPage(pipelineWithUnsourcedTargets);

    // The failure this replaces is not a crash. `unmapped_target` was declared
    // `string[]` while the wire carried objects, so any reader that trusted the
    // declaration rendered `[object Object]` or a JSON blob as a field name.
    // `MappingEditorSection.tsx:97` shows the same hazard on the write side: an
    // entry reaching `DragDropMappingEditor` unextracted becomes a *selectable*
    // `[object Object]` that `handleSave` would store as a `target_field`.
    expect(screen.queryByText('[object Object]')).not.toBeInTheDocument();
    expect(screen.queryByText(/"default_value"/)).not.toBeInTheDocument();
  });

  it('makes no required/optional claim, because this record carries no Unsourced_Keys', () => {
    renderPage(pipelineWithUnsourcedTargets);

    // R2.6 / do-NOT 5. Presenting a field whose requirement was never
    // established as "optional" is the permissive collapse C4 /
    // `LGR-4854b3d-1` (**template / ratified**) forbids, and it is the defect
    // p45 exists to remove. The two testid checks stay document-wide and
    // untouched — they are the pin.
    expect(screen.queryByTestId('requirement-optional')).not.toBeInTheDocument();
    expect(screen.queryByTestId('requirement-required')).not.toBeInTheDocument();

    // The word sweep is now scoped to the rendered entries, and that is a
    // correction rather than a relaxation — recorded at the assertion per
    // `LGR-60c4985-6` (**template / ratified**), because changing a deliberate
    // hold is a code change with a reason attached.
    //
    // When 17.1 wrote this line, `MappingViewer` rendered names only, so a
    // document-wide `queryByText(/optional/i)` was free. Task 13.2's renderer
    // puts design §4.6's fallback statement above the list, verbatim: "The
    // target schema's requirement for these fields was not established, so none
    // of them is shown as required **or optional**." The word appears there in a
    // *negation* — the sentence exists precisely to deny the claim — so a
    // document-wide sweep would now fail on the one element whose purpose is to
    // satisfy R2.6. What do-NOT 5 and R2.3 forbid is presenting a **field** as
    // optional, so that is what is asserted: per entry, plus the two global
    // testids above. A reworded per-entry label that reintroduces the word still
    // fails here, which was 17.1's stated reason for choosing a substring sweep
    // over a testid check.
    const entriesText = screen
      .getAllByTestId('unmapped-target')
      .map((el) => el.textContent ?? '')
      .join(' ');
    expect(entriesText).not.toMatch(/optional/i);
  });

  /**
   * The three positive assertions 17.1's docstring asked task 13.2 to add.
   *
   * Expectations are design §4.6's strings transcribed as literals, not calls to
   * `requirementLabel` / `consequenceStatement` / `reasonAttribution` —
   * `LGR-28b4d75-5` (**template / ratified**): a test written from the producer's
   * output cannot detect the producer drifting. Calling those functions would
   * assert that the page renders whatever they return, which stays true through a
   * rewording.
   *
   * All three entries land on `not_established` here because the keys are
   * structurally absent from a mapping record, so this case is also the R1.4 /
   * R2.6 fallback rendered end-to-end through a real page.
   */
  it('states the three facts per entry: requirement, consequence and reason', () => {
    renderPage(pipelineWithUnsourcedTargets);

    // 1. The requirement, in text, once per entry — plus the fallback statement
    //    once above the list, never repeated per entry.
    expect(screen.getAllByTestId('requirement-not-established')).toHaveLength(3);
    expect(
      screen.getAllByText("The target schema's requirement for this field was not established"),
    ).toHaveLength(3);
    expect(screen.getAllByTestId('unmapped-target-requirement-not-established')).toHaveLength(1);
    expect(
      screen.getByText(
        "The target schema's requirement for these fields was not established, so none of them is shown as required or optional.",
      ),
    ).toBeInTheDocument();

    // 2. The consequence, one per entry, and the three-state `default_value`
    //    distinguished on screen. `legacy_code` is a bare string so no default
    //    was recorded (R3.6); `mobile_phone` carries a **present null** (R3.3);
    //    `region` carries `'N/A'`.
    expect(screen.getAllByTestId('unmapped-target-consequence')).toHaveLength(3);
    expect(
      screen.getByText(
        'No source column supplies this field. The target column is written empty on every row; the mapper recorded no default value.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'No source column supplies this field. The target column is written as null on every row — that is the default the mapper recorded.',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'No source column supplies this field. The target column is written with the default the mapper recorded, "N/A", on every row.',
      ),
    ).toBeInTheDocument();

    // 3. The reason, verbatim, under an attribution heading of its own — two of
    //    them, because the bare string carries no reason to claim (R4.6). The two
    //    machine-written constants are **different facts** (C14, the producer says
    //    so at `:96-101`), so they get different headings.
    expect(screen.getAllByTestId('unmapped-target-reason')).toHaveLength(2);
    expect(
      screen.getByText('Reason (structural — the generated mapping named no source column)'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Reason (structural — name-similarity matching found no candidate)'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Reason (the mapping model's own account)"),
    ).not.toBeInTheDocument();
  });

  it('renders no unmapped-target section when the list is empty', () => {
    const noUnsourced: Pipeline = {
      ...pipelineWithUnsourcedTargets,
      schema_history: [
        mockPipeline.schema_history![0],
        {
          ...mockPipeline.schema_history![1],
          mapping_details: {
            mappings: [{ source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 }],
            unmapped_source_fields: [],
            unmapped_target_fields: [],
          },
        },
      ],
    };

    renderPage(noUnsourced);

    // The empty list must stay empty rather than gaining a placeholder entry:
    // `unsourcedTargetFields` returns `[]` for an absent or empty `target`, which
    // is what keeps this page's behaviour unchanged for the records that have
    // nothing unsourced.
    expect(screen.queryAllByTestId('unmapped-target')).toHaveLength(0);
    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
  });
});
