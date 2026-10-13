// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import { colorBorderStatusInfo, colorBorderStatusSuccess } from '@cloudscape-design/design-tokens';
import PromptResponseModal from './PromptResponseModal';
import type { AgentTraceData, AgentModelCall, AgentToolCall } from '../types';

export interface AgentTraceTimelineProps {
  stepName: string;
  trace: AgentTraceData;
  defaultExpanded?: boolean;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatCost(usd: number | string): string {
  return `$${Number(usd).toFixed(4)}`;
}

function formatStepLabel(stepName: string): string {
  return stepName.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Merge model calls and tool calls into a single chronological timeline. */
function buildTimeline(trace: AgentTraceData): Array<{ type: 'model' | 'tool'; item: AgentModelCall | AgentToolCall }> {
  const entries: Array<{ type: 'model' | 'tool'; item: AgentModelCall | AgentToolCall; time: number }> = [];
  for (const mc of trace.model_calls) {
    entries.push({ type: 'model', item: mc, time: mc.start_time });
  }
  for (const tc of trace.tool_calls) {
    entries.push({ type: 'tool', item: tc, time: tc.start_time });
  }
  entries.sort((a, b) => a.time - b.time);
  return entries.map(({ type, item }) => ({ type, item }));
}

export default function AgentTraceTimeline({ stepName, trace, defaultExpanded = false }: AgentTraceTimelineProps) {
  const [modalCall, setModalCall] = useState<AgentModelCall | null>(null);
  const timeline = buildTimeline(trace);
  const totalDuration = trace.start_time && trace.end_time
    ? (trace.end_time - trace.start_time) * 1000
    : 0;
  const totalTokens = Number(trace.total_input_tokens) + Number(trace.total_output_tokens);

  return (
    <ExpandableSection
      defaultExpanded={defaultExpanded}
      headerText={`AI Trace: ${formatStepLabel(stepName)}`}
      data-testid={`agent-trace-${stepName}`}
    >
      <SpaceBetween size="s">
        {/* Summary bar */}
        <div data-testid="trace-summary" style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'center' }}>
          {totalDuration > 0 && <Badge>⏱ {formatDuration(totalDuration)}</Badge>}
          <Badge>🔤 {formatTokens(totalTokens)} tokens</Badge>
          <Badge>💰 {formatCost(trace.estimated_cost_usd)}</Badge>
          {trace.error && <StatusIndicator type="error">{trace.error}</StatusIndicator>}
        </div>

        {/* Timeline entries */}
        {timeline.length === 0 && (
          <Box color="text-body-secondary" data-testid="trace-empty">No trace events recorded</Box>
        )}

        {timeline.map((entry, idx) => {
          if (entry.type === 'model') {
            const mc = entry.item as AgentModelCall;
            return (
              <div
                key={`model-${idx}`}
                data-testid="trace-model-call"
                style={{ borderLeft: `3px solid ${colorBorderStatusInfo}`, paddingLeft: 12, marginLeft: 4 }}
              >
                <SpaceBetween size="xxs">
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <StatusIndicator type="info">Model Call</StatusIndicator>
                    <Badge>{formatDuration(mc.duration_ms)}</Badge>
                  </div>
                  <ColumnLayout columns={3} variant="text-grid">
                    <div>
                      <Box variant="awsui-key-label">Model</Box>
                      <Box variant="small">{mc.model_id || 'Unknown'}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Input Tokens</Box>
                      <Box variant="small">{mc.input_tokens.toLocaleString()}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">Output Tokens</Box>
                      <Box variant="small">{mc.output_tokens.toLocaleString()}</Box>
                    </div>
                  </ColumnLayout>
                  <Button
                    variant="inline-link"
                    onClick={() => setModalCall(mc)}
                    data-testid="show-prompt-response"
                  >
                    Show Prompt &amp; Response
                  </Button>
                </SpaceBetween>
              </div>
            );
          }

          const tc = entry.item as AgentToolCall;
          return (
            <div
              key={`tool-${idx}`}
              data-testid="trace-tool-call"
              style={{ borderLeft: `3px solid ${colorBorderStatusSuccess}`, paddingLeft: 12, marginLeft: 4 }}
            >
              <SpaceBetween size="xxs">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <StatusIndicator type={tc.status === 'success' ? 'success' : 'error'}>
                    Tool: {tc.tool_name}
                  </StatusIndicator>
                  <Badge>{formatDuration(tc.duration_ms)}</Badge>
                </div>
                {tc.tool_params && (
                  <Box variant="small">
                    <Box variant="awsui-key-label">Params</Box>
                    <code style={{ fontSize: 12 }}>{tc.tool_params}</code>
                  </Box>
                )}
                {tc.result_preview && (
                  <Box variant="small">
                    <Box variant="awsui-key-label">Result</Box>
                    <code style={{ fontSize: 12 }}>{tc.result_preview}</code>
                  </Box>
                )}
              </SpaceBetween>
            </div>
          );
        })}
      </SpaceBetween>

      {/* Prompt/Response Modal */}
      <PromptResponseModal
        visible={modalCall !== null}
        onDismiss={() => setModalCall(null)}
        promptPreview={modalCall?.prompt_preview ?? ''}
        responsePreview={modalCall?.response_preview ?? ''}
        modelId={modalCall?.model_id}
      />
    </ExpandableSection>
  );
}
