// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator, { type StatusIndicatorProps } from '@cloudscape-design/components/status-indicator';
import Link from '@cloudscape-design/components/link';
import StepFlowVisualization from '../StepFlowVisualization';
import DataPreviewTable from '../DataPreviewTable';
import type { RunDetail, Pipeline } from '../../types';
import type { SelfHealingActivity } from '../../hooks/use-self-healing';

interface StepFlowTabProps {
  runDetail: RunDetail;
  pipelineId?: string;
  runId?: string;
  selfHealingActivity?: SelfHealingActivity | null;
  pipelineDetail?: Pipeline;
}

/**
 * Severity for a `rerun_status`, which the self-healing agent emits as one of:
 * `success`, `running`, `failed`, `start_failed`, `skipped`,
 * `skipped_heal_guard`, `skipped_partial_write_guard`.
 *
 * Every `skipped*` value is a DECISION, not a fault. The agent diagnosed the
 * failure and deliberately did not re-run — either because the error class is
 * not auto-healable (`skipped`), or because a guard refused (the other two).
 * The previous mapping was `success ? 'success' : running ? 'in-progress' :
 * 'error'`, so all three landed on `error` and a correct decision was rendered
 * with a red error cross, indistinguishable from a re-run that genuinely broke.
 *
 * `failed` and `start_failed` are the two real failures and keep `error`. An
 * unrecognised value also gets `error`, deliberately: a status this component
 * does not know about should look wrong rather than quietly reassuring.
 */
export function rerunStatusType(status: string): StatusIndicatorProps.Type {
  if (status === 'success') return 'success';
  if (status === 'running') return 'in-progress';
  if (status.startsWith('skipped')) return 'info';
  return 'error';
}

/**
 * Step Flow tab content extracted from RunDetailPage (P29 R11.5 decomposition).
 * Presentational — renders the step-flow visualization, the optional
 * self-healing activity card, and the data-preview table.
 */
export default function StepFlowTab({
  runDetail,
  pipelineId,
  runId,
  selfHealingActivity,
  pipelineDetail,
}: StepFlowTabProps) {
  return (
    <SpaceBetween size="l">
      <StepFlowVisualization
        steps={runDetail.steps ?? []}
        overallStatus={runDetail.status}
        trigger={runDetail.trigger}
        retrySourceRunId={runDetail.retry_source_run_id}
        runId={runId}
        pipelineId={pipelineId}
      />
      {selfHealingActivity && (
        <Container header={<Header variant="h2">Self-Healing Activity</Header>}>
          <SpaceBetween size="m">
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Error Classification</Box>
                <Badge color={selfHealingActivity.rerun_status === 'success' ? 'green' : 'red'}>
                  {selfHealingActivity.error_classification}
                </Badge>
              </div>
              <div>
                <Box variant="awsui-key-label">Re-run Status</Box>
                <StatusIndicator
                  type={rerunStatusType(selfHealingActivity.rerun_status)}
                  data-testid="self-healing-rerun-status"
                >
                  {selfHealingActivity.rerun_status}
                </StatusIndicator>
              </div>
              <div>
                <Box variant="awsui-key-label">Re-run</Box>
                {/* `rerun_id` is "" whenever no re-run started, which is every
                    `skipped*` outcome. The unguarded Link rendered empty text
                    against an href ending in `/runs/` — an invisible, clickable
                    dead link to a route with no run id. */}
                {selfHealingActivity.rerun_id ? (
                  <Link href={`/pipelines/${pipelineId}/runs/${selfHealingActivity.rerun_id}`}>
                    {selfHealingActivity.rerun_id}
                  </Link>
                ) : (
                  <Box variant="p" data-testid="self-healing-no-rerun">
                    None — no re-run was started
                  </Box>
                )}
              </div>
            </ColumnLayout>
            <div>
              <Box variant="awsui-key-label">Remediation Applied</Box>
              <Box variant="p">{selfHealingActivity.remediation_applied}</Box>
            </div>
          </SpaceBetween>
        </Container>
      )}
      {pipelineDetail && (
        <DataPreviewTable pipelineId={pipelineId!} runId={runId} sourceType={pipelineDetail.source_type} />
      )}
    </SpaceBetween>
  );
}
