// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import { useUpdates } from '../hooks';
import { statusType, formatRelativeTime } from '../utils/formatting';
import ClickableArea from './common/ClickableArea';
import type { UpdateItem } from '../types';

interface ActivityFeedProps {
  limit?: number;
  collapsedCount?: number;
}

export function ActivityFeedItem({ item, onClick }: { item: UpdateItem; onClick?: () => void }) {
  const displayName = item.pipeline_name || item.pipeline_id;
  return (
    <ClickableArea
      data-testid={`activity-item-${item.run_id}`}
      onClick={onClick}
      ariaLabel={onClick ? `View run for ${displayName}` : undefined}
    >
      <SpaceBetween size="xxs" direction="horizontal">
        <StatusIndicator type={statusType(item.status)}>{displayName}</StatusIndicator>
        {item.current_step && (
          <Box color="text-body-secondary" fontSize="body-s">{item.current_step}</Box>
        )}
        <Box color="text-body-secondary" fontSize="body-s">
          {formatRelativeTime(item.started_at)}
        </Box>
      </SpaceBetween>
    </ClickableArea>
  );
}

export default function ActivityFeed({ limit = 10, collapsedCount = 3 }: ActivityFeedProps) {
  const { data, isLoading, isError } = useUpdates(limit);
  const [expanded, setExpanded] = useState(false);
  const navigate = useNavigate();

  if (isLoading) {
    return (
      <Box data-testid="activity-feed-loading">
        <Spinner size="normal" />
      </Box>
    );
  }

  if (isError) {
    return (
      <StatusIndicator type="error">Failed to load recent activity</StatusIndicator>
    );
  }

  const updates = data?.updates ?? [];
  if (!updates.length) {
    return <Box color="text-body-secondary" fontSize="body-s">No recent activity</Box>;
  }

  const visible = expanded ? updates : updates.slice(0, collapsedCount);
  const hasMore = updates.length > collapsedCount;

  return (
    <div data-testid="activity-feed">
      <SpaceBetween size="xxs">
        {visible.map((item) => (
          <ActivityFeedItem
            key={item.run_id}
            item={item}
            onClick={() => navigate(`/pipelines/${item.pipeline_id}/runs/${item.run_id}`)}
          />
        ))}
        {hasMore && (
          <Button
            variant="inline-link"
            onClick={() => setExpanded(!expanded)}
            data-testid="activity-feed-toggle"
          >
            {expanded ? 'Show less' : `Show ${updates.length - collapsedCount} more`}
          </Button>
        )}
      </SpaceBetween>
    </div>
  );
}
