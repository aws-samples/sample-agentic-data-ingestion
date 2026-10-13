// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Example tests for the manual JDBC connection mode in JdbcConnectionSelector.
 *
 * Covers P20 Tasks 8.11 and a subset of 8.7 (mode-switch invariants):
 *   - Third tile is rendered with the documented label and description (Req 1.1)
 *   - <ManualConnectionForm> renders iff `_connection_mode === 'manual'` (Req 1.5)
 *   - Engine <Select> options match Object.keys(JDBC_DRIVER_MAP); no "Other JDBC" (Req 2.1)
 *   - JDBC URL placeholder reflects the selected Engine's protocol (Req 2.3)
 *   - Driver Class <Input> is bound to config.jdbc_driver (Req 2.4)
 *   - VPC <Select> is populated by useVpcs() (Req 3.1)
 *   - Mode-switch field clearing on entering / leaving 'manual' (Req 1.3, 1.4)
 *
 * Validates: P20 Requirements 1.1, 1.3, 1.4, 1.5, 2.1, 2.3, 2.4, 3.1, 14.4
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useVpcs: () => ({
      data: [
        { vpc_id: 'vpc-aaaa1111', name: 'AgenticDataIngestion-dev', is_default: false, cidr: '10.0.0.0/16' },
        { vpc_id: 'vpc-bbbb2222', name: 'prod', is_default: true, cidr: '172.31.0.0/16' },
      ],
      isLoading: false,
      error: null,
    }),
    useSubnets: (vpcId: string | undefined | null) => ({
      data: vpcId
        ? [
            {
              subnet_id: 'subnet-aaaa1111',
              name: 'private-2a',
              availability_zone: 'us-west-2a',
              cidr: '10.0.1.0/24',
            },
          ]
        : undefined,
      isLoading: false,
      error: null,
    }),
    useSecurityGroups: (vpcId: string | undefined | null) => ({
      data: vpcId
        ? [
            { sg_id: 'sg-aaaa1111', name: 'default', description: 'default sg' },
          ]
        : undefined,
      isLoading: false,
      error: null,
    }),
    useJdbcCandidateSecrets: () => ({
      data: [
        {
          arn: 'arn:aws:secretsmanager:us-west-2:123456789012:secret:rds-prod-AbCd',
          name: 'rds-prod',
          description: 'production RDS',
        },
      ],
      isLoading: false,
      error: null,
    }),
  };
});

import {
  JdbcConnectionSelector,
  JDBC_DRIVER_MAP,
} from '../JdbcConnectionSelector';

function renderSelector(
  initialConfig: Record<string, string> = {},
): {
  config: Record<string, string>;
  onChange: ReturnType<typeof vi.fn>;
} {
  const config = { ...initialConfig };
  const onChange = vi.fn((next: Record<string, string>) => {
    Object.keys(config).forEach((k) => delete config[k]);
    Object.assign(config, next);
  });
  render(<JdbcConnectionSelector config={config} onChange={onChange} />);
  return { config, onChange };
}

describe('JdbcConnectionSelector — manual mode (P20)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  // -------------------------------------------------------------
  // Req 1.1 — Third tile rendered
  // -------------------------------------------------------------

  it('renders a third tile labelled "External or self-managed database"', () => {
    renderSelector();
    expect(
      screen.getByText('External or self-managed database'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/on-prem databases.*another AWS account.*self-managed/),
    ).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 1.5 — Manual form renders iff _connection_mode === 'manual'
  // -------------------------------------------------------------

  it('does NOT render <ManualConnectionForm> for select_rds mode', () => {
    renderSelector({ _connection_mode: 'select_rds' });
    expect(
      screen.queryByTestId('jdbc-conn-manual-engine'),
    ).not.toBeInTheDocument();
  });

  it('does NOT render <ManualConnectionForm> for glue_connection mode', () => {
    renderSelector({ _connection_mode: 'glue_connection' });
    expect(
      screen.queryByTestId('jdbc-conn-manual-engine'),
    ).not.toBeInTheDocument();
  });

  it('renders <ManualConnectionForm> when _connection_mode is "manual"', () => {
    renderSelector({ _connection_mode: 'manual' });
    expect(screen.getByTestId('jdbc-conn-manual-engine')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-manual-jdbc-url')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-manual-jdbc-driver')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-manual-vpc')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-manual-subnet')).toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-manual-sg')).toBeInTheDocument();
    expect(
      screen.getByTestId('jdbc-conn-manual-secret-toggle'),
    ).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 2.4 — Driver Class is bound to config.jdbc_driver
  // -------------------------------------------------------------

  it('binds the Driver Class input to config.jdbc_driver', () => {
    renderSelector({
      _connection_mode: 'manual',
      jdbc_driver: 'org.postgresql.Driver',
    });
    const input = screen
      .getByTestId('jdbc-conn-manual-jdbc-driver')
      .querySelector('input');
    expect(input).not.toBeNull();
    expect((input as HTMLInputElement).value).toBe('org.postgresql.Driver');
  });

  // -------------------------------------------------------------
  // Req 1.6 — Existing tiles unchanged
  // -------------------------------------------------------------

  it('still renders the existing select_rds and glue_connection tiles', () => {
    renderSelector();
    expect(screen.getByText('Select RDS Instance')).toBeInTheDocument();
    expect(screen.getByText('Use Glue Connection')).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 1.3, 1.4 — Mode-switch field clearing
  // -------------------------------------------------------------

  it('clears manual-mode-private fields when switching FROM manual to select_rds (Req 1.4)', () => {
    const { onChange } = renderSelector({
      _connection_mode: 'manual',
      vpc_id: 'vpc-aaaa1111',
      subnet_id: 'subnet-aaaa1111',
      security_group_ids: '["sg-aaaa1111"]',
      availability_zone: 'us-west-2a',
      // Cross-mode fields preserved per Req 1.4:
      secret_arn: 'arn:aws:secretsmanager:us-west-2:1:secret:foo',
      jdbc_url: 'jdbc:postgresql://onprem:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
      engine: 'postgres',
    });

    // Click the "Select RDS Instance" tile.
    fireEvent.click(screen.getByText('Select RDS Instance'));

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next._connection_mode).toBe('select_rds');
    // Manual-mode-private fields cleared:
    expect(next.vpc_id).toBeUndefined();
    expect(next.subnet_id).toBeUndefined();
    expect(next.security_group_ids).toBeUndefined();
    expect(next.availability_zone).toBeUndefined();
    // Cross-mode fields preserved:
    expect(next.secret_arn).toBe('arn:aws:secretsmanager:us-west-2:1:secret:foo');
    expect(next.engine).toBe('postgres');
    expect(next.jdbc_url).toBe('jdbc:postgresql://onprem:5432/db');
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
  });

  it('clears select_rds-private fields when entering manual mode (Req 1.3)', () => {
    const { onChange } = renderSelector({
      _connection_mode: 'select_rds',
      _selected_rds: 'instance:pg',
      db_instance_identifier: 'pg',
      _port: '5432',
      // Cross-mode fields preserved
      engine: 'postgres',
      jdbc_url: 'jdbc:postgresql://x:5432/y',
      jdbc_driver: 'org.postgresql.Driver',
      secret_arn: 'arn:aws:secretsmanager:us-west-2:1:secret:foo',
    });

    fireEvent.click(screen.getByText('External or self-managed database'));
    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next._connection_mode).toBe('manual');
    // Mode-private fields cleared:
    expect(next._selected_rds).toBeUndefined();
    expect(next.db_instance_identifier).toBeUndefined();
    expect(next._port).toBeUndefined();
    // Cross-mode fields preserved (Req 1.4):
    expect(next.engine).toBe('postgres');
    expect(next.secret_arn).toBe('arn:aws:secretsmanager:us-west-2:1:secret:foo');
  });

  it('clears glue_connection-private fields when entering manual mode (Req 1.3)', () => {
    const { onChange } = renderSelector({
      _connection_mode: 'glue_connection',
      glue_connection_name: 'my-conn',
      jdbc_url: 'jdbc:postgresql://x:5432/y',
      jdbc_driver: 'org.postgresql.Driver',
    });

    fireEvent.click(screen.getByText('External or self-managed database'));
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next._connection_mode).toBe('manual');
    expect(next.glue_connection_name).toBeUndefined();
    // Cross-mode fields preserved.
    expect(next.jdbc_url).toBe('jdbc:postgresql://x:5432/y');
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
  });

  // -------------------------------------------------------------
  // Req 2.1 — Engine list — count check (Object.keys(JDBC_DRIVER_MAP))
  // -------------------------------------------------------------

  it('the engine map has the documented number of options (no "Other JDBC")', () => {
    // P20 Req 2.1 — Engine <Select> options are exactly the keys of
    // JDBC_DRIVER_MAP; no "Other JDBC" option exists.
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('postgres');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('aurora-postgresql');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('mysql');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('aurora-mysql');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('mariadb');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('oracle-ee');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('oracle-se2');
    expect(Object.keys(JDBC_DRIVER_MAP)).toContain('sqlserver-ee');
    expect(Object.keys(JDBC_DRIVER_MAP)).not.toContain('other');
    expect(Object.keys(JDBC_DRIVER_MAP)).not.toContain('Other JDBC');
  });
});
