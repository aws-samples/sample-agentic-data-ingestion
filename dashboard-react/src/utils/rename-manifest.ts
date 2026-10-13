// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R10.1 / R10.2 — the Approval_Payload's Rename_Manifest, read **once**.
 *
 * Three approval surfaces show the proposed renaming of source columns whose
 * names cannot be used as target identifiers: the artifact panel
 * (`ApprovalArtifact`, a table), the review modal (`ApprovalsPage`, a list) and
 * the compact approval card (`ApprovalWorkflowSection`, one line). This module is
 * where the wire shape becomes those facts, and it is the **only** place that
 * reads `details.rename_manifest`.
 *
 * **Decision: one reader, normalise at the boundary — the shape
 * `utils/unsourced-target-fields.ts` established and `LGR-7561acb-1`
 * (template / ratified) requires.** That record's reason applies verbatim: *"a
 * union hands every one of the four readers the tolerance, which is three more
 * places for the two shapes to diverge."* So `renames` arrives as `unknown[]`,
 * exactly one function tolerates a malformed element, and one shape —
 * {@link ProposedRename} — travels downstream. *Rejected:* three local
 * `details.rename_manifest as …` casts, one per surface. `utils/approval-mapping-
 * count.ts` and `utils/approval-unsourced-target-fields.ts` both exist because two
 * of these same three surfaces had **already** diverged on one expression, one
 * correct and one collapsing null into a confident zero (`LGR-4854b3d-1`,
 * template / ratified: the expression cannot be rewritten per site). This is that
 * precedent applied a third time, before the divergence rather than after it.
 *
 * **Three states, and absence is one of them.**
 *
 * | `details.rename_manifest` | meaning | reader |
 * |---|---|---|
 * | a dict with `renames` entries | read; renames proposed | the entries |
 * | a dict whose `renames` is empty | read; every source column name already conforms | `[]` |
 * | absent, `null`, or not a dict | the payload says nothing about renames | `null` |
 *
 * The third row is **not** a guard against something hypothetical. It is the
 * common case today and stays common: the producer only started writing this key
 * with p55 task 6.2, so every mapping row written before it carries none;
 * `create_approval_payload`'s degraded `except` arm returns three keys and neither
 * this one nor `unmapped_fields`, which task 1.2's characterization recorded as an
 * obligation on *this* reader in as many words — *"any consumer of the new key
 * must tolerate its absence"*; and `approval_handler._schema_mapping_payload`
 * projects the key with `.get`, so an unreadable `output_data.approval` yields a
 * present `null` rather than a missing key. All three arrive here as *not
 * established*, and none of them may render as "no renames proposed" (p45 do-NOT 7
 * one column over: an empty roster and an unreadable payload are different facts).
 *
 * This does **not** pin the degraded payload as intended behaviour
 * (`LGR-b2f79ef-13`, template / ratified, which narrowed a blanket `except` in the
 * producer's own module); it only declines to make it worse.
 *
 * **The original name is attacker-controlled and is carried verbatim** (p55
 * do-NOT 1, do-NOT 17). Two consequences, and they are load-bearing rather than
 * hygiene:
 *
 * 1. **Nothing here sanitizes, normalises or trims it.** The manifest is a
 *    *proposal* reaching a human, and the whole point of R10.2 is that they see
 *    the name as it actually is beside the name it would become. A reader that
 *    tidied it would be showing them a third string that is neither.
 * 2. **It is only ever rendered as React text content**, which escapes by
 *    default. Do NOT pass one to `dangerouslySetInnerHTML`, into a URL or query
 *    string, or into a `title`/`aria-label`/`alt` attribute without deciding
 *    afresh — an attribute sink has different escaping and a tooltip cannot be
 *    truncated by the caller. {@link displayName} is the only thing in this file
 *    that alters a name, it is for **length** rather than for safety, and it is
 *    never applied to the value a machine reads.
 *
 * **The length cap mirrors `identifier_utils._DISPLAY_LIMIT`'s reasoning, not its
 * number.** That constant is 300 and exists so a *message* stays readable "even
 * for megabyte-sized garbage". A table cell has the same problem with a smaller
 * budget: a 2 MB column name in a `<td>` is the same class of defect as a 2 MB
 * error string, and the row it belongs to is one of several on screen. So the cap
 * here is its own number with its own reason (see {@link RENAME_DISPLAY_LIMIT}),
 * and truncation is **marked** — an elision the reader cannot see is a lie about
 * what the column is called.
 */
import type { Approval, ProposedRenamePayload, RenameManifestPayload } from '../types';

/** One Proposed_Rename as every surface reads it — both names, both strings. */
export interface ProposedRename {
  /** The source column name, VERBATIM (R10.2, do-NOT 1). Never sanitized here. */
  original_name: string;
  /** The proposed target column name. Satisfies the Identifier_Allowlist (R10.3),
   *  which the producer enforces before it emits the manifest — this reader does
   *  not re-check it, because a second definition of "conforming" in a second
   *  language is what `LGR-9e76249-7` (template / ratified) exists to prevent. */
  target_name: string;
}

/**
 * Characters of an original or proposed name that reach a screen.
 *
 * **Its own number, for its own reason.** `identifier_utils._DISPLAY_LIMIT` is 300
 * and bounds a *message* — one string, alone, in an error the reader has stopped to
 * read. This bounds a *cell*, in a table of rows an approver is scanning, beside a
 * second cell doing the same. 120 keeps a pathological name from pushing the
 * proposed name it must be compared against off the visible row, which is the one
 * thing this table exists to make possible.
 *
 * A real column name does not reach it: the producer's own sanitizer works on
 * header cells, and a header that long is already the "megabyte-sized garbage" case
 * that limit was written for.
 *
 * Truncation is MARKED (see {@link displayName}) and applies to the **display**
 * string only. `original_name` on {@link ProposedRename} is the full value, so a
 * test, a copy-paste or a later machine reader gets the real name.
 */
export const RENAME_DISPLAY_LIMIT = 120;

/** A payload that established nothing about proposed renames. */
export const RENAMES_NOT_ESTABLISHED = '\u2014';

/** A payload that was read and proposes no rename. */
export const RENAMES_NONE = 'None';

/**
 * The manifest's `renames`, or `null` when the payload established none.
 *
 * `null` is returned only for a value that is not the producer's dict at all. An
 * empty array means the dict was read and it proposes nothing — the distinction
 * all three callers render differently, and the reason this returns a nullable
 * array rather than an always-empty one.
 *
 * The `Array.isArray` rejection on the container is not redundant with the
 * `typeof` test: a JSON array is an `object`, and reading `renames` off one yields
 * `undefined`, which would report "no renames proposed" for a shape that stated
 * nothing.
 *
 * A malformed **element** is skipped rather than fatal, and rendered as nothing
 * rather than as a rename: an entry missing either name is not a Proposed_Rename
 * R10.2 can present, and inventing the missing half — `String(undefined)`, or the
 * original echoed as the target — would put a claim on screen the wire does not
 * support. Skipping is the only answer that makes no claim. `renames: []` with a
 * malformed element and `renames: []` from the producer are therefore
 * indistinguishable here, which is accepted: both say nothing was proposed that
 * this surface can show.
 */
export function proposedRenames(
  manifest: RenameManifestPayload | null | undefined,
): ProposedRename[] | null {
  if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) return null;
  if (!Array.isArray(manifest.renames)) return null;

  const entries: ProposedRename[] = [];
  for (const entry of manifest.renames) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const { original_name: original, target_name: target } = entry as ProposedRenamePayload;
    if (typeof original !== 'string' || typeof target !== 'string') continue;
    if (original === '' || target === '') continue;
    entries.push({ original_name: original, target_name: target });
  }
  return entries;
}

/**
 * The Approval_Payload's proposed renames, or `null` when it established none.
 *
 * The adapter over `Approval['details']`, mirroring
 * `utils/approval-unsourced-target-fields.ts`'s `approvalUnsourcedTargetFields`
 * so the two approval sub-payloads are reached the same way. Named at the point
 * of use — `Approval.details` stays `Record<string, unknown>` (`LGR-69ca78b-4`).
 */
export function approvalProposedRenames(details: Approval['details']): ProposedRename[] | null {
  return proposedRenames(details?.rename_manifest as RenameManifestPayload | null | undefined);
}

/**
 * A name as it appears on screen: verbatim, or truncated and **marked**.
 *
 * The marker matters more than the cap. An elided name that looks whole is a false
 * statement about what a column is called, and an approver comparing two names
 * cannot afford one of them to be quietly wrong. The suffix is included in the
 * budget so the returned string never exceeds {@link RENAME_DISPLAY_LIMIT}.
 *
 * Applied at the render sites only. It is deliberately **not** applied inside
 * {@link proposedRenames}, so the entry a test or a later reader sees carries the
 * real name.
 */
export function displayName(name: string): string {
  if (name.length <= RENAME_DISPLAY_LIMIT) return name;
  const marker = `… (${name.length} characters, truncated)`;
  return `${name.slice(0, Math.max(0, RENAME_DISPLAY_LIMIT - marker.length))}${marker}`;
}

/**
 * One Proposed_Rename as a line — both names, in the direction of the change.
 *
 * `→` rather than the word "to", matching the arrow this dashboard already uses
 * for a source/target pair (`schema_mapping`'s own mapping-hint rendering builds
 * `f"{src} → {tgt}"`). Both halves go through {@link displayName}.
 */
export function proposedRenameLine(entry: ProposedRename): string {
  return `${displayName(entry.original_name)} → ${displayName(entry.target_name)}`;
}

/**
 * The whole fact on one line, for the compact approval card.
 *
 * Total over all three states and never the empty string: a blank is
 * indistinguishable from a failed read, which is what the em dash exists to
 * remove.
 */
export function proposedRenamesText(details: Approval['details']): string {
  const entries = approvalProposedRenames(details);
  if (entries === null) return RENAMES_NOT_ESTABLISHED;
  if (entries.length === 0) return RENAMES_NONE;
  return entries.map(proposedRenameLine).join(', ');
}
