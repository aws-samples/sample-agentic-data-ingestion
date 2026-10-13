// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for ToastContext
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity, Property 16: Toast notification content and replacement
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, act } from '@testing-library/react';
import { ToastProvider, useToast, type ToastType } from '../ToastContext';

// ---------------------------------------------------------------------------
// Arbitraries (smart generators)
// ---------------------------------------------------------------------------

/** Generates a non-empty trimmed toast message string (no leading/trailing spaces). */
const toastMessageArb = fc
  .stringOf(
    fc.constantFrom(
      ...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'.split(''),
    ),
    { minLength: 1, maxLength: 60 },
  );

/** Generates a valid toast type. */
const toastTypeArb: fc.Arbitrary<ToastType> = fc.constantFrom(
  'success',
  'error',
  'info',
  'warning',
);

// ---------------------------------------------------------------------------
// Test helper: renders ToastProvider and captures showToast
// ---------------------------------------------------------------------------

function renderToastProvider() {
  let showToast: (message: string, type: ToastType) => void = () => {};

  function Capture() {
    const toast = useToast();
    showToast = toast.showToast;
    return null;
  }

  const result = render(
    <ToastProvider>
      <Capture />
    </ToastProvider>,
  );

  return {
    ...result,
    showToast: (msg: string, type: ToastType) => showToast(msg, type),
  };
}

// ---------------------------------------------------------------------------
// Property 16: Toast notification content and replacement
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 16: Toast notification content and replacement', () => {
  /**
   * **Validates: Requirements 14.1, 14.4**
   *
   * For any toast message and type, the ToastNotification SHALL render with
   * the message text and the CSS class/attribute corresponding to the type.
   * For any sequence of two toast calls, only the most recent toast SHALL
   * be visible.
   */

  it('renders toast with correct message text and data-toast-type attribute for any message and type', () => {
    fc.assert(
      fc.property(toastMessageArb, toastTypeArb, (message, type) => {
        cleanup();
        const { showToast, container } = renderToastProvider();

        act(() => {
          showToast(message, type);
        });

        const toastEl = within(container).getByTestId('toast-container');

        // Verify the type attribute matches
        expect(toastEl).toHaveAttribute('data-toast-type', type);

        // The toast container includes an icon span and a message span.
        // Verify the message text is present in the rendered output.
        expect(toastEl.textContent).toContain(message);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('after two sequential toast calls, only the most recent toast message and type are visible', () => {
    fc.assert(
      fc.property(
        toastMessageArb,
        toastTypeArb,
        toastMessageArb,
        toastTypeArb,
        (msg1, type1, msg2, type2) => {
          cleanup();
          const { showToast, container } = renderToastProvider();

          // Show first toast
          act(() => {
            showToast(msg1, type1);
          });

          // Show second toast (should replace the first)
          act(() => {
            showToast(msg2, type2);
          });

          // Only one toast container should exist within this render
          const containers = within(container).queryAllByTestId('toast-container');
          expect(containers).toHaveLength(1);

          // The visible toast should have the second message and type
          const toastEl = containers[0];
          expect(toastEl).toHaveAttribute('data-toast-type', type2);
          expect(toastEl.textContent).toContain(msg2);

          cleanup();
        },
      ),
      { numRuns: 100 },
    );
  });
});
