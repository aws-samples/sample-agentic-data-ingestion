// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Popover from '@cloudscape-design/components/popover';
import SpaceBetween from '@cloudscape-design/components/space-between';
import type { AgentTrace } from '../types';

interface ModelStats {
  model_id: string;
  display_name: string;
  input_tokens: number;
  output_tokens: number;
  invocations: number;
  cost: number;
}

interface TokenUsageSummaryProps {
  traces: AgentTrace[] | undefined | null;
  isLoading?: boolean;
}

export default function TokenUsageSummary({ traces, isLoading }: TokenUsageSummaryProps) {
  if (isLoading) {
    return <Spinner size="normal" />;
  }

  if (!traces || traces.length === 0) {
    return null;
  }

  let totalInput = 0;
  let totalOutput = 0;
  let totalCacheRead = 0;
  let totalCost = 0;
  const modelIds = new Set<string>();

  for (const t of traces) {
    totalInput += Number(t.trace.total_input_tokens) || 0;
    totalOutput += Number(t.trace.total_output_tokens) || 0;
    totalCacheRead += Number(t.trace.total_cache_read_tokens) || 0;
    totalCost += Number(t.trace.estimated_cost_usd) || 0;
    if (t.trace.model_id) modelIds.add(t.trace.model_id);
    for (const mc of t.trace.model_calls) {
      if (mc.model_id) modelIds.add(mc.model_id);
    }
  }

  const totalTokens = totalInput + totalOutput;

  // No meaningful data captured yet — show placeholder
  if (totalTokens === 0 && totalCost === 0) {
    return (
      <div data-testid="token-usage-summary">
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">AI Token Usage</Box>
            <Box color="text-body-secondary">Awaiting trace data</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Estimated Cost</Box>
            <Box color="text-body-secondary">—</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Model</Box>
            <Box color="text-body-secondary">{modelIds.size === 1 ? [...modelIds][0].split('.').pop()?.replace(/-v\d.*$/, '') ?? '—' : '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Cache Hit</Box>
            <Box color="text-body-secondary">—</Box>
          </div>
        </ColumnLayout>
      </div>
    );
  }

  const cacheHitRate = totalInput > 0
    ? Math.round((totalCacheRead / totalInput) * 100)
    : 0;
  const modelLabel = modelIds.size === 1
    ? [...modelIds][0].split('.').pop()?.replace(/-v\d.*$/, '') ?? 'Unknown'
    : `${modelIds.size} models`;

  // Build per-model stats for the popover
  const modelStatsMap = new Map<string, ModelStats>();
  for (const t of traces) {
    const mid = t.trace.model_id || 'unknown';
    const displayName = mid.split('.').pop()?.replace(/-v\d.*$/, '') ?? mid;
    if (!modelStatsMap.has(mid)) {
      modelStatsMap.set(mid, { model_id: mid, display_name: displayName, input_tokens: 0, output_tokens: 0, invocations: 0, cost: 0 });
    }
    const stats = modelStatsMap.get(mid)!;
    stats.input_tokens += Number(t.trace.total_input_tokens) || 0;
    stats.output_tokens += Number(t.trace.total_output_tokens) || 0;
    stats.cost += Number(t.trace.estimated_cost_usd) || 0;
    stats.invocations += 1;
  }
  const modelStatsList = [...modelStatsMap.values()].sort((a, b) => (b.input_tokens + b.output_tokens) - (a.input_tokens + a.output_tokens));

  return (
    <div data-testid="token-usage-summary">
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">AI Token Usage</Box>
          <Box>
            <div>
              <StatusIndicator type="info">
                {totalTokens.toLocaleString()} tokens
              </StatusIndicator>
            </div>
            <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>
              {totalInput.toLocaleString()} in / {totalOutput.toLocaleString()} out
            </Box>
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Estimated Cost</Box>
          <Box>${totalCost.toFixed(4)}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Model</Box>
          <Popover
            header="Models Used"
            size="large"
            triggerType="custom"
            content={
              <SpaceBetween size="s">
                {modelStatsList.map((m) => (
                  <div key={m.model_id}>
                    <Box variant="h4">{m.display_name}</Box>
                    <Box variant="small" color="text-body-secondary">
                      {m.input_tokens.toLocaleString()} in / {m.output_tokens.toLocaleString()} out
                      {' · '}{m.invocations} invocation{m.invocations !== 1 ? 's' : ''}
                      {' · '}${m.cost.toFixed(4)}
                    </Box>
                    <Box variant="small" color="text-body-secondary">
                      {m.model_id}
                    </Box>
                  </div>
                ))}
              </SpaceBetween>
            }
          >
            <span style={{ cursor: 'pointer', textDecoration: 'underline dotted' }}>
              {modelLabel}
            </span>
          </Popover>
        </div>
        <div>
          <Box variant="awsui-key-label">Cache Hit</Box>
          <Box>
            {cacheHitRate}%
            {totalCacheRead > 0 && (
              <Box variant="small" color="text-body-secondary">
                {totalCacheRead.toLocaleString()} tokens from cache
              </Box>
            )}
          </Box>
        </div>
      </ColumnLayout>
    </div>
  );
}
