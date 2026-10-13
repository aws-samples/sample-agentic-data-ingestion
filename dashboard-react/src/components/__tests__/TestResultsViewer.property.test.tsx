// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for TestResultsViewer
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 10: Test results display with status classification
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import { TestResultsDisplay } from '../TestResultsViewer';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const passingStatusArb = fc.constantFrom('passed', 'pass', 'success', 'succeeded');
const failingStatusArb = fc.constantFrom('failed', 'fail', 'error');

const plainOutputArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789 \n'.split('')),
  { minLength: 1, maxLength: 100 },
);

const jsonOutputArb = fc.record({
  result: fc.constantFrom('ok', 'error'),
  count: fc.integer({ min: 0, max: 1000 }),
}).map((obj) => JSON.stringify(obj));

const errorDetailsArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789 :\n'.split('')),
  { minLength: 1, maxLength: 100 },
);

// ---------------------------------------------------------------------------
// Property 10: Test results display with status classification
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 10: Test results display with status classification', () => {
  /**
   * **Validates: Requirements 8.1, 8.2, 8.3**
   *
   * For any TestResult, the viewer SHALL render status with correct color
   * class (green for passing, red for failing), output, and error details
   * when present. Structured JSON output SHALL be formatted with indentation.
   */
  it('renders passing tests with pass class and correct content', () => {
    const passingTestArb = fc.record({
      test_status: passingStatusArb,
      test_output: fc.oneof(plainOutputArb, jsonOutputArb),
      error_details: fc.constant(undefined as string | undefined),
    });

    fc.assert(
      fc.property(passingTestArb, (testResult) => {
        cleanup();
        const { container } = render(
          <TestResultsDisplay testResult={testResult} />,
        );

        const viewer = within(container).getByTestId('test-results-viewer');

        // Status should have pass class
        const statusEl = within(viewer).getByTestId('test-status');
        expect(statusEl.className).toContain('test-status-pass');
        expect(statusEl.textContent).toBe(testResult.test_status);

        // Green (success) design token for passing (P29 R8.2), not a hardcoded hex
        expect(statusEl.style.color).toContain('color-text-status-success');

        // Output should be rendered
        const outputEl = within(viewer).getByTestId('test-output');
        expect(outputEl.textContent!.length).toBeGreaterThan(0);

        // No error details for passing tests
        const errorEl = within(viewer).queryByTestId('error-details');
        expect(errorEl).toBeNull();

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('renders failing tests with fail class and shows error details when present', () => {
    const failingTestArb = fc.record({
      test_status: failingStatusArb,
      test_output: plainOutputArb,
      error_details: fc.option(errorDetailsArb, { nil: undefined }),
    });

    fc.assert(
      fc.property(failingTestArb, (testResult) => {
        cleanup();
        const { container } = render(
          <TestResultsDisplay testResult={testResult} />,
        );

        const viewer = within(container).getByTestId('test-results-viewer');

        // Status should have fail class
        const statusEl = within(viewer).getByTestId('test-status');
        expect(statusEl.className).toContain('test-status-fail');
        expect(statusEl.textContent).toBe(testResult.test_status);

        // Red (error) design token for failing (P29 R8.2), not a hardcoded hex
        expect(statusEl.style.color).toContain('color-text-status-error');

        // Output should be rendered
        const outputEl = within(viewer).getByTestId('test-output');
        expect(outputEl.textContent!.length).toBeGreaterThan(0);

        // Error details should appear when present
        if (testResult.error_details) {
          const errorEl = within(viewer).getByTestId('error-details');
          expect(errorEl.textContent).toContain(testResult.error_details);
        } else {
          const errorEl = within(viewer).queryByTestId('error-details');
          expect(errorEl).toBeNull();
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('formats structured JSON output with indentation', () => {
    const jsonTestArb = fc.record({
      test_status: fc.constantFrom('passed', 'failed'),
      test_output: jsonOutputArb,
      error_details: fc.constant(undefined as string | undefined),
    });

    fc.assert(
      fc.property(jsonTestArb, (testResult) => {
        cleanup();
        const { container } = render(
          <TestResultsDisplay testResult={testResult} />,
        );

        const viewer = within(container).getByTestId('test-results-viewer');
        const outputEl = within(viewer).getByTestId('test-output');

        // JSON output should be formatted with indentation (contains newlines)
        const outputText = outputEl.textContent ?? '';
        const parsed = JSON.parse(testResult.test_output);
        const expected = JSON.stringify(parsed, null, 2);
        expect(outputText).toBe(expected);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
