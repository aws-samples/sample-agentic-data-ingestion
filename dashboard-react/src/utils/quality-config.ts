// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p64 R7 / R6.6 (design C7) — the Quality_Config's wire keys, its dedup-column
 * rule, and the three-state reader that keeps "absent" a fact.
 *
 * ## Why this is a module and not expressions in the wizard
 *
 * The same argument `utils/worksheet-selection.ts` makes, and behind it
 * `LGR-4854b3d-1` (**template / ratified**): the value has a producer (the Model
 * step's controls), a gate (the Model step's navigation guard), a display (the
 * Review step), a wire shape (`buildQualityConfigRequest`) and a hydrate reader
 * (`hydrateWizardStateFromPipeline`). Every one of those written by hand is
 * another chance to spell the rule differently — and one of them, the three-state
 * read below, is wrong in a way that silently reclassifies a governance verdict if
 * it is spelled as `?? false`.
 *
 * ## The keys are imported, never transcribed
 *
 * `LGR-eee8ac8-5`: writer and readers live in different Lambdas and are only ever
 * compared through these strings, so a second spelling would not fail — it would
 * make every opt-in silently never match, and an operator's dedup toggle would
 * appear saved and do nothing. `shared/quality_verdict.py` is the one definition
 * on the Python side (`QUALITY_CONFIG_KEY`, `DATA_QUALITY_ENABLED_KEY`,
 * `DEDUP_ENABLED_KEY`, `DEDUP_COLUMNS_KEY`); this module is the TypeScript end of
 * that one hop across languages, exactly as `SELECTED_WORKSHEET_REQUEST_KEY` and
 * `DISPLAY_LABEL_REQUEST_KEY` already are.
 *
 * ## snake_case on the wire, camelCase in wizard state
 *
 * p63 R1.2 requires exactly **one** normalisation authority and p64 2a.1b placed
 * it at this client's request boundary for the Approval_Config. The Quality_Config
 * follows it: `buildQualityConfigRequest` emits these snake_case keys and
 * `pipelines_crud._submitted_quality_config` persists them verbatim. There is no
 * server-side camelCase normaliser and **one must not be added** — it would make a
 * second spelling legal on the wire permanently with nothing to arbitrate between
 * them.
 */

/** The JSON body key `POST /pipelines/provision` carries the sub-object under. */
export const QUALITY_CONFIG_REQUEST_KEY = 'quality_config';

/**
 * R6.6's data-quality opt-in.
 *
 * **Three-state, and the third state is load-bearing.** Absent means "use the
 * template's own default" — on for `glue_job_s3_tables.py.j2`, off for the other
 * four (`shared/quality_verdict.py`'s `DQ_DEFAULT_ON_TEMPLATES`, read by
 * `code_assembly.resolve_data_quality_enabled`). That is why
 * `pipelines_crud._default_quality_config` deliberately does not seed this key,
 * and why nothing on this side may collapse absence into `false`.
 */
export const DATA_QUALITY_ENABLED_REQUEST_KEY = 'data_quality_enabled';

/** R7.1's dedup opt-in. Off everywhere today, so its default IS unconditional. */
export const DEDUP_ENABLED_REQUEST_KEY = 'dedup_enabled';

/**
 * R7.2's duplicate-identifying columns.
 *
 * `[]` and an omitted key mean the same thing — every mapped target column, which
 * is what `dropDuplicates()` with no argument already does — so absence and
 * emptiness agree here and neither needs a sentinel. That is the opposite of the
 * Selected_Worksheet, where `''` is a *recorded* selection naming no sheet
 * (`LGR-44d601e-1`); do not carry that intuition across.
 */
export const DEDUP_COLUMNS_REQUEST_KEY = 'dedup_columns';

/**
 * The Identifier_Allowlist, as `shared/identifier_utils.py` defines it.
 *
 * A dedup column is interpolated into `dropDuplicates([...])` in the generated
 * Glue job, so it is a generated-code sink and the allowlist applies. Checked here
 * only so the operator sees the problem on the step that owns the field; the
 * refusal that matters is `pipelines_crud._validate_quality_config`'s 400, and
 * `code_assembly.resolve_quality_config` checks it a third time at render. This
 * copy weakens nothing and replaces nothing.
 */
export const DEDUP_COLUMN_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** `MAX_IDENTIFIER_LENGTH` in `shared/identifier_utils.py`. */
export const MAX_DEDUP_COLUMN_LENGTH = 255;

/**
 * Split the Model step's free-text field into column names.
 *
 * This parses an **input string** into entries; it does not filter a list of
 * columns the operator named. The distinction matters: `_validate_quality_config`
 * refuses an offending entry rather than dropping it, for `LGR-4ea43f3-4`'s reason
 * one value class over — "a dedup key list quietly missing a column deduplicates
 * on the wrong identity" — so nothing here may discard a non-empty token. Only
 * separators and the whitespace around them are removed.
 *
 * Commas and whitespace both separate, because a user pasting a column list gets
 * either. Entries are **not** de-duplicated and **not** case-folded: both would be
 * silent alterations of a value bound for generated code.
 */
export function parseDedupColumns(raw: string | undefined | null): string[] {
  return (raw ?? '')
    .split(/[,\s]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

/** Render a stored list back into the field's text form. */
export function formatDedupColumns(columns: readonly string[] | undefined | null): string {
  return (columns ?? []).join(', ');
}

/**
 * The reason a dedup column list cannot be used, or `undefined` when it can.
 *
 * Reads defensively (R7.5): a draft saved before `qualityConfig` existed restores
 * the whole sub-object as `undefined` despite the type, because
 * `useWizardDraft.readDraft` is a bare `JSON.parse(raw) as T` and
 * `handleRestoreDraft` replaces state rather than merging into it. An empty or
 * absent list is valid — it means every mapped target column — so emptiness is
 * never an error here.
 */
export function dedupColumnsError(
  columns: readonly string[] | undefined | null,
): string | undefined {
  for (const column of columns ?? []) {
    if (typeof column !== 'string' || column.length === 0) {
      return 'A dedup column name cannot be empty.';
    }
    if (column.length > MAX_DEDUP_COLUMN_LENGTH) {
      return `A dedup column name cannot be longer than ${MAX_DEDUP_COLUMN_LENGTH} characters; ${column.slice(0, 20)}… is ${column.length}.`;
    }
    if (!DEDUP_COLUMN_PATTERN.test(column)) {
      return `"${column}" is not a usable column name. Use a letter or underscore followed by letters, digits or underscores.`;
    }
  }
  return undefined;
}

/**
 * Read a persisted `data_quality_enabled` **without inventing a value for
 * absence**.
 *
 * This function is the whole reason the module exists. An existing S3 Tables
 * pipeline has data quality on by template default and stores **no**
 * `data_quality_enabled` key, so reading absence as `false` and re-submitting
 * would write an explicit opt-OUT: the row-level split and the quarantine write
 * that target type has had since P28 would disappear, and
 * `shared/quality_verdict.py` would move the pipeline's verdict from `enforced` to
 * `advisory`. That is R6.6's "no existing pipeline's behaviour changes" broken in
 * the direction nobody looks, and it is silently reclassifying a governance
 * verdict on someone's first edit.
 *
 * Anything that is not a JSON boolean also reads as absent, matching
 * `code_assembly.resolve_data_quality_enabled`'s `isinstance(explicit, bool)`.
 * `_validate_quality_config` 400s a non-boolean at the boundary, so a record
 * carrying one predates that validator; falling back to the template default is
 * what it would have got at the time.
 */
export function readDataQualityEnabled(raw: unknown): boolean | undefined {
  return typeof raw === 'boolean' ? raw : undefined;
}
