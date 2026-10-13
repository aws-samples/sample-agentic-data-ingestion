// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 R1.4 / R4.1 — which Glue databases a Glue Catalog **target** can accept,
 * and the words that explain a refusal, defined **once**.
 *
 * Two `GlueCatalogForm`s choose a Glue database as a pipeline target — the
 * create wizard's (`pages/wizard/TargetForms.tsx`) and the edit modal's
 * (`components/TargetConfigForm.tsx`). R1.4 makes "the two cannot disagree
 * about which databases are offerable" the criterion, so the rule that decides
 * it is a module rather than an expression written out at each site.
 *
 * **Why a module and not two `startsWith` calls.** This is `LGR-4854b3d-1`'s
 * shape (**template / ratified**), whose own words are that "the expression
 * lives once, in utils/schema-field-count.ts" — it rejected keeping
 * `fields?.length ?? 0` at each display site because design §5 risk 3 counted
 * those sites and "found one more than it had enumerated", and every
 * hand-written copy was another chance to collapse a distinction. The same
 * hazard is live here in two forms: a second `startsWith` spelling can drift
 * from the first, and a plain `.filter(...)` at each site destroys the total
 * count R1.5 needs (see {@link partitionGlueTargetDatabases}).
 * `utils/schema-display.ts` (`LGR-4854b3d-2`) is the sibling precedent.
 *
 * **Scope, stated because this prefix is shared.** `LGR-9932c9b-1` records that
 * `agentic_data_ingestion_` is used by two naming schemes, so the prefix is a
 * **necessary and not a sufficient** condition for a usable target. p66's O2
 * decided deliberately **not** to also exclude pipeline-scaffolding databases
 * (those stamped `Parameters.created_by = "agentic_data_ingestion"` by
 * `create_glue_pipeline/iam_roles.py:727-753`), on four recorded grounds — the
 * first being that it would make the picker **stricter than the pre-flight**,
 * which looks at the prefix and nothing else. Do not add that exclusion here
 * without reopening O2.
 *
 * Nothing in this module reads a query object, renders, or fetches. It is a
 * pure rule plus its copy, which is what lets both components and a property
 * test consume it without a fixture.
 */

/**
 * The prefix the per-pipeline Glue role is scoped to.
 *
 * **This literal exists twice on purpose and is pinned, not trusted.** The
 * Python side is `_GLUE_DATABASE_PREFIX`
 * (`dashboard_api/handlers/resources.py:338`), read by the detect-schema
 * pre-flight. Two literals for one rule is what R1.4 exists to prevent and a
 * comment is not a mechanism, so p66 task 1.4 adds a contract test that
 * **parses both sources** and compares them — `LGR-28b4d75-5` (**template /
 * ratified**): "a test written from the producer's output cannot detect the
 * producer drifting, because the output is its own oracle." Change this and
 * that test fails until the Python side moves too.
 */
export const GLUE_TARGET_DATABASE_PREFIX = 'agentic_data_ingestion_';

/**
 * Machine-readable exclusion reason, deliberately the **same token** the
 * detect-schema pre-flight emits: `_REASON_GLUE_DATABASE_PREFIX`
 * (`resources.py:340`), returned in the `reason` key of its 400
 * (`resources.py:440`).
 *
 * p66's O4 is "one vocabulary, two checks" — the picker's exclusion and the
 * pre-flight are two checks, running at different times against different
 * inputs, and C2 forbids touching the second. Sharing the token is what makes a
 * refusal read as **one rule at both points in the flow** rather than two
 * coincidentally similar ones. This is C6's shape (classify locally, emit a
 * machine-readable `reason`) applied to a client-side surface. Also pinned
 * across the language boundary by task 1.4.
 */
export const GLUE_TARGET_EXCLUSION_REASON = 'glue_catalog_database_prefix';

/** The one field this module needs off a Glue database option. */
export interface GlueTargetDatabaseLike {
  database_name: string;
}

/**
 * R1.1 — is this database name one the Glue Catalog target type can accept?
 *
 * **A bare `startsWith`, matching the pre-flight byte for byte.** The Python
 * rule is `database_name.startswith(_GLUE_DATABASE_PREFIX)`
 * (`_glue_database_prefix_refusal`, `resources.py:420`): no trimming, no case
 * folding, no normalisation. Adding any of those here would make the picker and
 * the pre-flight disagree about a name in the gap between them — the picker
 * would offer ` agentic_data_ingestion_x` or `AGENTIC_DATA_INGESTION_x` and the
 * pre-flight would then refuse it at Detect Schema, which is the *false accept*
 * O4 exists to prevent. If the Python rule ever normalises, change both.
 *
 * `''` is not eligible, which is the honest answer for the empty name
 * `fetchGlueDatabases` produces when a row carries neither `database_name` nor
 * `name` (`use-resources.ts:104`).
 */
export function isEligibleGlueTargetDatabase(name: string): boolean {
  return name.startsWith(GLUE_TARGET_DATABASE_PREFIX);
}

/**
 * R1.1 + R1.5 — the offerable databases **and** how many there were.
 *
 * Returns **both**, because R1.5 requires the empty Database picker to tell
 * "databases exist, none eligible" apart from "no Glue databases in this
 * account at all", and those are two different sentences
 * ({@link GLUE_TARGET_NONE_ELIGIBLE} vs {@link GLUE_TARGET_NO_DATABASES}). A
 * plain `.filter(...)` at each call site yields one number and **destroys the
 * other**: `eligible.length === 0` cannot distinguish the two arms. That is
 * `LGR-4854b3d-1`'s collapse-a-distinction failure in a new shape, which is why
 * the partition is the unit both components consume rather than the predicate.
 *
 * `eligible` preserves the **source order** of `databases` — R1.1 offers "the
 * sublist", and a picker that reordered its options would be offering a
 * different list from the one the endpoint returned. `Array.prototype.filter`
 * is order-preserving, so this is a property of the implementation as written
 * and Property 1 quantifies it rather than assuming it.
 *
 * Generic over the element type so a caller's own option shape survives the
 * partition (both components go on to map `eligible` into Cloudscape options).
 * Design §4.3 names the parameter `GlueDatabaseOption[]`; that interface is
 * local and unexported in `hooks/use-resources.ts:51`, and exporting it would
 * mean editing a hook this spec's §A2 records as **not modified** — so the
 * shape is required structurally instead, which `GlueDatabaseOption` satisfies.
 *
 * `totalCount` is the length of the input, i.e. what the endpoint returned. It
 * is deliberately **not** an account-wide total: O1 puts the filter on the
 * client and leaves `GET /resources/glue/databases` unfiltered (R4.2), which is
 * exactly what makes this number available at all.
 */
export function partitionGlueTargetDatabases<T extends GlueTargetDatabaseLike>(
  databases: readonly T[],
): { eligible: T[]; totalCount: number } {
  return {
    eligible: databases.filter((d) => isEligibleGlueTargetDatabase(d.database_name)),
    totalCount: databases.length,
  };
}

/* ---------- Copy ----------
 *
 * Four constants and one function, consumed by both components so the two
 * surfaces cannot word the same refusal differently.
 *
 * **C10 — no fourth phrasing of the three preconditions.** The comment at
 * `TargetForms.tsx:213-218` ("Change one, change all three") governs three
 * existing statements: the target-type tile copy in `WizardStepTarget.tsx`, the
 * info Alert at `TargetForms.tsx:219-241`, and the pre-flight's 400 messages.
 * The strings below name **one** of the three rules — the prefix — and reuse the
 * clause the Alert and the pre-flight already share, verbatim: *"the pipeline's
 * Glue role grants access only to databases and tables under that prefix"*
 * (`TargetForms.tsx:227-229`, `resources.py:434-436`). They do not restate the
 * Alert's other two bullets. Task 1.7 asserts the bullets are not duplicated
 * beneath the note.
 *
 * **Why "points at the Alert" is honoured by omission, not by a locative.**
 * C10's wording is that the sentence points at the Alert rather than repeating
 * it. It cannot do that with words like "see above": the edit modal's
 * `GlueCatalogForm` (`TargetConfigForm.tsx:142-185`) renders two `FormField`s
 * and **nothing else** — the preconditions Alert exists only in the wizard — so
 * a locative reference would be false on one of the two surfaces. Design §4.3
 * resolves it: one string, two contexts, "written to stand alone". In the wizard
 * it sits beneath an Alert that expands on it; in the modal it is complete by
 * itself. (The modal's missing Alert is a recorded, deliberately unfixed
 * asymmetry — §4.3, `LGR-8a4fefa-2`'s shape.)
 *
 * **C8 — the meaning is in the WORDS.** `LGR-99050e6-1` (**template /
 * ratified**): colour alone fails WCAG 1.4.1. Every string below states its
 * fact in prose; none relies on an icon, a colour or the mere absence of
 * options. `LGR-2bcdd0d-3` (**template / ratified**) is the closer analogue and
 * all three of its rejections apply here: it rejected `return null` because "no
 * words at all leaves an empty region indistinguishable from a panel that failed
 * to load" — which is precisely today's defect, since with no `empty` prop
 * Cloudscape's `useDropdownStatus` sets no content at all (design §2.2);
 * rejected a Retry button as "a false affordance" for a condition re-requesting
 * cannot fix; and rejected echoing the server's own sentence, which names
 * internal ids. Its scope is `DataPreviewTable.tsx`'s 404 branch rather than a
 * dropdown, so it transfers rather than binds — the relationship C5 has to
 * `LGR-9932c9b-1`.
 */

/**
 * R1.2 — the rule that excluded the names not on the list.
 *
 * Rendered as the Database `FormField`'s `description`, so it is present
 * **whenever the filter is active** rather than only when everything was
 * excluded. R1.2 requires the subform to state the rule wherever a database is
 * excluded, and an empty-state-only note would say nothing in the ordinary case
 * where some databases are eligible and others were silently dropped — which is
 * the silent exclusion C5's reasoning forbids.
 */
export const GLUE_TARGET_EXCLUSION_NOTE =
  `Only databases whose name starts with ${GLUE_TARGET_DATABASE_PREFIX} are listed — ` +
  `the pipeline's Glue role grants access only to databases and tables under that prefix.`;

/**
 * R1.3 / R1.5 — databases exist in this account, and none of them is eligible.
 *
 * Says both halves, because R1.5's whole point is that this is a *different*
 * fact from an account with no Glue databases at all, and an option list of
 * length zero cannot tell them apart.
 *
 * **States no next step, deliberately.** R2.4 requires one for the Table picker
 * and no requirement asks for one here. "Create a database with that prefix" is
 * an action the author could take, but it is unrequested copy on a refusal whose
 * requirement is only to name the rule, and the wizard's Alert already covers
 * what a valid target looks like.
 */
export const GLUE_TARGET_NONE_ELIGIBLE =
  `This account has Glue databases, but none has a name starting with ` +
  `${GLUE_TARGET_DATABASE_PREFIX} — the pipeline's Glue role grants access only to ` +
  `databases and tables under that prefix.`;

/**
 * R1.5, the other arm — the account holds no Glue databases at all.
 *
 * Asserts nothing about the prefix, because the prefix excluded nothing here:
 * there was nothing to exclude. Saying "none matches the prefix" for an empty
 * account would be a cause this state did not check, which is R2.5's rule for
 * the sibling picker and the same honesty either way.
 */
export const GLUE_TARGET_NO_DATABASES = 'No Glue databases were found in this account.';

/**
 * R2.4 — what the author can do next when the selected database holds no
 * tables, given that C1 forbids the pipeline creating one.
 *
 * **Offers no control, and that is the requirement rather than an omission.**
 * C1 (`LGR-2682c7f-1`, **template / ratified**, four recorded grounds) means
 * this pipeline will never create the catalog table, so a "Create table" button
 * here would be a control the system cannot honour — the false affordance
 * `LGR-2bcdd0d-3` rejected when it turned down a Retry button for a condition
 * re-requesting cannot fix. Task 1.7 asserts no create-table control is
 * rendered.
 *
 * The first clause is the Alert's own opening sentence
 * (`TargetForms.tsx:221`), and "register the table … and select it again" is the
 * pre-flight's own remedy (`resources.py:438-439`) — one vocabulary (O4), not a
 * fourth phrasing (C10).
 */
export const GLUE_TARGET_NO_TABLES_NEXT_STEP =
  'The pipeline does not create catalog tables. Register the table in the Glue Data ' +
  'Catalog first, then select it here.';

/**
 * R2.1 — the Table picker's finished-and-empty state, **naming the selected
 * database**.
 *
 * A generic "No tables found" is what `LGR-2bcdd0d-3` rejected — one sentence
 * standing in for several distinct meanings — and R2.1 requires the empty state
 * to name the database, so the name is embedded rather than left to the reader
 * to infer from the control above.
 *
 * **Makes the positive claim, and is entitled to.** "contains no tables" is a
 * statement about the database, not about the request. R2.5 forbids that claim
 * when an enumeration was truncated or bounded; p66's O5 introduces **no
 * bound** — `_paginated_items` (`handlers/schemas.py:345-361`) iterates every
 * page and has no page cap and no deadline — so after task 2.1 the enumeration
 * is complete and the claim is honest (design §4.7). A structural guard test
 * (task 2.5) fails loudly if a bound is ever added, which is what keeps this
 * sentence true rather than hoping it stays true.
 *
 * Pairs with {@link GLUE_TARGET_NO_TABLES_NEXT_STEP}: this states the fact, that
 * states the next step.
 */
export function glueTargetNoTablesEmpty(database: string): string {
  return `The database ${database} contains no tables.`;
}
