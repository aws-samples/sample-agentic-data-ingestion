// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for MappingVersionNav
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 9: Mapping version navigation state
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import MappingVersionNav from '../MappingVersionNav';

// ---------------------------------------------------------------------------
// Property 9: Mapping version navigation state
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 9: Mapping version navigation state', () => {
  /**
   * **Validates: Requirements 6.2, 6.3, 6.4, 6.5**
   *
   * For any currentIndex and totalVersions, display shows
   * `currentIndex + 1` of `totalVersions`, previous disabled at 0,
   * next disabled at totalVersions - 1.
   */
  it('displays correct version text and disables buttons at boundaries', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 50 }).chain((total) =>
          fc.integer({ min: 0, max: total - 1 }).map((idx) => ({
            currentIndex: idx,
            totalVersions: total,
          })),
        ),
        ({ currentIndex, totalVersions }) => {
          cleanup();
          const onPrevious = vi.fn();
          const onNext = vi.fn();

          const { container } = render(
            <MappingVersionNav
              currentIndex={currentIndex}
              totalVersions={totalVersions}
              onPrevious={onPrevious}
              onNext={onNext}
            />,
          );

          const nav = within(container).getByTestId('version-nav');

          // Version counter text
          const counter = within(nav).getByTestId('version-counter');
          expect(counter.textContent).toContain(`${currentIndex + 1}`);
          expect(counter.textContent).toContain(`${totalVersions}`);

          // Previous button disabled at index 0
          const prevBtn = within(nav).getByTestId('prev-button');
          const prevButtonEl = prevBtn.querySelector('button') ?? prevBtn;
          if (currentIndex === 0) {
            expect(
              prevButtonEl.hasAttribute('disabled') ||
              prevButtonEl.getAttribute('aria-disabled') === 'true',
            ).toBe(true);
          } else {
            expect(
              prevButtonEl.hasAttribute('disabled') &&
              prevButtonEl.getAttribute('disabled') !== null,
            ).toBe(
              prevButtonEl.getAttribute('aria-disabled') === 'true' ? true : false,
            );
          }

          // Next button disabled at last index
          const nextBtn = within(nav).getByTestId('next-button');
          const nextButtonEl = nextBtn.querySelector('button') ?? nextBtn;
          if (currentIndex === totalVersions - 1) {
            expect(
              nextButtonEl.hasAttribute('disabled') ||
              nextButtonEl.getAttribute('aria-disabled') === 'true',
            ).toBe(true);
          } else {
            expect(
              nextButtonEl.hasAttribute('disabled') &&
              nextButtonEl.getAttribute('disabled') !== null,
            ).toBe(
              nextButtonEl.getAttribute('aria-disabled') === 'true' ? true : false,
            );
          }

          cleanup();
        },
      ),
      { numRuns: 100 },
    );
  });
});
