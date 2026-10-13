// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `RunMetadataPanel`'s fifth cell — Schema Version (p54 task 4.2).
 *
 * **Validates: Requirements 4.1, 4.2**
 *
 * Two assertions, which is the whole of what R4.1 and R4.2 ask of this surface:
 * a resolved version renders as the badge the two Run_List_Surfaces already
 * render, and an unresolved one renders the same not-established indication
 * those surfaces already use.
 *
 * **R4.2 is a sameness requirement, not a new indication.** The expression in
 * `RunMetadataPanel.tsx` is copied byte-for-byte from `ExecutionsPage.tsx:383`
 * and `PipelineOverviewPage.tsx:542`, so the em dash asserted below is the same
 * U+2014 character all three surfaces spell it with. The key-spelling half of
 * that sameness — that the producer and all three readers agree the attribute is
 * `schema_version` — is asserted across the language boundary in
 * `tests/contracts/api_responses/test_run_details_contract.py`
 * (`TestRunDetailsSchemaVersionKeyContract`), because a TypeScript test cannot
 * see the Python producer.
 *
 * **Why this is a separate file from the characterization suite.**
 * `RunMetadataPanel.characterization.test.tsx` (p54 task 0.3) is the proof that
 * this task left the `completed_at` ternary byte-identical, and it passes
 * unmodified. Adding to it would weaken that: a file edited by the change it is
 * meant to pin cannot testify about the change. Its own header records that it
 * deliberately asserts no column count so that adding a fifth cell requires no
 * edit to it.
 *
 * **The guards differ on purpose.** This cell tests `!= null`; the two timestamp
 * cells test truthiness. 0.3 verified the trap by mutation — switching
 * `completed_at` to `!= null` fails its empty-string case, because
 * `formatTimestamp('')` returns `Unknown`. `schema_version` has no
 * present-and-empty shape to distinguish (`types.ts:161-170`, `LGR-44d601e-1`
 * clears a value by removing the key), so `!= null` is sufficient here and the
 * two guards must not be harmonised (p54 do-NOT 10, p48 R8.3).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import RunMetadataPanel from '../RunMetadataPanel';
import type { RunDetail } from '../../../types';

/** U+2014 EM DASH — the character all three surfaces use for "not established". */
const EM_DASH = '\u2014';

function runDetail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: 'run-0c4f19ab',
    pipeline_key: 'customer-orders-daily',
    status: 'SUCCEEDED',
    started_at: '2026-08-20T14:32:05.000Z',
    completed_at: '2026-08-20T14:47:11.000Z',
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
 * The value rendered beside a `Box variant="awsui-key-label"` label, read off the
 * label's parent cell. Same lookup the characterization suite uses, so neither
 * file depends on the grid's column count or on a Cloudscape class name.
 */
function cellValue(label: string): string {
  const key = screen.getByText(label);
  const cell = key.parentElement;
  expect(cell).not.toBeNull();
  return (cell!.textContent ?? '').replace(label, '').trim();
}

describe('RunMetadataPanel — the Schema Version cell (p54 R4.1)', () => {
  it('renders the version as a v-prefixed badge when the run has one', () => {
    renderPanel(runDetail({ schema_version: 3 }));

    expect(cellValue('Schema Version')).toBe('v3');
  });

  it('renders v1 rather than an em dash for the first version', () => {
    // `1` is truthy, so this passes under either guard — but `0` would not, and
    // the producer's contract is that an unresolved version is ABSENT rather
    // than `0` (R4.4, `LGR-44d601e-1`). Asserted so a later reader who reaches
    // for a truthiness guard here — harmonising with the timestamp cells above —
    // has the ordinal boundary pinned rather than merely commented.
    renderPanel(runDetail({ schema_version: 1 }));

    expect(cellValue('Schema Version')).toBe('v1');
  });
});

describe('RunMetadataPanel — Schema Version renders an em dash when absent (p54 R4.2)', () => {
  it('renders an em dash when the key is absent from the body', () => {
    // The dominant live case. `get_run_details` omits the key entirely for all
    // five ways the version fails to resolve — no `schema_extract` row, no
    // `schema_hash` on it, an unknown hash, a page-capped walk, a
    // deadline-truncated walk — so absence is what this cell renders most often.
    renderPanel(runDetail());

    expect(cellValue('Schema Version')).toBe(EM_DASH);
  });

  it('renders an em dash when the value is null', () => {
    // `RunDetail.schema_version` is declared `number | undefined`, so the cast is
    // what lets an explicit wire `null` be exercised. `!= null` catches both,
    // which is why the guard is `!= null` and not `!== undefined`.
    renderPanel(runDetail({ schema_version: null as unknown as number }));

    expect(cellValue('Schema Version')).toBe(EM_DASH);
  });
});
