// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMemo, useState } from 'react';
import Wizard from '@cloudscape-design/components/wizard';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { useToast } from '../contexts/ToastContext';
import { usePipelines } from '../hooks';
import { useWizardDraft } from './wizard/useWizardDraft';
import { WizardStepName, validatePipelineName } from './wizard/WizardStepName';
import { WizardStepSource } from './wizard/WizardStepSource';
import { WizardStepTarget } from './wizard/WizardStepTarget';
import { WizardStepSchema } from './wizard/WizardStepSchema';
import { WizardStepModel } from './wizard/WizardStepModel';
import { WizardStepReview } from './wizard/WizardStepReview';
import { INITIAL_WIZARD_STATE } from './wizard/types';
import type { ApprovalConfig, QualityConfig, SchemaField, WizardState } from './wizard/types';
// p36 R2.8 — one definition of the Display_Label request key, shared with the
// Schemas page's upload path. `utils/schema-display.ts` is a pure util that no
// `vi.mock` factory replaces, so the constant cannot read back `undefined` and
// put `{ undefined: label }` on the wire.
import { DISPLAY_LABEL_REQUEST_KEY } from '../utils/schema-display';
// p55 R8.4 — the Selected_Worksheet's wire key, its "is one set" gate and its
// only workbook-free validation, all from one pure module no `vi.mock` factory
// replaces (the same reason `schema-display.ts` holds the label's key).
import {
  SELECTED_WORKSHEET_REQUEST_KEY,
  worksheetSelectionError,
  worksheetSelectionIsSet,
} from '../utils/worksheet-selection';
// p64 R7 / R6.6 — the Quality_Config's wire keys, its dedup-column rule and the
// three-state read of `data_quality_enabled`, all from one pure module for the
// same reason the two above are imported rather than transcribed
// (`LGR-eee8ac8-5`: a second spelling would silently never match).
import {
  DATA_QUALITY_ENABLED_REQUEST_KEY,
  DEDUP_COLUMNS_REQUEST_KEY,
  DEDUP_ENABLED_REQUEST_KEY,
  QUALITY_CONFIG_REQUEST_KEY,
  dedupColumnsError,
} from '../utils/quality-config';
// The record→state reader, shared with `PipelineFormModal` so both editors of one
// pipeline read the same record the same way. Lives beside the controls it feeds.
import { hydrateQualityConfig } from '../components/QualitySettingsFields';

/**
 * P17 Task 20.4 — Validates: Requirements 14.3, 14.4.
 *
 * Edit-flow migration utility: a persisted pipeline whose `target_config.type`
 * is the legacy `'RDS_DATABASE'` is hydrated into the wizard as a `'jdbc'`
 * target so the consolidated `JdbcTargetForm` renders identically for both
 * shapes. The persisted `type` value is normalised to `'JDBC'` so the next
 * save through `handleSubmit` writes the unified shape.
 *
 * Exported for future edit flows and for unit-testing.
 */
export interface PersistedTargetConfig {
  type?: string;
  [key: string]: unknown;
}

export interface MigratedTarget {
  targetType: WizardState['targetType'];
  targetConfig: Record<string, string>;
}

export function migrateLegacyTargetConfig(persisted: PersistedTargetConfig): MigratedTarget {
  const rawType = (persisted.type ?? '').toString().toUpperCase();
  const stringConfig: Record<string, string> = {};
  for (const [k, v] of Object.entries(persisted)) {
    if (v == null) continue;
    stringConfig[k] = typeof v === 'string' ? v : String(v);
  }
  if (rawType === 'JDBC' || rawType === 'RDS_DATABASE') {
    stringConfig.type = 'JDBC';
    return { targetType: 'jdbc', targetConfig: stringConfig };
  }
  if (rawType === 'GLUE_CATALOG') {
    return { targetType: 'glue_catalog', targetConfig: stringConfig };
  }
  if (rawType === 'NEW_S3_TABLE_BUCKET' || rawType === 'EXISTING_S3_TABLE_BUCKET') {
    return { targetType: 's3_tables', targetConfig: stringConfig };
  }
  if (rawType === 'S3_PARQUET') {
    return { targetType: 's3_parquet', targetConfig: stringConfig };
  }
  // Unknown / empty types fall through to the wizard default — `s3_tables`.
  return { targetType: 's3_tables', targetConfig: stringConfig };
}

/**
 * Build the `source_config` payload for the create/provision call.
 *
 * The wizard's source form stores the database namespace and table under the
 * user-friendly keys `schema` / `table`, but the backend's canonical
 * pipeline_record shape (and `_validate_jdbc_source_config`) requires the
 * `jdbc_schema` / `jdbc_table` keys — the same aliasing the Detect Schema
 * call performs via `buildDetectSchemaSourceConfig` in WizardStepSource.
 * Without this, JDBC pipeline creation fails server-side with HTTP 400
 * ("source_config.jdbc_schema is required ...").
 *
 * It also materialises `read_mode`'s default. The source form treats
 * `read_mode` as a DISPLAY default (`sourceConfig.read_mode || 'full_table'`)
 * and only writes the key into state when the user actively changes the
 * toggle. A user who leaves it on the default "Full table" therefore never
 * persists `read_mode`, and the backend rejects the create with HTTP 400
 * ("source_config.read_mode is required ..."). We backfill the same
 * 'full_table' default here so the wire payload always carries it.
 *
 * Non-JDBC sources pass through unchanged. Exported for unit testing.
 */
export function buildCreateSourceConfig(
  sourceType: WizardState['sourceType'],
  cfg: Record<string, string>,
): Record<string, string> {
  if (sourceType !== 'jdbc') return { ...cfg };
  const { schema, table, ...rest } = cfg;
  const out: Record<string, string> = { ...rest };
  if (schema) out.jdbc_schema = schema;
  if (table) out.jdbc_table = table;
  // Materialise the read_mode default so the backend's required-field
  // check passes even when the user never touched the Read Mode toggle.
  if (!out.read_mode) out.read_mode = 'full_table';
  return out;
}

/**
 * The Approval_Config's shape **on the wire** — snake_case, and the confidence
 * threshold as a fraction.
 *
 * An earlier revision deliberately reserved this decision rather than making it;
 * it is now settled. The wizard's own state stays camelCase, because that is what every
 * React consumer reads; this interface is the boundary, and it is the ONLY place
 * the wire spelling exists on this side.
 *
 * These four keys are the ones the readers read **today** — deliberately not
 * p63 C4's proposed `require_approval_on_schema_change`. That rename is a
 * separate change with a compatibility fallback and nine-plus sites, and p63 D3
 * records it; adopting its vocabulary here would land half a rename.
 */
export interface ApprovalConfigRequest {
  schema_mapping_enabled: boolean;
  /**
   * Optional **only** because `PipelineFormModal`'s edit path omits it — see
   * `buildApprovalConfigRequest`'s `includeConfidenceThreshold` option and
   * `ApprovalGatesFields`' header for why. The wizard's create path always sends
   * it, so this is not a loosening of the create contract.
   */
  schema_mapping_confidence_threshold?: number;
  code_generation_enabled: boolean;
  always_approve_schema_changes: boolean;
}

/**
 * The wizard's slider is a **percent**; the persisted attribute is a
 * **fraction**. Convert, do not rename around it.
 *
 * The two units are not a stylistic difference. `WizardStepModel.tsx`'s Slider
 * is `min={0} max={100} step={5}` and renders the value with a `%` suffix, while
 * the persisted attribute's unit is set by the only thing that writes it —
 * `pipelines_crud._default_approval_config`, which seeds
 * `Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))`, i.e. `0.85`. Sending the
 * slider's `80` under the readers' key would put two units under one attribute
 * name: percent for wizard-created pipelines, fraction for defaulted ones. Any
 * future fraction-comparing reader would then read `80` as "never auto-approve".
 */
export function percentToConfidenceFraction(percent: number): number {
  return percent / 100;
}

/**
 * The inverse, for the hydrate direction. Rounded to a whole percent, and
 * deliberately **not clamped**.
 *
 * Rounding: the slider's domain is whole percents in steps of 5, so a stored
 * value off that grid is not representable by this control either way, and the
 * multiplication is not exact — `0.55 * 100` is `55.00000000000001`, so an
 * unrounded round trip would feed the Slider a value off its own step grid. 55 is
 * the only multiple of 5 affected; off the grid it is ordinary (`0.07 * 100` is
 * `7.000000000000001`), and nothing stops a record carrying such a value.
 *
 * Not clamped: p63 D2 rejected silent clamping precisely because a control that
 * displays 70% while the system enforces 85% manufactures a new inert control.
 * Range enforcement belongs to the boundary (p64 task 2a.2 / R10.4) and to p63
 * R9's stricter-only resolver, where a refusal is visible. Showing an
 * out-of-range stored value as what it is beats quietly redrawing it.
 */
export function confidenceFractionToPercent(fraction: number): number {
  return Math.round(fraction * 100);
}

/**
 * Serialise the wizard's Approval_Config for `POST /pipelines/provision`.
 *
 * **This is the single normalisation authority** (p63 R1.2 requires exactly one,
 * and p64 places it here). The backend persists a submitted `approval_config`
 * **verbatim** — `pipelines_crud._submitted_approval_config`, p64 task 2a.1 — so
 * whatever this function emits is what lands on the record and what
 * `schema_mapping` and `test_code` read. There is no server-side normaliser and
 * no camelCase spelling on the wire.
 *
 * Why this side rather than the server: it adds **no** new accepted wire shape.
 * The server already accepted these four snake_case keys from
 * `PipelineFormModal.tsx:103-106` on the edit path and persisted them; this makes
 * the create path spell the API the same way its sibling client already does. A
 * server-side camelCase normaliser would instead make a second spelling legal
 * forever, and — because `schema_mapping:1669` tests `not schema_mapping_enabled`
 * by truthiness rather than by identity — would newly let a non-boolean falsy
 * value arriving under a camelCase key disable a governance gate. That reachability
 * does not exist today and this direction does not create it.
 *
 * All four keys are always sent **on the create path**. A partial submission is
 * legal (the handler persists what it is given) but pointless there: the wizard
 * always has a value for all four, and omitting one would record "no opinion" for a
 * control the user just looked at.
 *
 * The **edit** path is the exception, and it is a deliberate one rather than a
 * partial implementation — see `includeConfidenceThreshold` below.
 */
export interface BuildApprovalConfigOptions {
  /**
   * Emit `schema_mapping_confidence_threshold`. Defaults to `true`; only
   * `PipelineFormModal`'s edit path passes `false`.
   *
   * **Why an option and not simply "always send it".** That key has no reader
   * anywhere in `lambda/`, `shared/` or `mcp/` — the auto-approval comparison uses
   * `shared/config.py`'s `CONFIDENCE_AUTO_APPROVE_THRESHOLD` directly, and
   * `create_infrastructure/lambda_function.py:620` names the key explicitly to say
   * "Not this one." `edit_pipeline` merges `approval_config` **per key**
   * (`LGR-7bea749-1`), so a key this function omits is genuinely left alone on the
   * record, and a key it emits is genuinely written.
   *
   * That is what makes the difference matter. On create there is no prior value to
   * overwrite. On edit there is: a record carrying no threshold hydrates to the
   * wizard's default of `80`, so sending it would write `0.8` onto a pipeline
   * against `_default_approval_config`'s seeded `Decimal("0.85")` — resolving a
   * knowing disagreement that **p63 R9/D2 owns** (stricter-only, refused rather
   * than clamped) as a side effect of an unrelated edit, for a control the operator
   * never saw. `ApprovalGatesFields` renders no threshold control for the same
   * reason; the two halves must agree or one of them is lying.
   *
   * **Why an option rather than a second builder or a caller-side `delete`.** p63
   * R1.2 permits exactly one normalisation authority for this sub-object and p64
   * 2a.1b placed it here. A second serialiser in the modal — or the modal stripping
   * a key off this function's output — would be a second policy on which keys reach
   * the record, which is the arrangement that produced the subset divergence in the
   * first place. One function, one documented arm.
   */
  includeConfidenceThreshold?: boolean;
}

export function buildApprovalConfigRequest(
  config: ApprovalConfig,
  options: BuildApprovalConfigOptions = {},
): ApprovalConfigRequest {
  // The full map is built unconditionally and then projected, rather than the
  // threshold being spread in conditionally. Two reasons, both about keeping this
  // pinnable: `tests/contracts/test_approval_config_casing_cross_language.py`
  // parses this function's object-literal keys with a regex anchored at exactly
  // four spaces of indentation, and separately asserts
  // `schema_mapping_confidence_threshold: percentToConfidenceFraction(` appears
  // adjacent in this block. A conditional spread would indent the key out of that
  // regex's reach and the cross-language key-set check would silently stop seeing
  // it. Do not "simplify" this into a spread.
  const full: ApprovalConfigRequest = {
    schema_mapping_enabled: config.schemaMappingEnabled,
    schema_mapping_confidence_threshold: percentToConfidenceFraction(
      config.schemaMappingConfidenceThreshold,
    ),
    code_generation_enabled: config.codeGenerationEnabled,
    always_approve_schema_changes: config.alwaysApproveSchemaChanges,
  };
  if (options.includeConfidenceThreshold === false) {
    const { schema_mapping_confidence_threshold: _omitted, ...rest } = full;
    return rest;
  }
  return full;
}

/**
 * A persisted `approval_config` as either editor's caller has it.
 *
 * Members are `unknown` rather than the concrete `boolean` / `number` the
 * `Pipeline` type declares, because a record predating the boundary's
 * `_validate_approval_config` could hold anything under any of them and this is the
 * reader that has to survive that. Same shape and same reasoning as
 * `PersistedQualityConfig` (`components/QualitySettingsFields.tsx`);
 * `LGR-7bea749-5` documents why a *display* type stays concrete while a *reader's*
 * input type does not. Concrete shapes are assignable to this one, so both call
 * sites pass their own type unchanged.
 */
export interface PersistedApprovalConfig {
  schema_mapping_enabled?: unknown;
  schema_mapping_confidence_threshold?: unknown;
  code_generation_enabled?: unknown;
  always_approve_schema_changes?: unknown;
}

/**
 * Read one boolean gate off a record, treating **absent and non-boolean alike**.
 *
 * That equivalence is not defensive tidiness — it is what every reader of these
 * keys already does. `schema_mapping/lambda_function.py:1657` is
 * `approval_config.get("always_approve_schema_changes", True)` consumed by an `if`,
 * and `:1669` tests `not schema_mapping_enabled` by **truthiness rather than
 * identity**, so a stored `"false"` string is truthy there and the gate holds. A
 * `??` read would carry that string into editor state and hand it to a `Toggle` as
 * `checked`, rendering the opposite of what the backend enforces — a control
 * disagreeing with the system, which is the defect class p63 exists to remove.
 *
 * The fallback is a parameter rather than a hardcoded `true` so a future divergence
 * between the three gates' defaults stays visible at the call site. Today all three
 * are `true`, at their readers and in `INITIAL_WIZARD_STATE` alike; that agreement
 * is load-bearing and `LGR-b993b94-1` records what happened when it last broke.
 */
export function readApprovalGate(raw: unknown, fallback: boolean): boolean {
  return typeof raw === 'boolean' ? raw : fallback;
}

/**
 * Record → editor state for the Approval_Config. The read-side counterpart of
 * `buildApprovalConfigRequest`, and like it a **single** authority: both editors of
 * the pipeline object call this rather than each spelling the four reads.
 *
 * Returns all **four** fields even though `ApprovalGatesFields` renders only three.
 * The read side carries no hazard — hydrating a value the modal will not submit
 * costs nothing, and the wizard needs the fourth for its Slider. The exclusion
 * lives on the **write** side, in `buildApprovalConfigRequest`'s
 * `includeConfidenceThreshold`.
 *
 * Two per-key traps, both load-bearing:
 *
 * - `always_approve_schema_changes` hydrates to **`true`** when absent, because
 *   `true` means approval **is required**. A record written before the key existed
 *   is a record whose gate is on; reading absence as `false` and saving turns that
 *   gate OFF for a pipeline nobody asked to change. Same class of hazard as
 *   `data_quality_enabled`'s `?? false` (`LGR-919254d-2`), and the reason this is a
 *   named reader rather than an inline `??`.
 * - `schema_mapping_confidence_threshold` is a **fraction** on the record and a
 *   **percent** in state. `typeof === 'number'` rather than `??` so a JSON `null`
 *   falls back too: `Math.round(null * 100)` is `0`, which would present a
 *   never-auto-approve gate as an approve-everything one.
 */
export function hydrateApprovalConfig(
  persisted: PersistedApprovalConfig | undefined,
): ApprovalConfig {
  const raw = persisted ?? {};
  const initial = INITIAL_WIZARD_STATE.approvalConfig;
  return {
    schemaMappingEnabled: readApprovalGate(
      raw.schema_mapping_enabled,
      initial.schemaMappingEnabled,
    ),
    schemaMappingConfidenceThreshold:
      typeof raw.schema_mapping_confidence_threshold === 'number'
        ? confidenceFractionToPercent(raw.schema_mapping_confidence_threshold)
        : initial.schemaMappingConfidenceThreshold,
    codeGenerationEnabled: readApprovalGate(
      raw.code_generation_enabled,
      initial.codeGenerationEnabled,
    ),
    // `?? true` by way of `initial`, and it MUST stay `true`. See the docblock.
    // Mutation-checked: spelled `?? false`, this fails 4 tests in
    // `components/__tests__/PipelineFormModal.approval.test.tsx` and 1 in
    // `__tests__/CreatePipelinePage.hydrate.test.ts`.
    alwaysApproveSchemaChanges: readApprovalGate(
      raw.always_approve_schema_changes,
      initial.alwaysApproveSchemaChanges,
    ),
  };
}

/**
 * The Quality_Config's shape **on the wire** — snake_case, and a peer of
 * `approval_config` rather than a key inside `target_config`.
 *
 * p64 R7 / R6.6, design C7. `data_quality_enabled` is **optional here and that is
 * the contract, not laxity**: omitting it means "this pipeline has no opinion, use
 * the template's own default", which is a state the record can hold and
 * `pipelines_crud._default_quality_config` deliberately preserves by seeding no
 * value for that key. The other two are always sent, because their default IS
 * unconditional — dedup is off on every target type today, so stating it is both
 * cheap and true.
 */
export interface QualityConfigRequest {
  data_quality_enabled?: boolean;
  dedup_enabled: boolean;
  dedup_columns: string[];
}

/**
 * Serialise the wizard's Quality_Config for `POST /pipelines/provision`.
 *
 * **The single normalisation authority for this setting**, in the same place
 * `buildApprovalConfigRequest` sits and for the same reason: p63 R1.2 requires
 * exactly one, p64 2a.1b put it at this boundary, and
 * `pipelines_crud._submitted_quality_config` persists what arrives **verbatim** —
 * so whatever this function emits is what lands on the record and what
 * `code_assembly` reads. There is no server-side camelCase normaliser and adding
 * one is forbidden: it would legalise a second wire spelling permanently with
 * nothing to arbitrate between them.
 *
 * **`data_quality_enabled` is omitted unless the operator actually set it, and
 * that omission is the load-bearing part of this function.** An S3 Tables pipeline
 * has data quality on by template default and stores no such key. Sending `false`
 * for an untouched control would write an explicit opt-OUT: the row-level split
 * and quarantine write that target type has had since P28 would stop being
 * generated, and `shared/quality_verdict.py` would report the pipeline's verdict as
 * `advisory` where it used to say `enforced`. Nobody asked for that, no error
 * surfaces, and a governance verdict quietly changes meaning. R6.6 forbids exactly
 * this, in exactly this direction, and `_default_quality_config`'s docstring makes
 * the same argument for the server's own seed.
 *
 * So the key is emitted only for a real `boolean`. A `dict` with the key absent and
 * a `dict` with the key set to `undefined` are the same thing after
 * `JSON.stringify`, but they are **not** the same thing to `Object.keys`, and the
 * tests assert the key set — so the conditional spread is deliberate rather than
 * defensive noise.
 *
 * Takes `QualityConfig | undefined` because R7.5 requires every consumer to read
 * defensively: `handleRestoreDraft` replaces state rather than merging into it, so
 * a draft saved before `qualityConfig` existed restores it as `undefined` despite
 * the type. Such a draft submits the documented defaults, which is what it would
 * have got before the field existed.
 */
export function buildQualityConfigRequest(
  config: QualityConfig | undefined,
): QualityConfigRequest {
  const resolved = config ?? INITIAL_WIZARD_STATE.qualityConfig;
  return {
    ...(typeof resolved.dataQualityEnabled === 'boolean'
      ? { [DATA_QUALITY_ENABLED_REQUEST_KEY]: resolved.dataQualityEnabled }
      : {}),
    [DEDUP_ENABLED_REQUEST_KEY]: resolved.dedupEnabled ?? false,
    // Sent verbatim, never filtered. `_validate_quality_config` REFUSES an
    // offending entry with a 400 rather than dropping it, for `LGR-4ea43f3-4`'s
    // reason one value class over — a dedup key list quietly missing a column
    // deduplicates on the wrong identity — so this side must not pre-empt that by
    // sanitising. The Model step reports the problem inline and its navigation
    // guard stops the submit, which is how the operator sees it before the 400.
    [DEDUP_COLUMNS_REQUEST_KEY]: [...(resolved.dedupColumns ?? [])],
  };
}

/**
 * P19 Task 11.2 — Validates: Requirements 6.8, 8.4, 9.2, 9.3, 9.4.
 *
 * Edit-flow hydration utility: builds a `WizardState` from a persisted
 * pipeline record so the wizard can be opened pre-populated for editing.
 *
 * Behavior contract:
 * - `detectedTargetSchema` is hydrated from the persisted record's
 *   `schema_fields` (or fields-shaped equivalent) when present; the legacy
 *   `detectedSchema` field is intentionally NOT written (Decision 1).
 * - `detectedSourceSchema` is left `null` — the source crawler does not run
 *   automatically in an edit session and the user must click Detect Schema
 *   to repopulate it.
 * - `schemaSourceExplicitlySet` is set to `true` so the Step 3 pre-selection
 *   `useEffect` (Task 10.2) does NOT override the user's previously-persisted
 *   choice on the first render of an edit session.
 * - `schemaSource` is read verbatim from the persisted record. Missing or
 *   non-allowed values fall back to `'detected'` for backward compatibility
 *   with pre-P19 records (Req 9.2).
 * - `target_config.write_mode == 'merge'` is preserved verbatim — the
 *   `JdbcTargetForm` renders the Req 6.8 info alert and forces the user to
 *   re-pick a tile before Save. No silent backfill (Decision 4).
 *
 * The function returns a fully-formed `WizardState` (using
 * `INITIAL_WIZARD_STATE` for any field the persisted record does not
 * provide) so callers can pass the result directly to `setState`.
 *
 * Exported for unit-testing and for any future edit-flow page that wires
 * an existing pipeline back into the wizard.
 */
export interface PersistedPipelineForEdit {
  pipeline_name?: string;
  pipeline_description?: string;
  description?: string;
  ai_model?: string;
  model_id?: string;
  target_schema?: string;
  target_config?: PersistedTargetConfig;
  schema_source?: string;
  schema_fields?: unknown;
  source_type?: string;
  source_config?: Record<string, unknown>;
  schedule?: string | null;
  approval_config?: {
    schema_mapping_enabled?: boolean;
    schema_mapping_confidence_threshold?: number;
    code_generation_enabled?: boolean;
    always_approve_schema_changes?: boolean;
  };
  /**
   * p64 R7.4 — the Quality_Config as the record carries it.
   *
   * Every field is optional, and `data_quality_enabled`'s optionality is a real
   * three-state rather than a convenience: a record written by
   * `_default_quality_config` carries `dedup_enabled` and `dedup_columns` and
   * deliberately **no** `data_quality_enabled`. `dataQualityEnabled` is typed
   * `unknown` because a record predating `_validate_quality_config` could hold a
   * non-boolean under it, and `readDataQualityEnabled` treats that as absent the
   * same way `code_assembly.resolve_data_quality_enabled` does.
   *
   * The read path now carries this: `get_pipeline_details` projects both
   * `quality_config` and `approval_config` on **both** response arms
   * (`pipelines_crud.py:3296-3303` and `:3565-3568`), and `normalizePipeline`
   * passes both through onto `Pipeline` verbatim. What remains open is that no
   * route opens this wizard on an existing pipeline — `App.tsx` registers
   * `/pipelines/new` only — so this hydration has no production caller yet.
   */
  quality_config?: {
    data_quality_enabled?: unknown;
    dedup_enabled?: unknown;
    dedup_columns?: unknown;
  };
}

const VALID_SCHEMA_SOURCES: ReadonlyArray<WizardState['schemaSource']> = [
  'detected',
  'upload',
  'existing',
  'mirror_source',
];

function coerceSchemaFields(raw: unknown): SchemaField[] {
  // Accepts either the wizard-side `{ name, type, nullable, description }`
  // shape or the persisted `{ field_name, field_type, description }` shape
  // surfaced by `Schema.fields` (`types.ts`). Returns an empty array on any
  // other input.
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((f): f is Record<string, unknown> => typeof f === 'object' && f !== null)
    .map((f) => {
      const name =
        typeof f.name === 'string'
          ? f.name
          : typeof f.field_name === 'string'
            ? f.field_name
            : '';
      const type =
        typeof f.type === 'string'
          ? f.type
          : typeof f.field_type === 'string'
            ? f.field_type
            : 'string';
      const nullable = typeof f.nullable === 'boolean' ? f.nullable : true;
      const description =
        typeof f.description === 'string'
          ? f.description
          : typeof f.context === 'string'
            ? f.context
            : '';
      return { name, type, nullable, description };
    })
    .filter((f) => f.name.length > 0);
}

export function hydrateWizardStateFromPipeline(
  persisted: PersistedPipelineForEdit,
): WizardState {
  const { targetType, targetConfig } = migrateLegacyTargetConfig(
    persisted.target_config ?? {},
  );

  // Default missing `schema_source` to `'detected'` for backward
  // compatibility with pre-P19 records (Req 9.2). Reject any value not in
  // the documented allow-list — same fallback applies.
  const rawSchemaSource = persisted.schema_source;
  const schemaSource: WizardState['schemaSource'] =
    typeof rawSchemaSource === 'string' &&
    (VALID_SCHEMA_SOURCES as readonly string[]).includes(rawSchemaSource)
      ? (rawSchemaSource as WizardState['schemaSource'])
      : 'detected';

  // The persisted record carries the target schema (the schema the pipeline
  // was created with). Hydrate `detectedTargetSchema` from it — the legacy
  // `detectedSchema` field is intentionally NOT written (Decision 1).
  const persistedSchemaFields = coerceSchemaFields(persisted.schema_fields);
  const detectedTargetSchema: SchemaField[] | null =
    persistedSchemaFields.length > 0 ? persistedSchemaFields : null;

  const sourceType: WizardState['sourceType'] =
    (persisted.source_type ?? '').toString().toUpperCase() === 'JDBC'
      ? 'jdbc'
      : 's3_upload';

  const sourceConfigRaw = persisted.source_config ?? {};
  const sourceConfig: Record<string, string> = {};
  for (const [k, v] of Object.entries(sourceConfigRaw)) {
    if (v == null) continue;
    sourceConfig[k] = typeof v === 'string' ? v : String(v);
  }

  // The Approval_Config, hydrated through `hydrateApprovalConfig` — the ONE
  // record→state reader for this sub-object, and the same one
  // `PipelineFormModal`'s edit branch calls. The four reads used to be spelled
  // inline here; they were extracted alongside `ApprovalGatesFields` so the two
  // editors of one pipeline cannot read the same record two ways, which is how the
  // subset divergence started. Two behaviours changed with the move and both are
  // deliberate: the three booleans now fall back on `typeof === 'boolean'` rather
  // than `??`, so a non-boolean stored value fails closed to "approval required"
  // instead of being handed to a `Toggle` as `checked`; and the fallback for
  // `always_approve_schema_changes` is `true`, which it must stay.
  const approvalConfig: ApprovalConfig = hydrateApprovalConfig(persisted.approval_config);

  // p64 R7.4 — the Quality_Config, hydrated so an edit session does not silently
  // reset it. Three separate reads because the three keys have three different
  // absence semantics, and collapsing them would break the one that matters.
  // Delegated to `hydrateQualityConfig`, which is the ONE record→state reader for
  // this sub-object and is also what `PipelineFormModal`'s edit branch calls. The
  // three reads used to be spelled inline here; they were extracted with the
  // controls so the two editors of one pipeline cannot read the same record two
  // ways. The load-bearing part is unchanged and documented there: an absent
  // `data_quality_enabled` hydrates as `undefined`, never `false`.
  const qualityConfig: QualityConfig = hydrateQualityConfig(persisted.quality_config);

  return {
    ...INITIAL_WIZARD_STATE,
    pipelineName: persisted.pipeline_name ?? '',
    description: persisted.pipeline_description ?? persisted.description ?? '',
    sourceType,
    sourceConfig,
    schedule: persisted.schedule ?? null,
    targetType,
    targetConfig,
    // Source crawler does not run in an edit session — leave null until the
    // user clicks Detect Schema again.
    detectedSourceSchema: null,
    detectedTargetSchema,
    schemaSource,
    schemaFields: persistedSchemaFields,
    existingSchemaId: persisted.target_schema ?? null,
    // The user already picked when they originally created the pipeline; the
    // pre-selection effect should not override their choice on edit-load
    // (Req 6.8, Req 9.4).
    schemaSourceExplicitlySet: true,
    aiModel: persisted.ai_model ?? persisted.model_id ?? '',
    approvalConfig,
    qualityConfig,
  };
}

export default function CreatePipelinePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [activeStepIndex, setActiveStepIndex] = useState(0);
  const [state, setState] = useState<WizardState>({ ...INITIAL_WIZARD_STATE });
  const [submitting, setSubmitting] = useState(false);
  // P29 R5.1 — flip on after a Next attempt from Step 1 so the "required"
  // error surfaces inline on an empty name (before the user has typed).
  const [nameNextAttempted, setNameNextAttempted] = useState(false);

  // P29 R5.1 — existing pipeline names for case-insensitive duplicate
  // detection on Step 1. Reuses the same `usePipelines` list source the
  // dashboard already renders; a fetch failure or in-flight load simply
  // yields an empty list (no false "duplicate" while data is unavailable).
  const { data: pipelinesData } = usePipelines();
  const existingPipelineNames = useMemo(
    () => (pipelinesData?.pipelines ?? []).map((p) => p.pipeline_name).filter(Boolean),
    [pipelinesData],
  );

  // P29 R5.2/R5.3 — persist the in-progress wizard state to sessionStorage so
  // it survives a mid-wizard Cognito Hosted UI redirect (same tab). On mount,
  // if a draft exists, `draft.showRestorePrompt` drives the Restore/Discard
  // prompt below; the draft is cleared on successful submit and on cancel.
  const draft = useWizardDraft<WizardState>(state);

  const handleRestoreDraft = () => {
    const restored = draft.restore();
    if (restored) {
      setState(restored);
      // Re-arm the Step 1 "required" guard state only after an explicit Next;
      // a restored draft starts back at Step 1 with its data intact.
      setNameNextAttempted(false);
      setActiveStepIndex(0);
    }
  };

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      // 1. Resolve target_schema ID
      let targetSchemaId = state.existingSchemaId ?? '';

      let schemaLocation = '';

      if (!targetSchemaId && state.schemaFields.length > 0) {
        // Upload schema inline and get back an ID
        const schemaBlob = {
          properties: Object.fromEntries(
            state.schemaFields.map((f) => [f.name, { type: f.type, description: f.description }]),
          ),
          required: state.schemaFields.filter((f) => !f.nullable).map((f) => f.name),
        };
        // P29 R11.1 — compute the uploaded schema's object name EXACTLY ONCE
        // and reuse it for both the upload request and the fallback id below.
        // The pre-P29 code called `Date.now()` twice (once for the upload
        // `name`, once for the fallback id), so when the upload response
        // omitted a schema id the fallback resolved to a *different* name than
        // the object that was actually uploaded — a mismatch that stranded the
        // pipeline against a non-existent schema.
        const uploadedSchemaName = `uploaded_schema_${Date.now()}`;
        // p36 R2.2 / R2.3 / R2.4 — the optional Display_Label, forwarded only
        // when the user supplied one and sent **verbatim** when they did.
        //
        // The gate trims and the value does not, and those are two different
        // rules that both belong to the producer. `_submitted_display_label`
        // (`handlers/schemas.py:1974-1994`) treats absent, `null` and `""`
        // alike as "no label", and `upload_schema` stores a surviving label with
        // its padding intact and no uniqueness transformation (`:2098-2105`,
        // R4.7) — so a whitespace-only value must not be sent as a label, while
        // a padded non-empty one must arrive unchanged. `uploadSchema`, the
        // Schemas page's client, gates and forwards identically
        // (`use-schemas.ts:129-133`); this is the same rule, not a second one.
        //
        // `?? ''` is not defensive noise — it is a real `TypeError` guard.
        // `useWizardDraft.readDraft` returns `JSON.parse(raw) as T` with no
        // validation and no merge (`useWizardDraft.ts:66-77`) and
        // `handleRestoreDraft` does a bare `setState(restored)`, so a draft
        // saved before `schemaDisplayLabel` existed restores it as `undefined`
        // and `undefined.trim()` would throw here at submit. Merging on restore
        // (`setState({ ...INITIAL_WIZARD_STATE, ...restored })`) is the better
        // fix and is deliberately out of scope — it changes restore semantics
        // for every wizard field and belongs to a spec that owns the draft.
        const schemaDisplayLabel = state.schemaDisplayLabel ?? '';
        // Route through the typed apiClient — it unwraps the Lambda proxy
        // response ({ statusCode, body }) centrally, so no manual .json() /
        // JSON.parse(body) handling here. Matches the useUploadSchema hook
        // payload shape ({ name, content }).
        const uploadBody = await apiClient.post<{
          schemaId?: string;
          schema_id?: string;
          schema_name?: string;
          location?: string;
          /** Arrives on a **200**, not a failure — see R2.7 below (`types.ts:310`). */
          displayLabelError?: string;
        }>('/schemas/upload', {
          name: uploadedSchemaName,
          content: JSON.stringify(schemaBlob),
          // R2.8 — the request key is imported, not transcribed. Two literals
          // that happen to match satisfy "the same key" only today, and these
          // two upload paths are different HTTP clients.
          ...(schemaDisplayLabel.trim() ? { [DISPLAY_LABEL_REQUEST_KEY]: schemaDisplayLabel } : {}),
        });
        // The upload response may carry the schema id under several keys; fall
        // back to the SAME name we uploaded under (never a fresh Date.now()).
        targetSchemaId = uploadBody?.schemaId ?? uploadBody?.schema_id ?? uploadBody?.schema_name ?? uploadedSchemaName;
        schemaLocation = uploadBody?.location ?? '';
        // p36 R2.7 — a success that is also a complaint. `upload_schema` returns
        // **200** with `displayLabelError` when the body was written and only
        // the Context_Sidecar write failed (`handlers/schemas.py:2106-2116`),
        // because R4.2 makes a schema with no Display_Label fully usable and a
        // 500 would make the caller retry an upload that already succeeded and
        // produce a second object.
        //
        // So this warns and **continues**. Throwing here would convert an
        // unsaved cosmetic label into a lost pipeline — the schema is stored,
        // addressable, and its label is recoverable through Rename on the
        // Schemas page, which writes the same key. Same shape as
        // `reportUploadOutcome` (`SchemaManagementPage.tsx:314-320`).
        if (uploadBody?.displayLabelError) {
          showToast(
            `Schema uploaded, but its display label was not saved (${uploadBody.displayLabelError}). ` +
              'Use Rename on the Schemas page to set it.',
            'warning',
          );
        }
      }

      // 2. Build target_config in the format the backend expects
      const targetConfig: Record<string, string> = {};

      if (state.targetType === 's3_tables') {
        if (state.targetConfig.table_bucket_arn) {
          // Existing S3 Table Bucket
          targetConfig.type = 'EXISTING_S3_TABLE_BUCKET';
          targetConfig.bucket_name = state.targetConfig.bucket_name ?? '';
          targetConfig.table_name = state.targetConfig.table_name ?? '';
          targetConfig.namespace = state.targetConfig.namespace ?? 'default';
          targetConfig.table_bucket_arn = state.targetConfig.table_bucket_arn ?? '';
        } else {
          // New S3 Table Bucket
          targetConfig.type = 'NEW_S3_TABLE_BUCKET';
          targetConfig.bucket_name = state.targetConfig.bucket_name ?? '';
          targetConfig.namespace = state.targetConfig.namespace ?? '';
          targetConfig.table_name = state.targetConfig.table_name ?? '';
        }
      } else if (state.targetType === 'glue_catalog') {
        targetConfig.type = 'GLUE_CATALOG';
        targetConfig.database = state.targetConfig.database_name ?? '';
        targetConfig.table = state.targetConfig.table_name ?? '';
      } else if (state.targetType === 'jdbc') {
        // P17 Task 20.4 — Validates: Requirements 14.4, 14.6.
        // Send the unified key shape: jdbc_url / jdbc_driver / jdbc_table plus
        // optional secret_arn and glue_connection_name. The legacy
        // connection_url / username / password keys (P14 / pre-P17) are NO
        // LONGER emitted — the backend's create_pipeline validator no longer
        // accepts them.
        targetConfig.type = 'JDBC';
        targetConfig.jdbc_url = state.targetConfig.jdbc_url ?? '';
        targetConfig.jdbc_driver = state.targetConfig.jdbc_driver ?? '';
        targetConfig.jdbc_table =
          state.targetConfig.jdbc_table ??
          (state.targetConfig.database_name && state.targetConfig.table_name
            ? `${state.targetConfig.database_name}.${state.targetConfig.table_name}`
            : '');
        // Optional fields — only include when the user provided them so the
        // request body stays minimal.
        if (state.targetConfig.secret_arn) {
          targetConfig.secret_arn = state.targetConfig.secret_arn;
        }
        if (state.targetConfig.glue_connection_name) {
          targetConfig.glue_connection_name = state.targetConfig.glue_connection_name;
        }
        // The connection mode, and the networking `JdbcConnectionSelector` sets
        // in select_rds and manual mode. The backend reads all of them:
        // `pipelines_jdbc._validate_jdbc_target_config` dispatches its checks
        // on `_connection_mode` (absent means select_rds),
        // `_augment_glue_connection_config` refuses a Glue connection with no
        // SECRET_ID only when the mode is glue_connection, and
        // `_build_jdbc_target_physical_requirements` builds the new
        // connection's VPC attachment from the three networking keys. This
        // block used to drop them, so every JDBC target arrived as select_rds
        // with no networking: a Glue connection holding inline credentials was
        // accepted, then refused at Create Glue Pipeline on the first run, and a
        // select_rds or manual target's connection was created with no VPC
        // attachment. The source side never had this gap, because
        // `buildCreateSourceConfig` passes its config through whole.
        // Mode switches clear the networking keys (`handleModeChange`), so a
        // glue_connection target carries none of them.
        for (const key of ['_connection_mode', 'vpc_id', 'subnet_id', 'security_group_ids', 'availability_zone']) {
          const value = state.targetConfig[key];
          if (value) targetConfig[key] = value;
        }
        // Pass through engine + database/table identifiers used by both the
        // discovery flow and downstream display.
        if (state.targetConfig.engine) {
          targetConfig.engine = state.targetConfig.engine;
        }
        if (state.targetConfig.database_name) {
          targetConfig.database_name = state.targetConfig.database_name;
        }
        if (state.targetConfig.table_name) {
          targetConfig.table_name = state.targetConfig.table_name;
        }
      } else if (state.targetType === 's3_parquet') {
        targetConfig.type = 'S3_PARQUET';
        targetConfig.s3_path = state.targetConfig.s3_path ?? '';
      }

      // Pass through write_mode + merge_key for every target type that has
      // them populated — the wizard's Write Mode select lives outside the
      // per-target subforms.
      if (state.targetConfig.write_mode) {
        targetConfig.write_mode = state.targetConfig.write_mode;
      }
      if (state.targetConfig.merge_key) {
        targetConfig.merge_key = state.targetConfig.merge_key;
      }

      // 3. Create pipeline — P17 includes source_type, source_config, and schedule.
      // P19 Task 11.1 — Validates: Requirements 8.3, 8.4.
      // Pass `schema_source` through to the backend verbatim (no client-side
      // transformation or re-defaulting). The handler enforces the allow-list
      // and persists the value diagnostically; absent values continue to
      // default server-side to `'detected'` for backward compatibility with
      // pre-P19 records (Req 9.2).
      await apiClient.post('/pipelines/provision', {
        pipeline_name: state.pipelineName,
        description: state.description,
        target_schema: targetSchemaId,
        schema_location: schemaLocation || undefined,
        ai_model: state.aiModel,
        target_config: targetConfig,
        schema_source: state.schemaSource,
        // p64 (authorised addition to 2a) — serialised, not passed through.
        // `state.approvalConfig` is camelCase and every reader reads snake_case,
        // so posting it verbatim persisted four keys nothing reads and left the
        // step-5 toggles inert. `buildApprovalConfigRequest` is the one
        // normalisation authority (p63 R1.2 permits exactly one); it also
        // converts the slider's percent to the fraction the attribute is stored
        // in. Do NOT revert this to `state.approvalConfig`.
        approval_config: buildApprovalConfigRequest(state.approvalConfig),
        // p64 R7.3 / R6.6 — a PEER of `approval_config`, deliberately not routed
        // through `target_config`. That route would have been free (`code_assembly`
        // splats the persisted source and target maps into the render context) and
        // design C7 rejects it: a data-quality setting is not target
        // configuration, and `target_config` is a free-form
        // `Record<string, string>` with no validation, so it is a place to hide an
        // unvalidated boolean.
        //
        // Serialised, not passed through. `state.qualityConfig` is camelCase and
        // every reader reads snake_case; `buildQualityConfigRequest` is the one
        // normalisation authority and is also what decides whether
        // `data_quality_enabled` is sent at all. Do NOT replace this with
        // `state.qualityConfig`.
        [QUALITY_CONFIG_REQUEST_KEY]: buildQualityConfigRequest(state.qualityConfig),
        source_type: state.sourceType === 'jdbc' ? 'JDBC' : 'S3_UPLOAD',
        source_config: buildCreateSourceConfig(state.sourceType, state.sourceConfig),
        schedule: state.schedule ?? undefined,
        // p55 R8.4 — the Selected_Worksheet, sent ONLY when the user actually
        // chose one, and sent **verbatim** when they did. The gate trims and the
        // value does not: a worksheet name may legitimately carry padding, but a
        // whitespace-only input is a user who typed nothing.
        //
        // Omitted rather than sent as `''`, and that is the load-bearing half.
        // `schema_extract._configured_worksheet` reads the attribute with a
        // MEMBERSHIP test, so a recorded `''` is a *selection naming no sheet*
        // and refuses the run under R8.5, while an absent key means "no human has
        // chosen" and lets a single-sheet workbook proceed under R8.1. Sending
        // `''` for "no selection" would therefore break every single-sheet Excel
        // pipeline created here. `LGR-44d601e-1` settled the same question for the
        // Display_Label in the same direction — remove the key, never store `''`,
        // because a stored `''` "would have to be distinguished from absence at
        // every reader".
        //
        // The key is imported, not transcribed (the `DISPLAY_LABEL_REQUEST_KEY`
        // rule from p36 R2.8): two literals that happen to match satisfy "the
        // same key" only today, and this one has to match a Python constant.
        ...(worksheetSelectionIsSet(state.selectedWorksheet)
          ? { [SELECTED_WORKSHEET_REQUEST_KEY]: state.selectedWorksheet }
          : {}),
      });

      showToast('Pipeline created successfully', 'success');
      // P29 R5.2 — the wizard succeeded; drop the persisted draft so it does
      // not survive a completed run and offer a stale restore next time.
      draft.clear();
      queryClient.invalidateQueries({ queryKey: ['pipelines'] });
      navigate('/');
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to create pipeline', 'error');
    } finally {
      setSubmitting(false);
    }
  };

  // P29 R5.1 — Step 1 (Name) navigation guard. The wizard's Next must be
  // gated by per-step validation so an empty or duplicate pipeline name is
  // caught inline at Step 1 — not deferred to a submit-time toast on the
  // review step (the pre-P29 behavior, where the empty/duplicate name only
  // failed server-side at /pipelines/provision). `validatePipelineName` is
  // the shared validator used by WizardStepName's inline FormField error.
  // Step indices: 0=Name, 1=Source, 2=Target, 3=Schema, 4=Model, 5=Review.
  const NAME_STEP_INDEX = 0;
  const nameStepError = validatePipelineName(state.pipelineName, existingPipelineNames);
  const isNameStepNextDisabled =
    activeStepIndex === NAME_STEP_INDEX && nameStepError !== undefined;

  // P19 Task 10.6 — Step 3 (Target Schema) navigation guard. Validates Req 5.3.
  // When the user is in mirror_source mode but either the source is not JDBC
  // or the source schema has not been detected, forward navigation from Step 3
  // is blocked. Cloudscape's `<Wizard>` exposes only an `onNavigate` callback
  // (no per-step `nextStepDisabled` prop), so the guard is enforced by
  // refusing to advance `activeStepIndex` when `detail.reason === 'next'` and
  // the predicate is true. The navigation pane and Previous button still work.
  // Step indices: 0=Name, 1=Source, 2=Target, 3=Schema, 4=Model, 5=Review.
  const TARGET_STEP_INDEX = 2;
  const SCHEMA_STEP_INDEX = 3;
  const isMirrorSourceBlocked =
    state.schemaSource === 'mirror_source' &&
    (state.sourceType !== 'jdbc' || !state.detectedSourceSchema?.length);
  // Upload is the only schema source whose commit depended on a button click
  // inside the step, so it is the only one that can reach Review with an empty
  // `schemaFields` while `schemaSource` still reads 'upload'. The others write
  // the fields elsewhere: `detected` from the Target step's Detect Schema
  // handlers (WizardStepTarget.tsx:73, JdbcTargetDetectPanel.tsx:65),
  // `existing` from the schema select (WizardStepSchema.tsx:81), and
  // `mirror_source` from the effect covered by the predicate above. Without
  // this guard the
  // review step displayed the contradiction "Schema Source: Uploaded /
  // Fields: 0 fields" and submit sent `target_schema: ''`, which the backend
  // rejects with 400 "target_schema is required" — a failure the user could
  // only see after clicking Create.
  const isUploadSchemaMissing =
    state.schemaSource === 'upload' && state.schemaFields.length === 0;
  // p35 R5.2 — the `existing` path was the third case this guard did not cover.
  // Selecting a schema that declares zero fields must not advance: `handleSubmit`
  // sends `state.existingSchemaId` as `target_schema` (:283), so the advance is
  // where R5.1's "must not be committed as a pipeline's target schema" becomes
  // observable, and a pipeline created this way reaches p33's `no_target_schema`
  // non-verification.
  //
  // Keyed on the picker's verdict, never on `state.schemaFields.length`. After
  // DD4 the listing reports `fieldCount: null` for every uploaded and every S3
  // Tables schema and the field list arrives asynchronously, so a length test
  // would block the step for every schema a user uploads — and would block it
  // while the per-id fetch was still in flight. The picker sets the flag from
  // `fieldCount === 0` or from a resolved-and-empty field list; `undefined`
  // (nothing selected, or not yet resolved) allows the advance.
  const isExistingSchemaEmpty =
    state.schemaSource === 'existing' && state.existingSchemaDeclaresNoFields === true;
  const isSchemaStepNextDisabled =
    activeStepIndex === SCHEMA_STEP_INDEX &&
    (isMirrorSourceBlocked || isUploadSchemaMissing || isExistingSchemaEmpty);

  // P19 Task 8.4 — Step 2 (Target Destination) navigation guard.
  // Validates: Requirements 6.5, 6.8.
  // When the user picks a JDBC target, the JdbcTargetForm's `<Tiles>`
  // selector is the source of truth for `write_mode` (the parent's legacy
  // `<Select>` is suppressed by Task 7.4). Forward navigation from Step 2 is
  // blocked when `targetType === 'jdbc'` and the persisted `write_mode` is
  // not one of the two user-selectable values, so `null`, `undefined`,
  // legacy `'merge'`, and any arbitrary other value all keep Next disabled
  // until the user explicitly picks a tile.
  const isJdbcWriteModeBlocked =
    state.targetType === 'jdbc' &&
    !['overwrite', 'append'].includes(state.targetConfig.write_mode ?? '');
  // A JDBC target must have a resolvable write table — either an explicit
  // `jdbc_table` (schema-qualified) or both `database_name` and `table_name`
  // (from which the backend derives `database.table`). Without it,
  // CreateGluePipeline fails at build_jdbc_arguments with "target table is
  // required", so block Next here rather than letting the run fail mid-pipeline.
  const isJdbcTargetTableMissing =
    state.targetType === 'jdbc' &&
    !state.targetConfig.jdbc_table &&
    !(state.targetConfig.database_name && state.targetConfig.table_name);
  // A JDBC target in a mode where AgenticDataIngestion CREATES the Glue
  // Connection needs a credential reference. `create_glue_jdbc_connection`
  // sets `SECRET_ID` only when `secret_arn` is present; without it the
  // connection carries `JDBC_CONNECTION_URL` alone and Glue rejects it with
  // "Validation for connection properties failed". `create_pipeline` already
  // 400s on this, so block Next to put the requirement on the step that owns
  // the field instead of surfacing it as a failed Create four steps later —
  // the same reasoning as the write-table guard above.
  //
  // Exempt when `glue_connection_name` is set, keyed on that field rather than
  // the mode string because `_provision_jdbc_target_connection` early-returns
  // on it in ANY mode: no connection is created, and the user's existing one
  // supplies the credentials. This mirrors the backend check exactly.
  //
  // Both non-exempt modes render a secret control — `select_rds` in
  // JdbcConnectionSelector, `manual` in ManualConnectionForm — so this cannot
  // strand a restored draft on a step with no visible field to correct, the
  // dead end the Source step's worksheet guard documents.
  const isJdbcTargetSecretMissing =
    state.targetType === 'jdbc' &&
    (state.targetConfig._connection_mode ?? 'select_rds') !== 'glue_connection' &&
    !state.targetConfig.glue_connection_name &&
    !state.targetConfig.secret_arn;
  const isTargetStepNextDisabled =
    activeStepIndex === TARGET_STEP_INDEX &&
    (isJdbcWriteModeBlocked || isJdbcTargetTableMissing || isJdbcTargetSecretMissing);

  // p55 R8.4 — Source step guard on the Selected_Worksheet.
  //
  // Blocks ONLY on a name Excel could never accept (too long, or containing a
  // forbidden character), never on emptiness: no selection is valid and is what
  // R8.1 requires for a single-sheet workbook. Whether the named sheet EXISTS is
  // not checkable here — no workbook exists at wizard time — and is R8.5's job at
  // run time, where it refuses and reports both the configured name and the names
  // present.
  //
  // The guard exists so an impossible name is never persisted: a recorded
  // selection no workbook can match would refuse every run of the pipeline
  // forever, which is a key nothing honours (`LGR-eee8ac8-6`). This is a wizard
  // guard and NOT a new 400 in `create_pipeline` — that handler's permissive
  // posture is governed by `LGR-cad8032-2`, whose blast radius is nine test
  // modules asserting 201.
  //
  // Gated on `sourceType === 's3_upload'` because that is the only branch that
  // renders the control. A restored draft could otherwise carry a bad worksheet
  // name alongside a JDBC source and block the step with **no visible field to
  // correct** — a dead end, and unreachable for a keyboard or screen-reader user
  // in particular. The value is also cleared on a source-type switch, so this
  // only guards the draft-restore path.
  const SOURCE_STEP_INDEX = 1;
  const worksheetStepError =
    state.sourceType === 's3_upload'
      ? worksheetSelectionError(state.selectedWorksheet)
      : undefined;
  const isSourceStepNextDisabled =
    activeStepIndex === SOURCE_STEP_INDEX && worksheetStepError !== undefined;

  // p64 R7.2 — Model step guard on the dedup column names.
  //
  // Blocks only on a name the Identifier_Allowlist refuses, never on emptiness: an
  // empty list means every mapped target column and is the default (R7.2). The
  // guard is not what enforces the allowlist — `_validate_quality_config` 400s at
  // the boundary and `code_assembly.resolve_quality_config` checks again at render
  // — it is what puts the refusal on the step that owns the field, instead of
  // surfacing it as a failed Create with the offending name three layers away.
  //
  // Gated on `dedupEnabled` because that is the only branch that renders the
  // control. A restored draft could otherwise carry bad columns with dedup off and
  // block the step with no visible field to correct — the dead end the Source
  // step's worksheet guard documents, and unreachable for a keyboard or
  // screen-reader user in particular.
  const MODEL_STEP_INDEX = 4;
  const dedupColumnsStepError = (state.qualityConfig ?? INITIAL_WIZARD_STATE.qualityConfig)
    .dedupEnabled
    ? dedupColumnsError(state.qualityConfig?.dedupColumns)
    : undefined;
  const isModelStepNextDisabled =
    activeStepIndex === MODEL_STEP_INDEX && dedupColumnsStepError !== undefined;

  return (
    <SpaceBetween size="m">
      {/* P29 R5.2 — a draft from a prior (redirect-interrupted) session was
          found; let the user restore their in-progress work or discard it.
          Persistence stays suspended until they choose, so the fresh wizard
          state below does not overwrite the saved draft. */}
      {draft.showRestorePrompt && (
        <Alert
          type="info"
          header="Restore your in-progress pipeline?"
          data-testid="wizard-draft-restore"
          action={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={handleRestoreDraft} data-testid="wizard-draft-restore-button">
                Restore
              </Button>
              <Button onClick={() => draft.discard()} data-testid="wizard-draft-discard-button">
                Discard
              </Button>
            </SpaceBetween>
          }
        >
          We found an unsaved Create Pipeline draft from a previous session.
        </Alert>
      )}
      <Wizard
      i18nStrings={{
        stepNumberLabel: (n) => `Step ${n}`,
        collapsedStepsLabel: (step, total) => `Step ${step} of ${total}`,
        submitButton: 'Create Pipeline',
        cancelButton: 'Cancel',
        previousButton: 'Previous',
        nextButton: 'Next',
        optional: 'optional',
      }}
      activeStepIndex={activeStepIndex}
      onNavigate={({ detail }) => {
        // Block forward navigation from Step 1 when the pipeline name is empty
        // or a duplicate (P29 R5.1). Flip `nameNextAttempted` so an empty
        // field's "required" error surfaces inline via WizardStepName's
        // FormField. Backwards / step-link navigation is unaffected.
        if (detail.reason === 'next' && isNameStepNextDisabled) {
          setNameNextAttempted(true);
          return;
        }
        // Block forward navigation from Step 3 when mirror_source preconditions
        // are not met (P19 Task 10.6, Req 5.3). Backwards / step-link / skip
        // navigation is still allowed so the user can return to Step 2 to
        // detect the source schema or pick a different source type.
        if (detail.reason === 'next' && isSchemaStepNextDisabled) return;
        // Block forward navigation from Step 2 when the JDBC target's
        // write_mode has not been resolved to one of the two user-selectable
        // values (P19 Task 8.4, Req 6.5, 6.8). Backwards / step-link / skip
        // navigation is still allowed so the user can correct earlier steps
        // or return after picking a tile.
        if (detail.reason === 'next' && isTargetStepNextDisabled) return;
        // p55 R8.4 — block forward navigation from the Source step when the
        // worksheet name could not name a sheet in any workbook. The inline
        // `FormField` errorText on the control already says why. Backwards /
        // step-link navigation is unaffected, as with every guard above.
        if (detail.reason === 'next' && isSourceStepNextDisabled) return;
        // p64 R7.2 — block forward navigation from the Model step when a dedup
        // column name could not be a column name. The inline `FormField`
        // errorText on the control already says which one and why. Backwards /
        // step-link navigation is unaffected, as with every guard above.
        if (detail.reason === 'next' && isModelStepNextDisabled) return;
        setActiveStepIndex(detail.requestedStepIndex);
      }}
      onSubmit={handleSubmit}
      onCancel={() => {
        // P29 R5.2 — explicit cancel discards the in-progress work; clear the
        // persisted draft so it is not offered for restore on the next visit.
        draft.clear();
        navigate('/');
      }}
      isLoadingNextStep={submitting}
      steps={[
        {
          title: 'Name & Description',
          content: (
            <WizardStepName
              state={state}
              onChange={setState}
              existingNames={existingPipelineNames}
              showRequiredError={nameNextAttempted}
            />
          ),
        },
        {
          // P17 Task 19.7 — new Source step inserted between Name and Target,
          // making this a 6-step wizard: Name → Source → Target → Schema → Model → Review.
          // Validates: P17 Requirement 1.1.
          title: 'Source',
          content: <WizardStepSource state={state} onChange={setState} />,
        },
        {
          title: 'Target Destination',
          // Surface WHY Next is blocked instead of silently refusing to
          // advance. Cloudscape renders step errorText as an alert below the
          // step content; we only set it once the user has actually tried to
          // proceed (the guard fires on the 'next' reason) — but since we
          // can't observe that here, we show it whenever the target step is
          // active and incomplete, which is the same condition that blocks Next.
          errorText:
            activeStepIndex === TARGET_STEP_INDEX && isTargetStepNextDisabled
              ? isJdbcTargetTableMissing
                ? 'Enter the target Database Name and Table Name before continuing.'
                : 'Select a Write Mode before continuing.'
              : undefined,
          content: <WizardStepTarget state={state} onChange={setState} />,
        },
        {
          title: 'Target Schema',
          // Surface WHY Next is blocked, in the same way the Target
          // Destination step above does. Only the upload-mode case needs a
          // step-level message: `mirror_source`'s two blocked states already
          // render their own inline `<Alert type="warning">` inside
          // WizardStepSchema (non-JDBC source / source schema not detected),
          // so adding an errorText there would duplicate the explanation.
          errorText:
            isSchemaStepNextDisabled && isUploadSchemaMissing
              ? 'Upload a schema file with at least one field before continuing.'
              : undefined,
          content: <WizardStepSchema state={state} onChange={setState} />,
        },
        {
          title: 'AI Model & Options',
          // p64 R7.2 — surface WHY Next is blocked, the same way the Target
          // Destination and Target Schema steps above do. The message is the
          // control's own errorText, so the step alert and the field cannot
          // disagree about which column name is the problem.
          errorText: isModelStepNextDisabled ? dedupColumnsStepError : undefined,
          content: <WizardStepModel state={state} onChange={setState} />,
        },
        {
          title: 'Review & Create',
          content: <WizardStepReview state={state} />,
        },
      ]}
      />
    </SpaceBetween>
  );
}
