// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for ApprovalWorkflowSection
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 13: Approval workflow rendering
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const approvalStatusArb = fc.constantFrom('pending', 'approved', 'rejected');
const approvalTypeArb = fc.constantFrom('schema_mapping', 'code_generation', 'first_run');

const approvalArb: fc.Arbitrary<Approval> = fc.record({
  approval_id: fc.uuid(),
  pipeline_key: fc.constant('test-pipeline'),
  run_id: fc.option(fc.uuid(), { nil: undefined }),
  approval_type: approvalTypeArb,
  status: approvalStatusArb,
  created_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  decided_at: fc.option(
    fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
    { nil: undefined },
  ),
  decided_by: fc.option(fc.constantFrom('admin', 'reviewer', 'user1'), { nil: undefined }),
  comment: fc.option(fc.constant('Looks good'), { nil: undefined }),
  details: fc.option(fc.constant({ mapping_count: 5 } as Record<string, unknown>), { nil: undefined }),
  gate_name: fc.option(fc.constantFrom('Schema Review', 'Code Review', 'Deploy Gate'), { nil: undefined }),
  response_time_ms: fc.option(fc.integer({ min: 100, max: 60000 }), { nil: undefined }),
});

const runDetailArb: fc.Arbitrary<RunDetail> = fc.record({
  run_id: fc.uuid(),
  pipeline_key: fc.constant('test-pipeline'),
  status: fc.constantFrom('RUNNING', 'COMPLETED', 'FAILED'),
  started_at: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  completed_at: fc.option(
    fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
    { nil: undefined },
  ),
});

// ---------------------------------------------------------------------------
// Property 13: Approval workflow rendering
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 13: Approval workflow rendering', () => {
  /**
   * **Validates: Requirements 10.1, 10.2, 10.3**
   *
   * For any set of Approval objects, the ApprovalWorkflowSection SHALL render
   * gate indicators for each approval, action buttons for pending approvals,
   * decision details for completed approvals, and an audit trail entry for
   * every approval with timestamp and actor.
   */
  it('renders gate indicators, action buttons for pending, decision details for completed, and audit trail', () => {
    const approvalsArb = fc.array(approvalArb, { minLength: 1, maxLength: 6 });

    fc.assert(
      fc.property(fc.tuple(approvalsArb, runDetailArb), ([approvals, runDetail]) => {
        cleanup();
        const { container } = render(
          <ApprovalWorkflowSection approvals={approvals} runDetails={runDetail} />,
        );

        const section = within(container).getByTestId('approval-workflow-section');

        // Gate indicators: one per approval
        const gates = within(section).getAllByTestId('approval-gate');
        expect(gates).toHaveLength(approvals.length);

        // Pending approvals should have action buttons
        const pendingCount = approvals.filter((a) => a.status?.toLowerCase() === 'pending').length;
        const pendingEls = within(section).queryAllByTestId('pending-approval');
        expect(pendingEls).toHaveLength(pendingCount);

        // Each pending approval should have approve and reject buttons
        for (const pendingEl of pendingEls) {
          expect(within(pendingEl).getByTestId('approve-button')).toBeTruthy();
          expect(within(pendingEl).getByTestId('reject-button')).toBeTruthy();
        }

        // Completed approvals should have decision details
        const completedCount = approvals.filter((a) => a.status?.toLowerCase() !== 'pending').length;
        const completedEls = within(section).queryAllByTestId('completed-approval');
        expect(completedEls).toHaveLength(completedCount);

        // Audit trail: one entry per approval
        const auditEntries = within(section).getAllByTestId('audit-trail-entry');
        expect(auditEntries).toHaveLength(approvals.length);

        // Each audit entry has timestamp and actor
        for (const entry of auditEntries) {
          expect(within(entry).getByTestId('audit-timestamp')).toBeTruthy();
          expect(within(entry).getByTestId('audit-actor')).toBeTruthy();
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
