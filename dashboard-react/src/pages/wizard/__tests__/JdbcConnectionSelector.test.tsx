// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the shared JdbcConnectionSelector component.
 *
 * Validates: P17 Requirement 22.1
 *
 * Coverage:
 *  - Both connection modes (`select_rds` and `glue_connection`) render correctly
 *  - Auto-population on RDS instance selection (engine → URL/driver/port)
 *  - Auto-population on Aurora cluster selection
 *  - Auto-population on Glue connection selection
 *  - Manual override of auto-populated fields
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import {
  JdbcConnectionSelector,
  JDBC_DRIVER_MAP,
  buildJdbcUrl,
} from '../JdbcConnectionSelector';

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
  {
    db_instance_identifier: 'mysql-instance',
    engine: 'mysql',
    engine_version: '8.0',
    endpoint_address: 'mysql.xxx.us-west-2.rds.amazonaws.com',
    endpoint_port: 3306,
    db_name: 'shopdb',
    db_instance_status: 'available',
    vpc_id: 'vpc-789',
  },
];

const mockClusters = [
  {
    db_cluster_identifier: 'aurora-cluster',
    engine: 'aurora-mysql',
    engine_version: '8.0',
    endpoint: 'aurora.xxx.us-west-2.rds.amazonaws.com',
    port: 3306,
    database_name: 'auroradb',
    status: 'available',
    vpc_id: 'vpc-456',
  },
];

const mockGlueConnections = [
  {
    name: 'my-glue-conn',
    connection_type: 'JDBC',
    jdbc_url: 'jdbc:postgresql://glue-host:5432/glue_db',
    jdbc_driver: 'org.postgresql.Driver',
    status: 'READY',
  },
];

const mockSecrets = [
  {
    name: 'agentic-data-ingestion/mydb-secret',
    arn: 'arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/mydb-AbCdEf',
    description: 'DB credentials',
    last_changed_date: '2024-01-01',
  },
];

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: mockInstances, isLoading: false }),
    useRdsClusters: () => ({ data: mockClusters, isLoading: false }),
    useRdsGlueConnections: () => ({ data: mockGlueConnections, isLoading: false }),
    useRdsSecrets: () => ({ data: mockSecrets, isLoading: false }),
  };
});

function renderSelector(initialConfig: Record<string, string> = {}) {
  const onChange = vi.fn<(c: Record<string, string>) => void>();
  const utils = render(<JdbcConnectionSelector config={initialConfig} onChange={onChange} />);
  return { onChange, ...utils };
}

/* ---------- JDBC_DRIVER_MAP / buildJdbcUrl helpers ---------- */

describe('JDBC_DRIVER_MAP', () => {
  it('contains all 11 supported engine entries', () => {
    const engines = [
      'postgres', 'aurora-postgresql', 'mysql', 'aurora-mysql', 'mariadb',
      'oracle-ee', 'oracle-se2',
      'sqlserver-ee', 'sqlserver-se', 'sqlserver-ex', 'sqlserver-web',
    ];
    expect(Object.keys(JDBC_DRIVER_MAP)).toHaveLength(11);
    for (const engine of engines) {
      expect(JDBC_DRIVER_MAP[engine]).toBeDefined();
      expect(JDBC_DRIVER_MAP[engine].driver).toBeTruthy();
      expect(JDBC_DRIVER_MAP[engine].port).toBeGreaterThan(0);
      expect(JDBC_DRIVER_MAP[engine].protocol).toBeTruthy();
    }
  });
});

describe('buildJdbcUrl', () => {
  it('constructs a postgres URL', () => {
    expect(buildJdbcUrl('postgres', 'host.example', 5432, 'mydb'))
      .toBe('jdbc:postgresql://host.example:5432/mydb');
  });

  it('constructs an aurora-mysql URL using the mysql protocol', () => {
    expect(buildJdbcUrl('aurora-mysql', 'host.example', 3306, 'mydb'))
      .toBe('jdbc:mysql://host.example:3306/mydb');
  });

  it('falls back to postgresql when the engine is unknown', () => {
    expect(buildJdbcUrl('unknown-engine', 'host', 1234, 'db'))
      .toBe('jdbc:postgresql://host:1234/db');
  });
});

/* ---------- Rendering both modes ---------- */

describe('JdbcConnectionSelector — rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('defaults to select_rds mode and shows the RDS dropdown + secret selector', () => {
    renderSelector();
    expect(screen.getByText('Select RDS Instance')).toBeInTheDocument();
    expect(screen.getByText('Use Glue Connection')).toBeInTheDocument();
    expect(screen.getByText('RDS Instance / Aurora Cluster')).toBeInTheDocument();
    expect(screen.getByText('Secret (Credentials)')).toBeInTheDocument();
    // Glue dropdown is NOT shown in select_rds mode
    expect(screen.queryByText('Glue JDBC Connection')).not.toBeInTheDocument();
  });

  it('renders the Glue connection dropdown when _connection_mode is glue_connection', () => {
    renderSelector({ _connection_mode: 'glue_connection' });
    expect(screen.getByText('Glue JDBC Connection')).toBeInTheDocument();
    // RDS dropdown is NOT shown in glue_connection mode
    expect(screen.queryByText('RDS Instance / Aurora Cluster')).not.toBeInTheDocument();
    expect(screen.queryByText('Secret (Credentials)')).not.toBeInTheDocument();
  });

  it('always renders the JDBC URL, driver, and port inputs in both modes', () => {
    renderSelector();
    expect(screen.getByText('JDBC URL')).toBeInTheDocument();
    expect(screen.getByText('JDBC Driver')).toBeInTheDocument();
    expect(screen.getByText('Port')).toBeInTheDocument();
  });

  it('switching modes clears mode-private fields but preserves cross-mode shared fields', () => {
    // P20 — cross-mode fields (`secret_arn`, `jdbc_url`, `jdbc_driver`,
    // `engine`) are preserved on every transition so the user does not
    // lose typed-in values. Mode-private fields are still cleared.
    // Validates: P20 Req 1.3, 1.4, 12.1.
    const { onChange } = renderSelector({
      _connection_mode: 'select_rds',
      _selected_rds: 'instance:pg-instance',
      engine: 'postgres',
      jdbc_url: 'jdbc:postgresql://x:5432/y',
      jdbc_driver: 'org.postgresql.Driver',
      _port: '5432',
      secret_arn: 'arn:aws:secretsmanager:...',
      database_name: 'mydb',
    });

    // Click the "Use Glue Connection" tile.
    fireEvent.click(screen.getByText('Use Glue Connection'));

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next._connection_mode).toBe('glue_connection');
    // Mode-private fields cleared:
    expect(next._selected_rds).toBeUndefined();
    expect(next._port).toBeUndefined();
    // Cross-mode fields preserved (P20 Req 1.3, 1.4):
    expect(next.engine).toBe('postgres');
    expect(next.jdbc_url).toBe('jdbc:postgresql://x:5432/y');
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
    expect(next.secret_arn).toBe('arn:aws:secretsmanager:...');
  });
});

/* ---------- Auto-population from RDS / Aurora / Glue selection ---------- */

describe('JdbcConnectionSelector — auto-population', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('auto-populates engine, URL, driver, and port when an RDS instance is selected', () => {
    const { onChange, container } = renderSelector({ _connection_mode: 'select_rds' });

    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="jdbc-conn-rds-select"]')!;
    select.openDropdown();
    select.selectOptionByValue('instance:pg-instance');

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next.engine).toBe('postgres');
    expect(next.jdbc_url).toBe('jdbc:postgresql://pg.xxx.us-west-2.rds.amazonaws.com:5432/mydb');
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
    expect(next._port).toBe('5432');
    expect(next.db_instance_identifier).toBe('pg-instance');
    expect(next.database_name).toBe('mydb');
  });

  it('auto-populates correctly when an Aurora cluster is selected', () => {
    const { onChange, container } = renderSelector({ _connection_mode: 'select_rds' });

    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="jdbc-conn-rds-select"]')!;
    select.openDropdown();
    select.selectOptionByValue('cluster:aurora-cluster');

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next.engine).toBe('aurora-mysql');
    // aurora-mysql uses the mysql protocol per JDBC_DRIVER_MAP
    expect(next.jdbc_url).toBe('jdbc:mysql://aurora.xxx.us-west-2.rds.amazonaws.com:3306/auroradb');
    expect(next.jdbc_driver).toBe('com.mysql.cj.jdbc.Driver');
    expect(next._port).toBe('3306');
    // Aurora clusters set db_cluster_identifier (not db_instance_identifier)
    expect(next.db_cluster_identifier).toBe('aurora-cluster');
    expect(next.db_instance_identifier).toBeUndefined();
    expect(next.database_name).toBe('auroradb');
  });

  it('auto-populates URL and driver from a Glue connection selection', () => {
    const { onChange, container } = renderSelector({ _connection_mode: 'glue_connection' });

    const wrapper = createWrapper(container);
    const select = wrapper.findSelect('[data-testid="jdbc-conn-glue-select"]')!;
    select.openDropdown();
    select.selectOptionByValue('my-glue-conn');

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next.glue_connection_name).toBe('my-glue-conn');
    expect(next.jdbc_url).toBe('jdbc:postgresql://glue-host:5432/glue_db');
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
  });
});

/* ---------- Manual override ---------- */

describe('JdbcConnectionSelector — manual override', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('allows manual override of jdbc_url after auto-population', () => {
    const { onChange } = renderSelector({
      _connection_mode: 'select_rds',
      jdbc_url: 'jdbc:postgresql://auto-host:5432/autodb',
      jdbc_driver: 'org.postgresql.Driver',
      _port: '5432',
    });

    const urlInput = screen.getByTestId('jdbc-conn-jdbc-url').querySelector('input')!;
    fireEvent.change(urlInput, {
      target: { value: 'jdbc:postgresql://manual-host:6543/manualdb' },
    });

    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next.jdbc_url).toBe('jdbc:postgresql://manual-host:6543/manualdb');
    // Other fields are preserved
    expect(next.jdbc_driver).toBe('org.postgresql.Driver');
    expect(next._port).toBe('5432');
    expect(next._connection_mode).toBe('select_rds');
  });

  it('allows manual override of jdbc_driver and port', () => {
    const { onChange } = renderSelector({
      _connection_mode: 'select_rds',
      jdbc_url: 'jdbc:postgresql://host:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
      _port: '5432',
    });

    const driverInput = screen.getByTestId('jdbc-conn-jdbc-driver').querySelector('input')!;
    fireEvent.change(driverInput, { target: { value: 'com.example.CustomDriver' } });
    expect(onChange).toHaveBeenCalled();
    let next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next.jdbc_driver).toBe('com.example.CustomDriver');

    const portInput = screen.getByTestId('jdbc-conn-port').querySelector('input')!;
    fireEvent.change(portInput, { target: { value: '6543' } });
    next = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(next._port).toBe('6543');
  });
});
