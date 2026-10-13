// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepModel } from '../WizardStepModel';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

vi.mock('../../../hooks', () => ({
  useBedrockModels: () => ({
    data: [
      { model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' },
      { model_id: 'amazon.nova-pro-v1:0', model_name: 'Amazon Nova Pro' },
    ],
    isLoading: false,
  }),
}));

function renderStep(overrides: Partial<WizardState> = {}) {
  const state = { ...INITIAL_WIZARD_STATE, ...overrides };
  const onChange = vi.fn();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <WizardStepModel state={state} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { state, onChange };
}

describe('WizardStepModel', () => {
  it('renders AI model selector', () => {
    renderStep();
    expect(screen.getByText('AI Model')).toBeInTheDocument();
  });

  it('renders approval gate toggles', () => {
    renderStep();
    expect(screen.getByText('Require approval for schema mapping')).toBeInTheDocument();
    expect(screen.getByText('Require approval for code generation')).toBeInTheDocument();
    // Was "Auto-approve schema changes", which stated the opposite of what
    // `alwaysApproveSchemaChanges: true` does (approval IS required). Relabel only —
    // the field, the wire key and the `auto-approve-toggle` test id are unchanged
    // (p63 R2.9 forbids inverting the stored polarity).
    expect(screen.getByText('Require approval for schema changes')).toBeInTheDocument();
    expect(screen.queryByText('Auto-approve schema changes')).not.toBeInTheDocument();
  });

  it('renders confidence threshold slider', () => {
    renderStep();
    expect(screen.getByText('Confidence Threshold')).toBeInTheDocument();
    expect(screen.getByText(/80%/)).toBeInTheDocument();
  });

  it('toggles schema mapping approval', () => {
    const { onChange } = renderStep();
    // Cloudscape Toggle renders a checkbox input inside the label
    const toggles = screen.getAllByRole('checkbox');
    // First toggle is schema mapping
    fireEvent.click(toggles[0]);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        approvalConfig: expect.objectContaining({ schemaMappingEnabled: false }),
      }),
    );
  });

  it('toggles code generation approval', () => {
    const { onChange } = renderStep();
    const toggles = screen.getAllByRole('checkbox');
    // Second toggle is code generation
    fireEvent.click(toggles[1]);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        approvalConfig: expect.objectContaining({ codeGenerationEnabled: false }),
      }),
    );
  });
});
