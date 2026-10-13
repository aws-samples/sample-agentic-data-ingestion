// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WizardStepName, validatePipelineName } from '../WizardStepName';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

function renderStep(
  overrides: Partial<WizardState> = {},
  props: { existingNames?: string[]; showRequiredError?: boolean } = {},
) {
  const state = { ...INITIAL_WIZARD_STATE, ...overrides };
  const onChange = vi.fn();
  render(<WizardStepName state={state} onChange={onChange} {...props} />);
  return { state, onChange };
}

describe('WizardStepName', () => {
  it('renders pipeline name and description fields', () => {
    renderStep();
    expect(screen.getByText('Pipeline Name')).toBeInTheDocument();
    expect(screen.getByText('Description')).toBeInTheDocument();
  });

  it('shows validation error for short name', () => {
    renderStep({ pipelineName: 'ab' });
    expect(screen.getByText(/at least 3 characters/)).toBeInTheDocument();
  });

  it('shows validation error for invalid characters', () => {
    renderStep({ pipelineName: 'my pipeline!' });
    expect(screen.getByText(/letters, numbers, hyphens/)).toBeInTheDocument();
  });

  it('does not show error for valid name', () => {
    renderStep({ pipelineName: 'my-valid-pipeline' });
    expect(screen.queryByText(/required/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/at least/i)).not.toBeInTheDocument();
  });

  it('calls onChange when name is typed', () => {
    const { onChange } = renderStep();
    const input = screen.getByPlaceholderText('my-etl-pipeline');
    fireEvent.change(input, { target: { value: 'new-name' } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ pipelineName: 'new-name' }),
    );
  });

  it('calls onChange when description is typed', () => {
    const { onChange } = renderStep();
    const textarea = screen.getByPlaceholderText("Describe this pipeline's purpose");
    fireEvent.change(textarea, { target: { value: 'A test description' } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ description: 'A test description' }),
    );
  });

  // P29 R5.1 — inline error is forced onto an empty field once the user has
  // attempted Next (showRequiredError), and duplicate names surface inline.
  it('shows the required error on an empty name when showRequiredError is set', () => {
    renderStep({ pipelineName: '' }, { showRequiredError: true });
    expect(screen.getByText('Pipeline name is required')).toBeInTheDocument();
  });

  it('does not show the required error on an empty name before a Next attempt', () => {
    renderStep({ pipelineName: '' });
    expect(screen.queryByText('Pipeline name is required')).not.toBeInTheDocument();
  });

  it('shows a duplicate-name error inline (case-insensitive)', () => {
    renderStep({ pipelineName: 'Existing-Pipeline' }, { existingNames: ['existing-pipeline'] });
    expect(screen.getByText(/already exists/i)).toBeInTheDocument();
  });
});

describe('validatePipelineName (P29 R5.1)', () => {
  it('flags an empty/whitespace name as required', () => {
    expect(validatePipelineName('')).toBe('Pipeline name is required');
    expect(validatePipelineName('   ')).toBe('Pipeline name is required');
  });

  it('accepts a valid, unique name', () => {
    expect(validatePipelineName('my-pipeline', ['other-pipeline'])).toBeUndefined();
  });

  it('flags a case-insensitive duplicate', () => {
    expect(validatePipelineName('My-Pipeline', ['my-pipeline'])).toMatch(/already exists/i);
  });

  it('does not flag a name absent from the existing list', () => {
    expect(validatePipelineName('unique-name', ['a', 'b', 'c'])).toBeUndefined();
  });

  it('applies format rules before duplicate detection', () => {
    expect(validatePipelineName('ab', ['ab'])).toBe('Name must be at least 3 characters');
  });
});
