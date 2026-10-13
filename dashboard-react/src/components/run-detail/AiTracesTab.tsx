// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import TokenUsageSummary from '../TokenUsageSummary';
import AgentTraceTimeline from '../AgentTraceTimeline';
import { normalizeStepName } from '../../utils/step-names';
import type { AgentTrace, AgentTraceListResponse, StepDetail } from '../../types';

interface AiTracesTabProps {
  tracesData?: AgentTraceListResponse | null;
  tracesLoading?: boolean;
  tracesError?: boolean;
  onRetryTraces: () => void;
  steps?: StepDetail[];
  tracesByStep: Map<string, AgentTrace>;
}

/**
 * AI Traces tab content extracted from RunDetailPage (P29 R11.5).
 * Preserves the Async_State_Triple (loading skeleton via TokenUsageSummary,
 * error + Retry, and genuinely-empty copy) exactly as before.
 */
export default function AiTracesTab({
  tracesData,
  tracesLoading,
  tracesError,
  onRetryTraces,
  steps,
  tracesByStep,
}: AiTracesTabProps) {
  return (
    <SpaceBetween size="l">
      <TokenUsageSummary traces={tracesData?.traces} isLoading={tracesLoading} />
      {(steps ?? []).map((step) => {
        const traceRecord = tracesByStep.get(normalizeStepName(step.step_name));
        if (!traceRecord) return null;
        return (
          <AgentTraceTimeline
            key={step.step_name}
            stepName={step.step_name}
            trace={traceRecord.trace}
          />
        );
      })}
      {!tracesLoading && tracesError && (
        <Alert
          type="error"
          data-testid="traces-error"
          action={<Button data-testid="traces-retry" onClick={() => onRetryTraces()}>Retry</Button>}
        >
          Failed to load AI traces for this run.
        </Alert>
      )}
      {!tracesLoading && !tracesError && (!tracesData?.traces || tracesData.traces.length === 0) && (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No AI traces available for this run.
        </Box>
      )}
    </SpaceBetween>
  );
}
