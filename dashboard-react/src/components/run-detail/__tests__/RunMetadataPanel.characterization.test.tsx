// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `RunMetadataPanel`'s four metadata cells
 * (p54 task 0.3, before task 4.2 adds a fifth).
 *
 * **Validates: Requirements 4.1, 4.2**
 *
 * **Why this file exists.** `RunMetadataPanel` had zero test references anywhere
 * in the tree before it — codegraph flagged it as having no covering tests and
 * the flag is a **verified true positive**. The eight `RunDetailPage.*.test.tsx`
 * files do render the real panel (they `vi.mock('../../hooks')`, not the
 * children — see `RunDetailPage.load-outcome.test.tsx:113`), but not one of them
 * asserts any panel content. So the panel executes under test today while
 * nothing observes what it renders.
 *
 * **Expectations here derive from the requirements, not from current output.**
 * A characterization test written from what the component happens to print
 * ratifies whatever defect is in it. The two authorities used instead:
 *
 * - **p48 R8.3** — *"Where `completed_at` is genuinely absent — a run still in
 *   flight — the UI MUST keep rendering `—` rather than inventing a time.
 *   `RunMetadataPanel` already does this correctly; the requirement is that the
 *   fix not change it."* That is a requirement about **one cell**, and it is the
 *   whole reason two records (`LGR-2bcdd0d-2`, `LGR-1f7892b-1`) name this file
 *   as out of *their* scope.
 * - **the two Run_List_Surfaces' own not-established indication** —
 *   `PipelineOverviewPage.tsx:478` and `ExecutionsPage.tsx:383` both spell it
 *   `'—'` (U+2014 EM DASH). p54 task 4.2 copies that expression byte-for-byte
 *   for its new `Schema Version` cell, so the em dash asserted below is the same
 *   character the whole application already uses for "not established".
 *
 * **What this file is the proof of (p54 do-NOT 10).** Task 4.2 adds a fifth cell
 * whose guard is `!= null`. The `completed_at` ternary is a **truthiness** test,
 * so `undefined`, `null` and `''` all render `—` — recorded at
 * `pipelines_runs.py:2002-2005` and asserted from the producer side in
 * `tests/unit/lambdas/dashboard_api/test_run_details_lineage.py:669-675`. All
 * three falsy shapes are pinned below precisely because "harmonising" the
 * existing ternary with the new cell's `!= null` guard would silently start
 * rendering `formatTimestamp('')` — i.e. `Unknown` — for a row that was read and
 * records no completion. That is the byte-identical requirement, expressed as an
 * assertion rather than as a comment.
 *
 * **The column count is deliberately NOT asserted.** `ColumnLayout columns={4}`
 * becomes `5` in task 4.2, and no requirement governs the layout: p48 R8.3 is
 * about a cell's content and p54 R4.2 is about the not-established indication.
 * Asserting `columns={4}`, or asserting that exactly four cells exist, would
 * force task 4.2 to edit this file — which would destroy its value as the proof
 * that 4.2 left the `completed_at` expression alone. Every assertion below is
 * therefore **positive and per-cell**, and survives a fifth cell appearing
 * beside them.
 *
 * `formatTimestamp` output is locale- and timezone-dependent and carries a
 * relative-time suffix, so the timestamp cells are compared against the shared
 * formatter's own output rather than against a transcribed literal. The
 * formatter is the authority both cells already use, and p48 R8.1's "MUST render
 * a completion time" is a statement about that formatter's result reaching the
 * cell.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import RunMetadataPanel from '../RunMetadataPanel';
import { formatTimestamp } from '../../../utils/formatting';
import type { RunDetail } from '../../../types';

const STARTED_AT = '2026-08-20T14:32:05.000Z';
const COMPLETED_AT = '2026-08-20T14:47:11.000Z';

/** U+2014 EM DASH — the same character both Run_List_Surfaces use. */
const EM_DASH = '\u2014';

/**
 * Minimal `RunDetail` honouring the type's four required fields
 * (`types.ts:102-107`). Transcribed from the interface rather than produced by
 * calling the producer: a fixture derived from the producer's output cannot
 * detect the producer drifting (`LGR-28b4d75-5`).
 */
function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: 'run-0c4f19ab',
    pipeline_key: 'customer-orders-daily',
    status: 'SUCCEEDED',
    started_at: STARTED_AT,
    completed_at: COMPLETED_AT,
    ...overrides,
  };
}

function renderPanel(detail: RunDetail) {
  return render(
    <RunMetadataPanel
      runDetail={detail}
      isActive={false}
      isWebSocketConnected={false}
      optimisticAction={null}
      onApprove={vi.fn()}
      onReject={vi.fn()}
    />,
  );
}

/**
 * The value rendered beside a `Box variant="awsui-key-label"` label. Located by
 * the label's text and read off its parent cell, so the lookup does not depend
 * on the grid's column count or on any Cloudscape class name — both of which
 * task 4.2 changes.
 */
function cellValue(label: string): string {
  const key = screen.getByText(label);
  const cell = key.parentElement;
  expect(cell).not.toBeNull();
  return (cell!.textContent ?? '').replace(label, '').trim();
}

describe('RunMetadataPanel — the four metadata cells (p54 R4.1)', () => {
  it('renders the Status cell as the run\'s own status text', () => {
    renderPanel(runDetail({ status: 'RUNNING' }));

    expect(cellValue('Status')).toBe('RUNNING');
  });

  it('renders the Pipeline cell as the run\'s pipeline key', () => {
    renderPanel(runDetail());

    expect(cellValue('Pipeline')).toBe('customer-orders-daily');
  });

  it('renders the Started cell through the shared timestamp formatter', () => {
    renderPanel(runDetail());

    expect(cellValue('Started')).toBe(formatTimestamp(STARTED_AT));
  });

  it('renders the Completed cell through the shared timestamp formatter (p48 R8.1)', () => {
    renderPanel(runDetail());

    const completed = cellValue('Completed');
    expect(completed).toBe(formatTimestamp(COMPLETED_AT));
    // The positive case is asserted alongside the negative ones below so that
    // "renders an em dash" cannot pass by the cell never rendering at all.
    expect(completed).not.toBe(EM_DASH);
  });
});

describe('RunMetadataPanel — completed_at renders an em dash when absent (p48 R8.3)', () => {
  it('renders an em dash when the key is absent — a run still in flight', () => {
    renderPanel(runDetail({ status: 'RUNNING', completed_at: undefined }));

    expect(cellValue('Completed')).toBe(EM_DASH);
  });

  it('renders an em dash when the value is null', () => {
    // The wire can carry an explicit null; `RunDetail.completed_at` is declared
    // `string | undefined`, so the cast is what lets the falsy shape the
    // producer can actually send be exercised.
    renderPanel(runDetail({ completed_at: null as unknown as string }));

    expect(cellValue('Completed')).toBe(EM_DASH);
  });

  it('renders an em dash when the value is the empty string', () => {
    // The load-bearing case for p54 do-NOT 10. A row that WAS read and records
    // no completion is present-and-empty (`test_run_details_lineage.py:771-775`).
    // The existing ternary is a truthiness test, so `''` renders the em dash. A
    // `!= null` guard — the expression task 4.2's new cell uses — would render
    // `formatTimestamp('')` here instead, inventing a time for a run that has
    // not finished. This assertion is what forbids that harmonisation.
    renderPanel(runDetail({ completed_at: '' }));

    expect(cellValue('Completed')).toBe(EM_DASH);
    expect(cellValue('Completed')).not.toContain('Unknown');
  });

  it('renders an em dash for started_at on the same terms', () => {
    // `started_at` guards identically (`RunMetadataPanel.tsx:52`). Pinned so the
    // two timestamp cells cannot be made to disagree about what absence means.
    renderPanel(runDetail({ started_at: '' as unknown as string }));

    expect(cellValue('Started')).toBe(EM_DASH);
  });
});

describe('RunMetadataPanel — pipeline_key is deliberately unguarded', () => {
  it('renders blank rather than an em dash when the pipeline key is empty', () => {
    // Not an oversight and not this spec's to change. `RunDetail.pipeline_key`
    // is declared **required** (`types.ts:104`) and p48 fixed the blank-Pipeline
    // defect on the **producer** side — `get_run_details` now always assigns the
    // key — rather than by adding a `'—'` fallback here. The accepted
    // consequence is recorded at
    // `test_run_details_lineage.py:1064-1068`: `''` renders "the same blank it
    // renders for `undefined`, so nothing about the UI changes for this case".
    // Pinned so that adding the Schema Version cell does not quietly reverse
    // that decision by giving this cell a fallback the producer contract makes
    // unnecessary.
    renderPanel(runDetail({ pipeline_key: '' }));

    expect(cellValue('Pipeline')).toBe('');
  });
});
