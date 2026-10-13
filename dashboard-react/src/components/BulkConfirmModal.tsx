// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import type { Pipeline } from '../types';

export interface BulkConfirmModalProps {
  visible: boolean;
  action: 'archive' | 'delete';
  pipelines: Pipeline[];
  onConfirm: () => void;
  onDismiss: () => void;
}

export default function BulkConfirmModal({
  visible,
  action,
  pipelines,
  onConfirm,
  onDismiss,
}: BulkConfirmModalProps) {
  const actionLabel = action === 'archive' ? 'Archive' : 'Delete';

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={`${actionLabel} ${pipelines.length} pipeline${pipelines.length !== 1 ? 's' : ''}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} data-testid="bulk-confirm-cancel">
              Cancel
            </Button>
            <Button variant="primary" onClick={onConfirm} data-testid="bulk-confirm-submit">
              {actionLabel}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          Are you sure you want to {action} the following {pipelines.length} pipeline
          {pipelines.length !== 1 ? 's' : ''}?
        </Box>
        <ul data-testid="bulk-confirm-list">
          {pipelines.map((p) => (
            <li key={p.pipeline_key}>{p.pipeline_name || p.pipeline_key}</li>
          ))}
        </ul>
      </SpaceBetween>
    </Modal>
  );
}
