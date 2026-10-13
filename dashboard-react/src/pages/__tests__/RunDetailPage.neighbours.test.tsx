// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Previous/Next Run come from the server's neighbour answer (p48 C10, R10.2, task 5.2).
 *
 * WHAT REPLACED WHAT
 * ------------------
 * The page used to call `usePipelineExecutions(pipelineId)` — one page of run
 * history — and derive the two ids by `findIndex` + array arithmetic. That went
 * dead at the edge of page 1: the neighbour of the tenth run is on page 2, and
 * nothing on page 1 could name it. It now reads `useRunNeighbours`, which the
 * backend answers with three keyed reads at `Limit=1` over `RunsByTimeIndex`.
 *
 * WHAT THIS FILE CAN AND CANNOT PROVE
 * -----------------------------------
 * The *depth* half of R10.2 is a property of the seek and is proved where the seek
 * happens: `tests/unit/lambdas/dashboard_api/test_run_neighbours.py`, against a
 * 25-row partition with the target run asserted absent from page 1. A component
 * test with a mocked hook cannot prove it — it would be asserting that a fixture
 * the test itself wrote came back.
 *
 * What it CAN prove, and what no backend test can, is the half that is purely a
 * frontend mapping and is invisible when wrong:
 *
 *  - `older` drives **Previous Run** and `newer` drives **Next Run**. Swap them and
 *    both buttons still work; they just go the wrong way. No type checks it.
 *  - the ids reach `navigate` unchanged, so the page does not re-derive position
 *    (R10.4 — one ordering authority).
 *  - the ends are stated **in words**, not by the greyed-out control alone
 *    (`LGR-99050e6-1`, WCAG 1.4.1).
 *
 * Requirements: p48 R10.2, R10.4
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { RunDetail } from '../../types';
import type { RunNeighbours } from '../../hooks/use-run-neighbours';

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

const succeededRun: RunDetail = {
  run_id: 'run-middle',
  pipeline_key: 'pipe-1',
  status: 'SUCCEEDED',
  started_at: '2026-06-02T10:00:00Z',
  completed_at: '2026-06-02T10:05:00Z',
  approvals: [],
  steps: [{ step_name: 'schema_extract', status: 'success' }],
};

/**
 * Chronologically unambiguous, and with `run_id` order the REVERSE of time order
 * (`run-zzz-oldest` < `run-middle` is false lexicographically), so a mapping that
 * had fallen back to id ordering could not pass by accident.
 */
const BOTH_NEIGHBOURS: RunNeighbours = {
  pipeline_id: 'pipe-1',
  run_id: 'run-middle',
  older: { run_id: 'run-zzz-oldest', started_at: '2026-06-01T10:00:00Z', status: 'SKIPPED' },
  newer: { run_id: 'run-aaa-newest', started_at: '2026-06-03T10:00:00Z', status: 'SUCCEEDED' },
};

let neighbours: RunNeighbours | undefined;

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: succeededRun,
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
  useRunNeighbours: () => ({ data: neighbours }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/pipe-1/runs/run-middle']}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const prevButton = () => screen.getByRole('button', { name: /^Previous Run/ });
const nextButton = () => screen.getByRole('button', { name: /^Next Run/ });

beforeEach(() => {
  neighbours = BOTH_NEIGHBOURS;
  mockNavigate.mockReset();
});

describe('RunDetailPage — Previous/Next from the neighbour query (R10.2)', () => {
  it('Previous Run navigates to the OLDER run', () => {
    renderPage();
    fireEvent.click(prevButton());
    expect(mockNavigate).toHaveBeenCalledWith('/pipelines/pipe-1/runs/run-zzz-oldest');
  });

  it('Next Run navigates to the NEWER run', () => {
    renderPage();
    fireEvent.click(nextButton());
    expect(mockNavigate).toHaveBeenCalledWith('/pipelines/pipe-1/runs/run-aaa-newest');
  });

  it('does not confuse the two directions', () => {
    // The paired negative. Either assertion above passes on its own if the two are
    // swapped AND the fixture is symmetric; together with distinct ids they cannot.
    renderPage();
    fireEvent.click(prevButton());
    fireEvent.click(nextButton());
    const targets = mockNavigate.mock.calls.map((call) => call[0]);
    expect(targets).toEqual([
      '/pipelines/pipe-1/runs/run-zzz-oldest',
      '/pipelines/pipe-1/runs/run-aaa-newest',
    ]);
  });

  it('offers a SKIPPED row as a navigation target rather than skipping it', () => {
    // The `older` fixture is a SKIPPED run. Navigation steps onto it: a skip is a
    // real position in the history, and filtering it here would disagree with the
    // list these buttons navigate (R10.4). Task 5.6 skips non-previewable rows when
    // choosing a run to render data FROM, which is a different question.
    renderPage();
    expect(prevButton()).not.toBeDisabled();
    fireEvent.click(prevButton());
    expect(mockNavigate).toHaveBeenCalledWith('/pipelines/pipe-1/runs/run-zzz-oldest');
  });
});

describe('RunDetailPage — the ends, stated in words (LGR-99050e6-1)', () => {
  it('states that there is no newer run when this is the newest', () => {
    neighbours = { ...BOTH_NEIGHBOURS, newer: null };
    renderPage();

    // The WORDING carries the fact, not just the disabled styling — colour alone
    // fails WCAG 1.4.1, and "Next Run" greyed out does not say why.
    expect(nextButton()).toHaveAccessibleName(
      'Next Run, unavailable: this is the newest run of this pipeline',
    );
    expect(nextButton()).toBeDisabled();
    // The other direction is unaffected.
    expect(prevButton()).not.toBeDisabled();
  });

  it('states that there is no older run when this is the oldest', () => {
    neighbours = { ...BOTH_NEIGHBOURS, older: null };
    renderPage();

    expect(prevButton()).toHaveAccessibleName(
      'Previous Run, unavailable: this is the oldest run of this pipeline',
    );
    expect(prevButton()).toBeDisabled();
    expect(nextButton()).not.toBeDisabled();
  });

  it('states both ends when the pipeline has only this run', () => {
    neighbours = { ...BOTH_NEIGHBOURS, older: null, newer: null };
    renderPage();

    expect(prevButton()).toBeDisabled();
    expect(nextButton()).toBeDisabled();
    expect(prevButton()).toHaveAccessibleName(/this is the oldest run/);
    expect(nextButton()).toHaveAccessibleName(/this is the newest run/);
  });

  it('says what an available button will do, so enabled and disabled read differently', () => {
    renderPage();
    expect(prevButton()).toHaveAccessibleName('Previous Run, the run before this one');
    expect(nextButton()).toHaveAccessibleName('Next Run, the run after this one');
  });

  it('disables both buttons while the neighbour query has not answered', () => {
    // `undefined` is "not answered yet", which is not the same as "no neighbour" —
    // but both must refuse to navigate, because there is no id to navigate to.
    neighbours = undefined;
    renderPage();

    expect(prevButton()).toBeDisabled();
    expect(nextButton()).toBeDisabled();
    fireEvent.click(prevButton());
    fireEvent.click(nextButton());
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
