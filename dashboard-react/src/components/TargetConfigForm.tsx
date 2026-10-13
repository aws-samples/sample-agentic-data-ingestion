// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useEffect } from 'react';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import type { TargetConfig } from '../types';
import {
  useS3TablesBuckets,
  useTablesInBucket,
  useGlueDatabases,
  useTablesInDatabase,
} from '../hooks';
import { selectAsyncStatus, selectFiltering } from './common/select-async-status';
import { NO_S3_TABLE_BUCKETS, noS3TablesEmpty } from '../utils/resource-empty-states';
// p66 R1.4 / R4.1 — the SAME module the create wizard's `GlueCatalogForm`
// consumes (`pages/wizard/TargetForms.tsx:24-31`). R1.4's criterion is that the
// two Database pickers "cannot disagree about which databases are offerable",
// and importing the one rule is what makes that true rather than asserted: a
// second `startsWith` spelling here, or a local `.filter(...)`, would be the
// duplication R1.4 exists to prevent (`LGR-4854b3d-1`, the ratified precedent
// for keeping such an expression in `utils/`). Nothing below re-spells the
// prefix, the predicate or the copy.
import {
  GLUE_TARGET_EXCLUSION_NOTE,
  GLUE_TARGET_NONE_ELIGIBLE,
  GLUE_TARGET_NO_DATABASES,
  GLUE_TARGET_NO_TABLES_NEXT_STEP,
  glueTargetNoTablesEmpty,
  partitionGlueTargetDatabases,
} from '../utils/glue-target-eligibility';

export type SchemaSourceType = 'uploaded' | 's3_table_bucket' | 'glue_data_catalog';

export interface TargetConfigFormProps {
  schemaSourceType: SchemaSourceType;
  value: Partial<TargetConfig>;
  onChange: (config: Partial<TargetConfig>) => void;
}

// Validation patterns (Requirement 3.10)
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

/** Form for NEW_S3_TABLE_BUCKET — manual bucket/namespace/table inputs with validation. */
function NewS3TableBucketForm({ value, onChange }: Pick<TargetConfigFormProps, 'value' | 'onChange'>) {
  const [bucketErr, setBucketErr] = useState<string | undefined>();
  const [nsErr, setNsErr] = useState<string | undefined>();
  const [tableErr, setTableErr] = useState<string | undefined>();

  const bucket = value.bucket_name ?? '';
  const namespace = (value as Record<string, unknown>).namespace as string ?? '';
  const table = value.table_name ?? '';

  return (
    <SpaceBetween size="s">
      <FormField label="Bucket name" errorText={bucketErr} description="3-63 chars, lowercase/numbers/hyphens">
        <Input
          value={bucket}
          onChange={({ detail }) => {
            setBucketErr(validateBucket(detail.value));
            onChange({ ...value, type: 'NEW_S3_TABLE_BUCKET', bucket_name: detail.value });
          }}
          placeholder="my-data-bucket"
        />
      </FormField>
      <FormField label="Namespace" errorText={nsErr} description="Lowercase letters, numbers, underscores">
        <Input
          value={namespace}
          onChange={({ detail }) => {
            setNsErr(validateNamespace(detail.value));
            onChange({ ...value, type: 'NEW_S3_TABLE_BUCKET', namespace: detail.value } as Partial<TargetConfig>);
          }}
          placeholder="my_namespace"
        />
      </FormField>
      <FormField label="Table name" errorText={tableErr} description="Lowercase letters, numbers, underscores">
        <Input
          value={table}
          onChange={({ detail }) => {
            setTableErr(validateTableName(detail.value));
            onChange({ ...value, type: 'NEW_S3_TABLE_BUCKET', table_name: detail.value });
          }}
          placeholder="my_table"
        />
      </FormField>
    </SpaceBetween>
  );
}

/** Form for EXISTING_S3_TABLE_BUCKET — bucket/table dropdowns loaded from API. */
function ExistingS3TableBucketForm({ value, onChange }: Pick<TargetConfigFormProps, 'value' | 'onChange'>) {
  // P29 R1.2/R1.3 — full query objects so a failed load renders error + Retry.
  const bucketsQuery = useS3TablesBuckets();
  const { data: buckets } = bucketsQuery;
  const [selectedBucket, setSelectedBucket] = useState<string | undefined>(value.bucket_name);
  const tablesQuery = useTablesInBucket(selectedBucket);
  const { data: tables } = tablesQuery;

  useEffect(() => {
    if (value.bucket_name) setSelectedBucket(value.bucket_name);
  }, [value.bucket_name]);

  const bucketOptions = (buckets ?? []).map((b) => ({ value: b.bucket_name, label: b.bucket_name }));
  const tableOptions = (tables ?? []).map((t) => ({ value: t.table_name, label: t.table_name }));

  return (
    <SpaceBetween size="s">
      <FormField label="S3 Table Bucket">
        <Select
          selectedOption={selectedBucket ? { value: selectedBucket, label: selectedBucket } : null}
          onChange={({ detail }) => {
            const b = detail.selectedOption.value!;
            setSelectedBucket(b);
            onChange({ type: 'EXISTING_S3_TABLE_BUCKET', bucket_name: b });
          }}
          options={bucketOptions}
          placeholder="Select bucket"
          {...selectAsyncStatus(bucketsQuery, 'buckets', NO_S3_TABLE_BUCKETS)}
          {...selectFiltering('buckets', bucketOptions.length)}
        />
      </FormField>
      <FormField label="Table">
        <Select
          selectedOption={value.table_name ? { value: value.table_name, label: value.table_name } : null}
          onChange={({ detail }) => {
            onChange({ ...value, type: 'EXISTING_S3_TABLE_BUCKET', table_name: detail.selectedOption.value! });
          }}
          options={tableOptions}
          placeholder="Select table"
          disabled={!selectedBucket}
          {...selectAsyncStatus(tablesQuery, 'tables', selectedBucket ? noS3TablesEmpty(selectedBucket) : undefined)}
          {...selectFiltering('tables', tableOptions.length)}
        />
      </FormField>
    </SpaceBetween>
  );
}

/** Form for GLUE_CATALOG — database/table dropdowns loaded from API. */
function GlueCatalogForm({ value, onChange }: Pick<TargetConfigFormProps, 'value' | 'onChange'>) {
  // P29 R1.2/R1.3 — full query objects so a failed load renders error + Retry.
  const databasesQuery = useGlueDatabases();
  const { data: databases } = databasesQuery;
  const [selectedDb, setSelectedDb] = useState<string | undefined>(value.database);
  const tablesQuery = useTablesInDatabase(selectedDb);
  const { data: tables } = tablesQuery;

  useEffect(() => {
    if (value.database) setSelectedDb(value.database);
  }, [value.database]);

  // p66 R1.1 — only databases this target type can accept are offered, by the
  // same rule the wizard applies. The partition returns `totalCount` as well as
  // `eligible` because R1.5 requires the empty picker to tell "databases exist,
  // none eligible" apart from "no Glue databases in this account at all", and a
  // plain `.filter(...)` here would yield one number and destroy the other.
  //
  // `selectedOption` below is deliberately NOT derived from `dbOptions` — it
  // reads `selectedDb`, seeded from `value.database` at `:162` and re-synced by
  // the `useEffect` at `:166-168`. That is *why* R4.3 holds with no special
  // case: filtering the option list cannot clear a stored value, and there is
  // no clearing logic in this component to suppress (design §4.5).
  const { eligible: eligibleDatabases, totalCount: databaseTotalCount } =
    partitionGlueTargetDatabases(databases ?? []);

  const dbOptions = eligibleDatabases.map((d) => ({ value: d.database_name, label: d.database_name }));
  const tableOptions = (tables ?? []).map((t) => ({ value: t.table_name, label: t.table_name }));

  return (
    <SpaceBetween size="s">
      {/* p66 R1.2 — the exclusion rule is stated as the field's `description`,
          so it is present WHENEVER the filter is active rather than only when
          everything happened to be excluded. An empty-state-only note would say
          nothing in the ordinary case where some databases are offered and
          others were dropped, which is the silent exclusion C5's reasoning
          forbids.
          This modal has NO preconditions Alert — that exists only in the wizard
          (`TargetForms.tsx:246-268`), and design §4.3 records the asymmetry as
          pre-existing with no requirement behind it and deliberately NOT fixed
          here, because closing it would put unrequested copy on a surface p66
          was not scoped for (`LGR-8a4fefa-2`'s shape). The copy below is written
          to stand alone for exactly that reason. */}
      <FormField label="Glue Database" description={GLUE_TARGET_EXCLUSION_NOTE}>
        <Select
          selectedOption={selectedDb ? { value: selectedDb, label: selectedDb } : null}
          onChange={({ detail }) => {
            const db = detail.selectedOption.value!;
            setSelectedDb(db);
            onChange({ type: 'GLUE_CATALOG', database: db });
          }}
          options={dbOptions}
          placeholder="Select database"
          /* p66 R1.3 / R1.5 — the finished-and-empty state, as a total function
             of the two counts the partition returns. Keys on `totalCount === 0`
             alone: reaching this node already implies the option list is empty,
             because Cloudscape's third branch is `isEmpty && empty`
             (`internal/components/dropdown-status/index.js` is an if/else-if
             chain: `loading`, else `error`, else `isEmpty && empty`), so the
             second conjunct comes from the framework and R2.2's mutual
             exclusion holds by construction — `empty` is unreachable while
             loading or erroring. p66 passed `empty` as a sibling of the
             `selectAsyncStatus` spread; since every resource dropdown got an
             empty state, it is the helper's third argument, still written here
             rather than by the helper (LGR-88b210b-1). */
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
          selectedOption={value.table ? { value: value.table, label: value.table } : null}
          onChange={({ detail }) => {
            onChange({ ...value, type: 'GLUE_CATALOG', table: detail.selectedOption.value! });
          }}
          options={tableOptions}
          placeholder="Select table"
          disabled={!selectedDb}
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

export default function TargetConfigForm({ schemaSourceType, value, onChange }: TargetConfigFormProps) {
  switch (schemaSourceType) {
    case 'uploaded':
      return <NewS3TableBucketForm value={value} onChange={onChange} />;
    case 's3_table_bucket':
      return <ExistingS3TableBucketForm value={value} onChange={onChange} />;
    case 'glue_data_catalog':
      return <GlueCatalogForm value={value} onChange={onChange} />;
    default:
      return <Box color="text-body-secondary">Select a schema source to configure target.</Box>;
  }
}
