// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45 R2.5 / R5.5 — the required/optional vocabulary, expressed **once**.
 *
 * Whether the target schema demands a field or merely offers it is three-state,
 * and the third state is not a shade of the second:
 *
 * | `FieldRequirement` | meaning |
 * |---|---|
 * | `required` | the schema declares the field required |
 * | `optional` | the schema declares the field optional |
 * | `not_established` | the source never stated a requirement — no claim can be made |
 *
 * **Two inputs, one vocabulary.** R2.5 and R5.5 are the same rule applied to two
 * different producers, which is why the resolver below is named for its input
 * rather than for its output:
 *
 * - the Schema_Detail_View derives the state from `SchemaField.nullable` —
 *   `fieldRequirementFromNullable` here;
 * - the mapping surfaces derive it from membership of the Approval_Payload's
 *   `unmapped_fields.target_required` roster — `utils/unsourced-target-fields.ts`,
 *   which imports {@link FieldRequirement} from this module and produces the
 *   states directly.
 *
 * Both then read the same words out of the label functions below.
 *
 * **Why this is a module and not per-surface strings.** `LGR-4854b3d-1`
 * (**template / ratified**) put `field_count`'s three-way expression in exactly
 * one place because *"the expression that produces it cannot be rewritten per
 * site"* (`LGR-4854b3d-2`), and its own design *"counted those sites and found one
 * more than it had enumerated"*. There are four readers of this vocabulary — the
 * approval panel, the run-detail Schema tab, the Schema_Detail_View, and
 * `SchemaSourceSelector`'s `SchemaPreview`, which reads the same `SchemaField`
 * without rendering the status. `utils/schema-field-count.ts` is the shape being
 * copied: one resolver plus one label function per reading context.
 *
 * The lookups are `Record<FieldRequirement, …>` rather than `if` chains so that
 * `LGR-4854b3d-1`'s own revisit condition — *"a fourth state is needed"* — lands
 * as a compile error at every label function instead of as a silent fallthrough
 * to whichever branch happened to be last.
 *
 * **Every one of these strings is text** (C1, `LGR-99050e6-1`, **template /
 * ratified**: *"Colour alone fails WCAG 1.4.1 and makes the inversion
 * invisible"*). A Cloudscape `Badge` colour may accompany a label and must never
 * replace it. Introduce no new emoji here or at any render site;
 * `MappingViewer.tsx:114-125`'s bare `💡` predates this spec and is C1's explicit
 * carve-out, not a pattern to copy.
 *
 * **`data-testid`s for `requirementLabel`'s three strings**, specified here so the
 * label and the handle a test grabs it by are written down together (design §4.6,
 * on `EmptyFieldList`'s model at `SchemaManagementPage.tsx:55-70` — distinct
 * sentences, distinct testids, no shared stem a reader has to complete mentally):
 *
 * | `FieldRequirement` | `data-testid` |
 * |---|---|
 * | `required` | `requirement-required` |
 * | `optional` | `requirement-optional` |
 * | `not_established` | `requirement-not-established` |
 *
 * That is `requirement-${requirement.replace('_', '-')}` for all three, and it is
 * total over the union.
 *
 * **Do NOT collapse the third state.** Two specific spellings are forbidden:
 *
 * - `nullable ?? true`, anywhere — not here, not in a cell renderer, not in
 *   `toWizardFields` (C12, do-NOT 16). It reports *"the source did not say"* as
 *   *"optional"*, which is C4 / `LGR-4854b3d-1`'s collapse in the permissive
 *   direction and is exactly the defect p45 exists to remove.
 * - `null` as a third value, accepted or emitted (do-NOT 17). `LGR-44d601e-1`
 *   (**template / ratified**) clears a stored value by *removing* the key
 *   precisely because *"a stored `""` would have to be distinguished from
 *   absence at every reader"*. Absence is the spelling; three spellings of two
 *   facts is the defect, not the fix.
 *
 * The `not_established` name is the repo's existing spelling for the same idea:
 * `shared/load_outcome.py:133`'s `OUTCOME_NOT_ESTABLISHED` marks the arm where
 * *"no claim is made"* about a run's row counts.
 */
export type FieldRequirement = 'required' | 'optional' | 'not_established';

/**
 * `SchemaField.nullable` as the Schema_Detail_View reads it — R5.2, R5.3.
 *
 * `nullable` is the negation of required (`handlers/schemas.py:1394`,
 * `field_name not in required`), so a stated `false` is a stated *requirement*.
 * The third state is **absence**: after p45 a producer that never established
 * nullability sends no key at all (`handlers/schemas.py:496` for Glue, whose
 * `StorageDescriptor.Columns[]` carries no nullability, and `:1569` for
 * `_schema_detail`), so `undefined` arrives here and must not be read as
 * permission.
 *
 * The non-boolean arm is written as the fallthrough on purpose: anything that is
 * not a stated boolean — `undefined`, or a `null` that escaped the type through a
 * cast — lands on `not_established` and can never land on `optional` (C4,
 * do-NOT 5, do-NOT 17). Do NOT fall back to `true` here.
 */
export function fieldRequirementFromNullable(nullable: boolean | undefined): FieldRequirement {
  if (nullable === false) return 'required';
  if (nullable === true) return 'optional';
  return 'not_established';
}

const LABELS: Record<FieldRequirement, string> = {
  required: 'Required by the target schema',
  optional: 'Optional in the target schema',
  not_established: "The target schema's requirement for this field was not established",
};

/**
 * The requirement as panel prose reads it — R2.1, R2.3, R5.3.
 *
 * Three whole sentences rather than a stem plus a word, so the `not_established`
 * case states the absence outright instead of leaving a reader to infer it from a
 * missing adjective.
 */
export function requirementLabel(requirement: FieldRequirement): string {
  return LABELS[requirement];
}

const CELL_TEXT: Record<FieldRequirement, string> = {
  required: 'Required',
  optional: 'Optional',
  not_established: 'Not established',
};

/**
 * The requirement as a Fields-table cell reads it — R5.1.
 *
 * Same root words as {@link requirementLabel}, cut to cell length: the
 * `fieldCountLabel` / `fieldCountCellText` split from `utils/schema-field-count.ts`
 * applied to a word instead of a number. Never an empty cell for
 * `not_established` — a blank is indistinguishable from a failed load, which is
 * the confusion `EmptyFieldList`'s two sentences already exist to remove.
 */
export function requirementCellText(requirement: FieldRequirement): string {
  return CELL_TEXT[requirement];
}

const RULES: Record<FieldRequirement, string | undefined> = {
  required:
    'A required target field that no source column supplies sends the mapping to a human for approval.',
  optional: undefined,
  not_established: undefined,
};

/**
 * R2.4 — **the rule, not the run.**
 *
 * A required target field with no source column forces human approval, and this
 * states that policy. It deliberately does **not** read `approval_required` off
 * the state row and does **not** re-derive the gate: the gate lives in
 * `agentic_data_ingestion_schema_mapping/lambda_function.py`'s
 * `determine_approval_requirement`, at `:1290-1296` —
 *
 *     unsourced_target_fields = unsourced_target_field_names(mapping_result, target_schema)
 *     required_target_fields = (target_schema or {}).get("required", []) or []
 *     required_unsourced = [f for f in unsourced_target_fields if f in required_target_fields]
 *     if required_unsourced:
 *         return True
 *
 * — and p43 task 13.1's reasoning applies verbatim: re-deriving that decision here
 * would be a second implementation of it in a second language, with no test able
 * to see the two drift (do-NOT 2). The citation stays in this comment so the next
 * reader can re-check the sentence against the policy; it never appears on screen.
 *
 * `undefined` for the other two states, because neither carries a rule to state:
 * an optional unsourced field does not force approval, and a field whose
 * requirement was never established cannot be said to do either (R2.6).
 */
export function requirementRule(requirement: FieldRequirement): string | undefined {
  return RULES[requirement];
}
