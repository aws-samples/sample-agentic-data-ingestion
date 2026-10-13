// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * P29 R11.6 — AI-model dropdown honest async states.
 *
 * FALLBACK_BEDROCK_MODELS was deleted: the model list comes solely from the API.
 * When the models fetch fails, the AI-model Select must render the
 * Async_State_Triple inline (statusType="error", an error message, and a Retry
 * recovery link that re-triggers the query) — NOT a silently-stale hardcoded
 * model list. On success, the API-provided models render as options.
 *
 * Validates: Requirements 11.6
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { WizardStepModel } from '../WizardStepModel';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

// Reconfigurable mock for the models fetcher hook.
const useBedrockModelsMock = vi.fn();
const refetchModels = vi.fn();

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useBedrockModels: () => useBedrockModelsMock(),
  };
});

// Names that used to live in the deleted FALLBACK_BEDROCK_MODELS list. None of
// these should ever appear when the API fails — that would be the old
// silently-stale hardcoded fallback we removed.
const FORMER_FALLBACK_NAMES = [
  'Claude Sonnet 4',
  'Claude 3.5 Sonnet v2',
  'Amazon Nova Pro',
  'Amazon Titan Text Express',
];

function renderStep(overrides: Partial<WizardState> = {}) {
  const state = { ...INITIAL_WIZARD_STATE, ...overrides };
  const onChange = vi.fn();
  const { container } = render(<WizardStepModel state={state} onChange={onChange} />);
  return { state, onChange, container };
}

describe('WizardStepModel — AI-model dropdown honest async states (P29 R11.6)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders an inline error + Retry when the models fetch fails (no hardcoded list)', () => {
    useBedrockModelsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('boom'),
      refetch: refetchModels,
    });

    const { container } = renderStep();
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="ai-model-select"]')!;
    expect(select).not.toBeNull();

    // Opening the dropdown surfaces the error copy + a recovery (Retry) link
    // rather than a stale hardcoded model list.
    select.openDropdown();
    expect(screen.getByText(/Couldn't load models/)).toBeInTheDocument();
    expect(select.findErrorRecoveryButton()).not.toBeNull();

    // The deleted FALLBACK_BEDROCK_MODELS names must NOT leak into the UI.
    for (const name of FORMER_FALLBACK_NAMES) {
      expect(screen.queryByText(name)).not.toBeInTheDocument();
    }
    // No options are rendered on failure (empty option list, not a fallback list).
    expect(select.findDropdown().findOptions()).toHaveLength(0);
  });

  it('Retry recovery re-triggers the query', () => {
    useBedrockModelsMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('boom'),
      refetch: refetchModels,
    });

    const { container } = renderStep();
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="ai-model-select"]')!;
    select.openDropdown();

    const recovery = select.findErrorRecoveryButton()!;
    fireEvent.click(recovery.getElement());

    expect(refetchModels).toHaveBeenCalled();
  });

  it('renders the API-provided models as options on success', () => {
    useBedrockModelsMock.mockReturnValue({
      data: [
        { model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'API Haiku' },
        { model_id: 'amazon.nova-lite-v1:0', model_name: 'API Nova Lite' },
      ],
      isLoading: false,
      isError: false,
      refetch: refetchModels,
    });

    const { container } = renderStep();
    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="ai-model-select"]')!;
    select.openDropdown();

    // Healthy path: no error recovery link, options come from the API payload.
    expect(select.findErrorRecoveryButton()).toBeNull();
    const options = select.findDropdown().findOptions();
    expect(options).toHaveLength(2);
    const text = options.map((o) => o.getElement().textContent).join('|');
    expect(text).toContain('API Haiku');
    expect(text).toContain('API Nova Lite');
  });
});
