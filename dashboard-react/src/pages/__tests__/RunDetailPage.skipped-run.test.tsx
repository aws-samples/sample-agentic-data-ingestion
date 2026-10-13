// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 5.2 — the honest skip page, and the 404 branch it sits beside.
 *
 * _Requirements: 6.1, 6.2, 6.3, 6.4_
 *
 * **What was wrong.** A skip row has no state-table rows, so `get_run_details`
 * answers 404 for its run id, `fetchRunDetail` maps 404 to `null`
 * (`use-run-detail.ts:29-30`), and the run-detail page rendered *"No run with id
 * … exists."* — a denial of existence about a row the list the operator clicked
 * from had just read to them. The backend repair is fenced (R6.3 / R8.2, p38
 * do-NOT 6 / p38 R1.4), so arm B2 recognises the 404 in the frontend beside the
 * descriptor the two run lists now attach to the navigation (task 5.1).
 *
 * **The two branches are what this file is about.** Branch 2 is strictly narrower
 * than branch 3 — both require `!isError && runDetail === null`, and branch 2
 * adds the carried descriptor. So this file asserts both halves: with a
 * descriptor the page states what happened, and with **no** descriptor the p38
 * R4.1 copy still renders unchanged (R6.4, R8.5). The second half is also
 * asserted by `RunDetailPage.error-states.test.tsx:137-151`, which must pass
 * **unmodified** — it is the proof branch 2 sits beside branch 3 rather than
 * replacing it. Duplicating the claim here is deliberate: this file would
 * otherwise pass with branch 3 deleted.
 *
 * **No skip copy is transcribed.** Every expected sentence is read from the
 * production authority — `describeSkip` for the reason, and the exported
 * constants for the page's own statements. A test carrying its own copy of the
 * copy is the drift `utils/skip-reason.ts`'s p30 R10.2 docstring is written
 * against, and R7 in this same spec is that drift class between these same two
 * run lists.
 *
 * **The state is built by the production builder**, `buildSkippedRunNavState`,
 * not hand-written. That is what makes "the page reached from a list" a real
 * claim rather than a claim about a fixture: if the writer stops emitting a field
 * or renames the router-state key, these tests fail rather than testing a shape
 * nothing produces.
 *
 * **Never keyed on the run id — do-NOT 4.** Two id shapes exist and the
 * contention one is `run-`-prefixed deliberately (`LGR-21a1b5b-4`). Both appear
 * below and nothing reads a prefix.
 *
 * **DOM assertions under jsdom, not observations of what a user sees**, the same
 * limitation `RunDetailPage.error-states.test.tsx` records for the branch beside
 * this one.
 *
 * Harness taken from `RunDetailPage.error-states.test.tsx`: `../../hooks` mocked
 * because the page reads all of them before the early returns, `useNavigate`
 * mocked, and `useLocation` left REAL so the router — not a stub — carries the
 * state.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { RunDetail } from '../../types';
import { describeSkip } from '../../utils/skip-reason';
import { buildSkippedRunNavState } from '../../utils/skipped-run-descriptor';
import {
  SKIPPED_RUN_NO_EXECUTION_STATEMENT,
  SKIPPED_RUN_FILE_LABEL,
  SKIPPED_RUN_PREVIOUS_RUN_LABEL,
} from '../RunDetailPage';

/** The duplicate-skip id shape — `pipeline_trigger._duplicate_skip_record_id`. */
const DUPLICATE_RUN_ID = 'skipped-8f14e45fceea167a5a36dedd4bea2543';
/** The contention id shape — `_contention_skip_record_id`, `run-`-prefixed on purpose. */
const CONTENTION_RUN_ID = 'run-2026-08-26-11-30-00-skip-pipeline_lock_contention';
const PIPELINE_ID = 'newtest-ab2a08a0';

let mockData: RunDetail | null | undefined = null;
let mockIsError = false;
let mockRefetch = vi.fn();

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../hooks', () => ({
  useRunDetail: () => ({
    data: mockData,
    isLoading: false,
    isError: mockIsError,
    refetch: mockRefetch,
    isWebSocketConnected: false,
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
  useRunTraces: () => ({ data: null, isLoading: false }),
  useSelfHealingActivity: () => ({ data: undefined }),
  useQualityReport: () => ({ data: null, isError: false, refetch: vi.fn() }),
  useRunNeighbours: () => ({ data: undefined }),
  usePipelineDetail: () => ({ data: undefined, refetch: vi.fn() }),
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn() }),
  useMappingRegenPolling: () => {},
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  qualityReportKeys: { run: (runId: string) => ['quality-report', runId] },
}));

import RunDetailPage from '../RunDetailPage';

/** A run-list row, as `get_pipeline_executions` publishes one (unprojected). */
type Row = Record<string, unknown>;

/** The duplicate skip both writers' fields differ over — it carries all of them. */
function duplicateRow(overrides: Row = {}): Row {
  return {
    run_id: DUPLICATE_RUN_ID,
    status: 'SKIPPED',
    skip_reason: 'duplicate_content',
    skip_detail:
      'Content already processed by run run-2026-08-24-09-12-00-3f9a1b (same size and ETag).',
    previous_run_id: 'run-2026-08-24-09-12-00-3f9a1b',
    file_name: 'customers_aug.csv',
    started_at: '2026-08-26T11:30:00Z',
    ...overrides,
  };
}

/** The contention skip — no `previous_run_id` at all (`pipeline_trigger:488-505`). */
function contentionRow(overrides: Row = {}): Row {
  return {
    run_id: CONTENTION_RUN_ID,
    status: 'SKIPPED',
    skip_reason: 'pipeline_lock_contention',
    skip_detail: 'The previous execution for this pipeline had not finished.',
    file_name: 'customers_aug.csv',
    started_at: '2026-08-26T11:30:00Z',
    ...overrides,
  };
}

/**
 * Render the page as a Run_List_Surface reaches it: the router carries whatever
 * `buildSkippedRunNavState` produced for the row, which is `undefined` for a
 * non-skip row and for the no-descriptor cases below.
 */
function renderFromList(row: Row | null, runId: string) {
  const state = row === null ? undefined : buildSkippedRunNavState(row);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        initialEntries={[{ pathname: `/pipelines/${PIPELINE_ID}/runs/${runId}`, state }]}
      >
        <Routes>
          <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  // 8.1's mapping, and the state every case here shares: `null` with no error is
  // the 404 a skip's run id correctly produces (R6.3 — the backend is unchanged).
  mockData = null;
  mockIsError = false;
  mockRefetch = vi.fn();
  mockNavigate.mockClear();
});

describe('RunDetailPage — a skip reached from a run list states what happened (R6.1, R6.2)', () => {
  it('renders the reason `describeSkip` gives, and never the 404 denial', () => {
    const row = duplicateRow();
    const { container } = renderFromList(row, DUPLICATE_RUN_ID);
    const text = container.textContent ?? '';

    // R6.2, read from the authority rather than transcribed. `describeSkip`
    // folds the row's own `skip_detail` into `detail`, and for a duplicate that
    // detail already names the previous run in a full sentence.
    const expected = describeSkip(
      row.status as string,
      row.skip_reason as string,
      row.skip_detail as string,
    );
    expect(expected).not.toBeNull();
    expect(screen.getByTestId('skip-reason-badge').textContent).toBe(expected!.label);
    expect(screen.getByTestId('run-detail-skip-reason-detail').textContent).toBe(expected!.detail);
    expect(text).toContain(row.skip_detail as string);

    // R6.1 — the whole point. The navigation the list offered must not resolve to
    // a statement that no run with that id exists.
    expect(text).not.toMatch(/no run with id/i);
    expect(text).toContain(SKIPPED_RUN_NO_EXECUTION_STATEMENT);
  });

  it('states the reason for all three known reasons and for one it does not recognise', () => {
    for (const reason of [
      'duplicate_content',
      'global_concurrency',
      'pipeline_lock_contention',
      'some_future_reason',
    ]) {
      const row = duplicateRow({ skip_reason: reason });
      const view = renderFromList(row, DUPLICATE_RUN_ID);
      const expected = describeSkip(
        row.status as string,
        reason,
        row.skip_detail as string,
      );
      expect(screen.getByTestId('skip-reason-badge').textContent).toBe(expected!.label);
      expect(view.container.textContent ?? '').not.toMatch(/no run with id/i);
      view.unmount();
    }
  });

  it('states the skip even when the row recorded no reason at all', () => {
    // `describeSkip` reports a reasonless skip AS reasonless rather than guessing
    // (`skip-reason.ts:97-102`), and this page must still not deny the run.
    const row: Row = { run_id: CONTENTION_RUN_ID, status: 'SKIPPED' };
    const { container } = renderFromList(row, CONTENTION_RUN_ID);
    const expected = describeSkip('SKIPPED', undefined, undefined);
    expect(screen.getByTestId('skip-reason-badge').textContent).toBe(expected!.label);
    expect(screen.getByTestId('run-detail-skip-reason-detail').textContent).toBe(expected!.detail);
    expect(container.textContent ?? '').not.toMatch(/no run with id/i);
  });

  it('works for either id shape, because nothing reads the run id (do-NOT 4)', () => {
    for (const [row, runId] of [
      [duplicateRow(), DUPLICATE_RUN_ID],
      [contentionRow(), CONTENTION_RUN_ID],
    ] as Array<[Row, string]>) {
      const view = renderFromList(row, runId);
      expect(screen.getByTestId('skip-reason-badge')).toBeInTheDocument();
      expect(view.container.textContent ?? '').not.toMatch(/no run with id/i);
      view.unmount();
    }
  });
});

describe('RunDetailPage — the skip page promises only what the row carried (R6.2)', () => {
  it('names the file and the previous run when a duplicate skip carried both', () => {
    renderFromList(duplicateRow(), DUPLICATE_RUN_ID);
    expect(screen.getByTestId('run-detail-skip-file').textContent).toContain(
      `${SKIPPED_RUN_FILE_LABEL}: customers_aug.csv`,
    );
    expect(screen.getByTestId('run-detail-skip-previous-run').textContent).toContain(
      `${SKIPPED_RUN_PREVIOUS_RUN_LABEL}: run-2026-08-24-09-12-00-3f9a1b`,
    );
  });

  it('renders nothing about a previous run when a contention skip carried none', () => {
    // The load-bearing assertion of this block. A contention skip has no
    // `previous_run_id` — nothing was duplicated — so the page must not promise
    // one, blank or otherwise (`LGR-44d601e-1`: absence stays absence). The file
    // name is still there, so this is not vacuous.
    const row = contentionRow();
    expect(buildSkippedRunNavState(row)!.skippedRun.previous_run_id).toBeUndefined();

    const { container } = renderFromList(row, CONTENTION_RUN_ID);
    expect(screen.queryByTestId('run-detail-skip-previous-run')).not.toBeInTheDocument();
    expect(container.textContent ?? '').not.toContain(SKIPPED_RUN_PREVIOUS_RUN_LABEL);
    expect(screen.getByTestId('run-detail-skip-file')).toBeInTheDocument();
  });

  it('renders neither optional line when the row carried neither', () => {
    const row: Row = { run_id: CONTENTION_RUN_ID, status: 'SKIPPED', skip_reason: 'global_concurrency' };
    const { container } = renderFromList(row, CONTENTION_RUN_ID);
    expect(screen.queryByTestId('run-detail-skip-file')).not.toBeInTheDocument();
    expect(screen.queryByTestId('run-detail-skip-previous-run')).not.toBeInTheDocument();
    expect(container.textContent ?? '').not.toContain(SKIPPED_RUN_FILE_LABEL);
    expect(container.textContent ?? '').not.toContain(SKIPPED_RUN_PREVIOUS_RUN_LABEL);
    // Still states the skip, so the absence above is not the page rendering nothing.
    expect(screen.getByTestId('skip-reason-badge')).toBeInTheDocument();
  });
});

describe('RunDetailPage — no Retry on the skip page, and a route back (p38 R4.4)', () => {
  it('offers Back to Pipeline and no Retry', () => {
    renderFromList(duplicateRow(), DUPLICATE_RUN_ID);

    // Re-requesting an id that correctly 404s cannot succeed, so a retry is a
    // false affordance — p38 R4.4, and `LGR-2bcdd0d-3` rejected one by name for
    // this same reason on this same skip row's data preview.
    expect(screen.queryByTestId('run-detail-retry')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(mockRefetch).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Back to Pipeline' }));
    expect(mockNavigate).toHaveBeenCalledWith(`/pipelines/${PIPELINE_ID}`);
  });
});

describe('RunDetailPage — the 404 copy stays reachable and unchanged (R6.4, R8.5)', () => {
  it('renders p38 R4.1s sentence when no descriptor was carried', () => {
    // A pasted URL, a bookmark, a deep link, a cmd/ctrl/middle-click, or a run id
    // that genuinely does not exist: all arrive with no router state, and branch 3
    // fires. `RunDetailPage.error-states.test.tsx:137-151` asserts this too and
    // must pass unmodified; this repeats the claim so THIS file cannot pass with
    // branch 3 deleted.
    const { container } = renderFromList(null, DUPLICATE_RUN_ID);
    expect(container.textContent).toContain(`No run with id ${DUPLICATE_RUN_ID} exists.`);
    expect(screen.getByRole('button', { name: 'Back to Pipeline' })).toBeInTheDocument();
    expect(screen.queryByTestId('run-detail-retry')).not.toBeInTheDocument();
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
    expect(container.textContent ?? '').not.toContain(SKIPPED_RUN_NO_EXECUTION_STATEMENT);
  });

  it('renders it for a non-skip row too, because the builder attaches nothing', () => {
    // The gate is `describeSkip`'s own, shared with the list side, so a SUCCEEDED
    // row carries no descriptor even though it has a `file_name` — and the page
    // is then byte-equivalent to today's stateless navigation.
    const row: Row = { run_id: 'run-2026-08-26-09-00-00-aabbcc', status: 'SUCCEEDED', file_name: 'x.csv' };
    expect(buildSkippedRunNavState(row)).toBeUndefined();

    const { container } = renderFromList(row, 'run-2026-08-26-09-00-00-aabbcc');
    expect(container.textContent).toContain('No run with id run-2026-08-26-09-00-00-aabbcc exists.');
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
  });

  it('leaves the unavailable branch alone — a carried descriptor does not divert isError', () => {
    // Branch 2 requires `!isError`, so a transport failure on a skip's run id
    // still reports availability rather than claiming the skip is the whole story
    // (p38 R4.2). This is the conjunct that keeps branch 4 reachable.
    mockData = undefined;
    mockIsError = true;
    const { container } = renderFromList(duplicateRow(), DUPLICATE_RUN_ID);
    expect(container.textContent).toContain('Run details could not be retrieved.');
    expect(screen.queryByTestId('skip-reason-badge')).not.toBeInTheDocument();
    expect(screen.getByTestId('run-detail-retry')).toBeInTheDocument();
  });
});
