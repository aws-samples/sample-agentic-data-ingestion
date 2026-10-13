// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useRef } from 'react';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Modal from '@cloudscape-design/components/modal';
import Container from '@cloudscape-design/components/container';
import Input from '@cloudscape-design/components/input';
import TextFilter from '@cloudscape-design/components/text-filter';
import Select from '@cloudscape-design/components/select';
import Alert from '@cloudscape-design/components/alert';
import FormField from '@cloudscape-design/components/form-field';
import { spaceScaledM } from '@cloudscape-design/design-tokens';
import {
  useSchemas,
  useSchemaDetail,
  useUploadSchema,
  useUpdateFieldContext,
  useDeleteSchema,
  useSetDisplayLabel,
} from '../hooks';
import { useToast } from '../contexts/ToastContext';
import { SchemaUploadWizard } from '../components/SchemaUploadWizard';
import type { SchemaField as ParsedSchemaField } from '../utils/schema-parser';
// p45 R5.5 — the required/optional vocabulary, and the expression that produces
// it, live in exactly one place. `utils/schema-field-count.ts` is the
// precedent this follows (`LGR-4854b3d-1`, **template / ratified**): the resolver
// is named for its input, the label function for its reading context.
import { fieldRequirementFromNullable, requirementCellText } from '../utils/field-requirement';
// p35 R4.8 / R3.4 — the displayed name and the "will the API write to this?"
// test, each defined once. See `utils/schema-display.ts`.
import { isUploadedSchema, schemaDisplayName } from '../utils/schema-display';
// p35 R3.6 / R3.9 — the three refusals, kept as three.
import {
  classifySchemaDeleteFailure,
  type SchemaDeleteFailure,
} from '../utils/schema-delete-failure';
import type { RawSchemaUploadResponse, Schema, SchemaField } from '../types';

/**
 * Newest first, by `created_at`.
 *
 * The listing arrives grouped by source (uploaded, then Glue, then S3 Tables),
 * each group in its own source's order, so the Created column read as unsorted.
 * A row with no parseable `created_at` goes last rather than being given a
 * date, and ties fall back to the name so the order is stable.
 */
function byNewestFirst(a: Schema, b: Schema): number {
  const ta = Date.parse(a.created_at ?? '');
  const tb = Date.parse(b.created_at ?? '');
  const aKnown = !Number.isNaN(ta);
  const bKnown = !Number.isNaN(tb);
  if (aKnown && bKnown && ta !== tb) return tb - ta;
  if (aKnown !== bKnown) return aKnown ? -1 : 1;
  return schemaDisplayName(a).localeCompare(schemaDisplayName(b));
}

function formatDate(iso: string | undefined): string {
  if (!iso) return '\u2014';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

/**
 * R1.4 — a schema with nothing to show in the Fields table gets a statement,
 * and the two reasons for that are different statements.
 *
 * An empty table is indistinguishable from a failed load, which is the failure
 * class this requirement exists to remove. Telling the two apart is what DD4's
 * three-way `field_count` buys: `0` is the source stating that the schema
 * declares no fields, and `null` is no count having been established at all.
 * Both name the source type, so the reader knows which kind of schema they are
 * looking at.
 */
function EmptyFieldList({ schema }: { schema: Schema }) {
  const sourceType = schema.source_type || 'unknown';
  if (schema.field_count === 0) {
    return (
      <Alert type="info" data-testid="schema-declares-no-fields">
        This schema declares no fields. Its source type is {sourceType}.
      </Alert>
    );
  }
  return (
    <Alert type="warning" data-testid="schema-fields-unresolved">
      No field list came back for this schema (source type: {sourceType}). That is not the
      same as a schema that declares none — close and reopen the row to try again.
    </Alert>
  );
}

function SchemaDetailModal({
  schemaId,
  onDismiss,
}: {
  schemaId: string;
  onDismiss: () => void;
}) {
  const { data: schema, isLoading } = useSchemaDetail(schemaId);
  const updateContext = useUpdateFieldContext();
  const [editingField, setEditingField] = useState<string | null>(null);
  const [contextValue, setContextValue] = useState('');
  const [contextError, setContextError] = useState<string | null>(null);

  const handleSaveContext = (fieldName: string) => {
    setContextError(null);
    updateContext.mutate(
      { schemaId, fieldName, context: contextValue },
      {
        onSuccess: () => {
          setEditingField(null);
          setContextError(null);
        },
        // R2.1 — "IF a Field_Context edit is submitted and the write fails, THEN
        // THE Schemas_Page SHALL report the failure to the user and SHALL leave
        // the entered text recoverable."
        //
        // Both halves, and both were missing: there was no `onError` at all, so
        // Save neither said anything nor closed, and the row simply stayed in
        // edit mode with no way to tell a stored annotation from a discarded
        // one. `editingField` and `contextValue` are deliberately **not**
        // cleared here — that is what "recoverable" means, and it is why the
        // failure is reported inline rather than as a toast that would leave the
        // editor looking successful.
        onError: (error) => {
          setContextError(error instanceof Error ? error.message : 'The context could not be saved.');
        },
      },
    );
  };

  // R2.2 / R2.5 — the Context column's editor exists only where the write can
  // land. `isUploadedSchema` is the one definition of that test, gated on the
  // `source_type` the row carries rather than on the id prefix the handler
  // inspects. While the detail is still loading there is no source type to judge,
  // so no editor is offered — which is the safe direction.
  const contextIsEditable = !!schema && isUploadedSchema(schema);

  return (
    // R4.8 — the Display_Label where one is stored, the schema id otherwise.
    // `schemaDisplayName` is the single definition; the `?? schemaId` tail covers
    // the window before the detail resolves, when there is no schema to name yet.
    <Modal visible onDismiss={onDismiss} size="large" header={`Schema: ${schema ? schemaDisplayName(schema) : schemaId}`}>
      {isLoading ? (
        <Box textAlign="center">Loading schema details...</Box>
      ) : !schema ? (
        <Box>Schema not found.</Box>
      ) : (
        <SpaceBetween size="l">
          <Container>
            <SpaceBetween size="xs">
              <div><Box variant="awsui-key-label">Source type</Box>{schema.source_type}</div>
              <div><Box variant="awsui-key-label">Created</Box>{formatDate(schema.created_at)}</div>
              {/* No "Schema id" row here, deliberately. A draft of task 8.7 added
                  one so the id stayed visible behind a Display_Label, and it made
                  `SchemaManagementPage.detail-modal.test.tsx`'s
                  `getByText(/uploaded_schema_beta/)` ambiguous — task 6.1's
                  assertion for R1.3, which no requirement in task 8.7's set gives
                  a reason to move. No criterion asks for the id here either: R4.8
                  is about which *name* is displayed, and the id is stated where it
                  is load-bearing, in the Rename dialog, next to the action that
                  could be mistaken for changing it (R4.10). */}
            </SpaceBetween>
          </Container>
          {(schema.fields ?? []).length === 0 ? (
            <EmptyFieldList schema={schema} />
          ) : (
          <Table
            header={<Header variant="h3" counter={`(${(schema.fields ?? []).length})`}>Fields</Header>}
            columnDefinitions={[
              { id: 'name', header: 'Field name', cell: (f: SchemaField) => f.field_name },
              { id: 'type', header: 'Type', cell: (f: SchemaField) => f.field_type },
              {
                id: 'requirement',
                // p45 R5.1 — the written required/optional status, beside the
                // name, type, description and context this table already
                // renders. Placed here rather than last so the two columns whose
                // authority is *the schema itself* sit together, ahead of the one
                // whose authority is a person: `Required (declared by the
                // schema)`, `Description (declared by the schema)`, then `Context
                // (added by a person)`. That naming convention is C5 /
                // `LGR-acca05d-3` (**template / ratified**), whose accepted cost
                // was *"a second authority for a field's description alongside the
                // schema's own, which Requirement 1.6 keeps legible by putting
                // them under distinct headings"* — the reason is stated at
                // `:242-245` below and this heading extends it rather than
                // inventing a convention.
                header: 'Required (declared by the schema)',
                // R5.2 — the status is sourced from the Field_Nullable the schemas
                // API already emits (`handlers/schemas.py:1569`, and `:496` for
                // the Glue branch, which after p45 task 8 omits the key rather
                // than inventing `True`). **No schema body is parsed in the
                // browser.** `use-schemas.ts:normalizeField` is the one boundary
                // where the fact enters, carrying it as `boolean | undefined`
                // (design C12), so `f.nullable` here is genuinely three-state and
                // `undefined` means the source was silent.
                //
                // R5.3 / R5.5 — the whole expression is
                // `fieldRequirementFromNullable` plus `requirementCellText`, both
                // in `utils/field-requirement.ts`. Do **NOT** inline a
                // `f.nullable ? 'Optional' : 'Required'` here: that is the
                // collapse `LGR-4854b3d-1` exists to prevent, and it reports "the
                // source did not say" as "optional" (C4, do-NOT 5, do-NOT 16).
                // `not_established` renders `Not established` rather than a blank,
                // because a blank cell is indistinguishable from a failed load —
                // the same confusion `EmptyFieldList` above exists to remove.
                //
                // Text only, no colour, no icon, no emoji (C1, `LGR-99050e6-1`,
                // do-NOT 3): colour alone fails WCAG 1.4.1. A Cloudscape `Badge`
                // colour would be permitted *alongside* the words and is not used
                // here because the words are sufficient.
                //
                // Two things this column deliberately does not touch:
                //
                // - **`EmptyFieldList` (`:60-75`).** This column lives inside the
                //   non-empty branch at `:150`, so where `fields` is empty the
                //   table is not rendered and **no required/optional claim is
                //   made at all** (R5.7). That matters beyond tidiness:
                //   `_resolve_row_fields`' docstring
                //   (`handlers/schemas.py:1501-1509`) records that `get_schema`
                //   still cannot tell an unreadable body from a schema declaring
                //   none, so a required/optional column must not imply the field
                //   list is complete.
                // - **the six wizard `Nullable` columns** —
                //   `SchemaUploadWizard.tsx:129-133`,
                //   `wizard/WizardStepSchema.tsx:34`,
                //   `wizard/WizardStepTarget.tsx:197`,
                //   `wizard/WizardStepReview.tsx:170`,
                //   `wizard/JdbcTargetDetectPanel.tsx:154`,
                //   `wizard/JdbcSourceDetectionPanel.tsx:266-268`. They read a
                //   **non-optional** boolean off the wizard's own draft-state type
                //   (`pages/wizard/types.ts:2-7`, `nullable: boolean` at `:5`) and
                //   already satisfy C1 in text. They are the precedent, not the
                //   work (R5.6, do-NOT 12).
                //
                // And one trap that is cleared rather than reachable:
                // `handleSchemaReady`'s `fields.filter((f) => !f.nullable)` at
                // `:347` reads the **parser** `SchemaField`
                // (`utils/schema-parser.ts:2-7`, non-optional `nullable` at `:5`),
                // not this API read-back type, so `!undefined` is not reachable
                // from `normalizeField`'s output there. This column does not read
                // from it and it is not "fixed" here.
                cell: (f: SchemaField) => {
                  const requirement = fieldRequirementFromNullable(f.nullable);
                  // The testid is the total derivation
                  // `requirement-${requirement.replace('_', '-')}` documented in
                  // `utils/field-requirement.ts`'s own table — written at the
                  // render site because the module exports the words, not the
                  // handles. One testid per state, scoped per row by the reader.
                  return (
                    <span data-testid={`requirement-${requirement.replace('_', '-')}`}>
                      {requirementCellText(requirement)}
                    </span>
                  );
                },
              },
              // R1.6 — two headings, two authorities. The description is the
              // schema's **own** declared description; before task 4.5 the API
              // emitted none at all and this column rendered "—" for every field
              // of every schema, next to a Context column fed the one real value.
              { id: 'desc', header: 'Description (declared by the schema)', cell: (f: SchemaField) => f.description || '\u2014' },
              {
                id: 'context',
                // The Context_Sidecar's annotation, so it carries a value only
                // where a person has written one (R2.3).
                header: 'Context (added by a person)',
                cell: (f: SchemaField) =>
                  editingField === f.field_name ? (
                    <SpaceBetween size="xxs">
                      <SpaceBetween direction="horizontal" size="xs">
                        <Input
                          value={contextValue}
                          onChange={({ detail }) => setContextValue(detail.value)}
                          ariaLabel={`Context for ${f.field_name}`}
                        />
                        <Button variant="primary" onClick={() => handleSaveContext(f.field_name)} loading={updateContext.isPending}>Save</Button>
                        <Button variant="link" onClick={() => setEditingField(null)}>Cancel</Button>
                      </SpaceBetween>
                      {/* R2.1 — the reason the API gave, beside the text it did
                          not store, which is still in the input above. */}
                      {contextError && (
                        <Alert type="error" data-testid="context-save-error">
                          {contextError} Your text is still here — Save to try again, or Cancel to
                          discard it.
                        </Alert>
                      )}
                    </SpaceBetween>
                  ) : (
                    <SpaceBetween direction="horizontal" size="xs">
                      <span>{f.context || '\u2014'}</span>
                      {/* R2.2 — "THE Schemas_Page SHALL NOT present an editing
                          control for a value it cannot persist", and R2.5: the
                          context `PUT` answers `400` for a catalog-derived id,
                          because only an Uploaded_Schema has a Context_Sidecar to
                          write into. Offering the pencil on a Glue or S3 Tables
                          row would be offering a `400`. */}
                      {contextIsEditable && (
                        <Button variant="icon" iconName="edit" ariaLabel={`Edit context for ${f.field_name}`} onClick={() => { setEditingField(f.field_name); setContextValue(f.context || ''); setContextError(null); }} />
                      )}
                    </SpaceBetween>
                  ),
              },
            ]}
            items={schema.fields ?? []}
            empty={<Box textAlign="center">No fields defined.</Box>}
          />
          )}
        </SpaceBetween>
      )}
    </Modal>
  );
}

export default function SchemaManagementPage() {
  // `sourcesFailed` defaults to `[]` so a test that stubs `useSchemas` with
  // only `{ data, isLoading, isError }` keeps working unchanged.
  const { data: schemas = [], isLoading, isError, sourcesFailed = [] } = useSchemas();
  const uploadSchema = useUploadSchema();
  const deleteSchema = useDeleteSchema();
  // D2's rename (R4.9). Called at the component's **top level**, which is why
  // held-assertion inventory row 5 had to be amended before this line existed —
  // `src/__tests__/pages.test.tsx` carries that factory. Row 5's mechanism:
  // a `vi.mock` factory **replaces** the module, so any export it omits is
  // `undefined`; the component then calls `undefined()`
  // **during render, inside the test that renders it**. The failure is a stack
  // trace naming a value that is not a function — not an assertion diff — and the
  // file still collects and still reports per test. Amend the factory before
  // adding a hook to a component the factory's tests render.
  const setDisplayLabel = useSetDisplayLabel();
  const { showToast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [selectedSchemaId, setSelectedSchemaId] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [sourceTypeFilter, setSourceTypeFilter] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Schema | null>(null);
  /** The refusal the API gave for the schema in `deleteTarget`, if any (R3.6). */
  const [deleteFailure, setDeleteFailure] = useState<SchemaDeleteFailure | null>(null);
  const [showUploadWizard, setShowUploadWizard] = useState(false);
  /** R4.1 — the optional Display_Label, shared by both upload paths. */
  const [uploadDisplayLabel, setUploadDisplayLabel] = useState('');
  /** The schema being renamed, and the label being typed for it (R4.9). */
  const [renameTarget, setRenameTarget] = useState<Schema | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [renameError, setRenameError] = useState<string | null>(null);

  /**
   * R4.3 — the basename of the file the user chose in the preview wizard.
   *
   * A ref rather than state because `SchemaUploadWizard` reports the filename
   * and commits the parsed fields in the **same tick**: a `setState` would not
   * have landed by the time `handleSchemaReady` reads it, so the upload would
   * carry the previous selection's name (or none at all on the first upload).
   */
  const chosenFileNameRef = useRef<string | null>(null);

  const handleSchemaReady = (fields: ParsedSchemaField[]) => {
    // Convert parsed fields to a JSON blob and upload via the existing hook
    const schemaBlob = new Blob(
      [JSON.stringify({
        properties: Object.fromEntries(
          fields.map((f) => [f.name, { type: f.type, description: f.description }]),
        ),
        required: fields.filter((f) => !f.nullable).map((f) => f.name),
      })],
      { type: 'application/json' },
    );
    // R4.3 — the user's own basename, not a fixed 'schema.json'. Before this,
    // every "Upload with preview" landed as `shared/schema_<timestamp>.json`
    // (SchemaManagementPage.tsx:112), so distinct schemas collapsed into one
    // indistinguishable family — R4.2's "derive the schema's name from the
    // selected file's basename, exactly as it does today" was true of the plain
    // Upload button and false of this path.
    //
    // The extension is dropped and `.json` re-added because the bytes really
    // are JSON now: the wizard parses a `.csv` into fields and this rebuilds
    // them as a JSON Schema document, so naming the blob `orders.csv` would
    // describe it wrongly. `name` is sent explicitly beside the file so the
    // schema name is this component's decision rather than `uploadSchema`'s
    // regex on a filename (use-schemas.ts:111).
    const basename = (chosenFileNameRef.current ?? '').replace(/\.[^.]+$/, '').trim() || 'schema';
    const file = new File([schemaBlob], `${basename}.json`, { type: 'application/json' });
    const formData = new FormData();
    formData.append('file', file);
    formData.append('name', basename);
    appendDisplayLabel(formData);
    uploadSchema.mutate(formData, {
      onSuccess: (response) => {
        setShowUploadWizard(false);
        reportUploadOutcome(response);
      },
      onError: (error) => {
        showToast(
          error instanceof Error ? error.message : 'The schema could not be uploaded',
          'error',
        );
      },
    });
  };

  /**
   * R4.1 — attach the optional Display_Label, when the user typed one.
   *
   * Shared by both upload paths on this page so there is one rule rather than
   * two: `handleUpload` (the plain file picker) and `handleSchemaReady` (the
   * preview wizard). Whitespace-only is treated as no label, which is what the
   * producer does with it too.
   */
  function appendDisplayLabel(formData: FormData) {
    if (uploadDisplayLabel.trim()) formData.append('displayLabel', uploadDisplayLabel);
  }

  /**
   * Report an upload, including the one case that is a success **and** a
   * complaint.
   *
   * `displayLabelError` arrives on a **`200`**: the schema body was stored and is
   * fully usable (R4.2) and only the sidecar write failed. Reporting that as an
   * upload failure would make the user retry an upload that succeeded and produce
   * a second object, so it is a warning naming the recovery — the rename control
   * on this page writes the same key.
   */
  function reportUploadOutcome(response: RawSchemaUploadResponse | undefined) {
    setUploadDisplayLabel('');
    if (response?.displayLabelError) {
      showToast(
        `Schema uploaded, but its display label was not saved (${response.displayLabelError}). ` +
          'Use Rename to set it.',
        'warning',
      );
      return;
    }
    showToast('Schema uploaded successfully', 'success');
  }

  const filtered = schemas.filter((s) => {
    if (filterText) {
      // Match the name on screen as well as the underlying id, so a schema
      // carrying a Display_Label (R4.8) is findable by what the table shows.
      const needle = filterText.toLowerCase();
      const shown = (s.display_label || s.schema_name).toLowerCase();
      if (!shown.includes(needle) && !s.schema_name.toLowerCase().includes(needle)) return false;
    }
    if (sourceTypeFilter && s.source_type !== sourceTypeFilter) return false;
    return true;
  }).sort(byNewestFirst);

  // Derive unique source types for the filter dropdown
  const sourceTypes = [...new Set(schemas.map((s) => s.source_type).filter(Boolean))].sort();

  const handleUpload = () => {
    const file = fileInputRef.current?.files?.[0];
    if (!file) return;
    const formData = new FormData();
    formData.append('file', file);
    // R4.1 — the same optional label as the preview path. R4.2 is what happens
    // when it is blank: the name is still derived from the file's basename
    // (`uploadSchema`, `use-schemas.ts`), exactly as before.
    appendDisplayLabel(formData);
    uploadSchema.mutate(formData, {
      onSuccess: (response) => {
        if (fileInputRef.current) fileInputRef.current.value = '';
        reportUploadOutcome(response);
      },
      onError: (error) => {
        if (fileInputRef.current) fileInputRef.current.value = '';
        showToast(
          error instanceof Error ? error.message : 'The schema could not be uploaded',
          'error',
        );
      },
    });
  };

  /**
   * R3.6 — "WHEN a delete succeeds, THE Schemas_Page SHALL report success naming
   * the deleted schema; WHEN a delete fails, THE Schemas_Page SHALL report the
   * reason the Schemas_API gave rather than a fixed message."
   *
   * The failure path keeps the dialog **open** and renders the classified
   * refusal inside it. A toast cannot carry R3.9's list of referencing pipelines
   * and cannot carry the `503`'s retry affordance, and both of those are the
   * point: the previous code toasted `Failed to delete schema "<name>"` for all
   * three refusals plus every `500`.
   */
  const handleDelete = () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    setDeleteFailure(null);
    deleteSchema.mutate(target.schema_id, {
      onSuccess: () => {
        showToast(`Schema "${schemaDisplayName(target)}" deleted successfully`, 'success');
        setDeleteTarget(null);
        setDeleteFailure(null);
      },
      onError: (error) => {
        setDeleteFailure(classifySchemaDeleteFailure(error));
      },
    });
  };

  /**
   * R4.9 — persist the new Display_Label. An attribute write on a sidecar and
   * nothing else: the schema id, its object key and that object's bytes are
   * untouched (R4.10), and no pipeline record is read or written (R4.11).
   *
   * A blank value **clears** the label, which is why there is no separate Clear
   * button and no validation refusing an empty input — the producer removes the
   * key rather than storing `""`, and the schema then displays its id again
   * (R4.8).
   */
  const handleRename = () => {
    if (!renameTarget) return;
    const target = renameTarget;
    setRenameError(null);
    setDisplayLabel.mutate(
      { schemaId: target.schema_id, displayLabel: renameValue },
      {
        onSuccess: (response) => {
          setRenameTarget(null);
          showToast(
            response?.cleared
              ? `Display label cleared for "${target.schema_id}"`
              : `Schema "${target.schema_id}" is now shown as "${response?.displayLabel ?? renameValue}"`,
            'success',
          );
        },
        // The typed label stays in the input, same rule as the context editor.
        onError: (error) => {
          setRenameError(
            error instanceof Error ? error.message : 'The display label could not be saved.',
          );
        },
      },
    );
  };

  return (
    <SpaceBetween size="l">
      {/* R1.4's other half — design §5 risk 10's mitigation, on screen.
          `get_schemas` reports which sources it could not enumerate, and until
          this rendered, a permissions or service problem produced a short list
          with nothing to say so. A non-empty `sourcesFailed` is still a 200:
          one broken source degrades one section, not the page. */}
      {sourcesFailed.length > 0 && (
        <Alert
          type="warning"
          header="This list is incomplete — a schema source could not be read"
          data-testid="schema-sources-failed"
        >
          <SpaceBetween size="xxs">
            <Box variant="p">
              Schemas from the sources below are missing from the table, so a schema you expect
              may be absent rather than deleted.
            </Box>
            <ul>
              {sourcesFailed.map((f) => (
                <li key={`${f.source}:${f.scope}`}>
                  <strong>{f.source}</strong> ({f.scope}): {f.error}
                </li>
              ))}
            </ul>
          </SpaceBetween>
        </Alert>
      )}
      <Table
        columnDefinitions={[
          // R4.8 — the Schemas_Page and the Schema_Picker display the same name
          // for the same schema: the Display_Label where one is stored, the
          // schema id otherwise.
          //
          // The name may break at any character. Schema ids are long tokens
          // with no spaces, and `wrapLines` alone only wraps at spaces, so an
          // id still set its column's minimum width to the whole token. On a
          // narrow window that squeezed the Actions column down to one letter
          // per line. The full name stays in the DOM, so nothing is truncated.
          {
            id: 'name',
            header: 'Schema name',
            cell: (s: Schema) => <span style={{ overflowWrap: 'anywhere' }}>{schemaDisplayName(s)}</span>,
          },
          { id: 'source', header: 'Source type', cell: (s: Schema) => s.source_type },
          // No Fields column. The listing states a count only for Glue catalog
          // rows; uploaded and S3 Tables rows carry `fieldCount: null` because
          // the listing does not open each schema (p35 task 4.4), so the column
          // read "—" on every row a user normally has. The count is in View
          // details, which reads the schema itself. This drops p35 R1.3's
          // "modal count equals the Fields column" with the column it compared.
          { id: 'created', header: 'Created', cell: (s: Schema) => formatDate(s.created_at) },
          {
            id: 'actions',
            header: 'Actions',
            cell: (s: Schema) => (
              // `wrapText={false}`: a Cloudscape Button breaks its label between
              // any two letters by default.
              //
              // One line, never wrapped. A horizontal SpaceBetween wraps its
              // children, and with the table's auto layout the Actions column
              // was sized narrower than its three controls, so the delete icon
              // dropped under "View details" and uploaded rows came out twice
              // as tall as catalog rows. `nowrap` makes the controls' combined
              // width this column's minimum; the Schema name column, which
              // breaks anywhere, gives up the space instead (LGR-f0c0c30-1).
              //
              // `inline-link` / `inline-icon` rather than `link` / `icon`: the
              // plain variants carry button padding, which at one line took
              // about 80px more and left the names breaking mid-word at 900px.
              // Same controls, same labels and roles, so the label-based test
              // lookups LGR-f0c0c30-1 counts are unaffected.
              <div style={{ display: 'flex', alignItems: 'center', gap: spaceScaledM, whiteSpace: 'nowrap' }}>
                <Button variant="inline-link" wrapText={false} onClick={() => setSelectedSchemaId(s.schema_id)}>View details</Button>
                {/* R3.4 — "THE Schemas_Page SHALL offer the delete action only for
                    schemas the Schemas_API will delete", and R4.9's rename writes
                    into the same Uploaded_Schema sidecar, so both controls share
                    one gate. A `glue_catalog` or `s3_table_bucket` row is a view
                    of a catalog object the API refuses with `400` on all three of
                    its write routes; rendering either control there offers the
                    user a refusal. See `isUploadedSchema`. */}
                {isUploadedSchema(s) && (
                  <>
                    <Button
                      variant="inline-link"
                      wrapText={false}
                      data-testid={`rename-schema-${s.schema_id}`}
                      onClick={() => {
                        setRenameTarget(s);
                        // Prefilled with the stored label, so a rename edits
                        // rather than retypes — and empty when there is none,
                        // where the table is showing the id.
                        setRenameValue(s.display_label ?? '');
                        setRenameError(null);
                      }}
                    >
                      Rename
                    </Button>
                    <Button
                      variant="inline-icon"
                      iconName="remove"
                      ariaLabel={`Delete schema ${schemaDisplayName(s)}`}
                      onClick={() => { setDeleteTarget(s); setDeleteFailure(null); }}
                    />
                  </>
                )}
              </div>
            ),
          },
        ]}
        items={filtered}
        // Cells wrap at spaces (dates, source types) instead of holding their
        // full width on one line, so a narrow window still fits every column.
        wrapLines
        loading={isLoading}
        loadingText="Loading schemas"
        header={
          <Header
            variant="h1"
            counter={`(${filtered.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <input type="file" ref={fileInputRef} accept=".csv,.json,.parquet" style={{ display: 'none' }} onChange={handleUpload} />
                {/* R4.1 — "THE Schemas_Page SHALL offer an optional Display_Label
                    field and, WHERE the user supplies one, SHALL send it to the
                    Schema_Upload." One control rather than one per path: both
                    "Upload schema" and "Upload with preview" read it, and R4.2
                    is the case where it is left blank — the name still comes from
                    the file's basename and the schema is fully usable with no
                    label at all. */}
                <Input
                  value={uploadDisplayLabel}
                  onChange={({ detail }) => setUploadDisplayLabel(detail.value)}
                  placeholder="Display label (optional)"
                  ariaLabel="Display label for the next upload (optional)"
                  data-testid="upload-display-label"
                />
                <Button
                  iconName="upload"
                  onClick={() => fileInputRef.current?.click()}
                  loading={uploadSchema.isPending}
                >
                  Upload schema
                </Button>
                <Button
                  variant="primary"
                  iconName="upload"
                  onClick={() => setShowUploadWizard(true)}
                  data-testid="upload-with-preview-button"
                >
                  Upload with preview
                </Button>
              </SpaceBetween>
            }
          >
            Schema Management
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Search schemas"
              onChange={({ detail }) => setFilterText(detail.filteringText)}
            />
            <Select
              selectedOption={sourceTypeFilter ? { value: sourceTypeFilter, label: sourceTypeFilter } : null}
              onChange={({ detail }) => setSourceTypeFilter(detail.selectedOption?.value ?? null)}
              options={[
                { value: '', label: 'All source types' },
                ...sourceTypes.map((t) => ({ value: t, label: t })),
              ]}
              placeholder="Filter by source type"
              data-testid="source-type-filter"
            />
          </SpaceBetween>
        }
        empty={
          <Box textAlign="center" color="inherit">
            {isError ? 'Failed to load schemas.' : 'No schemas found.'}
          </Box>
        }
      />

      {selectedSchemaId && (
        <SchemaDetailModal schemaId={selectedSchemaId} onDismiss={() => setSelectedSchemaId(null)} />
      )}

      {/* Delete confirmation dialog, and the three refusals it renders */}
      {deleteTarget && (
        <Modal
          visible
          onDismiss={() => { setDeleteTarget(null); setDeleteFailure(null); }}
          header="Delete Schema"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => { setDeleteTarget(null); setDeleteFailure(null); }}>
                  {deleteFailure ? 'Close' : 'Cancel'}
                </Button>
                {/* The primary button is offered again only where trying again can
                    change the answer — which is the `503` and nothing else. On a
                    `409` the pipelines have to change first, and on the `400` the
                    schema is never deletable here, so re-offering Delete would be
                    offering the same refusal. */}
                {(!deleteFailure || deleteFailure.retryable) && (
                  <Button
                    variant="primary"
                    onClick={handleDelete}
                    loading={deleteSchema.isPending}
                    data-testid={deleteFailure ? 'delete-retry' : 'delete-confirm'}
                  >
                    {deleteFailure ? 'Try again' : 'Delete'}
                  </Button>
                )}
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {!deleteFailure && (
              <Box>
                Are you sure you want to delete schema <strong>{schemaDisplayName(deleteTarget)}</strong>? This action cannot be undone.
              </Box>
            )}

            {/* R3.7 / R3.9 — the `409`. "WHEN a delete is refused under 3.7, THE
                Schemas_Page SHALL present the referencing pipelines the
                Schemas_API named, so that the user can act on them rather than
                only being told no." So this renders the pipelines themselves and
                not `referencingPipelines.length` — a count tells the user there
                is an obstacle without telling them which one. */}
            {deleteFailure?.kind === 'in_use' && (
              <Alert
                type="error"
                header="This schema is in use, so it was not deleted"
                data-testid="delete-refused-in-use"
              >
                <SpaceBetween size="xs">
                  <Box variant="p">{deleteFailure.message}</Box>
                  {deleteFailure.referencingPipelines.length > 0 ? (
                    <>
                      <Box variant="p">Update or archive these pipelines first:</Box>
                      <ul data-testid="delete-referencing-pipelines">
                        {deleteFailure.referencingPipelines.map((p) => (
                          <li key={p.pipelineId}>
                            <strong>{p.pipelineName}</strong>
                            {p.pipelineName !== p.pipelineId && <> ({p.pipelineId})</>}
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : (
                    // The producer always names them, so this reads as what it
                    // is — a response that did not — rather than as an empty list
                    // that could be mistaken for "none".
                    <Box variant="p">
                      The API refused the delete but did not name the referencing pipelines.
                    </Box>
                  )}
                </SpaceBetween>
              </Alert>
            )}

            {/* R3.3 / R3.4 — the `400`. Reachable even with the row action gated
                on `source_type`: a listing that arrived before a source type was
                known, or a hand-issued request, still has to read correctly. */}
            {deleteFailure?.kind === 'catalog_derived' && (
              <Alert
                type="error"
                header="This schema is a view of a catalog object and cannot be deleted here"
                data-testid="delete-refused-catalog-derived"
              >
                <SpaceBetween size="xs">
                  <Box variant="p">{deleteFailure.message}</Box>
                  {deleteFailure.source && (
                    <Box variant="p">
                      It is owned by <strong>{deleteFailure.source}</strong>, which created it.
                      Deleting it here would destroy a pipeline's target.
                    </Box>
                  )}
                </SpaceBetween>
              </Alert>
            )}

            {/* R3.10 / design §5 risk 6 — the `503`, and the branch with no user
                story behind it. It is therefore the one most likely to be
                collapsed into a generic error, which would put this page back to
                "always toasts Failed to delete schema" for the one case where the
                answer is **retry**. It is `warning` rather than `error` on
                purpose: nothing failed and nothing was deleted, the question
                simply was not answered. */}
            {deleteFailure?.kind === 'check_incomplete' && (
              <Alert
                type="warning"
                header="Nothing was deleted — we could not finish checking whether a pipeline uses this schema"
                data-testid="delete-refused-check-incomplete"
              >
                <SpaceBetween size="xs">
                  <Box variant="p">{deleteFailure.message}</Box>
                  <Box variant="p">
                    The schema is untouched. This is usually temporary, so please try again.
                  </Box>
                  {deleteFailure.checked.length > 0 && (
                    <Box variant="small" data-testid="delete-checked-lookups">
                      Lookups that did complete: {deleteFailure.checked.join(', ')}
                    </Box>
                  )}
                </SpaceBetween>
              </Alert>
            )}

            {/* Anything the API did not classify. Deliberately not folded into one
                of the three above: those three all guarantee the object still
                exists, because R3.10 orders the reference check before the delete.
                This one guarantees nothing, and saying so is the honest answer. */}
            {deleteFailure?.kind === 'unknown' && (
              <Alert type="error" header="The schema could not be deleted" data-testid="delete-failed">
                <SpaceBetween size="xs">
                  <Box variant="p">{deleteFailure.message}</Box>
                  {deleteFailure.details && <Box variant="small">{deleteFailure.details}</Box>}
                  <Box variant="p">
                    Close this dialog and check the list to see whether the schema is still there.
                  </Box>
                </SpaceBetween>
              </Alert>
            )}
          </SpaceBetween>
        </Modal>
      )}

      {/* R4.9 — D2's rename. A Display_Label write, not a relocation: nothing
          here can change the schema's id or its object key. */}
      {renameTarget && (
        <Modal
          visible
          onDismiss={() => setRenameTarget(null)}
          header={`Rename ${schemaDisplayName(renameTarget)}`}
          data-testid="rename-schema-modal"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setRenameTarget(null)}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={handleRename}
                  loading={setDisplayLabel.isPending}
                  data-testid="rename-save"
                >
                  Save
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <FormField
              label="Display label"
              description="Shown in this list and in the Create Pipeline wizard. Leave it empty to go back to showing the schema id. Two schemas may carry the same label."
            >
              <Input
                value={renameValue}
                onChange={({ detail }) => setRenameValue(detail.value)}
                placeholder={renameTarget.schema_id}
                ariaLabel="Display label"
                data-testid="rename-input"
              />
            </FormField>
            {/* R4.10 / R4.11, said out loud where the action is taken. The id is
                what every pipeline record's `target_schema_name` and every S3 key
                carry, and a label never becomes one. */}
            <Box variant="small">
              The schema id stays <strong>{renameTarget.schema_id}</strong>. Renaming changes only
              what is displayed — no object is moved, copied or deleted, and no pipeline is changed.
            </Box>
            {renameError && (
              <Alert type="error" data-testid="rename-error">
                {renameError} Your text is still here — Save to try again.
              </Alert>
            )}
          </SpaceBetween>
        </Modal>
      )}

      {/* Schema upload with preview wizard */}
      {showUploadWizard && (
        <Modal
          visible
          onDismiss={() => setShowUploadWizard(false)}
          header="Upload Schema with Preview"
          size="large"
          data-testid="upload-wizard-modal"
        >
          <SchemaUploadWizard
            onSchemaReady={handleSchemaReady}
            onFileChosen={(fileName) => {
              chosenFileNameRef.current = fileName;
            }}
          />
        </Modal>
      )}
    </SpaceBetween>
  );
}
