// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * MetricsDashboardPage — Not_Established never reads as a measured value
 * (p39 task 17.2).
 *
 * **R4.11** is the whole of it: the dashboard renders Not_Established
 * distinguishably from a measured zero. So for every three-state tile field this
 * file renders the SAME payload twice — once with the figure absent, once with
 * the figure measured at `0` — and asserts the two do not read alike and that
 * neither shows the other's text. `R4.4` and `R3.6` carry the same rule into the
 * trend indicators: an absence is not a measured `stable`, and it says so in
 * text rather than in a glyph and a colour alone.
 *
 * Expectations here are derived from those acceptance criteria, not from what
 * the page currently prints (`LGR-28b4d75-5`, template / ratified: output that
 * is its own oracle cannot detect the producer drifting). Concretely: an absent
 * figure must carry **no digit**, because a digit is what makes an absence
 * readable as a value; a measured zero must carry one. The exact unit strings
 * ("0ms", "$0", "0.0 KB") are formatting, are in no requirement, and are not
 * pinned.
 *
 * **Nine routed sites, and three of them were the dangerous ones.**
 * `overall_success_rate`, `agentcore.availability` and `s3.requests_per_minute`
 * reach the DOM through template-literal / `String()` interpolation, which
 * accepts a `null` without a type error and prints it — they would have shipped
 * rendering `null%` / `null`. Hence the explicit "no tile renders the literal
 * text null" assertion, which a type-checker cannot make.
 *
 * **No tile label is asserted anywhere in this file, deliberately.** Task 18.1
 * leaves label wording unpinned and pins `performance.measurement` instead,
 * because the measurement is the thing that stops a label and its source
 * drifting apart. The figure readers below discard the label for that reason.
 *
 * Additive: `MetricsDashboardPage.trend-polarity.test.tsx` and
 * `MetricsDashboardPage.property.test.tsx` are untouched and keep their own
 * `vi.mock('../../hooks', …)`.
 *
 * **Validates: Requirements 4.11, 4.4, 3.6, 7.5, 7.6**
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ComprehensiveMetrics, MonthlyCostBasis, TrendIndicators } from '../../types';

/**
 * The token 17.1 chose for an absent figure — a word, so it cannot be read as a
 * number, and shared by every site so one decision covers them all
 * (constraint 18, `LGR-4854b3d-1`). Named once here; the assertions below test
 * its *properties* (letters, no digits, distinct from a measured zero) as well
 * as the literal, so a re-wording fails one assertion rather than twenty.
 */
const NOT_ESTABLISHED = 'Not established';

/**
 * R7.6 — the measurement behind each performance tile, keyed by the payload's
 * own field name. Digit-free on purpose: the description assertions below look
 * for the cost basis's run count and rate as digits, and a digit in a
 * measurement sentence would let those pass vacuously.
 *
 * No existing fixture on this page carries `measurement` at all, so without
 * this map every description assertion would assert about an element that never
 * renders.
 */
const MEASUREMENT: Record<string, string> = {
  lines_ingested_24h: 'sum of file-size derived record estimates',
  processing_rate: 'records estimate over the window wall clock',
  overall_success_rate: 'succeeded over all terminal executions',
  failed_pipelines: 'ExecutionsFailed sum',
  agent_latency_ms: 'AgentCore stage duration average',
  monthly_cost: 'Glue DPU-minutes estimate',
};

/** R4.2 — `<source>_absent` provenance for each field that can come out absent. */
const DETERMINED_FROM: Record<string, string> = {
  lines_ingested_24h: 'file_validation_absent',
  processing_rate: 'file_validation_absent',
  overall_success_rate: 'state_machine_metrics_absent',
  agent_latency_ms: 'stage_duration_absent',
  monthly_cost: 'glue_cost_metric_absent',
  agentcore_availability: 'agent_error_rate_incomplete',
  storage_used_bytes: 'bucket_size_bytes_absent',
  requests_per_minute: 'request_metrics_not_enabled',
};

/** R2.6 / R7.5 — what a measured cost figure was summed from. */
const COST_BASIS: MonthlyCostBasis = {
  metric: 'Glue.RunCost',
  window_start: 'window start',
  window_end: 'window end',
  runs: 7,
  rate_usd_per_dpu_hour: 0.44,
  dpu_minimum_minutes: 10,
  note: 'estimate from Glue DPU-minutes, never measured spend',
};

const STABLE_TRENDS: TrendIndicators = {
  ingestion: 'stable',
  success: 'stable',
  agent_latency: 'stable',
  cost: 'stable',
};

/**
 * One payload shape, one knob: `figure` is applied to every three-state field at
 * once, so the absent render and the measured-zero render differ in nothing
 * else. `failed_pipelines` is deliberately NOT on the knob — it is a plain
 * `number` and an `ExecutionsFailed` sum with no datapoints is a genuine zero.
 */
function metricsWith(figure: number | null, trends: TrendIndicators = STABLE_TRENDS): ComprehensiveMetrics {
  const inertStage = { success_count: 0, error_count: 0, avg_time_ms: null };
  return {
    performance: {
      lines_ingested_24h: figure,
      processing_rate: figure,
      overall_success_rate: figure,
      failed_pipelines: 0,
      agent_latency_ms: figure,
      monthly_cost: figure,
      monthly_cost_basis: figure === null ? null : COST_BASIS,
      terminal_counts: null,
      measurement: MEASUREMENT,
      determined_from: DETERMINED_FROM,
      missing: [],
      trends,
    },
    stages: {
      schema_extract: inertStage,
      schema_mapping: inertStage,
      code_generation: inertStage,
      infrastructure: inertStage,
      quality_checks: inertStage,
      pipeline_run: inertStage,
    },
    infrastructure: {
      agentcore: { availability: figure, response_time_ms: figure, status: 'unknown' },
      dynamodb: { read_capacity_per_hour: 0, write_capacity_per_hour: 0, status: 'healthy' },
      s3: { storage_used_bytes: figure, requests_per_minute: figure, status: 'unknown' },
      lambda: { invocations_per_minute: 0, error_rate: 0, status: 'healthy' },
    },
  };
}

let payload: ComprehensiveMetrics = metricsWith(null);

vi.mock('../../hooks', () => ({
  useMetrics: () => ({ data: payload, isLoading: false, isError: false, refetch: vi.fn() }),
  useTokenUsageMetrics: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useCostRecommendations: () => ({ data: undefined, isLoading: false, isError: false, refetch: vi.fn() }),
  useApplyRecommendation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import MetricsDashboardPage from '../MetricsDashboardPage';

function renderWith(figure: number | null, trends: TrendIndicators = STABLE_TRENDS) {
  payload = metricsWith(figure, trends);
  return render(
    <MemoryRouter>
      <MetricsDashboardPage />
    </MemoryRouter>,
  );
}

// --- Reading a figure without reading its label ------------------------------
//
// Two tile shapes on this page. `MetricBox` puts the label and the figure in
// sibling elements; an `InfraCard` detail renders "<label>: <figure>" in one.
// Both readers below drop the label, so nothing here asserts label wording
// (task 18.1) — and both drop the description, which is asserted separately.

/** The figure a `MetricBox` shows: the element after the label. */
function boxFigure(testId: string): string {
  const kids = Array.from(screen.getByTestId(testId).children);
  expect(kids.length, `unexpected MetricBox shape for ${testId}`).toBeGreaterThanOrEqual(2);
  return (kids[1].textContent ?? '').trim();
}

/**
 * A `MetricBox`'s description text. The description sits behind the tile's
 * question-mark button and is only in the DOM while that popover is open, so
 * this opens it first.
 */
function openedDescription(testId: string): string {
  fireEvent.click(within(screen.getByTestId(testId)).getByRole('button', { name: /^About / }));
  return screen.getByTestId(`${testId}-description`).textContent ?? '';
}

/** The figure an `InfraCard` detail shows: everything after "<label>:". */
function cardFigure(testId: string): string {
  const own = screen.getByTestId(testId).textContent ?? '';
  const sep = own.indexOf(':');
  return (sep >= 0 ? own.slice(sep + 1) : own).trim();
}

/**
 * Every display site that routes a three-state figure through
 * `formatOrNotEstablished`. `field` is the payload's own name for the fact,
 * which is how `measurement` and `determined_from` are keyed — note
 * `agentcore-response-time` reads `agent_latency_ms`: one fact published under
 * two names.
 */
const THREE_STATE_SITES: { testId: string; field: string; read: (id: string) => string }[] = [
  { testId: 'metric-lines-ingested', field: 'lines_ingested_24h', read: boxFigure },
  { testId: 'metric-processing-rate', field: 'processing_rate', read: boxFigure },
  // Interpolated as `${n}%` — a null prints, and no type error is raised.
  { testId: 'metric-success-rate', field: 'overall_success_rate', read: boxFigure },
  { testId: 'metric-agent-latency', field: 'agent_latency_ms', read: boxFigure },
  { testId: 'metric-monthly-cost', field: 'monthly_cost', read: boxFigure },
  // Interpolated as `${n}%`.
  { testId: 'agentcore-availability', field: 'agentcore_availability', read: cardFigure },
  { testId: 'agentcore-response-time', field: 'agent_latency_ms', read: cardFigure },
  { testId: 's3-storage-used', field: 'storage_used_bytes', read: cardFigure },
  // Interpolated via `String(n)`.
  { testId: 's3-requests-per-min', field: 'requests_per_minute', read: cardFigure },
];

describe('MetricsDashboardPage — an absence and a measured zero read differently (R4.11)', () => {
  it.each(THREE_STATE_SITES)('$testId renders an absent figure as a word carrying no digit', ({ testId, read }) => {
    renderWith(null);
    const figure = read(testId);
    // A digit is what would let an absence be read as a value. R4.11 forbids it.
    expect(figure, `${testId} renders a digit for an absent figure`).not.toMatch(/\d/);
    expect(figure).toMatch(/[A-Za-z]/);
    expect(figure).toBe(NOT_ESTABLISHED);
  });

  it.each(THREE_STATE_SITES)('$testId renders a measured zero as a number', ({ testId, read }) => {
    renderWith(0);
    const figure = read(testId);
    expect(figure, `${testId} shows no numeral for a measured zero`).toMatch(/0/);
    expect(figure).not.toContain(NOT_ESTABLISHED);
  });

  it.each(THREE_STATE_SITES)('$testId: the two renders differ and neither shows the other text', ({ testId, read }) => {
    const absent = renderWith(null);
    const absentFigure = read(testId);
    absent.unmount();

    renderWith(0);
    const zeroFigure = read(testId);

    expect(absentFigure).not.toBe(zeroFigure);
    expect(zeroFigure, `${testId} shows the absence token for a measured zero`).not.toContain(absentFigure);
    expect(absentFigure, `${testId} shows the measured-zero text for an absence`).not.toContain(zeroFigure);
  });

  it('renders one and the same absence token at all nine sites', () => {
    // Constraint 18 / `LGR-4854b3d-1`: the three-state decision lives in exactly
    // one place, so every site must show the same token. Nine different words
    // would mean nine decisions.
    renderWith(null);
    const tokens = new Set(THREE_STATE_SITES.map((s) => s.read(s.testId)));
    expect(tokens.size, `sites disagree on the absence token: ${[...tokens].join(' | ')}`).toBe(1);
  });

  it('never renders the literal text null, at any site or anywhere on the page', () => {
    // The three interpolated sites print a null without a type error. This is
    // the assertion a type-checker cannot make.
    renderWith(null);
    for (const { testId } of THREE_STATE_SITES) {
      const whole = screen.getByTestId(testId).textContent ?? '';
      expect(whole, `${testId} renders the literal text null`).not.toContain('null');
    }
    expect(screen.getByTestId('metrics-dashboard').textContent ?? '').not.toContain('null');
  });

  it('renders a zero failed-execution count as zero, never as an absence', () => {
    // The control. `failed_pipelines` is not three-state: an `ExecutionsFailed`
    // sum with no datapoints is a genuine zero — nothing failed. It must read as
    // a measurement even in a payload where every other figure is absent.
    renderWith(null);
    const figure = boxFigure('metric-failed-pipelines');
    expect(figure).toMatch(/0/);
    expect(figure).not.toContain(NOT_ESTABLISHED);
  });
});

// --- Trend indicators -------------------------------------------------------

/**
 * The Cloudscape colour token on a trend arrow. Same extraction as
 * `trend-polarity.test.tsx:89-94` — `Box color` lands in the DOM as an
 * `awsui_color-<token>_<hash>` class — widened only to admit the fourth token
 * that 17.1's neutral arm introduced.
 */
function arrowColorToken(testId: string): string {
  const cls = screen.getByTestId(`${testId}-arrow`).className;
  const match = /awsui_color-(text-status-(?:success|error|info|inactive))_/.exec(cls);
  expect(match, `no colour token class on ${testId}-arrow (class="${cls}")`).not.toBeNull();
  return match![1];
}

const TREND_TESTIDS = ['trend-ingestion', 'trend-success', 'trend-agent-latency', 'trend-cost'];

const ABSENT_TRENDS: TrendIndicators = {
  ingestion: 'not_established',
  success: 'not_established',
  agent_latency: 'not_established',
  cost: 'not_established',
};

describe('MetricsDashboardPage — a trend absence is not a measured stable (R4.4)', () => {
  it('gives an absent trend a different colour token from stable, under either polarity', () => {
    // R4.4: no baseline in the previous window reports `not_established` rather
    // than `stable`. That distinction is only worth reporting if it survives to
    // the screen — this is the assertion that stops a later refactor mapping the
    // absence onto stable's colour.
    const absent = renderWith(0, ABSENT_TRENDS);
    const absentTokens = TREND_TESTIDS.map(arrowColorToken);
    absent.unmount();

    renderWith(0, STABLE_TRENDS);
    const stableTokens = TREND_TESTIDS.map(arrowColorToken);

    TREND_TESTIDS.forEach((testId, i) => {
      expect(
        absentTokens[i],
        `${testId} paints an absent trend with stable's colour`,
      ).not.toBe(stableTokens[i]);
    });
    // The tokens 17.1 settled on, pinned so the pair cannot drift back together.
    expect(new Set(absentTokens)).toEqual(new Set(['text-status-inactive']));
    expect(new Set(stableTokens)).toEqual(new Set(['text-status-info']));
  });

  it('gives an absent trend a different arrow glyph from stable', () => {
    const absent = renderWith(0, ABSENT_TRENDS);
    const absentArrows = TREND_TESTIDS.map((id) => screen.getByTestId(`${id}-arrow`).textContent?.trim() ?? '');
    absent.unmount();

    renderWith(0, STABLE_TRENDS);
    const stableArrows = TREND_TESTIDS.map((id) => screen.getByTestId(`${id}-arrow`).textContent?.trim() ?? '');

    TREND_TESTIDS.forEach((testId, i) => {
      expect(absentArrows[i], `${testId} draws an absent trend as stable's arrow`).not.toBe(stableArrows[i]);
    });
    expect(new Set(absentArrows)).toEqual(new Set(['?']));
    expect(new Set(stableArrows)).toEqual(new Set(['→']));
  });

  it('states an absent trend in text, so the glyph and the colour are not the only carriers (R3.6)', () => {
    const absent = renderWith(0, ABSENT_TRENDS);
    const absentLabels = TREND_TESTIDS.map((id) => screen.getByTestId(id).textContent ?? '');
    for (const text of absentLabels) {
      expect(text).toContain(NOT_ESTABLISHED);
    }
    absent.unmount();

    renderWith(0, STABLE_TRENDS);
    for (const testId of TREND_TESTIDS) {
      const text = screen.getByTestId(testId).textContent ?? '';
      expect(text, `${testId} words a measured stable as an absence`).not.toContain(NOT_ESTABLISHED);
    }
  });
});

// --- The one description slot ------------------------------------------------

describe('MetricsDashboardPage — a tile states its measurement and, when absent, why (R7.6 / R4.11)', () => {
  it('states both the measurement and the reason in one slot when the figure is absent', () => {
    renderWith(null);
    const description = openedDescription('metric-lines-ingested');
    expect(description).toContain(MEASUREMENT['lines_ingested_24h']);
    expect(description).toContain(NOT_ESTABLISHED);
    expect(description).toContain(DETERMINED_FROM['lines_ingested_24h']);
  });

  it('states the measurement with no absence clause when the figure is measured', () => {
    renderWith(0);
    const description = openedDescription('metric-processing-rate');
    expect(description).toContain(MEASUREMENT['processing_rate']);
    expect(description, 'a measured tile reports a reason for an absence it does not have').not.toContain(
      NOT_ESTABLISHED,
    );
  });

  it('surfaces the cost basis — the run count and the rate summed — on the cost tile (R7.5 / R2.6)', () => {
    renderWith(0);
    const description = openedDescription('metric-monthly-cost');
    expect(description).toContain(MEASUREMENT['monthly_cost']);
    // R2.6: the basis names what was summed. The run count and the rate are what
    // make the figure checkable, so both must reach the screen.
    expect(description).toContain(String(COST_BASIS.runs));
    expect(description).toContain(String(COST_BASIS.rate_usd_per_dpu_hour));
  });

  it('states no run count or rate on an absent cost figure', () => {
    // There is no basis for a sum that was never taken. A basis printed beside
    // Not_Established would assert a window nobody summed.
    renderWith(null);
    const description = openedDescription('metric-monthly-cost');
    expect(description).toContain(NOT_ESTABLISHED);
    expect(description).not.toContain(String(COST_BASIS.rate_usd_per_dpu_hour));
  });
});
