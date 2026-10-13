// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import type { RunDetail } from '../../types';

/* ------------------------------------------------------------------ */
/*  Mock data                                                          */
/* ------------------------------------------------------------------ */

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

const failedRun: RunDetail = {
  run_id: 'run-fail-1',
  pipeline_key: 'pipe-1',
  status: 'FAILED',
  started_at: '2024-06-01T10:00:00Z',
  completed_at: '2024-06-01T10:05:00Z',
  steps: [
    { step_name: 'file_validation', status: 'completed' },
    { step_name: 'schema_extract', status: 'failed', error_message: 'Schema error' },
  ],
};

const runningRun: RunDetail = {
  ...failedRun,
  run_id: 'run-running-1',
  status: 'RUNNING',
  completed_at: undefined,
  steps: [{ step_name: 'file_validation', status: 'running' }],
};

/* ------------------------------------------------------------------ *
 *  The retried-run fixtures (p38 R2.2, R2.3, R5.3)
 *
 *  These replace a fixture that could not have detected the defect it
 *  was guarding. The old `retriedOriginalRun` carried
 *  `retried_by_run_id: 'run-retry-1'` alongside `run_id: 'run-retry-1'`,
 *  and the banner test asserted the banner rendered from it. Both halves
 *  were wrong in the same direction:
 *
 *    - `retried_by_run_id` was written to DynamoDB by the pre-p38 retry
 *      endpoint and emitted by **no** endpoint, so no response ever
 *      carried it. The fixture supplied by hand the one field the
 *      producer never sent.
 *    - `run-retry-1` is a `run-`-shaped id. The pre-p38 producer minted
 *      `retry-{run_id}[:40]`, and the p38 producer mints nothing at all,
 *      so no producer in either era has ever emitted that string.
 *
 *  That is `LGR-28b4d75-5` (**template / ratified**) in its fixture-side
 *  form: a test whose oracle is a hand-made fixture cannot detect the
 *  producer emitting something else — the fixture is its own oracle. So
 *  the fixtures below are built from what `get_run_details` actually
 *  emits: `attempts`, `attempt_count` and a `retry_source_run_id` that
 *  equals the run's own id, or — for every run that predates p38 —
 *  none of the three.
 * ------------------------------------------------------------------ */

/**
 * §5 delta D2 / `requirements.md` §5 risk 5 — the common case, not the edge.
 *
 * Every retried run that predates p38 is in exactly this state. The old
 * retry endpoint wrote only `retried_by_run_id` and `updated_at` onto the
 * original row (it put `retry_source_run_id` on the phantom row, not
 * here), so this row carries **no** `attempts`, **no** `attempt_count`
 * and **no** `retry_source_run_id`. The retry then executed under a
 * different id but `execution_updater` overwrote this row's `status` in
 * place, leaving `SUCCEEDED` over a step that still reads `failed`.
 *
 * That contradiction is the only surviving evidence a retry happened, so
 * it is the only thing the banner can key on here. R2.3 requires both
 * values to be shown rather than one reconciled away.
 */
const preP38RetriedRun: RunDetail = {
  run_id: 'run-2026-08-09-15-38-19-5c5f5f8a',
  pipeline_key: 'pipe-1',
  status: 'SUCCEEDED',
  started_at: '2026-08-09T15:30:00Z',
  completed_at: '2026-08-09T15:38:19Z',
  steps: [
    { step_name: 'file_validation', status: 'completed' },
    { step_name: 'code_testing', status: 'failed', error_message: 'assertion failed' },
  ],
};

const CONTINUED_RUN_ID = 'run-2026-08-10-09-02-11-7ab31c04';

/**
 * The shape the p38 producer emits: one id, an attempt snapshot of what
 * the retry displaced, and a `retry_source_run_id` that equals the run's
 * own id because the retry executes under it.
 *
 * The equal ids are the point, not an accident of the fixture — they are
 * what do-NOT 5 / R5.3 (`LGR-453f12d-5`, **template / ratified**) guards:
 * a related-run link may render only from an id that resolves somewhere
 * else, and this one resolves to the page the user is already on.
 */
const continuedRetryRun: RunDetail = {
  run_id: CONTINUED_RUN_ID,
  pipeline_key: 'pipe-1',
  status: 'RUNNING',
  started_at: '2026-08-10T09:02:11Z',
  trigger: 'retry',
  retry_source_run_id: CONTINUED_RUN_ID,
  attempt_count: 2,
  attempts: [
    {
      attempt: 1,
      failed_step: 'code_testing',
      status: 'FAILED',
      steps_status: { file_validation: 'success', code_testing: 'failed' },
      started_at: '2026-08-09T15:30:00Z',
      completed_at: '2026-08-09T15:38:19Z',
      execution_name: 'retry-run-2026-08-10-09-02-11-7ab31c04-1a2b3c4d',
    },
  ],
  steps: [
    { step_name: 'file_validation', status: 'skipped', logs: ['cached from attempt 1'] },
    { step_name: 'code_testing', status: 'running' },
  ],
};

/**
 * The two-id shape, kept deliberately.
 *
 * This is **not** what the p38 producer emits — it is the pre-p38 phantom
 * run's shape, where the retry ran under its own id and pointed back at
 * the original. It is retained because R5.3's link branch still has to be
 * covered from somewhere: `linkable` renders a link exactly when the
 * related id is non-empty and differs from the current one, and this is
 * the only fixture in which they differ. The `Result from run-original-1`
 * assertion below is the one task 9 requires to pass unamended.
 */
const retryRun: RunDetail = {
  run_id: 'run-retry-1',
  pipeline_key: 'pipe-1',
  status: 'RUNNING',
  started_at: '2024-06-02T10:00:00Z',
  trigger: 'retry',
  retry_source_run_id: 'run-original-1',
  steps: [
    { step_name: 'file_validation', status: 'skipped', logs: ['original log line'] },
    { step_name: 'schema_extract', status: 'running' },
  ],
};

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

let mockRunDetail: RunDetail | null = failedRun;
const mockRetryMutate = vi.fn();
let mockRetryIsPending = false;

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: mockRunDetail,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: mockRetryMutate, isPending: mockRetryIsPending }),
  useDataPreview: () => ({ data: null, isLoading: false, isError: false }),
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
}));

import RunDetailPage from '../RunDetailPage';

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

/**
 * `runId` comes from the URL, not from the fixture — `RunDetailPage`
 * reads it with `useParams` and threads it to `StepFlowVisualization` as
 * the "current" id the link guard compares against. A fixture whose
 * `retry_source_run_id` is meant to equal the current id therefore has to
 * be rendered at its own URL, or the comparison is against `run-fail-1`
 * and the equal-id branch is never reached.
 */
function renderPage(runId = 'run-fail-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={[`/pipelines/pipe-1/runs/${runId}`]}>
          <Routes>
            <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/** The banner element, so every assertion is scoped to it. */
function banner(): HTMLElement {
  return screen.getByTestId('retried-banner');
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  mockRunDetail = failedRun;
  mockRetryIsPending = false;
  mockRetryMutate.mockReset();
  mockNavigate.mockReset();
});

describe('RunDetailPage — Retried banner', () => {
  /* ---------------------------------------------------------------- *
   *  D2 first. A banner that renders correctly only with `attempts`
   *  present has been tested against the rare case and shipped broken
   *  for the common one.
   * ---------------------------------------------------------------- */
  describe('with no attempt attribution — every run that predates p38', () => {
    it('renders the banner from the status/steps contradiction alone', () => {
      mockRunDetail = preP38RetriedRun;
      renderPage(preP38RetriedRun.run_id);
      expect(banner()).toBeInTheDocument();
    });

    it('states both disagreeing values, reconciling neither (R2.3)', () => {
      mockRunDetail = preP38RetriedRun;
      renderPage(preP38RetriedRun.run_id);
      const text = banner().textContent ?? '';
      expect(text).toMatch(
        /This run records status SUCCEEDED, while its own Code Testing step records failed\./,
      );
      // Both values survive into the banner — neither is picked over the
      // other, because no record establishes which attempt wrote which.
      expect(text).toContain('SUCCEEDED');
      expect(text).toContain('failed');
    });

    it('says the per-attempt record is missing rather than leaving it blank', () => {
      mockRunDetail = preP38RetriedRun;
      renderPage(preP38RetriedRun.run_id);
      expect(banner().textContent ?? '').toMatch(
        /No per-attempt record was kept for the earlier attempts/,
      );
    });

    it('closes by saying which attempt the steps below describe', () => {
      mockRunDetail = preP38RetriedRun;
      renderPage(preP38RetriedRun.run_id);
      // The load-bearing sentence: a bookmarked original-run URL now
      // resolves here and shows the retry's outcome.
      expect(banner().textContent ?? '').toMatch(
        /The steps and status below describe the most recent attempt, not the earlier ones\./,
      );
    });

    it('never renders an unrecorded attempt count as zero', () => {
      mockRunDetail = preP38RetriedRun;
      renderPage(preP38RetriedRun.run_id);
      const text = banner().textContent ?? '';
      // LGR-99050e6-2 (template / ratified): absent is reported as
      // unknown, never defaulted. This row carries no `attempt_count`.
      expect(text).not.toMatch(/0 attempts/);
      expect(text).not.toMatch(/attempted 0/);
      expect(text).toMatch(/How many times is not recorded/);
    });
  });

  /* ---------------------------------------------------------------- *
   *  Then the attributed case.
   * ---------------------------------------------------------------- */
  describe('with attempt attribution', () => {
    it('states the attempt count and which attempt the steps describe', () => {
      mockRunDetail = continuedRetryRun;
      renderPage(CONTINUED_RUN_ID);
      const text = banner().textContent ?? '';
      expect(text).toMatch(/This run has been attempted 2 times\./);
      expect(text).toMatch(/The steps and status below describe attempt 2\./);
    });

    it('states what the prior attempt did and where it stopped', () => {
      mockRunDetail = continuedRetryRun;
      renderPage(CONTINUED_RUN_ID);
      // The status the retry displaced, and the step it stopped at — not
      // a link to a second run, because there is no second run.
      expect(banner().textContent ?? '').toMatch(/Attempt 1 ended FAILED at Code Testing/);
    });

    it('does not claim the attribution is missing when it is present', () => {
      mockRunDetail = continuedRetryRun;
      renderPage(CONTINUED_RUN_ID);
      expect(banner().textContent ?? '').not.toMatch(/No per-attempt record was kept/);
    });
  });

  /* ---------------------------------------------------------------- *
   *  do-NOT 5 / R5.3, both eras. Task 5.4's Property 9 asserts the
   *  same thing over generated id pairs.
   * ---------------------------------------------------------------- */
  it('renders no link anywhere in the banner, with or without attribution', () => {
    mockRunDetail = preP38RetriedRun;
    const { unmount } = renderPage(preP38RetriedRun.run_id);
    expect(banner().querySelector('a')).toBeNull();
    unmount();

    mockRunDetail = continuedRetryRun;
    renderPage(CONTINUED_RUN_ID);
    // LGR-453f12d-5: the only candidate id is this run's own, so a link
    // here would resolve to the page the user is already on.
    expect(banner().querySelector('a')).toBeNull();
  });

  it('does not show the banner when nothing says the run was retried', () => {
    mockRunDetail = failedRun;
    renderPage();
    expect(screen.queryByTestId('retried-banner')).not.toBeInTheDocument();
  });

  it('does not treat a FAILED run whose steps all read success as a contradiction', () => {
    // LGR-9e76249-6 (template / ratified): the RecordFailure sentinel is
    // the sole carrier of a run's terminal FAILED whenever the failing
    // step never wrote a row of its own — a Lambda timeout, a
    // States.Timeout at an approval gate, a Glue failure. That shape is a
    // legitimate state, so it must not raise a retried banner.
    mockRunDetail = {
      ...failedRun,
      run_id: 'run-timeout-1',
      status: 'FAILED',
      steps: [
        { step_name: 'file_validation', status: 'completed' },
        { step_name: 'code_testing', status: 'success' },
      ],
    };
    renderPage('run-timeout-1');
    expect(screen.queryByTestId('retried-banner')).not.toBeInTheDocument();
  });
});

describe('RunDetailPage — Retry button', () => {
  it('shows "Retry from Step" button when run status is FAILED', () => {
    mockRunDetail = failedRun;
    renderPage();
    expect(screen.getByText('Retry from Step')).toBeInTheDocument();
  });

  it('hides retry button when run status is RUNNING', () => {
    mockRunDetail = runningRun;
    renderPage();
    expect(screen.queryByText('Retry from Step')).not.toBeInTheDocument();
  });

  it('hides retry button when run status is COMPLETED', () => {
    mockRunDetail = { ...failedRun, status: 'COMPLETED' };
    renderPage();
    expect(screen.queryByText('Retry from Step')).not.toBeInTheDocument();
  });

  it('calls retryMutation.mutate with runId on click', () => {
    mockRunDetail = failedRun;
    renderPage();
    fireEvent.click(screen.getByText('Retry from Step'));
    expect(mockRetryMutate).toHaveBeenCalledWith(
      expect.objectContaining({ runId: 'run-fail-1' }),
    );
  });
});

describe('RunDetailPage — Skipped step rendering', () => {
  it('renders "Skipped" status for skipped steps in retry runs', () => {
    mockRunDetail = retryRun;
    renderPage();
    expect(screen.getByText('Skipped')).toBeInTheDocument();
  });

  it('renders "Result from" link for skipped steps', () => {
    mockRunDetail = retryRun;
    renderPage();
    expect(screen.getByText('Result from run-original-1')).toBeInTheDocument();
  });

  it('renders the non-link note when the source id is this run\'s own', () => {
    // R5.3 / do-NOT 5, the skipped-step site. Same render as the test
    // above, one field different: `retry_source_run_id === run_id`, so
    // the link becomes a self-link and the note takes its place.
    mockRunDetail = continuedRetryRun;
    renderPage(CONTINUED_RUN_ID);
    expect(screen.getByTestId('skipped-step-source-note')).toHaveTextContent(
      "Result from this run's earlier attempt",
    );
    expect(screen.queryByTestId('skipped-step-source-link')).not.toBeInTheDocument();
    expect(screen.queryByText(`Result from ${CONTINUED_RUN_ID}`)).not.toBeInTheDocument();
  });

  it('shows original run logs note for skipped steps with logs', async () => {
    mockRunDetail = retryRun;
    renderPage();
    // The logs section should reference the original run
    await waitFor(() => {
      expect(screen.getByText(/Original Run Logs/)).toBeInTheDocument();
    });
  });
});
