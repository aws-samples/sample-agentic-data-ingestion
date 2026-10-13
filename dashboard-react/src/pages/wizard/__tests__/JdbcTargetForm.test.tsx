// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the consolidated JdbcTargetForm component (P17 Tasks 20.1 + 20.6).
 *
 * Validates: P17 Requirements 14.1, 14.2, 14.4, 22.1.
 *
 * Coverage:
 *  - Renders the shared `JdbcConnectionSelector` (mode tiles, JDBC URL, driver, port)
 *  - Renders the target-specific Database Name + Table Name inputs
 *  - Persists `target_config.type = 'JDBC'` (Requirement 14.4)
 *  - Auto-derives `jdbc_table` as `database_name.table_name`
 *  - Allows manual override of JDBC URL via the connection selector
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { JdbcTargetForm, buildJdbcTable } from '../JdbcTargetForm';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

const mockInstances = [
  {
    db_instance_identifier: 'pg-instance',
    engine: 'postgres',
    engine_version: '15.4',
    endpoint_address: 'pg.xxx.us-west-2.rds.amazonaws.com',
    endpoint_port: 5432,
    db_name: 'mydb',
    db_instance_status: 'available',
    vpc_id: 'vpc-123',
  },
];

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: mockInstances, isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
  };
});

function renderForm(overrides: Partial<WizardState> = {}) {
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    targetType: 'jdbc',
    ...overrides,
  };
  const onChange = vi.fn();
  render(<JdbcTargetForm state={state} onChange={onChange} />);
  return { state, onChange };
}

describe('JdbcTargetForm', () => {
  beforeEach(() => vi.clearAllMocks());

  describe('rendering', () => {
    it('renders the shared connection selector', () => {
      renderForm();
      // Connection-mode tiles + JDBC URL/driver/port inputs all live in the
      // shared `JdbcConnectionSelector`.
      expect(screen.getByText('Select RDS Instance')).toBeInTheDocument();
      expect(screen.getByText('Use Glue Connection')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-target-conn-jdbc-url')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-target-conn-jdbc-driver')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-target-conn-port')).toBeInTheDocument();
    });

    it('renders database-name and table-name inputs', () => {
      renderForm();
      expect(screen.getByText('Database Name')).toBeInTheDocument();
      expect(screen.getByText('Table Name')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-target-database-name')).toBeInTheDocument();
      expect(screen.getByTestId('jdbc-target-table-name')).toBeInTheDocument();
    });

    it('does not render any legacy JdbcForm fields (Username/Password/Connection URL)', () => {
      renderForm();
      expect(screen.queryByText('JDBC Connection URL')).not.toBeInTheDocument();
      expect(screen.queryByText('Username')).not.toBeInTheDocument();
      expect(screen.queryByText('Password')).not.toBeInTheDocument();
    });
  });

  describe('persistence', () => {
    it('always sets target_config.type = "JDBC" on update', () => {
      const { onChange } = renderForm();
      const dbInput = screen.getByTestId('jdbc-target-database-name').querySelector('input')!;
      fireEvent.change(dbInput, { target: { value: 'mydb' } });

      expect(onChange).toHaveBeenCalled();
      const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(updated.targetConfig.type).toBe('JDBC');
      expect(updated.targetConfig.database_name).toBe('mydb');
    });

    it('derives jdbc_table as "database_name.table_name" when both are populated', () => {
      // Start with database_name already populated so a single keystroke into
      // table_name triggers the derivation in the form's update() helper.
      const { onChange } = renderForm({
        targetConfig: { type: 'JDBC', database_name: 'mydb' },
      });
      const tableInput = screen.getByTestId('jdbc-target-table-name').querySelector('input')!;
      fireEvent.change(tableInput, { target: { value: 'customers' } });

      expect(onChange).toHaveBeenCalled();
      const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(updated.targetConfig.jdbc_table).toBe('mydb.customers');
      expect(updated.targetConfig.type).toBe('JDBC');
    });

    it('allows the connection selector to override the JDBC URL', () => {
      const { onChange } = renderForm({
        targetConfig: { type: 'JDBC', database_name: 'mydb', table_name: 'orders' },
      });
      const urlInput = screen.getByTestId('jdbc-target-conn-jdbc-url').querySelector('input')!;
      fireEvent.change(urlInput, {
        target: { value: 'jdbc:postgresql://override-host:5432/mydb' },
      });

      expect(onChange).toHaveBeenCalled();
      const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(updated.targetConfig.jdbc_url).toBe(
        'jdbc:postgresql://override-host:5432/mydb',
      );
      expect(updated.targetConfig.type).toBe('JDBC');
      // The selector path also re-derives jdbc_table.
      expect(updated.targetConfig.jdbc_table).toBe('mydb.orders');
    });

    it('clears detectedTargetSchema on update so the user must re-detect after target changes', () => {
      const { onChange } = renderForm({
        detectedTargetSchema: [
          { name: 'id', type: 'int', nullable: false, description: '' },
        ],
      });
      const dbInput = screen.getByTestId('jdbc-target-database-name').querySelector('input')!;
      fireEvent.change(dbInput, { target: { value: 'newdb' } });
      const updated = onChange.mock.calls[onChange.mock.calls.length - 1][0];
      expect(updated.detectedTargetSchema).toBeNull();
    });
  });
});

describe('buildJdbcTable', () => {
  it('joins database and table with a dot', () => {
    expect(buildJdbcTable('mydb', 'customers')).toBe('mydb.customers');
  });

  it('handles empty values without throwing (caller is responsible for guarding)', () => {
    expect(buildJdbcTable('', 'customers')).toBe('.customers');
    expect(buildJdbcTable('mydb', '')).toBe('mydb.');
  });
});
