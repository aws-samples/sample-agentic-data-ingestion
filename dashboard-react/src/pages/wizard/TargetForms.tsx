// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tiles from '@cloudscape-design/components/tiles';
import {
  useS3TablesBuckets,
  useTablesInBucket,
  useGlueDatabases,
  useTablesInDatabase,
} from '../../hooks';
import type { WizardStepProps } from './types';
import { JdbcTargetForm } from './JdbcTargetForm';
import { selectAsyncStatus, selectFiltering } from '../../components/common/select-async-status';
import {
  NO_S3_TABLE_BUCKETS,
  NO_S3_TABLE_BUCKETS_WIZARD_NEXT_STEP,
  noS3TablesEmpty,
} from '../../utils/resource-empty-states';
// p66 R1.4 / R4.1 — the Glue Catalog target's eligibility rule and every word
// that explains a refusal live in ONE module, consumed here and by the edit
// modal's `GlueCatalogForm` (`components/TargetConfigForm.tsx`). Nothing below
// re-spells the prefix, the predicate or the copy: that duplication is exactly
// what R1.4 exists to prevent, and `LGR-4854b3d-1` is the ratified precedent
// for putting such an expression in `utils/` rather than at each call site.
import {
  GLUE_TARGET_EXCLUSION_NOTE,
  GLUE_TARGET_NONE_ELIGIBLE,
  GLUE_TARGET_NO_DATABASES,
  GLUE_TARGET_NO_TABLES_NEXT_STEP,
  glueTargetNoTablesEmpty,
  partitionGlueTargetDatabases,
} from '../../utils/glue-target-eligibility';

/* ---------- Validation helpers for "create new" mode ---------- */

const BUCKET_NAME_RE = /^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/;
const NAMESPACE_RE = /^[a-z0-9_]+$/;
const TABLE_NAME_RE = /^[a-z0-9_]+$/;

function validateBucket(v: string): string | undefined {
  if (!v) return 'Bucket name is required';
  if (v.length < 3 || v.length > 63) return 'Bucket name must be 3-63 characters';
  if (!BUCKET_NAME_RE.test(v)) return 'Only lowercase letters, numbers, and hyphens allowed';
  return undefined;
}

function validateNamespace(v: string): string | undefined {
  if (!v) return 'Namespace is required';
  if (!NAMESPACE_RE.test(v)) return 'Only lowercase letters, numbers, and underscores allowed';
  return undefined;
}

function validateTableName(v: string): string | undefined {
  if (!v) return 'Table name is required';
  if (!TABLE_NAME_RE.test(v)) return 'Only lowercase letters, numbers, and underscores allowed';
  return undefined;
}

/* ---------- S3 Tables — "Use existing" sub-form ---------- */

function ExistingS3TablesForm({ state, onChange }: WizardStepProps) {
  // P29 R1.2/R1.3 — hold the full query objects so a failed load surfaces an
  // inline error + Retry in the dropdown instead of an empty option list.
  const bucketsQuery = useS3TablesBuckets();
  const { data: buckets } = bucketsQuery;
  const selectedBucket = state.targetConfig.bucket_name || undefined;
  const tablesQuery = useTablesInBucket(selectedBucket);
  const { data: tables } = tablesQuery;

  const bucketOptions = (buckets ?? []).map((b) => ({ value: b.bucket_name, label: b.bucket_name }));
  const tableOptions = (tables ?? []).map((t) => ({
    value: t.table_name,
    label: t.table_name,
    description: t.namespace ? `Namespace: ${t.namespace}` : undefined,
  }));

  return (
    <SpaceBetween size="s">
      <FormField label="S3 Table Bucket">
        <Select
          selectedOption={selectedBucket ? { value: selectedBucket, label: selectedBucket } : null}
          onChange={({ detail }) => {
            const bucketName = detail.selectedOption.value!;
            const bucket = (buckets ?? []).find((b) => b.bucket_name === bucketName);
            onChange({
              ...state,
              targetConfig: {
                bucket_name: bucketName,
                table_bucket_arn: bucket?.arn ?? '',
              },
              detectedTargetSchema: null,
            });
          }}
          options={bucketOptions}
          placeholder="Select bucket"
          data-testid="s3t-bucket-select"
          {...selectAsyncStatus(bucketsQuery, 'buckets', `${NO_S3_TABLE_BUCKETS} ${NO_S3_TABLE_BUCKETS_WIZARD_NEXT_STEP}`)}
          {...selectFiltering('buckets', bucketOptions.length)}
        />
      </FormField>
      <FormField label="Table">
        <Select
          selectedOption={
            state.targetConfig.table_name
              ? { value: state.targetConfig.table_name, label: state.targetConfig.table_name }
              : null
          }
          onChange={({ detail }) => {
            const tableName = detail.selectedOption.value!;
            const table = (tables ?? []).find((t) => t.table_name === tableName);
            onChange({
              ...state,
              targetConfig: {
                ...state.targetConfig,
                table_name: tableName,
                namespace: table?.namespace ?? '',
              },
              detectedTargetSchema: null,
            });
          }}
          options={tableOptions}
          placeholder="Select table"
          disabled={!selectedBucket}
          data-testid="s3t-table-select"
          {...selectAsyncStatus(tablesQuery, 'tables', selectedBucket ? noS3TablesEmpty(selectedBucket) : undefined)}
          {...selectFiltering('tables', tableOptions.length)}
        />
      </FormField>
    </SpaceBetween>
  );
}

/* ---------- S3 Tables — "Create new" sub-form ---------- */

function NewS3TablesForm({ state, onChange }: WizardStepProps) {
  const [bucketErr, setBucketErr] = useState<string | undefined>();
  const [nsErr, setNsErr] = useState<string | undefined>();
  const [tableErr, setTableErr] = useState<string | undefined>();

  const bucket = state.targetConfig.bucket_name ?? '';
  const namespace = state.targetConfig.namespace ?? '';
  const table = state.targetConfig.table_name ?? '';

  function update(patch: Record<string, string>) {
    onChange({ ...state, targetConfig: { ...state.targetConfig, ...patch }, detectedTargetSchema: null });
  }

  return (
    <SpaceBetween size="s">
      <FormField label="Bucket name" errorText={bucketErr} description="3-63 chars, lowercase/numbers/hyphens">
        <Input
          value={bucket}
          onChange={({ detail }) => {
            setBucketErr(validateBucket(detail.value));
            update({ bucket_name: detail.value });
          }}
          placeholder="my-data-bucket"
        />
      </FormField>
      <FormField label="Namespace" errorText={nsErr} description="Lowercase letters, numbers, underscores">
        <Input
          value={namespace}
          onChange={({ detail }) => {
            setNsErr(validateNamespace(detail.value));
            update({ namespace: detail.value });
          }}
          placeholder="my_namespace"
        />
      </FormField>
      <FormField label="Table name" errorText={tableErr} description="Lowercase letters, numbers, underscores">
        <Input
          value={table}
          onChange={({ detail }) => {
            setTableErr(validateTableName(detail.value));
            update({ table_name: detail.value });
          }}
          placeholder="my_table"
        />
      </FormField>
    </SpaceBetween>
  );
}


/* ---------- S3 Tables — combined with mode toggle ---------- */

function S3TablesForm({ state, onChange }: WizardStepProps) {
  const [mode, setMode] = useState<'existing' | 'new'>('existing');

  function handleModeChange(newMode: 'existing' | 'new') {
    setMode(newMode);
    // Reset target config when switching modes
    onChange({ ...state, targetConfig: {}, detectedTargetSchema: null });
  }

  return (
    <SpaceBetween size="m">
      <Tiles
        value={mode}
        onChange={({ detail }) => handleModeChange(detail.value as 'existing' | 'new')}
        items={[
          { value: 'existing', label: 'Use existing', description: 'Select from existing S3 Table Buckets' },
          { value: 'new', label: 'Create new', description: 'Define a new bucket, namespace, and table' },
        ]}
      />
      {mode === 'existing' ? (
        <ExistingS3TablesForm state={state} onChange={onChange} />
      ) : (
        <NewS3TablesForm state={state} onChange={onChange} />
      )}
    </SpaceBetween>
  );
}

/* ---------- Glue Catalog — API-backed dropdowns ---------- */

function GlueCatalogForm({ state, onChange }: WizardStepProps) {
  // P29 R1.2/R1.3 — full query objects so failed loads render error + Retry.
  const databasesQuery = useGlueDatabases();
  const { data: databases } = databasesQuery;
  const selectedDb = state.targetConfig.database_name || undefined;
  const tablesQuery = useTablesInDatabase(selectedDb);
  const { data: tables } = tablesQuery;

  // p66 R1.1 — only databases this target type can accept are offered. The
  // partition returns `totalCount` as well as `eligible` because R1.5 requires
  // the empty picker to tell "databases exist, none eligible" apart from "no
  // Glue databases in this account at all", and a plain `.filter(...)` here
  // would yield one number and destroy the other.
  //
  // `selectedOption` below is deliberately NOT derived from `dbOptions` — it
  // reads `state.targetConfig.database_name` directly, which is what keeps a
  // stored-but-ineligible database rendering as selected rather than being
  // silently cleared (R4.3, and the sibling modal's §4.5 reasoning).
  const { eligible: eligibleDatabases, totalCount: databaseTotalCount } =
    partitionGlueTargetDatabases(databases ?? []);

  const dbOptions = eligibleDatabases.map((d) => ({ value: d.database_name, label: d.database_name }));
  const tableOptions = (tables ?? []).map((t) => ({ value: t.table_name, label: t.table_name }));

  return (
    <SpaceBetween size="s">
      {/* The three preconditions a Glue Catalog target has and no other target
          type does. Stated in the same words as the tile description in
          `WizardStepTarget.tsx` and as the `detect_schema` pre-flight's 400s
          (`dashboard_api/handlers/resources.py`, `GLUE_CATALOG target
          pre-flight`), so a refusal reads as the same rule rather than a new
          one. Change one, change all three. */}
      <Alert type="info" header="This target maps to an existing table" data-testid="glue-catalog-preconditions">
        The pipeline does not create catalog tables. Before you continue:
        <ul>
          <li>
            The table must already exist in the Glue Data Catalog, with a schema and an S3 location.
          </li>
          <li>
            The database name must start with <Box variant="code">agentic_data_ingestion_</Box> — the
            pipeline&rsquo;s Glue role grants access only to databases and tables under that prefix.
          </li>
          <li>
            The table&rsquo;s S3 location must be in a bucket the pipeline can write — use{' '}
            <Box variant="code">adi-processed-files-&lt;account&gt;-&lt;region&gt;</Box>. Two buckets
            the pipeline&rsquo;s policy allows are still refused: the per-pipeline{' '}
            <Box variant="code">agentic-data-ingestion-table-&lt;pipeline_id&gt;</Box> bucket, which
            is never created for this target type, and{' '}
            <Box variant="code">adi-source-files-&lt;account&gt;-&lt;region&gt;</Box>, which is the
            bucket this pipeline watches for uploads — table data written there would re-trigger the
            pipeline on its own output.
          </li>
        </ul>
        Detect schema checks all three and names the one that fails.
      </Alert>
      {/* p66 R1.2 — the exclusion rule is stated as the field's `description`,
          so it is present WHENEVER the filter is active rather than only when
          everything happened to be excluded. An empty-state-only note would say
          nothing in the ordinary case where some databases are offered and
          others were dropped, which is the silent exclusion C5's reasoning
          forbids. It names ONE of the three preconditions and does not restate
          the Alert's three bullets (C10 — "Change one, change all three" above
          governs three phrasings, not four). */}
      <FormField label="Glue Database" description={GLUE_TARGET_EXCLUSION_NOTE}>
        <Select
          selectedOption={selectedDb ? { value: selectedDb, label: selectedDb } : null}
          onChange={({ detail }) => {
            const db = detail.selectedOption.value!;
            onChange({
              ...state,
              targetConfig: { database_name: db },
              detectedTargetSchema: null,
            });
          }}
          options={dbOptions}
          placeholder="Select database"
          data-testid="glue-db-select"
          /* p66 R1.3 / R1.5 — the finished-and-empty state, as a total function
             of the two counts the partition returns. Cloudscape renders `empty`
             only when the option list is empty AND `statusType` is `finished`
             or unset (`internal/components/dropdown-status/index.js` is an
             if/else-if chain: `loading`, else `error`, else `isEmpty && empty`),
             so reaching this node already means `eligible.length === 0` and
             R2.2's mutual exclusion holds by construction — `empty` is
             unreachable while loading or erroring. p66 passed `empty` as a
             sibling of the `selectAsyncStatus` spread, because no other
             dropdown had an empty state yet; since every resource dropdown got
             one, the statement is the helper's third argument, still written
             here rather than by the helper (LGR-88b210b-1). */
          {...selectAsyncStatus(
            databasesQuery,
            'databases',
            databaseTotalCount === 0 ? GLUE_TARGET_NO_DATABASES : GLUE_TARGET_NONE_ELIGIBLE,
          )}
          {...selectFiltering('databases', dbOptions.length)}
        />
      </FormField>
      <FormField label="Table">
        <Select
          selectedOption={
            state.targetConfig.table_name
              ? { value: state.targetConfig.table_name, label: state.targetConfig.table_name }
              : null
          }
          onChange={({ detail }) => {
            onChange({
              ...state,
              targetConfig: { ...state.targetConfig, table_name: detail.selectedOption.value! },
              detectedTargetSchema: null,
            });
          }}
          options={tableOptions}
          placeholder="Select table"
          disabled={!selectedDb}
          data-testid="glue-table-select"
          /* p66 R2.1 / R2.4 — the reported symptom. A successful load with zero
             tables previously set no dropdown content at all, leaving "this
             database holds no tables" indistinguishable from a failed request
             (`LGR-2bcdd0d-3` rejected exactly that silence). The statement names
             the selected database, and the next step offers no create-table
             control because C1 (`LGR-2682c7f-1`) means the pipeline will never
             create one — a control it cannot honour would be the false
             affordance that record also rejected.
             `undefined` while no database is selected: the picker is disabled
             there (`disabled` above, R2.3) and an empty statement naming no
             database would assert something it did not check. */
          {...selectAsyncStatus(
            tablesQuery,
            'tables',
            selectedDb ? `${glueTargetNoTablesEmpty(selectedDb)} ${GLUE_TARGET_NO_TABLES_NEXT_STEP}` : undefined,
          )}
          {...selectFiltering('tables', tableOptions.length)}
        />
      </FormField>
    </SpaceBetween>
  );
}

/* ---------- S3 Parquet (unchanged) ---------- */

function S3ParquetForm({ state, onChange }: WizardStepProps) {
  return (
    <SpaceBetween size="s">
      <FormField label="S3 Path">
        <Input
          value={state.targetConfig.s3_path ?? ''}
          onChange={({ detail }) => onChange({ ...state, targetConfig: { ...state.targetConfig, s3_path: detail.value } })}
          placeholder="s3://my-bucket/path/to/data/"
        />
      </FormField>
    </SpaceBetween>
  );
}

/* ---------- Target form dispatcher ---------- */

/**
 * Target subform dispatcher extracted from `WizardStepTarget.tsx`
 * (P29 R11.5 decomposition) with no behavior change.
 */
export function TargetForm(props: WizardStepProps) {
  switch (props.state.targetType) {
    case 's3_tables': return <S3TablesForm {...props} />;
    case 'glue_catalog': return <GlueCatalogForm {...props} />;
    case 'jdbc': return <JdbcTargetForm {...props} />;
    case 's3_parquet': return <S3ParquetForm {...props} />;
    default: return null;
  }
}
