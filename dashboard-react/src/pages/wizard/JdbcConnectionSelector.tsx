// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * JdbcConnectionSelector — shared connection selector for JDBC source and target forms.
 *
 * Validates: P17 Requirements 2.1-2.9, 14.2
 *
 * Encapsulates the connection-mode toggle, RDS dropdown (instances + clusters merged),
 * Glue connection dropdown, Secrets Manager dropdown, and JDBC URL/driver/port inputs
 * with auto-population from `JDBC_DRIVER_MAP` (on RDS selection) or from a Glue
 * connection's properties.
 *
 * The component is fully driven by the parent's `config` / `onChange` props — there is no
 * local state for any persisted field. The parent owns the source/target config record.
 *
 * Per the design's `connection_mode` values, this component uses `'select_rds'` and
 * `'glue_connection'` (the values referenced in P17 spec § 2 — these differ from the
 * P14 `'rds_instance'` value, which is intentionally being renamed as part of the
 * consolidation).
 *
 * The manual-mode subform (`ManualConnectionForm`) and the `JDBC_DRIVER_MAP` /
 * `buildJdbcUrl` / `JdbcConnectionMode` primitives live in sibling modules
 * (`ManualConnectionForm.tsx`, `jdbc-driver-map.ts`) after the P29 R11.5
 * size decomposition. They are re-exported below so existing importers keep
 * working unchanged (behavior-preserving split).
 */

import Alert from '@cloudscape-design/components/alert';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tiles from '@cloudscape-design/components/tiles';
import {
  useRdsInstances,
  useRdsClusters,
  useRdsGlueConnections,
  useRdsSecrets,
} from '../../hooks';
import type { RdsInstance, RdsCluster } from '../../types';
import { selectAsyncStatus, selectFiltering } from '../../components/common/select-async-status';
import { NO_GLUE_JDBC_CONNECTIONS, NO_RDS_DATABASES, NO_RDS_SECRETS } from '../../utils/resource-empty-states';
import { JDBC_DRIVER_MAP, buildJdbcUrl, type JdbcConnectionMode } from './jdbc-driver-map';
import { ManualConnectionForm } from './ManualConnectionForm';

// Re-export the JDBC connection primitives from their extracted modules so the
// existing import path (`./JdbcConnectionSelector`) stays stable for tests and
// other consumers.
export { JDBC_DRIVER_MAP, buildJdbcUrl } from './jdbc-driver-map';
export type { JdbcConnectionMode } from './jdbc-driver-map';

export interface JdbcConnectionSelectorProps {
  /** The parent's source or target config record. The selector reads from and writes to it. */
  config: Record<string, string>;
  /** Replacement callback — receives a new config record (parent fully owns persistence). */
  onChange: (config: Record<string, string>) => void;
  /**
   * Optional `data-testid` prefix so the component can be rendered twice (source + target)
   * on the same wizard page without test-id collisions. Defaults to `'jdbc-conn'`.
   */
  testIdPrefix?: string;
}

type MergedRdsOption = {
  value: string;
  label: string;
  description: string;
  engine: string;
  endpoint: string;
  port: number;
  dbName: string;
  isCluster: boolean;
  subnetIds: string[];
  securityGroupIds: string[];
  availabilityZone: string;
};

export function JdbcConnectionSelector({
  config,
  onChange,
  testIdPrefix = 'jdbc-conn',
}: JdbcConnectionSelectorProps) {
  const connectionMode: JdbcConnectionMode =
    (config._connection_mode as JdbcConnectionMode | undefined) ?? 'select_rds';

  // P29 R1.2/R1.3 — keep the full query objects so a failed resource load
  // surfaces an inline error + Retry in the dropdown instead of an empty list.
  const instancesQuery = useRdsInstances();
  const clustersQuery = useRdsClusters();
  const glueConnectionsQuery = useRdsGlueConnections();
  const secretsQuery = useRdsSecrets();
  const { data: instances } = instancesQuery;
  const { data: clusters } = clustersQuery;
  const { data: glueConnections } = glueConnectionsQuery;
  const { data: secrets } = secretsQuery;

  // The RDS dropdown merges two queries; treat it as failed/loading if either
  // underlying query is, and retry both on recovery.
  const rdsQuery = {
    isLoading: !!instancesQuery.isLoading || !!clustersQuery.isLoading,
    isError: !!instancesQuery.isError || !!clustersQuery.isError,
    refetch: () => {
      void instancesQuery.refetch?.();
      void clustersQuery.refetch?.();
    },
  };

  // Merge instances + clusters into one dropdown
  const instanceOptions: MergedRdsOption[] = (instances ?? []).map((i: RdsInstance) => ({
    value: `instance:${i.db_instance_identifier}`,
    label: i.db_instance_identifier,
    description: `${i.engine} ${i.engine_version} — ${i.endpoint_address}`,
    engine: i.engine,
    endpoint: i.endpoint_address,
    port: i.endpoint_port,
    dbName: i.db_name,
    isCluster: false,
    subnetIds: i.subnet_ids ?? [],
    securityGroupIds: i.security_group_ids ?? [],
    availabilityZone: i.availability_zone ?? '',
  }));
  const clusterOptions: MergedRdsOption[] = (clusters ?? []).map((c: RdsCluster) => ({
    value: `cluster:${c.db_cluster_identifier}`,
    label: c.db_cluster_identifier,
    description: `${c.engine} ${c.engine_version} — ${c.endpoint}`,
    engine: c.engine,
    endpoint: c.endpoint,
    port: c.port,
    dbName: c.database_name,
    isCluster: true,
    subnetIds: c.subnet_ids ?? [],
    securityGroupIds: c.security_group_ids ?? [],
    availabilityZone: c.availability_zone ?? '',
  }));
  const allRdsOptions: MergedRdsOption[] = [...instanceOptions, ...clusterOptions];

  const glueOptions = (glueConnections ?? []).map((g) => ({
    value: g.name,
    label: g.name,
    description: g.jdbc_url,
    jdbcUrl: g.jdbc_url,
    jdbcDriver: g.jdbc_driver,
  }));

  const secretOptions = (secrets ?? []).map((s) => ({
    value: s.arn,
    label: s.name,
    description: s.description || s.arn,
  }));

  function update(patch: Record<string, string>) {
    onChange({ ...config, ...patch });
  }

  function handleModeChange(newMode: JdbcConnectionMode) {
    // P20 — Reset every connection-mode-private field on every transition.
    // The same delete-set covers both directions (entering manual ⇒ clear
    // select_rds + glue_connection fields; leaving manual ⇒ clear vpc_id /
    // subnet_id / security_group_ids / availability_zone).
    //
    // Cross-mode shared fields (`secret_arn`, `jdbc_url`, `jdbc_driver`,
    // `engine`) are deliberately NOT in this delete-set — switching modes
    // preserves the values that survive the new mode's required-field
    // shape so the user doesn't re-type them.
    //
    // Validates: P20 Requirements 1.3, 1.4, 12.1.
    const cleared: Record<string, string> = { ...config };
    // select_rds-private
    delete cleared._selected_rds;
    delete cleared.db_instance_identifier;
    delete cleared.db_cluster_identifier;
    delete cleared._port;
    // glue_connection-private
    delete cleared.glue_connection_name;
    // manual-private (P20)
    delete cleared.vpc_id;
    delete cleared.subnet_id;
    delete cleared.security_group_ids;
    delete cleared.availability_zone;
    cleared._connection_mode = newMode;
    onChange(cleared);
  }

  function handleRdsSelect(optionValue: string) {
    const selected = allRdsOptions.find((o) => o.value === optionValue);
    if (!selected) return;
    const mapping = JDBC_DRIVER_MAP[selected.engine];
    const jdbcUrl = buildJdbcUrl(selected.engine, selected.endpoint, selected.port, selected.dbName);
    const idKey = selected.isCluster ? 'db_cluster_identifier' : 'db_instance_identifier';
    // Pick the first subnet from the DBSubnetGroup. RDS instances live
    // in a single subnet (the chosen AZ); RDS subnet groups are
    // multi-AZ for failover so any of the listed subnets is reachable.
    // The wizard takes the first deterministically — operators who
    // need a different subnet can fall back to manual mode.
    const subnetId = selected.subnetIds[0] ?? '';
    update({
      _connection_mode: 'select_rds',
      _selected_rds: optionValue,
      [idKey]: selected.label,
      engine: selected.engine,
      jdbc_url: jdbcUrl,
      jdbc_driver: mapping?.driver ?? '',
      _port: String(mapping?.port ?? selected.port),
      database_name: selected.dbName,
      // Networking fields — required by the JDBC discovery /
      // connection-provisioning backends in select_rds mode.
      subnet_id: subnetId,
      security_group_ids: JSON.stringify(selected.securityGroupIds),
      availability_zone: selected.availabilityZone,
    });
  }

  function handleGlueConnectionSelect(optionValue: string) {
    const selected = glueOptions.find((o) => o.value === optionValue);
    if (!selected) return;
    update({
      _connection_mode: 'glue_connection',
      glue_connection_name: selected.value,
      jdbc_url: selected.jdbcUrl,
      jdbc_driver: selected.jdbcDriver,
    });
  }

  // P31 R10.1/R10.2/R10.5 — the connection-mode tiles used to render as a bare
  // <Tiles> directly under the step's "Source Type" / "Target Type" tile group,
  // with no label of their own. Two radio groups then read as one continuous
  // 2×3 grid with two selections — a state a radio group cannot be in. The
  // group now lives in its own Container (visual separation) and its own
  // FormField (visible label, and — because Cloudscape's Tiles reads
  // `aria-labelledby` from FormFieldContext — a distinct accessible group
  // label, so a screen reader announces two choices instead of one list).
  // Both `JdbcSourceForm` and `JdbcTargetForm` inherit this; do NOT fork it.
  //
  // P31 R10.3/R10.4 — the three tiles are ordered and described by expected
  // use, not presented at equal weight. The three modes cost very different
  // amounts of work: `select_rds` derives engine, URL, driver and networking
  // from the RDS API, `glue_connection` reuses networking and credentials the
  // customer already configured, and `manual` is the only path that reaches an
  // on-prem, cross-account, or non-RDS database — at the price of seven
  // hand-entered fields. A flat presentation walked users into the hardest
  // path, so the cost is stated on the tile (before selection) and repeated as
  // an Alert once manual is chosen.
  //
  // The ORDER is deliberately left as easiest → hardest, which it already was.
  // Two prior constraints independently land on the same order: P20 Req 1.1
  // specifies `manual` as the THIRD tile, and `select_rds` is the default mode
  // (see `connectionMode` above), so it has to lead. Reordering would break one
  // and change which tile reads as the default. Only the copy changed here.
  return (
    <Container header={<Header variant="h3">Database Connection</Header>}>
      <SpaceBetween size="m">
        <FormField
          label="Connection Method"
          description="How should the pipeline connect to the database?"
        >
          <Tiles
            value={connectionMode}
            onChange={({ detail }) => handleModeChange(detail.value as JdbcConnectionMode)}
            items={[
              {
                // R10.3 — the common path. Everything but the database and its
                // secret is read from the RDS API, so this is the cheapest mode
                // and the one most users want.
                value: 'select_rds',
                label: 'Select RDS Instance',
                description:
                  'Most common. For RDS instances and Aurora clusters in this account. ' +
                  'Pick the database and its secret — engine, JDBC URL, driver and ' +
                  'networking are read from the RDS API.',
              },
              {
                // R10.3 — reuse path. The customer already configured the
                // networking and credentials on the Glue connection.
                value: 'glue_connection',
                label: 'Use Glue Connection',
                description:
                  'For a database you already reach through a Glue JDBC connection. Pick the ' +
                  'connection — its networking and credentials come with it.',
              },
              {
                // P20 Req 1.1 — third tile for external / self-managed databases.
                // P20 pinned this description verbatim; P31 R10.4 supersedes that
                // copy to state the seven-field cost up front. The value, the
                // label and the tile's third position are unchanged, and the
                // "on-prem / another AWS account / self-managed" phrasing is kept
                // in order (a test asserts it as a regex).
                value: 'manual',
                label: 'External or self-managed database',
                description:
                  'Needs seven fields: engine, JDBC URL, driver class, VPC, subnet, security ' +
                  'groups and secret. The only option for on-prem databases, databases in ' +
                  'another AWS account, or self-managed instances.',
              },
            ]}
            data-testid={`${testIdPrefix}-mode-tiles`}
          />
        </FormField>

        {connectionMode === 'select_rds' && (
          <SpaceBetween size="s">
            <FormField label="RDS Instance / Aurora Cluster">
              <Select
                selectedOption={
                  config._selected_rds
                    ? allRdsOptions.find((o) => o.value === config._selected_rds) ?? null
                    : null
                }
                onChange={({ detail }) => handleRdsSelect(detail.selectedOption.value!)}
                options={allRdsOptions}
                placeholder="Select an RDS instance or Aurora cluster"
                data-testid={`${testIdPrefix}-rds-select`}
                {...selectAsyncStatus(rdsQuery, 'RDS instances', NO_RDS_DATABASES)}
                {...selectFiltering('databases', allRdsOptions.length)}
              />
            </FormField>
            {/* REQUIRED in this mode, for both source and target. `select_rds`
                means AgenticDataIngestion creates the Glue Connection, and
                `create_glue_jdbc_connection` sets `SECRET_ID` only when
                `secret_arn` is present — otherwise the connection carries a URL
                and no credentials, which Glue rejects outright. Saying so inline
                is the difference between correcting a field here and reading a
                400 on the final Create step. */}
            <FormField
              label="Secret (Credentials)"
              description="The Secrets Manager secret holding this database's username and password"
              errorText={config.secret_arn ? undefined : 'A credentials secret is required'}
            >
              <Select
                selectedOption={
                  config.secret_arn
                    ? secretOptions.find((o) => o.value === config.secret_arn) ?? null
                    : null
                }
                onChange={({ detail }) => update({ secret_arn: detail.selectedOption.value! })}
                options={secretOptions}
                placeholder="Select a Secrets Manager secret"
                data-testid={`${testIdPrefix}-secret-select`}
                {...selectAsyncStatus(secretsQuery, 'secrets', NO_RDS_SECRETS)}
                {...selectFiltering('secrets', secretOptions.length)}
              />
            </FormField>
          </SpaceBetween>
        )}

        {connectionMode === 'glue_connection' && (
          <FormField label="Glue JDBC Connection">
            <Select
              selectedOption={
                config.glue_connection_name
                  ? glueOptions.find((o) => o.value === config.glue_connection_name) ?? null
                  : null
              }
              onChange={({ detail }) => handleGlueConnectionSelect(detail.selectedOption.value!)}
              options={glueOptions}
              placeholder="Select a Glue JDBC connection"
              data-testid={`${testIdPrefix}-glue-select`}
              {...selectAsyncStatus(glueConnectionsQuery, 'Glue connections', NO_GLUE_JDBC_CONNECTIONS)}
              {...selectFiltering('connections', glueOptions.length)}
            />
          </FormField>
        )}

        {/* P20 — Manual connection mode. Renders the ManualConnectionForm
            subcomponent which owns the Engine / JDBC URL / Driver Class
            inputs, the VPC / Subnet / Security Group dropdowns, and the
            Secret picker with manual-ARN toggle. The legacy URL / Driver /
            Port inputs (rendered below) are HIDDEN in manual mode because
            the ManualConnectionForm provides equivalent inputs that are
            mode-aware (Req 1.5). */}
        {connectionMode === 'manual' && (
          <SpaceBetween size="s">
            {/* P31 R10.4 — restate the cost at the top of the subform, so the
                effort is visible before the user starts typing rather than
                discovered field by field. Deliberately avoids repeating the
                other two tiles' label strings verbatim: those labels are
                asserted with `getByText`, which throws on more than one
                match. */}
            <Alert
              type="info"
              header="This mode needs seven values"
              data-testid={`${testIdPrefix}-manual-effort-notice`}
            >
              Engine, JDBC URL, driver class, VPC, subnet, security groups and secret — none
              of them can be discovered for you. If the database is an RDS instance or Aurora
              cluster in this account, or already has a Glue JDBC connection, one of the first
              two options above needs roughly one field instead.
            </Alert>
            <ManualConnectionForm
              config={config}
              onChange={onChange}
              testIdPrefix={testIdPrefix}
            />
          </SpaceBetween>
        )}

        {connectionMode !== 'manual' && (
          <>
            <FormField label="JDBC URL" description="Auto-populated; override if needed">
              <Input
                value={config.jdbc_url ?? ''}
                onChange={({ detail }) => update({ jdbc_url: detail.value })}
                placeholder="jdbc:postgresql://host:5432/mydb"
                data-testid={`${testIdPrefix}-jdbc-url`}
              />
            </FormField>
            <FormField label="JDBC Driver" description="Auto-populated; override if needed">
              <Input
                value={config.jdbc_driver ?? ''}
                onChange={({ detail }) => update({ jdbc_driver: detail.value })}
                placeholder="org.postgresql.Driver"
                data-testid={`${testIdPrefix}-jdbc-driver`}
              />
            </FormField>
            <FormField label="Port" description="Auto-populated; override if needed">
              <Input
                value={config._port ?? ''}
                onChange={({ detail }) => update({ _port: detail.value })}
                placeholder="5432"
                data-testid={`${testIdPrefix}-port`}
              />
            </FormField>
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default JdbcConnectionSelector;
