// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Queued Files footer stops promising, and `failed_dlq` finally renders —
 * p54 task 7.4 (_Requirements: 9.2, 9.3, 9.4_).
 *
 * **Validates: Requirements 9.2, 9.3, 9.4**
 *
 * **This is the first test in the tree to render a non-empty `queued_files` array,
 * and the first to exercise the `failed_dlq` branch at all.** Every existing
 * `usePipelineExecutions` mock in this directory hard-codes `queued_files: []`, so
 * `ExecutionsPage.tsx`'s Queued Files table (`:588-616`, gated on `queuedCount > 0`)
 * has never rendered under test — including the `failed_dlq` arm of its Status cell,
 * which had **zero producers repo-wide** until 7.4 gave it one.
 *
 * ## What each assertion is for
 *
 * **R9.2 — the old sentence is gone, not merely supplemented.** The footer read
 * *"Files queued — will process after current execution completes"*. That is an
 * unconditional promise, and it is false the moment a gate holds the pipeline lock:
 * a run parked at a human approval gate can outlast the file's ~25-minute
 * Redelivery_Budget (`maxReceiveCount` × `visibilityTimeout`), after which the file
 * goes to the DLQ. So the absence assertion is the substantive half — a page that
 * added an honest sentence *beside* the promise would still be showing the promise.
 *
 * **R9.3/R9.4 — the two states render differently, in words.** Asserted in both
 * directions on one page: a deferred entry reads "Queued", a lost one reads
 * "Failed (DLQ)" and the reason sentence appears. One direction alone would pass for
 * a surface that labelled every entry lost. Words rather than colour, because
 * `LGR-99050e6-1` (**template / ratified**) records that colour alone fails
 * **WCAG 1.4.1** and `LGR-1f7892b-2` re-states it for a `StatusIndicator` type
 * specifically — *"a `warning` type *is* colour, so the sentence is the carrier and
 * the type is not"*.
 *
 * **The reason sentence is conditional**, so a page with only deferred files makes no
 * mention of loss. That is R9.5's three-state discipline on the copy: absence of the
 * sentence means "nothing here is lost", which is a different fact from "something
 * here is lost" and from "we do not know".
 *
 * Shaped after `ExecutionsPage.load-outcome.test.tsx`, which mocks the same hook.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import { ToastProvider } from '../../contexts/ToastContext';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { Pipeline } from '../../types';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => vi.fn() };
});

interface QueuedRow {
  file_name: string;
  source_key: string;
  queued_at: string;
  status: string;
}

let mockQueued: QueuedRow[] = [];

vi.mock('../../hooks', () => ({
  usePipelineExecutions: () => ({
    data: {
      executions: [],
      queued_files: mockQueued,
      pagination: {
        page: 1,
        page_size: 10,
        total_count: 0,
        total_pages: 1,
        has_next: false,
        has_prev: false,
        next_cursor: null,
        partial_counts: [],
      },
    },
  }),
  useCancelRun: () => ({ mutate: vi.fn(), isPending: false }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useRetryRun: () => ({ mutate: vi.fn(), isPending: false }),
}));

import ExecutionsPage from '../ExecutionsPage';

const mockPipeline: Pipeline = {
  pipeline_key: 'latesttesting-19ca826a',
  pipeline_name: 'latesttesting',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2026-08-20T17:00:00Z',
  updated_at: '2026-08-24T13:48:26Z',
};

/** The published shape, four keys — `queued_at` is `started_at` renamed on the way
 *  out (`pipelines_runs.py:762`), and there is deliberately no `run_id`. */
function queued(file_name: string, status: string): QueuedRow {
  return {
    file_name,
    source_key: `mynewtesting/${file_name}`,
    queued_at: '2026-08-27T09:00:00.000000+00:00',
    status,
  };
}

function renderWith(queuedFiles: QueuedRow[]) {
  cleanup();
  mockQueued = queuedFiles;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter>
          <PipelineProvider
            value={{ pipeline: mockPipeline, isLoading: false, isError: false, refetch: vi.fn() }}
          >
            <ExecutionsPage />
          </PipelineProvider>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/** The exact sentence R9.2 removes. Spelled out so a partial reword still fails. */
const OLD_PROMISE = /will process after current execution completes/i;

describe('ExecutionsPage — Queued Files footer (p54 R9.2)', () => {
  it('no longer promises the file will be processed when the run completes', () => {
    renderWith([queued('customer_data_5.csv', 'queued')]);

    expect(screen.queryByText(OLD_PROMISE)).toBeNull();
  });

  it('states what is known — a run is holding the pipeline — with no claim about when', () => {
    renderWith([queued('customer_data_5.csv', 'queued')]);

    // The sentence is behind the header's question-mark button, not printed under
    // the table.
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'About queued files' }));
    expect(
      screen.getByText(/a run is currently holding this pipeline/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(OLD_PROMISE)).toBeNull();
  });

  it('renders nothing at all when no file is queued', () => {
    // The whole Container is gated on `queuedCount > 0`, so an empty array adds
    // nothing to the page — absence is the third state and is not a value.
    renderWith([]);

    expect(screen.queryByText(/queued files/i)).toBeNull();
    expect(screen.queryByRole('button', { name: 'About queued files' })).toBeNull();
    expect(screen.queryByText(/a run is currently holding this pipeline/i)).toBeNull();
    expect(screen.queryByText(OLD_PROMISE)).toBeNull();
  });
});

describe('ExecutionsPage — the two queued states (p54 R9.3, R9.4)', () => {
  it('renders a deferred file as Queued and a lost one as Failed (DLQ)', () => {
    renderWith([
      queued('customer_data_5.csv', 'queued'),
      queued('customer_data_7.csv', 'failed_dlq'),
    ]);

    // Both file names are on the page — R9.4's "state the file name".
    expect(screen.getByText('customer_data_5.csv')).toBeInTheDocument();
    expect(screen.getByText('customer_data_7.csv')).toBeInTheDocument();

    // Both directions, so neither passes for a surface that labels everything one way.
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(screen.getByText('Failed (DLQ)')).toBeInTheDocument();
  });

  it('states the reason a lost file was lost, in words', () => {
    renderWith([queued('customer_data_7.csv', 'failed_dlq')]);

    expect(
      screen.getByText(/waited longer than its redelivery attempts allowed/i),
    ).toBeInTheDocument();
  });

  it('says nothing about loss when every queued file is still deferred', () => {
    renderWith([queued('customer_data_5.csv', 'queued')]);

    expect(screen.queryByText(/redelivery attempts/i)).toBeNull();
    expect(screen.queryByText('Failed (DLQ)')).toBeNull();
  });
});
