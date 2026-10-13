// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Canonical step-name normalization (P28 R8.2, task 14.2).
 *
 * TypeScript mirror of `shared/step_names.py` `from_any`: the backend now
 * writes state rows and `steps_status` keys with canonical snake_case step
 * names ("create_code", "run_pipeline", …), while runs from before the
 * rename persist the legacy PascalCase names ("CodeGeneration",
 * "RunPipeline", …) and agent traces carry a third variant
 * ("code_generation", "code_testing"). Matching is case- and `_`/`-`
 * insensitive with an explicit alias table for names that don't reduce to
 * the same normal form.
 *
 * Display labels are NOT changed here — `formatStepName` in formatting.ts
 * keeps owning the human-readable labels.
 */

/** Canonical pipeline step names (snake_case) — mirror of shared/step_names.py StepName. */
export const CANONICAL_STEP_ORDER = [
  'file_validation',
  'schema_extract',
  'schema_compare',
  'check_schema_action',
  'schema_mapping',
  'check_schema_mapping_approval',
  'create_code',
  'test_code',
  'check_code_approval',
  'create_infrastructure',
  'create_glue_pipeline',
  'create_quality_checks',
  'run_pipeline',
] as const;

export type CanonicalStepName = (typeof CANONICAL_STEP_ORDER)[number];

/**
 * The ten state-row-writing task steps in execution order (mirror of
 * shared/step_names.py TASK_STEPS — the check_* Choice gates never write
 * state rows or steps_status keys).
 */
export const CANONICAL_TASK_STEPS: readonly CanonicalStepName[] = CANONICAL_STEP_ORDER.filter(
  (s) => !s.startsWith('check_'),
);

/** Casing/word-separator-insensitive normal form for name lookup. */
function normalKey(value: string): string {
  return value.replace(/[_-]/g, '').toLowerCase();
}

/**
 * Legacy variants that do NOT reduce to a canonical name's normal form
 * (mirror of shared/step_names.py _LEGACY_ALIASES). PascalCase SFN state
 * ids ("CreateCode", "RunPipeline", …) need no entry — they normalize to
 * the same key as their canonical snake_case name.
 */
const LEGACY_ALIASES: Record<string, CanonicalStepName> = {
  codegeneration: 'create_code',
  codetesting: 'test_code',
  schemamappingapproval: 'check_schema_mapping_approval',
  codeapproval: 'check_code_approval',
  infrastructure: 'create_infrastructure',
  firstrun: 'create_quality_checks',
};

const LOOKUP: Record<string, CanonicalStepName> = (() => {
  const table: Record<string, CanonicalStepName> = {};
  for (const step of CANONICAL_STEP_ORDER) table[normalKey(step)] = step;
  for (const [alias, step] of Object.entries(LEGACY_ALIASES)) table[alias] = step;
  return table;
})();

/**
 * Normalize any known step-name variant to its canonical snake_case name.
 * Unknown names (agent records like "self_healing", sentinels like
 * "pipeline_failure") pass through unchanged — tolerant by design, the UI
 * must never throw on a name it merely displays.
 */
export function normalizeStepName(value: string): string {
  if (!value) return value;
  return LOOKUP[normalKey(value)] ?? value;
}

/** True when two step names refer to the same pipeline step (any variants). */
export function sameStep(a: string, b: string): boolean {
  return normalizeStepName(a) === normalizeStepName(b);
}

/**
 * Order a run's step keys (e.g. `Object.keys(steps_status)`) into canonical
 * execution order, deduplicating naming variants of the same step (the
 * canonical spelling wins when a run somehow carries both generations).
 * Keys that aren't pipeline steps are appended in their original order.
 * Returned keys are the RAW input keys, so callers can index back into the
 * source record.
 */
export function orderStepKeys(keys: readonly string[]): string[] {
  const byCanonical = new Map<string, string>();
  const unknown: string[] = [];
  for (const key of keys) {
    const canonical = normalizeStepName(key);
    if ((CANONICAL_TASK_STEPS as readonly string[]).includes(canonical)) {
      const existing = byCanonical.get(canonical);
      // Prefer the canonically-spelled key if both generations are present.
      if (existing === undefined || key === canonical) byCanonical.set(canonical, key);
    } else {
      unknown.push(key);
    }
  }
  const known = CANONICAL_TASK_STEPS.filter((c) => byCanonical.has(c)).map((c) => byCanonical.get(c)!);
  return [...known, ...unknown];
}
