// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepSchema } from '../WizardStepSchema';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

vi.mock('../../../hooks', () => ({
  useSchemas: () => ({
    data: [
      {
        schema_id: 's1',
        schema_name: 'Test Schema',
        source_type: 'uploaded',
        fields: [{ field_name: 'id', field_type: 'int', description: 'Primary key' }],
        created_at: '2024-01-01',
      },
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
      <WizardStepSchema state={state} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { state, onChange };
}

describe('WizardStepSchema', () => {
  it('renders schema source tiles', () => {
    renderStep();
    expect(screen.getByText('Schema Source')).toBeInTheDocument();
    expect(screen.getByText('Auto-detected')).toBeInTheDocument();
    expect(screen.getByText('Upload')).toBeInTheDocument();
    expect(screen.getByText('Existing')).toBeInTheDocument();
  });

  it('shows info message when no detected schema', () => {
    renderStep({ schemaSource: 'detected', detectedTargetSchema: null });
    expect(screen.getByText(/No schema was detected/)).toBeInTheDocument();
  });

  it('shows detected schema fields when available', () => {
    renderStep({
      schemaSource: 'detected',
      detectedTargetSchema: [
        { name: 'user_id', type: 'bigint', nullable: false, description: 'User ID' },
        { name: 'email', type: 'string', nullable: true, description: 'Email address' },
      ],
    });
    expect(screen.getByText('user_id')).toBeInTheDocument();
    expect(screen.getByText('email')).toBeInTheDocument();
    expect(screen.getByText(/2 fields/)).toBeInTheDocument();
  });

  it('shows upload wizard when upload source selected', () => {
    renderStep({ schemaSource: 'upload' });
    expect(screen.getByTestId('schema-file-upload')).toBeInTheDocument();
  });

  it('shows existing schema selector when existing source selected', () => {
    renderStep({ schemaSource: 'existing' });
    expect(screen.getByText('Select an existing schema')).toBeInTheDocument();
  });
});

// P19 Task 10.10 — extension: regression tests for the existing three modes
// under the extended `schemaSource` type union (Req 8.1) plus the
// `INITIAL_WIZARD_STATE.schemaSource === 'detected'` invariant (Req 8.2).
describe('WizardStepSchema (P19 Task 10.10 regressions)', () => {
  it('INITIAL_WIZARD_STATE.schemaSource is "detected" (Req 8.2)', () => {
    // Pre-P19 default is preserved so non-JDBC pipelines and pipeline-edit
    // flow loads continue to start with `'detected'`. The Step 3
    // pre-selection effect overrides this for JDBC targets at runtime.
    expect(INITIAL_WIZARD_STATE.schemaSource).toBe('detected');
  });

  it('still renders the auto-detected view for `detected` mode under the extended union', () => {
    renderStep({ schemaSource: 'detected' });
    expect(screen.getByText('Schema Source')).toBeInTheDocument();
    // The detected view either shows the no-schema info alert or a
    // populated preview table. The default state has no detected schema
    // so the info alert is what's rendered.
    expect(screen.getByText(/No schema was detected/)).toBeInTheDocument();
  });

  it('still renders the upload wizard for `upload` mode under the extended union', () => {
    renderStep({ schemaSource: 'upload' });
    expect(screen.getByTestId('schema-file-upload')).toBeInTheDocument();
  });

  it('still renders the existing-schema selector for `existing` mode under the extended union', () => {
    renderStep({ schemaSource: 'existing' });
    expect(screen.getByText('Select an existing schema')).toBeInTheDocument();
  });
});
