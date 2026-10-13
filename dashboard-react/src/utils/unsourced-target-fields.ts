// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p45 R1/R3/R4 — the Approval_Payload's unsourced-target facts, read **once**.
 *
 * Every mapping surface renders the same three facts about a target field no
 * source column supplies: whether the schema demands it, what the column will
 * contain, and why the mapper left it alone. This module is where the wire
 * shape becomes those facts, and it is the **only** place that reads
 * `unmapped_fields` (do-NOT 2). Three surfaces consume its output and none
 * re-derives it: the approval panel (`ApprovalArtifact.toMappingData`), the
 * run-detail Schema tab (`RunDetailPage`), and Schema History
 * (`SchemaHistoryPage`).
 *
 * **Decision: normalise at the boundary; do not widen to a union.**
 * `MappingData.unmapped_target` becomes `UnsourcedTargetField[]`, so the
 * tolerance for the wire's two element shapes lives in one function and one
 * shape travels downstream. *Rejected:* `(string | UnmappedEntry)[]`, which
 * would leave three tolerant readers of one shape —
 * `MappingViewer.tsx:184` and `:200` already carry the
 * `typeof field === 'string' ? … : (…).field ?? JSON.stringify` test, and C11
 * puts a third inside `DragDropMappingEditor`, whose entire subject is field
 * *names*. That is the tolerant-consumer shape `LGR-28b4d75-5` (**template /
 * ratified**) rejects. *Rejected:* a parallel `unmapped_target_entries` beside
 * `unmapped_target: string[]`, which is a second roster and a second ordering
 * authority (do-NOT 2, do-NOT 13).
 *
 * The producer already made this choice one language over:
 * `_unmapped_target_field_name` (`schema_mapping/lambda_function.py:977-994`)
 * is one tolerant reader with one shape downstream, and {@link entryName}
 * below is deliberately its mirror rather than a fresh guess at the same
 * question. There are in fact **two** Python copies of that reader — the
 * second is `create_code/code_assembly.py:165-194`, a behaviour-for-behaviour
 * copy that exists because the two Lambdas cannot import each other, linked
 * only by `tests/unit/lambdas/schema_mapping/test_unsourced_definition_parity.py`.
 * {@link entryName} is a third copy of the same predicate for the same reason,
 * and that parity test is the shape a future guard for this one would take.
 *
 * **This is not the narrowing `LGR-99050e6-5` refused.** That record refused
 * reducing what a frontend type admits so a parser would stop complaining.
 * This *widens* what the type carries — a name becomes a name plus a
 * requirement plus a default plus a reason — and leaves the wire as the
 * authority.
 *
 * **Everything here is phrased about the mapping, in the present indicative,
 * never about the run** (design §4.5). "The target column is written with …"
 * is true before the load and equally true afterwards, because it describes
 * what the mapping says rather than predicting an execution. Two things follow.
 * The approval panel and the run-detail Schema tab can share one wording, which
 * is what keeps `MappingViewer` free of a surface-mode prop (R7.6) — a
 * defaulted prop would technically comply and would buy two copies of copy in
 * exchange for a branch, the trade `LGR-4854b3d-1` refuses. And R3.5 falls out
 * for free: no sentence below names a run, so none can claim a row count, a
 * completeness percentage or a data-quality outcome.
 *
 * **The vocabulary lives next door, not here.** `utils/field-requirement.ts`
 * owns the words for `required` / `optional` / `not_established`; this module
 * owns the payload join and the two sentences that are specific to an unsourced
 * field. *Rejected:* one module for both — the Schema_Detail_View would then
 * import an Approval_Payload reader it has no use for.
 *
 * **No truncation anywhere on this path** (R4.1, do-NOT 14). There is no
 * `slice`, no `substring` and no line clamp in this file, and there must not be
 * one added: the `reason` is the mapping model's own account of a decision, and
 * a paraphrase of it is a claim this repository cannot substantiate. The
 * attribution is carried by {@link reasonAttribution}'s **heading** precisely so
 * the model's words are never edited to carry it.
 *
 * **`unmapped_source` is not widened here or anywhere** (do-NOT 19). Both
 * producers write bare names — `generate_simple_mapping:893` is
 * `unmapped_source = source_fields` — so an object shape there would be
 * invented. {@link unsourcedTargetFields} never reads the `source` key, and its
 * parameter type deliberately does not mention it.
 */
import type { FieldRequirement } from './field-requirement';

/** One target field no source column supplies, as every surface reads it. */
export interface UnsourcedTargetField {
  field: string;
  /** From `target_required` membership only (R1.3). `not_established` when the
   *  Unsourced_Keys were absent from the payload (R1.4, R2.6). */
  requirement: FieldRequirement;
  /** Present ONLY when the mapper recorded one. A present `null` is a recorded
   *  null default (R3.3); an absent key is no default recorded (R3.6). Read it
   *  with `'default_value' in entry`, never with `?? null` — LGR-44d601e-1.
   *
   *  **Both facts are reachable, which is why the distinction is not
   *  theoretical.** A present `null` arrives by three routes, all verified in
   *  the producer: `get_default_value` ends `return defaults.get(field_type)`
   *  (`:974`), so a target field whose declared `type` is none of the six
   *  known ones yields `None`; and `_default_value_or_none` (`:1053-1059`)
   *  returns `None` both for a non-dict `target_schema` and after absorbing an
   *  `AttributeError`/`TypeError` from an uploaded schema whose `properties` is
   *  a list or whose field entry is a string. An **absent** key arrives from
   *  the shapes that are not the producer's object at all — a bare-string
   *  Unmapped_Entry, a model's own unnormalised dict, or {@link
   *  unsourcedTargetFields}' roster-superset case — because both object
   *  producers write all three keys together (`:885-889`, `:1123-1127`). */
  default_value?: unknown;
  /** The mapper's own words, verbatim (R4.1, do-NOT 14). Absent for a
   *  bare-string Unmapped_Entry (R4.6). */
  reason?: string;
}

/**
 * The `unmapped_fields` object as this function reads it.
 *
 * Structural on purpose. `types.ts`'s exported `UnmappedFieldsPayload` is the
 * **named** wire contract — doc-commented with
 * `schema_mapping/lambda_function.py:1391-1397` as its source of truth and
 * guarded by the contract test that fails on a rename (R6.4) — and it is
 * structurally assignable to this. Declaring a second exported interface of the
 * same shape here would be the second declaration that guard exists to prevent,
 * so this one is local, unexported and lists only the keys actually read.
 *
 * `source` is absent from it by choice, not by oversight (do-NOT 19).
 */
interface UnsourcedTargetFieldsInput {
  target?: unknown[];
  target_unsourced?: string[];
  target_required?: string[];
}

/**
 * `UNSOURCED_MAPPING_REASON` — `schema_mapping/lambda_function.py:102`.
 *
 * Written by `_normalise_mapping_structure` (`:1122-1128`) onto a target field
 * it moves out of `mappings` because the entry named no source column.
 */
const UNSOURCED_MAPPING_REASON = 'No source field in generated mapping';

/**
 * `generate_simple_mapping`'s reason — `schema_mapping/lambda_function.py:888`.
 *
 * A **different fact** from the constant above, and the producer says so in as
 * many words at `:96-101`: *"Distinct from `generate_simple_mapping`'s 'No
 * matching source field found' (`:888`) on purpose — that one records that
 * name-similarity matching looked and found nothing; this one records that the
 * generated mapping claimed the field without naming a source."* Hence three
 * attributions in {@link reasonAttribution}, not two (R4.7, C14).
 */
const NO_MATCHING_SOURCE_FIELD_REASON = 'No matching source field found';

/**
 * The name of one `unmapped_fields.target` entry, tolerating both wire shapes.
 *
 * A deliberate mirror of the producer's `_unmapped_target_field_name`
 * (`schema_mapping/lambda_function.py:977-994`): a bare string is trimmed; an
 * object's `field` is trimmed when it is a string; anything else has no name.
 *
 * **The trim is load-bearing, and it is why this is a mirror rather than an
 * `entry.field === name` test.** The roster in `target_unsourced` is built from
 * names that have *already been through* that function
 * (`unsourced_target_field_names:1187-1190`), so a detail entry
 * `{field: '  phone  '}` contributes the roster name `phone`. Comparing the raw
 * `field` against the roster would fail to find that entry, and the field would
 * then render R3.6's "the mapper recorded no default value" — a false statement
 * about a default that was in fact recorded. Reproducing the producer's own
 * identity relation is what makes the lookup exact rather than merely strict.
 *
 * Case is **not** folded, for the same reason: the producer does not fold it, so
 * `Phone` and `phone` are two fields to it and must stay two here.
 */
function entryName(entry: unknown): string {
  if (typeof entry === 'string') return entry.trim();
  if (entry !== null && typeof entry === 'object') {
    const name = (entry as Record<string, unknown>).field;
    return typeof name === 'string' ? name.trim() : '';
  }
  return '';
}

/**
 * Join the Approval_Payload's `unmapped_fields` into one entry per unsourced
 * target field — R1.3, R1.4, R1.5, R1.7, R2.6, R3.3, R3.6, R4.6.
 *
 * Four steps and nothing else (design §4.8):
 *
 * 1. **Roster and order** come from `target_unsourced` (`:1395`), **taken as
 *    received**. The producer already orders schema-`required` names first
 *    (`:1199-1200`, pinned by
 *    `test_schema_mapping_approval.py::test_approval_payload_target_unsourced_names_all_of_them_required_first`,
 *    which feeds `[nickname, phone]` with `required: ['phone']` and asserts
 *    `['phone', 'nickname']` comes back). Re-sorting here would create a second
 *    ordering authority (R1.5, do-NOT 13). Every name renders, optional ones
 *    included, because an approver must not be shown a filtered list whose
 *    filter is invisible (R1.7).
 * 2. **`default_value` and `reason`** are looked up in `target` (`:1393`) by
 *    name, via {@link entryName} on both sides. A roster name with no matching
 *    entry carries neither key — reachable, not hypothetical:
 *    `unsourced_target_field_names` also names source-less `mappings` entries
 *    (`:1192-1197`), so the roster is a **superset** of `target`. Such an entry
 *    renders R3.6's sentence and makes no reason claim (R4.6).
 * 3. **`requirement`** is `required` iff the name is in `target_required`
 *    (`:1396`), else `optional`. Nothing else is consulted — not
 *    `proposed_mappings`, not `target_schema.required`, not any frontend
 *    expression (R1.3, do-NOT 2). `LGR-9e76249-7` (**template / ratified**) is
 *    the reason: *"one shared definition: `unsourced_target_field_names` reads
 *    both lists … and the approval decision, the approval payload and the
 *    normaliser all call it, so they cannot drift about what 'unsourced'
 *    means."* A second definition here would put the drift back.
 * 4. **Absent keys** fall back. Roster and requirement are answered
 *    **independently**, which is what makes this total over all four
 *    combinations without inventing a claim:
 *    - no `target_unsourced` → the roster is `target`'s own names in `target`'s
 *      order (R1.4);
 *    - no `target_required` → there is no authority for a requirement, so every
 *      entry is `not_established` and **nothing says "optional"** (R2.6,
 *      do-NOT 5).
 *    Both absent together is the spec'd case and gets both halves. The two
 *    mixed combinations are unreachable from the producer — `:1391-1397` writes
 *    the two keys together in one dict literal — and are answered this way
 *    because it is the answer that makes the fewest claims.
 *
 * **Per C15 the fallback is the COMMON case today, not an edge case.** p37 is
 * not deployed (`backlog.md` item 20: *"Needs Compute + AgentCore"*), and the
 * Unsourced_Keys are written by `create_approval_payload:1395-1396`, which
 * ships in that deploy. Until it lands, `target_unsourced` is absent from every
 * payload every account produces and **every p45 surface takes this branch**.
 * Two further causes are permanent: mapping records written before p37, and
 * C9's degraded payload (`:1435-1439`), which carries no `unmapped_fields` at
 * all — hence the nullable parameter and the empty return rather than a throw.
 *
 * The degraded payload is **not** pinned here as intended behaviour
 * (do-NOT 10, `LGR-b2f79ef-13`); this function only declines to make it worse.
 *
 * **One asymmetry, stated because it is surprising.** The roster from
 * `target_unsourced` is used verbatim, duplicates and all — "taken as received"
 * means one entry per roster *element*, and the real producer cannot emit a
 * duplicate anyway (`:1189` dedupes). The fallback roster is *constructed* from
 * `target`, so it dedupes: the only non-arbitrary construction is the one the
 * producer itself applies to that same list, and rendering one field twice
 * would be a defect visible on screen. First occurrence wins, preserving
 * `target`'s order.
 */
export function unsourcedTargetFields(
  payload: UnsourcedTargetFieldsInput | null | undefined,
): UnsourcedTargetField[] {
  const detail = Array.isArray(payload?.target) ? payload.target : [];
  const rosterKey = Array.isArray(payload?.target_unsourced) ? payload.target_unsourced : undefined;
  const requiredKey = Array.isArray(payload?.target_required) ? payload.target_required : undefined;

  // Step 2's index. Only object entries are indexed: a bare-string entry has no
  // `default_value` and no `reason` to look up, which is R4.6 rather than a
  // gap. First object per name wins, mirroring the producer's own first-wins
  // dedupe at `:1189`.
  const detailByName = new Map<string, Record<string, unknown>>();
  const fallbackRoster: string[] = [];
  for (const entry of detail) {
    const name = entryName(entry);
    if (!name) continue;
    if (!fallbackRoster.includes(name)) fallbackRoster.push(name);
    if (entry !== null && typeof entry === 'object' && !detailByName.has(name)) {
      detailByName.set(name, entry as Record<string, unknown>);
    }
  }

  const roster = rosterKey ?? fallbackRoster;
  const requiredNames = requiredKey === undefined ? undefined : new Set(requiredKey);

  return roster.map((rawName) => {
    const field = entryName(rawName);
    const result: UnsourcedTargetField = {
      field,
      // Membership is tested against the **roster element**, not the trimmed
      // name, and that is the faithful mirror rather than an oversight:
      // `target_required` is
      // `[f for f in unsourced_target if f in required_target_fields]`
      // (`:1353`) — filtered out of the roster list itself, which `:1351`
      // computed two lines above — so its members are byte-equal to roster
      // elements. On the fallback roster the element is already the trimmed
      // name, so one expression is correct on both paths.
      requirement:
        requiredNames === undefined ? 'not_established' : requiredNames.has(rawName) ? 'required' : 'optional',
    };

    const entry = detailByName.get(field);
    if (entry !== undefined) {
      // `'default_value' in entry`, never `entry.default_value ?? null`
      // (`LGR-44d601e-1`, **template / ratified**, whose own revisit condition is
      // *"a reader needs to tell 'cleared' apart from 'never set'"* — this is
      // that reader). A present `null` is a recorded null default (R3.3); an
      // absent key is no default recorded (R3.6); `??` collapses the two.
      if ('default_value' in entry) result.default_value = entry.default_value;
      // Verbatim, or absent. A non-string `reason` is not a sentence, and
      // carrying it as one would be a lie the `string` type cannot catch.
      if (typeof entry.reason === 'string') result.reason = entry.reason;
    }

    return result;
  });
}

/**
 * Render a recorded `default_value` for {@link consequenceStatement}.
 *
 * **Total, and never the empty string.** `get_default_value`
 * (`schema_mapping/lambda_function.py:961-974`) returns `''` for a `string`
 * field, `0` for a number, `false` for a boolean, `[]` for an array and `{}`
 * for an object — so a recorded empty string is the *common* case, and a
 * non-scalar default is reachable rather than theoretical. Rendering `''` as
 * nothing would produce "written with the default the mapper recorded, , on
 * every row", which is exactly the unreadable blank R3.3 removes for `null`.
 * Quoting via `JSON.stringify` makes `""` legible as a value, keeps every other
 * JSON value verbatim, and matches the `?? JSON.stringify(field)` fallback
 * `MappingViewer.tsx:184` already uses for an entry it cannot read.
 *
 * The `??` here is a fallback for a value `JSON.stringify` cannot express — a
 * `BigInt`, which throws, or a `function`/`undefined`, which return `undefined`,
 * none of them reachable from parsed JSON. It is **not** the
 * `default_value ?? null` shape `LGR-44d601e-1` forbids: whether a default was
 * recorded at all is decided before this is called, by
 * `'default_value' in entry`.
 */
function formatDefaultValue(value: unknown): string {
  let rendered: string | undefined;
  try {
    rendered = JSON.stringify(value);
  } catch {
    rendered = undefined;
  }
  return rendered ?? String(value);
}

/**
 * What the mapping says will be in the column — R3.1 through R3.6.
 *
 * Three cases, because `default_value` is three-state (design §4.6):
 *
 * | case | sentence |
 * |---|---|
 * | a default was recorded and it is not null | names the value |
 * | a default was recorded and it is null (R3.3) | says "as null … that is the default the mapper recorded" |
 * | no default was recorded (R3.6) | says "written empty … the mapper recorded no default value" |
 *
 * The third case is **not** a producer that omits the key — both object
 * producers write `field`, `default_value` and `reason` together (`:885-889`,
 * `:1123-1127`). It is the shapes that are not that object: a bare-string
 * Unmapped_Entry, a model's own unnormalised dict, or a roster name with no
 * matching `target` entry. See {@link UnsourcedTargetField.default_value} for
 * the three routes to a present `null`, which is the case it must not be
 * confused with.
 *
 * **R3.4 holds in all three**: every sentence says the column *is written*, so
 * none claims it will be absent from the target. The generated code does write
 * it — as `lit(None).cast(…)` on the observed run — so the column exists and is
 * empty.
 *
 * **R3.5 holds because no sentence names a run.** A sentence that never
 * mentions an execution cannot claim a row count, a completeness percentage or
 * a data-quality outcome, all of which are measured elsewhere and after the
 * load. Do NOT add one; a pre-load panel restating a post-load measurement
 * would be a second authority for it.
 *
 * The word `null` is spelled out rather than rendered as an empty string, which
 * is the whole of R3.3 — "the default is null" and "no default was recorded"
 * have to be distinguishable on screen, not just in the payload.
 */
export function consequenceStatement(entry: UnsourcedTargetField): string {
  if (!('default_value' in entry)) {
    return 'No source column supplies this field. The target column is written empty on every row; the mapper recorded no default value.';
  }
  if (entry.default_value === null) {
    return 'No source column supplies this field. The target column is written as null on every row — that is the default the mapper recorded.';
  }
  return `No source column supplies this field. The target column is written with the default the mapper recorded, ${formatDefaultValue(entry.default_value)}, on every row.`;
}

/**
 * The heading a `reason` is rendered under — R4.7, C14, three-way.
 *
 * | `reason` | heading |
 * |---|---|
 * | `:102`'s `UNSOURCED_MAPPING_REASON` | `Reason (structural — the generated mapping named no source column)` |
 * | `:888`'s constant | `Reason (structural — name-similarity matching found no candidate)` |
 * | anything else | `Reason (the mapping model's own account)` |
 *
 * Two of the three are machine-written constants that mean different things and
 * the third is a model's judgement, so there are three attributions rather than
 * the two R4.7 names — see {@link NO_MATCHING_SOURCE_FIELD_REASON} for the
 * producer's own statement of the distinction.
 *
 * **The attribution lives in the heading, and the body is the `reason` string
 * verbatim** (R4.1, do-NOT 14). That split is the point: it lets a surface say
 * where a sentence came from without editing the sentence to carry it.
 *
 * The comparison is exact — no trim, no case fold. A reason that differs from a
 * constant by a character is the model's own wording, and attributing it to the
 * normaliser would be a claim about provenance this function cannot support. A
 * model that happens to emit a constant byte-for-byte is indistinguishable, and
 * that is accepted: it is the same sentence.
 *
 * The heading is **distinct from the schema's own `description` and from the
 * Context_Sidecar's `context`** (R4.8, C5, `LGR-acca05d-3`). This is the third
 * prose authority on that row; it is never merged with either of the others.
 */
export function reasonAttribution(reason: string): string {
  if (reason === UNSOURCED_MAPPING_REASON) {
    return 'Reason (structural — the generated mapping named no source column)';
  }
  if (reason === NO_MATCHING_SOURCE_FIELD_REASON) {
    return 'Reason (structural — name-similarity matching found no candidate)';
  }
  return "Reason (the mapping model's own account)";
}
