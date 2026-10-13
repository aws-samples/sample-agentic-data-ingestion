// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import { formatTimestamp, statusType } from '../../utils/formatting';
import type { RunDetail } from '../../types';

interface RunMetadataPanelProps {
  runDetail: RunDetail;
  isActive: boolean;
  isWebSocketConnected: boolean;
  optimisticAction: 'approving' | 'rejecting' | null;
  onApprove: (approvalId: string) => void;
  onReject: (approvalId: string) => void;
}

/**
 * Run metadata card + live/polling badge + pending-approval action buttons
 * extracted from RunDetailPage (P29 R11.5). The optimistic-banner state and
 * the shared approval mutation stay owned by the parent; the Approve/Reject
 * buttons forward the pending approval id via callbacks. Behavior-preserving.
 */
export default function RunMetadataPanel({
  runDetail,
  isActive,
  isWebSocketConnected,
  optimisticAction,
  onApprove,
  onReject,
}: RunMetadataPanelProps) {
  return (
    <Container header={<Header variant="h2">Run Details</Header>}>
      <ColumnLayout columns={5} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Status</Box>
          <StatusIndicator type={statusType(runDetail.status)}>
            {runDetail.status}
          </StatusIndicator>
        </div>
        <div>
          <Box variant="awsui-key-label">Pipeline</Box>
          <Box>{runDetail.pipeline_key}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Started</Box>
          <Box>{runDetail.started_at ? formatTimestamp(runDetail.started_at) : '—'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Completed</Box>
          <Box>{runDetail.completed_at ? formatTimestamp(runDetail.completed_at) : '—'}</Box>
        </div>
        {/*
          Schema Version (p54 R4.1, R4.2). The expression is copied byte-for-byte
          from the two run lists — ExecutionsPage.tsx:383 and
          PipelineOverviewPage.tsx:542 — so all three surfaces spell the key and
          the not-established indication identically. The '—' is U+2014 EM DASH.

          Its `!= null` guard deliberately DIFFERS from the two timestamp cells
          above, which are truthiness tests. Do not harmonise them (p54 do-NOT
          10, p48 R8.3): `completed_at` can arrive present-and-empty for a run
          that was read and records no completion, and `formatTimestamp('')`
          returns 'Unknown', so a `!= null` guard there would invent a time for
          an unfinished run. `schema_version` has no such empty shape — it is
          absent or a number, never '' (types.ts:161-170, LGR-44d601e-1) — so
          `!= null` is the whole test here. Both guards are pinned by
          __tests__/RunMetadataPanel.characterization.test.tsx.
        */}
        <div>
          <Box variant="awsui-key-label">Schema Version</Box>
          <Box>{runDetail.schema_version != null ? <Badge>v{runDetail.schema_version}</Badge> : '—'}</Box>
        </div>
      </ColumnLayout>
      {isActive && (
        <Box padding={{ top: 's' }}>
          {isWebSocketConnected ? (
            <Badge color="green">Live updates via WebSocket</Badge>
          ) : (
            <Badge color="blue">Polling every 5s</Badge>
          )}
        </Box>
      )}
      {/* Pending approval action buttons — optimistic UI (event-driven refresh) */}
      {(() => {
        const pendingApproval = (runDetail.approvals ?? []).find((a) => a.status === 'pending');

        // Optimistic feedback while the action resolves. The banner clears
        // when the run no longer has a pending approval (see effect in parent) —
        // no setTimeout refetch chains.
        if (optimisticAction === 'approving') {
          return (
            <Box padding={{ top: 'm' }}>
              <Alert type="info"><Spinner size="normal" /> Approving... pipeline will resume shortly.</Alert>
            </Box>
          );
        }
        if (optimisticAction === 'rejecting') {
          return (
            <Box padding={{ top: 'm' }}>
              <Alert type="info"><Spinner size="normal" /> Rejecting... pipeline will stop.</Alert>
            </Box>
          );
        }

        if (!pendingApproval) return null;
        return (
          <Box padding={{ top: 'm' }}>
            <Alert type="warning" action={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="primary"
                  onClick={() => onApprove(pendingApproval.approval_id)}
                >
                  Approve
                </Button>
                <Button
                  onClick={() => onReject(pendingApproval.approval_id)}
                >
                  Reject
                </Button>
              </SpaceBetween>
            }>
              Waiting for approval: {pendingApproval.approval_type?.replace(/_/g, ' ')}
            </Alert>
          </Box>
        );
      })()}
    </Container>
  );
}
