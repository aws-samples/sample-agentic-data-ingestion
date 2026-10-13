// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Modal from '@cloudscape-design/components/modal';
import Link from '@cloudscape-design/components/link';
import Button from '@cloudscape-design/components/button';
import Badge from '@cloudscape-design/components/badge';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import { useMetrics, useTokenUsageMetrics, useCostRecommendations, useApplyRecommendation } from '../hooks';
import type { TokenUsageDataPoint, ModelComparisonRow, CostRecommendation } from '../hooks';
import Table from '@cloudscape-design/components/table';
import Select from '@cloudscape-design/components/select';
import { colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import { formatStepName } from '../utils/formatting';
import { humanizeDuration } from '../components/common/formatters';
import ClickableArea from '../components/common/ClickableArea';
import DetailPopover from '../components/common/DetailPopover';
import type {
  StageMetrics,
  StageFailureAttribution,
  HealthStatus,
  TrendIndicators,
  MonthlyCostBasis,
} from '../types';

// ---------------------------------------------------------------------------
// Not_Established — the one place an absent measurement is decided
// ---------------------------------------------------------------------------

// p39 R4.11 / constraint 18, `LGR-4854b3d-1` (template / ratified): a
// three-state figure is never collapsed to its zero, and the collapsing
// expression lives in exactly ONE place. On this page that place is
// `formatOrNotEstablished` below — the only null test on a display path here.
// Every tile passes the RAW value plus a formatter for the measured case, so no
// call site can quietly decide that an absence is a zero.

/**
 * The token a figure renders as when no measurement was obtained.
 *
 * A word, on purpose. `0` asserts a measurement nobody took. An em dash was
 * rejected too: `humanizeDuration` and `formatUsd` already emit `—` for their
 * own unknowns, so reusing it would file an established absence under the same
 * glyph as a formatter's fallback and leave the two unreadable apart.
 */
const NOT_ESTABLISHED = 'Not established';

/**
 * Render a three-state figure: the measured case through `format`, the absent
 * case as {@link NOT_ESTABLISHED}.
 *
 * `value == null` here is the page's whole three-state decision. Do NOT add
 * `?? 0`, a `!` assertion or a cast at a call site to get a `number` — the union
 * is the true wire shape (`types.ts` `PerformanceMetrics`), and a second
 * collapsing expression is the defect constraint 18 exists to prevent.
 */
function formatOrNotEstablished(
  value: number | null | undefined,
  format: (measured: number) => string,
): string {
  return value == null ? NOT_ESTABLISHED : format(value);
}

/**
 * The `determined_from` provenance for a figure that came out Not_Established,
 * for the tile's description (R4.11) — so a tile says not just that a number is
 * absent but what was looked at.
 *
 * Keyed off the ALREADY-RENDERED token rather than a second null test of its
 * own, which is what keeps `formatOrNotEstablished` the single place the
 * three-state decision is made. `undefined` when the figure was measured, and
 * when the payload carries no reason for it — there is nothing honest to say.
 */
function notEstablishedReason(
  rendered: string,
  field: string,
  determinedFrom: Record<string, string> | undefined,
): string | undefined {
  if (rendered !== NOT_ESTABLISHED) return undefined;
  const reason = determinedFrom?.[field];
  return reason ? `${NOT_ESTABLISHED} — ${reason}` : undefined;
}

/**
 * A tile has ONE description slot and two things it may need to say: what its
 * figure measures (R7.6, always) and why there is no figure (R4.11, only when
 * there is none). This composes them into that one slot rather than growing a
 * second description mechanism beside it.
 *
 * The three-state decision is NOT re-made here: every statement handed in has
 * already been decided elsewhere — the measurement by the payload, the absence
 * by `notEstablishedReason`, which itself keys off the token
 * `formatOrNotEstablished` already rendered. This function only drops the
 * statements that are not there, so `formatOrNotEstablished` stays the single
 * place a null is tested (constraint 18, `LGR-4854b3d-1`).
 *
 * Plain sentences joined by a full stop, on purpose: the description is the
 * text equivalent of a figure that reads {@link NOT_ESTABLISHED}, so it cannot
 * lean on a glyph or a colour to separate its parts (`LGR-99050e6-1`'s ground —
 * colour alone fails WCAG 1.4.1 — applied one layer down).
 */
function tileDescription(...statements: (string | undefined)[]): string | undefined {
  const stated = statements.filter((s): s is string => s !== undefined && s !== '');
  return stated.length > 0 ? stated.join('. ') : undefined;
}

/**
 * R7.5 / R2.6 — the cost figure's `basis` in one line: how many Glue runs were
 * summed, and at what rate. Absent when the basis is (there is no basis for a
 * sum that was never taken — `types.ts` `MonthlyCostBasis`), and absent for a
 * payload that carries no basis at all.
 *
 * Deliberately does NOT restate `basis.note`: the handler derives that sentence
 * and `measurement.monthly_cost` from one fact registry, so the tile already
 * says "a Glue DPU-minutes estimate … never measured spend" once. Printing it
 * twice would make one source look like two. What is added here is the part the
 * measurement cannot carry — the run count and rate this window's figure was
 * actually summed from, which is what makes the number checkable.
 */
function costBasisStatement(basis: MonthlyCostBasis | null | undefined): string | undefined {
  if (!basis) return undefined;
  return `${basis.runs.toLocaleString()} Glue runs at $${basis.rate_usd_per_dpu_hour}/DPU-hour`;
}

// ---------------------------------------------------------------------------
// Trend arrow + semantic polarity helpers
// ---------------------------------------------------------------------------

// P30 R13.1 — Semantic_Polarity.
// A direction on its own is not a signal: ingestion trending up is good, cost
// trending up is not. Polarity is a property of the *metric*, so it is keyed on
// the `TrendIndicators` field name rather than on the tile's display label — a
// label is copy and can be reworded without changing what the number means.
//
// Constraint C6 (`LGR-620555b-1`, backfilled): that commit grounded every tile
// in live data and added real previous-period trends. This changes only how a
// trend is *encoded*; the trend values themselves are untouched.

type TrendMetric = keyof TrendIndicators;
type TrendPolarity = 'higher-is-better' | 'lower-is-better';

/**
 * Every value a trend can take, read off the payload type rather than restated
 * here — p39 widened it with `not_established` and a second copy of the union
 * would have gone stale.
 */
type TrendDirection = TrendIndicators[TrendMetric];

const TREND_POLARITY: Record<TrendMetric, TrendPolarity> = {
  ingestion: 'higher-is-better',
  success: 'higher-is-better',
  agent_latency: 'lower-is-better',
  cost: 'lower-is-better',
};

const POLARITY_CAPTION: Record<TrendPolarity, string> = {
  'higher-is-better': 'Higher is better',
  'lower-is-better': 'Lower is better',
};

// p39 R4.11 — the fourth trend value. `_reported_trend` has emitted
// `not_established` since the backend gained its absent cases; the two functions
// below carry it as a NEW arm and nothing else. `TREND_POLARITY` and
// `POLARITY_CAPTION` above are untouched, and what `up`, `down` and `stable` map
// to is unchanged for every metric — `LGR-99050e6-1` (template / ratified)
// governs that MAPPING, and extending the DOMAIN is a different act.
//
// The neutral arm is deliberately NOT `stable`'s arrow or colour. `stable` is a
// measurement — two windows were compared and came out level; `not_established`
// is an absence — one of them had nothing to compare. Rendering them alike is
// the `?? 0` collapse one layer up (`LGR-4854b3d-1`, constraint 18).

function trendArrow(direction: TrendDirection): string {
  if (direction === 'not_established') return '?';
  if (direction === 'up') return '↑';
  if (direction === 'down') return '↓';
  return '→';
}

/** The trend's direction in words, so the glyph is never the only carrier. */
function trendLabel(direction: TrendDirection): string {
  if (direction === 'not_established') return NOT_ESTABLISHED;
  return direction.charAt(0).toUpperCase() + direction.slice(1);
}

type TrendColorToken =
  | 'text-status-success'
  | 'text-status-error'
  | 'text-status-info'
  | 'text-status-inactive';

/**
 * Colour a trend by the *signal* it carries rather than by its direction, so an
 * upward Cost or Agent Latency reads negative. `stable` is neutral under either
 * polarity; `not_established` carries no signal at all, and gets its own token
 * rather than borrowing `stable`'s neutral one.
 */
function trendColor(direction: TrendDirection, polarity: TrendPolarity): TrendColorToken {
  if (direction === 'not_established') return 'text-status-inactive';
  if (direction === 'stable') return 'text-status-info';
  const isPositiveSignal = polarity === 'higher-is-better' ? direction === 'up' : direction === 'down';
  return isPositiveSignal ? 'text-status-success' : 'text-status-error';
}

// ---------------------------------------------------------------------------
// Health status helper
// ---------------------------------------------------------------------------

function healthIndicatorType(status: HealthStatus): 'success' | 'warning' | 'error' | 'info' {
  switch (status) {
    case 'healthy': return 'success';
    case 'warning': return 'warning';
    case 'critical': return 'error';
    default: return 'info';
  }
}

function formatBytes(bytes: number): string {
  if (bytes >= 1_073_741_824) return `${(bytes / 1_073_741_824).toFixed(1)} GB`;
  if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(1)} MB`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

// P29 R4.2 — duration formatting decision:
// Elapsed-time values (agent latency, stage avg time, model latency) are
// humanized via the shared `humanizeDuration` for values >= 1s so long
// durations read as "45s" / "2m" / "2h 15m" instead of raw ms or unwieldy
// "128.0s". Sub-second values keep millisecond fidelity ("120ms") because
// humanizeDuration would floor them to "0s" (the design's explicit carve-out
// for keeping sub-second latency fidelity).

/**
 * Format a latency/duration in ms that was actually measured.
 *
 * p39 R4.11: this answered `'N/A'` for `!ms || ms <= 0`, so a measured zero and
 * an absent measurement rendered as the same string — the three-state collapse
 * `LGR-4854b3d-1` fences, wearing a truthy check rather than a `?? 0`. The
 * absent case is now decided once, by `formatOrNotEstablished`, and this
 * function only ever formats a number it was given: `0` reads "0ms", which is a
 * measurement, and absence reads {@link NOT_ESTABLISHED}, which is not.
 */
function formatLatency(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return humanizeDuration(ms);
}

/** Format a stage avg time; null => "N/A" (the stage records no duration). */
function formatAvgTime(ms: number | null): string {
  if (ms == null) return 'N/A';
  if (ms < 1000) return `${ms}ms`;
  return humanizeDuration(ms);
}

// ---------------------------------------------------------------------------
// p59 — reconciling the panel with the Failed Executions tile above it
// ---------------------------------------------------------------------------

/**
 * The sentences that make the Stage Metrics panel add up against
 * `performance.failed_pipelines`, or state exactly what is unaccounted for.
 *
 * The page used to report 26 Failed Executions directly above a panel showing
 * ~1 error across all six stages and leave the reader to notice. The backend now
 * attributes the failures no stage row recorded, and everything it could NOT
 * attribute arrives here to be said out loud rather than dropped.
 *
 * Words, not a glyph or a colour. Every statement is a full sentence carrying its
 * own number, so the reconciliation survives without the Alert's icon or hue
 * (`LGR-99050e6-1`'s ground — colour alone fails WCAG 1.4.1 — applied here).
 *
 * The `null` arm is the three-state discipline reaching the page
 * (`LGR-4854b3d-1`, template / ratified): a failure-record read that could not
 * be made is NOT "no failures". Silence there would put the panel back to
 * implying the counts are complete, which is the defect one level up.
 */
function stageReconciliationStatements(
  attribution: StageFailureAttribution | null | undefined,
  determinedFrom: Record<string, string> | undefined,
): string[] {
  if (attribution == null) {
    const reason = determinedFrom?.['stage_failure_attribution'];
    const why = reason ? ` (${reason})` : '';
    return [
      `Failures that no stage recorded could not be read${why}, so the error counts below are a floor rather than a total and cannot be checked against Failed Executions.`,
    ];
  }

  const statements: string[] = [];

  if (attribution.counted_into_stage_errors > 0) {
    const n = attribution.counted_into_stage_errors;
    statements.push(
      `${n} of the errors below ${n === 1 ? 'comes' : 'come'} from a failure record rather than from the stage's own row — the run died where the stage never got to write one.`,
    );
  }

  if (attribution.unattributed > 0) {
    const n = attribution.unattributed;
    const breakdown = Object.entries(attribution.unattributed_by_step)
      .map(([step, count]) => `${formatStepName(step)} (${count})`)
      .join(', ');
    statements.push(
      `${n} failure${n === 1 ? '' : 's'} happened at a step with no stage on this panel, so ${n === 1 ? 'it is' : 'they are'} in none of the counts below: ${breakdown}.`,
    );
  }

  // 2026-10-10 — rejections are decisions, not failures, and are in none of the
  // counts above or below. Said here so the panel still accounts for every run
  // the gates stopped.
  const rejected = attribution.rejected_at_approval;
  if (rejected > 0) {
    const byGate = Object.entries(attribution.rejected_by_step)
      .map(([step, count]) => `${formatStepName(step)} (${count})`)
      .join(', ');
    statements.push(
      `${rejected} run${rejected === 1 ? ' was' : 's were'} rejected by a reviewer at approval, so ${rejected === 1 ? 'it is' : 'they are'} counted under Rejected at Approval, not as ${rejected === 1 ? 'a failure' : 'failures'}: ${byGate}.`,
    );
  }

  const residual = attribution.failed_executions_without_a_record;
  if (residual != null && residual > 0) {
    statements.push(
      `${residual} failed execution${residual === 1 ? '' : 's'} left no failure record at all, so nothing on this panel accounts for ${residual === 1 ? 'it' : 'them'}.`,
    );
  } else if (residual != null && residual < 0) {
    const excess = -residual;
    statements.push(
      `${excess} more failure record${excess === 1 ? '' : 's'} than failed executions. The two are read from different systems over the same window, so a run on the boundary can be counted by one and not the other.`,
    );
  }

  if (attribution.failed_executions == null) {
    statements.push(
      'Failed-execution totals were not read this period, so these counts cannot be checked against the Failed Executions tile.',
    );
  }

  return statements;
}

// ---------------------------------------------------------------------------
// Expected stage keys
// ---------------------------------------------------------------------------

const STAGE_KEYS = [
  'schema_extract',
  'schema_mapping',
  'code_generation',
  'infrastructure',
  'quality_checks',
  'pipeline_run',
] as const;

// Shared period labels. Note the two selectors use different backend keys for
// "24 hours": the comprehensive-metrics endpoint expects "24h", the
// token-usage endpoint expects "1d". Both map to the same label here.
const PERIOD_LABELS: Record<string, string> = {
  '1d': 'Last 24 hours',
  '24h': 'Last 24 hours',
  '7d': 'Last 7 days',
  '30d': 'Last 30 days',
  '90d': 'Last 90 days',
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MetricsDashboardPage() {
  const [perfPeriod, setPerfPeriod] = useState('24h');
  const { data: metrics, isLoading, isError, refetch: refetchMetrics } = useMetrics(perfPeriod);
  const [selectedStage, setSelectedStage] = useState<{ name: string; metrics: StageMetrics } | null>(null);
  const [tokenPeriod, setTokenPeriod] = useState('7d');
  const { data: tokenMetrics, isError: tokenError, refetch: refetchTokenMetrics } = useTokenUsageMetrics(tokenPeriod);
  const { data: costData, isError: costError, refetch: refetchCost } = useCostRecommendations();
  const applyRecommendation = useApplyRecommendation();
  const [costTypeFilter, setCostTypeFilter] = useState('all');
  const [costRiskFilter, setCostRiskFilter] = useState('all');

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary">Loading metrics...</Box>
      </Box>
    );
  }

  // P29 R4.1: an honest error state with a retry affordance — never fabricated
  // numbers (MOCK_METRICS and the fake $1,850/mo figure were deleted from the hook).
  if (isError || !metrics) {
    return (
      <SpaceBetween size="l">
        <Header variant="h1">Metrics Dashboard</Header>
        <Alert
          type="error"
          header="Failed to load metrics"
          data-testid="metrics-error"
          action={
            <Button data-testid="metrics-retry" onClick={() => refetchMetrics()}>
              Retry
            </Button>
          }
        >
          Metrics could not be loaded. Try again in a moment.
        </Alert>
      </SpaceBetween>
    );
  }

  const CLOUDWATCH_DASHBOARD_NAME = `AgenticDataIngestion-Pipeline-Monitoring-${import.meta.env.VITE_ENVIRONMENT ?? 'dev'}`;
  const CLOUDWATCH_DASHBOARD_REGION = import.meta.env.VITE_AWS_REGION ?? 'us-west-2';
  const cloudwatchDashboardUrl = `https://${CLOUDWATCH_DASHBOARD_REGION}.console.aws.amazon.com/cloudwatch/home?region=${CLOUDWATCH_DASHBOARD_REGION}#dashboards/dashboard/${CLOUDWATCH_DASHBOARD_NAME}`;

  // Cost recommendations: derive filter dropdown options from the data and
  // apply the active type/risk filters. The backend already sorts by savings.
  const allCostRecs = costData?.recommendations ?? [];
  const costTypeOptions = ['all', ...Array.from(new Set(allCostRecs.map((r) => r.type)))];
  const costRiskOptions = ['all', ...Array.from(new Set(allCostRecs.map((r) => r.risk)))];
  const filteredCostRecs = allCostRecs.filter(
    (r) =>
      (costTypeFilter === 'all' || r.type === costTypeFilter) &&
      (costRiskFilter === 'all' || r.risk === costRiskFilter),
  );
  // R13.3: a recommendation of any type other than 'idle' names Worker Type and
  // Number of Workers (the Current/Recommended columns below). Those fields are
  // read-only everywhere in this dashboard, so the panel has to say how they can
  // actually be changed. Negative check on 'idle' rather than a positive check on
  // 'downsize'/'upgrade' because the agent emits 'upsize' where the frontend type
  // declares 'upgrade' — a known, unfixed producer/consumer drift (LGR-28b4d75-5).
  const namesWorkerConfig = filteredCostRecs.some((r) => r.type !== 'idle');

  // p39 R4.11 + R7.6 — the display props for one three-state figure: its text,
  // and the one description that says what the figure measures and, when there
  // is no figure, why. `field` is the payload's OWN name for the fact, which is
  // how both `measurement` and `determined_from` are keyed
  // (`handlers/metrics.py`'s single fact registry), so a tile, its source and
  // its provenance cannot address different things.
  // `agentcore.response_time_ms` is `agent_latency_ms` under a second name and
  // therefore reads that fact's measurement AND its reason — the backend
  // registers the fact once, and both published copies quote it.
  const determinedFrom = metrics.performance.determined_from;
  // R7.6 — tile field -> the measurement behind it. Read as possibly absent: a
  // payload assembled before p39 carries no such map, and a tile with nothing to
  // say about its source says nothing rather than throwing.
  const measurement: Record<string, string> | undefined = metrics.performance.measurement;
  const threeState = (
    field: string,
    value: number | null | undefined,
    format: (measured: number) => string,
    // One more statement for the tile, where the payload has something to say
    // beyond the measurement name — today only the cost basis (R7.5).
    alsoStates?: string,
  ): { value: string; description?: string } => {
    const rendered = formatOrNotEstablished(value, format);
    return {
      value: rendered,
      description: tileDescription(
        measurement?.[field],
        alsoStates,
        notEstablishedReason(rendered, field, determinedFrom),
      ),
    };
  };

  return (
    <SpaceBetween size="l" data-testid="metrics-dashboard">
      <Header
        variant="h1"
        info={
          <Link
            href={cloudwatchDashboardUrl}
            external
            data-testid="cloudwatch-dashboard-link"
          >
            Open CloudWatch Dashboard
          </Link>
        }
      >
        Metrics Dashboard
      </Header>

      {/* Performance Metrics */}
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <Select
                selectedOption={{
                  value: perfPeriod,
                  label: PERIOD_LABELS[perfPeriod] ?? perfPeriod,
                }}
                onChange={({ detail }) => setPerfPeriod(detail.selectedOption.value ?? '24h')}
                options={[
                  { value: '24h', label: 'Last 24 hours' },
                  { value: '7d', label: 'Last 7 days' },
                  { value: '30d', label: 'Last 30 days' },
                ]}
                data-testid="perf-period-select"
              />
            }
          >
            Performance Metrics
          </Header>
        }
      >
        <div data-testid="performance-metrics">
          <ColumnLayout columns={4} variant="text-grid">
            {/* p39 R7 — each label names what its tile measures, and the
                measurement itself is quoted from the payload rather than
                paraphrased here (R7.6), so a relabelling cannot leave the stated
                source behind. Every `data-testid` is unchanged: the labels are
                copy, the testIds are the contract the suites key on.

                "Rows Loaded" is a count, not an estimate: the rows the Glue job
                reports writing to its target, where the write was confirmed,
                for every source type. It replaced "Records Accepted
                (estimated)" on 2026-10-10. That tile divided file size by a fixed
                guess, re-counted re-runs, credited runs that never reached Glue,
                and said nothing about JDBC sources, which have no file.
                `measurement.lines_ingested_24h` carries what is and is not
                counted; the wire field keeps its historical name and the testId
                is unchanged. */}
            <MetricBox label="Rows Loaded" {...threeState('lines_ingested_24h', metrics.performance.lines_ingested_24h, (n) => n.toLocaleString())} testId="metric-lines-ingested" />
            {/* The same numerator over wall-clock hours in the selected window,
                which `measurement.processing_rate` says. It is an average over the
                window, not the speed of a run: the clock runs whether or not a
                pipeline does. The value renders "<n>/hr", so the label names the
                quantity and the figure names the unit. */}
            <MetricBox label="Rows Loaded per Hour" {...threeState('processing_rate', metrics.performance.processing_rate, (n) => `${n.toLocaleString()}/hr`)} testId="metric-processing-rate" />
            <MetricBox label="Success Rate" {...threeState('overall_success_rate', metrics.performance.overall_success_rate, (n) => `${n}%`)} testId="metric-success-rate" />
            {/* R7.1 — "Failed Executions", not "Failed Pipelines": the value is
                the `ExecutionsFailed` sum and one pipeline can contribute many
                failed executions. The testId keeps its original name.

                NOT routed through `threeState`, deliberately: `failed_pipelines`
                is not three-state. An `ExecutionsFailed` sum with no datapoints
                is a genuine zero — nothing failed — and the type declares it a
                plain `number` for that reason. It still states its measurement
                (R7.6 covers every tile), which is why the description is
                composed here rather than skipped. Since 2026-10-10 the figure has
                approval rejections taken out; the next tile counts them. */}
            <MetricBox
              label="Failed Executions"
              value={String(metrics.performance.failed_pipelines)}
              description={tileDescription(measurement?.['failed_pipelines'])}
              testId="metric-failed-pipelines"
            />
            {/* 2026-10-10 — runs a reviewer rejected at an approval gate. They end
                FAILED in Step Functions, but the gate did its job, so the backend
                takes them out of Failed Executions and the Success Rate and
                counts them here instead. Three-state, unlike Failed Executions:
                `null` means the failure records could not be read, and then
                nothing was taken out of the two tiles beside it. */}
            <MetricBox label="Rejected at Approval" {...threeState('rejected_at_approval', metrics.performance.rejected_at_approval, (n) => n.toLocaleString())} testId="metric-rejected-at-approval" />
            {/* R7.4 / D1 — the figure is now AgentCore stage duration, so the
                label names a duration rather than a latency. Renaming without
                re-sourcing was the fallback R7.4 permitted; task 15.1 re-sourced
                the read, and `measurement.agent_latency_ms` names the series. */}
            <MetricBox label="Avg Agent Stage Duration" {...threeState('agent_latency_ms', metrics.performance.agent_latency_ms, formatLatency)} testId="metric-agent-latency" />
            {/* R7.5 — the description carries the basis: the measurement names
                the series, `costBasisStatement` adds the runs and rate this
                window's figure was summed from.

                The label said "Est. Monthly Cost" over a figure that is the
                selected window's sum, not a month's (`_get_estimated_monthly_cost`'s
                docstring: "THE FIGURE IS THE SELECTED WINDOW'S"). On "Last 24
                hours" that put a day's $0.29 under the word "Monthly". The
                payload key `monthly_cost` and the testId keep their names, because
                the contract and other suites key on them; only the words change. */}
            <MetricBox label="Est. Glue Cost (selected period)" {...threeState('monthly_cost', metrics.performance.monthly_cost, (n) => `$${n.toLocaleString()}`, costBasisStatement(metrics.performance.monthly_cost_basis))} testId="metric-monthly-cost" />
          </ColumnLayout>
        </div>
      </Container>

      {/* Trend Indicators */}
      <Container
        header={
          <Header
            variant="h2"
            info={
              <DetailPopover header="Trend Indicators" ariaLabel="About Trend Indicators" testId="trend-indicators-about">
                <SpaceBetween size="xs">
                  <Box>
                    Each trend compares the period selected above with the period of the same length just
                    before it. For Last 24 hours, that is the 24 hours before that.
                  </Box>
                  <Box>
                    {NOT_ESTABLISHED} means one of the two periods has nothing to compare, for example
                    because no pipeline ran in the earlier period, or no Glue run finished. It is not the
                    same as Stable, which means both periods were measured and came out level.
                  </Box>
                  <Box>A longer period compares over more history.</Box>
                </SpaceBetween>
              </DetailPopover>
            }
          >
            Trend Indicators
          </Header>
        }
      >
        <div data-testid="trend-indicators">
          <ColumnLayout columns={4} variant="text-grid">
            <TrendBox label="Ingestion" metric="ingestion" direction={metrics.performance.trends.ingestion} testId="trend-ingestion" />
            <TrendBox label="Success Rate" metric="success" direction={metrics.performance.trends.success} testId="trend-success" />
            <TrendBox label="Agent Latency" metric="agent_latency" direction={metrics.performance.trends.agent_latency} testId="trend-agent-latency" />
            <TrendBox label="Cost" metric="cost" direction={metrics.performance.trends.cost} testId="trend-cost" />
          </ColumnLayout>
        </div>
      </Container>

      {/* Stage Metrics */}
      <Container header={<Header variant="h2">Stage Metrics</Header>}>
        <div data-testid="stage-metrics">
          {/*
            p59 — the reconciliation, ABOVE the counts it qualifies, so a reader
            meets the caveat before the numbers rather than after. `warning` when
            the failure-record read did not complete (the counts are a floor),
            `info` when it did and there is simply something the panel cannot
            attribute.
          */}
          <StageReconciliationNote
            attribution={metrics.performance.stage_failure_attribution}
            determinedFrom={metrics.performance.determined_from}
          />
          <ColumnLayout columns={3} variant="text-grid">
            {STAGE_KEYS.map((key) => {
              const stage = metrics.stages[key];
              if (!stage) return null;
              return (
                <ClickableArea
                  key={key}
                  data-testid={`stage-${key}`}
                  ariaLabel={`View ${formatStepName(key)} stage details`}
                  onClick={() => setSelectedStage({ name: key, metrics: stage })}
                  style={{ padding: '8px', borderRadius: '8px', border: `1px solid ${colorBorderDividerDefault}` }}
                >
                  <Box variant="h4">{formatStepName(key)}</Box>
                  <SpaceBetween size="xxs">
                    <Box variant="small" data-testid={`stage-${key}-success`}>Success: {stage.success_count}</Box>
                    <Box variant="small" data-testid={`stage-${key}-error`}>Errors: {stage.error_count}</Box>
                    <Box variant="small" data-testid={`stage-${key}-avg-time`}>Avg Time: {formatAvgTime(stage.avg_time_ms)}</Box>
                  </SpaceBetween>
                </ClickableArea>
              );
            })}
          </ColumnLayout>
        </div>
      </Container>

      {/* Infrastructure Status */}
      <Container header={<Header variant="h2">Infrastructure Status</Header>}>
        <div data-testid="infrastructure-status">
          <ColumnLayout columns={4} variant="text-grid">
            <InfraCard
              name="AgentCore"
              testId="infra-agentcore"
              status={metrics.infrastructure.agentcore.status}
              details={[
                { label: 'Availability', ...threeState('agentcore_availability', metrics.infrastructure.agentcore.availability, (n) => `${n}%`), testId: 'agentcore-availability' },
                { label: 'Response Time', ...threeState('agent_latency_ms', metrics.infrastructure.agentcore.response_time_ms, formatLatency), testId: 'agentcore-response-time' },
              ]}
            />
            <InfraCard
              name="DynamoDB"
              testId="infra-dynamodb"
              status={metrics.infrastructure.dynamodb.status}
              details={[
                { label: 'Read Capacity', value: `${Math.round(metrics.infrastructure.dynamodb.read_capacity_per_hour).toLocaleString()} RCU/hr`, testId: 'dynamodb-read-capacity' },
                { label: 'Write Capacity', value: `${Math.round(metrics.infrastructure.dynamodb.write_capacity_per_hour).toLocaleString()} WCU/hr`, testId: 'dynamodb-write-capacity' },
              ]}
            />
            <InfraCard
              name="S3"
              testId="infra-s3"
              status={metrics.infrastructure.s3.status}
              details={[
                { label: 'Storage Used', ...threeState('storage_used_bytes', metrics.infrastructure.s3.storage_used_bytes, formatBytes), testId: 's3-storage-used' },
                // R4.7 — the label is right and stays; the figure is always
                // Not_Established, because request metrics were never enabled on
                // these buckets, and the description carries that reason. Neither
                // this nor `Storage Used` appears in `measurement`: that map's key
                // set is the six performance tiles exactly, so these two state
                // their absence and nothing else.
                { label: 'Requests/min', ...threeState('requests_per_minute', metrics.infrastructure.s3.requests_per_minute, (n) => String(n)), testId: 's3-requests-per-min' },
              ]}
            />
            <InfraCard
              name="Lambda"
              testId="infra-lambda"
              status={metrics.infrastructure.lambda.status}
              details={[
                { label: 'Invocations/min', value: String(metrics.infrastructure.lambda.invocations_per_minute), testId: 'lambda-invocations' },
                { label: 'Error Rate', value: `${metrics.infrastructure.lambda.error_rate}%`, testId: 'lambda-error-rate' },
              ]}
            />
          </ColumnLayout>
        </div>
      </Container>

      {/* AI Token Usage — Async_State_Triple (R1.4): show an error+retry rather
          than silently hiding the widget on failure. */}
      {tokenError && (
        <Container header={<Header variant="h2">AI Token Usage</Header>}>
          <Alert
            type="error"
            data-testid="token-usage-error"
            action={<Button data-testid="token-usage-retry" onClick={() => refetchTokenMetrics()}>Retry</Button>}
          >
            Failed to load token usage metrics.
          </Alert>
        </Container>
      )}
      {!tokenError && tokenMetrics && (
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <Select
                  selectedOption={{ value: tokenPeriod, label: PERIOD_LABELS[tokenPeriod] ?? tokenPeriod }}
                  onChange={({ detail }) => setTokenPeriod(detail.selectedOption.value ?? '7d')}
                  options={[
                    { value: '1d', label: 'Last 24 hours' },
                    { value: '7d', label: 'Last 7 days' },
                    { value: '30d', label: 'Last 30 days' },
                    { value: '90d', label: 'Last 90 days' },
                  ]}
                  data-testid="token-period-select"
                />
              }
            >
              AI Token Usage
            </Header>
          }
        >
          <SpaceBetween size="m">
            {/* Summary metrics */}
            <div data-testid="token-usage-summary">
              <ColumnLayout columns={4} variant="text-grid">
                <MetricBox label="Total Input Tokens" value={tokenMetrics.total_input_tokens.toLocaleString()} testId="metric-total-input-tokens" />
                <MetricBox label="Total Output Tokens" value={tokenMetrics.total_output_tokens.toLocaleString()} testId="metric-total-output-tokens" />
                <MetricBox label="Total Cost" value={`$${(tokenMetrics.total_cost_usd ?? 0).toFixed(3)}`} testId="metric-total-cost" />
                <MetricBox
                  label="Cache Hit Rate"
                  value={tokenMetrics.total_input_tokens > 0
                    ? `${((tokenMetrics.total_cache_read_tokens / tokenMetrics.total_input_tokens) * 100).toFixed(1)}%`
                    : 'N/A'}
                  testId="metric-cache-hit-rate"
                />
              </ColumnLayout>
            </div>

            {/* Daily usage table (line chart substitute) */}
            <Table
              columnDefinitions={[
                { id: 'date', header: 'Date', cell: (item: TokenUsageDataPoint) => item.date },
                { id: 'input', header: 'Input Tokens', cell: (item: TokenUsageDataPoint) => item.input_tokens.toLocaleString() },
                { id: 'output', header: 'Output Tokens', cell: (item: TokenUsageDataPoint) => item.output_tokens.toLocaleString() },
                { id: 'cache', header: 'Cache Read', cell: (item: TokenUsageDataPoint) => item.cache_read_tokens.toLocaleString() },
                { id: 'cost', header: 'Est. Cost', cell: (item: TokenUsageDataPoint) => `$${item.estimated_cost_usd.toFixed(4)}` },
                { id: 'invocations', header: 'Invocations', cell: (item: TokenUsageDataPoint) => String(item.invocation_count) },
              ]}
              items={tokenMetrics.daily_usage}
              variant="embedded"
              stripedRows
              data-testid="token-daily-usage-table"
              header={<Header variant="h3">Daily Token Usage</Header>}
            />

            {/* Model comparison table */}
            {tokenMetrics.model_comparison.length > 0 && (
              <Table
                columnDefinitions={[
                  {
                    id: 'model',
                    header: 'Model',
                    // Show the exact model id (version-specific), only stripping the
                    // Bedrock region (us./eu./…) and vendor (anthropic./amazon./…)
                    // prefixes for readability — e.g. "claude-sonnet-4-6".
                    cell: (item: ModelComparisonRow) =>
                      item.model_id
                        .replace(/^(us|eu|apac|global)\./, '')
                        .replace(/^(anthropic|amazon|meta|mistral|cohere|ai21|deepseek)\./, ''),
                  },
                  { id: 'avg_tokens', header: 'Avg Tokens/Invocation', cell: (item: ModelComparisonRow) => `${item.avg_input_tokens.toLocaleString()} in / ${item.avg_output_tokens.toLocaleString()} out` },
                  { id: 'avg_cost', header: 'Avg Cost', cell: (item: ModelComparisonRow) => `$${item.avg_cost_usd.toFixed(4)}` },
                  // Routed through the same formatter as the tiles: `formatLatency`
                  // no longer answers for an absent value, so the null arm has to
                  // be decided somewhere, and it is decided in the one place.
                  { id: 'avg_latency', header: 'Avg Latency', cell: (item: ModelComparisonRow) => formatOrNotEstablished(item.avg_latency_ms, formatLatency) },
                  { id: 'count', header: 'Invocations', cell: (item: ModelComparisonRow) => String(item.invocation_count) },
                ]}
                items={tokenMetrics.model_comparison}
                variant="embedded"
                stripedRows
                data-testid="model-comparison-table"
                header={<Header variant="h3">Model Comparison</Header>}
              />
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* Cost Recommendations — Async_State_Triple (R1.4): surface an error+retry
          instead of silently hiding the widget on failure. */}
      {costError && (
        <Container header={<Header variant="h2">Cost Recommendations</Header>}>
          <Alert
            type="error"
            data-testid="cost-recommendations-error"
            action={<Button data-testid="cost-recommendations-retry" onClick={() => refetchCost()}>Retry</Button>}
          >
            Failed to load cost recommendations.
          </Alert>
        </Container>
      )}
      {!costError && costData && costData.recommendations?.length > 0 && (
        <Container
          header={
            <Header
              variant="h2"
              counter={`(${filteredCostRecs.length}/${allCostRecs.length})`}
              description={`Total projected savings: $${(costData.total_projected_savings_monthly ?? 0).toFixed(2)}/month`}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Select
                    selectedOption={{ value: costTypeFilter, label: costTypeFilter === 'all' ? 'All types' : costTypeFilter }}
                    onChange={({ detail }) => setCostTypeFilter(detail.selectedOption.value ?? 'all')}
                    options={costTypeOptions.map((t) => ({ value: t, label: t === 'all' ? 'All types' : t }))}
                    data-testid="cost-type-filter"
                  />
                  <Select
                    selectedOption={{ value: costRiskFilter, label: costRiskFilter === 'all' ? 'All risk' : costRiskFilter }}
                    onChange={({ detail }) => setCostRiskFilter(detail.selectedOption.value ?? 'all')}
                    options={costRiskOptions.map((r) => ({ value: r, label: r === 'all' ? 'All risk' : r }))}
                    data-testid="cost-risk-filter"
                  />
                </SpaceBetween>
              }
            >
              Cost Recommendations
            </Header>
          }
          data-testid="cost-recommendations"
        >
          <SpaceBetween size="m">
            {filteredCostRecs.length === 0 && (
              <Box textAlign="center" color="text-body-secondary" padding="m">
                No recommendations match the selected filters.
              </Box>
            )}
            {/* R13.3: these recommendations name Worker Type and Number of Workers, and
                both are read-only in this dashboard. Stating the real route — including
                the fact that it is gated and not durable — is the requirement; a vaguer
                "edit the job configuration" would be the misdirection R13 exists to
                remove. Panel-level rather than per-recommendation so the statement stays
                visible with every ExpandableSection collapsed, and is not repeated once
                per row. */}
            {namesWorkerConfig && (
              <Alert
                type="info"
                header="How to change Worker Type and Number of Workers"
                data-testid="worker-config-change-route"
              >
                <SpaceBetween size="xxs">
                  <Box variant="small">
                    This dashboard cannot change them. There is no update route for a Glue job&apos;s
                    configuration, which is why the job&apos;s Configuration panel is read-only.
                  </Box>
                  <Box variant="small">
                    Apply is offered only on low-risk downsize recommendations, and it changes
                    nothing until an operator sets <Box variant="code">AUTO_APPLY_ENABLED=true</Box>{' '}
                    on both the dashboard API and the cost optimization agent. It is off by default.
                  </Box>
                  <Box variant="small">
                    Anything else has to be changed on the Glue job itself, in the AWS Glue console
                    or with <Box variant="code">aws glue update-job</Box>. That change is not
                    durable: the pipeline&apos;s infrastructure stage re-derives Worker Type and
                    Number of Workers from the measured data volume on every run and calls
                    UpdateJob, so a later run can overwrite it.
                  </Box>
                </SpaceBetween>
              </Alert>
            )}
            {filteredCostRecs.map((rec: CostRecommendation) => (
              <ExpandableSection
                key={rec.id}
                variant="container"
                data-testid={`cost-rec-${rec.id}`}
                headerText={
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span>{rec.pipeline_id}</span>
                    <Badge color={rec.risk === 'low' ? 'green' : rec.risk === 'medium' ? 'blue' : 'red'}>
                      {rec.risk} risk
                    </Badge>
                    <Badge>{rec.type}</Badge>
                  </span>
                }
              >
                <SpaceBetween size="xs">
                  {rec.type === 'idle' ? (
                    <Box variant="small">{rec.recommendation ?? `No runs since ${rec.last_run}`}</Box>
                  ) : (
                    <ColumnLayout columns={3} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Current</Box>
                        <Box variant="small">
                          {rec.current ? `${rec.current.worker_type} × ${rec.current.workers}` : '—'}
                        </Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Recommended</Box>
                        <Box variant="small">
                          {rec.recommended ? `${rec.recommended.worker_type} × ${rec.recommended.workers}` : '—'}
                        </Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Projected Savings</Box>
                        <Box variant="small">
                          {rec.projected_savings_monthly != null ? `$${rec.projected_savings_monthly.toFixed(2)}/month` : '—'}
                        </Box>
                      </div>
                    </ColumnLayout>
                  )}
                  {rec.avg_dpu_utilization != null && (
                    <Box variant="small" color="text-body-secondary">
                      Avg DPU utilization: {(rec.avg_dpu_utilization * 100).toFixed(0)}%
                    </Box>
                  )}
                  {rec.risk === 'low' && rec.auto_apply_eligible && (
                    <Button
                      variant="primary"
                      loading={applyRecommendation.isPending}
                      onClick={() => applyRecommendation.mutate(rec.id)}
                      data-testid={`apply-rec-${rec.id}`}
                    >
                      Apply
                    </Button>
                  )}
                </SpaceBetween>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        </Container>
      )}

      {/* Stage Detail Modal */}
      <Modal
        visible={!!selectedStage}
        onDismiss={() => setSelectedStage(null)}
        header={selectedStage ? `${formatStepName(selectedStage.name)} Details` : ''}
        data-testid="stage-detail-modal"
      >
        {selectedStage && (
          <SpaceBetween size="m">
            <Box variant="p">
              <strong>Success Rate:</strong>{' '}
              {selectedStage.metrics.success_count + selectedStage.metrics.error_count > 0
                ? `${((selectedStage.metrics.success_count / (selectedStage.metrics.success_count + selectedStage.metrics.error_count)) * 100).toFixed(1)}%`
                : 'N/A'}
            </Box>
            <Box variant="p"><strong>Success Count:</strong> {selectedStage.metrics.success_count}</Box>
            <Box variant="p"><strong>Error Count:</strong> {selectedStage.metrics.error_count}</Box>
            <Box variant="p"><strong>Average Time:</strong> {formatAvgTime(selectedStage.metrics.avg_time_ms)}</Box>
            {selectedStage.metrics.error_count > 0 && (
              <Box variant="p" color="text-status-error">
                <strong>Recent Errors:</strong> {selectedStage.metrics.error_count} errors detected in this stage.
              </Box>
            )}
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/**
 * p59 — the Stage Metrics panel's reconciliation against `failed_pipelines`.
 *
 * Renders nothing when there is nothing to say: the failure-record read
 * completed, every failure it found is already inside a stage's `error_count`,
 * and the residual against Failed Executions is zero. That silence is a
 * statement in itself, and it is the only case where silence is honest — every
 * other case produces at least one sentence from
 * `stageReconciliationStatements`.
 */
function StageReconciliationNote({
  attribution,
  determinedFrom,
}: {
  attribution: StageFailureAttribution | null | undefined;
  determinedFrom: Record<string, string> | undefined;
}) {
  const statements = stageReconciliationStatements(attribution, determinedFrom);
  if (statements.length === 0) return null;

  // `warning` only for the unreadable case: there, the counts below are a floor
  // and a reader acting on them would be acting on an undercount. Everything
  // else is a complete answer that simply does not fit in six stage buckets.
  const unreadable = attribution == null;
  return (
    <Box padding={{ bottom: 's' }}>
      <Alert
        type={unreadable ? 'warning' : 'info'}
        statusIconAriaLabel={unreadable ? 'Warning' : 'Info'}
        header="Runs not shown per stage"
        data-testid="stage-failure-reconciliation"
      >
        <SpaceBetween size="xxs">
          {statements.map((statement) => (
            <Box key={statement} variant="small">{statement}</Box>
          ))}
        </SpaceBetween>
      </Alert>
    </Box>
  );
}

function MetricBox({
  label,
  value,
  testId,
  description,
}: {
  label: string;
  /** Already-formatted display text — the caller decides Not_Established, not this. */
  value: string;
  testId: string;
  /**
   * ONE description slot, composed by `tileDescription` — the measurement behind
   * the figure (R7.6) and, when the figure is absent, the `determined_from`
   * reason for that (R4.11). A second slot was rejected: two slots is how a
   * tile's label and its source start drifting apart, which is the thing R7.6
   * exists to prevent.
   */
  description?: string;
}) {
  // The description is the same one composed slot, shown on request rather than
  // printed under every figure. The label stays the FIRST child and the figure
  // the second: the suites read a tile by that shape, and the popover trigger
  // carries no text of its own, so it adds nothing to the label's text.
  return (
    <div data-testid={testId}>
      <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
        <Box variant="small" color="text-body-secondary">{label}</Box>
        {description && (
          <DetailPopover header={label} ariaLabel={`About ${label}`} testId={`${testId}-description`}>
            {description}
          </DetailPopover>
        )}
      </SpaceBetween>
      <Box variant="h3">{value}</Box>
    </div>
  );
}


function TrendBox({
  label,
  metric,
  direction,
  testId,
}: {
  label: string;
  /** Identifies the metric, which is what determines polarity. */
  metric: TrendMetric;
  direction: TrendDirection;
  testId: string;
}) {
  const polarity = TREND_POLARITY[metric];
  return (
    <div data-testid={testId}>
      <Box variant="small" color="text-body-secondary">{label}</Box>
      <Box variant="h3">
        <Box color={trendColor(direction, polarity)} display="inline" data-testid={`${testId}-arrow`}>
          {trendArrow(direction)}
        </Box>{' '}
        {trendLabel(direction)}
      </Box>
      {/* Polarity is stated in text as well as carried by colour: colour alone
          is not an accessible signal (WCAG 1.4.1), and the inversion on Cost
          and Agent Latency is invisible without it. */}
      <Box variant="small" color="text-body-secondary" data-testid={`${testId}-polarity`}>
        {POLARITY_CAPTION[polarity]}
      </Box>
    </div>
  );
}

function InfraCard({
  name,
  testId,
  status,
  details,
}: {
  name: string;
  testId: string;
  status: HealthStatus;
  /**
   * `description` is the same one composed slot `MetricBox` takes: the
   * measurement behind the figure where the payload names one (R7.6), and the
   * `determined_from` reason behind an absent figure (R4.11).
   */
  details: { label: string; value: string; testId: string; description?: string }[];
}) {
  return (
    <div data-testid={testId}>
      <SpaceBetween size="xxs">
        <Box variant="h4">{name}</Box>
        <StatusIndicator type={healthIndicatorType(status)}>{status}</StatusIndicator>
        {details.map((d) => (
          <div key={d.testId}>
            <Box variant="small" data-testid={d.testId}>
              {d.label}: {d.value}
            </Box>
            {d.description && (
              <Box variant="small" color="text-body-secondary" data-testid={`${d.testId}-description`}>
                {d.description}
              </Box>
            )}
          </div>
        ))}
      </SpaceBetween>
    </div>
  );
}
