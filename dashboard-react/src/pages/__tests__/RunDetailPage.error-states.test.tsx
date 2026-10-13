// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The run detail page's two error states (p38 task 8.4, `R4.1`–`R4.4`).
 *
 * **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
 *
 * Task 8.2 split one collapsed guard — `isError || !runDetail` rendering the
 * single string `"Failed to load run details"` — into two branches:
 *
 * - `!isError && runDetail === null` → `No run with id {runId} exists.`, with
 *   **Back to Pipeline only** (`RunDetailPage.tsx:367-386`).
 * - `isError || !runDetail` → `Run details could not be retrieved.`, with a
 *   Retry that calls `refetch()` **and** Back to Pipeline (`:392-409`).
 *
 * The two states come from task 8.1's narrowing of `fetchRunDetail`: it now
 * returns `null` **only** for a 404 and rejects on everything else
 * (`use-run-detail.ts:16-33`), so `data === null && !isError` means "no such
 * run" and `isError` means "the request did not complete". 8.1 chose a plain
 * `Error` rather than an `ApiError` deliberately, because `main.tsx` toasts only
 * for `ApiError` — no global toast fires on the unavailable branch, so the
 * page's own text is the whole user-facing signal. That is why these four tests
 * exist.
 *
 * **No test covered either branch before this file.** All five sibling
 * `RunDetailPage.*.test.tsx` files hard-code `isError: false` on `useRunDetail`
 * and supply a run, and a grep of `dashboard-react/src` for
 * `data-testid="run-detail-retry"` returns exactly one hit, the render site
 * itself.
 *
 * **Example-based, not property-based, on purpose.** Two states is the whole
 * input space of this branch; 100 generated renders would find nothing that two
 * examples do not.
 *
 * **Both branches deliberately keep `StatusIndicator type="error"`.** The
 * distinction is carried by the wording alone, which is what makes the textual
 * difference the *only* difference. `LGR-99050e6-1` (**template / ratified**)
 * is the record: colour alone fails WCAG 1.4.1, so each surface also carries a
 * written statement of its state. The `R4.3` test below therefore holds the
 * indicator type constant and asserts the *text* differs — asserting two
 * different indicator types would contradict both the design and the record.
 *
 * Harness taken from `RunDetailPage.load-outcome.test.tsx` unchanged: the page
 * mounts with `../../hooks` mocked, because the page reads all of them before
 * the early returns.
 *
 * **These are DOM assertions under jsdom, not observations of what a user
 * sees.** `R4.1`–`R4.4` are visual surfaces and this file does not close the
 * observation gap task 11.1 records.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { RunDetail } from '../../types';

/**
 * The live phantom id from `requirements.md` — the run the dashboard navigated
 * to and got a 404 for, which is the case `R4.1` exists for. Using the observed
 * id rather than a hand-made one keeps the fixture side honest
 * (`LGR-28b4d75-5`, **template / ratified**).
 */
const RUN_ID = 'retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303';
const PIPELINE_ID = 'newtest-ab2a08a0';

/** Cloudscape emits this class for `StatusIndicator type="error"`. Precedent: `ExecutionsPage.skipped.test.tsx:135`. */
const ERROR_INDICATOR = '[class*="status-error"]';

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

/** 8.1's mapping: `null` with no error means there is no run with this id. */
function notFound() {
  mockData = null;
  mockIsError = false;
}

/** 8.1's mapping: a rejection means the request did not complete. */
function unavailable() {
  mockData = undefined;
  mockIsError = true;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/pipelines/${PIPELINE_ID}/runs/${RUN_ID}`]}>
        <Routes>
          <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  notFound();
  mockRefetch = vi.fn();
  mockNavigate.mockClear();
});

describe('RunDetailPage — no run with that id exists (R4.1, R4.4)', () => {
  it('names the run id and offers Back to Pipeline with no Retry', () => {
    notFound();
    const { container } = renderPage();

    // R4.1 — the id is the subject, because a 404 on an id the dashboard just
    // navigated to is a claim about identity, not availability. The id arrives
    // from `useParams`, so this asserts the route's id reaches the sentence.
    expect(container.textContent).toContain(`No run with id ${RUN_ID} exists.`);

    // R4.4 — the route back survives the split.
    expect(screen.getByRole('button', { name: 'Back to Pipeline' })).toBeInTheDocument();

    // R4.4's note, and the more meaningful of the two assertions: a
    // retry-the-request button is the wrong affordance on a 404, because
    // re-requesting an id that correctly 404s cannot succeed.
    expect(screen.queryByTestId('run-detail-retry')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });
});

describe('RunDetailPage — the request did not complete (R4.2, R4.4)', () => {
  it('states the details could not be retrieved and re-requests them on Retry', () => {
    unavailable();
    const { container } = renderPage();

    // R4.2 — availability, stated as availability. No global toast fires on
    // this branch (8.1 throws a plain `Error`, and `main.tsx` toasts only for
    // `ApiError`), so this sentence is the entire user-facing signal.
    expect(container.textContent).toContain('Run details could not be retrieved.');
    expect(container.textContent).not.toContain('No run with id');

    // R4.4 — retrying the request can succeed here, so this is the one branch
    // carrying the affordance, and it must actually re-request.
    expect(mockRefetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('run-detail-retry'));
    expect(mockRefetch).toHaveBeenCalledTimes(1);
  });
});

describe('RunDetailPage — the two states are distinguishable in text (R4.3)', () => {
  it('differs in wording while the indicator type stays constant', () => {
    // LGR-99050e6-1 (template / ratified): colour alone fails WCAG 1.4.1, so
    // the written statement carries the state. Holding the indicator type
    // constant across both renders is what makes the text the only difference —
    // a test asserting two different indicator types would assert the opposite
    // of what the record requires.
    notFound();
    const first = renderPage();
    const notFoundText = first.container.textContent ?? '';
    const notFoundIndicator = first.container.querySelector(ERROR_INDICATOR);
    expect(notFoundIndicator).not.toBeNull();
    const notFoundClass = notFoundIndicator!.className;
    first.unmount();

    unavailable();
    const second = renderPage();
    const unavailableText = second.container.textContent ?? '';
    const unavailableIndicator = second.container.querySelector(ERROR_INDICATOR);
    expect(unavailableIndicator).not.toBeNull();

    // Same visual treatment on both branches — nothing about the colour tells
    // the two states apart.
    expect(unavailableIndicator!.className).toEqual(notFoundClass);

    // And yet the two are distinguishable, in text: each names its own state
    // and neither sentence appears on the other branch.
    expect(notFoundText).not.toEqual(unavailableText);
    expect(notFoundText).toMatch(/no run with id .+ exists/i);
    expect(notFoundText).not.toMatch(/could not be retrieved/i);
    expect(unavailableText).toMatch(/run details could not be retrieved/i);
    expect(unavailableText).not.toMatch(/no run with id/i);
  });
});

describe('RunDetailPage — Back to Pipeline on both branches (R4.4)', () => {
  it('routes back to the pipeline from the not-found branch and from the unavailable one', () => {
    notFound();
    const first = renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Back to Pipeline' }));
    expect(mockNavigate).toHaveBeenCalledWith(`/pipelines/${PIPELINE_ID}`);
    first.unmount();
    mockNavigate.mockClear();

    unavailable();
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Back to Pipeline' }));
    expect(mockNavigate).toHaveBeenCalledWith(`/pipelines/${PIPELINE_ID}`);
  });
});
