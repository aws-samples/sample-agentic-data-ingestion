// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Feature: p16-retry-from-any-step, Property 4: Step selector options are correctly computed
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { PIPELINE_STEP_ORDER, getRetryStepOptions } from '../constants';
import type { StepDetail } from '../types';

/**
 * Property 4: Step selector options are correctly computed
 *
 * For any subset of PIPELINE_STEP_ORDER marked as completed and exactly one
 * step marked as failed, the step selector options SHALL contain all completed
 * steps plus the failed step, ordered according to PIPELINE_STEP_ORDER, with
 * no duplicates and no steps outside the completed+failed set.
 *
 * Validates: Requirements 4.2
 */

const allSteps = [...PIPELINE_STEP_ORDER];

/** Build a StepDetail from a step name and status. */
function makeStep(name: string, status: string): StepDetail {
  return { step_name: name, status };
}

describe('Property 4: Step selector options are correctly computed', () => {
  it('returns completed + failed steps in PIPELINE_STEP_ORDER with no duplicates or extras', () => {
    fc.assert(
      fc.property(
        // Generate a random subset of step indices for "completed" steps
        fc.subarray(allSteps, { minLength: 0 }),
        // Pick one random step index for the "failed" step
        fc.integer({ min: 0, max: allSteps.length - 1 }),
        (completedSteps, failedIdx) => {
          const failedStep = allSteps[failedIdx];

          // Build StepDetail array: completed steps + the failed step
          const steps: StepDetail[] = [
            ...completedSteps.map((s) => makeStep(s, 'success')),
            makeStep(failedStep, 'error'),
          ];

          const options = getRetryStepOptions(steps, PIPELINE_STEP_ORDER);
          const optionValues = options.map((o) => o.value);

          // Expected: union of completed + failed, deduplicated, in PIPELINE_STEP_ORDER order
          const eligibleSet = new Set([...completedSteps, failedStep]);
          const expected = allSteps.filter((s) => eligibleSet.has(s));

          // 1. Contains all completed + failed steps
          for (const s of expected) {
            expect(optionValues).toContain(s);
          }

          // 2. Ordered by PIPELINE_STEP_ORDER
          expect(optionValues).toEqual(expected);

          // 3. No duplicates
          expect(new Set(optionValues).size).toBe(optionValues.length);

          // 4. No steps outside the completed+failed set
          for (const v of optionValues) {
            expect(eligibleSet.has(v)).toBe(true);
          }
        },
      ),
      { numRuns: 100 },
    );
  });
});
