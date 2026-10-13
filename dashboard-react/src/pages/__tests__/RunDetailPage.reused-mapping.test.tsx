// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 4.3 — the page-level wiring of a Reuse_Run's Schema tab, plus R2.4.
 *
 * `SchemaTab.reused-mapping.test.tsx` covers the component's three arms. This
 * file covers what only the page can be wrong about:
 *
 *   1. that the reused mapping's fields are sourced from the run's OWN
 *      `schema_mapping` approval record (`approvals[].details`), which is where
 *      p54 task 2.2 puts the origin run's `output_data.approval` verbatim;
 *   2. that the reuse pair reaches `SchemaTab` from `runDetail`;
 *   3. that a run which is not a Reuse_Run is byte-identical to before — the
 *      reuse projection is gated on `reused_mapping === true`, not on the
 *      absence of a mapping, so an own-step run with an empty
 *      `proposed_mappings` still renders the existing absence sentence;
 *   4. **R2.4 — a Reuse_Run still lists the AI traces it produced itself.**
 *
 * **R2.4 holds BY CONSTRUCTION and nothing in this task changes it.** Read end
 * to end: `agent_trace_plugin.py:530-545` writes `{run_id, step_name}`,
 * `agent_traces.py:66-80` queries `Key("run_id").eq(run_id)`, and
 * `AiTracesTab.tsx:35-37` iterates the run's own steps returning `null` where
 * there is no record. On a Reuse_Run `create_code`, `test_code` and
 * `create_glue_pipeline` all execute; only `schema_mapping` does not. So the
 * assertion below is a guard against a future change miscrediting them, not a
 * fix — trace collection, storage and the `AiTracesTab` read are untouched.
 *
 * _Requirements: 1.3, 2.1, 2.2, 2.3, 2.4_
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { AgentTraceData, AgentTraceListResponse, RunDetail } from '../../types';
import {
  REUSED_MAPPING_STATEMENT,
  SCHEMA_MAPPING_STEP_ABSENT_STATEMENT,
  REUSED_MAPPING_FIELDS_UNAVAILABLE,
} from '../../components/run-detail/SchemaTab';
import { FALLBACK_MODEL_DISPLAY, FALLBACK_MODEL_SENTINEL } from '../../utils/formatting';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

/* ------------------------------------------------------------------ */
/*  Fixtures — a Reuse_Run as the wire presents one                    */
/* ------------------------------------------------------------------ */

const REUSED_MAPPING_ID = 'map-reused-0001';

/**
 * The reuse Approval_Payload, as `_reused_mapping_payload` assembles it: the
 * origin run's own ten keys plus the three reuse keys appended
 * (`approval_handler/lambda_function.py`). `get_run_details` parses the stored
 * `approval_data` JSON into `approvals[].details`, so this is that dict.
 */
const REUSE_APPROVAL_DETAILS: Record<string, unknown> = {
  source_schema: { fields: [{ name: 'cust_id', type: 'string' }] },
  target_schema: { fields: [{ name: 'customer_id', type: 'string' }] },
  proposed_mappings: [
    { source_field: 'cust_id', target_field: 'customer_id', confidence: 0.95 },
    { source_field: 'fname', target_field: 'first_name', confidence: 0.9 },
  ],
  confidence_scores: null,
  unmapped_fields: { source: ['legacy_notes'], target: ['nickname'] },
  mapping_summary: null,
  evidence: null,
  questions: null,
  generated_by: 'bedrock',
  mapping_count: 2,
  reused_mapping: true,
  reused_mapping_id: REUSED_MAPPING_ID,
  reused_from_run_id: 'run-origin-0001',
};

function emptyTrace(): AgentTraceData {
  return {
    model_calls: [],
    tool_calls: [],
    total_input_tokens: 120,
    total_output_tokens: 80,
    total_cache_read_tokens: 0,
    estimated_cost_usd: 0.0012,
    start_time: 1,
    end_time: 2,
    model_id: 'anthropic.claude-3-5-haiku',
    error: null,
  };
}

/**
 * A Reuse_Run: **no `schema_mapping` step at all** (the `ReuseExistingMapping`
 * Pass writes nothing), but `create_code` and `test_code` both ran — which is
 * why R2.4's traces are genuinely this run's work.
 */
function reuseRun(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: 'run-reuse-0001',
    pipeline_key: 'pipe-1',
    status: 'SUCCEEDED',
    started_at: '2024-06-01T10:00:00Z',
    reused_mapping: true,
    reused_mapping_id: REUSED_MAPPING_ID,
    approvals: [
      {
        approval_id: 'appr-1',
        pipeline_key: 'pipe-1',
        run_id: 'run-reuse-0001',
        approval_type: 'schema_mapping',
        status: 'approved',
        created_at: '2024-06-01T10:01:00Z',
        details: REUSE_APPROVAL_DETAILS,
      },
    ],
    steps: [
      { step_name: 'schema_extract', status: 'completed', extracted_schema: '{"type":"object"}' },
      { step_name: 'create_code', status: 'completed', code_version_id: 'code-1' },
      { step_name: 'test_code', status: 'completed' },
    ],
    ...overrides,
  } as unknown as RunDetail;
}

/** A run that produced its own mapping — the non-reuse control. */
function ownMappingRun(proposedMappings: unknown[]): RunDetail {
  return {
    run_id: 'run-own-0001',
    pipeline_key: 'pipe-1',
    status: 'SUCCEEDED',
    started_at: '2024-06-01T10:00:00Z',
    reused_mapping: false,
    approvals: [
      {
        approval_id: 'appr-2',
        pipeline_key: 'pipe-1',
        run_id: 'run-own-0001',
        approval_type: 'schema_mapping',
        status: 'approved',
        created_at: '2024-06-01T10:01:00Z',
        // Populated on purpose: the reuse projection must NOT read this for a
        // run that is not a Reuse_Run.
        details: REUSE_APPROVAL_DETAILS,
      },
    ],
    steps: [
      { step_name: 'schema_extract', status: 'completed', extracted_schema: '{"type":"object"}' },
      {
        step_name: 'schema_mapping',
        status: 'completed',
        mapping_id: 'map-own',
        output_data: { approval: { proposed_mappings: proposedMappings } },
      },
    ],
  } as unknown as RunDetail;
}

let currentRun: RunDetail = reuseRun();
let currentTraces: AgentTraceListResponse | null = null;
let currentPipeline: Record<string, unknown> = { mapping_id: 'map-pipeline-current' };

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
  useRunTraces: () => ({ data: currentTraces, isLoading: false, isError: false, refetch: vi.fn() }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: undefined }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: currentPipeline, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
  useUpdateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, error: null }),
  // Reached through the Step Flow tab, which is the default active tab:
  // `StepFlowTab` → `DataPreviewTable` → `useDataPreview`.
  useDataPreview: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
}));

import RunDetailPage from '../RunDetailPage';

function renderPage(run: RunDetail, traces: AgentTraceListResponse | null = null) {
  currentRun = run;
  currentTraces = traces;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/pipelines/pipe-1/runs/${run.run_id}`]}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function openSchemaTab() {
  fireEvent.click(screen.getByText('Schema & Mappings'));
}

beforeEach(() => {
  currentTraces = null;
  currentPipeline = { mapping_id: 'map-pipeline-current' };
});

/** The text of `MappingViewer`'s "AI Model" cell. */
function aiModelCell(): string {
  const label = screen.getByText('AI Model');
  return label.nextElementSibling?.textContent ?? '';
}

/* ------------------------------------------------------------------ */
/*  R1.3, R2.1-R2.3 — the page hands the reused mapping over          */
/* ------------------------------------------------------------------ */

describe('RunDetailPage — a Reuse_Run\'s Schema tab (p54 R1.3, R2.1, R2.2, R2.3)', () => {
  it("projects the reused mapping's fields out of the schema_mapping approval record (R1.3)", () => {
    renderPage(reuseRun());
    openSchemaTab();

    // Pre-p54 this tab rendered "No mapping data available for this run.",
    // because the walk over `steps` finds no `schema_mapping` row.
    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(screen.queryByText('No mapping data available for this run.')).not.toBeInTheDocument();
  });

  it('states the reuse, names the mapping, and states the absent step (R2.1, R2.2, R2.3)', () => {
    renderPage(reuseRun());
    openSchemaTab();

    const note = screen.getByTestId('schema-reused-mapping');
    expect(note.textContent).toContain(REUSED_MAPPING_STATEMENT);
    expect(note.textContent).toContain(REUSED_MAPPING_ID);
    expect(note.textContent).toContain(SCHEMA_MAPPING_STEP_ABSENT_STATEMENT);
  });

  it('states the fields could not be read when the payload was marked unreadable', () => {
    // The unreadable arm of `_reused_mapping_payload`: `proposed_mappings` is
    // `None`, never `[]` and never `{}`.
    renderPage(
      reuseRun({
        approvals: [
          {
            approval_id: 'appr-1',
            pipeline_key: 'pipe-1',
            run_id: 'run-reuse-0001',
            approval_type: 'schema_mapping',
            status: 'pending',
            created_at: '2024-06-01T10:01:00Z',
            details: {
              reused_mapping: true,
              reused_mapping_id: REUSED_MAPPING_ID,
              mapping_unreadable: true,
              proposed_mappings: null,
              mapping_count: null,
            },
          },
        ],
      } as unknown as Partial<RunDetail>),
    );
    openSchemaTab();

    expect(screen.getByText(REUSED_MAPPING_FIELDS_UNAVAILABLE)).toBeInTheDocument();
    expect(screen.queryByTestId('mapping-viewer')).not.toBeInTheDocument();
    // The reuse is still stated, so the absence carries its reason.
    expect(screen.getByText(REUSED_MAPPING_STATEMENT)).toBeInTheDocument();
  });

  it('does not offer the mapping editor on a Reuse_Run (LGR-99050e6-3)', () => {
    renderPage(reuseRun());
    openSchemaTab();

    expect(screen.queryByRole('button', { name: /edit mappings/i })).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/*  The AI Model cell names the model that produced the mapping       */
/* ------------------------------------------------------------------ */

/** `reuseRun()` with the reused payload's `generated_by` replaced. */
function reuseRunGeneratedBy(generatedBy: unknown): RunDetail {
  const run = reuseRun();
  const [approval] = run.approvals ?? [];
  return {
    ...run,
    approvals: [{ ...approval, details: { ...REUSE_APPROVAL_DETAILS, generated_by: generatedBy } }],
  } as unknown as RunDetail;
}

describe("RunDetailPage — the AI Model is the mapping's own generated_by", () => {
  // Observed on 2026-10-10: a run that reused a Sonnet 5.5 mapping showed
  // "AI Model: Unknown", although the reused payload carried
  // `generated_by: "us.anthropic.claude-sonnet-5-5"`.
  it('names the model on a Reuse_Run', () => {
    renderPage(reuseRunGeneratedBy('us.anthropic.claude-sonnet-5-5'));
    openSchemaTab();

    expect(aiModelCell()).toBe('Claude Sonnet 5.5');
  });

  it("never names the pipeline's current model for a reused mapping", () => {
    currentPipeline = { mapping_id: 'map-pipeline-current', ai_model: 'us.anthropic.claude-opus-4-7' };
    renderPage(reuseRunGeneratedBy(undefined));
    openSchemaTab();

    expect(aiModelCell()).toBe('Unknown');
  });

  it('states that a reused fallback mapping used no model', () => {
    renderPage(reuseRunGeneratedBy(FALLBACK_MODEL_SENTINEL));
    openSchemaTab();

    expect(aiModelCell()).toBe(FALLBACK_MODEL_DISPLAY);
  });

  it("names the run's own mapping by its generated_by, not by the pipeline's model", () => {
    // Run 1 of the same pipeline: a fallback mapping on a pipeline set to Sonnet 5.5
    // used to read "Claude Sonnet 5.5" here.
    currentPipeline = { mapping_id: 'map-pipeline-current', ai_model: 'anthropic.claude-sonnet-5-5' };
    const run = ownMappingRun([{ source_field: 'a', target_field: 'b', confidence: 0.5 }]);
    const [extract, mapping] = run.steps;
    const withProvenance = {
      ...run,
      steps: [
        extract,
        {
          ...mapping,
          output_data: {
            approval: {
              proposed_mappings: [{ source_field: 'a', target_field: 'b', confidence: 0.5 }],
              generated_by: FALLBACK_MODEL_SENTINEL,
            },
          },
        },
      ],
    } as unknown as RunDetail;
    renderPage(withProvenance);
    openSchemaTab();

    expect(aiModelCell()).toBe(FALLBACK_MODEL_DISPLAY);
  });

  it("falls back to the pipeline's model for an own mapping whose payload names none", () => {
    currentPipeline = { mapping_id: 'map-pipeline-current', ai_model: 'anthropic.claude-sonnet-5-5' };
    renderPage(ownMappingRun([{ source_field: 'a', target_field: 'b', confidence: 0.5 }]));
    openSchemaTab();

    expect(aiModelCell()).toBe('Claude Sonnet 5.5');
  });
});

/* ------------------------------------------------------------------ */
/*  Non-regression — a run that is not a Reuse_Run is unchanged       */
/* ------------------------------------------------------------------ */

describe('RunDetailPage — the reuse projection is gated on the reuse fact, not on absence', () => {
  it("renders the run's OWN mapping and no reuse note when the run produced one", () => {
    renderPage(ownMappingRun([{ source_field: 'a', target_field: 'b', confidence: 0.5 }]));
    openSchemaTab();

    expect(screen.getByTestId('mapping-viewer')).toBeInTheDocument();
    expect(screen.queryByTestId('schema-reused-mapping')).not.toBeInTheDocument();
  });

  it('keeps the existing absence sentence for an own-step run whose mapping proposed nothing', () => {
    // The approval record on this fixture DOES carry two `proposed_mappings`.
    // Reading it here would change which runs render a mapping panel at all —
    // exactly what the `reused_mapping === true` gate prevents.
    renderPage(ownMappingRun([]));
    openSchemaTab();

    expect(screen.getByText('No mapping data available for this run.')).toBeInTheDocument();
    expect(screen.queryByTestId('mapping-viewer')).not.toBeInTheDocument();
    expect(screen.queryByTestId('schema-reused-mapping')).not.toBeInTheDocument();
  });
});

/* ------------------------------------------------------------------ */
/*  R2.4 — a Reuse_Run still lists its own AI traces                  */
/* ------------------------------------------------------------------ */

describe('RunDetailPage — a Reuse_Run lists the AI traces it produced itself (p54 R2.4)', () => {
  it('lists a trace for every step the run actually ran, and none for the step it skipped', () => {
    renderPage(reuseRun(), {
      run_id: 'run-reuse-0001',
      traces: [
        { run_id: 'run-reuse-0001', step_name: 'create_code', pipeline_id: 'pipe-1', trace: emptyTrace() },
        { run_id: 'run-reuse-0001', step_name: 'test_code', pipeline_id: 'pipe-1', trace: emptyTrace() },
      ],
    });

    fireEvent.click(screen.getByText('AI Traces'));

    // `AgentTraceTimeline` keys its section on the step name it was handed.
    expect(screen.getByTestId('agent-trace-create_code')).toBeInTheDocument();
    expect(screen.getByTestId('agent-trace-test_code')).toBeInTheDocument();
    expect(screen.getByText('AI Trace: Create Code')).toBeInTheDocument();
    // Nothing is miscredited: the reuse produced no schema-mapping trace and
    // none is invented for it.
    expect(screen.queryByTestId('agent-trace-schema_mapping')).not.toBeInTheDocument();
    expect(screen.queryByText('No AI traces available for this run.')).not.toBeInTheDocument();
  });
});
