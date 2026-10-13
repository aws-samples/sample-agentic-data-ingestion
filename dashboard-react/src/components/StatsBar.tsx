// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import { useStats } from '../hooks';
import { formatRelativeTime } from '../utils/formatting';
import ActivityFeed from './ActivityFeed';

export default function StatsBar() {
  const { data: stats, isLoading, isError, refetch } = useStats();

  if (isLoading) {
    return (
      <Box data-testid="stats-bar-loading">
        <Spinner size="normal" />
      </Box>
    );
  }

  const lastActivity = stats?.last_activity
    ? formatRelativeTime(stats.last_activity)
    : '—';

  return (
    <div data-testid="stats-bar" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
      {/* Async_State_Triple (R1.4): on failure surface an error + retry rather
          than rendering misleading zeros for every stat. */}
      {isError ? (
        <Alert
          type="error"
          data-testid="stats-bar-error"
          action={<Button data-testid="stats-bar-retry" onClick={() => refetch()}>Retry</Button>}
        >
          Failed to load stats.
        </Alert>
      ) : (
      <SpaceBetween direction="horizontal" size="xl">
        <Box variant="div" data-testid="stat-total-pipelines">
          <Box variant="awsui-key-label">Total Pipelines</Box>
          <Box variant="p" fontSize="display-l" fontWeight="bold">
            {stats?.total_pipelines ?? 0}
          </Box>
        </Box>
        <Box variant="div" data-testid="stat-total-files">
          <Box variant="awsui-key-label">Total Files</Box>
          <Box variant="p" fontSize="display-l" fontWeight="bold">
            {stats?.total_files ?? 0}
          </Box>
        </Box>
        <Box variant="div" data-testid="stat-schema-versions">
          <Box variant="awsui-key-label">Schema Versions</Box>
          <Box variant="p" fontSize="display-l" fontWeight="bold">
            {stats?.schema_versions ?? 0}
          </Box>
        </Box>
        <Box variant="div" data-testid="stat-last-activity">
          <Box variant="awsui-key-label">Last Activity</Box>
          <Box variant="p" fontSize="display-l" fontWeight="bold">
            {lastActivity}
          </Box>
        </Box>
      </SpaceBetween>
      )}
      <Box variant="div">
        <Box variant="awsui-key-label">Recent Activity</Box>
        <Box padding={{ top: 'xxs' }}>
          <ActivityFeed collapsedCount={3} />
        </Box>
      </Box>
    </div>
  );
}
