// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

interface ApprovalsTabProps {
  approvals?: Approval[];
  runDetails: RunDetail;
  onApprove: (approvalId: string, questionAnswers?: Record<string, string>) => void;
  onReject: (approvalId: string) => void;
}

/**
 * Approvals tab content extracted from RunDetailPage (P29 R11.5).
 * The approve/reject callbacks (which route through the shared
 * `useApprovalAction` hook) stay owned by the parent and are forwarded here.
 */
export default function ApprovalsTab({
  approvals,
  runDetails,
  onApprove,
  onReject,
}: ApprovalsTabProps) {
  return (
    <SpaceBetween size="l">
      {approvals && approvals.length > 0 ? (
        <ApprovalWorkflowSection
          approvals={approvals}
          runDetails={runDetails}
          onApprove={onApprove}
          onReject={onReject}
        />
      ) : (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No approval requests for this run.
        </Box>
      )}
    </SpaceBetween>
  );
}
