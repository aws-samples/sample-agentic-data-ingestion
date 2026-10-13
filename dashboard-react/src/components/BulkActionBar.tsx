// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import type { Pipeline } from '../types';
import type { BulkProgress } from '../hooks';

export interface BulkActionBarProps {
  selectedItems: Pipeline[];
  onArchive: () => void;
  onDelete: () => void;
  progress: BulkProgress | null;
  isExecuting: boolean;
}

export default function BulkActionBar({
  selectedItems,
  onArchive,
  onDelete,
  progress,
  isExecuting,
}: BulkActionBarProps) {
  const allArchived = selectedItems.length > 0 && selectedItems.every((p) => p.status === 'archived');

  return (
    <Box padding={{ vertical: 'xs' }}>
      <SpaceBetween direction="horizontal" size="s">
        <Button
          data-testid="bulk-archive-button"
          onClick={onArchive}
          disabled={isExecuting}
          loading={isExecuting && progress !== null && !allArchived}
        >
          Archive Selected ({selectedItems.length})
        </Button>
        <Button
          data-testid="bulk-delete-button"
          onClick={onDelete}
          disabled={!allArchived || isExecuting}
          loading={isExecuting && progress !== null && allArchived}
        >
          Delete Selected ({selectedItems.length})
        </Button>
        {progress && (
          <Box
            data-testid="bulk-progress"
            variant="span"
            color="text-status-info"
            fontSize="body-s"
          >
            {progress.completed}/{progress.total}
          </Box>
        )}
      </SpaceBetween>
    </Box>
  );
}
