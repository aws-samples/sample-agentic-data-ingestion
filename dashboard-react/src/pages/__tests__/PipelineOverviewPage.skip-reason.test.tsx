// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Overview card's skip description — p54 task 1.3 (_Requirements: 7.1, 7.2, 7.3_).
 *
 * **The drift this pins closed.** `PipelineOverviewPage.tsx`'s status cell never
 * called `describeSkip`, so it rendered the run's internal `current_step` in a
 * parenthetical: a duplicate skip read **"SKIPPED (duplicate_check)"** on the
 * Overview card and **"Skipped: duplicate content"** on the Executions tab
 * (`ExecutionsPage.tsx:298`) — one outcome, two vocabularies. R7.1 makes both
 * surfaces read from the one derivation; R7.2 forbids the internal step name
 * standing in for the reason.
 *
 * **The asymmetry in R7.2 is the thing most likely to be broken later**, so it
 * gets its own assertion: the badge replaces the parenthetical for a skip row
 * **only**. On a `RUNNING` row `current_step` is the step in flight and is the
 * one place this five-row preview says where a run has got to, so it stays.
 *
 * **Never keyed on the run id** — two id shapes exist and one is `run-`-prefixed
 * deliberately (`skipped-{fingerprint}` for a duplicate,
 * `run-{window}-skip-{reason}` for contention), so the fixtures below use both
 * shapes and neither is what decides the rendering. `status` + `skip_reason` is.
 *
 * The label *text* is not restated here — `utils/__tests__/skip-reason.test.ts`
 * owns the reason table, and duplicating its strings would create the second
 * source of skip copy that R7.1 exists to remove. What this file asserts is that
 * the card renders `describeSkip`'s answer rather than its own.
 *
 * `api-client` is mocked **without** `importOriginal`, matching the sibling
 * footer suite: this file must not execute the real module, which a concurrent
 * task is editing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { describeSkip } from '../../utils/skip-reason';
import type { Pipeline } from '../../types';

type Row = Record<string, unknown>;

/** The producer's envelope (`pipelines_runs.py:1148-1176`), nested per `LGR-2bcdd0d-1`. */
function envelope(executions: Row[]) {
  return {
    executions,
    queued_files: [],
    pagination: {
      page: 1,
      page_size: 10,
      total_count: executions.length,
      total_pages: 1,
      has_next: false,
      has_prev: false,
      next_cursor: null,
      partial_counts: [] as string[],
    },
  };
}

let rows: Row[] = [];

vi.mock('../../hooks', () => ({
  usePipelineExecutions: (
    _key: unknown,
    _page: unknown,
    _cursor: unknown,
    status?: string | null,
  ) => {
    if (status) return { data: envelope(rows.filter((r) => r.status === status)) };
    return { data: envelope(rows) };
  },
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  usePipelinePendingApprovals: () => ({ data: [] }),
  useDataPreview: () => ({ data: undefined, isLoading: false, isError: false, error: null }),
  usePipelineCosts: () => ({ data: undefined, isLoading: true, isError: false, refetch: vi.fn() }),
  useBedrockModels: () => ({ data: [] }),
  useCreatePipeline: () => ({ mutateAsync: vi.fn() }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useSchemas: () => ({ data: [], isLoading: false }),
  useGlueDatabases: () => ({ data: [], isLoading: false }),
  useTablesInDatabase: () => ({ data: [], isLoading: false }),
  useS3TablesBuckets: () => ({ data: [], isLoading: false }),
  useTablesInBucket: () => ({ data: [], isLoading: false }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../services/api-client', () => ({
  ApiError: class ApiError extends Error {},
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

import PipelineOverviewPage from '../PipelineOverviewPage';

const PIPELINE_KEY = 'latesttesting-19ca826a';

const mockPipeline: Pipeline = {
  pipeline_key: PIPELINE_KEY,
  pipeline_name: 'latesttesting',
  status: 'active',
  source_schema: 'customer',
  target_schema: 'customer_target',
  created_at: '2026-08-20T17:00:00Z',
  updated_at: '2026-08-24T13:48:26Z',
  ai_model: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  schema_history: [],
  all_files: [],
};

/**
 * A duplicate skip, in the `skipped-{fingerprint}` id shape, carrying the
 * internal gate name in `current_step` — which is exactly what the card used to
 * render as the reason.
 */
const DUPLICATE_SKIP: Row = {
  run_id: 'skipped-7412636fccb938719673179658c05773',
  file_name: 'customer_dupe.csv',
  started_at: '2026-08-26T12:00:00Z',
  status: 'SKIPPED',
  skip_reason: 'duplicate_content',
  skip_detail: 'Content already processed by run run-2026-08-25-11-02-33-9f1c2e.',
  current_step: 'duplicate_check',
};

/**
 * A lock-contention skip in the *other* id shape — `run-`-prefixed on purpose
 * (`LGR-21a1b5b-4`), which is why nothing here may key on the prefix.
 */
const CONTENTION_SKIP: Row = {
  run_id: 'run-2026-08-26-11-30-00-skip-pipeline_lock_contention',
  file_name: 'customer_sched.csv',
  started_at: '2026-08-26T11:30:00Z',
  status: 'SKIPPED',
  skip_reason: 'pipeline_lock_contention',
  current_step: 'pipeline_lock_gate',
};

/** A skip with no reason recorded — `describeSkip` still answers for it. */
const REASONLESS_SKIP: Row = {
  run_id: 'skipped-0000000000000000',
  file_name: 'customer_mystery.csv',
  started_at: '2026-08-26T10:00:00Z',
  status: 'SKIPPED',
  current_step: 'duplicate_check',
};

/** A run in flight: `current_step` here is real information and must survive. */
const RUNNING_ROW: Row = {
  run_id: 'run-2026-08-26-09-00-00-aabbcc',
  file_name: 'customer_live.csv',
  started_at: '2026-08-26T09:00:00Z',
  status: 'RUNNING',
  current_step: 'create_code',
};

function renderWith(executions: Row[]) {
  rows = executions;
  return render(
    <MemoryRouter>
      <PipelineProvider
        value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
      >
        <PipelineOverviewPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  cleanup();
});

describe('PipelineOverviewPage — the skip description (p54 task 1.3)', () => {
  it('describes a skip with the same derivation the Executions tab uses (R7.1)', () => {
    renderWith([DUPLICATE_SKIP]);

    // The expectation is read from `describeSkip` itself, not transcribed: a
    // test carrying its own copy of the label would let the two surfaces drift
    // again while still passing.
    const expected = describeSkip(
      DUPLICATE_SKIP.status as string,
      DUPLICATE_SKIP.skip_reason as string,
      DUPLICATE_SKIP.skip_detail as string,
    );
    expect(expected).not.toBeNull();
    expect(screen.getByTestId('skip-reason-badge')).toHaveTextContent(expected!.label);
  });

  it('does not display the internal step name as the skip reason (R7.2)', () => {
    renderWith([DUPLICATE_SKIP]);

    // The parenthetical the badge replaces. `duplicate_check` is the gate that
    // made the decision, not anything an operator can act on.
    expect(screen.queryByText('(duplicate_check)')).not.toBeInTheDocument();
    expect(screen.queryByText(/duplicate_check/)).not.toBeInTheDocument();
  });

  it('answers for a skip in either id shape, and for one with no reason', () => {
    // Never derive skip-ness from the run id: `CONTENTION_SKIP` is
    // `run-`-prefixed and `REASONLESS_SKIP` carries no reason at all, so a
    // prefix test or a reason-table lookup alone would render nothing for one
    // of them.
    renderWith([CONTENTION_SKIP, REASONLESS_SKIP]);

    expect(screen.getAllByTestId('skip-reason-badge')).toHaveLength(2);
    expect(screen.queryByText('(pipeline_lock_gate)')).not.toBeInTheDocument();
    expect(screen.queryByText('(duplicate_check)')).not.toBeInTheDocument();
  });

  it('keeps the step parenthetical on a row that is not a skip (R7.2 is one-sided)', () => {
    // The badge replaces the parenthetical for skip rows ONLY. On a running run
    // `current_step` names the step in flight, and dropping it here would be a
    // regression dressed up as consistency.
    renderWith([RUNNING_ROW]);

    expect(screen.getByText('(create_code)')).toBeInTheDocument();
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
  });

  it('shows the reason beside the status, not instead of it', () => {
    // A skip must still read as SKIPPED — the badge is additive to the status
    // indicator, the same arrangement the Executions tab uses.
    renderWith([DUPLICATE_SKIP]);

    expect(screen.getByText('SKIPPED')).toBeInTheDocument();
    expect(screen.getByTestId('skip-reason-badge')).toBeInTheDocument();
  });

  it('carries the recorded detail as hover text, so the words are not lost', () => {
    // `describeSkip` folds `skip_detail` into `detail`; the card must surface it
    // rather than drop it, since for a duplicate that is the sentence naming the
    // run that already processed the content.
    renderWith([DUPLICATE_SKIP]);

    const expected = describeSkip(
      DUPLICATE_SKIP.status as string,
      DUPLICATE_SKIP.skip_reason as string,
      DUPLICATE_SKIP.skip_detail as string,
    );
    const badge = screen.getByTestId('skip-reason-badge');
    expect(badge.closest('[title]')).toHaveAttribute('title', expected!.detail);
  });

  it('leaves the five-row preview and its lack of a pager alone', () => {
    // Guard on this task's blast radius: the four assertions in
    // `recent-runs-order.test.tsx` / `running-count.test.tsx` are the real
    // regression fence, and this restates the part they share in one place.
    renderWith([DUPLICATE_SKIP, CONTENTION_SKIP, REASONLESS_SKIP, RUNNING_ROW]);

    expect(screen.getAllByRole('link', { name: 'View Run' })).toHaveLength(4);
    expect(screen.queryByRole('button', { name: /Next page/i })).not.toBeInTheDocument();
  });
});
