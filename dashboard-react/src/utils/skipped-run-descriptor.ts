// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The descriptor a Run_List_Surface carries into the run-detail navigation for a
 * skipped run (p54 R6.1 / R6.2, design 4.6 — arm **B2**).
 *
 * **Why anything is carried at all.** A skip row has no state-table rows —
 * verified rather than inherited at `pipelines_runs.py:2035-2041`: the seven skip
 * attributes occur on `skipped-` rows and only there, and "none of the 3 live
 * `skipped-` run ids appears in the state table". So `get_run_details` answers
 * 404 for a skip's run id, `fetchRunDetail` maps 404 to `null`
 * (`use-run-detail.ts:29-30`), and the run-detail page renders *"No run with id …
 * exists."* — a denial of existence about a row the list the operator clicked
 * from had just read to them. The obvious backend repair is fenced: R6.3 / R8.2,
 * p38 do-NOT 6 and p38 R1.4 forbid a tolerant 200, which "would convert a
 * diagnosable 404 into a run that appears to exist and is empty". Arm B2
 * therefore has the frontend recognise the 404 **beside the row it already
 * holds**, which is `LGR-2bcdd0d-3`'s ratified precedent one surface over: the
 * data-preview 404 on this same skip row renders its own worded statement,
 * because "a 404 … means 'there is nothing to preview here', not 'the read
 * failed'". Nothing is re-read here and no fence is moved.
 *
 * **This module is the one place the descriptor's shape is spelled.** Both run
 * lists build it and task 5.2's run-detail branch reads it — three sites for one
 * payload, which is the arrangement `LGR-4854b3d-1` (**template / ratified**)
 * settled by ruling that a payload expression cannot be rewritten per display
 * site, and that `LGR-e59edd7-6` then applied to two surfaces which had *already*
 * diverged on `mapping_count` (one `?? '\u2014'`, one `?? 0`). R7 in this same
 * spec is that drift class between these same two run lists, so a second
 * spelling here is the defect this file exists to prevent.
 * `utils/approval-mapping-count.ts` and `utils/approval-reused-mapping.ts` are
 * the in-tree precedents for the shape.
 *
 * **Skip-ness is `status` + `skip_reason`, never the run id — do-NOT 4.** Two id
 * shapes exist: `skipped-{fingerprint[:32]}` for duplicate content
 * (`pipeline_trigger._duplicate_skip_record_id`) and `run-{window}-skip-{reason}`
 * for concurrency and lock contention (`_contention_skip_record_id`), the latter
 * `run-`-prefixed **deliberately** (`LGR-21a1b5b-4`). `pipeline_trigger:373`
 * states the discriminator on the writer side because the contention id "must not
 * be mistaken for an execution", and `use-executions.ts:69` states it on the
 * reader side. So the gate below is {@link describeSkip}'s own — one authority
 * for "is this a skip", shared with the badge R7 put on both surfaces, so a row
 * cannot be badged as a skip on the list and refused as one on the detail page.
 *
 * **What is deliberately NOT here: the reader.** Task 5.2 owns narrowing
 * `useLocation().state` and the branch that renders from it. Shipping the
 * narrowing ahead of its only caller would be the shape `LGR-eee8ac8-6` warns
 * about — a key with no reader, "read as enforcement nothing performs".
 */

import { describeSkip } from './skip-reason';

/**
 * What a Run_List_Surface hands the run-detail page about a skipped run.
 *
 * Every field except `status` is optional and **omitted when the row does not
 * carry it**, rather than emitted blank: absence stays absence
 * (`LGR-44d601e-1` clears a value by removing the key so no reader must
 * distinguish `""` from absence; do-NOT 7 is the general form). The two writers
 * differ in what they record and that difference is real, not a gap to paper
 * over:
 *
 * - a **duplicate** skip (`pipeline_trigger:225-244`) carries `file_name`,
 *   `previous_run_id`, and a `skip_detail` that already names the previous run in
 *   a full sentence;
 * - a **contention** skip (`:488-505`) carries no `previous_run_id` at all —
 *   nothing was duplicated — and `file_name` only where the trigger had a
 *   `source_key`.
 *
 * All of them reach the client: `get_pipeline_executions` publishes each
 * non-`QUEUED` row whole (`pipelines_runs.py:757` splits, `:1356` returns the
 * list unprojected), so these are the row's own bytes and not a second
 * derivation.
 */
export interface SkippedRunDescriptor {
  /** The row's own `status`, trimmed. Task 5.2 upper-cases it to discriminate. */
  status: string;
  skip_reason?: string;
  skip_detail?: string;
  previous_run_id?: string;
  file_name?: string;
}

/**
 * The router state both surfaces attach. The key literal `skippedRun` is spelled
 * here and in {@link buildSkippedRunNavState}'s return, and `tsc` binds the two
 * — so task 5.2 reads a name that cannot silently drift from the one written.
 */
export interface SkippedRunNavState {
  skippedRun: SkippedRunDescriptor;
}

/**
 * The subset of a run-list row this reads.
 *
 * Deliberately all-optional and `unknown`-valued so **both** surfaces satisfy it
 * with no cast and no type change: `ExecutionsPage` holds a typed
 * `PipelineExecution`, `PipelineOverviewPage` holds `Record<string, unknown>`
 * (`PipelineOverviewPage.tsx:453`), and both are assignable to this.
 *
 * `PipelineExecution` does **not** declare `previous_run_id`, and must not gain
 * it for this — the list side stays unchanged, which is 5.1's stated constraint.
 * The attribute is nonetheless on the wire (see {@link SkippedRunDescriptor}),
 * so an optional `unknown` field reads it at runtime without asserting it onto
 * the row type. That is also why the fields are named in the row's snake_case
 * rather than camelCase: they are read straight off the published row.
 */
export interface SkippedRunRow {
  status?: unknown;
  skip_reason?: unknown;
  skip_detail?: unknown;
  previous_run_id?: unknown;
  file_name?: unknown;
}

/** A non-blank string, or nothing. Anything else on the wire is not a value. */
function text(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed === '' ? undefined : trimmed;
}

/**
 * The router state for a skip row, or `undefined` for every other row.
 *
 * `undefined` is the normal answer — this is called on every rendered row of both
 * run lists, and `navigate(path, { state: undefined })` is exactly today's
 * stateless navigation. Only a skip carries anything.
 *
 * The gate is `describeSkip(...) !== null`, i.e. the row's `status` upper-cases to
 * `SKIPPED` — {@link describeSkip}'s own discriminator (`skip-reason.ts:93`),
 * reused rather than restated so the list cannot attach a descriptor the detail
 * page would reject or withhold one it would accept. **Never the run id**
 * (do-NOT 4).
 */
export function buildSkippedRunNavState(row: SkippedRunRow): SkippedRunNavState | undefined {
  const status = text(row.status);
  const skipReason = text(row.skip_reason);
  const skipDetail = text(row.skip_detail);
  if (status === undefined || describeSkip(status, skipReason, skipDetail) === null) {
    return undefined;
  }

  const descriptor: SkippedRunDescriptor = { status };
  const previousRunId = text(row.previous_run_id);
  const fileName = text(row.file_name);
  if (skipReason !== undefined) descriptor.skip_reason = skipReason;
  if (skipDetail !== undefined) descriptor.skip_detail = skipDetail;
  if (previousRunId !== undefined) descriptor.previous_run_id = previousRunId;
  if (fileName !== undefined) descriptor.file_name = fileName;
  return { skippedRun: descriptor };
}
