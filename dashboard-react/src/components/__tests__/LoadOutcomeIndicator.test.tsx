// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the run list's Load_Outcome indicator (p37 task 6.6, R1.7/R1.9).
 *
 * **Validates: Requirements 1.7, 1.9**
 *
 * The defect this indicator exists for: a run reported `SUCCEEDED` — no
 * `TaskFailed` or `ExecutionFailed` event anywhere in its Step Functions
 * history — while writing zero rows to its target. So the assertions that
 * matter here are the ones about *distinctness* and *silence*, and both
 * directions of each are asserted:
 *
 * - `no_rows_loaded` renders a warning **and** `complete_load` renders nothing
 *   (R1.7). One assertion on its own proves nothing: an indicator that
 *   rendered a warning for every outcome would satisfy "the warning is present"
 *   and would not distinguish anything.
 * - `partial_load` renders the withheld count at **any** N, with no threshold
 *   (R1.9 / design D1).
 * - `not_established` renders **nothing at all** — an empty container, not a
 *   muted dash. It is a reachable steady state (design §5 risk 3), not a
 *   migration artefact, and an indicator on a common steady state is the
 *   cry-wolf failure that would stop the `no_rows_loaded` warning working.
 *
 * These are examples rather than properties on purpose: the indicator space is
 * a fixed five-way enumeration plus absence, so randomising over it buys
 * nothing that enumerating it does not already give.
 *
 * Fixtures carry the `reason` strings transcribed from
 * `shared/load_outcome.py::derive_load_outcome` rather than invented copy, so
 * producer/consumer drift on the wire shape fails here (`LGR-28b4d75-5`).
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoadOutcomeIndicator, { describeLoadOutcome } from '../LoadOutcomeIndicator';
import type { LoadOutcome, LoadOutcomeKind } from '../../types';

/** One established Load_Outcome, as `get_pipeline_executions` attaches it. */
function established(over: Partial<LoadOutcome> & { outcome: LoadOutcomeKind }): LoadOutcome {
  return {
    rows_read: 0,
    rows_written: 0,
    rows_not_written: 0,
    blocks_run: false,
    missing: [],
    determined_from: 'load_metrics_row',
    reason: '',
    ...over,
  };
}

const NO_ROWS_LOADED = established({
  outcome: 'no_rows_loaded',
  rows_read: 5,
  rows_written: 0,
  rows_not_written: 5,
  reason:
    'This run read 5 row(s) and wrote none to the target. Every row was withheld — ' +
    'quarantined by a data quality rule, or dropped by the transform.',
});

const PARTIAL_LOAD = established({
  outcome: 'partial_load',
  rows_read: 1200,
  rows_written: 1150,
  rows_not_written: 50,
  reason: 'This run read 1200 row(s) and wrote 1150 to the target; 50 row(s) were not written.',
});

const COMPLETE_LOAD = established({
  outcome: 'complete_load',
  rows_read: 1200,
  rows_written: 1200,
  reason:
    'This run read 1200 row(s) and wrote all of them to the target. Note that this counts ' +
    'rows presented to the target writer, not rows re-read from the target.',
});

const EMPTY_SOURCE = established({
  outcome: 'empty_source',
  reason:
    "This run's source had no rows, so writing none to the target is the correct outcome " +
    'rather than a loss.',
});

/** No counts row for this run — the pre-p37 and failed-telemetry steady state. */
const NOT_ESTABLISHED: LoadOutcome = {
  outcome: 'not_established',
  rows_read: null,
  rows_written: null,
  rows_not_written: 0,
  blocks_run: false,
  missing: ['rows_read', 'rows_written'],
  determined_from: 'load_metrics_row_absent',
  reason:
    'How many rows this run read and wrote was not established ' +
    '(load_metrics_row_absent), so what reached the target cannot be stated. ' +
    'This is not a claim that nothing loaded.',
};

describe('describeLoadOutcome — no_rows_loaded is distinct from complete_load (R1.7)', () => {
  it('describes no_rows_loaded as a warning that names the zero', () => {
    const descriptor = describeLoadOutcome(NO_ROWS_LOADED);

    expect(descriptor).not.toBeNull();
    expect(descriptor!.type).toBe('warning');
    expect(descriptor!.text).toBe('0 rows loaded');
    expect(descriptor!.testId).toBe('load-outcome-no-rows-loaded');
  });

  it('describes complete_load as nothing — the run’s own success indicator says it', () => {
    expect(describeLoadOutcome(COMPLETE_LOAD)).toBeNull();
  });

  it('renders the warning for no_rows_loaded', () => {
    render(<LoadOutcomeIndicator outcome={NO_ROWS_LOADED} />);

    expect(screen.getByTestId('load-outcome-no-rows-loaded').textContent).toContain(
      '0 rows loaded',
    );
  });

  it('renders no indicator at all for complete_load — the other direction of the same claim', () => {
    const { container } = render(<LoadOutcomeIndicator outcome={COMPLETE_LOAD} />);

    expect(screen.queryByTestId('load-outcome-no-rows-loaded')).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });

  it('carries the meaning in text, not in colour alone', () => {
    // `LGR-99050e6-1` (template / ratified): colour alone fails WCAG 1.4.1 and
    // makes the distinction invisible, so the visible text states it.
    render(<LoadOutcomeIndicator outcome={NO_ROWS_LOADED} />);

    expect(screen.getByText('0 rows loaded')).toBeInTheDocument();
  });

  it('carries the deriver’s own sentence as the hover explanation', () => {
    render(<LoadOutcomeIndicator outcome={NO_ROWS_LOADED} />);

    expect(screen.getByTestId('load-outcome-no-rows-loaded')).toHaveAttribute(
      'title',
      NO_ROWS_LOADED.reason,
    );
  });
});

describe('describeLoadOutcome — partial_load always shows the withheld count (R1.9)', () => {
  it('states the count and stays informational, never a warning', () => {
    const descriptor = describeLoadOutcome(PARTIAL_LOAD);

    expect(descriptor!.type).toBe('info');
    expect(descriptor!.text).toBe('50 rows not written');
    expect(descriptor!.testId).toBe('load-outcome-partial-load');
  });

  it('shows the count at N = 1 — there is no threshold anywhere in this spec (D1)', () => {
    const descriptor = describeLoadOutcome(
      established({ outcome: 'partial_load', rows_read: 1000, rows_written: 999, rows_not_written: 1 }),
    );

    expect(descriptor!.text).toBe('1 row not written');
  });

  it('localises a large count rather than printing a bare integer', () => {
    const descriptor = describeLoadOutcome(
      established({
        outcome: 'partial_load',
        rows_read: 2_000_000,
        rows_written: 1_000_000,
        rows_not_written: 1_000_000,
      }),
    );

    expect(descriptor!.text).toBe(`${(1_000_000).toLocaleString()} rows not written`);
  });

  it('says rows were withheld rather than inventing a number when the count did not arrive', () => {
    const descriptor = describeLoadOutcome({
      ...PARTIAL_LOAD,
      rows_not_written: undefined as unknown as number,
    });

    expect(descriptor!.text).toBe('Some rows not written');
    expect(descriptor!.text).not.toMatch(/\d/);
  });

  it('renders the count in the DOM', () => {
    render(<LoadOutcomeIndicator outcome={PARTIAL_LOAD} />);

    expect(screen.getByTestId('load-outcome-partial-load').textContent).toContain(
      '50 rows not written',
    );
  });
});

describe('describeLoadOutcome — not_established renders nothing at all', () => {
  it('describes it as nothing rather than as an unknown-value indicator', () => {
    expect(describeLoadOutcome(NOT_ESTABLISHED)).toBeNull();
  });

  it('renders an empty container — not a dash, not a muted "unknown"', () => {
    const { container } = render(<LoadOutcomeIndicator outcome={NOT_ESTABLISHED} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('treats an absent load_outcome identically to not_established', () => {
    // A response cached before p37 shipped, and every fixture that predates it,
    // carries no `load_outcome` at all. Both mean "we do not know".
    const absent = render(<LoadOutcomeIndicator outcome={undefined} />);
    expect(absent.container).toBeEmptyDOMElement();

    const nulled = render(<LoadOutcomeIndicator outcome={null} />);
    expect(nulled.container).toBeEmptyDOMElement();
  });

  it('renders nothing for an unrecognised wire value rather than guessing a severity', () => {
    const unknown = { ...NOT_ESTABLISHED, outcome: 'some_future_outcome' as LoadOutcomeKind };

    expect(describeLoadOutcome(unknown)).toBeNull();
    expect(render(<LoadOutcomeIndicator outcome={unknown} />).container).toBeEmptyDOMElement();
  });
});

describe('describeLoadOutcome — empty_source is stated, but never as loss (R1.3)', () => {
  it('names the source and stays informational', () => {
    const descriptor = describeLoadOutcome(EMPTY_SOURCE);

    expect(descriptor!.type).toBe('info');
    expect(descriptor!.text).toBe('Empty source — no rows to load');
    expect(descriptor!.testId).toBe('load-outcome-empty-source');
  });

  it('is not the same indicator as no_rows_loaded — an empty poll is a legitimate no-op', () => {
    const emptySource = describeLoadOutcome(EMPTY_SOURCE)!;
    const noRows = describeLoadOutcome(NO_ROWS_LOADED)!;

    expect(emptySource.type).not.toBe(noRows.type);
    expect(emptySource.testId).not.toBe(noRows.testId);
    expect(emptySource.text).not.toMatch(/0 rows loaded/);
  });

  it('renders through its own test id', () => {
    render(<LoadOutcomeIndicator outcome={EMPTY_SOURCE} />);

    expect(screen.getByTestId('load-outcome-empty-source').textContent).toContain(
      'Empty source',
    );
  });
});

describe('describeLoadOutcome — the five-way enumeration is closed', () => {
  it('renders exactly three of the five outcomes and is silent on the other two', () => {
    const rendered: LoadOutcomeKind[] = [];
    const silent: LoadOutcomeKind[] = [];

    for (const outcome of [NO_ROWS_LOADED, PARTIAL_LOAD, EMPTY_SOURCE, COMPLETE_LOAD, NOT_ESTABLISHED]) {
      (describeLoadOutcome(outcome) ? rendered : silent).push(outcome.outcome);
    }

    expect(rendered).toEqual(['no_rows_loaded', 'partial_load', 'empty_source']);
    expect(silent).toEqual(['complete_load', 'not_established']);
  });

  it('never uses an error severity — a Load_Outcome describes a run, it does not fail one', () => {
    for (const outcome of [NO_ROWS_LOADED, PARTIAL_LOAD, EMPTY_SOURCE]) {
      expect(['warning', 'info']).toContain(describeLoadOutcome(outcome)!.type);
    }
  });
});
