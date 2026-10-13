// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p51 task 4.1 — the overview's awaiting-approval set comes from the keyed
 * approvals read, not from one page of ten execution rows.
 *
 * Three things are pinned here, and the fixtures are built so that each one
 * fails loudly if the source reverts to `latestExecs`:
 *
 * **P2 — the count and the controls describe the same set.** Over a generated
 * set of approvals *and* an independently generated executions page, the number
 * of rendered Approve buttons equals the number the page renders beside them.
 * The executions generator deliberately emits `WAITING_APPROVAL` rows carrying a
 * `pending_approval_id` — the exact shape the old derivation counted — so a
 * page-scoped count cannot satisfy this property by coincidence.
 * **Validates: Requirements 1.1, 1.2**
 *
 * **P3 — an off-page approval still has its affordance.** The approvals fixture
 * is *disjoint* from the executions fixture: no `run_id` in one appears in the
 * other. This is the case no existing test covered. A second test covers the
 * live variant found by task 1.2, where the run *is* on the page but its
 * top-level status is `FAILED` — 2 of 2 live pending approvals are in that state,
 * and the old top-level `status === 'WAITING_APPROVAL'` filter rendered zero
 * controls for both.
 * **Validates: Requirements 1.1**
 *
 * **D4 — the optimistic pending state is unchanged.** `pendingApprovalId` still
 * gates the per-row loading state, both `mutate` calls still send exactly
 * `{ id, action, comment: '' }`, and their `onSuccess` / `onError` still clear the
 * state and raise the same four toasts. The approvals row now carries `run_id`,
 * and it is deliberately **not** threaded into `mutate` as `runId`: R1.5 asks for
 * today's behaviour, not an improvement on it.
 * **Validates: Requirements 1.5**
 *
 * Copy is asserted on **text**, per `LGR-99050e6-1` (colour alone fails WCAG
 * 1.4.1). The rendered wording is anchored on the *approval*, not the run:
 * `LGR-7d19eca-3`'s guard keeps an approval's execution row from going missing,
 * but nothing keeps that row's status healthy — see the FAILED case below.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import * as fc from 'fast-check';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';

type ApprovalRow = {
  approval_id: string;
  pipeline_key: string;
  run_id?: string;
  approval_type: string;
  status: string;
  created_at: string;
};

type ExecRow = Record<string, unknown>;

const state = vi.hoisted(() => ({
  approvals: [] as unknown[],
  executions: [] as unknown[],
  isPending: false,
}));
const mutateMock = vi.hoisted(() => vi.fn());
const showToastMock = vi.hoisted(() => vi.fn());
/** Every `(key)` the approvals hook was asked for, so its scope is checkable. */
const approvalHookCalls = vi.hoisted(() => [] as unknown[]);

vi.mock('../../hooks', () => ({
  useArchivePipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: mutateMock, isPending: state.isPending }),
  usePipelineExecutions: () => ({ data: { executions: state.executions } }),
  usePipelinePendingApprovals: (key: unknown) => {
    approvalHookCalls.push(key);
    return { data: state.approvals };
  },
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
  useToast: () => ({ showToast: showToastMock }),
}));

vi.mock('../../services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api-client')>();
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  };
});

import PipelineOverviewPage from '../PipelineOverviewPage';

const PIPELINE_KEY = 'helloworld-f9a76ce9';

const mockPipeline: Pipeline = {
  pipeline_key: PIPELINE_KEY,
  pipeline_name: 'helloworld',
  status: 'active',
  source_schema: 'customer',
  target_schema: 'customer_target',
  created_at: '2026-08-20T17:00:00Z',
  updated_at: '2026-08-25T12:44:37Z',
  ai_model: 'anthropic.claude-3-5-sonnet-20241022-v2:0',
  schema_history: [],
  all_files: [],
};

function renderWith(approvals: unknown[], executions: unknown[] = []) {
  state.approvals = approvals;
  state.executions = executions;
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

const approveButtons = () => screen.queryAllByRole('button', { name: /^Approve$/ });
const rejectButtons = () => screen.queryAllByRole('button', { name: /^Reject$/ });

/** The number the page renders beside the controls, read off the rendered text. */
function renderedApprovalCount(): number | null {
  const el = screen.queryByTestId('approval-count');
  if (!el) return null;
  const match = (el.textContent ?? '').match(/(\d+)/);
  return match ? Number(match[1]) : null;
}

function approval(over: Partial<ApprovalRow> = {}): ApprovalRow {
  return {
    approval_id: 'helloworld-f9a76ce9_schema_mapping_1787402675',
    pipeline_key: PIPELINE_KEY,
    run_id: 'run-2026-08-22-12-43-58-c3e56b8f',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2026-08-22T12:44:35.411868+00:00',
    ...over,
  };
}

beforeEach(() => {
  cleanup();
  mutateMock.mockReset();
  showToastMock.mockReset();
  approvalHookCalls.length = 0;
  state.isPending = false;
});

/* ------------------------------------------------------------------ */
/* Source of the set                                                  */
/* ------------------------------------------------------------------ */

describe('PipelineOverviewPage — approvals source (p51 task 4.1)', () => {
  it('asks the approvals hook for this pipeline', () => {
    renderWith([]);
    expect(approvalHookCalls).toContain(PIPELINE_KEY);
  });

  it('renders nothing — not a zero — when there are no pending approvals', () => {
    // **Amended by 4.3, twice over.**
    //
    // The name said "when the read returned nothing" and the comment said
    // "`data: undefined` is both 'still loading' and 'failed'" — but the fixture
    // sets `[]`, which is an *answered* read finding none. The comment described
    // a case this test never exercised.
    //
    // And the conflation it asserted is now false regardless: `classifyCount`
    // reads `isError` before `data`, so a failed read renders "Awaiting approval:
    // unavailable" in words while an answered-zero and an in-flight read stay
    // silent. Those three arms are asserted in `count-states.test.tsx` (P7).
    //
    // What this test pins, accurately: an answered read finding none renders no
    // count and no controls — never `0` (`LGR-99050e6-2`). This mock supplies no
    // `isError`, which is what keeps it on the answered arm.
    state.approvals = [];
    renderWith([]);
    expect(renderedApprovalCount()).toBeNull();
    expect(approveButtons()).toHaveLength(0);
    expect(screen.queryAllByTestId('approval-label')).toHaveLength(0);
  });

  it('ignores WAITING_APPROVAL execution rows — the old source', () => {
    // Three rows of exactly the shape the previous derivation counted. If the
    // page still read `latestExecs`, this would render three sets of controls.
    const execs: ExecRow[] = [1, 2, 3].map((n) => ({
      run_id: `run-page-${n}`,
      status: 'WAITING_APPROVAL',
      pending_approval_id: `appr-from-exec-${n}`,
      current_step: 'schema_mapping',
      started_at: `2026-08-2${n}T00:00:00Z`,
    }));

    renderWith([], execs);

    expect(renderedApprovalCount()).toBeNull();
    expect(approveButtons()).toHaveLength(0);
    expect(rejectButtons()).toHaveLength(0);
  });
});

/* ------------------------------------------------------------------ */
/* P3 — an off-page approval still has its affordance                 */
/* ------------------------------------------------------------------ */

describe('P3 — an approval whose run is absent from the executions page', () => {
  const OFF_PAGE_RUN = 'run-2026-07-01-09-15-00-offpage1';

  /** Ten rows, none of them the approval's run — a genuinely disjoint fixture. */
  const onPageExecs: ExecRow[] = Array.from({ length: 10 }, (_, i) => ({
    run_id: `run-2026-08-2${i % 10}-00-00-00-onpage${i}`,
    status: i === 0 ? 'RUNNING' : 'SUCCEEDED',
    started_at: `2026-08-2${i % 10}T00:00:00Z`,
    file_name: `file-${i}.csv`,
  }));

  it('the fixtures really are disjoint, so this test is asserting something', () => {
    const pageRunIds = onPageExecs.map((r) => r.run_id as string);
    expect(pageRunIds).not.toContain(OFF_PAGE_RUN);
    expect(pageRunIds).toHaveLength(10);
  });

  it('renders its Approve and Reject controls anyway', () => {
    renderWith([approval({ approval_id: 'appr-offpage', run_id: OFF_PAGE_RUN })], onPageExecs);

    expect(approveButtons()).toHaveLength(1);
    expect(rejectButtons()).toHaveLength(1);
    expect(renderedApprovalCount()).toBe(1);
  });

  it('labels it by approval_type and run id, with no claim about the table', () => {
    renderWith([approval({ approval_id: 'appr-offpage', run_id: OFF_PAGE_RUN })], onPageExecs);

    const label = screen.getByTestId('approval-label');
    expect(label).toHaveTextContent('schema_mapping');
    expect(label).toHaveTextContent(OFF_PAGE_RUN.slice(0, 16));
    // The run need not be in Recent Runs, so no wording may imply it is.
    for (const forbidden of [/below/i, /above/i, /in the table/i, /this run's row/i]) {
      expect(label.textContent ?? '').not.toMatch(forbidden);
    }
  });

  it('acts on the approval id from the approvals row, not on a page row', () => {
    renderWith([approval({ approval_id: 'appr-offpage', run_id: OFF_PAGE_RUN })], onPageExecs);

    fireEvent.click(approveButtons()[0]);

    expect(mutateMock).toHaveBeenCalledTimes(1);
    expect(mutateMock.mock.calls[0][0]).toEqual({
      id: 'appr-offpage',
      action: 'approve',
      comment: '',
    });
  });
});

/* ------------------------------------------------------------------ */
/* The live case task 1.2 measured: pending approval on a FAILED run  */
/* ------------------------------------------------------------------ */

describe('a pending approval whose execution row is FAILED (live, 2 of 2)', () => {
  /**
   * The measured live row, reproduced: top-level `FAILED`, `current_step`
   * `"pipeline_failure"`, `steps_status.schema_mapping` still `WAITING_APPROVAL`
   * and `pending_approval_id` still set. This is the designed steady state for a
   * `timeout_behavior: "manual"` approval, so it is not a corrupt fixture.
   */
  const failedRun: ExecRow = {
    run_id: 'run-2026-08-22-12-43-58-c3e56b8f',
    status: 'FAILED',
    current_step: 'pipeline_failure',
    pending_approval_id: 'helloworld-f9a76ce9_schema_mapping_1787402675',
    started_at: '2026-08-22T12:43:58.512134+00:00',
    steps_status: {
      file_validation: 'SUCCEEDED',
      schema_extract: 'SUCCEEDED',
      schema_compare: 'SUCCEEDED',
      schema_mapping: 'WAITING_APPROVAL',
      pipeline_failure: 'FAILED',
    },
  };

  it('renders its controls, which the top-level status filter did not', () => {
    renderWith([approval()], [failedRun]);

    expect(approveButtons()).toHaveLength(1);
    expect(rejectButtons()).toHaveLength(1);
    expect(renderedApprovalCount()).toBe(1);
  });

  it('does not carry "pipeline_failure" into the label', () => {
    renderWith([approval()], [failedRun]);

    const label = screen.getByTestId('approval-label');
    expect(label).toHaveTextContent('schema_mapping');
    expect(label.textContent ?? '').not.toMatch(/pipeline_failure/);
  });

  it('does not describe the run as awaiting approval', () => {
    renderWith([approval()], [failedRun]);

    // The *approval* is pending; the run is FAILED. Copy anchored on the run
    // would be false here, and is false on 2 of 2 live rows.
    const count = screen.getByTestId('approval-count');
    expect(count).toHaveTextContent('1 approval pending');
    expect(count.textContent ?? '').not.toMatch(/run/i);
  });
});

/* ------------------------------------------------------------------ */
/* P2 — the count and the controls describe the same set              */
/* ------------------------------------------------------------------ */

describe('P2 — rendered Approve buttons equal the rendered count', () => {
  const approvalArb = fc
    .uniqueArray(fc.integer({ min: 0, max: 999 }), { minLength: 0, maxLength: 6 })
    .map((ids) =>
      ids.map((n) =>
        approval({
          approval_id: `appr-${n}`,
          run_id: `run-${n}`,
          approval_type: n % 2 === 0 ? 'schema_mapping' : 'code_generation',
        }),
      ),
    );

  /**
   * Generated independently of the approvals, and deliberately including the
   * `WAITING_APPROVAL` + `pending_approval_id` shape the old derivation counted.
   * The property must hold whatever this page happens to contain — which is
   * exactly what a page-scoped count cannot do.
   */
  const execArb = fc.array(
    fc.record({
      run_id: fc.integer({ min: 0, max: 999 }).map((n) => `exec-run-${n}`),
      status: fc.constantFrom('RUNNING', 'FAILED', 'SUCCEEDED', 'WAITING_APPROVAL', 'SKIPPED'),
      pending_approval_id: fc.integer({ min: 0, max: 999 }).map((n) => `exec-appr-${n}`),
      started_at: fc.constant('2026-08-22T00:00:00Z'),
    }),
    { maxLength: 10 },
  );

  it('holds over a generated set of approvals and an unrelated executions page', () => {
    fc.assert(
      fc.property(approvalArb, execArb, (approvals, execs) => {
        cleanup();
        renderWith(approvals, execs);

        const count = renderedApprovalCount();
        const approves = approveButtons().length;
        const rejects = rejectButtons().length;
        const labels = screen.queryAllByTestId('approval-label').length;

        if (approvals.length === 0) {
          // Gated off entirely — no count, and so nothing to disagree with.
          expect(count).toBeNull();
          expect(approves).toBe(0);
          expect(rejects).toBe(0);
          return;
        }

        expect(count).toBe(approvals.length);
        expect(approves).toBe(count);
        expect(rejects).toBe(count);
        expect(labels).toBe(count);
      }),
      { numRuns: 40 },
    );
  });

  it('pluralises on the same number it counts', () => {
    renderWith([approval({ approval_id: 'a1', run_id: 'r1' })]);
    expect(screen.getByTestId('approval-count')).toHaveTextContent('1 approval pending');

    cleanup();
    renderWith([
      approval({ approval_id: 'a1', run_id: 'r1' }),
      approval({ approval_id: 'a2', run_id: 'r2' }),
    ]);
    expect(screen.getByTestId('approval-count')).toHaveTextContent('2 approvals pending');
    expect(approveButtons()).toHaveLength(2);
  });
});

/* ------------------------------------------------------------------ */
/* D4 — the optimistic pending state is unchanged                     */
/* ------------------------------------------------------------------ */

describe('D4 — pendingApprovalId still gates the optimistic state (R1.5)', () => {
  const two = [
    approval({ approval_id: 'appr-first', run_id: 'run-first' }),
    approval({ approval_id: 'appr-second', run_id: 'run-second' }),
  ];

  it('sends exactly { id, action, comment } — no runId, though the row now has one', () => {
    renderWith(two);

    fireEvent.click(approveButtons()[1]);
    expect(mutateMock.mock.calls[0][0]).toEqual({
      id: 'appr-second',
      action: 'approve',
      comment: '',
    });

    fireEvent.click(rejectButtons()[0]);
    expect(mutateMock.mock.calls[1][0]).toEqual({
      id: 'appr-first',
      action: 'reject',
      comment: '',
    });
  });

  it('puts only the acted-on row in the loading state', () => {
    // `isPending` is the mutation's own flag; `pendingApprovalId` is what
    // narrows it to one row. Both are needed for a row to show loading.
    state.isPending = true;
    renderWith(two);

    // Before any click, no row is pending even though the mutation flag is set.
    for (const b of [...approveButtons(), ...rejectButtons()]) {
      expect(b).not.toHaveAttribute('aria-disabled', 'true');
    }

    fireEvent.click(approveButtons()[1]);

    expect(approveButtons()[1]).toHaveAttribute('aria-disabled', 'true');
    expect(rejectButtons()[1]).toHaveAttribute('aria-disabled', 'true');
    expect(approveButtons()[0]).not.toHaveAttribute('aria-disabled', 'true');
    expect(rejectButtons()[0]).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('clears the state and toasts on approve success', () => {
    state.isPending = true;
    renderWith(two);
    fireEvent.click(approveButtons()[0]);

    const options = mutateMock.mock.calls[0][1] as { onSuccess: () => void };
    // Inside `act` because the callback clears `pendingApprovalId`; the state
    // update has to flush before the loading state can be read off the DOM.
    act(() => options.onSuccess());

    expect(showToastMock).toHaveBeenCalledWith('Approved', 'success');
    expect(approveButtons()[0]).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('clears the state and toasts on approve failure', () => {
    // The live risk task 1.2 recorded: `has_task_token` is true but the run
    // already failed at the timeout instant, so `SendTaskSuccess` may reject.
    // This path is what makes that visible rather than silent.
    state.isPending = true;
    renderWith(two);
    fireEvent.click(approveButtons()[0]);

    const options = mutateMock.mock.calls[0][1] as { onError: () => void };
    act(() => options.onError());

    expect(showToastMock).toHaveBeenCalledWith('Failed to approve', 'error');
    expect(approveButtons()[0]).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('clears the state and toasts on reject success and failure', () => {
    renderWith(two);

    fireEvent.click(rejectButtons()[0]);
    act(() => (mutateMock.mock.calls[0][1] as { onSuccess: () => void }).onSuccess());
    expect(showToastMock).toHaveBeenCalledWith('Rejected', 'success');

    fireEvent.click(rejectButtons()[1]);
    act(() => (mutateMock.mock.calls[1][1] as { onError: () => void }).onError());
    expect(showToastMock).toHaveBeenCalledWith('Failed to reject', 'error');
  });
});
