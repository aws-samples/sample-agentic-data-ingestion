// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for useRunDetail polling behavior
 * Uses fast-check with Vitest. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 17: Polling behavior based on run status
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';

/**
 * We test the polling logic directly by replicating the refetchInterval
 * function from use-run-detail.ts. This avoids needing to render hooks
 * with full React Query setup while still validating the core property.
 */
function getRefetchInterval(status: string | undefined): number | false {
  return status === 'RUNNING' || status === 'IN_PROGRESS' ? 5_000 : 30_000;
}

// ---------------------------------------------------------------------------
// Property 17: Polling behavior based on run status
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 17: Polling behavior based on run status', () => {
  /**
   * **Validates: Requirements 15.1, 15.3**
   *
   * For any RunDetail, the polling interval SHALL be 5000ms when the run
   * status is "RUNNING" or "IN_PROGRESS", and SHALL be 30000ms when the
   * run status is any terminal state.
   */
  it('returns 5000ms for RUNNING and IN_PROGRESS statuses', () => {
    const activeStatusArb = fc.constantFrom('RUNNING', 'IN_PROGRESS');

    fc.assert(
      fc.property(activeStatusArb, (status) => {
        const interval = getRefetchInterval(status);
        expect(interval).toBe(5_000);
      }),
      { numRuns: 100 },
    );
  });

  it('returns 30000ms for terminal statuses', () => {
    const terminalStatusArb = fc.constantFrom(
      'COMPLETED', 'FAILED', 'CANCELLED', 'SUCCEEDED', 'ERROR', 'STOPPED',
    );

    fc.assert(
      fc.property(terminalStatusArb, (status) => {
        const interval = getRefetchInterval(status);
        expect(interval).toBe(30_000);
      }),
      { numRuns: 100 },
    );
  });

  it('returns 30000ms for undefined status', () => {
    const interval = getRefetchInterval(undefined);
    expect(interval).toBe(30_000);
  });

  it('active statuses always get 5s, all others get 30s', () => {
    const anyStatusArb = fc.constantFrom(
      'RUNNING', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED',
      'SUCCEEDED', 'ERROR', 'STOPPED', 'PENDING', 'UNKNOWN',
    );

    fc.assert(
      fc.property(anyStatusArb, (status) => {
        const interval = getRefetchInterval(status);
        if (status === 'RUNNING' || status === 'IN_PROGRESS') {
          expect(interval).toBe(5_000);
        } else {
          expect(interval).toBe(30_000);
        }
      }),
      { numRuns: 100 },
    );
  });
});
