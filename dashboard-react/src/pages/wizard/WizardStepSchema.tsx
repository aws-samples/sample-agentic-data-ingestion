// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useMemo, useRef } from 'react';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import Tiles from '@cloudscape-design/components/tiles';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import { useSchemas, useSchemaDetail } from '../../hooks';
// p35 R5.6 / DD4 — the option description used to read
// `${s.fields?.length ?? 0} fields`, which offers every uploaded and every S3
// Tables schema as "0 fields" while it declares some. One definition of the
// three-way count, shared with the Schemas page and SchemaSourceSelector.
import { fieldCountLabel, schemaFieldCount } from '../../utils/schema-field-count';
// p35 R4.8 — the one definition of the name to show for a schema.
import { isUploadedSchema, schemaDisplayName } from '../../utils/schema-display';
import { SchemaUploadWizard } from '../../components/SchemaUploadWizard';
import type { Schema } from '../../types';
import type { WizardStepProps, SchemaField, WizardState } from './types';

function SchemaPreviewTable({ fields }: { fields: SchemaField[] }) {
  if (!fields.length) return <Box color="text-body-secondary">No schema fields available</Box>;
  return (
    <Table
      data-testid="schema-preview-table"
      columnDefinitions={[
        { id: 'name', header: 'Field Name', cell: (f) => f.name },
        { id: 'type', header: 'Type', cell: (f) => f.type },
        { id: 'nullable', header: 'Nullable', cell: (f) => (f.nullable ? 'Yes' : 'No') },
        { id: 'description', header: 'Description', cell: (f) => f.description || '—' },
      ]}
      items={fields}
      variant="embedded"
    />
  );
}

// P19 Task 10.5 / Decision 1 — read `detectedTargetSchema` (target-side detection
// result) instead of the legacy `detectedSchema` field. The split allows
// `mirror_source` mode to read `detectedSourceSchema` independently.
function DetectedSchemaView({ state }: { state: WizardStepProps['state'] }) {
  if (!state.detectedTargetSchema?.length) {
    return (
      <Alert type="info">
        No schema was detected from the target. Go back to step 2 and click "Detect Schema", or choose another source below.
      </Alert>
    );
  }
  return (
    <SpaceBetween size="s">
      <Alert type="success">Schema auto-detected from target ({state.detectedTargetSchema.length} fields)</Alert>
      <SchemaPreviewTable fields={state.detectedTargetSchema} />
    </SpaceBetween>
  );
}

/** Map the API's SchemaField onto the wizard's own field shape. */
function toWizardFields(fields: Schema['fields'] | undefined): SchemaField[] {
  return (fields ?? []).map((f) => ({
    name: f.field_name,
    type: f.field_type,
    nullable: true,
    // R1.6 — the schema's **own** declared description, which the API emits per
    // field since task 4.5. This is the same `normalizeField` source the modal
    // reads, so the wizard's Description column stops being blank on the
    // mechanism the modal's was.
    description: f.description ?? '',
  }));
}

/**
 * Resolves the selected schema's field list from `GET /api/schemas/{id}`,
 * commits it, and enforces R5.1/R5.2 — the wizard half of DD4.
 *
 * **Why the listing row cannot answer this.** Task 4.4 moved field resolution
 * off the listing, so an uploaded or S3 Tables row arrives with no `fields` key
 * at all. The code this replaces mapped `schema?.fields ?? []` off that row, so
 * it committed an **empty** field list for every uploaded and every S3 Tables
 * schema — R5.1's defect on the `existing` path, which `LGR-b304401-1`
 * (**template / ratified**) deliberately left outside the upload guard's scope.
 *
 * Mounted only while a schema is selected. That is what makes the fetch
 * conditional, and it also keeps `useSchemaDetail` out of the render path of the
 * sibling suites that replace the `hooks` module with a factory listing
 * `useSchemas` alone. Held-assertion inventory row 5's mechanism: a `vi.mock`
 * factory **replaces** the module, so any export it omits is `undefined`; the
 * component then calls `undefined()`
 * **during render, inside the test that renders it**. The failure is a stack
 * trace naming a value that is not a function — not an assertion diff — and the
 * file still collects and still reports per test. Amend the factory before
 * adding a hook to a component the factory's tests render.
 */
function SelectedExistingSchema({
  schemaId,
  listingFieldCount,
  state,
  onChange,
}: {
  schemaId: string;
  listingFieldCount: number | null;
  state: WizardState;
  onChange: WizardStepProps['onChange'];
}) {
  const { data: detail, isLoading, isError, refetch } = useSchemaDetail(schemaId);

  const resolvedFields = useMemo<SchemaField[] | null>(
    () => (detail ? toWizardFields(detail.fields) : null),
    [detail],
  );

  // R5.1's verdict, on DD4's three-way count and NOT on `fields.length`:
  //
  //   fieldCount === 0  the source stated zero — refuse, immediately
  //   fieldCount === null  no count established (every uploaded, every S3
  //                        Tables row) — wait for the detail and judge that
  //   a positive number    fine
  //
  // `normalizeSchema` resolves an absent `fields` to `[]`, so a guard reading
  // `fields.length` conflates "not established" with "zero" and would refuse
  // every schema a user uploads.
  const declaresNoFields =
    listingFieldCount === 0 || (resolvedFields !== null && resolvedFields.length === 0);

  useEffect(() => {
    if (!resolvedFields) return;
    const noFields = listingFieldCount === 0 || resolvedFields.length === 0;
    onChange({
      ...state,
      schemaFields: noFields ? [] : resolvedFields,
      existingSchemaDeclaresNoFields: noFields,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resolvedFields]);

  if (declaresNoFields) {
    // R5.2 — the inline statement. `SchemaPreviewTable`'s "No schema fields
    // available" placeholder cannot serve as one: it renders identically for a
    // field list that is empty and one that was never resolved, which is the
    // ambiguity DD4's `fieldCount: null` and R1.4 exist to remove. Rendered
    // inline rather than as a step-level `errorText`, which `LGR-b304401-1`
    // scopes to the upload case.
    return (
      <Alert type="warning" data-testid="existing-schema-no-fields">
        This schema declares no fields, so it cannot be a pipeline's target schema. Choose
        another schema, or upload one from the Upload tile.
      </Alert>
    );
  }
  if (isLoading) return <Spinner />;
  if (isError) {
    return (
      <Alert
        type="error"
        data-testid="existing-schema-detail-error"
        action={<Button onClick={() => refetch()}>Retry</Button>}
      >
        Could not load this schema's fields.
      </Alert>
    );
  }
  return <SchemaPreviewTable fields={resolvedFields ?? []} />;
}

function ExistingSchemaSelector({ state, onChange }: WizardStepProps) {
  const { data: rawSchemas, isLoading } = useSchemas();
  // Uploaded schemas only, which is what this tile says it offers ("Select from
  // previously uploaded schemas"). `/schemas` also lists every Glue Data Catalog
  // table in the account and every S3 Tables table, and neither works as an
  // existing target schema:
  //
  // * No target-schema object is written for them. `create_pipeline` records an
  //   S3 address only for an uploaded schema, and code generation, code testing
  //   and schema comparison read the target schema from that address alone
  //   (`shared/target_schema_resolution.resolve_target_schema`). So the transform
  //   was generated, and the run verified, without a target schema.
  // * The Glue list is mostly this app's own tables: JDBC discovery crawler
  //   output, per-pipeline databases, and S3 Tables mirrors whose bucket is gone,
  //   which show as "0 fields".
  //
  // An existing S3 Tables or JDBC destination's own schema comes from the
  // Auto-detected tile, and any other shape from Upload. `isUploadedSchema` is
  // the allowlist the Schemas page uses for the same "is this an uploaded
  // schema" question (`LGR-4854b3d-2`), so a source type added later stays out
  // until it works here.
  const schemas: Schema[] = (Array.isArray(rawSchemas) ? rawSchemas : []).filter(isUploadedSchema);

  if (isLoading) return <Spinner />;

  const options = schemas.map((s) => {
    // R5.6's count, stated only when the listing established one. An uploaded
    // row never carries a count (p35 DD4: its fields load from the detail
    // endpoint once selected), and "— fields not loaded — uploaded" on every
    // option told the user nothing. The source type went with it, since every
    // option is now an uploaded schema.
    const count = schemaFieldCount(s);
    return {
      value: s.schema_id,
      // R4.8 — the Display_Label where one is stored, the schema id otherwise.
      // One definition, shared with the Schemas page, so "the same name for the
      // same schema" is structural rather than two expressions agreeing.
      label: schemaDisplayName(s),
      ...(count === null ? {} : { description: fieldCountLabel(s) }),
    };
  });

  const selectedSchema = schemas.find((s) => s.schema_id === state.existingSchemaId);

  return (
    <SpaceBetween size="m">
      {/* Held-assertion inventory row 12: `WizardStepSchema.test.tsx:68` and
          `:99` assert this exact label, and R6's search lands INSIDE this
          control rather than in a differently-labelled one. Do not rename it. */}
      <FormField label="Select an existing schema">
        <Select
          selectedOption={state.existingSchemaId ? options.find((o) => o.value === state.existingSchemaId) ?? null : null}
          onChange={({ detail }) => {
            const id = detail.selectedOption.value!;
            const row = schemas.find((s) => s.schema_id === id);
            onChange({
              ...state,
              existingSchemaId: id,
              // The field list is resolved from the per-id endpoint and
              // committed by <SelectedExistingSchema>; clearing it here stops a
              // previous selection's fields surviving into a new one.
              schemaFields: [],
              // R5.1 — a listing that states zero is refused at once. `null`
              // waits for the detail rather than being read as zero.
              existingSchemaDeclaresNoFields: row ? schemaFieldCount(row) === 0 : false,
            });
          }}
          options={options}
          // R6.1 / R6.4 / R6.6 — the filtered-select form already used at
          // `DragDropMappingEditor.tsx:168` and `:188`, the dashboard's only
          // filtered `Select`s. `auto` narrows on each option's label and
          // description, which is R6.2's name and source type, and it renders a
          // filter input the keyboard reaches on open (R6.4).
          filteringType="auto"
          filteringPlaceholder="Find a schema"
          filteringAriaLabel="Find a schema by name or source type"
          // R6.3 — say so, and leave the text editable.
          noMatch={<Box>No schema matches that text.</Box>}
          empty={<Box>No schemas are available to choose.</Box>}
          placeholder="Choose a schema"
          data-testid="existing-schema-select"
        />
      </FormField>
      {state.existingSchemaId && (
        <SelectedExistingSchema
          schemaId={state.existingSchemaId}
          listingFieldCount={selectedSchema ? schemaFieldCount(selectedSchema) : null}
          state={state}
          onChange={onChange}
        />
      )}
    </SpaceBetween>
  );
}

function UploadSchemaView({ state, onChange }: WizardStepProps) {
  // `SchemaUploadWizard.handleFileChange` is a `useCallback` that captures its
  // props at the render in which the file was chosen: it fires `onFileChosen`
  // synchronously and `onSchemaReady` only *after* `await parseSchemaFile`
  // (`SchemaUploadWizard.tsx:44-66`). A callback that spread the render-time
  // `state` would therefore rebuild the wizard state from a snapshot taken
  // before the basename default landed, silently undoing it. Reading through a
  // ref makes both callbacks write on top of the newest state instead.
  const stateRef = useRef(state);
  stateRef.current = state;

  return (
    <SpaceBetween size="l">
      {/* p36 R2.1 — "THE wizard SHALL offer an optional Display_Label input."
          Deliberately NOT `required`: R2.3 makes the schema usable with no label
          at all, so the step stays completable with this empty.

          D1 (ratified at p36 requirement 2.5) — presentation only. This value
          never reaches the uploaded schema's id, the key of its backing object,
          or the `target_schema` the wizard sends to `/pipelines/provision`. It is
          also independent of `schemaFields` and of the field count: do NOT derive
          one from the other (`LGR-4854b3d-1`, template / ratified;
          `wizard/types.ts:75-79`). */}
      <FormField
        label="Schema display label"
        description="Optional. Shown on the Schemas page and in the schema picker in place of the generated id."
      >
        <Input
          // `?? ''` rather than a bare read, even though the type says `string`:
          // a draft saved before this field existed restores it as `undefined`,
          // because `useWizardDraft.readDraft` is a bare `JSON.parse(raw) as T`
          // (`useWizardDraft.ts:66-77`) and `handleRestoreDraft` replaces the
          // state rather than merging into it (`CreatePipelinePage.tsx:294-296`).
          // See the field's own comment in `wizard/types.ts` for the better fix
          // that was rejected for scope.
          value={state.schemaDisplayLabel ?? ''}
          onChange={({ detail }) =>
            onChange({ ...stateRef.current, schemaDisplayLabel: detail.value })
          }
          placeholder="Display label (optional)"
          ariaLabel="Display label for the uploaded schema (optional)"
          data-testid="wizard-display-label"
        />
      </FormField>
      <SchemaUploadWizard
        onSchemaReady={(fields) => {
          // Map from parser SchemaField to wizard SchemaField (same shape)
          const mapped: SchemaField[] = fields.map((f) => ({
            name: f.name,
            type: f.type,
            nullable: f.nullable,
            description: f.description,
          }));
          onChange({ ...stateRef.current, schemaFields: mapped });
        }}
        // p36 R2.9 — offer the chosen file's basename as the label. An "offer"
        // is a **default, not a lock**: only fill it when the current label is
        // empty, so a user who typed a name before choosing a file keeps it.
        //
        // `onFileChosen` was made "additive and optional on purpose"
        // (`SchemaUploadWizard.tsx:23-31`) and fires before the parse (`:54` —
        // "the name is a fact about the selection, not about the parse"), so a
        // file whose parse then fails still names the label. The Schemas page
        // consumes the same prop for the schema **`name`**
        // (`SchemaManagementPage.tsx:789`), not the label; two purposes on one
        // additive prop is what that shape bought.
        //
        // "Basename" is the page's definition of it — extension stripped
        // (`SchemaManagementPage.tsx:272`) — because this is a name a person
        // reads. A name that is nothing but an extension leaves the label alone
        // rather than blanking it.
        onFileChosen={(fileName) => {
          if ((stateRef.current.schemaDisplayLabel ?? '').trim()) return;
          const basename = fileName.replace(/\.[^.]+$/, '').trim();
          if (!basename) return;
          onChange({ ...stateRef.current, schemaDisplayLabel: basename });
        }}
      />
    </SpaceBetween>
  );
}

// P19 Task 10.4 — Mirror_Source mode (Mode 3). Validates Requirements 2.3, 2.4,
// 2.5, 5.1, 5.2. The component:
//   - Guards against non-JDBC source (Req 5.2) and missing source schema
//     (Req 2.4 / 5.1) with inline `<Alert type="warning">` blocks.
//   - On the happy path, displays the diagnostic "Mirroring from {db}.{table}"
//     line (Req 2.5), a success alert, and the schema preview table.
//   - Copies `state.detectedSourceSchema` into `state.schemaFields` from a
//     `useEffect` keyed on the source schema's identity (Req 2.3) — the copy
//     lives in an effect rather than during render so React doesn't see a
//     render-time setState.
function MirrorSourceView({ state, onChange }: WizardStepProps) {
  // Req 5.2 — non-JDBC source guard. Always rendered first; the missing-schema
  // and happy-path branches below presume `state.sourceType === 'jdbc'`.
  if (state.sourceType !== 'jdbc') {
    return (
      <Alert type="warning">
        Mirror source mode is only available when the source is a JDBC database. Choose another schema source or change the source type on step 2.
      </Alert>
    );
  }

  // Req 2.4 / Req 5.1 — missing source schema guard. Note: when this branch
  // returns, the `useEffect` below is NOT mounted, so `state.schemaFields` is
  // intentionally left untouched until the user detects the source schema.
  if (!state.detectedSourceSchema?.length) {
    return (
      <Alert type="warning">
        Mirror source mode requires source schema. Go back to step 2 and click Detect Schema.
      </Alert>
    );
  }

  return <MirrorSourceHappyPath state={state} onChange={onChange} />;
}

// Split out so the `useEffect` can be conditional on the guards above without
// violating the rules of hooks.
function MirrorSourceHappyPath({ state, onChange }: WizardStepProps) {
  const detectedSourceSchema = state.detectedSourceSchema!;

  // Req 2.3 — copy the source schema into `state.schemaFields`. Keyed on the
  // detected-schema reference identity so a re-detect propagates; running in
  // `useEffect` (rather than during render) avoids the React warning about
  // setState during render.
  useEffect(() => {
    if (state.schemaFields !== detectedSourceSchema) {
      onChange({ ...state, schemaFields: detectedSourceSchema });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detectedSourceSchema]);

  const sc = state.sourceConfig;
  const detectedDb = sc.detected_glue_database;
  const detectedTable = sc.detected_glue_table;
  const ref =
    detectedDb && detectedTable ? `Mirroring from ${detectedDb}.${detectedTable}` : null;

  return (
    <SpaceBetween size="s">
      {ref && <Box color="text-body-secondary">{ref}</Box>}
      <Alert type="success">
        Mirroring source schema ({detectedSourceSchema.length} fields)
      </Alert>
      <SchemaPreviewTable fields={detectedSourceSchema} />
    </SpaceBetween>
  );
}

export function WizardStepSchema({ state, onChange }: WizardStepProps) {
  // P19 Task 10.1 — fourth Tile for `mirror_source` mode (Req 2.1, 2.2).
  const schemaSourceOptions = [
    {
      value: 'detected',
      label: 'Auto-detected',
      description: 'Use schema detected from target destination',
    },
    {
      value: 'upload',
      label: 'Upload',
      description: 'Upload a JSON or CSV schema file',
    },
    {
      value: 'existing',
      label: 'Existing',
      description: 'Select from previously uploaded schemas',
    },
    {
      value: 'mirror_source',
      label: 'Mirror source schema',
      description:
        "Write the source table's columns directly to the target. The target table will be created or replaced with the same column names and types as the source table",
    },
  ];

  // P19 Task 10.2 — pre-selection rule (Req 3.1, 3.2, 3.4). When the user has
  // not explicitly picked a tile during this wizard session, pre-select
  // `'mirror_source'` for JDBC targets and `'detected'` for everything else.
  // Lives in `useEffect` (not in `INITIAL_WIZARD_STATE`) so non-JDBC pipelines
  // and pipeline-edit-flow loads continue to start with `'detected'`.
  useEffect(() => {
    if (state.schemaSourceExplicitlySet) return;
    const desired: WizardState['schemaSource'] =
      state.targetType === 'jdbc' ? 'mirror_source' : 'detected';
    if (state.schemaSource !== desired) {
      onChange({ ...state, schemaSource: desired });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.targetType, state.schemaSourceExplicitlySet]);

  return (
    <Container header={<Header variant="h2">Target Schema</Header>}>
      <SpaceBetween size="l">
        <FormField label="Schema Source">
          <Tiles
            value={state.schemaSource}
            // P19 Task 10.3 — flip `schemaSourceExplicitlySet` so the pre-selection
            // effect (Task 10.2) skips on subsequent renders (Req 3.3, 3.4). The
            // handler intentionally does NOT mutate `detectedSourceSchema`,
            // `detectedTargetSchema`, or `schemaFields`: switching modes preserves
            // the previously-detected schema.
            onChange={({ detail }) =>
              onChange({
                ...state,
                schemaSource: detail.value as WizardState['schemaSource'],
                schemaSourceExplicitlySet: true,
              })
            }
            items={schemaSourceOptions}
            data-testid="schema-source-tiles"
          />
        </FormField>

        {/* P19 Task 10.5 — four-way conditional render. Each branch reads its
            own state slice: 'detected' uses detectedTargetSchema (Decision 1),
            'mirror_source' uses detectedSourceSchema. */}
        {state.schemaSource === 'detected' && <DetectedSchemaView state={state} />}
        {state.schemaSource === 'upload' && <UploadSchemaView state={state} onChange={onChange} />}
        {state.schemaSource === 'existing' && <ExistingSchemaSelector state={state} onChange={onChange} />}
        {state.schemaSource === 'mirror_source' && <MirrorSourceView state={state} onChange={onChange} />}
      </SpaceBetween>
    </Container>
  );
}
