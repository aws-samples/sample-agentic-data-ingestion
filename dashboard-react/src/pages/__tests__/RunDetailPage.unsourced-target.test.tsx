// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45 task 16.1 — the run-detail Schema tab stops discarding the Unmapped_Entry.
 *
 * `RunDetailPage` builds the run's `MappingData` from the `schema_mapping` step's
 * `output_data.approval` payload. Until p45 the target half of that build was
 * `.map(f => typeof f === 'string' ? f : f.field as string)`, which threw
 * `default_value` and `reason` away one line before the component that needs
 * them, and never read the two Unsourced_Keys at all. It now goes through
 * `unsourcedTargetFields`.
 *
 * **The Unsourced_Keys are available on this surface**, which is why this is R1's
 * ground and not just R4.4's. `store_mapping_result`
 * (`schema_mapping/lambda_function.py:1533`) calls `create_approval_payload` and
 * writes the whole approval object onto the state row at `:1578`, so the run
 * detail page reads the *same* payload the approval panel does.
 *
 * **Expectations are derived from the producer, never from
 * `unsourcedTargetFields`' own output** — `LGR-28b4d75-5` (**template /
 * ratified**): a test written from the thing under test cannot detect it
 * drifting, because the output is its own oracle. The producer is
 * `create_approval_payload`'s `unmapped_fields` dict literal
 * (`schema_mapping/lambda_function.py:1391-1396`): `target` carries the detail
 * objects, `target_unsourced` carries the names required-first (`:1199-1200`),
 * and `target_required` is the subset of that roster the target schema declares
 * required (`:1353`).
 *
 * **What this file asserts is the construction, not the rendering.** The three
 * facts per entry are rendered by `MappingViewer` under p45 task 13.2, so there
 * is nothing on screen yet that carries a `requirement`; what is observable here
 * is the `MappingData` the page hands to `SchemaTab`. `SchemaTab` is therefore
 * wrapped rather than replaced, and the real component still renders inside the
 * wrapper — which is what lets the last two cases assert `SchemaTab`'s own
 * empty-state against the real component.
 *
 * _Requirements: 4.4, 1.5, 1.7, 2.1, 7.5_
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { MappingData, RunDetail } from '../../types';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

/* ------------------------------------------------------------------ */
/*  Read SchemaTab's props off the committed DOM                       */
/* ------------------------------------------------------------------ */

type CapturedSchemaTabProps = { mappingData: MappingData | null } & Record<string, unknown>;

/**
 * `SchemaTab` is wrapped rather than replaced: the wrapper serialises the
 * `mappingData` it was handed into a `data-` attribute and then renders the
 * **real** component, so the same test can read the construction and assert
 * `SchemaTab`'s own empty-state (`run-detail/SchemaTab.tsx:90-93`) unchanged.
 *
 * The serialisation is deliberately a DOM side-channel rather than a captured
 * props array or a `vi.fn()` spy. Both of those were tried and both record an
 * extra invocation carrying no props on the first mount of the page — an
 * invocation that never reaches the real component (nothing in the committed
 * tree renders from it) and that made "the last props the page passed" read
 * back `undefined`. The DOM only ever shows a committed render, so reading it
 * cannot see that phantom.
 */
vi.mock('../../components/run-detail/SchemaTab', async () => {
  const actual = await vi.importActual<{ default: (p: CapturedSchemaTabProps) => unknown }>(
    '../../components/run-detail/SchemaTab',
  );
  const { createElement } = await import('react');
  const Real = actual.default as Parameters<typeof createElement>[0];
  return {
    default: (props: CapturedSchemaTabProps) =>
      createElement(
        'div',
        {
          'data-testid': 'schema-tab-under-test',
          // A `null` `mappingData` serialises as "null", which is the value the
          // page passes when its `mappings.length` guard declines to build one.
          // `RunDetailPage` always passes the prop, so collapsing an absent one
          // to `null` here loses nothing this test needs to tell apart.
          'data-mapping-data': JSON.stringify(props?.mappingData ?? null),
        },
        createElement(Real, props),
      ),
  };
});

/* ------------------------------------------------------------------ */
/*  Fixtures — shaped as `create_approval_payload` writes them         */
/* ------------------------------------------------------------------ */

const PROPOSED_MAPPINGS = [
  { source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 },
];

/**
 * `unmapped_fields.target` — the producer's `{field, default_value, reason}`
 * objects (`:1123-1127`, `:885-889`), one bare-string entry (reachable: the
 * producer tolerates both shapes at `:977-994`), and deliberately in a
 * *different* order from the roster so the ordering authority is observable.
 */
const UNMAPPED_TARGET_DETAIL = [
  { field: 'nickname', default_value: '', reason: 'No matching source field found' },
  { field: 'mobile_phone', default_value: null, reason: 'phone is the landline; duplicating it would invent data' },
  'legacy_code',
];

/** Required first, as `unsourced_target_field_names` emits it (`:1199-1200`). */
const TARGET_UNSOURCED = ['mobile_phone', 'legacy_code', 'nickname'];

/** The roster subset the target schema declares required (`:1353`). */
const TARGET_REQUIRED = ['mobile_phone'];

interface StepOverrides {
  proposed_mappings?: unknown[];
  unmapped_fields?: Record<string, unknown>;
}

function runWithMappingStep(overrides: StepOverrides = {}): RunDetail {
  return {
    run_id: 'run-unsourced-1',
    pipeline_key: 'pipe-1',
    status: 'SUCCEEDED',
    started_at: '2024-06-01T10:00:00Z',
    approvals: [],
    steps: [
      {
        step_name: 'schema_mapping',
        status: 'completed',
        mapping_id: 'map-current',
        output_data: {
          approval: {
            proposed_mappings: 'proposed_mappings' in overrides ? overrides.proposed_mappings : PROPOSED_MAPPINGS,
            // `in`, not `?? default`: one case below needs the key **absent**,
            // which is C9's degraded payload rather than an empty one.
            ...('unmapped_fields' in overrides
              ? { unmapped_fields: overrides.unmapped_fields }
              : {
                  unmapped_fields: {
                    source: ['legacy_notes'],
                    target: UNMAPPED_TARGET_DETAIL,
                    target_unsourced: TARGET_UNSOURCED,
                    target_required: TARGET_REQUIRED,
                  },
                }),
          },
        },
      },
      { step_name: 'schema_extract', status: 'completed', extracted_schema: '{"type":"object"}' },
    ],
  } as unknown as RunDetail;
}

let currentRun: RunDetail = runWithMappingStep();

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: currentRun,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: undefined }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
  // Reached through the rendered SchemaTab subtree.
  useUpdateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, error: null }),
}));

import RunDetailPage from '../RunDetailPage';

function renderSchemaTab(run: RunDetail) {
  currentRun = run;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const result = render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-unsourced-1']}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  // Only the active tab's content is mounted, so the Schema tab has to be opened
  // before `SchemaTab` receives anything.
  fireEvent.click(screen.getByText('Schema & Mappings'));
  return result;
}

/** The `MappingData` the page handed to `SchemaTab` in the committed render. */
function builtMappingData(): MappingData | null {
  const serialised = screen.getByTestId('schema-tab-under-test').getAttribute('data-mapping-data');
  expect(serialised).not.toBeNull();
  return JSON.parse(serialised!) as MappingData | null;
}

beforeEach(() => {
  currentRun = runWithMappingStep();
});

/* ------------------------------------------------------------------ */
/*  Tests                                                             */
/* ------------------------------------------------------------------ */

describe('RunDetailPage — the construction reads both Unsourced_Keys (R4.4, R1.7, R2.1)', () => {
  it('carries one entry per target_unsourced name, requirement from target_required only', () => {
    renderSchemaTab(runWithMappingStep());

    const built = builtMappingData();
    expect(built).not.toBeNull();
    // R1.7 — every roster name, optional ones included. A filtered list would be
    // a filter an approver cannot see.
    expect(built!.unmapped_target.map((e) => e.field)).toEqual(TARGET_UNSOURCED);
    // R1.3, R2.1 — `target_required` membership is the only authority.
    expect(built!.unmapped_target.map((e) => e.requirement)).toEqual([
      'required',
      'optional',
      'optional',
    ]);
  });

  it('preserves target_unsourced order rather than the detail list order (R1.5)', () => {
    renderSchemaTab(runWithMappingStep());

    const names = builtMappingData()!.unmapped_target.map((e) => e.field);
    // The fixture's `target` detail order is nickname, mobile_phone, legacy_code.
    // The producer's required-first roster order is the one that must survive
    // (`:1199-1200`, do-NOT 13) — re-sorting anywhere here would make the
    // frontend a second ordering authority.
    expect(names).toEqual(['mobile_phone', 'legacy_code', 'nickname']);
    expect(names).not.toEqual(UNMAPPED_TARGET_DETAIL.map((e) => (typeof e === 'string' ? e : e.field)));
  });

  it('keeps default_value and reason from the matching detail entry', () => {
    renderSchemaTab(runWithMappingStep());

    const byName = new Map(builtMappingData()!.unmapped_target.map((e) => [e.field, e]));

    // A recorded null default is a recorded default (R3.3) — present, and null.
    expect('default_value' in byName.get('mobile_phone')!).toBe(true);
    expect(byName.get('mobile_phone')!.default_value).toBeNull();
    expect(byName.get('mobile_phone')!.reason).toBe(
      'phone is the landline; duplicating it would invent data',
    );

    // A recorded empty-string default is also a recorded default.
    expect(byName.get('nickname')!.default_value).toBe('');
    expect(byName.get('nickname')!.reason).toBe('No matching source field found');

    // A bare-string entry records neither (R3.6, R4.6).
    expect('default_value' in byName.get('legacy_code')!).toBe(false);
    expect(byName.get('legacy_code')!.reason).toBeUndefined();
  });

  it('leaves unmapped_source as bare names (do-NOT 19)', () => {
    renderSchemaTab(runWithMappingStep());

    expect(builtMappingData()!.unmapped_source).toEqual(['legacy_notes']);
  });
});

describe('RunDetailPage — no Unsourced_Keys is the common case, not an edge case (R1.4, R2.6)', () => {
  it('falls back to target names with every entry not_established', () => {
    // Per C15 this is what every account produces until p37's Compute +
    // AgentCore deploy lands, so the branch is the live one.
    renderSchemaTab(
      runWithMappingStep({
        unmapped_fields: { source: [], target: UNMAPPED_TARGET_DETAIL },
      }),
    );

    const entries = builtMappingData()!.unmapped_target;
    // Roster is `target`'s own names in `target`'s order.
    expect(entries.map((e) => e.field)).toEqual(['nickname', 'mobile_phone', 'legacy_code']);
    expect(entries.map((e) => e.requirement)).toEqual([
      'not_established',
      'not_established',
      'not_established',
    ]);
    // do-NOT 5 — Not_Established is a third fact, never the permissive one.
    expect(entries.some((e) => e.requirement === 'optional')).toBe(false);
    // The detail still reaches the component; only the requirement is withheld.
    expect(entries[1].default_value).toBeNull();
  });

  it('carries no entries when the payload has no unmapped_fields at all', () => {
    // C9's degraded return (`:1435-1439`), and every pre-p37 record. Not pinned
    // as intended behaviour (do-NOT 10, `LGR-b2f79ef-13`) — only that the page
    // does not throw and renders an honest empty list.
    renderSchemaTab(runWithMappingStep({ unmapped_fields: undefined }));

    expect(builtMappingData()!.unmapped_target).toEqual([]);
  });
});

describe('RunDetailPage — the mappings.length gate is unchanged (design §4.11, R7.5)', () => {
  it('builds no MappingData when proposed_mappings is empty, even with unsourced target fields', () => {
    renderSchemaTab(runWithMappingStep({ proposed_mappings: [] }));

    // Deliberately not relaxed: relaxing it would change which runs render a
    // mapping panel at all, which no p45 criterion authorises. The comment at
    // the guard records it as a follow-on.
    expect(builtMappingData()).toBeNull();
  });

  it('still renders the existing empty-state text, unchanged', () => {
    renderSchemaTab(runWithMappingStep({ proposed_mappings: [] }));

    // `run-detail/SchemaTab.tsx:90-93`, reached through the real component.
    expect(screen.getByText('No mapping data available for this run.')).toBeInTheDocument();
    // R7.5, C7, `LGR-99050e6-3` — no affordance changes on this surface.
    expect(screen.queryByRole('button', { name: /edit mappings/i })).not.toBeInTheDocument();
  });
});
