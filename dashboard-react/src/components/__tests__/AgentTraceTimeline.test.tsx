// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AgentTraceTimeline from '../AgentTraceTimeline';
import type { AgentTraceData } from '../../types';

// NOTE: model IDs here are illustrative sample trace data, not a primary-model
// selection. The canonical primary model is `us.anthropic.claude-sonnet-4-6`
// (P21 Req 1); this fixture intentionally retains a historical Claude Sonnet 4.5
// ID to exercise the timeline component's rendering.
const MOCK_TRACE: AgentTraceData = {
  model_calls: [
    {
      model_id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
      input_tokens: 1250,
      output_tokens: 340,
      cache_read_tokens: 800,
      duration_ms: 2340,
      prompt_preview: 'Generate PySpark transform block for...',
      response_preview: 'df_transformed = df.select(...',
      start_time: 1710500000.0,
      end_time: 1710500002.34,
    },
    {
      model_id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
      input_tokens: 890,
      output_tokens: 120,
      cache_read_tokens: 400,
      duration_ms: 1700,
      prompt_preview: 'Verify the generated code...',
      response_preview: 'The code looks correct...',
      start_time: 1710500003.5,
      end_time: 1710500005.2,
    },
  ],
  tool_calls: [
    {
      tool_name: 'create_glue_job',
      tool_params: '{"job_name": "agentic-data-ingestion-pipe-123-etl"}',
      result_preview: '{"job_name": "agentic-data-ingestion-pipe-123-etl"}',
      duration_ms: 1200,
      status: 'success',
      start_time: 1710500002.5,
      end_time: 1710500003.7,
    },
  ],
  total_input_tokens: 2140,
  total_output_tokens: 460,
  total_cache_read_tokens: 1200,
  estimated_cost_usd: 0.0132,
  start_time: 1710500000.0,
  end_time: 1710500005.2,
  model_id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
  error: null,
};

const EMPTY_TRACE: AgentTraceData = {
  model_calls: [],
  tool_calls: [],
  total_input_tokens: 0,
  total_output_tokens: 0,
  total_cache_read_tokens: 0,
  estimated_cost_usd: 0,
  start_time: null,
  end_time: null,
  model_id: '',
  error: null,
};

describe('AgentTraceTimeline', () => {
  it('renders summary bar with total duration, tokens, and cost', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={MOCK_TRACE} defaultExpanded />);

    const summary = screen.getByTestId('trace-summary');
    expect(summary).toBeInTheDocument();
    // Total tokens: 2140 + 460 = 2600 → "2.6k tokens"
    expect(summary.textContent).toContain('2.6k tokens');
    expect(summary.textContent).toContain('$0.0132');
  });

  it('renders model calls with token counts and duration', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={MOCK_TRACE} defaultExpanded />);

    const modelCalls = screen.getAllByTestId('trace-model-call');
    expect(modelCalls).toHaveLength(2);

    // First model call shows input/output tokens
    expect(modelCalls[0].textContent).toContain('1,250');
    expect(modelCalls[0].textContent).toContain('340');
  });

  it('renders tool calls with name, status, and duration', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={MOCK_TRACE} defaultExpanded />);

    const toolCalls = screen.getAllByTestId('trace-tool-call');
    expect(toolCalls).toHaveLength(1);
    expect(toolCalls[0].textContent).toContain('create_glue_job');
  });

  it('orders timeline entries chronologically (model → tool → model)', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={MOCK_TRACE} defaultExpanded />);

    const allEntries = screen.getAllByTestId(/^trace-(model|tool)-call$/);
    expect(allEntries).toHaveLength(3);
    // First: model call (start_time 1710500000)
    expect(allEntries[0].getAttribute('data-testid')).toBe('trace-model-call');
    // Second: tool call (start_time 1710500002.5)
    expect(allEntries[1].getAttribute('data-testid')).toBe('trace-tool-call');
    // Third: model call (start_time 1710500003.5)
    expect(allEntries[2].getAttribute('data-testid')).toBe('trace-model-call');
  });

  it('shows empty state when no trace events exist', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={EMPTY_TRACE} defaultExpanded />);

    expect(screen.getByTestId('trace-empty')).toBeInTheDocument();
    expect(screen.getByText('No trace events recorded')).toBeInTheDocument();
  });

  it('opens prompt/response modal when button is clicked', () => {
    render(<AgentTraceTimeline stepName="code_generation" trace={MOCK_TRACE} defaultExpanded />);

    const buttons = screen.getAllByTestId('show-prompt-response');
    fireEvent.click(buttons[0]);

    expect(screen.getByTestId('prompt-content')).toBeInTheDocument();
    expect(screen.getByTestId('prompt-content').textContent).toContain('Generate PySpark');
  });

  it('formats step name as title case in header', () => {
    render(<AgentTraceTimeline stepName="schema_mapping" trace={MOCK_TRACE} defaultExpanded />);

    expect(screen.getByText('AI Trace: Schema Mapping')).toBeInTheDocument();
  });

  it('shows error indicator when trace has an error', () => {
    const errorTrace: AgentTraceData = {
      ...MOCK_TRACE,
      error: 'Model invocation failed: throttling',
    };
    render(<AgentTraceTimeline stepName="code_generation" trace={errorTrace} defaultExpanded />);

    expect(screen.getByText('Model invocation failed: throttling')).toBeInTheDocument();
  });
});
