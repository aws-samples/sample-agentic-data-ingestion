// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo } from 'react';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import TextFilter from '@cloudscape-design/components/text-filter';
import {
  colorTextStatusError,
  colorTextStatusWarning,
  colorBackgroundContainerContent,
  colorTextBodyDefault,
  colorBorderDividerDefault,
} from '@cloudscape-design/design-tokens';
import { useGlueJobLogs } from '../hooks';
import type { GlueJobRun, GlueLogEvent } from '../types';

const TERMINAL_STATUSES = ['SUCCEEDED', 'FAILED', 'STOPPED'];

export interface GlueJobLogViewerProps {
  pipelineId: string;
  runs: GlueJobRun[];
}

function logLineColor(level: string): string | undefined {
  const upper = level.toUpperCase();
  if (upper === 'ERROR') return colorTextStatusError;
  if (upper === 'WARN' || upper === 'WARNING') return colorTextStatusWarning;
  return undefined;
}

export default function GlueJobLogViewer({ pipelineId, runs }: GlueJobLogViewerProps) {
  const [selectedRunId, setSelectedRunId] = useState<string | undefined>(
    runs.length > 0 ? runs[0].run_id : undefined,
  );
  const [logType, setLogType] = useState<string>('output');
  const [filterText, setFilterText] = useState('');

  const selectedRun = runs.find((r) => r.run_id === selectedRunId);
  const isRunning = selectedRun ? !TERMINAL_STATUSES.includes(selectedRun.status.toUpperCase()) : false;

  const { data, isLoading, isError, refetch } = useGlueJobLogs(
    pipelineId,
    selectedRunId,
    logType,
    isRunning,
  );

  const filteredLogs = useMemo(() => {
    if (!data?.logs) return [];
    if (!filterText.trim()) return data.logs;
    const lower = filterText.toLowerCase();
    return data.logs.filter((e: GlueLogEvent) => e.message.toLowerCase().includes(lower));
  }, [data?.logs, filterText]);

  const runOptions = runs.map((r) => ({
    value: r.run_id,
    label: `${r.run_id} — ${r.status}`,
  }));

  return (
    <SpaceBetween size="m" data-testid="glue-job-log-viewer">
      <SpaceBetween direction="horizontal" size="s">
        <Select
          selectedOption={selectedRunId ? { value: selectedRunId, label: runOptions.find((o) => o.value === selectedRunId)?.label ?? selectedRunId } : null}
          onChange={({ detail }) => setSelectedRunId(detail.selectedOption.value ?? undefined)}
          options={runOptions}
          placeholder="Select a run"
          data-testid="log-run-select"
        />
        <SegmentedControl
          selectedId={logType}
          onChange={({ detail }) => setLogType(detail.selectedId)}
          options={[
            { id: 'output', text: 'Output' },
            { id: 'error', text: 'Error' },
          ]}
          data-testid="log-type-toggle"
        />
      </SpaceBetween>

      <TextFilter
        filteringText={filterText}
        filteringPlaceholder="Filter log lines by keyword"
        onChange={({ detail }) => setFilterText(detail.filteringText)}
        data-testid="log-keyword-filter"
      />

      {isLoading && (
        <Box textAlign="center" padding="l" data-testid="log-loading">
          <StatusIndicator type="loading">Loading logs...</StatusIndicator>
        </Box>
      )}

      {isError && (
        <Box textAlign="center" padding="l" data-testid="log-error">
          <SpaceBetween size="s">
            <StatusIndicator type="error">Failed to load logs</StatusIndicator>
            <Button onClick={() => refetch()} data-testid="log-retry-button">Retry</Button>
          </SpaceBetween>
        </Box>
      )}

      {!isLoading && !isError && filteredLogs.length === 0 && (
        <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="log-empty">
          No logs available for this run.
        </Box>
      )}

      {!isLoading && !isError && filteredLogs.length > 0 && (
        <pre
          data-testid="log-output"
          style={{
            background: colorBackgroundContainerContent,
            color: colorTextBodyDefault,
            border: `1px solid ${colorBorderDividerDefault}`,
            padding: 16,
            borderRadius: 4,
            fontSize: 13,
            overflow: 'auto',
            maxHeight: 500,
            whiteSpace: 'pre-wrap',
            margin: 0,
          }}
        >
          {filteredLogs.map((event: GlueLogEvent, i: number) => (
            <div key={i} data-testid="log-line" data-level={event.level} style={{ color: logLineColor(event.level) }}>
              <span data-testid="log-timestamp">{event.timestamp}</span>{' '}
              <span data-testid="log-level">[{event.level}]</span>{' '}
              <span data-testid="log-message">{event.message}</span>
            </div>
          ))}
        </pre>
      )}

      {isRunning && !isError && (
        <Box color="text-body-secondary" fontSize="body-s" data-testid="log-auto-refresh">
          Auto-refreshing every 5 seconds
        </Box>
      )}
    </SpaceBetween>
  );
}
