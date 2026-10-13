// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';
import { schemaKeys } from './query-keys';
import type {
  RawDisplayLabelResponse,
  RawSchemaDetail,
  RawSchemaListingResponse,
  RawSchemaUploadResponse,
  Schema,
  SchemaField,
  SchemaListing,
  SchemaSourceFailure,
} from '../types';

/** Normalize a raw schema field from the API into our SchemaField type. */
function normalizeField(raw: Record<string, unknown>): SchemaField {
  return {
    field_name: (raw.field_name as string) ?? (raw.name as string) ?? '',
    field_type: (raw.field_type as string) ?? (raw.type as string) ?? '',
    // R1.6 — the schema's own declared description. The API emitted no
    // field-level `description` at all before p35 task 4.5, which is why this
    // resolved to `undefined` for every field of every schema and the
    // Description column rendered "—" everywhere.
    description: (raw.description as string) ?? undefined,
    // R2.3 — the Context_Sidecar's annotation, and only that.
    context: (raw.context as string) ?? undefined,
    // R5.2 / R5.3 / R5.4 — the source's declared nullability, three-state and
    // preserved rather than collapsed. `false` = the source declared the field
    // required, `true` = declared optional, `undefined` = the source did not
    // say. This is the **only** place in the frontend where `nullable` can
    // enter (design C12): `RawSchemaDetailField` has always named the key and
    // this function used to drop it, so a fact the API already emitted reached
    // no reader at all.
    //
    // The producer no longer invents the third state either — the Glue branch
    // omits the key (`schemas.py:496`) because `StorageDescriptor.Columns[]`
    // carries no nullability, and `_schema_detail` sets it conditionally
    // (`:1569`) instead of defaulting it to `True`. So an absent key here means
    // the source was silent, not that it said "optional".
    //
    // Do NOT fall back to `true` here, and do not write `nullable ?? true` at
    // any reader (do-NOT 16, do-NOT 5). `true` and `undefined` are the
    // distinction R5.3 turns on, and collapsing them reports every Glue Data
    // Catalog column as "optional" on the strength of a value its source never
    // supplied — the same collapse `LGR-4854b3d-1` (**template / ratified**)
    // refused for `field_count` in `normalizeFieldCount` immediately below,
    // whose comment ends *"Do NOT fall back to `0` here."* Do NOT spell the
    // third state
    // `null` either (do-NOT 17, `LGR-44d601e-1`): absence is the spelling, and
    // three spellings of two facts is the defect rather than the fix. Read this
    // through `utils/field-requirement.ts`'s `fieldRequirementFromNullable`,
    // which is the single expression R5.5 asks for.
    nullable: typeof raw.nullable === 'boolean' ? raw.nullable : undefined,
  };
}

/**
 * DD4's three-way field count, preserved rather than collapsed.
 *
 * A stated count wins — **including `0`**, which is R5.1's "declares zero
 * fields" and is the one value a `??` chain would silently swallow. Otherwise a
 * present field **list** supplies its own count. With neither, the count was
 * never established and this returns `null`.
 *
 * Do NOT fall back to `0` here. `null` and `0` are the distinction R1.4, R5.1
 * and R5.3 all turn on, and collapsing them makes every uploaded and every S3
 * Tables schema look empty (`schemas.py:578`, `:641` send `fieldCount: null`
 * with no `fields` key).
 */
function normalizeFieldCount(raw: Record<string, unknown>): number | null {
  const declared = raw.fieldCount ?? raw.field_count;
  if (typeof declared === 'number') return declared;
  return Array.isArray(raw.fields) ? (raw.fields as unknown[]).length : null;
}

/** Normalize a raw schema object from the API into our Schema type. */
function normalizeSchema(raw: Record<string, unknown>): Schema {
  const rawFields = (raw.fields as Array<Record<string, unknown>>) ?? [];
  return {
    schema_id: (raw.schema_id as string) ?? (raw.schemaId as string) ?? '',
    schema_name: (raw.schema_name as string) ?? (raw.name as string) ?? '',
    source_type: (raw.source_type as string) ?? (raw.source as string) ?? '',
    display_label: (raw.displayLabel as string) ?? (raw.display_label as string) ?? undefined,
    fields: rawFields.map(normalizeField),
    field_count: normalizeFieldCount(raw),
    created_at: (raw.created_at as string) ?? (raw.lastModified as string) ?? '',
    updated_at: (raw.updated_at as string) ?? undefined,
  };
}

/**
 * `GET /api/schemas` — the rows **and** the sources that could not be read.
 *
 * `sourcesFailed` is carried through rather than dropped: task 4.5 narrowed the
 * listing's seven swallowing `except` clauses, so a permissions or service
 * problem in one source is now reported instead of silently shortening the list
 * (R1.4, design §5 risk 10). Until this reached a component the mitigation
 * existed in the response and not on screen.
 */
async function fetchSchemaListing(): Promise<SchemaListing> {
  const body = await apiClient.get<RawSchemaListingResponse | unknown[]>('/schemas');
  // The collection has always been permitted to arrive as a bare array; that
  // pre-existing tolerance is left alone. (DD9's "no tolerant consumer" rule is
  // about the **per-id** shape, which is handled below.)
  const envelope: RawSchemaListingResponse = Array.isArray(body) ? { schemas: body } : (body ?? {});
  const rawSchemas: unknown[] = Array.isArray(envelope.schemas) ? envelope.schemas : [];
  const sourcesFailed: SchemaSourceFailure[] = Array.isArray(envelope.sourcesFailed)
    ? envelope.sourcesFailed
    : [];
  return {
    schemas: rawSchemas.map((s) => normalizeSchema(s as Record<string, unknown>)),
    sourcesFailed,
  };
}

/**
 * `GET /api/schemas/{id}` — DD9's **bare schema object**, normalized as-is.
 *
 * **The `[0]` unwrap is deleted, not made tolerant.** The pre-p35 router
 * answered a per-id request with the whole collection, and this function took
 * element `[0]` of it — so the modal described the first row of the listing
 * under whichever id the user clicked. `body.schemas?.[0] ?? body` would keep
 * working through that regression *silently*, which is why it was rejected: a
 * tolerant consumer is indistinguishable from the bug it tolerates, and this
 * one took a bug report plus a full read to find the first time
 * (`LGR-28b4d75-5`, **template / ratified**; design DD9). An unexpected shape
 * now yields a schema with no fields, and the modal says so.
 *
 * The deploy window is handled by **ordering** — Compute before Hosting — not by
 * client tolerance.
 */
async function fetchSchemaDetail(id: string): Promise<Schema> {
  const body = await apiClient.get<RawSchemaDetail>(`/schemas/${encodeURIComponent(id)}`);
  return normalizeSchema(body as unknown as Record<string, unknown>);
}

async function uploadSchema(formData: FormData): Promise<RawSchemaUploadResponse> {
  // Read the file from FormData and send as JSON (API expects {name, content})
  const file = formData.get('file') as File | null;
  const name = (formData.get('name') as string) ?? file?.name?.replace(/\.[^.]+$/, '') ?? 'unnamed';

  let content = '{}';
  if (file) {
    content = await file.text();
  }

  const payload: Record<string, unknown> = { name, content };
  // R4.1 — the optional Display_Label, forwarded only when the user supplied
  // one. `upload_schema` accepts `displayLabel` on both the shapes it handles
  // (`schemas.py:1286`, `_DISPLAY_LABEL_KEYS`); this client uses the JSON body.
  //
  // Sent **verbatim** and gated on `trim()`, which are two different rules and
  // both are the producer's: an empty or whitespace-only label is the *absence*
  // of one (R4.2 makes "usable with no Display_Label at all" a first-class
  // state), while a label that survives that gate is stored with its padding
  // intact and with no uniqueness transformation (R4.7). So the gate trims and
  // the value does not.
  const displayLabel = formData.get('displayLabel');
  if (typeof displayLabel === 'string' && displayLabel.trim()) {
    payload.displayLabel = displayLabel;
  }

  return apiClient.post<RawSchemaUploadResponse>('/schemas/upload', payload);
}

async function updateFieldContext(params: { schemaId: string; fieldName: string; context: string }) {
  return apiClient.put<unknown>(`/schemas/${encodeURIComponent(params.schemaId)}/fields/${encodeURIComponent(params.fieldName)}/context`, { context: params.context });
}

export interface SetDisplayLabelVars {
  schemaId: string;
  /** Empty, whitespace-only or absent **clears** the label (R4.2, R4.12). */
  displayLabel: string;
}

/**
 * `PUT /api/schemas/{id}/display-label` — D2's rename (R4.9).
 *
 * **This is an attribute write and nothing else.** The request body is
 * `{"displayLabel": "<label>"}` and the handler writes `display_name` into the
 * Context_Sidecar, leaving the schema id, its backing object's key and that
 * object's bytes byte-identical (R4.10) and every referencing pipeline's
 * `target_schema_name` untouched (R4.11).
 *
 * **Do NOT send a label anywhere else.** R4.11's failure mode is silent and
 * lands six steps away: nothing type-distinguishes a Display_Label from a schema
 * id, and a label that reached `target_schema_name` would be interpolated
 * straight into candidate object keys by `schema_mapping`'s by-name fallback
 * (`schema_mapping/lambda_function.py:249-250` → `:296` →
 * `shared/target_schema_resolution.py:407`), making all six probes miss and
 * raising `Could not find uploaded schema` at `:317-319`. This function's only
 * argument that reaches the wire as an identifier is `schemaId`.
 */
async function setDisplayLabel(params: SetDisplayLabelVars): Promise<RawDisplayLabelResponse> {
  return apiClient.put<RawDisplayLabelResponse>(
    `/schemas/${encodeURIComponent(params.schemaId)}/display-label`,
    { displayLabel: params.displayLabel },
  );
}

/**
 * The schema listing. `data` stays `Schema[]`, exactly as before, so every
 * existing consumer is unaffected; `sourcesFailed` rides alongside it.
 *
 * Returning the failures on the hook result rather than as a second exported
 * hook is deliberate: a new export would have to be added to every `vi.mock`
 * factory that replaces the `hooks` module. Held-assertion inventory row 5's
 * mechanism: a `vi.mock` factory **replaces** the module, so any export it
 * omits is `undefined`; the component then calls `undefined()`
 * **during render, inside the test that renders it**. The failure is a stack
 * trace naming a value that is not a function — not an assertion diff — and the
 * file still collects and still reports per test. Amend the factory before
 * adding a hook to a component the factory's tests render.
 */
export function useSchemas() {
  const query = useQuery({
    queryKey: schemaKeys.all,
    queryFn: fetchSchemaListing,
    staleTime: 60_000,
  });
  return {
    ...query,
    data: query.data?.schemas,
    sourcesFailed: query.data?.sourcesFailed ?? [],
  };
}

export function useSchemaDetail(id: string | undefined) {
  return useQuery({
    queryKey: schemaKeys.detail(id!),
    queryFn: () => fetchSchemaDetail(id!),
    enabled: !!id,
    staleTime: 30_000,
  });
}

export function useUploadSchema() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: uploadSchema,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: schemaKeys.all });
    },
  });
}

/**
 * `PUT /api/schemas/{id}/fields/{name}/context` — R2.1, R2.3.
 *
 * **`onError` is the half that was missing.** This hook had `onSuccess` and no
 * `onError` at all, which is the mechanism behind R2.1's "Save does nothing":
 * a failed write left the editor sitting there with the entered text and no
 * statement either way, so the reader could not tell a stored annotation from a
 * discarded one.
 *
 * What the handler below does is the *cache* half — the caller's own
 * `mutate(vars, { onError })` still runs, and that is where the message and the
 * recoverable text live (`SchemaManagementPage`'s Context column).
 */
export function useUpdateFieldContext() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateFieldContext,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: schemaKeys.detail(variables.schemaId) });
    },
    onError: (_error, variables) => {
      // Refetch rather than leave the on-screen value standing. The write
      // failed, so the sidecar holds whatever it held before, and the column
      // must show that rather than the text the user typed — R2.1 keeps the
      // typed text recoverable *in the editor*, which is a different thing from
      // presenting it as stored.
      queryClient.invalidateQueries({ queryKey: schemaKeys.detail(variables.schemaId) });
    },
  });
}

async function deleteSchema(schemaId: string) {
  return apiClient.delete<unknown>(`/schemas/${encodeURIComponent(schemaId)}`);
}

/**
 * `DELETE /api/schemas/{id}` — R3.1, R3.6, R3.9.
 *
 * **`onError` is the half that was missing**, and it is why every failure read
 * as one fixed toast (design §5 risk 6). The endpoint has three refusals and
 * they are not interchangeable — see `RawSchemaDeleteError`.
 *
 * The handler below refetches the listing on a failure the API did **not**
 * classify. A `409`, `400` or `503` provably deleted nothing: R3.10 makes the
 * reference check precede the delete, so a refusal and a deletion can never both
 * be observable for one request, and the listing is already correct. A `500` is
 * the case with no such guarantee — it can only arrive from the `delete_object`
 * call itself or from something around it — so the listing is re-read rather
 * than assumed, which is what tells the user whether the object survived.
 */
export function useDeleteSchema() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteSchema,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: schemaKeys.all });
    },
    onError: (error) => {
      const status = error instanceof ApiError ? error.status : undefined;
      const refusal = status === 409 || status === 400 || status === 503;
      if (!refusal) {
        queryClient.invalidateQueries({ queryKey: schemaKeys.all });
      }
    },
  });
}

/**
 * D2's rename — R4.9, and the write-through R4.8 depends on.
 *
 * On success both cache entries are invalidated: the detail, because its
 * `displayLabel` has changed, and the **listing**, because R4.8 requires the
 * Schemas_Page and the Schema_Picker to display the same name for the same
 * schema and both of them are built from listing rows.
 *
 * > **Both endpoints now emit `displayLabel`** (p36 task 2.3 closed the gap this
 * > block used to describe). The listing resolves it in a post-pass over the
 * > enumerated rows — `_resolve_display_labels`, called from `get_schemas` at
 * > `schemas.py:774` — and the detail reads the **same**
 * > `_display_label_from_sidecar` gate (`schemas.py:862`, called at `:1590`), so
 * > the two cannot disagree about a label. Invalidating the listing therefore
 * > refetches rows that carry the label, and `normalizeSchema` (`:56`) folds the
 * > wire key into `display_label`.
 * >
 * > A row with no stored label carries **no key at all** rather than an empty
 * > one, so the three sites that display a name fall back to the schema id
 * > through the single `schemaDisplayName` (`utils/schema-display.ts:21-23`) —
 * > R4.8's *other* branch rather than a rendering fault. Those sites:
 * > `SchemaManagementPage.tsx`, `WizardStepSchema.tsx:171` and
 * > `SchemaSourceSelector.tsx:149`.
 * >
 * > A `setQueryData` write-through was **rejected** here: it would show the new
 * > label until the next listing refetch and then silently lose it, which is a
 * > value that is right for 60 seconds and wrong afterwards — the tolerant shape
 * > `LGR-28b4d75-5` (**template / ratified**) is about. Invalidating shows what
 * > the producer actually reports.
 */
export function useSetDisplayLabel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: setDisplayLabel,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: schemaKeys.detail(variables.schemaId) });
      queryClient.invalidateQueries({ queryKey: schemaKeys.all });
    },
    onError: (_error, variables) => {
      // The stored label is whatever the sidecar says, not what was typed.
      queryClient.invalidateQueries({ queryKey: schemaKeys.detail(variables.schemaId) });
    },
  });
}
