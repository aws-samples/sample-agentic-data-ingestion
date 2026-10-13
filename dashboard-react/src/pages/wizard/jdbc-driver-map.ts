// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Engine → JDBC driver/port/protocol mapping and the JDBC connection-mode
 * union, extracted from `JdbcConnectionSelector.tsx` (P29 R11.5 decomposition)
 * so both the selector and the `ManualConnectionForm` subcomponent can import
 * them without a circular dependency. Re-exported from
 * `JdbcConnectionSelector.tsx` so existing import paths stay stable.
 *
 * Co-located here so the shared selector is self-contained. The backend keeps
 * its own canonical copy in `shared/jdbc_helpers.py::JDBC_DRIVER_MAP`. The two
 * maps must stay in sync — see P17 Requirement 14.2 for the consolidation
 * rationale.
 */

export const JDBC_DRIVER_MAP: Record<string, { driver: string; port: number; protocol: string }> = {
  postgres:            { driver: 'org.postgresql.Driver', port: 5432, protocol: 'postgresql' },
  'aurora-postgresql': { driver: 'org.postgresql.Driver', port: 5432, protocol: 'postgresql' },
  mysql:               { driver: 'com.mysql.cj.jdbc.Driver', port: 3306, protocol: 'mysql' },
  'aurora-mysql':      { driver: 'com.mysql.cj.jdbc.Driver', port: 3306, protocol: 'mysql' },
  mariadb:             { driver: 'org.mariadb.jdbc.Driver', port: 3306, protocol: 'mariadb' },
  'oracle-ee':         { driver: 'oracle.jdbc.OracleDriver', port: 1521, protocol: 'oracle:thin:@' },
  'oracle-se2':        { driver: 'oracle.jdbc.OracleDriver', port: 1521, protocol: 'oracle:thin:@' },
  'sqlserver-ee':      { driver: 'com.microsoft.sqlserver.jdbc.SQLServerDriver', port: 1433, protocol: 'sqlserver' },
  'sqlserver-se':      { driver: 'com.microsoft.sqlserver.jdbc.SQLServerDriver', port: 1433, protocol: 'sqlserver' },
  'sqlserver-ex':      { driver: 'com.microsoft.sqlserver.jdbc.SQLServerDriver', port: 1433, protocol: 'sqlserver' },
  'sqlserver-web':     { driver: 'com.microsoft.sqlserver.jdbc.SQLServerDriver', port: 1433, protocol: 'sqlserver' },
};

/** Build a JDBC URL from engine, endpoint, port, and database name. */
export function buildJdbcUrl(engine: string, endpoint: string, port: number, dbName: string): string {
  const mapping = JDBC_DRIVER_MAP[engine];
  const protocol = mapping?.protocol ?? 'postgresql';
  return `jdbc:${protocol}://${endpoint}:${port}/${dbName}`;
}

export type JdbcConnectionMode = 'select_rds' | 'glue_connection' | 'manual';
