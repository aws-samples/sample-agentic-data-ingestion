// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * CursorPagination — p48 task 5.1.
 *
 * `ListPagination`, the component this one sits beside, is flagged by codegraph as
 * having **no covering tests** despite carrying a docstring about a real past bug.
 * This file exists so its cursor-model sibling does not inherit that gap, and the
 * first two tests below are that docstring's fence asserted directly: the control
 * is always mounted and only ever `disabled`.
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import CursorPagination, { describePagePosition } from '../CursorPagination';

function renderPager(props: Partial<React.ComponentProps<typeof CursorPagination>> = {}) {
  const onNext = vi.fn();
  const onPrevious = vi.fn();
  const result = render(
    <CursorPagination
      page={1}
      totalPages={3}
      hasNext
      hasPrev={false}
      onNext={onNext}
      onPrevious={onPrevious}
      {...props}
    />,
  );
  return { ...result, onNext, onPrevious };
}

describe('CursorPagination', () => {
  /**
   * **Validates: Requirements 2.6**
   *
   * The `ListPagination.tsx:11-16` fence: an unavailable direction is a disabled
   * button, never an absent one. Over every combination, so the "control vanished
   * and stranded the user" failure mode is unreachable by construction rather
   * than by inspection.
   */
  it('always mounts both directions, whatever is available', () => {
    fc.assert(
      fc.property(fc.boolean(), fc.boolean(), fc.boolean(), (hasNext, hasPrev, disabled) => {
        cleanup();
        renderPager({ hasNext, hasPrev, disabled });
        const next = screen.getByTestId('cursor-pagination-next');
        const previous = screen.getByTestId('cursor-pagination-previous');
        expect(next).toBeInTheDocument();
        expect(previous).toBeInTheDocument();
        expect((next as HTMLButtonElement).disabled).toBe(disabled || !hasNext);
        expect((previous as HTMLButtonElement).disabled).toBe(disabled || !hasPrev);
      }),
      { numRuns: 100 },
    );
  });

  it('invokes the handler for an available direction only', () => {
    const { onNext, onPrevious } = renderPager({ hasNext: true, hasPrev: false });
    fireEvent.click(screen.getByTestId('cursor-pagination-next'));
    fireEvent.click(screen.getByTestId('cursor-pagination-previous'));
    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onPrevious).not.toHaveBeenCalled();
  });

  /**
   * **Validates: Requirements 12.5**
   *
   * `LGR-99050e6-1` (**template / ratified**): colour alone fails WCAG 1.4.1. The
   * position is text, and each control carries an accessible name rather than
   * relying on its arrow glyph.
   */
  it('states position and direction in text', () => {
    renderPager({ page: 2, totalPages: 5, hasPrev: true });
    expect(screen.getByTestId('cursor-pagination-position')).toHaveTextContent('Page 2 of 5');
    expect(screen.getByLabelText('Next page')).toBeInTheDocument();
    expect(screen.getByLabelText('Previous page')).toBeInTheDocument();
  });
});

describe('describePagePosition', () => {
  /**
   * **Validates: Requirements 4.3**
   *
   * An exact total reads as a total; a floor says so. The floor never reads below
   * the page on screen — being on page N proves N pages exist, so widening one
   * genuine lower bound with another keeps the "at least" claim true. That is the
   * opposite of the `total_pages: 1` defect, which claimed exactness.
   */
  it('distinguishes an exact total from a floor, and never understates a floor', () => {
    expect(describePagePosition(2, 5, false)).toBe('Page 2 of 5');
    expect(describePagePosition(2, 5, true)).toBe('Page 2 of at least 5');
    expect(describePagePosition(4, 1, true)).toBe('Page 4 of at least 4');

    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 50 }),
        fc.integer({ min: 0, max: 50 }),
        (page, totalPages) => {
          const floor = describePagePosition(page, totalPages, true);
          expect(floor).toContain('at least');
          // The number quoted is never below the page being shown.
          const quoted = Number(floor.split('at least ')[1]);
          expect(quoted).toBeGreaterThanOrEqual(page);
          expect(quoted).toBeGreaterThanOrEqual(totalPages);
          // An exact reading never says "at least".
          expect(describePagePosition(page, totalPages, false)).not.toContain('at least');
        },
      ),
      { numRuns: 100 },
    );
  });

  it('quotes only the page when an exact total cannot describe it', () => {
    expect(describePagePosition(3, 0, false)).toBe('Page 3');
  });
});
