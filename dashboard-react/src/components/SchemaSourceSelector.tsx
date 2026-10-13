// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from 'react';
import Tabs from '@cloudscape-design/components/tabs';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Table from '@cloudscape-design/components/table';
import type { Schema } from '../types';
import {
  useSchemas,
  useSchemaDetail,
} from '../hooks';
// p35 held-assertion inventory row 6 — this component's three count sites read
// `${s.fields?.length ?? 0} fields`, and DD4 makes that "0 fields" for every
// uploaded and every S3 Tables schema. One definition of the three-way count,
// shared with the Schemas page and the wizard picker.
import { fieldCountLabel } from '../utils/schema-field-count';
// p35 R4.8 — the one definition of the name to show for a schema.
import { schemaDisplayName } from '../utils/schema-display';

export interface SchemaSourceSelectorProps {
  onSchemaSelect: (schemaId: string, schemaData: Schema) => void;
}

/** Displays a preview table of schema fields. */
export function SchemaPreview({ schema }: { schema: Schema | null | undefined }) {
  if (!schema) {
    return <Box color="text-body-secondary">No schema selected</Box>;
  }
  if (!schema.fields?.length) {
    // The gate used to be `!schema || !schema.fields?.length`, which said "No
    // schema selected" for a schema that was selected and whose fields simply
    // had not been resolved. DD4 makes those two states routine rather than
    // exceptional, and R1.4 is the requirement that they read differently.
    return schema.field_count === 0 ? (
      <Box color="text-body-secondary" data-testid="schema-preview-declares-no-fields">
        This schema declares no fields.
      </Box>
    ) : (
      <Box color="text-body-secondary" data-testid="schema-preview-fields-not-loaded">
        Fields not loaded for this schema.
      </Box>
    );
  }
  return (
    <Table
      data-testid="schema-preview"
      columnDefinitions={[
        { id: 'name', header: 'Field Name', cell: (f) => f.field_name },
        { id: 'type', header: 'Type', cell: (f) => f.field_type },
        // R1.6 — the schema's own declared description, which the API emits per
        // field since task 4.5.
        { id: 'desc', header: 'Description', cell: (f) => f.description ?? '—' },
      ]}
      items={schema.fields}
      variant="embedded"
      empty="No fields"
    />
  );
}

/** Inline error + retry for a failed schema load (Async_State_Triple, R1.4). */
function SchemaLoadError({ onRetry }: { onRetry: () => void }) {
  return (
    <Alert
      type="error"
      data-testid="schema-source-error"
      action={<Button data-testid="schema-source-retry" onClick={onRetry}>Retry</Button>}
    >
      Failed to load schemas.
    </Alert>
  );
}

/**
 * Resolves the selected schema from `GET /api/schemas/{id}` and only **then**
 * hands it to `onSchemaSelect` — held-assertion inventory row 6's second half.
 *
 * The select handler used to pass `{ ...schema }` straight from the listing row,
 * which goes into `PipelineFormModal`'s pipeline write
 * (`PipelineFormModal.tsx:185`). After DD4 that row carries no `fields` for
 * uploaded or S3 Tables schemas, so the pipeline would be created against a
 * field-less schema — R5.1's defect class on a surface the requirements were not
 * looking at.
 *
 * Mounted only while a schema is selected, so the detail query never runs with
 * nothing to fetch — and so `useSchemaDetail` stays out of the render path of a
 * suite that replaces the `hooks` module with a factory listing `useSchemas`
 * alone. Inventory row 5's mechanism: a `vi.mock` factory **replaces** the
 * module, so any export it omits is `undefined`; the component then calls
 * `undefined()` **during render, inside the test that renders it**. The failure
 * is a stack trace naming a value that is not a function — not an assertion
 * diff — and the file still collects and still reports per test.
 * Amend the factory before adding a hook to a component the factory's tests
 * render.
 */
function SelectedSchemaDetail({
  schemaId,
  listingRow,
  sourceTypeOverride,
  onSchemaSelect,
}: {
  schemaId: string;
  listingRow: Schema | undefined;
  sourceTypeOverride?: string;
  onSchemaSelect: SchemaSourceSelectorProps['onSchemaSelect'];
}) {
  const { data: detail, isLoading, isError, refetch } = useSchemaDetail(schemaId);

  useEffect(() => {
    if (!detail) return;
    onSchemaSelect(schemaId, sourceTypeOverride ? { ...detail, source_type: sourceTypeOverride } : detail);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail, schemaId]);

  if (isLoading) return <Spinner />;
  if (isError) {
    return (
      <Alert
        type="error"
        data-testid="schema-detail-error"
        action={<Button data-testid="schema-detail-retry" onClick={() => refetch()}>Retry</Button>}
      >
        Failed to load this schema's fields.
      </Alert>
    );
  }
  // Fall back to the listing row only for the field **count**, so a schema whose
  // source stated zero still reads as "declares no fields" rather than as
  // "not loaded" while the detail is in flight.
  return <SchemaPreview schema={detail ?? listingRow} />;
}

/** The select + preview body shared by all three tabs. */
function SchemaChoice({
  label,
  placeholder,
  schemas,
  sourceTypeOverride,
  onSchemaSelect,
}: {
  label: string;
  placeholder: string;
  schemas: Schema[];
  sourceTypeOverride?: string;
  onSchemaSelect: SchemaSourceSelectorProps['onSchemaSelect'];
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const options = schemas.map((s) => ({
    value: s.schema_id,
    // R4.8 — the Display_Label where one is stored, the schema id otherwise.
    // One definition, shared with the Schemas page and the wizard picker.
    label: schemaDisplayName(s),
    description: `${fieldCountLabel(s)} — ${s.source_type}`,
  }));

  return (
    <SpaceBetween size="m">
      <FormField label={label}>
        <Select
          selectedOption={selectedId ? options.find((o) => o.value === selectedId) ?? null : null}
          onChange={({ detail }) => setSelectedId(detail.selectedOption.value!)}
          options={options}
          placeholder={placeholder}
        />
      </FormField>
      {selectedId ? (
        <SelectedSchemaDetail
          schemaId={selectedId}
          listingRow={schemas.find((s) => s.schema_id === selectedId)}
          sourceTypeOverride={sourceTypeOverride}
          onSchemaSelect={onSchemaSelect}
        />
      ) : (
        <SchemaPreview schema={null} />
      )}
    </SpaceBetween>
  );
}

/** The listing, normalized. The API may return `Schema[]` or `{ schemas }`. */
function useSchemaRows(predicate: (s: Schema) => boolean) {
  const { data: rawSchemas, isLoading, isError, refetch } = useSchemas();
  const allSchemas: Schema[] = Array.isArray(rawSchemas)
    ? rawSchemas
    : (rawSchemas as unknown as { schemas?: Schema[] })?.schemas ?? [];
  return { schemas: allSchemas.filter(predicate), isLoading, isError, refetch };
}

/** Tab content for Uploaded Schemas. */
function UploadedSchemasTab({ onSchemaSelect }: { onSchemaSelect: SchemaSourceSelectorProps['onSchemaSelect'] }) {
  // Filter to only uploaded schemas (not glue_catalog or s3_table_bucket)
  const { schemas, isLoading, isError, refetch } = useSchemaRows((s) =>
    s.source_type === 'uploaded' || s.source_type === 'csv' || s.source_type === 'json' || s.source_type === 'parquet'
  );

  if (isLoading) return <Spinner />;
  if (isError) return <SchemaLoadError onRetry={() => refetch()} />;

  return (
    <SchemaChoice
      label="Select schema"
      placeholder="Choose an uploaded schema"
      schemas={schemas}
      onSchemaSelect={onSchemaSelect}
    />
  );
}

/** Tab content for Glue Data Catalog — shows schemas from /schemas API with source=glue_catalog. */
function GlueCatalogTab({ onSchemaSelect }: { onSchemaSelect: SchemaSourceSelectorProps['onSchemaSelect'] }) {
  const { schemas, isLoading, isError, refetch } = useSchemaRows((s) => s.source_type === 'glue_catalog');

  if (isLoading) return <Spinner />;
  if (isError) return <SchemaLoadError onRetry={() => refetch()} />;

  return (
    <SchemaChoice
      label="Select Glue Catalog schema"
      placeholder="Choose a Glue Data Catalog schema"
      schemas={schemas}
      sourceTypeOverride="glue_data_catalog"
      onSchemaSelect={onSchemaSelect}
    />
  );
}

/** Tab content for S3 Table Bucket — shows schemas from /schemas API with source=s3_table_bucket. */
function S3TableBucketTab({ onSchemaSelect }: { onSchemaSelect: SchemaSourceSelectorProps['onSchemaSelect'] }) {
  const { schemas, isLoading, isError, refetch } = useSchemaRows((s) => s.source_type === 's3_table_bucket');

  if (isLoading) return <Spinner />;
  if (isError) return <SchemaLoadError onRetry={() => refetch()} />;

  return (
    <SchemaChoice
      label="Select S3 Table Bucket schema"
      placeholder="Choose an S3 Table Bucket schema"
      schemas={schemas}
      sourceTypeOverride="s3_table_bucket"
      onSchemaSelect={onSchemaSelect}
    />
  );
}

export default function SchemaSourceSelector({ onSchemaSelect }: SchemaSourceSelectorProps) {
  return (
    <Tabs
      tabs={[
        {
          label: 'Uploaded Schemas',
          id: 'uploaded',
          content: <UploadedSchemasTab onSchemaSelect={onSchemaSelect} />,
        },
        {
          label: 'Glue Data Catalog',
          id: 'glue',
          content: <GlueCatalogTab onSchemaSelect={onSchemaSelect} />,
        },
        {
          label: 'S3 Table Bucket',
          id: 's3',
          content: <S3TableBucketTab onSchemaSelect={onSchemaSelect} />,
        },
      ]}
    />
  );
}
