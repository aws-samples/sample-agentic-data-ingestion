// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Shared types for the Create Pipeline Wizard. */

export interface SchemaField {
  name: string;
  type: string;
  nullable: boolean;
  description: string;
}

/**
 * Step 5's approval gates, in **wizard state**. camelCase here; the wire and the
 * pipeline record are snake_case, and `buildApprovalConfigRequest`
 * (`CreatePipelinePage.tsx`) is the single place that translates — p64's
 * reconciliation of the casing p63 R1.2 reserved a decision for.
 *
 * Two per-field traps, both documented at `INITIAL_WIZARD_STATE` below:
 *
 * - `schemaMappingConfidenceThreshold` is a **percent** (0–100) here and a
 *   **fraction** on the record. Never read one as the other.
 * - `alwaysApproveSchemaChanges: true` means approval **is required** — the
 *   opposite of what the field name reads like. The UI label used to say the
 *   opposite too; that half is now FIXED (both the toggle in `WizardStepModel`
 *   and the review row read "Require approval for schema changes"). The
 *   misleading FIELD NAME remains, and the do-NOT below still stands: it is
 *   p63 R2/D3's to rename, polarity unchanged.
 */
export interface ApprovalConfig {
  schemaMappingEnabled: boolean;
  schemaMappingConfidenceThreshold: number;
  codeGenerationEnabled: boolean;
  alwaysApproveSchemaChanges: boolean;
}

/**
 * Step 5's data-quality settings, in **wizard state**. camelCase here; the wire
 * and the pipeline record are snake_case, and `buildQualityConfigRequest`
 * (`CreatePipelinePage.tsx`) is the single place that translates — the same
 * boundary `buildApprovalConfigRequest` occupies, for the same p63 R1.2 reason.
 *
 * p64 R7 / R6.6, design C7. This is a **peer** of `approvalConfig`, and on the
 * wire a peer of `approval_config` — deliberately NOT a key inside
 * `targetConfig`. Routing it through `target_config` would make it a template
 * variable for free, because `code_assembly` splats the persisted source and
 * target maps wholesale into the render context; C7 rejects that, because a
 * data-quality setting is not target configuration and `targetConfig` is a
 * free-form `Record<string, string>` with no validation, i.e. a place to hide an
 * unvalidated boolean.
 */
export interface QualityConfig {
  /**
   * R6.6's data-quality opt-in — **three-state, and `undefined` is a real
   * state**, not a stand-in for `false`.
   *
   * `undefined` means "whatever this template does by default": on for S3 Tables
   * (`glue_job_s3_tables.py.j2`), off for the other four. That is the posture
   * every template had before p64, it is what
   * `code_assembly.resolve_data_quality_enabled` falls back to, and it is why
   * `pipelines_crud._default_quality_config` deliberately seeds no value for
   * this key.
   *
   * **Do NOT change this to a plain `boolean` with a `false` default.** An
   * existing S3 Tables pipeline stores no `data_quality_enabled`; hydrating that
   * as `false` and re-submitting writes an explicit opt-OUT, removing the
   * row-level split and quarantine write that target type has had since P28 and
   * moving the pipeline's verdict from `enforced` to `advisory`. Nobody asked for
   * that and nothing reports it. `readDataQualityEnabled`
   * (`utils/quality-config.ts`) is the one reader; `buildQualityConfigRequest`
   * omits the wire key entirely when this is `undefined`.
   *
   * The three-state shape is `LGR-4854b3d-1`'s rule one value class over: a
   * `field_count` of `null` is never collapsed into `0`, because "the source
   * stated zero" and "no count was established" are different facts and only one
   * of them may change behaviour.
   */
  dataQualityEnabled?: boolean;

  /** R7.1's dedup opt-in. Off by default, on every target type. */
  dedupEnabled: boolean;

  /**
   * R7.2's duplicate-identifying columns. `[]` means every mapped target column,
   * which is what `dropDuplicates()` with no argument already does — so absence
   * and emptiness agree and neither needs a sentinel.
   *
   * Each entry reaches `dropDuplicates([...])` in the generated Glue job, so it
   * must pass the Identifier_Allowlist. `dedupColumnsError` reports an offender
   * on the step that owns the field; the refusal that matters is
   * `_validate_quality_config`'s 400, which rejects rather than filters.
   */
  dedupColumns: string[];
}

export interface WizardState {
  // Step 1
  pipelineName: string;
  description: string;

  // Step 2 (Source — P17)
  // Validates: P17 Requirement 17.1
  // P20 — `sourceConfig` may carry the manual-mode keys `vpc_id`,
  // `subnet_id`, `security_group_ids` (JSON-encoded list), and
  // `availability_zone` when `_connection_mode === 'manual'`. The
  // `Record<string, string>` shape is preserved; the backend's
  // `_normalise_sg_list` helper parses `security_group_ids` back into
  // `list[str]` before regex validation.
  sourceType: 's3_upload' | 'jdbc';
  sourceConfig: Record<string, string>;
  schedule: string | null;

  /**
   * p55 R8.4 — the Selected_Worksheet for an Excel_Workbook source, chosen at
   * pipeline-creation time and persisted as pipeline config (design §4.4,
   * decision W3-D5 arm (a)).
   *
   * A `string` and not `string | null` so "no selection" has exactly one
   * spelling, matching `schemaDisplayLabel` below for the same reason. `''` is
   * "no selection" **in wizard state only** — `handleSubmit` gates on
   * `worksheetSelectionIsSet` and omits the key entirely rather than sending
   * `''`, because an empty *recorded* selection refuses the run under R8.5
   * instead of letting a single-sheet workbook proceed under R8.1. The two
   * meanings of "empty" are not interchangeable on the wire; see
   * `utils/worksheet-selection.ts`.
   *
   * **Read this field defensively — a restored draft can make it `undefined`.**
   * Same hazard as `schemaDisplayLabel`: `useWizardDraft.readDraft` is a bare
   * `JSON.parse(raw) as T` (`useWizardDraft.ts:66-77`) and `handleRestoreDraft`
   * replaces the state rather than merging into it
   * (`CreatePipelinePage.tsx:294-296`), so a draft saved before this field
   * existed restores with `selectedWorksheet === undefined` even though the type
   * says `string`. Every consumer therefore uses `?? ''`.
   *
   * There is deliberately **no** list of worksheet names beside it: no workbook
   * exists at wizard time, for the three structural reasons enumerated in
   * `utils/worksheet-selection.ts`. The run names every sheet present when it
   * refuses, which is the first moment such a list exists.
   */
  selectedWorksheet: string;

  // Step 3 (Target)
  // P17 Task 20.3 — Validates: Requirements 14.1, 14.3.
  // `'rds_database'` was dropped from the union once the legacy `RdsDatabaseForm`
  // was removed. Persisted pipelines whose `target_config.type == "RDS_DATABASE"`
  // are mapped onto `'jdbc'` for display in the edit-flow code path
  // (`CreatePipelinePage.tsx`, Task 20.4).
  // P20 — `targetConfig` may carry the same manual-mode keys as
  // `sourceConfig` (`vpc_id`, `subnet_id`, `security_group_ids`,
  // `availability_zone`) when `_connection_mode === 'manual'`.
  targetType: 's3_tables' | 'glue_catalog' | 'jdbc' | 's3_parquet';
  targetConfig: Record<string, string>;

  // P19 Decision 1 — schema fields are split by side so Step 3's `mirror_source`
  // mode can read the source-side schema while `'detected'` continues to read
  // the target-side schema. Populated by `WizardStepSource` (source) and
  // `WizardStepTarget` (target) respectively. Validates: Requirements 8.1, 8.2.
  detectedSourceSchema: SchemaField[] | null;
  detectedTargetSchema: SchemaField[] | null;

  /**
   * @deprecated P19 — use `detectedTargetSchema` instead. This field is kept
   * as an optional alias for one release so any consumer that has not yet
   * migrated continues to type-check; new code MUST read and write
   * `detectedTargetSchema`. The field is no longer populated by
   * `INITIAL_WIZARD_STATE` and will be removed after the migration window.
   */
  detectedSchema?: SchemaField[] | null;

  // Step 4 (Schema)
  // P19 — `'mirror_source'` added to expose the source-side detected schema as
  // a target schema source for JDBC pipelines (Decision 3, Req 8.1).
  schemaSource: 'detected' | 'upload' | 'existing' | 'mirror_source';
  schemaFields: SchemaField[];
  existingSchemaId: string | null;

  /**
   * p36 R2.1/R2.3 — the optional Display_Label for a schema uploaded inline by
   * the wizard (`Wizard_Inline_Upload`). Presentation only: it never reaches the
   * uploaded schema's id, its object key, or the `target_schema` the wizard
   * sends to `/pipelines/provision` (D1, ratified at p36 requirement 2.5).
   *
   * A `string` and not `string | null` so "no label" has exactly one spelling —
   * matching `uploadDisplayLabel` on the Schemas page
   * (`SchemaManagementPage.tsx:232`, gated on `.trim()` at `:301`). Independent
   * of `schemaFields` and of the field count: do NOT derive one from the other
   * (`LGR-4854b3d-1`, template / ratified; see the note on
   * `existingSchemaDeclaresNoFields` above).
   *
   * **Read this field defensively — a restored draft can make it `undefined`.**
   * `useWizardDraft.readDraft` returns `JSON.parse(raw) as T`, a bare cast with
   * no validation and no merge (`useWizardDraft.ts:66-77`), and
   * `handleRestoreDraft` does `setState(restored)`
   * (`CreatePipelinePage.tsx:294-296`). So a draft saved in a session before
   * this field existed restores with `schemaDisplayLabel === undefined` even
   * though the type says `string`, and `undefined.trim()` is a `TypeError` at
   * submit. Every consumer therefore uses `?? ''` where the value feeds an input
   * and `?.trim()` where it is gated.
   *
   * The better fix is to merge on restore — `setState({ ...INITIAL_WIZARD_STATE,
   * ...restored })` in `handleRestoreDraft` — which would repair this for every
   * wizard field at once. It was **rejected for scope here**, not on merit: it
   * changes restore semantics for all ~20 fields and belongs to a spec that owns
   * the draft. Prefer it over widening the defensive reads if you are already
   * touching `useWizardDraft` or `handleRestoreDraft`.
   */
  schemaDisplayLabel: string;

  /**
   * p35 R5.1/R5.2 — set by the `existing` picker when the selected schema
   * **declares** zero fields, which is what blocks the schema step.
   *
   * Optional and never defaulted to `false` in `INITIAL_WIZARD_STATE`, because
   * "not yet determined" and "determined to be fine" must both allow the
   * advance: after DD4 the listing reports `fieldCount: null` for every uploaded
   * and every S3 Tables schema, and the field list only arrives from
   * `GET /api/schemas/{id}`. A guard reading `schemaFields.length` instead would
   * block the step for every schema a user uploads, and would block it while the
   * fetch was still in flight.
   */
  existingSchemaDeclaresNoFields?: boolean;

  // P19 — set to `true` the first time the user picks a tile in Step 3, so the
  // pre-selection effect skips on subsequent renders (Req 3.4, Req 8.2).
  schemaSourceExplicitlySet: boolean;

  // Step 5 (Model)
  aiModel: string;
  approvalConfig: ApprovalConfig;

  /**
   * p64 R7 / R6.6 — the per-pipeline data-quality settings, set at creation
   * (R7.3) and round-tripped through the edit flow (R7.4).
   *
   * **Read this field defensively — a restored draft can make it `undefined`.**
   * Same hazard as `schemaDisplayLabel` and `selectedWorksheet`, and R7.5 names
   * it: `useWizardDraft.readDraft` is a bare `JSON.parse(raw) as T`
   * (`useWizardDraft.ts:66-77`) and `handleRestoreDraft` replaces the state
   * rather than merging into it (`CreatePipelinePage.tsx:294-296`), so a draft
   * saved before this field existed restores with `qualityConfig === undefined`
   * even though the type says otherwise. Every consumer therefore guards —
   * `buildQualityConfigRequest` takes `QualityConfig | undefined`,
   * `dedupColumnsError` accepts a missing list, and the Review step and the Model
   * step's controls fall back to `INITIAL_WIZARD_STATE.qualityConfig`.
   *
   * The better fix is still to merge on restore (see `schemaDisplayLabel` above);
   * it remains out of scope for the same reason.
   */
  qualityConfig: QualityConfig;
}

export const INITIAL_WIZARD_STATE: WizardState = {
  pipelineName: '',
  description: '',
  // P17 Task 16.1 — required fields on WizardState; defaults per Requirement 17.1.
  // (Task 16.2 nominally owns these defaults, but they are co-located here so the
  // interface change in 16.1 keeps types.ts compiling.)
  sourceType: 's3_upload',
  sourceConfig: {},
  schedule: null,
  // p55 R8.4 — `''` is "no selection", and `handleSubmit` omits the key rather
  // than sending it. As with `schemaDisplayLabel`, this initial value does NOT
  // protect a draft saved before the field existed, because `handleRestoreDraft`
  // replaces the state rather than merging into it.
  selectedWorksheet: '',
  targetType: 's3_tables',
  targetConfig: {},
  // P19 — split schema state, both null until the corresponding Detect Schema
  // flow runs. Legacy `detectedSchema` is intentionally omitted: it is an
  // optional `@deprecated` alias on the interface, not part of the initializer.
  detectedSourceSchema: null,
  detectedTargetSchema: null,
  // P19 Req 8.2 — keep `'detected'` as the initial default so non-JDBC
  // pipelines retain pre-P19 behaviour. The Step 3 pre-selection effect
  // (Task 10.2) overrides this to `'mirror_source'` for JDBC targets.
  schemaSource: 'detected',
  schemaFields: [],
  existingSchemaId: null,
  // p36 R2.3 — `''` is "no label". See the interface for the draft-restore
  // hazard: this initial value does NOT protect a draft saved before the field
  // existed, because `handleRestoreDraft` replaces the state rather than merging
  // into it (`CreatePipelinePage.tsx:294-296`).
  schemaDisplayLabel: '',
  schemaSourceExplicitlySet: false,
  aiModel: '',
  // p64 (authorised addition to task 2a) — these values are now LIVE. Until the
  // key casing was reconciled they reached no reader, so what a pipeline actually
  // got was each reader's own fallback. Every value here must therefore agree with
  // that fallback, or merely opening the wizard changes a gate.
  //
  // `alwaysApproveSchemaChanges` was `false` and is now `true`, and that is a fix
  // rather than a policy change: `schema_mapping/lambda_function.py:1657` reads
  // `approval_config.get("always_approve_schema_changes", True)`, and
  // `pipelines_crud._default_approval_config` seeds `True`. Sending the old
  // `false` for an untouched toggle would have turned the schema-change approval
  // gate OFF for every wizard-created pipeline, with nobody having asked — a
  // reduction in human oversight delivered as a casing fix. p63 R1.3 named this
  // three-way disagreement; p63 C3 resolves it in this same direction.
  //
  // The label on this toggle used to read "Auto-approve schema changes", the
  // OPPOSITE of what `true` does — so the fail-closed default above displayed as
  // "Auto-approve: Yes". That is now FIXED: the toggle
  // (`WizardStepModel.tsx`, `data-testid="auto-approve-toggle"`) and the review row
  // (`WizardStepReview.tsx`) both read "Require approval for schema changes". Only
  // the RELABEL half was done; the field, the `always_approve_schema_changes` wire
  // key, the record attribute and the `data-testid` are all unchanged, and the
  // rename half remains p63 R2/D3's.
  //
  // The do-NOT is UNCHANGED and still binding: do NOT "fix" the remaining
  // name/meaning mismatch by flipping this value or by inverting the stored
  // polarity. p63 R2.9 records inversion as a standing do-NOT, because every
  // existing record carries the current spelling and reusing the key with inverted
  // meaning flips every pipeline in the account.
  //
  // `schemaMappingConfidenceThreshold` is a PERCENT; the wire and the record carry
  // a fraction (`buildApprovalConfigRequest` in `CreatePipelinePage.tsx`
  // converts). 80 here is 0.8 on the record, against
  // `shared/config.py`'s `CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.85` — a knowing
  // disagreement with no consequence today, because the attribute has no reader
  // anywhere in `lambda/`, `shared/` or `mcp/`. Deliberately left as-is: whether a
  // per-pipeline threshold may loosen the shared floor is p63 R9/D2's decision
  // (stricter-only, refused rather than clamped), and moving this number now would
  // pre-empt it.
  approvalConfig: {
    schemaMappingEnabled: true,
    schemaMappingConfidenceThreshold: 80,
    codeGenerationEnabled: true,
    alwaysApproveSchemaChanges: true,
  },
  // p64 R7.1 / R6.6 — these values are LIVE from the moment they exist: the
  // submit path sends them and `pipelines_crud._submitted_quality_config`
  // persists them verbatim. So each one must agree with what a pipeline would
  // have got had the wizard said nothing, or merely opening the wizard changes a
  // pipeline's behaviour. That is the trap `alwaysApproveSchemaChanges` fell into
  // above — seeded `false` against readers defaulting to `True`, which would have
  // turned the schema-change approval gate off for every wizard-created pipeline.
  //
  // Checked one key at a time against the readers:
  //
  // * `dedupEnabled: false` agrees with `code_assembly.resolve_quality_config`,
  //   which returns `dedup_enabled=False` for a record with no `quality_config`
  //   at all, and with `_default_quality_config`, which seeds `False`. Dedup is
  //   off everywhere today, so stating it costs nothing and is true (R7.1).
  // * `dedupColumns: []` agrees the same way, and `[]` is not a weaker "unset" —
  //   it MEANS every mapped target column, which is `dropDuplicates()`'s own
  //   no-argument behaviour (R7.2).
  // * `dataQualityEnabled` is **deliberately ABSENT**, matching
  //   `_default_quality_config`'s deliberate omission of the same key. Its
  //   default is per TEMPLATE, not flat: `resolve_data_quality_enabled` honours
  //   an explicit `bool` and otherwise falls back to `DQ_DEFAULT_ON_TEMPLATES`,
  //   which is on for `glue_job_s3_tables.py.j2` and off for the other four —
  //   exactly the posture each template had before p64. Seeding `false` here
  //   would write an explicit opt-OUT onto every new S3 Tables pipeline, silently
  //   removing a DQ block that target type has had since P28 and moving its
  //   verdict from `enforced` to `advisory`. R6.6 forbids that in both
  //   directions.
  //
  // Absence is spelled by omitting the key rather than writing
  // `dataQualityEnabled: undefined`, so `'dataQualityEnabled' in
  // INITIAL_WIZARD_STATE.qualityConfig` is false and a membership test can tell
  // the two apart — the same distinction `_submitted_quality_config` draws with
  // `key not in data` rather than `data.get(key)`.
  qualityConfig: {
    dedupEnabled: false,
    dedupColumns: [],
  },
};

export interface WizardStepProps {
  state: WizardState;
  onChange: (state: WizardState) => void;
}
