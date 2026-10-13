// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for `DataQualityTab`.
 *
 * The tab is read-only. It used to forward an on-demand "Run Quality Check"
 * trigger (p30 R14.1) to `QualityReportCard`; that check was disabled on
 * 2026-10-11 because the agent received no target table and had no Athena
 * grant, so its only result was a failing report written over the inline Glue
 * report (docs/AGENTS.md § 3). The tests pin both halves: the report still
 * renders, and no trigger is offered beside it. The card is asserted present,
 * so the button's absence cannot pass merely because nothing rendered.
 *
 * The tab's error state (`quality-error` + `quality-retry`) is covered by
 * `pages/__tests__/RunDetailPage.async.test.tsx` and is not repeated.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import DataQualityTab from '../DataQualityTab';
import type { QualityReport } from '../../../hooks/use-quality-report';

/**
 * Literal report shape, transcribed from the `QualityReport` interface and the
 * DQDL rule names the S3 Tables template emits, rather than produced by calling
 * the hook (`LGR-28b4d75-5`).
 */
const PASSING_REPORT: QualityReport = {
  pipeline_id: 'pipe-1',
  run_id: 'run-1',
  overall_status: 'pass',
  checks: [
    { name: 'RowCount > 0', status: 'pass', actual: 1200 },
    { name: 'Completeness "customer_id" >= 0.95', status: 'pass', actual: 0.99, threshold: 0.95 },
  ],
  root_cause: null,
  recommendations: [],
};

function triggerButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: /run quality check/i });
}

describe('DataQualityTab', () => {
  it('renders the populated report card', () => {
    render(<DataQualityTab qualityReport={PASSING_REPORT} onRetry={vi.fn()} />);

    const card = screen.getByTestId('quality-report-card');
    expect(card.textContent).toContain('Overall: PASS');
    expect(screen.getByTestId('quality-check-RowCount > 0')).toBeInTheDocument();
    expect(screen.getByTestId('quality-check-Completeness "customer_id" >= 0.95')).toBeInTheDocument();
  });

  it('offers no on-demand quality check beside the report', () => {
    render(<DataQualityTab qualityReport={PASSING_REPORT} onRetry={vi.fn()} />);

    expect(screen.getByTestId('quality-report-card')).toBeInTheDocument();
    expect(triggerButton()).not.toBeInTheDocument();
  });

  it('renders no card and the empty-state copy when there is no report', () => {
    render(<DataQualityTab qualityReport={null} onRetry={vi.fn()} />);

    expect(screen.queryByTestId('quality-report-card')).not.toBeInTheDocument();
    expect(triggerButton()).not.toBeInTheDocument();
    expect(screen.getByText(/No data quality results available/)).toBeInTheDocument();
  });
});
