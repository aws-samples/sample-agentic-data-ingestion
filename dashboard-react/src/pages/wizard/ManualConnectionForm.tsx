// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Multiselect from '@cloudscape-design/components/multiselect';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import Toggle from '@cloudscape-design/components/toggle';
import {
  useVpcs,
  useSubnets,
  useSecurityGroups,
  useJdbcCandidateSecrets,
} from '../../hooks';
import type { Subnet } from '../../types';
import { JDBC_DRIVER_MAP } from './jdbc-driver-map';
import { selectFiltering } from '../../components/common/select-async-status';
import {
  NO_JDBC_CANDIDATE_SECRETS,
  NO_VPCS,
  noSecurityGroupsEmpty,
  noSubnetsEmpty,
} from '../../utils/resource-empty-states';

// ---------------------------------------------------------------------------
// P20 — ManualConnectionForm
// ---------------------------------------------------------------------------

/**
 * Per-engine placeholder text for the JDBC URL input in manual mode.
 * Used by `<ManualConnectionForm>` to give the user a worked example
 * matching their selected Engine. Falls back to the Postgres shape if
 * the engine is unknown.
 */
function jdbcUrlPlaceholder(engine: string): string {
  const mapping = JDBC_DRIVER_MAP[engine];
  if (!mapping) return 'jdbc:postgresql://host:5432/mydb';
  switch (mapping.protocol) {
    case 'postgresql':
      return 'jdbc:postgresql://host:5432/mydb';
    case 'mysql':
      return 'jdbc:mysql://host:3306/mydb';
    case 'mariadb':
      return 'jdbc:mariadb://host:3306/mydb';
    case 'oracle:thin:@':
      return 'jdbc:oracle:thin:@host:1521:ORCL';
    case 'sqlserver':
      return 'jdbc:sqlserver://host:1433;databaseName=mydb';
    default:
      return `jdbc:${mapping.protocol}://host:${mapping.port}/mydb`;
  }
}

export interface ManualConnectionFormProps {
  config: Record<string, string>;
  onChange: (config: Record<string, string>) => void;
  testIdPrefix: string;
}

/**
 * P20 — ManualConnectionForm.
 *
 * Renders the manual-mode inputs: Engine `<Select>`, JDBC URL `<Input>`,
 * Driver Class `<Input>` (auto-populated from Engine), VPC / Subnet /
 * Security Group dropdowns (filtered to the chosen VPC), and a Secret
 * picker with a "Paste ARN manually" toggle (Req 4.1-4.5).
 *
 * The component is rendered ONLY when `connectionMode === 'manual'`. It
 * shares the parent's `config` / `onChange` props so the parent owns the
 * persistence boundary; the only local state is the secret-picker
 * toggle (Req 4.2 — defaults to `false`).
 *
 * Extracted from `JdbcConnectionSelector.tsx` (P29 R11.5 decomposition) with
 * no behavior change.
 *
 * Validates: P20 Requirements 2.1–2.8, 3.1–3.9, 4.1–4.5.
 */
export function ManualConnectionForm({
  config,
  onChange,
  testIdPrefix,
}: ManualConnectionFormProps) {
  // Hooks — these only mount when the component renders, so we don't
  // pay the network cost for users who pick `select_rds` or
  // `glue_connection` (Req 10.2, 10.3 enable-skip).
  // P29 R1.2/R1.3 — these resource fetchers now throw on error; each dropdown
  // renders an inline <Alert> with a Retry button that re-triggers the query.
  const {
    data: vpcs,
    isLoading: vpcsLoading,
    error: vpcsError,
    refetch: refetchVpcs,
  } = useVpcs();
  const {
    data: subnets,
    isLoading: subnetsLoading,
    error: subnetsError,
    refetch: refetchSubnets,
  } = useSubnets(config.vpc_id);
  const {
    data: sgs,
    isLoading: sgsLoading,
    error: sgsError,
    refetch: refetchSgs,
  } = useSecurityGroups(config.vpc_id);
  const {
    data: candidateSecrets,
    isLoading: candidateSecretsLoading,
    error: candidateSecretsError,
    refetch: refetchCandidateSecrets,
  } = useJdbcCandidateSecrets();

  // Local toggle for the Secret picker (Req 4.2 — defaults to false).
  const [pasteSecretManually, setPasteSecretManually] = useState<boolean>(
    false,
  );

  // Req 3.2 — VPC pre-selection rule. When the VPC list loads AND
  // `config.vpc_id` is unset, pre-select the AgenticDataIngestion VPC if
  // discoverable (matched by `Name` tag prefix `'AgenticDataIngestion-'`),
  // otherwise the alphabetically-first VPC sorted by `name`.
  useEffect(() => {
    if (config.vpc_id) return;
    const vpcsList = vpcs ?? [];
    if (vpcsList.length === 0) return;

    const adiVpc = vpcsList.find((v) =>
      v.name.startsWith('AgenticDataIngestion-'),
    );
    let chosen = adiVpc;
    if (!chosen) {
      const sorted = [...vpcsList].sort((a, b) => {
        const aLabel = a.name || a.vpc_id;
        const bLabel = b.name || b.vpc_id;
        return aLabel.localeCompare(bLabel);
      });
      chosen = sorted[0];
    }
    if (chosen) {
      onChange({ ...config, vpc_id: chosen.vpc_id });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vpcs]);

  function update(patch: Record<string, string>) {
    onChange({ ...config, ...patch });
  }

  // Req 2.2 — auto-populate jdbc_driver when Engine changes; do NOT
  // mutate jdbc_url (Req 2.8).
  function handleEngineSelect(engine: string) {
    const mapping = JDBC_DRIVER_MAP[engine];
    update({
      engine,
      jdbc_driver: mapping?.driver ?? '',
    });
  }

  // Req 3.5 — when a subnet is selected, persist both subnet_id and
  // availability_zone (the latter is forwarded into
  // PhysicalConnectionRequirements by the backend).
  function handleSubnetSelect(subnetId: string) {
    const subnet = (subnets ?? []).find((s) => s.subnet_id === subnetId);
    if (!subnet) return;
    update({
      subnet_id: subnet.subnet_id,
      availability_zone: subnet.availability_zone,
    });
  }

  // Req 3.6 — security_group_ids is JSON-encoded for transport because
  // the wizard's WizardState shape is Record<string, string>. The
  // backend's `_normalise_sg_list` parses it back into list[str].
  function handleSecurityGroupsChange(sgIds: string[]) {
    update({ security_group_ids: JSON.stringify(sgIds) });
  }

  // Derive the current SG selection from the JSON-encoded persisted
  // value. Empty / missing = no selection.
  let currentSgIds: string[] = [];
  if (config.security_group_ids) {
    try {
      const parsed = JSON.parse(config.security_group_ids);
      if (Array.isArray(parsed)) currentSgIds = parsed.filter(
        (s): s is string => typeof s === 'string',
      );
    } catch {
      currentSgIds = [];
    }
  }

  const engineOptions = Object.keys(JDBC_DRIVER_MAP).map((e) => ({
    value: e,
    label: e,
  }));

  const vpcOptions = (vpcs ?? []).map((v) => ({
    value: v.vpc_id,
    label: v.name || v.vpc_id,
    description: `${v.cidr}${v.is_default ? ' (default)' : ''}`,
  }));

  const subnetOptions = (subnets ?? []).map((s: Subnet) => ({
    value: s.subnet_id,
    label: s.name || s.subnet_id,
    description: `${s.availability_zone} — ${s.cidr}`,
  }));

  const sgOptions = (sgs ?? []).map((g) => ({
    value: g.sg_id,
    label: g.name,
    description: g.description,
  }));

  const secretOptions = (candidateSecrets ?? []).map((s) => ({
    value: s.arn,
    label: s.name,
    description: s.description || s.arn,
  }));

  const selectedEngine = config.engine
    ? engineOptions.find((o) => o.value === config.engine) ?? null
    : null;
  const selectedVpc = config.vpc_id
    ? vpcOptions.find((o) => o.value === config.vpc_id) ?? null
    : null;
  const selectedSubnet = config.subnet_id
    ? subnetOptions.find((o) => o.value === config.subnet_id) ?? null
    : null;
  const selectedSecret = config.secret_arn
    ? secretOptions.find((o) => o.value === config.secret_arn) ?? null
    : null;

  return (
    <SpaceBetween size="s">
      <FormField label="Engine">
        <Select
          selectedOption={selectedEngine}
          onChange={({ detail }) => handleEngineSelect(detail.selectedOption.value!)}
          options={engineOptions}
          placeholder="Select a database engine"
          data-testid={`${testIdPrefix}-manual-engine`}
        />
      </FormField>

      <FormField label="JDBC URL">
        <Input
          value={config.jdbc_url ?? ''}
          onChange={({ detail }) => update({ jdbc_url: detail.value })}
          placeholder={jdbcUrlPlaceholder(config.engine ?? '')}
          data-testid={`${testIdPrefix}-manual-jdbc-url`}
        />
      </FormField>

      <FormField
        label="Driver Class"
        description="Auto-populated from Engine; override only if you know the driver class differs"
      >
        <Input
          value={config.jdbc_driver ?? ''}
          onChange={({ detail }) => update({ jdbc_driver: detail.value })}
          placeholder="org.postgresql.Driver"
          data-testid={`${testIdPrefix}-manual-jdbc-driver`}
        />
      </FormField>

      <FormField label="VPC">
        {vpcsError ? (
          <Alert
            type="error"
            data-testid={`${testIdPrefix}-manual-vpc-error`}
            action={
              <Button
                onClick={() => {
                  void refetchVpcs?.();
                }}
                data-testid={`${testIdPrefix}-manual-vpc-retry`}
              >
                Retry
              </Button>
            }
          >
            Failed to load VPCs: {String(vpcsError)}
          </Alert>
        ) : vpcsLoading ? (
          <Spinner />
        ) : (
          <Select
            selectedOption={selectedVpc}
            onChange={({ detail }) => {
              // Clear subnet + SG when VPC changes (Req 3.3 / 3.4 — the
              // dropdowns will be disabled until the new VPC's data
              // loads).
              const next: Record<string, string> = {
                ...config,
                vpc_id: detail.selectedOption.value!,
              };
              delete next.subnet_id;
              delete next.security_group_ids;
              delete next.availability_zone;
              onChange(next);
            }}
            options={vpcOptions}
            placeholder="Select a VPC"
            data-testid={`${testIdPrefix}-manual-vpc`}
            // Loading and failure are the Spinner and Alert above, so this
            // Select only ever renders a finished list: `empty` is the
            // finished-and-empty case, which used to open a blank popover.
            empty={NO_VPCS}
            {...selectFiltering('VPCs', vpcOptions.length)}
          />
        )}
      </FormField>

      <FormField label="Subnet">
        {subnetsError ? (
          <Alert
            type="error"
            data-testid={`${testIdPrefix}-manual-subnet-error`}
            action={
              <Button
                onClick={() => {
                  void refetchSubnets?.();
                }}
                data-testid={`${testIdPrefix}-manual-subnet-retry`}
              >
                Retry
              </Button>
            }
          >
            Failed to load subnets: {String(subnetsError)}
          </Alert>
        ) : subnetsLoading ? (
          <Spinner />
        ) : (
          <Select
            selectedOption={selectedSubnet}
            onChange={({ detail }) => handleSubnetSelect(detail.selectedOption.value!)}
            options={subnetOptions}
            placeholder={
              config.vpc_id ? 'Select a subnet' : 'Select a VPC first'
            }
            disabled={!config.vpc_id}
            data-testid={`${testIdPrefix}-manual-subnet`}
            empty={config.vpc_id ? noSubnetsEmpty(config.vpc_id) : undefined}
            {...selectFiltering('subnets', subnetOptions.length)}
          />
        )}
      </FormField>

      <FormField label="Security Groups">
        {sgsError ? (
          <Alert
            type="error"
            data-testid={`${testIdPrefix}-manual-sg-error`}
            action={
              <Button
                onClick={() => {
                  void refetchSgs?.();
                }}
                data-testid={`${testIdPrefix}-manual-sg-retry`}
              >
                Retry
              </Button>
            }
          >
            Failed to load security groups: {String(sgsError)}
          </Alert>
        ) : sgsLoading ? (
          <Spinner />
        ) : (
          <Multiselect
            selectedOptions={sgOptions.filter((o) =>
              currentSgIds.includes(o.value),
            )}
            onChange={({ detail }) =>
              handleSecurityGroupsChange(
                detail.selectedOptions.map((o) => o.value!),
              )
            }
            options={sgOptions}
            placeholder={
              config.vpc_id
                ? 'Select one or more security groups'
                : 'Select a VPC first'
            }
            disabled={!config.vpc_id}
            data-testid={`${testIdPrefix}-manual-sg`}
            empty={config.vpc_id ? noSecurityGroupsEmpty(config.vpc_id) : undefined}
            {...selectFiltering('security groups', sgOptions.length)}
          />
        )}
      </FormField>

      <FormField label="Secret (Credentials)">
        <SpaceBetween size="xxs">
          {!pasteSecretManually && (
            candidateSecretsError ? (
              <Alert
                type="error"
                data-testid={`${testIdPrefix}-manual-secret-error`}
                action={
                  <Button
                    onClick={() => {
                      void refetchCandidateSecrets?.();
                    }}
                    data-testid={`${testIdPrefix}-manual-secret-retry`}
                  >
                    Retry
                  </Button>
                }
              >
                Failed to load secrets: {String(candidateSecretsError)}
              </Alert>
            ) : candidateSecretsLoading ? (
              <Spinner />
            ) : (
              <Select
                selectedOption={selectedSecret}
                onChange={({ detail }) => update({ secret_arn: detail.selectedOption.value! })}
                options={secretOptions}
                placeholder="Select a Secrets Manager secret"
                data-testid={`${testIdPrefix}-manual-secret-select`}
                empty={NO_JDBC_CANDIDATE_SECRETS}
                {...selectFiltering('secrets', secretOptions.length)}
              />
            )
          )}
          {pasteSecretManually && (
            <Input
              value={config.secret_arn ?? ''}
              onChange={({ detail }) => update({ secret_arn: detail.value })}
              placeholder="arn:aws:secretsmanager:<region>:<account>:secret:<name>-<suffix>"
              data-testid={`${testIdPrefix}-manual-secret-arn`}
            />
          )}
          <Toggle
            checked={pasteSecretManually}
            onChange={({ detail }) => setPasteSecretManually(detail.checked)}
            data-testid={`${testIdPrefix}-manual-secret-toggle`}
          >
            Paste ARN manually
          </Toggle>
        </SpaceBetween>
      </FormField>
    </SpaceBetween>
  );
}
