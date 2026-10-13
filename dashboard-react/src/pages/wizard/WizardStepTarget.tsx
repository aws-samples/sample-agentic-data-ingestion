// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import Table from '@cloudscape-design/components/table';
import Spinner from '@cloudscape-design/components/spinner';
import { apiClient } from '../../services/api-client';
import type { WizardStepProps, SchemaField } from './types';
import { TargetForm } from './TargetForms';
import { JdbcTargetDetectPanel } from './JdbcTargetDetectPanel';
import { canDetectSchema } from './target-detect';

// P17 Task 20.2/20.3 — Validates: Requirements 14.1, 14.2, 14.3.
// The target-type selector now collapses the legacy `jdbc` and `rds_database`
// options into a single "JDBC Database (RDS, Aurora, or self-managed)" entry
// with value `'jdbc'`. The canonical JDBC helpers live in
// `JdbcConnectionSelector.tsx` and `JdbcTargetForm.tsx`; the target subforms
// live in `TargetForms.tsx`; the JDBC detect panel lives in
// `JdbcTargetDetectPanel.tsx`; and `canDetectSchema` lives in
// `target-detect.ts` (P29 R11.5 decomposition — split for size,
// behavior-preserving).

// Re-export `canDetectSchema` from its extracted module so the existing import
// path (`../WizardStepTarget`) stays stable for tests and other consumers.
export { canDetectSchema } from './target-detect';

const TARGET_TYPE_OPTIONS = [
  { value: 's3_tables', label: 'S3 Table Bucket', description: 'Apache Iceberg tables in S3 Table Buckets' },
  // No longer offered for new pipelines; listed only while already selected —
  // see `RETIRED_TARGET_TYPES` below.
  //
  // The one target type that does NOT create its destination. The wording below
  // is deliberately the same wording the `detect_schema` pre-flight uses in its
  // 400s (`dashboard_api/handlers/resources.py`, `GLUE_CATALOG target pre-flight`)
  // so a user who reads this tile and then hits a refusal recognises it as the
  // same rule rather than a second one. The full detail — including which buckets
  // are writable — is in `GlueCatalogForm`'s help text in `TargetForms.tsx`.
  {
    value: 'glue_catalog',
    label: 'Glue Catalog (existing table)',
    description:
      'Maps to a table that already exists — the pipeline does not create catalog tables. The database must be named agentic_data_ingestion_*, and the table\u2019s S3 location must be in a bucket the pipeline can write and does not watch for uploads.',
  },
  {
    value: 'jdbc',
    label: 'JDBC Database (RDS, Aurora, or self-managed)',
    description: 'Postgres, MySQL/MariaDB, Oracle, or SQL Server — RDS, Aurora, or self-managed',
  },
  { value: 's3_parquet', label: 'S3 Parquet', description: 'Parquet files in S3' },
];

// Target types that are no longer offered for a new pipeline. `create_pipeline`
// refuses them with a 400 (`pipelines_crud.py`, the `GLUE_CATALOG` branch), so
// listing one would only lead the user to a refusal at the last step. An S3
// Table Bucket or S3 Parquet target creates its own destination and needs none
// of the rules the Glue Catalog tile above describes.
//
// The entry stays in `TARGET_TYPE_OPTIONS` rather than being deleted, and is
// still listed when it is the CURRENT value, so a state hydrated from an
// existing Glue Catalog pipeline renders its selection and its form instead of
// an empty select. Existing Glue Catalog pipelines keep running unchanged.
const RETIRED_TARGET_TYPES: ReadonlySet<string> = new Set(['glue_catalog']);

/** The target types to list: every current one, plus a retired one only while it is selected. */
export function offeredTargetTypeOptions(currentTargetType: string) {
  return TARGET_TYPE_OPTIONS.filter(
    (o) => !RETIRED_TARGET_TYPES.has(o.value) || o.value === currentTargetType,
  );
}

const WRITE_MODE_OPTIONS = [
  { value: 'append', label: 'Append', description: 'Add new records, keep existing data' },
  { value: 'overwrite', label: 'Overwrite', description: 'Replace all existing data on each run' },
  { value: 'merge', label: 'Merge (Upsert)', description: 'Update matching rows, insert new ones (Iceberg only)' },
];

export function WizardStepTarget({ state, onChange }: WizardStepProps) {
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState<string | null>(null);

  const showDetect = canDetectSchema(state);

  async function handleDetectSchema() {
    // Pre-P19 path UNCHANGED for s3_tables / glue_catalog targets.
    // Validates: Requirement 1.3.
    setDetecting(true);
    setDetectError(null);
    try {
      const result = await apiClient.get<{ schema: SchemaField[] }>(
        `/resources/detect-schema?target_type=${encodeURIComponent(state.targetType)}&target_config=${encodeURIComponent(JSON.stringify(state.targetConfig))}`,
      );
      onChange({
        ...state,
        // Decision 1 — write both the new canonical field and the
        // deprecated alias so legacy consumers continue to see the
        // target-side schema during the migration window. Validates: 1.4.
        detectedTargetSchema: result.schema,
        detectedSchema: result.schema,
        schemaSource: 'detected',
        schemaSourceExplicitlySet: true,
        schemaFields: result.schema,
      });
    } catch (err) {
      // Requirement 1.5 — error path mutates only the inline alert.
      setDetectError(err instanceof Error ? err.message : 'Failed to detect schema');
    } finally {
      setDetecting(false);
    }
  }

  return (
    <Container header={<Header variant="h2">Target Destination</Header>}>
      <SpaceBetween size="l">
        <FormField label="Target Type" description="Where should the pipeline write data?">
          <Select
            selectedOption={TARGET_TYPE_OPTIONS.find((o) => o.value === state.targetType) ?? null}
            onChange={({ detail }) => {
              const newType = detail.selectedOption.value as WizardStepProps['state']['targetType'];
              // P19 Task 7.3 — Validates: Requirements 3.5, 6.6.
              // Reset the explicit-choice flag and the target-side
              // detected schema so the WizardStepSchema pre-selection
              // rule fires again on the next visit to Step 3 and so a
              // stale target schema from a previous target type does
              // not leak through.
              onChange({
                ...state,
                targetType: newType,
                targetConfig: {},
                detectedSchema: null,
                detectedTargetSchema: null,
                schemaSourceExplicitlySet: false,
              });
            }}
            options={offeredTargetTypeOptions(state.targetType)}
            data-testid="target-type-select"
          />
        </FormField>

        <TargetForm state={state} onChange={onChange} />

        {/*
          P19 Task 7.4 — Validates: Requirements 6.1, 6.2.
          The legacy target-agnostic write_mode `<Select>` is hidden for
          JDBC targets — the `<Tiles>` selector inside `JdbcTargetForm`
          (Task 8) is the source of truth for JDBC pipelines. Non-JDBC
          target types keep the existing parent `<Select>` unchanged.
        */}
        {state.targetType !== 'jdbc' && (
          <FormField label="Write Mode" description="How should new data be written to the target?">
            <Select
              selectedOption={
                WRITE_MODE_OPTIONS.find((o) => o.value === (state.targetConfig.write_mode ?? 'append')) ?? WRITE_MODE_OPTIONS[0]
              }
              onChange={({ detail }) => {
                const newConfig: Record<string, string> = {
                  ...state.targetConfig,
                  write_mode: detail.selectedOption.value!,
                };
                if (detail.selectedOption.value !== 'merge') {
                  delete newConfig.merge_key;
                }
                onChange({
                  ...state,
                  targetConfig: newConfig,
                });
              }}
              options={
                state.targetType === 's3_tables'
                  ? WRITE_MODE_OPTIONS
                  : WRITE_MODE_OPTIONS.filter((o) => o.value !== 'merge')
              }
              data-testid="write-mode-select"
            />
          </FormField>
        )}

        {state.targetType !== 'jdbc' && state.targetConfig.write_mode === 'merge' && (
          <FormField label="Merge Key" description="Column to match existing rows for upsert (Iceberg only)">
            <Input
              value={state.targetConfig.merge_key ?? ''}
              onChange={({ detail }) =>
                onChange({ ...state, targetConfig: { ...state.targetConfig, merge_key: detail.value } })
              }
              placeholder="e.g. customer_id"
            />
          </FormField>
        )}

        {/*
          JDBC target Detect Schema flow lives in a sub-component so its
          React Query hooks (`useJdbcDetectTargetSchemaStart` /
          `useJdbcDetectTargetSchemaStatus`) only mount when the user has
          chosen a JDBC target. Validates: Requirements 1.2, 1.4, 1.5.
        */}
        {state.targetType === 'jdbc' ? (
          <JdbcTargetDetectPanel state={state} onChange={onChange} />
        ) : (
          <>
            {showDetect && (
              <SpaceBetween size="s">
                <Button
                  onClick={handleDetectSchema}
                  loading={detecting}
                  disabled={detecting}
                  data-testid="detect-schema-button"
                >
                  {detecting ? 'Detecting...' : 'Detect Schema'}
                </Button>
                {detecting && <Spinner />}
              </SpaceBetween>
            )}

            {detectError && <Alert type="error" data-testid="detect-error">{detectError}</Alert>}

            {state.detectedTargetSchema && state.detectedTargetSchema.length > 0 && (
              <SpaceBetween size="s">
                <Alert type="success" data-testid="detect-success">
                  Schema detected: {state.detectedTargetSchema.length} field{state.detectedTargetSchema.length !== 1 ? 's' : ''} found
                </Alert>
                <Table
                  data-testid="detected-schema-preview"
                  columnDefinitions={[
                    { id: 'name', header: 'Field Name', cell: (f) => f.name },
                    { id: 'type', header: 'Type', cell: (f) => f.type },
                    { id: 'nullable', header: 'Nullable', cell: (f) => f.nullable ? 'Yes' : 'No' },
                    { id: 'description', header: 'Description', cell: (f) => f.description || '—' },
                  ]}
                  items={state.detectedTargetSchema}
                  variant="embedded"
                />
              </SpaceBetween>
            )}
          </>
        )}
      </SpaceBetween>
    </Container>
  );
}
