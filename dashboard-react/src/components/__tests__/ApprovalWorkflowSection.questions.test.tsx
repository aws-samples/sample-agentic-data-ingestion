// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for ApprovalWorkflowSection controlled questions (P29 R2).
 *
 * Validates: Requirements 2.2, 2.3, 2.4
 *
 * Covers the R2.7 subset relevant to task 4.2:
 *  - R2.2: the Questions RadioGroup is a *controlled* component bound to state
 *    (the previous `value={null as unknown as string}` + no-op onChange is
 *    gone) — selecting an option checks that radio.
 *  - R2.4: the Approve action is disabled with an inline hint while a required
 *    question is unanswered, and enables once answered.
 *  - R2.3: on approve, the collected answers are handed to `onApprove` so the
 *    consumer can include them in the approval payload as `question_answers`
 *    (via the shared useApprovalAction hook).
 *
 * Note: ApprovalWorkflowSection uses no React Query hooks itself, so no
 * QueryClientProvider wrapper is required here.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail, SchemaMappingApprovalDetails } from '../../types';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const baseRunDetail: RunDetail = {
  run_id: 'run-001',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2024-06-01T10:00:00Z',
};

function makePendingApproval(
  details: SchemaMappingApprovalDetails,
  overrides: Partial<Approval> = {},
): Approval {
  return {
    approval_id: 'approval-sm-001',
    pipeline_key: 'test-pipeline',
    run_id: 'run-001',
    approval_type: 'schema_mapping',
    status: 'pending',
    created_at: '2024-06-01T10:05:00Z',
    gate_name: 'Schema Review',
    details,
    ...overrides,
  };
}

/** Locates a Cloudscape radio input by its option value. */
function radioByValue(value: string): HTMLInputElement {
  const el = document.querySelector(`input[type="radio"][value="${value}"]`);
  expect(el).not.toBeNull();
  return el as HTMLInputElement;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ApprovalWorkflowSection – controlled questions (R2.2/2.3/2.4)', () => {
  it('renders question options as controlled radios and reflects the selected answer (R2.2)', () => {
    const details: SchemaMappingApprovalDetails = {
      questions: [
        {
          field: 'status_code',
          question: 'Which target field should status_code map to?',
          options: ['order_status', 'fulfillment_state', 'skip'],
        },
      ],
    };
    render(<ApprovalWorkflowSection approvals={[makePendingApproval(details)]} runDetails={baseRunDetail} />);

    // The radios render and start unselected (controlled value = null).
    expect(screen.getByTestId('question-options-status_code')).toBeTruthy();
    expect(radioByValue('order_status').checked).toBe(false);
    expect(radioByValue('fulfillment_state').checked).toBe(false);

    // Selecting an option updates the controlled state and checks that radio.
    fireEvent.click(radioByValue('order_status'));
    expect(radioByValue('order_status').checked).toBe(true);
    expect(radioByValue('fulfillment_state').checked).toBe(false);
  });

  it('disables Approve with an inline hint until required questions are answered (R2.4)', () => {
    const details: SchemaMappingApprovalDetails = {
      questions: [
        {
          field: 'status_code',
          question: 'Which target field should status_code map to?',
          options: ['order_status', 'skip'],
          required: true,
        },
      ],
    };
    render(<ApprovalWorkflowSection approvals={[makePendingApproval(details)]} runDetails={baseRunDetail} />);

    // Required question unanswered → Approve disabled + inline hint visible.
    expect(screen.getByTestId('approve-button')).toBeDisabled();
    expect(screen.getByTestId('approve-required-hint')).toBeTruthy();

    // Answering the required question enables Approve and clears the hint.
    fireEvent.click(radioByValue('order_status'));
    expect(screen.getByTestId('approve-button')).not.toBeDisabled();
    expect(screen.queryByTestId('approve-required-hint')).toBeNull();
  });

  it('hands the collected answers to onApprove for the approval payload (R2.3)', () => {
    const onApprove = vi.fn();
    const details: SchemaMappingApprovalDetails = {
      questions: [
        {
          field: 'status_code',
          question: 'Which target field should status_code map to?',
          options: ['order_status', 'skip'],
          required: true,
        },
        {
          field: 'amount',
          question: 'Is amount in cents or dollars?',
          options: ['cents', 'dollars'],
        },
      ],
    };
    render(
      <ApprovalWorkflowSection
        approvals={[makePendingApproval(details)]}
        runDetails={baseRunDetail}
        onApprove={onApprove}
      />,
    );

    fireEvent.click(radioByValue('order_status'));
    fireEvent.click(radioByValue('dollars'));
    fireEvent.click(screen.getByTestId('approve-button'));

    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove).toHaveBeenCalledWith('approval-sm-001', {
      status_code: 'order_status',
      amount: 'dollars',
    });
  });

  it('omits answers (undefined) when no questions were answered (R2.3)', () => {
    const onApprove = vi.fn();
    // No questions at all — approve is not gated and no answers are collected.
    render(
      <ApprovalWorkflowSection
        approvals={[makePendingApproval({})]}
        runDetails={baseRunDetail}
        onApprove={onApprove}
      />,
    );

    expect(screen.getByTestId('approve-button')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('approve-button'));

    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onApprove).toHaveBeenCalledWith('approval-sm-001', undefined);
  });
});
