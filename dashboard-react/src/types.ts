// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Shared TypeScript types for the AgenticDataIngestion dashboard. */

/**
 * p45 — `UnsourcedTargetField` is **declared in `utils/unsourced-target-fields.ts`
 * and imported here**, not re-declared and not re-exported.
 *
 * That module is the one place the Approval_Payload's `unmapped_fields` is read,
 * and its own doc comment records why the tolerance for the wire's two element
 * shapes lives in exactly one function. A second declaration of the shape — even
 * a structurally identical one — would be the second definition
 * `LGR-28b4d75-5` (**template / ratified**) rejects, so consumers import the type
 * from the module that owns it rather than from here.
 */
import type { UnsourcedTargetField } from './utils/unsourced-target-fields';
/**
 * p55 R8.4 — imported, not spelled again, for the same reason as
 * `UnsourcedTargetField` above: `utils/worksheet-selection.ts` owns this key and
 * is the end of a one-hop agreement with `shared/pipeline_record_read.py`
 * (`LGR-eee8ac8-5`). A `value` import rather than `import type`, because it is
 * used as a computed property name below.
 */
import { SELECTED_WORKSHEET_REQUEST_KEY } from './utils/worksheet-selection';

export interface Pipeline {
  pipeline_key: string;
  pipeline_name: string;
  status: string;
  source_schema: string;
  target_schema: string;
  created_at: string;
  updated_at: string;
  model_id?: string;
  target_type?: string;
  versions?: PipelineVersion[];
  schema_history?: SchemaVersion[];
  schema_version?: number;
  mapping_count?: number;
  has_mapping?: boolean;
  ai_model?: string;
  file_count?: number;
  all_files?: PipelineFile[];
  target_config?: TargetConfig;
  pipeline_description?: string;
  mapping_context?: string;
  mapping_id?: string;
  /**
   * The Approval_Config as `get_pipeline_details` reports it — projected on **both**
   * response arms by membership rather than truthiness (`LGR-7bea749-3`), and passed
   * through `normalizePipeline` whole (`LGR-7bea749-4`).
   *
   * **All four keys, not the two this type used to declare.** The missing pair was
   * the type-level half of the subset divergence: `PipelineFormModal` could not read
   * `always_approve_schema_changes` off a `Pipeline` even if it wanted to, so the
   * two-control edit form and this two-key type kept each other looking correct.
   *
   * Read it through `hydrateApprovalConfig` (`pages/CreatePipelinePage.tsx`), never
   * key by key:
   *
   * - `always_approve_schema_changes` absent means the gate is **ON**.
   *   `schema_mapping/lambda_function.py:1657` reads
   *   `approval_config.get("always_approve_schema_changes", True)`, so `?? false`
   *   here turns a live governance gate off on the operator's first save.
   * - `schema_mapping_confidence_threshold` is a **fraction** (its only writer,
   *   `_default_approval_config`, seeds `Decimal("0.85")`), while every control that
   *   displays it is a percent. It also has **no reader**, which is why no editor
   *   offers a control for it — see `buildApprovalConfigRequest`'s
   *   `includeConfidenceThreshold`.
   *
   * Typed concretely rather than `unknown` for `LGR-7bea749-5`'s reason: the
   * tolerance for a record predating the boundary's validation belongs in the
   * reader's own input type (`PersistedApprovalConfig`), not on every consumer here.
   */
  approval_config?: {
    schema_mapping_enabled?: boolean;
    schema_mapping_confidence_threshold?: number;
    code_generation_enabled?: boolean;
    always_approve_schema_changes?: boolean;
  };
  /**
   * p64 R7.4 — the Quality_Config as `get_pipeline_details` reports it, a peer
   * of `approval_config` rather than a key inside `target_config`.
   *
   * **`data_quality_enabled` is optional and its absence is a third state, not
   * a missing value.** Absent means "this pipeline has no opinion, use the
   * template's own default" — on for `glue_job_s3_tables.py.j2`, off for the
   * other four — which is a state the record genuinely holds:
   * `pipelines_crud._default_quality_config` seeds `dedup_enabled` and
   * `dedup_columns` and deliberately seeds **no** value for this key. Read it
   * through `readDataQualityEnabled` (`utils/quality-config.ts`), never as
   * `?? false`: collapsing absence into `false` and re-submitting writes an
   * explicit opt-OUT, which drops the row-level split and quarantine write an
   * S3 Tables pipeline has had since P28 and moves its verdict from `enforced`
   * to `advisory` with nobody having asked.
   *
   * The sub-object's own absence is likewise distinct from `{}`. `undefined`
   * means the record carries no `quality_config`; `{}` means it carries one
   * recording no overrides. `normalizePipeline` preserves both — see the
   * comment on the passthrough there before adding a default anywhere.
   */
  quality_config?: {
    data_quality_enabled?: boolean;
    dedup_enabled?: boolean;
    dedup_columns?: string[];
  };
  // P17 — source abstraction. Optional fields surfaced by
  // get_pipeline_details for both pre-P17 (defaulted to S3_UPLOAD) and
  // P17+ records. ``schedule`` carries the EventBridge cron(...) or
  // rate(...) expression when configured.
  source_type?: 'S3_UPLOAD' | 'JDBC' | string;
  source_config?: Record<string, string>;
  schedule?: string;
  /**
   * p55 R8.4 — the Selected_Worksheet the pipeline is configured with, as
   * `get_pipeline_details` reports it.
   *
   * **`undefined` and `''` are different facts here and neither may be
   * collapsed into the other.** `undefined` means the record carries no
   * `selected_worksheet` attribute — no human has chosen, so a single-sheet
   * workbook is ingested from its one sheet (R8.1) and a multi-sheet one
   * refuses and names every sheet present (R8.4). `''` means the attribute is
   * present and empty — a *recorded* selection naming no sheet, which refuses
   * under R8.5. The producer mirrors the record's membership rather than its
   * truthiness for exactly this reason, so a reader that writes
   * `worksheet || 'none'` throws away the distinction the wire went to
   * trouble to carry. Use `worksheetSelectionIsSet` for "is one set".
   *
   * Keyed off `SELECTED_WORKSHEET_REQUEST_KEY` rather than spelled again, for
   * `LGR-eee8ac8-5`'s reason: this name has to agree with
   * `SELECTED_WORKSHEET_ATTRIBUTE` in `shared/pipeline_record_read.py`, and a
   * disagreement would not fail — it would silently read nothing.
   */
  [SELECTED_WORKSHEET_REQUEST_KEY]?: string;
}

export interface PipelineVersion {
  version_index: number;
  status: string;
  created_at: string;
  code_version_id?: string;
}

export interface PaginatedPipelines {
  pipelines: Pipeline[];
  total_count: number;
  page: number;
  page_size: number;
  total_pages: number;
  has_more: boolean;
}

export interface PipelineFilters {
  page?: number;
  pageSize?: number;
  search?: string;
  status?: string;
}

/**
 * One attempt of a run that a retry displaced (p38 R2.1, R2.4).
 *
 * The wire shape of the node `retry_run` appends to the executions row's
 * `attempts` list. Only `attempt` and `failed_step` are always written; every
 * other field is copied off the row **only when truthy**, so each of them can
 * be absent from a node. A renderer must tolerate every one of those absences
 * without printing a placeholder — `LGR-99050e6-2` (**template / ratified**):
 * absent is reported as unknown, never defaulted.
 */
export interface RunAttempt {
  /**
   * 1-based attempt number, strictly increasing across retries. Derived from
   * the row's `attempt_count` rather than from the list length, so it keeps
   * increasing past the five-node cap.
   */
  attempt: number;
  /** The step this attempt was retried from — where it stopped. May be `''`. */
  failed_step: string;
  /** The run status this attempt had recorded before the retry overwrote it. */
  status?: string;
  /** This attempt's per-step statuses, keyed by step name. */
  steps_status?: Record<string, string>;
  started_at?: string;
  completed_at?: string;
  execution_name?: string;
}

export interface RunDetail {
  run_id: string;
  pipeline_key: string;
  status: string;
  started_at: string;
  completed_at?: string;
  steps?: StepDetail[];
  approvals?: Approval[];
  trigger?: string;
  retry_source_run_id?: string;
  /**
   * The attempts this run has had before the current one, oldest first
   * (p38 R2.1, R5.1). Snapshotted onto the run's executions row by the retry
   * endpoint, capped at five, and read back by `get_run_details`.
   *
   * **Three states, and they are not the same thing.** Absent means the
   * lineage read did not complete — the producer sets all three lineage keys
   * inside one `try`, so a raised read leaves them all off and the response is
   * still a 200. Present-and-empty means the row WAS read and carries no
   * attempt attribution, which is knowledge. Populated means full
   * attribution. `LGR-99050e6-2` (**template / ratified**) governs: an absent
   * value is reported as unknown, never defaulted to zero.
   *
   * Present-and-empty is also the state of **every** retried run that predates
   * p38, because the pre-p38 retry endpoint wrote no attempt record at all
   * (p38 §5 delta D2). That is the common case for this field, not the edge.
   */
  attempts?: RunAttempt[];
  /**
   * How many attempts this run has had in total, including the current one.
   * Absent or `0` means unknown — see `attempts` above. It is read in
   * preference to `attempts.length` because the list is capped at five and a
   * length-derived count would stop increasing at the cap.
   */
  attempt_count?: number;
  /**
   * What reached the target on this run (p37 R1.8). Attached by
   * `get_run_details` before the synthetic-step filter, from the same pure
   * deriver the run list uses — which is how R1.10 ("the same value on the run
   * list and the run detail page") holds by construction.
   *
   * Optional because a response produced before p37 shipped carries no such
   * field; `LoadOutcomeLabel` renders nothing for its absence, exactly as it
   * does for `not_established`.
   */
  load_outcome?: LoadOutcome;
  /**
   * Which schema version this run ran against (p54 R4.1, R4.3). Derived on the
   * read path by `get_run_details` — there is **no stored attribute** to
   * project, and p54 task 3.2 re-ran that negative search over the whole tree
   * before building the derivation. Same key name and same value the two run
   * lists already read (`PipelineOverviewPage.tsx`, `ExecutionsPage.tsx`),
   * which is what R4.3 asks for.
   *
   * A `number`, not a string: the ordinal is ones-based
   * (`_schema_hash_versions` assigns `len(versions) + 1`, so it cannot be `0`),
   * and both list surfaces already render it as `<Badge>v{…}</Badge>` behind a
   * `!= null` guard.
   *
   * **Optional because absence is the answer, not a gap.** All five ways the
   * version fails to resolve — no `schema_extract` row, no `schema_hash` on it,
   * an unknown hash, a page-capped walk, a deadline-truncated walk — leave the
   * key **off the body**. Never `''`, never `0`, never `null`.
   * `LGR-44d601e-1` clears a value by removing the key "precisely so no reader
   * has to distinguish `''` from absence", and `LGR-7561acb-7` refuses a third
   * spelling of two facts. So `runDetail.schema_version != null` is the whole
   * test; there is no sentinel to screen out.
   */
  schema_version?: number;
  /**
   * Whether this run reused a mapping an earlier run produced (p54 R2.1).
   *
   * **Three states, and `false` is never the unknown one.** `false` means a
   * `schema_mapping` state row IS present — the run generated its own mapping,
   * and that is knowledge. `true` means no such row and an id resolved.
   * **Both keys absent** means the fact is not established, which includes the
   * case where the producer's own read raised: `get_run_details` derives this
   * inside its own `try` and pops both keys on failure, so a failed read leaves
   * them off rather than reporting `false` for a run it could not read.
   * `LGR-8e34f37-3` is that omit-on-failure shape, already governing the three
   * lineage keys above; `LGR-99050e6-2` is the underlying rule — an absent
   * value is reported as unknown, never defaulted.
   *
   * Optional rather than `boolean | null` for the same reason `attempt_count`
   * is: absence is this file's spelling for not-established, and a third
   * spelling would make every reader distinguish two shapes of one fact.
   */
  reused_mapping?: boolean;
  /**
   * Which mapping was reused (p54 R2.2). Present **only** alongside
   * `reused_mapping: true` — the pair is exhaustive and mutually exclusive:
   * `false` alone, `true` beside a non-blank id, or neither. There is no fourth
   * combination, and p54 task 3.3's Property 7 asserts it.
   *
   * The id is resolved from the approval record's own
   * `approval_data.reused_mapping_id`, so it is never invented: a run with no
   * resolvable id carries neither key.
   */
  reused_mapping_id?: string;
}

// ── Load_Outcome (p37 R1.2-R1.9) ─────────────────────────────────────────
// The wire shape of `shared/load_outcome.py::derive_load_outcome`, attached by
// the dashboard API to **every** execution row in the run list
// (`pipelines_runs.py:271`, `:281`) and to the run-detail body (`:630`).
//
// One key set in every arm, so a consumer needs no optional-field branching
// beyond the two counts. The deriver is pure and is called from one place per
// surface, which is how p37 R1.10 — "the same value on the run list and the run
// detail page" — holds by construction rather than by discipline.

/**
 * The five outcomes, and they do NOT collapse into three.
 *
 * `empty_source` is deliberately not a subcase of `no_rows_loaded` (R1.3): an
 * empty source file is a legitimate no-op, not a data-loss event, and
 * collapsing them would make every empty scheduled JDBC poll a warning — the
 * cry-wolf failure R1.2 exists to avoid from the other side.
 *
 * `not_established` is deliberately not a subcase of `empty_source` (R1.6): it
 * means the counts were not available, which is a **reachable steady state**
 * rather than a legacy one — the Glue job's counts write is best-effort by
 * necessity, since a telemetry failure must not fail a job that has already
 * written to the target (design §5 risk 3). Reading it as "zero rows" would
 * report data loss on every run whose counts write happened to fail.
 */
export type LoadOutcomeKind =
  | 'complete_load'
  | 'partial_load'
  | 'no_rows_loaded'
  | 'empty_source'
  | 'not_established';

/**
 * Where the two counts came from. The three `load_metrics_row*` values take the
 * shape `derive_quality_verdict` established (`shared/quality_verdict.py:103-105`,
 * `LGR-eee8ac8-6`, template / ratified), so "we looked and there is nothing"
 * stays distinguishable from "we could not look".
 *
 * `dq_report_row_lower_bound` is a **fourth** value and it is not a variant of
 * the other three. Under it `rows_read` is a **lower bound**, taken from the
 * quarantine count on the run's DQ report — not a recorded count — so the true
 * number of rows read can only be higher. It is p37 C5's retroactive path: a run
 * that completed before the `(run_id, "load_metrics")` counts row existed, whose
 * `rows_written` is recoverable from the DQ report's `rows_passed` while its
 * Rows_Read is not (`shared/load_outcome.py`, `FROM_DQ_REPORT_ROW_LOWER_BOUND`).
 * `complete_load` is **structurally unreachable** under it — guarded inside the
 * deriver rather than promised by a caller — so `no_rows_loaded` is the only
 * outcome it can establish.
 *
 * `LGR-9e76249-8` (**template / ratified**) is the record for it being its own
 * value rather than being folded into `load_metrics_row_incomplete`, on
 * `LGR-78ce0b7-8`'s ground that a missing record and a record that establishes
 * nothing must not collapse into one value: a caller reading
 * `load_metrics_row_incomplete` could not tell that Rows_Read here is a lower
 * bound rather than absent.
 *
 * A reader that states `rows_read` as exact under this provenance understates
 * it. `describeCounts` in `LoadOutcomeLabel.tsx` is the branch that does not.
 */
export type LoadOutcomeProvenance =
  | 'load_metrics_row'
  | 'load_metrics_row_absent'
  | 'load_metrics_row_incomplete'
  | 'dq_report_row_lower_bound';

export interface LoadOutcome {
  outcome: LoadOutcomeKind;
  /** Rows read from the source. `null` **only** on `not_established`. */
  rows_read: number | null;
  /**
   * Rows the job presented to the target writer — note the wording; the state
   * row carries its own `write_confirmed` stamp. `null` **only** on
   * `not_established`.
   */
  rows_written: number | null;
  /**
   * Always present, never negative. Computed by the backend — do **not**
   * recompute it in the UI from the two counts above.
   */
  rows_not_written: number;
  /**
   * Always `false`. A Load_Outcome describes a run; it never re-decides one
   * (R1.12). Typed as the literal so a caller cannot branch on it as if it
   * might one day be `true`.
   */
  blocks_run: false;
  /** Which fact, or which relationship, was unavailable. `[]` when established. */
  missing: string[];
  determined_from: LoadOutcomeProvenance;
  /** One sentence, dashboard-showable, from the deriver. */
  reason: string;
}

export interface RetryResponse {
  run_id: string;
  pipeline_id: string;
  execution_name: string;
  retry_from_step: string;
}

export interface StepDetail {
  step_name: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  error_message?: string;
  duration_ms?: number;
  throughput?: number;
  mcp_latency_ms?: number;
  mcp_operations?: MCPOperation[];
  logs?: string[];
  error_details?: string;
  troubleshooting?: string[];
}

/** Per-field evidence produced by the complex mapping agent. */
export interface EvidenceEntry {
  sample_values?: string[];
  reasoning?: string;
  memory_matches?: number;
  confidence_before?: number;
  confidence_after?: number;
}

/** A question raised by the complex mapping agent for ambiguous fields. */
export interface EvidenceQuestion {
  field: string;
  question: string;
  options: string[];
  sample_values?: string[];
  current_confidence?: number;
  /**
   * When true, an answer is mandatory before the approval can be submitted
   * (R2.4). Absent/false questions are optional and never block approval.
   */
  required?: boolean;
}

/** Typed shape of `Approval.details` when `approval_type === 'schema_mapping'`. */
export interface SchemaMappingApprovalDetails extends Record<string, unknown> {
  /**
   * p50 C2's three-state count — a number, or `null` for **not established**.
   * Nullable because the producer sends it that way:
   * `approval_handler/lambda_function.py:343` derives
   * `len(proposed_mappings) if isinstance(proposed_mappings, list) else None`,
   * so `null` on the wire means the approval payload could not be read, which is
   * a different fact from a mapping with zero entries (R1.5, do-NOT 7). Same
   * declaration shape as `Schema.field_count` and the same governing record,
   * `LGR-4854b3d-1`. Read it through `utils/approval-mapping-count.ts`; do NOT
   * write `?? 0` against it.
   */
  mapping_count?: number | null;
  /**
   * p50 task 5.3 — **the producer's dict, not a count.** This was declared
   * `number` and read `(details.unmapped_fields as number) ?? 0`, and the cast
   * was the defect rather than the render: `create_approval_payload` writes
   * `{source, target, target_unsourced, target_required}`
   * (`schema_mapping/lambda_function.py:1740-1744`). Task 2.1 pointed
   * `get_approval_data` at `output_data.approval`, so the key stopped arriving
   * `null` and started arriving populated — and the review modal died with
   * `Objects are not valid as a React child (found: object with keys {source,
   * target, target_unsourced, target_required})`, measured at the render.
   *
   * {@link UnmappedFieldsPayload} rather than a shape declared here: it is the
   * **named** wire contract, doc-commented against that producer and guarded by
   * a contract test that fails on a rename of either Unsourced_Key (p45 R6.4,
   * R6.5). A second declaration of one shape is what `LGR-7561acb-1`
   * (**template / ratified**) rejects.
   *
   * Nullable because every approval row written before task 2.1 carries a
   * literal `null` here — eight of the observed record's nine keys were `null`
   * (`LGR-86698d4-1`) — and `create_approval_payload`'s degraded return
   * (`:1435-1439`) omits the key entirely. Read it through
   * `utils/approval-unsourced-target-fields.ts`, which routes it to the single
   * ratified reader (`LGR-9e76249-7`, `LGR-7561acb-1`); do NOT read
   * `target_unsourced` or `target_required` directly, and do NOT derive a count
   * from either.
   */
  unmapped_fields?: UnmappedFieldsPayload | null;
  /**
   * p55 R10.1 / R10.2 — the Rename_Manifest, the eleventh key of the
   * `schema_mapping` approval payload.
   *
   * {@link RenameManifestPayload} rather than a shape declared here, for the same
   * reason `unmapped_fields` above names {@link UnmappedFieldsPayload}: a second
   * declaration of one shape is what `LGR-7561acb-1` (**template / ratified**)
   * rejects.
   *
   * Nullable and optional, and both states are reachable — the projection reads
   * the key with `.get`, so a mapping row written before p55, a producer that took
   * `create_approval_payload`'s degraded `except` arm, and an unreadable
   * `output_data.approval` all arrive as present-and-`null`, while an approval row
   * whose `approval_data` predates the eleventh key omits it. Read it through
   * `utils/rename-manifest.ts`, which answers all of those as *not established*
   * and keeps that distinct from an empty `renames` list; do NOT read `renames`
   * directly and do NOT derive a count from it.
   */
  rename_manifest?: RenameManifestPayload | null;
  /**
   * p50 task 5.3 — three-state, and **no producer writes it today.**
   *
   * The corrected `schema_mapping` payload carries {@link confidence_scores},
   * plural, a per-field dict; there is no singular `confidence_score` among the
   * ten keys `get_approval_data` assembles
   * (`approval_handler/lambda_function.py:288-343`). So this key is absent on
   * every real payload, and `(details.confidence_score as number) ?? 0` rendered
   * a confident `Confidence score 0%` — a measured-zero claim about a
   * measurement that was never made. Same collapse as `mapping_count`'s, ruled
   * out by `LGR-4854b3d-1` (**template / ratified**): *"every hand-written
   * `?? 0` collapses null into 'declares nothing'"*.
   *
   * A percentage is rendered only for a finite number; anything else is not
   * established. Do **not** derive one by averaging {@link confidence_scores} —
   * the producer already computes `mapping_statistics.average_confidence`
   * (`schema_mapping/lambda_function.py:1727`), and a second derivation in a
   * second language is what `LGR-28b4d75-1` and `LGR-9e76249-2` (both
   * **template / ratified**) reject. The missing read is recorded as a finding,
   * not fixed here.
   */
  confidence_score?: number | null;
  /**
   * Per-field confidence as the producer actually writes it
   * (`schema_mapping/lambda_function.py:1741`). The `Decimal` carrier p50 R1.6
   * flagged, converted at the serialization boundary (`LGR-86698d4-1`).
   */
  confidence_scores?: Record<string, number> | null;
  /**
   * The mapping list, `unknown[]` because the wire carries **two element
   * shapes** — `{source, target}` and `{source_field, target_field}` — and this
   * is the raw payload rather than the normalised one. The tolerance lives in
   * `ApprovalArtifact.toMappingData` and nowhere else (`LGR-7561acb-1`).
   *
   * **Recorded, not fixed (p50 task 5.3).** `ApprovalsPage.tsx:44` casts this to
   * `Array<{source, target, confidence?}>` and renders its own embedded table
   * from it. Task 2.1 populated the key and the cast is wrong for the shape the
   * producer sends: measured at the render, that table now emits a row with
   * **blank Source and Target cells** and only the confidence — while
   * `MappingViewer`, twenty lines above it in the same modal, renders
   * `signup_date → registration_date  96% (high)` correctly through the tolerant
   * reader. Fixing it is a display decision rather than a cast correction —
   * whether the duplicate table survives at all, and if it does whether the
   * tolerance moves into a shared module or becomes a second tolerant reader
   * `LGR-7561acb-1` forbids — so it is left for a task that can size it.
   */
  proposed_mappings?: unknown[] | null;
  evidence?: Record<string, EvidenceEntry>;
  questions?: EvidenceQuestion[];
  /**
   * p54 task 4.4 — the reused mapping could not be read (R1.4).
   *
   * Set by `_reused_mapping_payload` (`approval_handler/lambda_function.py`) on
   * the **unreadable** state and on that state only, alongside
   * `proposed_mappings: null` and `mapping_count: null`. An unreadable reused
   * mapping is a *named reference to something absent* — `LGR-b2f79ef-14` — not
   * a mapping with no fields, so it must never render as an empty mapping
   * (do-NOT 7). Read it through `utils/approval-reused-mapping.ts`, which also
   * classifies the resolved-but-non-list `proposed_mappings` route task 2.3
   * found; do **not** test this key alone at a display site.
   *
   * Optional, because absence is a state: a non-reuse approval and a resolved
   * one both omit it, and a `false` written here would be a claim the producer
   * never makes.
   */
  mapping_unreadable?: boolean;
  /**
   * p54 task 4.4 — the run reused a mapping an earlier run produced (R2.1).
   *
   * Present as `true` on **both** the resolved and the unreadable state, absent
   * on the *no reuse* state. Optional rather than `boolean` defaulting to
   * `false` for the same reason `RunDetail.reused_mapping` is (task 4.1): `false`
   * would assert the run generated its own mapping, and absence means not
   * established (do-NOT 7, `LGR-44d601e-1`, `LGR-7561acb-7`).
   *
   * **This is not a narrowing of `Approval.details`**, which stays
   * `Record<string, unknown>` and is narrowed at the point of
   * use — `LGR-69ca78b-4`, citing `LGR-99050e6-5`.
   */
  reused_mapping?: boolean;
  /**
   * p54 task 4.4 — which mapping was reused (R2.2).
   *
   * The `mapping_id` the reuse Pass carried, present on both the resolved and
   * the unreadable state. Optional and never blank-as-a-value: a blank id is
   * indistinguishable from an absent one to a reader, so
   * `utils/approval-reused-mapping.ts` reports both as not established rather
   * than rendering an empty string beside the label.
   */
  reused_mapping_id?: string;
}

/**
 * Typed shape of `Approval.details` when `approval_type === 'first_run'`.
 *
 * p50 task 5.3. There was no type for this payload, and `ApprovalsPage`'s
 * `first_run` branch cast three keys by hand — one of them wrongly. Written
 * against the assembler at `approval_handler/lambda_function.py:442-590`, not
 * against what the surface happened to render.
 *
 * Named at the point of use, like {@link SchemaMappingApprovalDetails}:
 * `Approval.details` stays `Record<string, unknown>` (`LGR-69ca78b-4`,
 * **template / ratified**).
 */
export interface FirstRunApprovalDetails extends Record<string, unknown> {
  /**
   * **A list of human-readable lines, not a string.** `(details.quality_checks
   * as string) ?? ''` was the defect: p50 task 5.1 pointed the read at
   * `output_data.approval.pre_run_validation.quality_checks`
   * (`create_quality_checks/lambda_function.py:307-312`), which is a
   * four-element list, and React renders an array of strings **concatenated with
   * no separators**. Measured at the render before task 5.3:
   * `"5 quality rules configuredData type validations enabledNull value checks
   * configuredCompleteness validation active"`.
   *
   * Only the **first** line is derived from anything — `f"{total_rules} quality
   * rules configured"`. The other three are unconditional string literals, so a
   * run with zero rules still tells its approver "Null value checks
   * configured". That is a producer-side defect, recorded by task 5.1 at the
   * read and deliberately not fixed there or here; nothing on this path may
   * imply the three literals were measured.
   *
   * Nullable: `pre_run_validation.quality_checks` is absent from every approval
   * row written before task 5.1.
   */
  quality_checks?: string[] | null;
  /**
   * The generated DQDL rule list, from the payload's
   * `quality_check_configuration.quality_rules`. Carries `Decimal`-converted
   * numerics on the wire (`approval_handler` `:551-560`).
   */
  data_quality_rules?: unknown[] | null;
  /** p30 R7.2's Quality_Verdict mode. Read correctly before p50 and unchanged — R4.3, do-NOT 5. */
  quality_verdict?: Record<string, unknown> | null;
  /**
   * **`null` on every payload today, and correcting that is not a cast change.**
   * `get_approval_data` reads it off the `create_infrastructure` row's top level
   * (`approval_handler/lambda_function.py:487`), where nothing writes it — one of
   * three wrong-level reads that branch records and deliberately leaves alone.
   * `create_infrastructure` writes it at `output_data.infrastructure_status`
   * (`:958`) **as a dict**, so re-pointing the read would put an object where
   * this surface renders a string. Declared `string | null` because that is what
   * the wire carries now; a task that re-points the read owns widening it.
   */
  infrastructure_status?: string | null;
  /** No producer. `get_approval_data`'s `first_run` branch never assembles this key. */
  estimated_costs?: string | null;
}

export interface Approval {
  approval_id: string;
  pipeline_key: string;
  run_id?: string;
  approval_type: string;
  status: string;
  created_at: string;
  decided_at?: string;
  decided_by?: string;
  /**
   * p50 R2.2 — the human-meaningful half of the deciding identity, projected
   * beside `decided_by` by `pipelines_runs.py`'s run-detail read. Optional and
   * often absent: no approvals row written before p50 task 3.1 carries an email,
   * and 3.1 omits the stored attribute when none resolved rather than writing it
   * blank (`LGR-c58f276-2`). `decided_by` keeps the stable Cognito `sub`, so a
   * consumer prefers this and falls back to that — never to blank (R2.3).
   */
  decided_by_email?: string;
  comment?: string;
  details?: Record<string, unknown>;
  gate_name?: string;
  response_time_ms?: number;
}

/**
 * How the per-approval endpoint's read of the stored `approval_data` ended.
 *
 * Three states, because reading that attribute can end three ways and each says
 * something different to an approver:
 *
 * | value | meaning |
 * |---|---|
 * | `'parsed'` | the attribute decoded to an object — `details` carries it, and may legitimately be `{}` or all-`null` |
 * | `'absent'` | no attribute was stored; the **producer** recorded nothing |
 * | `'unreadable'` | something was stored and could not be interpreted |
 *
 * The last two are the pair that must not merge. `LGR-4854b3d-1` (**template /
 * ratified**) ruled this three-state shape for `field_count` and refused to
 * collapse `null` onto `0`, because "every hand-written `?? 0` collapses null
 * into 'declares nothing'"; sending `{}` for an unreadable payload would be that
 * collapse for the whole payload — it reads as *the producer stored nothing* when
 * the truth is *we could not read what it stored*. p54 drew the same line for one
 * field with `mapping_unreadable` / `REUSED_MAPPING_UNREADABLE_STATEMENT`, for
 * `LGR-b2f79ef-14`'s reason: a named reference to something absent is not a thing
 * with no fields.
 *
 * **Why a sibling field rather than `details: null`.** Every reader spells its
 * access `approval.details ?? {}` (`ApprovalArtifact.tsx:65`, `:130`, `:515`,
 * `:617`), and `??` catches `null` and `undefined` alike — so a nullable `details`
 * would be indistinguishable from an absent one at all four sites, which is the
 * collapse this field exists to prevent. Leaving `details` shaped exactly as those
 * readers expect is also what lets every one of them be reused rather than
 * re-implemented (`LGR-e59edd7-6`).
 */
export type ApprovalDetailsStatus = 'parsed' | 'absent' | 'unreadable';

/**
 * One approval as `GET /api/approvals/{approval_id}` returns it — an
 * {@link Approval} plus the discriminator saying whether `details` could be
 * established.
 *
 * `details` is **omitted** on the `'absent'` and `'unreadable'` states rather than
 * sent as `{}`, so a consumer that ignores `details_status` cannot read "the
 * producer stored an empty payload" out of a read that failed. Every field reader
 * (`approvalMappingCount`, `approvalUnsourcedTargetFields`,
 * `approvalProposedRenames`, `reusedMappingDisplay`) already answers *not
 * established* for an absent `details`, so those two states render correctly
 * through the existing readers with no new expression — the status is what lets
 * the surface additionally SAY which of the two happened.
 *
 * It does **not** carry `decided_by` / `decided_by_email` / `decided_at` /
 * `comment`: the handler deliberately does not synthesise them, because
 * `get_run_details` already collapses the arm-specific stored names into those
 * four and `LGR-e59edd7-3` / `LGR-e59edd7-4` (both **template / ratified**) make
 * that collapse one authority so two surfaces cannot disagree about a deciding
 * identity. They stay optional here only because `Approval` declares them so.
 */
export interface ApprovalDetail extends Approval {
  details_status: ApprovalDetailsStatus;
}

export interface Schema {
  schema_id: string;
  schema_name: string;
  source_type: string;
  fields: SchemaField[];
  created_at: string;
  updated_at?: string;
  /**
   * p35 R4.8 — the human-chosen Display_Label, when one is stored. The API
   * emits `displayLabel` **absent, not null**, when there is none, so this stays
   * optional: a consumer that falls back on a missing key renders the schema id
   * (which is what R4.8 asks for), while one handed `null` would render nothing.
   */
  display_label?: string;
  /**
   * p35 DD4 — the field count as the *listing* reports it, three-way and NOT
   * collapsible to a number:
   *
   * | value | meaning |
   * |---|---|
   * | a number | the source stated a count (Glue, free from the `get_tables` page) |
   * | `0` | the source stated a count and it is zero — R5.1/R5.2 refuse this schema |
   * | `null` | no count was established — every uploaded and every S3 Tables row |
   *
   * `fields` is resolved lazily from `GET /api/schemas/{id}`, so `fields.length`
   * is `0` for a row whose count was never established. A zero-field guard
   * written against `fields.length` therefore refuses every uploaded and every
   * S3 Tables schema — R5.1's guard broken in the worst direction. Read this
   * field, not `fields.length`.
   */
  field_count?: number | null;
}

/**
 * The **read-back** type for an API-sourced schema — p45 R5.4's carrier.
 *
 * Four declarations in this repository share the name `SchemaField`, and the
 * split is deliberate rather than drift (design §4.3):
 *
 * | declaration | role |
 * |---|---|
 * | **this one** | read-back for API-sourced schemas; written only by `use-schemas.ts:normalizeField:16-55` |
 * | `pages/wizard/types.ts:3` | the wizard's own **draft-state** type, whose `nullable` is non-optional because the field builder always has an answer — the user ticked a box |
 * | `utils/schema-parser.ts:3` | `parseSchemaFile`'s output shape |
 * | `hooks/use-resources.ts:244` `JdbcSchemaField` | the detect-schema API's wire shape |
 *
 * The evidence that the first two are a deliberate split is not inference:
 * `types.ts` and `pages/wizard/types.ts` are **both** in `LGR-4854b3d-1`'s and
 * `LGR-4854b3d-2`'s anchor list — one commit, both files — and that record's
 * subject is precisely which expression may live where. Neither is a stale copy
 * of the other, and p45 widens **only this one**.
 */
export interface SchemaField {
  field_name: string;
  field_type: string;
  /** R1.6 — the schema's **own** declared description (Glue `Comment`, an
   *  uploaded body's `description`, an Iceberg field's `doc`). */
  description?: string;
  /** R2.3 — the Context_Sidecar's annotation, and only that. */
  context?: string;
  /**
   * p45 R5.3 / R5.4 — three-state, and **the third state is absence**:
   *
   * | value | meaning |
   * |---|---|
   * | `false` | the source declared the field **required** |
   * | `true` | the source declared the field **optional** |
   * | **absent** | the source did not say — no requirement can be reported |
   *
   * `nullable` is the negation of required at the producer
   * (`handlers/schemas.py:1394`, `field_name not in required`), so a stated
   * `false` is a stated requirement rather than a missing one.
   *
   * The worked example for carrying a three-state value across this boundary
   * without collapsing it is `normalizeFieldCount` — `use-schemas.ts:57-74`,
   * immediately below the one function that writes this type, ending *"Do NOT
   * fall back to `0` here."* The same discipline applies to a word instead of a
   * number: do **NOT** write `nullable ?? true` at any reader (C12, C4,
   * do-NOT 5, do-NOT 16). It reports *"the source did not say"* as
   * *"optional"*, which is the collapse `LGR-4854b3d-1` (**template /
   * ratified**) exists to prevent and the defect p45 removes.
   *
   * `null` is **not** a third value, here or on the wire (do-NOT 17).
   * `LGR-44d601e-1` (**template / ratified**) clears a stored value by
   * *removing* the key precisely because *"a stored `""` would have to be
   * distinguished from absence at every reader"*. Absence is the spelling;
   * three spellings of two facts is the defect, not the fix.
   *
   * Read it through `utils/field-requirement.ts`'s
   * `fieldRequirementFromNullable`, which is the single expression R5.5 asks
   * for.
   */
  nullable?: boolean;
}

/**
 * One source (or one scope within a source) the Schema_Listing could not
 * enumerate — p35 R1.4 / design §5 risk 10.
 *
 * `get_schemas` narrowed its swallowing `except` clauses, so a permissions or
 * service problem in one source is now *reported* instead of silently producing
 * a short list. Emitted **always, empty list included**: an empty list is the
 * affirmative statement that every source was read to the end.
 */
export interface SchemaSourceFailure {
  source: string;
  scope: string;
  error: string;
}

/** What `GET /api/schemas` returns, both halves. */
export interface SchemaListing {
  schemas: Schema[];
  sourcesFailed: SchemaSourceFailure[];
}

/** The on-the-wire envelope for `GET /api/schemas`, before normalization. */
export interface RawSchemaListingResponse {
  schemas?: unknown[];
  sourcesFailed?: SchemaSourceFailure[];
}

/** One field of DD9's per-id response — five keys, two authorities. */
export interface RawSchemaDetailField {
  name: string;
  type: string;
  /**
   * p45 R5.3 — **optional, because the producer stopped inventing it.**
   *
   * Two `handlers/schemas.py` sites used to answer for a source that had said
   * nothing: the Glue branch hardcoded `"nullable": True` for every column
   * (`:496`) even though `StorageDescriptor.Columns[]` carries no nullability at
   * all, and `_schema_detail` read `field.get("nullable", True)` (`:1569`). Both
   * now **omit the key** when nothing was stated, so three states arrive here:
   * `false` = declared required, `true` = declared optional, **absent** = the
   * source did not say.
   *
   * Omission is this file's established spelling for that, not a p45 invention —
   * the same function's docstring records *"`displayLabel` is **absent, not
   * null**, when no label is stored"* (`handlers/schemas.py:1554-1557`),
   * implemented as a conditional key set at `:1590-1592`. `null` is never
   * emitted for this key (do-NOT 17, `LGR-44d601e-1`).
   *
   * The optionality is guarded across the wire by
   * `tests/contracts/api_responses/test_schema_detail_nullable_contract.py`,
   * which asserts `nullable` is **not** in this interface's required key set —
   * the `declares_null`-shaped guard `LGR-99050e6-5` built *"so a future
   * narrowing fails loudly."*
   *
   * `use-schemas.ts:normalizeField:16-55` is the only reader, and the only place
   * in the frontend where `nullable` can enter (C12). Task 10.1 carried the key
   * there, so this optionality now reaches a reader instead of being dropped.
   */
  nullable?: boolean;
  /** R1.6 — the schema's own declared description. */
  description: string;
  /** R2.3 — the Context_Sidecar's annotation. */
  context: string;
}

/**
 * DD9 — what `GET /api/schemas/{id}` returns: a **bare schema object**, not
 * `{"schemas": [one]}` and not `{"schema": {...}}`.
 *
 * `displayLabel` is absent rather than `null` when no label is stored. There is
 * no `fieldCount` here: the detail carries the field **list**, so its count is
 * `fields.length` and the two cannot disagree.
 */
export interface RawSchemaDetail {
  schemaId: string;
  name: string;
  source: string;
  targetType: string;
  lastModified: string;
  fields: RawSchemaDetailField[];
  displayLabel?: string;
}

/**
 * One pipeline the `409` names — p35 R3.7, R3.9.
 *
 * R3.9 requires the Schemas_Page to *present these pipelines*, not a count, so
 * the user can act on them rather than only being told no. `pipelineName` falls
 * back to the id at the producer (`schemas.py:1975`), so it is always a string.
 */
export interface ReferencingPipeline {
  pipelineId: string;
  pipelineName: string;
}

/**
 * The three ways `DELETE /api/schemas/{id}` refuses, kept as three — p35 R3.6,
 * R3.9 and design §5 risk 6.
 *
 * | status | `reason` | what the user should do |
 * |---|---|---|
 * | `409` | `schema_in_use` | update or archive the pipelines in `referencingPipelines` |
 * | `400` | `catalog_derived_schema` | nothing — the schema is a view of `source`'s catalog object |
 * | `503` | `reference_check_incomplete` | **try again**; `retryable` is `true` and nothing was deleted |
 *
 * The `503` is the branch with no user story behind it, and therefore the one a
 * generic-error collapse loses first — which would put the page back to "always
 * toasts Failed to delete schema" for the one case where the answer is *retry*.
 */
export interface RawSchemaDeleteError {
  error?: string;
  reason?: 'schema_in_use' | 'catalog_derived_schema' | 'reference_check_incomplete' | string;
  schemaId?: string;
  /** Which reference lookups completed. Absent on the `400`, which runs none. */
  checked?: string[];
  referencingPipelines?: ReferencingPipeline[];
  /** `glue_catalog` | `s3_table_bucket` — only on the `400`. */
  source?: string;
  retryable?: boolean;
  details?: string;
}

/**
 * What `PUT /api/schemas/{id}/display-label` returns — p35 R4.9, R4.12.
 *
 * `displayLabel` is **absent** on the cleared response (which instead carries
 * `cleared: true`), exactly as it is absent from the per-id detail when no label
 * is stored. Reading `displayLabel ?? ''` therefore gives the right answer for
 * both without a second branch.
 */
export interface RawDisplayLabelResponse {
  message?: string;
  schemaId?: string;
  displayLabel?: string;
  cleared?: boolean;
}

/**
 * What `POST /api/schemas/upload` returns — p35 R4.1, R4.2.
 *
 * `displayLabelError` is the one field worth a second look: it arrives on a
 * **`200`**, not on a failure. The schema body was stored and is fully usable
 * (R4.2), and only the label write failed — so treating it as an upload failure
 * would make the caller retry an upload that succeeded and produce a second
 * object. The label is recoverable through the rename endpoint.
 */
export interface RawSchemaUploadResponse {
  message?: string;
  schemaId?: string;
  location?: string;
  displayLabel?: string;
  displayLabelError?: string;
}

export interface DashboardStats {
  total_pipelines: number;
  total_files: number;
  schema_versions: number;
  last_activity: string;
}

// --- Schema version and pipeline file types ---

export interface SchemaVersionRun {
  run_id: string;
  timestamp: string;
  file_name?: string;
  code_version_id?: string;
  test_status?: string;
  ai_model_used?: string;
}

export interface SchemaVersion {
  version: number;
  created: string;
  has_mapping: boolean;
  mapping_count: number;
  ai_model_used?: string;
  schema_hash?: string;
  /**
   * Embedded mapping data from the API (not fetched separately) — a **raw wire**
   * field, which is why p45 widens one key to `unknown[]` rather than to the
   * normalised shape.
   */
  mapping_details?: {
    mappings?: FieldMapping[];
    /** Bare names on both producers, and not widened (do-NOT 19). */
    unmapped_source_fields?: string[];
    /**
     * p45 — `unknown[]`, deliberately **not** `UnsourcedTargetField[]`.
     *
     * The wire carries two element shapes here: a bare name, or the producer's
     * `{field, default_value, reason}` object. `string[]` was the narrower of
     * the two claims and it was wrong — `SchemaHistoryPage.tsx:83-88` had to
     * carry its own `typeof f === 'string' ? f : (f as Record<string,
     * unknown>).field` test to survive the object case, which was the evidence
     * that the declaration did not match the wire. Both are fixed together.
     *
     * It is **not** typed as the normalised shape either. `mapping_details` is
     * what the API returns verbatim, so declaring it `UnsourcedTargetField[]`
     * would claim the API had already performed the join
     * `utils/unsourced-target-fields.ts` performs — a claim no producer makes.
     * `unknown[]` states what is actually known: a list, of elements this type
     * does not vouch for. Normalise it through `unsourcedTargetFields` at the
     * point of use.
     *
     * Note that this record is a **mapping record, not an approval payload**, so
     * the Unsourced_Keys are structurally absent from it: they are computed in
     * `create_approval_payload` and stored on the approval, while this carries
     * `unmapped_target_fields` plus `target_schema_hash`. Every entry read from
     * here is therefore `not_established` (R2.6, design §4.8's per-surface
     * table), and that is correct rather than broken.
     */
    unmapped_target_fields?: unknown[];
    [key: string]: unknown;
  };
  mapping_confidence?: number;
  unmapped_source_count?: number;
  unmapped_target_count?: number;
  extracted_schema?: string;
  file_name?: string;
  /** @deprecated Use runs[] instead */
  run_id?: string;
  runs: SchemaVersionRun[];
}

export interface PipelineFile {
  file_key: string;
  pipeline_key: string;
  processed_at: string;
  execution_id?: string;
}

export interface TargetConfig {
  type: 'NEW_S3_TABLE_BUCKET' | 'EXISTING_S3_TABLE_BUCKET' | 'GLUE_CATALOG' | 'RDS_DATABASE' | 'JDBC' | string;
  bucket_name?: string;
  table_name?: string;
  namespace?: string;
  table_bucket_arn?: string;
  database?: string;
  table?: string;
  format?: string;
  write_mode?: string;
  merge_key?: string;
  // JDBC / RDS_DATABASE target fields (P17). Credentials are masked at render
  // time; secret_arn is a reference, not the credential value.
  engine?: string;
  jdbc_url?: string;
  jdbc_driver?: string;
  jdbc_schema?: string;
  jdbc_table?: string;
  database_name?: string;
  secret_arn?: string;
  glue_connection_name?: string;
  _connection_mode?: string;
}

// --- Mapping types ---

export interface FieldMapping {
  source_field: string;
  target_field: string;
  confidence?: number;
  transformation?: string;
  hint?: string;
}

/**
 * `output_data.approval.unmapped_fields`, as the approval payload writes it.
 *
 * **Source of truth: `schema_mapping/lambda_function.py:1391-1397`** —
 * `create_approval_payload`'s `unmapped_fields` dict literal, which writes
 * `source`, `target`, `target_unsourced` and `target_required` together. Renaming
 * either Unsourced_Key on either side fails
 * `tests/contracts/api_responses/test_unsourced_keys_contract.py`, which parses
 * this interface with `helpers.load_interfaces` and the Python dict literal with
 * `ast`, then asserts the two agree (R6.4, R6.5). A rename is precisely the
 * failure that guard exists for: the frontend would silently see `undefined`,
 * fall to R1.4's branch and report "required-ness not established" for every
 * field on every run.
 *
 * **Every key is optional, and that is load-bearing rather than lax.** Three live
 * causes put a payload here with the Unsourced_Keys missing — p37 is not deployed
 * (C15, `backlog.md` item 20), mapping records predate p37 (`backlog.md` item 4),
 * and `create_approval_payload`'s degraded return (`:1435-1439`) carries no
 * `unmapped_fields` at all. The contract test asserts both keys are still
 * declared **optional** for that reason, which is the `declares_null`-shaped
 * anti-vacuity guard `LGR-99050e6-5` built *"so a future narrowing fails
 * loudly."* Do not make either one required to tidy a reader.
 *
 * **`target` is `unknown[]`, like `SchemaVersion.mapping_details`'s twin key and
 * for the same reason:** the wire carries bare names *and* the producer's
 * `{field, default_value, reason}` objects, and this is the raw payload, not the
 * normalised one. `utils/unsourced-target-fields.ts` is the single place that
 * tolerates both and the single place that performs the join (do-NOT 2).
 *
 * **Naming this nested shape is NOT widening `Approval.details`** (C8, do-NOT 11,
 * `LGR-69ca78b-4`, **template / ratified**: *"`Approval.details` was NOT narrowed
 * to tidy the reader; it is narrowed at the point of use"*). `Approval.details`
 * stays `Record<string, unknown>` on `Approval` above, and `ApprovalArtifact.tsx`
 * keeps coercing locally — `detailStr:55-63`, `codeText:66-69`,
 * `toStringList:72-79` and `toMappingData`'s own `:87` all do. This type is
 * applied at that same point of use.
 *
 * `source` is listed for completeness and is **not** widened past `unknown[]`
 * (do-NOT 19): both producers write bare names there, so an object shape would be
 * invented. `unsourcedTargetFields` never reads it.
 */
export interface UnmappedFieldsPayload {
  source?: unknown[];
  target?: unknown[];
  target_unsourced?: string[];
  target_required?: string[];
}

/**
 * One Proposed_Rename on the wire — p55 R10.2.
 *
 * Both names, always. The requirement is that the approver sees the original
 * source column name *and* the proposed target column name, so a shape carrying
 * one of them would not be a proposal they could judge.
 *
 * `original_name` is **attacker-controlled text** carried verbatim (p55 do-NOT 1,
 * do-NOT 17): it is a CSV header, and `shared/identifier_utils.py`'s own docstring
 * states the threat as *"a hostile CSV header must never become code."* Nothing
 * sanitizes it in place on either side of the wire — the sanitized name is the
 * sibling `target_name`, which is a *proposal* about it. Render it as React text
 * content, which escapes; see `utils/rename-manifest.ts` for the display cap and
 * the sinks that are forbidden.
 */
export interface ProposedRenamePayload {
  original_name?: unknown;
  target_name?: unknown;
}

/**
 * The Rename_Manifest as `create_approval_payload` writes it and
 * `approval_handler._schema_mapping_payload` projects it — p55 R10.1, R10.2,
 * design §Data Models.
 *
 * A **named nested wire shape**, applied at the point of use exactly as
 * {@link UnmappedFieldsPayload} is. `Approval.details` stays
 * `Record<string, unknown>` (`LGR-69ca78b-4`, **template / ratified**: *"`Approval
 * .details` was NOT narrowed to tidy the reader; it is narrowed at the point of
 * use"*), and naming this nested payload is not narrowing that container.
 *
 * **Nullable and optional, and both states are reachable rather than defensive.**
 * The projection reads the key with `.get`, so it arrives present-and-`null` for
 * every mapping row written before p55, for a row whose producer took
 * `create_approval_payload`'s degraded `except` arm (which carries three keys and
 * neither this one nor `unmapped_fields`), and for an `output_data.approval` the
 * projection could not read. It is **absent** on an approval row whose
 * `approval_data` predates the projection's eleventh key. Read it through
 * `utils/rename-manifest.ts`, which answers those states as *not established* and
 * distinguishes them from an empty `renames` list — "no renames were proposed" and
 * "this payload says nothing about renames" are different facts, and collapsing
 * them is the defect `LGR-4854b3d-1` (**template / ratified**) records for
 * `field_count` and p50's `mapping_count`.
 *
 * `renames` is `unknown[]` rather than `ProposedRenamePayload[]`: this is the raw
 * payload, and the tolerance for a malformed element lives in one function, the
 * way `LGR-7561acb-1` (**template / ratified**) requires — *"a union hands every
 * one of the four readers the tolerance, which is three more places for the two
 * shapes to diverge."*
 *
 * `approval_id` is `null` until an approver decides (design §Data Models). No
 * frontend surface renders it yet; it is declared because the wire carries it and
 * a reader that omitted it would suggest the producer does too.
 */
export interface RenameManifestPayload {
  manifest_id?: unknown;
  source_schema_hash?: unknown;
  approval_id?: unknown;
  renames?: unknown[];
}

export interface MappingData {
  mappings: FieldMapping[];
  /** Bare names. Both producers write names, so this is **not** widened
   *  (do-NOT 19, design Current state item 6). */
  unmapped_source: string[];
  /**
   * p45 R4.2 — the Unmapped_Entry's **object shape**, not a bare name, so
   * `requirement`, `default_value` and `reason` survive the trip to the
   * component.
   *
   * **Decision: normalise at the boundary; do not widen to a union.** The
   * tolerance for the wire's two element shapes lives in exactly one function,
   * `unsourcedTargetFields` in `utils/unsourced-target-fields.ts`, and one shape
   * travels downstream from it. *Rejected:* `(string | UnmappedEntry)[]`, which
   * would leave three tolerant readers of one shape — `MappingViewer.tsx:184` and
   * `:200` already carry the `typeof field === 'string' ? … :
   * (…).field ?? JSON.stringify` test, and C11 puts a third inside
   * `DragDropMappingEditor`, whose entire subject is field *names*. That is the
   * tolerant-consumer shape `LGR-28b4d75-5` (**template / ratified**) rejects.
   * *Rejected:* a parallel `unmapped_target_entries` beside `unmapped_target:
   * string[]` — a second roster and a second ordering authority (do-NOT 2,
   * do-NOT 13).
   *
   * **This is not the narrowing `LGR-99050e6-5` refused.** That record refused
   * reducing what a frontend type admits so a parser would stop complaining. This
   * *widens* what the type carries and leaves the wire as the authority; the raw
   * wire fields stay `unknown[]` ({@link UnmappedFieldsPayload.target},
   * `SchemaVersion.mapping_details.unmapped_target_fields`).
   *
   * Order is the producer's, as received — `target_unsourced` already puts
   * schema-`required` names first (`schema_mapping/lambda_function.py:1199-1200`),
   * so no consumer re-sorts (R1.5, do-NOT 13).
   *
   * **A widened element must never reach `DragDropMappingEditor`** (C11,
   * do-NOT 15): its `unmappedTarget` prop is `string[]` (`:20`), it spreads the
   * list into a `Set<string>` (`:64-70`) and uses each member as a `Select`
   * option's `label` **and** `value` (`:183-187`), so an object renders
   * `[object Object]` as a selectable target field and `handleSave:105-115` would
   * save that into `FieldMapping.target_field`. Names are extracted at the call
   * site, `MappingEditorSection.tsx:82`.
   */
  unmapped_target: UnsourcedTargetField[];
  source_schema_fields?: string[];
  target_schema_fields?: string[];
  schema_hash: string;
  version_index: number;
  ai_model_used?: string;
  created_at?: string;
}

// --- Code and test types ---

export interface CodeDetail {
  code_version_id: string;
  code: string;
  language: string;
  created_at: string;
}

export interface TestResult {
  test_status: string;
  test_output: string;
  error_details?: string;
  executed_at?: string;
}

// --- MCP operation type ---

export interface MCPOperation {
  operation: string;
  status: string;
  duration_ms?: number;
}

export interface ScriptVersion {
  version: number;
  schema_version?: string;
  timestamp?: string;
  code_version_id?: string;
}

// --- Glue job types ---

export interface GlueJobDetail {
  job_name: string;
  status: string;
  worker_type: string;
  number_of_workers: number;
  timeout: number;
  timeout_minutes?: number;
  script_location: string;
  created_at: string;
  last_modified: string;
  runs: GlueJobRun[];
  configuration: Record<string, string>;
  script_content?: string;
  script_versions?: (string | ScriptVersion)[];
}

export interface GlueJobRun {
  run_id: string;
  status: string;
  started_at: string;
  completed_at?: string;
  duration_seconds?: number;
  error_message?: string;
}

// --- File detail types ---

export interface FileDetail {
  file_key: string;
  pipeline_key: string;
  execution_id?: string;
  timeline: FileTimelineStep[];
  steps: Record<string, FileStepData>;
}

export interface FileTimelineStep {
  step: string;
  status: string;
  timestamp: string;
  duration?: number;
}

export interface FileStepData {
  extracted_schema?: string;
  mapping_result?: string;
}

// --- Metrics types ---

export interface ComprehensiveMetrics {
  performance: PerformanceMetrics;
  stages: Record<string, StageMetrics>;
  infrastructure: InfrastructureStatus;
}

/**
 * p39 R2.6 / R2.7 — what the cost figure was summed from. Present exactly when
 * `monthly_cost` is: there is no basis for a sum that was never taken. It also
 * carries R2.7's statement that nothing here measures spend — the figure is a
 * Glue DPU-minutes estimate with a 10-minute per-run minimum.
 */
export interface MonthlyCostBasis {
  metric: string;
  window_start: string;
  window_end: string;
  runs: number;
  rate_usd_per_dpu_hour: number;
  dpu_minimum_minutes: number;
  note: string;
}

/**
 * p39 R5.4 — the per-status counts `overall_success_rate`'s denominator was
 * built from, so the rate on screen is checkable arithmetic. `null`, not four
 * zeros, when the Step Functions read did not complete: zeros there would
 * assert an empty window nobody observed.
 */
export interface TerminalExecutionCounts {
  succeeded: number;
  failed: number;
  aborted: number;
  timed_out: number;
}

export interface PerformanceMetrics {
  /**
   * Rows Loaded: the rows the Glue job reports writing in the window, where the
   * write was confirmed (the job's `load_metrics` state rows). `null` when no
   * Glue run in the window wrote one. The key keeps its historical name; it used
   * to carry a file-size estimate of accepted records (replaced 2026-10-10).
   *
   * Every `| null` below is the true wire shape, not a tolerance. Do NOT
   * collapse one with `?? 0` at a display site — `LGR-4854b3d-1` (template /
   * ratified) is the fence, and a contract-test guard asserts these unions
   * stay declared so a narrowing fails loudly.
   */
  lines_ingested_24h: number | null;
  /** Inherits R4.9: no numerator, no rate. */
  processing_rate: number | null;
  /** p39 R4.6 — `null` when no execution reached a terminal outcome, rather
   *  than the 100.0 a window with no runs used to report. */
  overall_success_rate: number | null;
  /** NOT nullable, deliberately: an `ExecutionsFailed` sum with no datapoints
   *  is a genuine zero — nothing failed. Since 2026-10-10 the runs rejected at
   *  approval are taken out of it (never below 0); when those could not be
   *  read it is the plain sum and `rejected_at_approval` is `null`. */
  failed_pipelines: number;
  /** 2026-10-10 — runs a reviewer rejected at an approval gate, taken out of
   *  `failed_pipelines` and the Success Rate denominator. `null` when the
   *  failure records could not be read, which is not "nobody rejected
   *  anything" and must not render as 0 (`LGR-4854b3d-1`). */
  rejected_at_approval: number | null;
  /** p39 R4.1 / D1 — `null` when no agent stage published a duration in the
   *  window. `InfrastructureStatus.agentcore.response_time_ms` is the same
   *  fact under a second name and carries the same union. */
  agent_latency_ms: number | null;
  /** p39 R2.4 — `null` when no Glue run reached a terminal state in the
   *  window. The flat-rate $0.50-per-execution fallback is gone. */
  monthly_cost: number | null;
  monthly_cost_basis: MonthlyCostBasis | null;
  terminal_counts: TerminalExecutionCounts | null;
  /** p59 — the reconciliation between `failed_pipelines` and the Stage Metrics
   *  panel. `null` when the failure-record partition could not be read, which is
   *  a different fact from "no run failed outside a stage row" and must not
   *  collapse into it (`LGR-4854b3d-1`). */
  stage_failure_attribution: StageFailureAttribution | null;
  /** p39 R7.6 — tile key -> the measurement behind that tile, so a label and
   *  its source cannot drift apart. */
  measurement: Record<string, string>;
  /** p39 R4.2 — field name -> provenance token, three-valued: the source name
   *  for a measurement, `<source>_absent` for an established nothing,
   *  `<source>_incomplete` for a read that could not be made. */
  determined_from: Record<string, string>;
  /** p39 R4.3 — every fact that was unavailable, by name. Derived by the API
   *  from the payload's own nulls, so the two cannot disagree. */
  missing: string[];
  trends: TrendIndicators;
}

export interface StageMetrics {
  success_count: number;
  error_count: number;
  /** null when the stage records no duration anywhere (rendered "N/A") */
  avg_time_ms: number | null;
}

/**
 * p59 — how the Stage Metrics panel's error counts relate to
 * `PerformanceMetrics.failed_pipelines` above it.
 *
 * `failed_pipelines` counts every failed execution except approval rejections.
 * A stage's `error_count`
 * could only ever see failures a stage row recorded, and most failures do not
 * write one — a Lambda timeout, a `States.Timeout` at an approval gate or a Glue
 * failure is carried solely by the RecordFailure sentinel row. Those are now
 * attributed to the stage their `failed_step` names and counted into that
 * stage's `error_count`; the ones naming a step this page renders no stage for
 * are counted here instead, so they are visible rather than dropped.
 */
export interface StageFailureAttribution {
  /** Failure records read in the window. */
  failure_records: number;
  /** Of those, the ones naming one of the six rendered stages. */
  attributed_to_stage: number;
  /** Of those, the ones that added a NEW error to a stage (post-dedupe). */
  counted_into_stage_errors: number;
  /** Of those, the ones whose stage had already recorded that run as an error. */
  already_counted_by_stage_row: number;
  /** Failure records naming a step no stage on this page renders. */
  unattributed: number;
  /** That total broken down by step name — the operationally useful half. */
  unattributed_by_step: Record<string, number>;
  /** 2026-10-10 — runs a reviewer rejected at an approval gate. Set aside before
   *  everything above: a rejection is a decision, so it is in none of the
   *  failure fields. Mirrors `PerformanceMetrics.rejected_at_approval`. */
  rejected_at_approval: number;
  /** That count by the gate the reviewer rejected at. */
  rejected_by_step: Record<string, number>;
  /** Mirrors `failed_pipelines` (rejections already out); null when the
   *  AWS/States read did not complete. */
  failed_executions: number | null;
  /**
   * The residual, SIGNED and unclamped: failed executions with no failure record
   * (positive), or more records than failed executions (negative, when a run
   * straddles the window bound in one system and not the other). This is what the
   * page says when the two halves cannot be made to add up.
   */
  failed_executions_without_a_record: number | null;
}

/**
 * p39 — three of these figures are three-state, for the same reason the
 * performance tiles are: `agentcore.availability` is `null` when the agent
 * error-rate query established no rate (R4.8, `status` then reads `unknown`);
 * `agentcore.response_time_ms` MIRRORS `PerformanceMetrics.agent_latency_ms`
 * (R4.1 / D1) — one fact under two names, so it inherits that field's absent
 * case; `s3.storage_used_bytes` is `null` when the buckets report no
 * `BucketSizeBytes` datapoint (R1.3) and `s3.requests_per_minute` is always
 * `null`, because request metrics were never enabled on them (R4.7).
 *
 * Each member stays a single-line object literal: the contract-test frontend
 * type reader parses one `field: type;` per line (`helpers.py`), so splitting
 * one across lines breaks the oracle these types are pinned against.
 */
export interface InfrastructureStatus {
  agentcore: { availability: number | null; response_time_ms: number | null; status: HealthStatus };
  dynamodb: { read_capacity_per_hour: number; write_capacity_per_hour: number; status: HealthStatus };
  s3: { storage_used_bytes: number | null; requests_per_minute: number | null; status: HealthStatus };
  lambda: { invocations_per_minute: number; error_rate: number; status: HealthStatus };
}

export type HealthStatus = 'healthy' | 'warning' | 'critical' | 'unknown';

/**
 * p39 R4.11 — four values, not three. `_reported_trend` answers
 * `not_established` when either side of the comparison had no measurement, and
 * has done since the backend's absent cases landed; the union declared three
 * until this change, so a real payload was rejected by the contract schema and
 * mistyped here.
 *
 * `not_established` is NOT a fourth flavour of `stable`. `stable` is a
 * measurement — two windows were compared and came out level. `not_established`
 * is an absence — one of them had nothing to compare. The dashboard renders
 * them with different glyphs and different colour tokens for that reason
 * (`LGR-4854b3d-1`); the polarity mapping for `up` / `down` / `stable` is
 * untouched (`LGR-99050e6-1`).
 */
export interface TrendIndicators {
  ingestion: 'up' | 'down' | 'stable' | 'not_established';
  success: 'up' | 'down' | 'stable' | 'not_established';
  agent_latency: 'up' | 'down' | 'stable' | 'not_established';
  cost: 'up' | 'down' | 'stable' | 'not_established';
}


// --- Unified artifact types ---

export interface ArtifactData {
  artifact_type: string;
  primary_id: string;
  status: 'success' | 'failed' | 'skipped';
  summary: Record<string, unknown>;
  s3_uris: Record<string, string>;
}

export interface RunArtifacts {
  pipeline_id: string;
  run_id: string;
  artifacts: Record<string, ArtifactData>;
  code_content: string | null;
}


// --- WebSocket real-time update types ---

export interface ExecutionUpdate {
  type: 'execution_update';
  pipeline_id: string;
  run_id: string;
  status: string;
  current_step: string;
  updated_at: string;
}

export interface WebSocketMessage {
  action: 'subscribe' | 'unsubscribe';
  pipeline_ids: string[];
}


// --- Data preview types ---

/** Response from GET /api/pipelines/{id}/preview or /api/pipelines/{id}/run/{runId}/preview */
export interface DataPreviewResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  total_rows_returned: number;
  source_file: string;
}

// --- Notification types ---

/** A single notification from the notifications API */
export interface Notification {
  notification_id: string;
  pipeline_id: string;
  message: string;
  notification_type: string;
  created_at: string;
  dashboard_read: string;
  read_at?: string;
}

/** Response from GET /api/notifications */
export interface NotificationsResponse {
  notifications: Notification[];
  count: number;
}

// --- Activity update types ---

/** A single pipeline activity update */
export interface UpdateItem {
  pipeline_id: string;
  run_id: string;
  status: string;
  started_at: string;
  current_step?: string;
  pipeline_name?: string;
}

/** Response from GET /api/updates */
export interface UpdatesResponse {
  updates: UpdateItem[];
  count: number;
}

// --- Schema diff types ---

/** A single field difference between two schema versions */
export interface SchemaDifference {
  field_name: string;
  change_type: 'added' | 'removed' | 'modified' | 'unchanged';
  old_type?: string;
  new_type?: string;
}

// --- Glue job log types ---

/** A single log event from a Glue job run */
export interface GlueLogEvent {
  timestamp: number;
  timestamp_iso: string;
  message: string;
  log_level: string;
  ingestion_time?: number;
  /** Computed alias — use `log_level` from the API, mapped to `level` in the hook */
  level: string;
}

/** Response from GET /api/pipelines/{id}/glue-job/runs/{runId}/logs */
export interface GlueJobLogsResponse {
  logs: GlueLogEvent[];
  log_type: string;
  run_id: string;
  pipeline_id: string;
  job_name: string;
  total_returned: number;
  has_more: boolean;
  next_forward_token?: string;
  job_status: string;
}

// --- Glue job version / rollback types ---

/** A single version entry from the job version history */
export interface JobVersion {
  version: number;
  timestamp: string;
  code_version_id?: string;
}

// --- Mapping memory types ---

/** A single mapping memory entry from AgentCore Memory */
export interface MappingMemoryEntry {
  entry_id: string;
  source_field: string;
  target_field: string;
  confidence: number;
  approved: boolean;
  timestamp: string;
}

/** Response from GET /api/pipelines/{id}/mapping-memory */
export interface MappingMemoryResponse {
  pipeline_id: string;
  entries: MappingMemoryEntry[];
  count: number;
}

/** Response from GET /api/pipelines/{id}/glue-job/versions */
export interface JobVersionHistoryResponse {
  versions: JobVersion[];
  current_version: number;
}

// ── RDS Database Target Types (P14) ──────────────────────────────

/** RDS instance returned by GET /api/resources/rds/instances */
export interface RdsInstance {
  db_instance_identifier: string;
  engine: string;
  engine_version: string;
  endpoint_address: string;
  endpoint_port: number;
  db_name: string;
  db_instance_status: string;
  vpc_id: string;
  /**
   * Subnet IDs from the instance's DBSubnetGroup. The wizard picks
   * one (the first by default) and writes it as
   * `source_config.subnet_id` so the JDBC discovery backend can
   * build PhysicalConnectionRequirements without re-describing.
   */
  subnet_ids?: string[];
  /** Active VPC security group IDs attached to the instance. */
  security_group_ids?: string[];
  /** AZ of the first subnet in the DBSubnetGroup. */
  availability_zone?: string;
}

/** Aurora cluster returned by GET /api/resources/rds/clusters */
export interface RdsCluster {
  db_cluster_identifier: string;
  engine: string;
  engine_version: string;
  endpoint: string;
  port: number;
  database_name: string;
  status: string;
  vpc_id: string;
  subnet_ids?: string[];
  security_group_ids?: string[];
  availability_zone?: string;
}

/** Glue JDBC connection returned by GET /api/resources/rds/glue-connections */
export interface GlueJdbcConnection {
  name: string;
  connection_type: string;
  jdbc_url: string;
  jdbc_driver: string;
  status: string;
}

/** Secrets Manager secret returned by GET /api/resources/rds/secrets */
export interface RdsSecret {
  name: string;
  arn: string;
  description: string;
  last_changed_date: string;
}


// ── VPC / Network Resource Types (P20) ───────────────────────────

/** VPC returned by GET /api/resources/vpc/list */
export interface Vpc {
  vpc_id: string;
  name: string;
  is_default: boolean;
  cidr: string;
}

/** Subnet returned by GET /api/resources/vpc/{vpc_id}/subnets */
export interface Subnet {
  subnet_id: string;
  name: string;
  availability_zone: string;
  cidr: string;
}

/** Security group returned by GET /api/resources/vpc/{vpc_id}/security-groups */
export interface SecurityGroup {
  sg_id: string;
  name: string;
  description: string;
}

/**
 * Generic Secrets Manager entry returned by
 * GET /api/resources/secrets/jdbc-candidates (P20).
 *
 * Distinct from {@link RdsSecret} because the JDBC-candidate endpoint
 * does not include `last_changed_date` — keeps the wire shape smaller
 * for the manual-mode dropdown's usage.
 */
export interface Secret {
  arn: string;
  name: string;
  description: string;
}


// --- Agent trace types (Phase 13a) ---

/** A single model (LLM) call within an agent trace */
export interface AgentModelCall {
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  duration_ms: number;
  prompt_preview: string;
  response_preview: string;
  start_time: number;
  end_time: number;
}

/** A single tool call within an agent trace */
export interface AgentToolCall {
  tool_name: string;
  tool_params: string;
  result_preview: string;
  duration_ms: number;
  status: string;
  start_time: number;
  end_time: number;
}

/** Full trace data for a single agent step */
export interface AgentTraceData {
  model_calls: AgentModelCall[];
  tool_calls: AgentToolCall[];
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  estimated_cost_usd: number;
  start_time: number | null;
  end_time: number | null;
  model_id: string;
  error: string | null;
}

/** Agent trace record for a single pipeline step */
export interface AgentTrace {
  run_id: string;
  step_name: string;
  pipeline_id: string;
  trace: AgentTraceData;
}

/** Response from GET /api/runs/{runId}/agent-trace (all steps) */
export interface AgentTraceListResponse {
  traces: AgentTrace[];
  run_id: string;
}

// ── API resource-endpoint response envelopes (P29 R11.4) ─────────────────────
// Typed responses for the form/dropdown "resource discovery" endpoints backing
// use-resources.ts, MIRRORED from the API contract fixtures in
// tests/contracts/api_responses/ (test_glue_databases_response_contract.py,
// test_vpc_list_contract.py, test_vpc_subnets_contract.py,
// test_vpc_security_groups_contract.py, test_jdbc_candidate_secrets_contract.py).
//
// These endpoints return a `{ <plural>: [...], count? }` envelope; a few legacy
// paths return a bare array, so the fetchers accept `Envelope | Item[]`. The
// `Raw*` item types model the on-the-wire field-name variance the fetchers
// normalize — e.g. the Glue handler returns `name` while the canonical
// frontend field is `database_name` (documented in
// test_glue_databases_response_contract.py), and the S3 Tables / Glue "tables"
// handlers return `name` rather than `table_name` — so the normalizing `.map()`
// callbacks in use-resources.ts stay fully typed with no `any`.

/** Raw Bedrock model item — the API returns camelCase (`modelId`/`modelName`);
 *  older handlers used snake_case. Both are normalized in use-resources.ts. */
export interface RawBedrockModel {
  modelId?: string;
  modelName?: string;
  model_id?: string;
  model_name?: string;
}

/** GET /bedrock-models → { models: [...] } (or a bare array on legacy paths). */
export interface BedrockModelsResponse {
  models?: RawBedrockModel[];
}

/** Raw S3 Tables bucket item — canonical `bucket_name`, legacy `name`. */
export interface RawS3TableBucket {
  bucket_name?: string;
  name?: string;
  arn?: string;
}

/** GET /resources/s3-tables/buckets → { buckets: [...], count }. */
export interface S3TableBucketsResponse {
  buckets?: RawS3TableBucket[];
  count?: number;
}

/** Raw table item shared by the S3 Tables and Glue "tables" endpoints — both
 *  return `name`; the fetchers normalize it to the canonical `table_name`. */
export interface RawTableInfo {
  table_name?: string;
  name?: string;
  namespace?: string;
  database?: string;
}

/** GET /resources/s3-tables/buckets/{bucket}/tables → { tables: [...], count }. */
export interface S3TablesResponse {
  tables?: RawTableInfo[];
  count?: number;
}

/** Raw Glue database item — canonical `database_name`, backend returns `name`
 *  (see test_glue_databases_response_contract.py). */
export interface RawGlueDatabase {
  database_name?: string;
  name?: string;
  description?: string;
}

/** GET /resources/glue/databases → { databases: [...], count }. */
export interface GlueDatabasesResponse {
  databases?: RawGlueDatabase[];
  count?: number;
}

/** GET /resources/glue/databases/{db}/tables → { tables: [...], count }. */
export interface GlueTablesResponse {
  tables?: RawTableInfo[];
  count?: number;
}

/** GET /resources/rds/instances → { instances: [...], count? }. */
export interface RdsInstancesResponse {
  instances?: RdsInstance[];
  count?: number;
}

/** GET /resources/rds/clusters → { clusters: [...], count? }. */
export interface RdsClustersResponse {
  clusters?: RdsCluster[];
  count?: number;
}

/** GET /resources/rds/glue-connections → { connections: [...], count? }. */
export interface RdsGlueConnectionsResponse {
  connections?: GlueJdbcConnection[];
  count?: number;
}

/** GET /resources/rds/secrets → { secrets: [...], count? }. */
export interface RdsSecretsResponse {
  secrets?: RdsSecret[];
  count?: number;
}

/** GET /resources/vpc/list → { vpcs: [...], count } (test_vpc_list_contract.py). */
export interface VpcListResponse {
  vpcs?: Vpc[];
  count?: number;
}

/** GET /resources/vpc/{id}/subnets → { subnets: [...], count }
 *  (test_vpc_subnets_contract.py). */
export interface SubnetsResponse {
  subnets?: Subnet[];
  count?: number;
}

/** GET /resources/vpc/{id}/security-groups → { security_groups: [...], count }
 *  (test_vpc_security_groups_contract.py). */
export interface SecurityGroupsResponse {
  security_groups?: SecurityGroup[];
  count?: number;
}

/** GET /resources/secrets/jdbc-candidates → { secrets: [...], count }
 *  (test_jdbc_candidate_secrets_contract.py). */
export interface JdbcCandidateSecretsResponse {
  secrets?: Secret[];
  count?: number;
}
