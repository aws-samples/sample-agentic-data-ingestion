// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Approval } from '../types';

/**
 * p50 C2's three-state mapping count, defined **once** — task 4.2 (R1.4, R1.5).
 *
 * Two surfaces render a mapping count off the same `approval.details` payload:
 * `ApprovalWorkflowSection`'s `renderTypeSpecificContent` (the pending and
 * completed approval cards) and `ApprovalsPage`'s `ApprovalTypeDetails` (the
 * review modal). Before this module they each wrote the expression by hand and
 * **they had already diverged**: the first spelled it `?? '\u2014'` and was
 * correct, the second spelled it `?? 0` and reported an unreadable payload as a
 * confident zero.
 *
 * | `details.mapping_count` | meaning | rendering |
 * |---|---|---|
 * | a positive number | the payload was read and carries that many mappings | the number |
 * | `0` | the payload was read and carries no mappings | `0` |
 * | `null` or absent | the payload could not be read — **not established** | an em dash |
 *
 * **Why a module rather than two expressions.** `LGR-4854b3d-1` (**template /
 * ratified**) ruled exactly this three-state shape for `field_count` and
 * rejected keeping the collapsing expression at each display site, because
 * "every hand-written `?? 0` collapses null into 'declares nothing'" — its own
 * design "counted those sites and found one more than it had enumerated". It put
 * the expression in one place, `utils/schema-field-count.ts`, and this module is
 * that precedent applied to the count p50 C2 derives. `LGR-7561acb-4`
 * (**template / ratified**) restates the same refusal of per-surface copies and
 * names `schema-field-count.ts` as the precedent to follow.
 *
 * **Why the cost is not hypothetical.** `LGR-9a35b2d-2` (**template /
 * ratified**) measured a wrong mapping-count zero on the sibling listing
 * surface: `mapping_count: 0` reported for a pipeline whose `schema_mapping` row
 * carried **7**. Zero mappings and an unreadable payload are different facts
 * (R1.5), and collapsing them is the class of quiet wrongness p50 exists to
 * remove (do-NOT 7).
 *
 * **Why there is no `proposed_mappings.length` fallback here.** The producer
 * already derives the count that way —
 * `approval_handler/lambda_function.py:343` is
 * `len(proposed_mappings) if isinstance(proposed_mappings, list) else None` — so
 * the key is present and numeric whenever the payload is readable, and `null`
 * exactly when it is not. A second derivation in TypeScript could therefore only
 * fire for a payload the producer could not read, where `proposed_mappings` is
 * unreadable too: dead by construction. It would also be a second authority for
 * a derivable number in a second language, which `LGR-28b4d75-1` and
 * `LGR-7561acb-7` both reject.
 */
export function approvalMappingCount(details: Approval['details']): number | null {
  const declared = details?.mapping_count;
  // A stated count wins, **including 0** — the one value a `??` chain swallows.
  // `Number.isFinite` rather than a bare `typeof`: `details` is
  // `Record<string, unknown>` (`types.ts:316`), deliberately loose per
  // `LGR-99050e6-5`, so a non-numeric or non-finite value reaching here is a
  // payload that established no count rather than one to stringify as a count.
  return typeof declared === 'number' && Number.isFinite(declared) ? declared : null;
}

/**
 * The count as both display sites read it: the number, or an em dash that
 * asserts nothing. Same rendering choice as `fieldCountCellText` for
 * `field_count`, so the two counts read alike on the surfaces that carry both.
 */
export function mappingCountText(details: Approval['details']): string {
  const count = approvalMappingCount(details);
  return count === null ? '\u2014' : String(count);
}
