// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Schema } from '../types';

/**
 * p35 R4.8 — the name to show for a schema, defined **once**.
 *
 * > THE Schemas_Page and the Schema_Picker SHALL display the same name for the
 * > same schema — the Display_Label WHERE one is stored, and the schema id
 * > otherwise.
 *
 * "The same name" is the whole criterion, so the expression that produces it
 * belongs in one place rather than being written out at each of the four sites
 * that need it (the Schemas page's table and its two modals, the wizard picker,
 * `SchemaSourceSelector`'s three tabs). This is the same reasoning that gave
 * `schema-field-count.ts` its own module after design §5 risk 3 found one more
 * consumer of `fields?.length ?? 0` than it had enumerated.
 *
 * `||` rather than `??` is deliberate: `normalizeSchema` resolves an absent name
 * to `''`, and `''` is not nullish — so `??` would render an empty cell where the
 * id belongs.
 */
export function schemaDisplayName(schema: Pick<Schema, 'display_label' | 'schema_name' | 'schema_id'>): string {
  return schema.display_label || schema.schema_name || schema.schema_id;
}

/**
 * The `source_type` values that name an Uploaded_Schema.
 *
 * `uploaded` is what `_collect_schema_rows` emits (`schemas.py`); the three file
 * extensions are the legacy aliases the dashboard has always also accepted, and
 * `SchemaSourceSelector`'s uploaded tab filters on the same four.
 */
const UPLOADED_SOURCE_TYPES = new Set(['uploaded', 'csv', 'json', 'parquet']);

/**
 * p35 R3.4 / R2.2 — is this a schema the Schemas_API will actually write to or
 * delete?
 *
 * > THE Schemas_Page SHALL offer the delete action only for schemas the
 * > Schemas_API will delete.
 *
 * Only Uploaded_Schema ids name a deletable object. A `glue_catalog` or
 * `s3_table_bucket` row is a **view** of a catalog entry owned by
 * `create_infrastructure` or by the S3 Tables service, and `DELETE`, the context
 * `PUT` and the display-label `PUT` all answer `400` for one. The same test gates
 * the rename and the context editor, because R2.2 forbids presenting an editing
 * control for a value that cannot be persisted.
 *
 * **An allowlist, not a denylist.** Testing for "not `glue_catalog` and not
 * `s3_table_bucket`" would hand a delete button to any source type added later,
 * which is the direction that destroys a pipeline's target rather than the
 * direction that merely withholds a control.
 *
 * Gated on `source_type`, which every listing row carries, rather than on the
 * `glue_` / `s3_table_` id prefixes the handler tests
 * (`_catalog_derived_source`). Re-deriving the producer's rule from the id shape
 * would be a second definition of it, and the two could drift.
 */
export function isUploadedSchema(schema: Pick<Schema, 'source_type'>): boolean {
  return UPLOADED_SOURCE_TYPES.has(schema.source_type);
}
/**
 * p36 R2.8 — the request key a Display_Label travels under, defined **once**.
 *
 * > WHEN an equivalent labelled upload is submitted from the Schemas_Page and
 * > from the Create Pipeline wizard, THE Schemas_API SHALL receive the
 * > Display_Label under the same request key in both cases.
 *
 * "The same key" is the whole criterion, and two string literals that happen to
 * match do not satisfy it — they satisfy it *today*. The two upload paths are
 * different HTTP clients: `uploadSchema` unpacks a `FormData` and posts JSON
 * (`use-schemas.ts:129-133`), while the wizard posts JSON directly
 * (`CreatePipelinePage.tsx:334-342`) and so never runs that forwarding code.
 * Two spellings of one forwarding rule is the state `LGR-cad8032-1` exists to
 * prevent; one import makes R2.8 true by construction instead.
 *
 * **The location is the design, not convenience.** A `vi.mock` factory
 * *replaces* the module it targets, so anything the factory omits reads back as
 * `undefined` — and a key constant imported from a replaced module would put
 * `{ undefined: label }` in the upload payload. That is held-assertion inventory
 * row 5's hazard in a new shape, on the very spec that corrects row 5's
 * description. `utils/schema-display.ts` is a pure util that no factory
 * replaces — `WizardStepSchema.picker.test.tsx:30-31` records that the real
 * `schemaDisplayName` and `normalizeField` "are part of what is under test" —
 * and it is already the single definition of the display-name rule, with nine
 * importers.
 *
 * This names the **preferred** spelling, not the only accepted one. The backend
 * reads both (`_DISPLAY_LABEL_KEYS = ("displayLabel", "display_label")`,
 * `handlers/schemas.py:1761`). camelCase is the one the detail response
 * (`:1592`) and `PUT /schemas/{id}/display-label` (`:2967`) already emit, so
 * using it keeps request and response on one spelling.
 */
export const DISPLAY_LABEL_REQUEST_KEY = 'displayLabel';
