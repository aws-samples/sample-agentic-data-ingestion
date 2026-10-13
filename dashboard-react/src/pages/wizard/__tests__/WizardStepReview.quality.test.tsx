// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p64 task 2c.2 — the Review step's Data Quality section.
 *
 * Validates: Requirements R7.1, R7.2, R7.5 (and R6.6 for the "Template default"
 * rendering).
 *
 * The section exists so the choice is visible before Create rather than only after
 * a run, which is the same reason the Selected_Worksheet gained a Review row in p55.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WizardStepReview } from '../WizardStepReview';
import { INITIAL_WIZARD_STATE } from '../types';
import type { QualityConfig, WizardState } from '../types';

function renderReview(quality: QualityConfig | undefined) {
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    pipelineName: 'review-quality',
    qualityConfig: quality as QualityConfig,
  };
  render(<WizardStepReview state={state} />);
}

describe('WizardStepReview — Data Quality', () => {
  it('names the template-default state instead of resolving it to On or Off', () => {
    // Which template `create_code` selects is decided after Create, so this page
    // cannot know the resolved value. Guessing "Off" would state the opposite of the
    // truth for the wizard's default S3 Tables target; naming the state is honest.
    renderReview(INITIAL_WIZARD_STATE.qualityConfig);

    expect(screen.getByText('Data Quality')).toBeInTheDocument();
    expect(screen.getByText(/Template default/)).toBeInTheDocument();
  });

  it('shows dedup off with no column row when dedup is off', () => {
    renderReview({ dedupEnabled: false, dedupColumns: [] });

    expect(screen.getByText('Drop Duplicate Rows')).toBeInTheDocument();
    expect(screen.queryByText('Duplicate-identifying Columns')).not.toBeInTheDocument();
  });

  it('lists the configured dedup columns', () => {
    renderReview({ dedupEnabled: true, dedupColumns: ['patient_id', 'visit_date'] });

    expect(screen.getByText('patient_id, visit_date')).toBeInTheDocument();
  });

  it('says "All mapped target columns" for an empty list rather than showing nothing', () => {
    // R7.2: `[]` MEANS every mapped target column. A blank cell would read as "no
    // dedup key", which is a different and wrong statement.
    renderReview({ dedupEnabled: true, dedupColumns: [] });

    expect(screen.getByText('All mapped target columns')).toBeInTheDocument();
  });

  it('distinguishes an explicit opt-out from the template default', () => {
    renderReview({ dataQualityEnabled: false, dedupEnabled: false, dedupColumns: [] });

    expect(screen.getByText('Off')).toBeInTheDocument();
    expect(screen.queryByText(/Template default/)).not.toBeInTheDocument();
  });

  it('renders an explicit opt-in as On', () => {
    renderReview({ dataQualityEnabled: true, dedupEnabled: false, dedupColumns: [] });

    expect(screen.getByText('On')).toBeInTheDocument();
  });

  it('survives a draft-restored undefined qualityConfig (R7.5)', () => {
    // The Review step is the last thing between a restored draft and Create, so a
    // throw here is the worst placement: the operator's work is complete and
    // unreachable.
    expect(() => renderReview(undefined)).not.toThrow();
    expect(screen.getByText(/Template default/)).toBeInTheDocument();
  });
});
