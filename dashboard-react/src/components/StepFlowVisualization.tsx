// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState } from 'react';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Link from '@cloudscape-design/components/link';
import {
  colorBorderStatusSuccess,
  colorBorderStatusError,
  colorBorderStatusInfo,
  colorBorderDividerDefault,
  colorBackgroundContainerContent,
  colorTextBodyDefault,
} from '@cloudscape-design/design-tokens';
import { getStatusIcon, formatStepName, resolveStepStatus } from '../utils/formatting';
import ClickableArea from './common/ClickableArea';
import type { StepDetail, MCPOperation } from '../types';

export interface StepFlowVisualizationProps {
  steps: StepDetail[];
  overallStatus?: string;
  onStepClick?: (stepName: string) => void;
  trigger?: string;
  retrySourceRunId?: string;
  runId?: string;
  pipelineId?: string;
}

/**
 * Render a related-run link only when the id resolves to a DIFFERENT run (P38 R5.3).
 *
 * Under the continuation model a retry executes and reports under the original
 * run's id, so `retry_source_run_id === run_id` and a link built from it would
 * resolve to the page the user is already on. `LGR-453f12d-5` (template /
 * ratified) sets the rule: a forward link renders only from an id that resolves
 * elsewhere, "because rerun_id is lineage ... so linking to it would be a dead link".
 *
 * Exported so the property test asserts against this predicate rather than a copy.
 */
export const linkable = (related?: string, current?: string) => !!related && related !== current;

function statusIndicatorType(status: string): 'success' | 'error' | 'in-progress' | 'pending' | 'info' {
  switch (status?.toLowerCase()) {
    case 'completed': case 'success': case 'succeeded': return 'success';
    case 'failed': case 'error': return 'error';
    case 'running': return 'in-progress';
    case 'pending': case 'skipped': return 'pending';
    default: return 'info';
  }
}

function statusBorderColor(status: string): string {
  switch (status?.toLowerCase()) {
    case 'completed': case 'success': case 'succeeded': return colorBorderStatusSuccess;
    case 'failed': case 'error': return colorBorderStatusError;
    case 'running': return colorBorderStatusInfo;
    case 'pending': return colorBorderDividerDefault;
    default: return colorBorderDividerDefault;
  }
}

export default function StepFlowVisualization({ steps, overallStatus, onStepClick, trigger, retrySourceRunId, runId, pipelineId }: StepFlowVisualizationProps) {
  const [expandedLogs, setExpandedLogs] = useState<Set<string>>(new Set());

  const toggleLogs = (stepName: string) => {
    setExpandedLogs((prev) => {
      const next = new Set(prev);
      if (next.has(stepName)) next.delete(stepName);
      else next.add(stepName);
      return next;
    });
  };

  if (!steps || steps.length === 0) {
    return (
      <Box textAlign="center" color="text-body-secondary" data-testid="step-flow-empty">
        No steps available
      </Box>
    );
  }

  return (
    <div data-testid="step-flow-visualization">
      <SpaceBetween size="xs">
        {steps.map((step, index) => (
          <div key={step.step_name} data-testid="step-card">
            {/* CSS connector between steps */}
            {index > 0 && (
              <div
                data-testid="step-connector"
                style={{
                  width: 2,
                  height: 20,
                  backgroundColor: colorBorderDividerDefault,
                  margin: '0 auto',
                }}
              />
            )}

            <Container
              data-testid={`step-container-${step.step_name}`}
            >
              {(() => {
                const resolved = overallStatus ? resolveStepStatus(step.status, overallStatus) : step.status;
                const isSkipped = trigger === 'retry' && step.status?.toLowerCase() === 'skipped';
                const sections: React.ReactNode[] = [
                  <div key="header" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <span data-testid="step-status-icon">{getStatusIcon(resolved)}</span>
                    <Box variant="h3" data-testid="step-name">
                      {formatStepName(step.step_name)}
                    </Box>
                    {isSkipped ? (
                      <>
                        <StatusIndicator type="info" data-testid="step-status">Skipped</StatusIndicator>
                        {linkable(retrySourceRunId, runId) ? (
                          <Link
                            href={`/pipelines/${pipelineId}/runs/${retrySourceRunId}`}
                            data-testid="skipped-step-source-link"
                          >
                            Result from {retrySourceRunId}
                          </Link>
                        ) : (
                          <Box variant="small" color="text-body-secondary" data-testid="skipped-step-source-note">
                            Result from this run&apos;s earlier attempt
                          </Box>
                        )}
                      </>
                    ) : (
                      <StatusIndicator
                        type={statusIndicatorType(resolved)}
                        data-testid="step-status"
                      >
                        {resolved}
                      </StatusIndicator>
                    )}
                  </div>,
                ];

                if (step.duration_ms != null || step.throughput != null || step.mcp_latency_ms != null) {
                  sections.push(
                    <div key="metrics" data-testid="step-metrics" style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
                      {step.duration_ms != null && <Box variant="small" data-testid="step-duration">Duration: {step.duration_ms}ms</Box>}
                      {step.throughput != null && <Box variant="small" data-testid="step-throughput">Throughput: {step.throughput}/s</Box>}
                      {step.mcp_latency_ms != null && <Box variant="small" data-testid="step-mcp-latency">MCP Latency: {step.mcp_latency_ms}ms</Box>}
                    </div>,
                  );
                }

                if (step.mcp_operations && step.mcp_operations.length > 0) {
                  sections.push(
                    <div key="mcp-ops" data-testid="step-mcp-operations">
                      <Box variant="awsui-key-label">MCP Operations</Box>
                      <SpaceBetween size="xxs">
                        {step.mcp_operations.map((op: MCPOperation, opIdx: number) => (
                          <div key={`${op.operation}-${opIdx}`} data-testid="mcp-operation">
                            <Badge color={op.status === 'completed' || op.status === 'success' ? 'green' : 'grey'}>
                              {op.operation}: {op.status}
                              {op.duration_ms != null && ` (${op.duration_ms}ms)`}
                            </Badge>
                          </div>
                        ))}
                      </SpaceBetween>
                    </div>,
                  );
                }

                if (step.error_details || step.error_message) {
                  sections.push(
                    <div key="error" data-testid="step-error-details">
                      <Box variant="awsui-key-label" color="text-status-error">Error Details</Box>
                      <Box color="text-status-error">{step.error_details || step.error_message}</Box>
                    </div>,
                  );
                }

                if (step.troubleshooting && step.troubleshooting.length > 0) {
                  sections.push(
                    <div key="troubleshooting" data-testid="step-troubleshooting">
                      <Box variant="awsui-key-label">Troubleshooting</Box>
                      <ul style={{ margin: 0, paddingLeft: 20 }}>
                        {step.troubleshooting.map((suggestion, sIdx) => (
                          <li key={sIdx}>{suggestion}</li>
                        ))}
                      </ul>
                    </div>,
                  );
                }

                if (step.logs && step.logs.length > 0) {
                  sections.push(
                    <ExpandableSection
                      key="logs"
                      headerText={isSkipped ? `Original Run Logs (${step.logs.length})` : `Logs (${step.logs.length})`}
                      expanded={expandedLogs.has(step.step_name)}
                      onChange={() => toggleLogs(step.step_name)}
                      data-testid="step-logs-section"
                    >
                      <div data-testid="step-logs">
                        {isSkipped && retrySourceRunId && (
                          <Box variant="small" color="text-body-secondary" padding={{ bottom: 'xs' }} data-testid="skipped-logs-note">
                            Logs from original run {retrySourceRunId}
                          </Box>
                        )}
                        <pre style={{
                          background: colorBackgroundContainerContent,
                          color: colorTextBodyDefault,
                          border: `1px solid ${colorBorderDividerDefault}`,
                          padding: 12,
                          borderRadius: 4,
                          fontSize: 12,
                          maxHeight: 200,
                          overflow: 'auto',
                          whiteSpace: 'pre-wrap',
                        }}>
                          {step.logs.join('\n')}
                        </pre>
                      </div>
                    </ExpandableSection>,
                  );
                }

                return (
              <ClickableArea
                ariaLabel={onStepClick ? `View ${formatStepName(step.step_name)} step details` : undefined}
                onClick={onStepClick ? () => onStepClick(step.step_name) : undefined}
                style={{
                  borderLeft: `4px solid ${statusBorderColor(resolved)}`,
                  paddingLeft: 12,
                }}
              >
                <SpaceBetween size="xs">
                  {sections}
                </SpaceBetween>
              </ClickableArea>
                );
              })()}
            </Container>
          </div>
        ))}
      </SpaceBetween>
    </div>
  );
}
