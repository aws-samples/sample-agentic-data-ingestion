// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Approval, UnmappedFieldsPayload } from '../types';
import { requirementCellText } from './field-requirement';
import { unsourcedTargetFields, type UnsourcedTargetField } from './unsourced-target-fields';

/**
 * p50 task 5.3 — the two approval surfaces read `unmapped_fields` **through the
 * ratified reader**, and the expression lives here once.
 *
 * ## What was wrong
 *
 * `ApprovalsPage.tsx:42` read `(details.unmapped_fields as number) ?? 0` and
 * rendered the result raw. The producer writes a **dict** —
 * `schema_mapping/lambda_function.py:1740-1744`,
 * `{source, target, target_unsourced, target_required}` — so once p50 task 2.1
 * pointed `get_approval_data` at `output_data.approval`, the key stopped
 * arriving `null` and started arriving populated. Measured at the render before
 * this change:
 *
 *     Objects are not valid as a React child (found: object with keys
 *     {source, target, target_unsourced, target_required})
 *
 * which took down the **entire review modal**, including the mapping count task
 * 4.2 had just corrected. Before 2.1 the key was `null`, `?? 0` produced `0`,
 * and the cast was merely false rather than fatal. The sibling site
 * (`ApprovalWorkflowSection.tsx:241`) survived only by accident: `String(...)`
 * on the same dict yields `"[object Object]"`, which is wrong in a quieter way.
 *
 * ## Why this delegates instead of counting
 *
 * There is no number on the wire to render. The producer writes two rosters and
 * two auxiliary lists, and `unmapped_source` / `target_unsourced` are different
 * facts about different schemas — so any single "unmapped fields: N" would be a
 * number this repository invented. Three ratified records forbid exactly that:
 *
 * - `LGR-9e76249-7` (**template / ratified**) — the reason there is **one**
 *   definition of "unsourced": *"`unsourced_target_field_names` reads both lists
 *   … and the approval decision, the approval payload and the normaliser all
 *   call it, so they cannot drift about what 'unsourced' means."* A second
 *   definition on this surface would put that drift back, in a second language.
 * - `LGR-7561acb-1` (**template / ratified**) — the tolerance for the wire's two
 *   element shapes lives in exactly one function, because *"a union hands every
 *   one of the four readers the tolerance, which is three more places for the
 *   two shapes to diverge."* {@link unsourcedTargetFields} is that function and
 *   this module calls it rather than re-deriving anything.
 * - `LGR-7561acb-4` (**template / ratified**) — the required/optional words live
 *   in `utils/field-requirement.ts` and nowhere else, because
 *   `LGR-4854b3d-1`'s own design *"counted those sites and found one more than
 *   it had enumerated"*. {@link requirementCellText} is imported, not restated.
 *
 * So this module is an **adapter**, not an authority: `Approval['details']` in,
 * the ratified join out, and the ratified vocabulary applied to it. It reads no
 * key that `unsourcedTargetFields` does not already read, and it never touches
 * `source` (p45 do-NOT 19 — both producers write bare names there, so an object
 * shape would be invented and this surface has nothing to say about it).
 *
 * ## Why a module and not two expressions
 *
 * The same two surfaces already needed this once. `utils/approval-mapping-count.ts`
 * exists because `ApprovalWorkflowSection`'s count and `ApprovalsPage`'s count
 * had **already diverged** — one correct, one collapsing null to `0`. These two
 * `unmapped_fields` reads diverged the same way, into a crash and an
 * `"[object Object]"`. One module for both is that precedent applied a second
 * time, and it is `LGR-4854b3d-1`'s ruling: the expression cannot be rewritten
 * per site.
 *
 * ## Three states, and absence is one of them
 *
 * | `details.unmapped_fields` | meaning | rendering |
 * |---|---|---|
 * | a dict with unsourced target fields | the payload was read | the field names, each with its requirement |
 * | a dict whose roster is empty | the payload was read and every target field has a source | {@link UNSOURCED_NONE} |
 * | absent, `null`, or not a dict | the payload could not be read — **not established** | {@link UNSOURCED_NOT_ESTABLISHED} |
 *
 * The third row is the reachable common case rather than a guard: an approval
 * row written before p50 task 2.1 carries `unmapped_fields: null`, and
 * `create_approval_payload`'s degraded return (`:1435-1439`) carries no
 * `unmapped_fields` key at all. It renders as an em dash for the same reason
 * `mapping_count` does — "no unsourced target fields" and "the payload could not
 * be read" are different facts, and collapsing them is do-NOT 7's defect one
 * column over. This does **not** pin the degraded payload as intended behaviour
 * (p45 do-NOT 10, `LGR-b2f79ef-13`); it only declines to make it worse.
 */

/** A payload that established nothing about unsourced target fields. */
export const UNSOURCED_NOT_ESTABLISHED = '\u2014';

/** A payload that was read and records no unsourced target field. */
export const UNSOURCED_NONE = 'None';

/**
 * The Approval_Payload's unsourced target fields, or `null` when the payload
 * established none.
 *
 * `null` is returned only for a payload that is not the producer's dict at all.
 * An empty array means the dict was read and its roster is empty — the
 * distinction the two callers render differently, and the reason this returns a
 * nullable array rather than an always-empty one.
 *
 * The `Array.isArray` rejection is not redundant with the `typeof` test: a JSON
 * array is an `object`, and `unsourcedTargetFields` would read `target` off it
 * as `undefined` and return `[]` — reporting "no unsourced target fields" for a
 * shape that stated nothing.
 */
export function approvalUnsourcedTargetFields(
  details: Approval['details'],
): UnsourcedTargetField[] | null {
  const payload = details?.unmapped_fields;
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) return null;
  // Named at the point of use, exactly as `RunDetailPage.tsx:298` does it.
  // `UnmappedFieldsPayload` is the guarded wire contract — a contract test fails
  // on a rename of either Unsourced_Key (p45 R6.4/R6.5) — so this is the
  // established spelling rather than a fresh assertion about the shape. Naming
  // the nested payload is not narrowing `Approval.details`, which stays
  // `Record<string, unknown>` (`LGR-69ca78b-4`).
  return unsourcedTargetFields(payload as UnmappedFieldsPayload);
}

/**
 * One field as a line: the name, then what the target schema demands of it.
 *
 * The requirement word comes from {@link requirementCellText} — `Required`,
 * `Optional`, or `Not established` — so this surface cannot disagree with the
 * Fields table or the mapping panel about the same field. `Not established` is
 * the state a pre-p37 payload lands in, and it is a whole third state rather
 * than a shade of `Optional` (p45 do-NOT 5).
 */
export function unsourcedTargetFieldLine(entry: UnsourcedTargetField): string {
  return `${entry.field} (${requirementCellText(entry.requirement)})`;
}

/**
 * The whole fact on one line, for the compact approval card.
 *
 * Total over all three states, and never the empty string: a blank is
 * indistinguishable from a failed read, which is the confusion the em dash
 * exists to remove.
 */
export function unsourcedTargetFieldsText(details: Approval['details']): string {
  const entries = approvalUnsourcedTargetFields(details);
  if (entries === null) return UNSOURCED_NOT_ESTABLISHED;
  if (entries.length === 0) return UNSOURCED_NONE;
  return entries.map(unsourcedTargetFieldLine).join(', ');
}
