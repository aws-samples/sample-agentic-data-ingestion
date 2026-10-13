// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import { usePipelineCosts } from '../hooks';
import { formatUsd } from './common/formatters';
import { formatRelativeTime } from '../utils/formatting';
import type { PipelineCostRun } from '../hooks';

interface PipelineCostPanelProps {
  /** Pipeline key — the same value the executions and preview calls use. */
  pipelineId: string;
  /**
   * Whether this pipeline has any run records at all, if the caller happens to
   * know. `GET /costs` cannot answer this: it reports `run_count: 0` both for a
   * pipeline that has never run and for one whose runs carry no cost yet, so
   * the caller's already-fetched execution list is the only cheap way to tell
   * those two apart in the empty-state copy. `undefined` (caller does not know,
   * or is still loading) falls back to copy that covers both cases.
   */
  hasRuns?: boolean;
}

/** Glue cost scope, spelled out on the surface itself (R3.3). */
const SCOPE_NOTE =
  'Actual AWS Glue cost only: DPU-hours at the published $0.44 per DPU-hour rate, ' +
  "including Glue's 10-minute minimum billing. Lambda, DynamoDB, S3 and Bedrock " +
  'costs are not included.';

/**
 * Per-pipeline cost, against the existing `GET /api/pipelines/{id}/costs`
 * endpoint (P30 R3.1–R3.4).
 *
 * No cost is computed here and none is computed in a new backend path:
 * `job_completion.collect_job_metrics` already writes `actual_cost` per run and
 * `handlers/costs.py` already aggregates it. This component was the only missing
 * piece.
 *
 * Async_State_Triple (R3.4), following the Container-level house pattern from
 * `StatsBar` and `run-detail/AiTracesTab`:
 *   - loading  → spinner
 *   - error    → `Alert type="error"` with a Retry wired to the query's own
 *                `refetch`, and NO figures (zeros here would read as "this
 *                pipeline is free", which is exactly the misreading R3.4 exists
 *                to prevent)
 *   - empty    → copy explaining why there is no cost yet, again not zeros
 */
export default function PipelineCostPanel({ pipelineId, hasRuns }: PipelineCostPanelProps) {
  const { data, isLoading, isError, refetch } = usePipelineCosts(pipelineId);

  const header = <Header variant="h2">Cost</Header>;

  if (isLoading) {
    return (
      <Container header={header}>
        <Box textAlign="center" padding="l" data-testid="cost-loading">
          <Spinner size="large" />
        </Box>
      </Container>
    );
  }

  if (isError || !data) {
    return (
      <Container header={header}>
        <Alert
          type="error"
          data-testid="cost-error"
          action={<Button data-testid="cost-retry" onClick={() => refetch()}>Retry</Button>}
        >
          Failed to load cost for this pipeline.
        </Alert>
      </Container>
    );
  }

  // Genuinely empty: the endpoint answered, and no run of this pipeline carries
  // a cost. Not an error, and not a $0.00 pipeline.
  if (data.run_count === 0) {
    return (
      <Container header={header}>
        <SpaceBetween size="s">
          <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="cost-empty">
            {hasRuns === false
              ? 'No cost recorded yet — this pipeline has not run.'
              : hasRuns === true
                ? 'No cost recorded yet. Glue cost is attributed when a run\u2019s Glue job finishes, so a run still in progress — or one that failed before its Glue job started — contributes nothing here.'
                : 'No cost recorded yet. Glue cost is attributed when a run\u2019s Glue job finishes.'}
          </Box>
          <Box variant="small" color="text-body-secondary" data-testid="cost-scope-note">
            {SCOPE_NOTE}
          </Box>
        </SpaceBetween>
      </Container>
    );
  }

  return (
    <Container header={header} data-testid="cost-panel">
      <SpaceBetween size="l">
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Total Glue cost</Box>
            <Box variant="p" fontSize="display-l" fontWeight="bold" data-testid="cost-total">
              {formatUsd(data.total_cost)}
            </Box>
            <Box variant="small" color="text-body-secondary">
              across {data.run_count} run{data.run_count === 1 ? '' : 's'}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Average per run</Box>
            <Box variant="p" fontSize="display-l" fontWeight="bold" data-testid="cost-average">
              {formatUsd(data.average_cost_per_run)}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Most expensive run</Box>
            <Box variant="p" fontSize="display-l" fontWeight="bold" data-testid="cost-most-expensive">
              {formatUsd(data.most_expensive_run?.actual_cost)}
            </Box>
            {data.most_expensive_run && (
              <Box variant="small" color="text-body-secondary" data-testid="cost-most-expensive-run">
                {data.most_expensive_run.run_id}
              </Box>
            )}
          </div>
        </ColumnLayout>

        {/* cost_trend as a per-run series (R3.2). The API returns the 7 most
            recent runs newest-first; that order is preserved so this reads the
            same way as Recent Runs on the same page. */}
        <Table
          variant="embedded"
          data-testid="cost-trend-table"
          items={data.cost_trend}
          header={
            <Header variant="h3" counter={`(${data.cost_trend.length})`}>
              Cost per run
            </Header>
          }
          columnDefinitions={[
            {
              id: 'run_id',
              header: 'Run',
              cell: (item: PipelineCostRun) => item.run_id || '—',
            },
            {
              id: 'timestamp',
              header: 'Recorded',
              cell: (item: PipelineCostRun) =>
                item.timestamp ? formatRelativeTime(item.timestamp) : '—',
            },
            {
              id: 'actual_cost',
              header: 'Cost',
              cell: (item: PipelineCostRun) => formatUsd(item.actual_cost),
            },
          ]}
          empty={<Box textAlign="center" color="text-body-secondary">No runs with a recorded cost</Box>}
          footer={
            <Box variant="small" color="text-body-secondary">
              Most recent {data.cost_trend.length} run
              {data.cost_trend.length === 1 ? '' : 's'}, newest first
              {data.run_count > data.cost_trend.length
                ? ` (of ${data.run_count} with a recorded cost)`
                : ''}
              .
            </Box>
          }
        />

        <Box variant="small" color="text-body-secondary" data-testid="cost-scope-note">
          {SCOPE_NOTE}
        </Box>
      </SpaceBetween>
    </Container>
  );
}
