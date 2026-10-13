// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the run detail page's Load_Outcome label (p37 task 6.6, R1.8).
 *
 * **Validates: Requirements 1.8**
 *
 * R1.8 has two halves and the second is the one a label can satisfy in letter
 * while still being useless: state the counts, **and** say where the unwritten
 * rows went. Pointing at nothing is the failure this surface exists to avoid,
 * so both location arms are asserted, and arm 2 is asserted in **both** of its
 * wordings:
 *
 * 1. `quarantine_prefix` — the run's DQ report carries `quarantine_path`, so the
 *    S3 prefix is named.
 * 2. `no_location_recorded` — it does not.
 *    - No quarantine evidence at all ⇒ the rows were dropped by the transform,
 *      and the label says so.
 *    - The report reports rows withheld by the quality check but carries no
 *      path, **or carries no `rows_quarantined` counter at all** ⇒ the label
 *      says no location was recorded *and* that the rows cannot be attributed
 *      to the transform either, because claiming they were would be false.
 *      `LGR-99050e6-2` (**template / ratified**) is the record: an absent
 *      quarantine counter is reported as **unknown rather than as zero**.
 *
 * The label is a **sibling** of `QualityVerdictLabel`, not a fifth branch of it
 * — that separation is pinned in `QualityVerdictLabel.rows-passed.test.tsx`,
 * which asserts the quality label still renders its own report unchanged
 * (R4.5).
 *
 * Examples rather than properties: a fixed five-way enumeration crossed with a
 * three-way report state, all of which are enumerated below.
 *
 * `reason` strings and the DQ-report fixtures are transcribed from their
 * producers — `shared/load_outcome.py::derive_load_outcome` and
 * `pyspark_templates/glue_job_s3_tables.py.j2` — rather than produced by calling
 * either, so shape drift fails here (`LGR-28b4d75-5`).
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import LoadOutcomeLabel, { describeLoadOutcomeLabel } from '../LoadOutcomeLabel';
import { describeLoadOutcome } from '../LoadOutcomeIndicator';
import type { LoadOutcome, LoadOutcomeKind } from '../../types';
import type { QualityReport } from '../../hooks/use-quality-report';

/**
 * `get_quality_report` returns the `(run_id, "data_quality")` state row verbatim
 * with no projection, so a faithful fixture carries keys the frontend type does
 * not declare.
 */
type StateRow = QualityReport & Record<string, unknown>;

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

/** The observed run: 5 rows read, every one quarantined, none written. */
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
  reason: 'This run read 1200 row(s) and wrote all of them to the target.',
});

const EMPTY_SOURCE = established({
  outcome: 'empty_source',
  reason: "This run's source had no rows, so writing none to the target is the correct outcome.",
});

const NOT_ESTABLISHED: LoadOutcome = {
  outcome: 'not_established',
  rows_read: null,
  rows_written: null,
  rows_not_written: 0,
  blocks_run: false,
  missing: ['rows_read', 'rows_written'],
  determined_from: 'load_metrics_row_absent',
  reason:
    'How many rows this run read and wrote was not established (load_metrics_row_absent), ' +
    'so what reached the target cannot be stated. This is not a claim that nothing loaded.',
};

/** Inline producer, every row quarantined — the observed run's DQ row. */
const INLINE_WITH_PREFIX: StateRow = {
  pipeline_id: 'pipe-1',
  run_id: 'run-1',
  step_name: 'data_quality',
  overall_status: 'fail',
  checks: [{ name: 'Completeness "phone" >= 0.95', status: 'fail', actual: 0 }],
  root_cause: 'data_issue',
  recommendations: ['Review failed data quality rules'],
  source: 'glue_inline_dq',
  rules_passed: 10,
  rules_failed: 1,
  rules_total: 11,
  rows_passed: 0,
  rows_quarantined: 5,
  quarantine_path: 's3://adi-quarantine/quarantine/run-1/',
};

/** Same row with the locator missing — arm 2's "cannot be attributed" wording. */
const INLINE_WITHOUT_PREFIX: StateRow = { ...INLINE_WITH_PREFIX, quarantine_path: null };

/** Inline producer that quarantined nothing — arm 2's transform wording. */
const INLINE_NOTHING_QUARANTINED: StateRow = {
  ...INLINE_WITH_PREFIX,
  overall_status: 'pass',
  rules_failed: 0,
  rows_quarantined: 0,
  quarantine_path: null,
  rows_passed: 1150,
};

/** On-demand agent row: Athena queries after the load. No counters at all. */
const AGENT_REPORT: StateRow = {
  pipeline_id: 'pipe-1',
  run_id: 'run-1',
  step_name: 'data_quality',
  agent_type: 'data_quality',
  overall_status: 'pass',
  checks: [{ name: 'row_count', status: 'pass', actual: 0 }],
  root_cause: null,
  recommendations: [],
};

describe('describeLoadOutcomeLabel — both counts are stated (R1.8)', () => {
  it('states Rows_Read and Rows_Written for a run that loaded nothing', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_WITH_PREFIX)!;

    expect(statement.counts).toContain('read 5 rows');
    expect(statement.counts).toContain('wrote 0 rows');
  });

  it('states both counts for a partial load, localised', () => {
    const statement = describeLoadOutcomeLabel(PARTIAL_LOAD, null)!;

    expect(statement.counts).toContain(`read ${(1200).toLocaleString()} rows`);
    expect(statement.counts).toContain(`wrote ${(1150).toLocaleString()} rows`);
  });

  it('uses singular wording for a single row', () => {
    const statement = describeLoadOutcomeLabel(
      established({ outcome: 'complete_load', rows_read: 1, rows_written: 1 }),
      null,
    )!;

    expect(statement.counts).toContain('read 1 row from the source');
    expect(statement.counts).toContain('wrote 1 row to the target');
  });

  it('says a count was not recorded rather than printing a number it does not have', () => {
    const statement = describeLoadOutcomeLabel(
      { ...NO_ROWS_LOADED, rows_read: null },
      INLINE_WITH_PREFIX,
    )!;

    expect(statement.counts).toContain('an unrecorded number of rows');
    expect(statement.counts).not.toMatch(/read (null|undefined|NaN)/);
  });

  it('renders both counts on the label itself', () => {
    render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={INLINE_WITH_PREFIX} />);

    const counts = screen.getByTestId('load-outcome-counts');
    expect(counts.textContent).toContain('read 5 rows');
    expect(counts.textContent).toContain('wrote 0 rows');
  });
});

describe('describeLoadOutcomeLabel — arm 1: the quarantine prefix is named (R1.8)', () => {
  it('names the S3 prefix the DQ report carries', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_WITH_PREFIX)!;

    expect(statement.location).toBe('quarantine_prefix');
    expect(statement.withheld).toContain('s3://adi-quarantine/quarantine/run-1/');
    expect(statement.withheld).toMatch(/quarantined by the data quality check/i);
  });

  it('does not blame the transform when the rows were demonstrably quarantined', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_WITH_PREFIX)!;

    expect(statement.withheld).not.toMatch(/dropped by the transform/i);
    expect(statement.withheld).not.toMatch(/no location was recorded/i);
  });

  it('renders the prefix on the label', () => {
    render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={INLINE_WITH_PREFIX} />);

    expect(screen.getByTestId('load-outcome-withheld').textContent).toContain(
      's3://adi-quarantine/quarantine/run-1/',
    );
  });

  it('names the prefix for a partial load too', () => {
    const statement = describeLoadOutcomeLabel(PARTIAL_LOAD, INLINE_WITH_PREFIX)!;

    expect(statement.location).toBe('quarantine_prefix');
    expect(statement.withheld).toContain('50 rows');
    expect(statement.withheld).toContain('s3://adi-quarantine/quarantine/run-1/');
  });
});

describe('describeLoadOutcomeLabel — arm 2: no location was recorded (R1.8)', () => {
  it('names the transform when there is no quarantine evidence at all', () => {
    // No DQ report exists — the common case. Four of the five PySpark templates
    // have no `EvaluateDataQuality` block, so a transform filter can drop every
    // row with no report written anywhere.
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, null)!;

    expect(statement.location).toBe('no_location_recorded');
    expect(statement.withheld).toMatch(/dropped by the transform rather than quarantined/i);
    expect(statement.withheld).toMatch(/no location was recorded/i);
    expect(statement.withheld).toMatch(/no prefix to inspect/i);
  });

  it('uses the same wording for an inline report that quarantined nothing', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_NOTHING_QUARANTINED)!;

    expect(statement.location).toBe('no_location_recorded');
    expect(statement.withheld).toMatch(/dropped by the transform rather than quarantined/i);
  });

  it('uses the same wording for an agent report, which carries no quarantine counters', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, AGENT_REPORT)!;

    expect(statement.location).toBe('no_location_recorded');
    expect(statement.withheld).toMatch(/dropped by the transform rather than quarantined/i);
  });

  it('refuses to blame the transform when the quality check reports rows withheld but no path', () => {
    // The second of arm 2's two wordings. The report says the quality check
    // withheld rows, so saying the transform dropped them would be false.
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_WITHOUT_PREFIX)!;

    expect(statement.location).toBe('no_location_recorded');
    expect(statement.withheld).toMatch(/no quarantine location was recorded/i);
    expect(statement.withheld).toMatch(/cannot be attributed to the transform either/i);
    expect(statement.withheld).not.toMatch(/dropped by the transform rather than quarantined/i);
  });

  it('reports an absent quarantine counter as unknown rather than as zero', () => {
    // `LGR-99050e6-2` (template / ratified). An inline row written before P28
    // R11.1's row-level split carries no counter, and that script had no
    // quarantine step — so neither "quarantined" nor "the transform dropped
    // them" is supportable, and the label takes the same restrained wording it
    // takes for a missing path.
    const { rows_quarantined: _dropped, ...withoutCounter } = INLINE_WITHOUT_PREFIX;
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, withoutCounter as StateRow)!;

    expect(statement.location).toBe('no_location_recorded');
    expect(statement.withheld).toMatch(/no quarantine location was recorded/i);
    expect(statement.withheld).toMatch(/cannot be attributed to the transform either/i);
  });

  it('both arm-2 wordings state that no location was recorded', () => {
    // That is what R1.8's second half requires of every no-prefix case, however
    // the report got there.
    for (const report of [null, INLINE_NOTHING_QUARANTINED, INLINE_WITHOUT_PREFIX, AGENT_REPORT]) {
      const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, report)!;
      expect(statement.location).toBe('no_location_recorded');
      expect(statement.withheld).toMatch(/no (quarantine )?location was recorded/i);
    }
  });

  it('never leaves the location statement empty, for any report state', () => {
    // Stating that rows were withheld and stopping there is the failure this
    // half of R1.8 exists to prevent.
    for (const report of [
      INLINE_WITH_PREFIX,
      INLINE_WITHOUT_PREFIX,
      INLINE_NOTHING_QUARANTINED,
      AGENT_REPORT,
      null,
      undefined,
    ]) {
      expect(describeLoadOutcomeLabel(NO_ROWS_LOADED, report)!.withheld.trim()).not.toBe('');
    }
  });
});

describe('describeLoadOutcomeLabel — which outcomes it states, and with what severity', () => {
  it('states complete_load, which the run list omits — a detail page has no neighbouring indicator', () => {
    const statement = describeLoadOutcomeLabel(COMPLETE_LOAD, null)!;

    expect(statement.severity).toBe('info');
    expect(statement.heading).toMatch(/all rows reached the target/i);
    expect(statement.location).toBeNull();
    expect(statement.withheld).toMatch(/no rows were withheld/i);
  });

  it('names the source rather than the load for empty_source', () => {
    const statement = describeLoadOutcomeLabel(EMPTY_SOURCE, null)!;

    expect(statement.severity).toBe('info');
    expect(statement.heading).toMatch(/source was empty/i);
    expect(statement.location).toBeNull();
    expect(statement.withheld).toMatch(/none to begin with/i);
  });

  it('warns for no_rows_loaded and stays informational for a partial load', () => {
    expect(describeLoadOutcomeLabel(NO_ROWS_LOADED, null)!.severity).toBe('warning');
    expect(describeLoadOutcomeLabel(PARTIAL_LOAD, null)!.severity).toBe('info');
  });

  it('states the withheld count in the partial-load heading, at any N', () => {
    expect(describeLoadOutcomeLabel(PARTIAL_LOAD, null)!.heading).toContain('50 rows');
    expect(
      describeLoadOutcomeLabel(
        established({ outcome: 'partial_load', rows_read: 1000, rows_written: 999, rows_not_written: 1 }),
        null,
      )!.heading,
    ).toContain('1 row ');
  });

  it('never disagrees with the run list about how alarming a run is', () => {
    // The two surfaces show different SETS of outcomes on purpose; they must
    // never differ in severity for an outcome they both show.
    for (const outcome of [NO_ROWS_LOADED, PARTIAL_LOAD, EMPTY_SOURCE]) {
      const indicator = describeLoadOutcome(outcome)!;
      const label = describeLoadOutcomeLabel(outcome, null)!;
      expect(label.severity).toBe(indicator.type);
    }
  });

  it('renders nothing for not_established, and for an absent load_outcome', () => {
    expect(describeLoadOutcomeLabel(NOT_ESTABLISHED, INLINE_WITH_PREFIX)).toBeNull();
    expect(describeLoadOutcomeLabel(undefined, INLINE_WITH_PREFIX)).toBeNull();
    expect(describeLoadOutcomeLabel(null, null)).toBeNull();

    expect(
      render(<LoadOutcomeLabel outcome={NOT_ESTABLISHED} report={INLINE_WITH_PREFIX} />).container,
    ).toBeEmptyDOMElement();
    expect(render(<LoadOutcomeLabel outcome={undefined} />).container).toBeEmptyDOMElement();
  });

  it('renders nothing for an unrecognised wire value rather than inventing a reading', () => {
    const unknown = { ...NOT_ESTABLISHED, outcome: 'some_future_outcome' as LoadOutcomeKind };

    expect(describeLoadOutcomeLabel(unknown, null)).toBeNull();
    expect(render(<LoadOutcomeLabel outcome={unknown} />).container).toBeEmptyDOMElement();
  });
});

describe('describeLoadOutcomeLabel — rows_passed corroborates and never supplies a count', () => {
  it('says the quality record agrees when it does', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, INLINE_WITH_PREFIX)!;

    expect(statement.corroboration).toMatch(/independently reports 0 rows passing/i);
    expect(statement.corroboration).toMatch(/matches the count above/i);
  });

  it('says so explicitly when it disagrees, and does not pick one', () => {
    const statement = describeLoadOutcomeLabel(NO_ROWS_LOADED, {
      ...INLINE_WITH_PREFIX,
      rows_passed: 3,
    })!;

    expect(statement.corroboration).toMatch(/does not match/i);
    expect(statement.corroboration).toMatch(/corroborating detail only/i);
    // The counts line is the load record's own and is unaffected by the
    // disagreement — `rows_passed` is not a general Rows_Written source (R1.11).
    expect(statement.counts).toContain('wrote 0 rows');
  });

  it('offers no corroboration for a report the inline producer did not stamp', () => {
    expect(describeLoadOutcomeLabel(NO_ROWS_LOADED, AGENT_REPORT)!.corroboration).toBe('');
    expect(
      describeLoadOutcomeLabel(NO_ROWS_LOADED, { ...INLINE_WITH_PREFIX, source: undefined })!
        .corroboration,
    ).toBe('');
  });

  it('offers no corroboration when the inline row carries no rows_passed', () => {
    const { rows_passed: _dropped, ...withoutCount } = INLINE_WITH_PREFIX;

    expect(describeLoadOutcomeLabel(NO_ROWS_LOADED, withoutCount as StateRow)!.corroboration).toBe('');
  });

  it('renders no corroboration element when there is none to render', () => {
    render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={AGENT_REPORT} />);

    expect(screen.getByTestId('load-outcome-label')).toBeInTheDocument();
    expect(screen.queryByTestId('load-outcome-corroboration')).not.toBeInTheDocument();
  });

  it('renders the corroboration element when there is', () => {
    render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={INLINE_WITH_PREFIX} />);

    expect(screen.getByTestId('load-outcome-corroboration').textContent).toMatch(
      /independently reports/i,
    );
  });
});

describe('LoadOutcomeLabel rendering', () => {
  it('renders one alert carrying the heading, counts and location together', () => {
    render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={INLINE_WITH_PREFIX} />);

    const label = screen.getByTestId('load-outcome-label');
    expect(label.textContent).toMatch(/no rows reached the target/i);
    expect(label).toContainElement(screen.getByTestId('load-outcome-counts'));
    expect(label).toContainElement(screen.getByTestId('load-outcome-withheld'));
  });

  it('states the severity in words, not by icon colour alone', () => {
    // `LGR-99050e6-1` (template / ratified): colour alone fails WCAG 1.4.1.
    const warning = render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={null} />);
    expect(warning.container.querySelector('[aria-label="Warning"]')).not.toBeNull();
    warning.unmount();

    const info = render(<LoadOutcomeLabel outcome={COMPLETE_LOAD} report={null} />);
    expect(info.container.querySelector('[aria-label="Info"]')).not.toBeNull();
  });
});

/**
 * The p37 C5 retroactive path, where `rows_read` is a **lower bound** (R1.6).
 *
 * **Validates: Requirements 1.6, 1.8**
 *
 * A run that completed before the `(run_id, "load_metrics")` counts row existed
 * has its `rows_written` recovered from the DQ report's `rows_passed`, and its
 * `rows_read` filled with the report's **quarantine count** — a floor on
 * Rows_Read, not Rows_Read. The transform filter and the quarantine both reduce
 * the frame and neither pre-transform count was ever written, so the true figure
 * can only be higher. `LGR-9e76249-8` (**template / ratified**) is the record for
 * that provenance being its own fourth value rather than being folded into
 * `load_metrics_row_incomplete`, on `LGR-78ce0b7-8`'s ground that a missing
 * record and a record that establishes nothing must not collapse into one value.
 *
 * Two things are asserted here and each has a **contrast** case beside it, so a
 * passing test proves the wording *branches* rather than proving it is
 * boilerplate every run carries:
 *
 * 1. The counts sentence hedges — and the same outcome under `load_metrics_row`
 *    still reads byte-for-byte as it always did, with no hedge.
 * 2. The corroboration box is **absent** — and the same report under
 *    `load_metrics_row` still renders it. `rows_passed` is not an independent
 *    witness on the lower-bound path: the reader puts it into `rows_written`
 *    (`handlers/pipelines_runs.py:190`), so "independently reports 0 rows
 *    passing … which matches the count above" would be one figure off one row
 *    presented as two agreeing sources.
 *
 * `reason` is transcribed from `derive_load_outcome`'s lower-bound branch rather
 * than produced by calling it, so drift fails here (`LGR-28b4d75-5`).
 */
describe('describeLoadOutcomeLabel — a lower-bound Rows_Read is never stated as exact (R1.6)', () => {
  /** The retroactive shape: no counts row, 5 rows quarantined, 0 passed. */
  const LOWER_BOUND: LoadOutcome = established({
    outcome: 'no_rows_loaded',
    rows_read: 5,
    rows_written: 0,
    rows_not_written: 5,
    determined_from: 'dq_report_row_lower_bound',
    reason:
      'This run wrote no rows to the target. At least 5 row(s) were read — that is a lower ' +
      "bound, taken from the 5 row(s) this run's data quality report quarantined, not a " +
      'recorded count, so the true number read may be higher. Every row that was read was ' +
      'withheld.',
  });

  /** The counts sentence as it read before this branch existed. */
  const EXACT_SENTENCE = 'This run read 5 rows from the source and wrote 0 rows to the target.';

  it('states the read count as a bound, names its origin, and says it is not a recorded count', () => {
    const statement = describeLoadOutcomeLabel(LOWER_BOUND, INLINE_WITH_PREFIX)!;

    expect(statement.counts).toContain('At least 5 rows were read from the source');
    expect(statement.counts).toContain('lower bound');
    expect(statement.counts).toContain('not a recorded count');
    expect(statement.counts).toContain('the true number read may be higher');
    // The write count is a recorded fact on this path and is stated plainly.
    expect(statement.counts).toContain('This run wrote 0 rows to the target');
  });

  it('does not state the bound as an exact read count', () => {
    const statement = describeLoadOutcomeLabel(LOWER_BOUND, INLINE_WITH_PREFIX)!;

    expect(statement.counts).not.toContain('This run read 5 rows from the source');
    expect(statement.counts).not.toBe(EXACT_SENTENCE);
  });

  it('renders the hedge on the label itself', () => {
    render(<LoadOutcomeLabel outcome={LOWER_BOUND} report={INLINE_WITH_PREFIX} />);

    const counts = screen.getByTestId('load-outcome-counts').textContent!;
    expect(counts).toContain('At least 5 rows were read from the source');
    expect(counts).toContain('not a recorded count');
    expect(counts).not.toContain('This run read 5 rows from the source');
  });

  it('leaves the recorded-counts sentence exactly as it was for every other provenance', () => {
    // The contrast. Same outcome, same numbers, provenance says the counts came
    // off the load-metrics row — so there is nothing to hedge and no hedge.
    for (const provenance of [
      'load_metrics_row',
      'load_metrics_row_absent',
      'load_metrics_row_incomplete',
    ] as const) {
      const statement = describeLoadOutcomeLabel(
        { ...LOWER_BOUND, determined_from: provenance },
        INLINE_WITH_PREFIX,
      )!;

      expect(statement.counts).toBe(EXACT_SENTENCE);
      expect(statement.counts).not.toMatch(/at least/i);
      expect(statement.counts).not.toMatch(/lower bound/i);
    }
  });

  it('agrees with the source in number at N = 1', () => {
    const statement = describeLoadOutcomeLabel(
      { ...LOWER_BOUND, rows_read: 1, rows_not_written: 1 },
      INLINE_WITH_PREFIX,
    )!;

    expect(statement.counts).toContain('At least 1 row was read from the source');
    expect(statement.counts).not.toContain('1 rows were read');
    expect(statement.counts).toContain('the 1 row this run’s data quality record quarantined');
  });

  it('falls back to the recorded-counts wording when there is no figure to bound', () => {
    // Nothing to state a floor about, and `formatRows` already says the count
    // was not recorded — so the hedge would be words about nothing.
    const statement = describeLoadOutcomeLabel(
      { ...LOWER_BOUND, rows_read: null },
      INLINE_WITH_PREFIX,
    )!;

    expect(statement.counts).toContain('an unrecorded number of rows');
    expect(statement.counts).not.toMatch(/at least/i);
  });

  it('claims no independent corroboration from the row its own count came off', () => {
    expect(describeLoadOutcomeLabel(LOWER_BOUND, INLINE_WITH_PREFIX)!.corroboration).toBe('');
  });

  it('renders no corroboration box on the lower-bound path, and still renders one without it', () => {
    // Both halves in one test on purpose: the second is what proves the
    // suppression is a branch and not the box quietly disappearing for everyone.
    const lowerBound = render(<LoadOutcomeLabel outcome={LOWER_BOUND} report={INLINE_WITH_PREFIX} />);
    expect(lowerBound.getByTestId('load-outcome-label')).toBeInTheDocument();
    expect(lowerBound.queryByTestId('load-outcome-corroboration')).toBeNull();
    lowerBound.unmount();

    // `INLINE_WITH_PREFIX` carries `rows_passed: 0`, matching this run's
    // `rows_written: 0` — the same figures, but read off a load-metrics row that
    // the DQ report did not supply, so the agreement is worth stating.
    const recorded = render(<LoadOutcomeLabel outcome={NO_ROWS_LOADED} report={INLINE_WITH_PREFIX} />);
    expect(recorded.getByTestId('load-outcome-corroboration').textContent).toMatch(
      /independently reports 0 rows passing/i,
    );
  });

  it('renders nothing for not_established under the lower-bound provenance', () => {
    // The reader reaches this whenever the DQ row does not settle an outcome.
    // Absence, not error — exactly as for every other provenance.
    const unsettled: LoadOutcome = {
      ...NOT_ESTABLISHED,
      determined_from: 'dq_report_row_lower_bound',
      rows_written: 0,
      missing: ['rows_read'],
      reason:
        'This run has no recorded row counts. Its data quality report shows 0 row(s) passing ' +
        'the quality check and 0 quarantined, which puts a lower bound on how many rows were ' +
        'read but does not establish it, so what reached the target cannot be stated.',
    };

    expect(describeLoadOutcomeLabel(unsettled, INLINE_WITH_PREFIX)).toBeNull();
    expect(
      render(<LoadOutcomeLabel outcome={unsettled} report={INLINE_WITH_PREFIX} />).container,
    ).toBeEmptyDOMElement();
  });
});
