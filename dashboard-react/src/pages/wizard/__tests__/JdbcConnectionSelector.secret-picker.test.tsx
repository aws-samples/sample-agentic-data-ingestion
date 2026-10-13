// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Example tests for the Secret picker manual-ARN toggle in
 * <ManualConnectionForm>.
 *
 * Covers P20 Task 8.13 (Reqs 4.2, 4.3, 4.4) and 12.2 / 12.3 backward-compat:
 *   - Toggle defaults to false; <Select> visible
 *   - Toggle on → <Input> visible; <Select> hidden
 *   - Toggle off → <Select> visible; <Input> hidden
 *   - Loading P17 select_rds pipeline pre-selects select_rds tile (Req 12.2)
 *   - Loading P17 glue_connection pipeline pre-selects glue_connection tile (Req 12.3)
 *
 * Validates: P20 Requirements 4.2, 4.3, 4.4, 12.2, 12.3, 14.4
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({
      data: [
        {
          db_instance_identifier: 'pg-instance',
          engine: 'postgres',
          engine_version: '15.4',
          endpoint_address: 'pg.example.rds.amazonaws.com',
          endpoint_port: 5432,
          db_name: 'mydb',
          db_instance_status: 'available',
          vpc_id: 'vpc-aaaa',
        },
      ],
      isLoading: false,
    }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({
      data: [
        {
          name: 'my-existing-conn',
          connection_type: 'JDBC',
          jdbc_url: 'jdbc:postgresql://glue:5432/db',
          jdbc_driver: 'org.postgresql.Driver',
          status: 'READY',
        },
      ],
      isLoading: false,
    }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useVpcs: () => ({
      data: [
        { vpc_id: 'vpc-aaaa1111', name: 'AgenticDataIngestion-dev', is_default: false, cidr: '10.0.0.0/16' },
      ],
      isLoading: false,
      error: null,
    }),
    useSubnets: () => ({ data: [], isLoading: false, error: null }),
    useSecurityGroups: () => ({ data: [], isLoading: false, error: null }),
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

import { JdbcConnectionSelector } from '../JdbcConnectionSelector';

function renderSelector(initialConfig: Record<string, string> = {}) {
  const onChange = vi.fn();
  render(
    <JdbcConnectionSelector config={initialConfig} onChange={onChange} />,
  );
  return { onChange };
}

describe('JdbcConnectionSelector — secret picker (P20)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  // -------------------------------------------------------------
  // Req 4.2, 4.4 — Default toggle off; <Select> visible
  // -------------------------------------------------------------

  it('defaults the manual-ARN toggle to false; the <Select> is visible', () => {
    renderSelector({ _connection_mode: 'manual' });
    expect(
      screen.getByTestId('jdbc-conn-manual-secret-select'),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('jdbc-conn-manual-secret-arn'),
    ).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 4.3 — Toggle on → <Input> visible; <Select> hidden
  // -------------------------------------------------------------

  it('clicking the toggle on swaps the <Select> for an <Input>', () => {
    renderSelector({ _connection_mode: 'manual' });
    const toggleEl = screen.getByTestId('jdbc-conn-manual-secret-toggle');
    const toggleInput = toggleEl.querySelector('input[type="checkbox"]');
    expect(toggleInput).not.toBeNull();
    fireEvent.click(toggleInput!);

    expect(
      screen.getByTestId('jdbc-conn-manual-secret-arn'),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('jdbc-conn-manual-secret-select'),
    ).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 4.4 — Toggle off → <Select> visible; <Input> hidden
  // -------------------------------------------------------------

  it('toggling back to off restores the <Select>', () => {
    renderSelector({ _connection_mode: 'manual' });
    const toggleEl = screen.getByTestId('jdbc-conn-manual-secret-toggle');
    const toggleInput = toggleEl.querySelector('input[type="checkbox"]');

    // On → off → confirm <Select> reappears.
    fireEvent.click(toggleInput!);
    fireEvent.click(toggleInput!);
    expect(
      screen.getByTestId('jdbc-conn-manual-secret-select'),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('jdbc-conn-manual-secret-arn'),
    ).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 12.2 — Loading a P17 select_rds pipeline pre-selects select_rds tile
  // -------------------------------------------------------------

  it('a persisted P17 select_rds config renders the select_rds form, not manual', () => {
    renderSelector({
      _connection_mode: 'select_rds',
      _selected_rds: 'instance:pg-instance',
      engine: 'postgres',
      jdbc_url: 'jdbc:postgresql://pg:5432/mydb',
    });
    // Manual form is NOT rendered.
    expect(
      screen.queryByTestId('jdbc-conn-manual-engine'),
    ).not.toBeInTheDocument();
    // Select-RDS form IS rendered.
    expect(screen.getByTestId('jdbc-conn-rds-select')).toBeInTheDocument();
  });

  // -------------------------------------------------------------
  // Req 12.3 — Loading a P17 glue_connection pipeline pre-selects glue_connection tile
  // -------------------------------------------------------------

  it('a persisted P17 glue_connection config renders the glue_connection form, not manual', () => {
    renderSelector({
      _connection_mode: 'glue_connection',
      glue_connection_name: 'my-existing-conn',
      jdbc_url: 'jdbc:postgresql://glue:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
    });
    expect(
      screen.queryByTestId('jdbc-conn-manual-engine'),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('jdbc-conn-glue-select')).toBeInTheDocument();
  });
});
