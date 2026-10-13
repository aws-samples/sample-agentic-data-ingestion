// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Approval } from '../types';

/**
 * p54 task 4.4 — the reuse fact on the approval payload, read **once** for both
 * approval surfaces (R1.4, R2.1, R2.2).
 *
 * ## Why a module and not two expressions
 *
 * Two surfaces render the schema-mapping approval payload, and both are
 * `details`-driven: `ApprovalWorkflowSection`'s `renderTypeSpecificContent`
 * (the Mapping Preview card, rendered twice from that file — the pending card
 * and the completed one) and `ApprovalsPage`'s `ApprovalTypeDetails` (the review
 * modal Approve/Reject is clicked from).
 *
 * `LGR-4854b3d-1` (**template / ratified**) ruled that a payload expression
 * cannot be rewritten per display site: it rejected keeping `fields?.length ?? 0`
 * at each site because its own design *"counted those sites and found one more
 * than it had enumerated"*, and put the expression in one module.
 * `LGR-e59edd7-6` (**template / ratified**) then applied that ruling to **these
 * two surfaces specifically**, for `unmapped_fields`, *after they had already
 * diverged* on `mapping_count` — one spelling `?? '\u2014'` and one `?? 0`, so
 * one reported an unreadable payload as a confident zero. So this is not a style
 * preference: it is the same pair of surfaces, the same payload, and the same
 * failure mode one key over. `approval-mapping-count.ts` and
 * `approval-unsourced-target-fields.ts` are the two in-tree precedents this
 * module copies.
 *
 * The **words** live here too, not just the derivation. That is
 * `LGR-e59edd7-6`'s other half: two surfaces that each phrase one fact are two
 * surfaces that can disagree about it.
 *
 * ## The three payload states, as the producer actually emits them
 *
 * `_reused_mapping_payload` (`approval_handler/lambda_function.py`) emits:
 *
 * | State | Shape |
 * |---|---|
 * | **resolved** | the origin payload's ten keys + `reused_mapping: true` + `reused_mapping_id` + `reused_from_run_id` |
 * | **unreadable** | exactly five keys — `reused_mapping`, `reused_mapping_id`, `mapping_unreadable: true`, `proposed_mappings: null`, `mapping_count: null` |
 * | **no reuse** | `{}` — a different fact, and not p54's to change |
 *
 * ## The THIRD route, and why {@link ReusedMappingDisplay.unreadable} is not
 * simply `mapping_unreadable`
 *
 * Task 2.3's Property 1 found a state neither of task 4.4's two named arms
 * covers, and flagged it for this task: **a *resolved* payload can carry a
 * non-list `proposed_mappings`** — `'not-a-list'`, `{}`, `4`, `false` — with all
 * thirteen keys, `mapping_count: null`, and **no `mapping_unreadable`**.
 *
 * It is not a producer defect. The producer's post-projection guard
 * (`lambda_function.py:481`) routes a projection whose `proposed_mappings` came
 * back `None` into the unreadable statement, so `null` specifically cannot reach
 * a resolved payload — but the *other* non-list shapes can, and the design's own
 * state table admits them.
 *
 * Keyed on `mapping_unreadable` alone, such a payload would render as **a mapping
 * table with no rows beside a payload asserting it is readable**, with the count
 * as an em dash: `toMappingData` (`ApprovalArtifact.tsx:124`) yields
 * `mappings: []` for a non-array, and `mappingCountText` yields `'\u2014'` for
 * the `null` count. That is exactly do-NOT 7's collapse — `LGR-b2f79ef-14`'s
 * *named reference to something absent* rendered as a thing with no fields —
 * arriving by a route the producer's marker does not signal.
 *
 * **The decision taken here:** a payload that states reuse but whose
 * `proposed_mappings` is not an array is classified `unreadable`, because from
 * the approver's position it is the same fact — there is no mapping to review —
 * and the alternative is the prohibited rendering. One sentence rather than a
 * fourth state: the design has three states, and a fourth would be vocabulary
 * this reader invented for a shape no producer labels. The sentence says the
 * mapping could not be read, which is true of both routes; it does not claim the
 * producer marked it.
 *
 * **Not widened further, and recorded rather than fixed.** `types.ts` declares
 * `proposed_mappings?: unknown[] | null`, which is narrower than what the wire
 * can carry — the declaration is inaccurate for the same reason
 * `LGR-e59edd7-7`'s item (1) records `ApprovalsPage`'s `{source, target}` cast
 * as wrong and leaves it: it needs a decision this task did not size. This
 * module reads the key off `Approval['details']`, which is
 * `Record<string, unknown>` (deliberately loose per
 * `LGR-69ca78b-4`), so `Array.isArray` is the whole narrowing and no type change
 * is needed. One consequence worth knowing before writing a fixture: a
 * `proposed_mappings` of `'not-a-list'` reaches `ApprovalsPage`'s untouched
 * duplicate table as a **string** with a truthy `.length`, which that table
 * passes to `Table items=`. That hazard belongs to the table `LGR-e59edd7-7`
 * deliberately left standing (do NOT touch it), not to this reader.
 *
 * ## Absence is a state, and it renders nothing
 *
 * A payload that states no reuse gets **no** reuse block on either surface.
 * `false` there would assert the run generated its own mapping, which is the
 * collapse do-NOT 7 forbids — the same reason `get_run_details` omits the key
 * rather than defaulting it (task 3.1, Property 7).
 *
 * ## Words, not colour
 *
 * Every state below is carried by a sentence. `LGR-99050e6-1` (**template /
 * ratified**) records that colour alone fails **WCAG 1.4.1**, and
 * `LGR-1f7892b-2` restates it for a `StatusIndicator` type specifically — *"a
 * `warning` type *is* colour, so the sentence is the carrier and the type is
 * not"*. do-NOT 8. Neither caller of this module renders a status colour for the
 * reuse fact.
 */

/** What the approval payload states about a reused mapping. */
export interface ReusedMappingDisplay {
  /**
   * Whether the payload states anything about a reused mapping at all.
   *
   * The gate for the whole block, exported rather than left as `reused ||
   * unreadable` at each site — that expression is precisely what
   * `LGR-4854b3d-1` refuses to have written twice.
   */
  stated: boolean;
  /** Whether the payload states the run reused an existing mapping (R2.1). */
  reused: boolean;
  /**
   * The reused mapping's identifier, or `null` when the payload stated none
   * (R2.2). A blank or non-string id is `null`: a blank is indistinguishable
   * from an absent one to a reader, and naming nothing is not naming a mapping.
   */
  mappingId: string | null;
  /**
   * Whether the mapping the payload names could not be read (R1.4).
   *
   * True on **two** routes — the producer's `mapping_unreadable` marker, and a
   * payload that states reuse whose `proposed_mappings` is not an array (task
   * 2.3's third route, argued in this module's header).
   */
  unreadable: boolean;
}

/** The payload states the run reused a mapping an earlier run produced (R2.1). */
export const REUSED_MAPPING_STATEMENT = 'This mapping was reused from an earlier run.';

/**
 * The payload names a mapping that could not be read (R1.4).
 *
 * Design's Error Handling posture, verbatim: *"the approver is told the mapping
 * could not be read instead of being shown an empty mapping"*. No claim about
 * where the mapping would otherwise appear, because the two surfaces lay it out
 * differently and a layout claim would be false on one of them.
 */
export const REUSED_MAPPING_UNREADABLE_STATEMENT =
  'The reused mapping could not be read, so its fields are not shown.';

/** Label for the reused mapping's identifier, shared so the two surfaces agree. */
export const REUSED_MAPPING_ID_LABEL = 'Reused mapping';

/** A reused mapping whose identifier the payload did not state. */
export const REUSED_MAPPING_ID_NOT_ESTABLISHED = '\u2014';

/**
 * The reuse fact, as both display sites read it.
 *
 * Total over every shape `Approval['details']` can hold, including `undefined`:
 * `ApprovalsPage` and `ApprovalWorkflowSection` both coalesce to `{}` before
 * calling, but a reader that only works for coalesced input is a reader the
 * third caller gets wrong.
 */
export function reusedMappingDisplay(details: Approval['details']): ReusedMappingDisplay {
  const declaredId = details?.reused_mapping_id;
  const mappingId =
    typeof declaredId === 'string' && declaredId.trim() !== '' ? declaredId : null;

  const reused = details?.reused_mapping === true;
  // `=== true` rather than truthiness on both flags: `details` is
  // `Record<string, unknown>`, so `'false'` is a truthy string and would invert
  // the fact — the same reason invariant `LGR-69ca78b-1` keeps `IsBoolean` in
  // the governance Choice's conjuncts rather than testing the value alone.
  const marked = details?.mapping_unreadable === true;

  // The third route. Only asked of a payload that states reuse: on a non-reuse
  // payload `proposed_mappings` is the run's own proposal and is no business of
  // this reader.
  const listUnreadable = reused && !Array.isArray(details?.proposed_mappings);

  const unreadable = marked || listUnreadable;
  return { stated: reused || unreadable, reused, mappingId, unreadable };
}

/**
 * The reused mapping's identifier as both sites render it: the id, or an em dash
 * that asserts nothing.
 *
 * Same rendering choice as `mappingCountText` for `mapping_count`
 * (`utils/approval-mapping-count.ts`), so the two not-established indications
 * read alike on a surface that carries both.
 */
export function reusedMappingIdText(details: Approval['details']): string {
  return reusedMappingDisplay(details).mappingId ?? REUSED_MAPPING_ID_NOT_ESTABLISHED;
}
