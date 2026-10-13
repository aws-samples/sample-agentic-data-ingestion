// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property test: mode-switch field-clearing invariants.
 *
 * P20 Task 8.7 — Property 1: for any (oldMode, newMode) ∈
 * {select_rds, glue_connection, manual}² and any starting config,
 * after handleModeChange:
 *   (a) config'._connection_mode === newMode
 *   (b) the mode-private fields of every mode other than newMode are
 *       absent from config'
 *   (c) cross-mode shared fields (secret_arn, jdbc_url, jdbc_driver,
 *       engine) carry over unchanged when present in the input.
 *
 * Validates: P20 Requirements 1.3, 1.4, 12.1
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
    useJdbcCandidateSecrets: () => ({ data: [], isLoading: false, error: null }),
  };
});

import {
  JdbcConnectionSelector,
  type JdbcConnectionMode,
} from '../JdbcConnectionSelector';

const MODES: JdbcConnectionMode[] = ['select_rds', 'glue_connection', 'manual'];

const TILE_LABEL: Record<JdbcConnectionMode, string> = {
  select_rds: 'Select RDS Instance',
  glue_connection: 'Use Glue Connection',
  manual: 'External or self-managed database',
};

const MODE_PRIVATE_FIELDS: Record<JdbcConnectionMode, readonly string[]> = {
  select_rds: ['_selected_rds', 'db_instance_identifier', 'db_cluster_identifier', '_port'],
  glue_connection: ['glue_connection_name'],
  manual: ['vpc_id', 'subnet_id', 'security_group_ids', 'availability_zone'],
};

const CROSS_MODE_FIELDS = ['secret_arn', 'jdbc_url', 'jdbc_driver', 'engine'] as const;

// Feature: p20-jdbc-manual-mode, Property 1: Mode-switch field-clearing invariants
describe('Property 1 — Mode-switch field-clearing invariants', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  it('after switching from oldMode to newMode the post-switch config satisfies the documented invariants', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...MODES),
        fc.constantFrom(...MODES),
        fc.record({
          secret_arn: fc.constantFrom('arn:aws:secretsmanager:1', '', undefined),
          jdbc_url: fc.constantFrom('jdbc:postgresql://x:5432/y', '', undefined),
          jdbc_driver: fc.constantFrom('org.postgresql.Driver', '', undefined),
          engine: fc.constantFrom('postgres', 'mysql', '', undefined),
        }),
        (oldMode, newMode, crossModeFields) => {
          fc.pre(oldMode !== newMode);

          cleanup();
          // Build the starting config: every mode-private field for
          // oldMode is populated; cross-mode fields per the generator.
          const startingConfig: Record<string, string> = {
            _connection_mode: oldMode,
          };
          for (const f of MODE_PRIVATE_FIELDS[oldMode]) {
            startingConfig[f] = `value-${f}`;
          }
          for (const [k, v] of Object.entries(crossModeFields)) {
            if (typeof v === 'string' && v) startingConfig[k] = v;
          }

          const onChange = vi.fn();
          render(
            <JdbcConnectionSelector
              config={startingConfig}
              onChange={onChange}
            />,
          );

          fireEvent.click(screen.getByText(TILE_LABEL[newMode]));
          expect(onChange).toHaveBeenCalled();
          const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];

          // (a) _connection_mode reflects the new mode.
          expect(next._connection_mode).toBe(newMode);

          // (b) Mode-private fields of every other mode are absent.
          for (const mode of MODES) {
            if (mode === newMode) continue;
            for (const f of MODE_PRIVATE_FIELDS[mode]) {
              expect(next[f]).toBeUndefined();
            }
          }

          // (c) Cross-mode fields carry over (when present in input).
          for (const f of CROSS_MODE_FIELDS) {
            if (
              typeof startingConfig[f] === 'string'
              && startingConfig[f]
            ) {
              expect(next[f]).toBe(startingConfig[f]);
            }
          }
        },
      ),
      { numRuns: 30 },
    );
  });
});
