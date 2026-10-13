// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p64 task 2c.2 — the Model step's Quality_Config controls.
 *
 * Validates: Requirements R7.1, R7.2, R7.3, R7.5 (and R6.6 for the three-state
 * data-quality control).
 *
 * A separate file from `WizardStepModel.test.tsx` because that file's toggle tests
 * address controls by index into `getAllByRole('checkbox')`, and adding a fourth
 * toggle to the same document is exactly the sort of change that silently
 * repurposes an index. The controls here are addressed by `data-testid`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepModel } from '../WizardStepModel';
import { INITIAL_WIZARD_STATE } from '../types';
import type { QualityConfig, WizardState } from '../types';

vi.mock('../../../hooks', () => ({
  useBedrockModels: () => ({
    data: [{ model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' }],
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

/** The `qualityConfig` the component was asked to write, from the last onChange. */
function writtenQuality(onChange: ReturnType<typeof vi.fn>): QualityConfig {
  const last = onChange.mock.calls.at(-1)?.[0] as WizardState;
  return last.qualityConfig;
}

describe('WizardStepModel — Data Quality section', () => {
  it('renders the section with its three controls reachable', () => {
    renderStep({ qualityConfig: { dedupEnabled: true, dedupColumns: [] } });

    expect(screen.getByText('Data Quality')).toBeInTheDocument();
    expect(screen.getByTestId('data-quality-select')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-enabled-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-columns-input')).toBeInTheDocument();
  });

  it('leaves the existing approval-gate labels alone', () => {
    // This test's original point was that the DQ additions must not disturb the
    // neighbouring approval labels, and that still holds. What changed is the third
    // one: the "Auto-approve schema changes" mislabel it used to pin as a known
    // residual has since been fixed DELIBERATELY, in its own change — the label now
    // states what `alwaysApproveSchemaChanges: true` actually does (approval IS
    // required). p63 R2/D3 still owns the RENAME half, and p63 R2.9 still forbids
    // inverting the stored polarity; neither was touched.
    renderStep();

    expect(screen.getByText('Require approval for schema mapping')).toBeInTheDocument();
    expect(screen.getByText('Require approval for code generation')).toBeInTheDocument();
    expect(screen.getByText('Require approval for schema changes')).toBeInTheDocument();
  });

  it('hides the column field until dedup is switched on', () => {
    // R7.1's "default to off" made visible: the columns only mean anything once
    // dedup is on, and a field that cannot affect anything is an inert control.
    renderStep();

    expect(screen.queryByTestId('dedup-columns-input')).not.toBeInTheDocument();
  });

  it('turns dedup on without touching the data-quality gate', () => {
    const { onChange } = renderStep();

    fireEvent.click(screen.getByTestId('dedup-enabled-toggle').querySelector('input')!);

    const written = writtenQuality(onChange);
    expect(written.dedupEnabled).toBe(true);
    // The two settings are independent, and the gate's absent state must survive an
    // edit to its neighbour — otherwise flipping dedup would opt an S3 Tables
    // pipeline out of data quality as a side effect.
    expect('dataQualityEnabled' in written).toBe(false);
  });

  it('parses the column field into a list', () => {
    const { onChange } = renderStep({ qualityConfig: { dedupEnabled: true, dedupColumns: [] } });

    fireEvent.change(screen.getByTestId('dedup-columns-input').querySelector('input')!, {
      target: { value: 'patient_id, visit_date' },
    });

    expect(writtenQuality(onChange).dedupColumns).toEqual(['patient_id', 'visit_date']);
  });

  it('shows the offending column name inline rather than filtering it away', () => {
    // `_validate_quality_config` refuses an offending entry with a 400 and does not
    // drop it, so the operator has to be told which name and why — on the step that
    // owns the field, not as a failed Create.
    renderStep({ qualityConfig: { dedupEnabled: true, dedupColumns: ['ok', 'not-ok'] } });

    expect(screen.getByText(/not-ok/)).toBeInTheDocument();
  });

  it('does not complain about an empty column list', () => {
    // R7.2: empty MEANS every mapped target column, and is the default.
    renderStep({ qualityConfig: { dedupEnabled: true, dedupColumns: [] } });

    expect(screen.queryByText(/not a usable column name/)).not.toBeInTheDocument();
  });

  it('survives a draft-restored undefined qualityConfig (R7.5)', () => {
    // `handleRestoreDraft` replaces state rather than merging into it, so a draft
    // saved before this field existed restores it as `undefined` despite the type.
    // Reading `.dedupEnabled` off that would throw on render — a blank step, not a
    // degraded one.
    expect(() =>
      renderStep({ qualityConfig: undefined as unknown as QualityConfig }),
    ).not.toThrow();
    expect(screen.getByTestId('dedup-enabled-toggle')).toBeInTheDocument();
  });
});

describe('WizardStepModel — the data-quality gate is three-state', () => {
  it('shows "Template default" when nobody has expressed an opinion', () => {
    renderStep();

    // Not "Off". The default is per template — on for S3 Table Buckets, off for the
    // other four — so rendering it as "Off" would state the opposite of the truth
    // for the wizard's own default target type.
    expect(screen.getByTestId('data-quality-select').textContent).toContain('Template default');
  });

  it('reflects an explicit opt-in as On, not as the default', () => {
    renderStep({
      qualityConfig: { dataQualityEnabled: true, dedupEnabled: false, dedupColumns: [] },
    });

    const shown = screen.getByTestId('data-quality-select').textContent ?? '';
    expect(shown).not.toContain('Template default');
    expect(shown).toContain('On');
  });

  it('reflects an explicit opt-out as Off, not as the default', () => {
    // The state a Toggle would be unable to tell apart from "no opinion", and the
    // one whose conflation with absence changes a pipeline's verdict.
    renderStep({
      qualityConfig: { dataQualityEnabled: false, dedupEnabled: false, dedupColumns: [] },
    });

    const shown = screen.getByTestId('data-quality-select').textContent ?? '';
    expect(shown).not.toContain('Template default');
    expect(shown).toContain('Off');
  });
});
