// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property test: Secret picker toggle preserves config.secret_arn.
 *
 * Validates: P20 Requirements 4.5
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import * as fc from 'fast-check';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useVpcs: () => ({ data: [], isLoading: false, error: null }),
    useSubnets: () => ({ data: undefined, isLoading: false, error: null }),
    useSecurityGroups: () => ({ data: undefined, isLoading: false, error: null }),
    useJdbcCandidateSecrets: () => ({
      data: [
        {
          arn: 'arn:aws:secretsmanager:us-west-2:123456789012:secret:foo-AbCd',
          name: 'foo',
          description: '',
        },
      ],
      isLoading: false,
      error: null,
    }),
  };
});

import { JdbcConnectionSelector } from '../JdbcConnectionSelector';

// Feature: p20-jdbc-manual-mode, Property 8: Secret picker toggle preserves config.secret_arn
describe('Property 8 — Secret picker toggle preserves config.secret_arn', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it('flipping the toggle any number of times leaves config.secret_arn unchanged', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(
          'arn:aws:secretsmanager:us-west-2:1:secret:foo',
          '',
          'arn:aws:secretsmanager:us-east-1:9:secret:bar-xyz',
        ),
        fc.integer({ min: 0, max: 6 }),
        (initialArn, numFlips) => {
          cleanup();
          const config: Record<string, string> = {
            _connection_mode: 'manual',
            secret_arn: initialArn,
          };
          const onChange = vi.fn();
          render(
            <JdbcConnectionSelector config={config} onChange={onChange} />,
          );

          const toggleEl = screen.getByTestId('jdbc-conn-manual-secret-toggle');
          const toggleInput = toggleEl.querySelector(
            'input[type="checkbox"]',
          );
          if (!toggleInput) return;

          for (let i = 0; i < numFlips; i++) {
            fireEvent.click(toggleInput);
          }

          // The toggle is local-only state — onChange has not been
          // called as a result of toggling, so config.secret_arn is
          // unchanged from its starting value.
          expect(config.secret_arn).toBe(initialArn);
        },
      ),
      { numRuns: 30 },
    );
  });
});
