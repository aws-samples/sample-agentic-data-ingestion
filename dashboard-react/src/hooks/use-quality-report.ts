// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient, ApiError } from '../services/api-client';

export interface QualityCheck {
  name: string;
  /**
   * `warning` is a real producer value, not a placeholder. TWO non-rule checks
   * report it, both from `_partials/_data_quality.py.j2`:
   *
   *   - `NonPrintableCharacters` (p64 R8.1) — rows containing control
   *     characters, because 2d.1 decided that check FLAGS rather than
   *     quarantines.
   *   - `TypeCastFailures` (p64 R9.1/R9.3) — cells that a type conversion
   *     turned into NULL. Spark's `cast` returns NULL on failure instead of
   *     raising, so an unparseable value is otherwise indistinguishable from a
   *     source that was empty, and the `Completeness` rule counts both the
   *     same way. This entry is the other half of that distinction.
   *
   * `fail` means rows were removed from the frame; neither of these removed
   * any, so the two must stay distinct. There is no fourth value.
   */
  status: 'pass' | 'warning' | 'fail';
  expected?: number;
  /**
   * Nullable, not merely optional. The DQDL rule loop writes
   * `float(_mv) if _mv is not None else None` for a rule that reported no
   * evaluated metric, so `null` is on the wire today. Declared rather than
   * narrowed to `number` — `LGR-99050e6-5` rejected narrowing a frontend type
   * to tidy up a union that the producer really sends.
   */
  actual?: number | null;
  /**
   * Why the check says what it says, in the producer's words. On a DQDL failure
   * it is Glue's own `FailureReason`; on the non-printable check it is the
   * sentence naming the row count and stating the rows were "flagged, not
   * quarantined". That sentence is the only place the report says the rows
   * reached the target, so it is rendered rather than dropped (p64 2d.3) —
   * before this it was on the wire and nothing displayed it.
   *
   * On `TypeCastFailures` it carries the per-column breakdown AND the sentence
   * saying these cells had a source value and are not empty sources. That
   * sentence is the whole of R9.3 on the wire: without it the entry is a bare
   * number sitting beside a `Completeness` outcome with nothing to connect
   * them. Same reason it is rendered rather than dropped.
   */
  failure_reason?: string;
  rate?: number;
  threshold?: number;
  duplicates?: number;
  latest_record?: string;
  sample_nulls?: string[];
  sample_duplicates?: string[];
}

export interface QualityReport {
  pipeline_id: string;
  run_id: string;
  overall_status: 'pass' | 'warning' | 'fail';
  checks: QualityCheck[];
  root_cause: string | null;
  recommendations: string[];

  // ── Producer provenance (p30 R7.3) ──────────────────────────────────────
  // GET /pipelines/_/runs/{runId}/quality-report returns the
  // (run_id, "data_quality") state row verbatim, with no projection
  // (handlers/agents.py::get_quality_report). TWO producers write that one
  // key, and only one of them evaluates anything before the target write:
  //
  //   source: "glue_inline_dq"   — the EvaluateDataQuality block inside the
  //       generated Glue job (glue_job_s3_tables.py.j2:281). Runs BEFORE the
  //       write and removes failing rows from the frame, so they never land.
  //       Present only on an S3_UPLOAD → S3 Tables pipeline with a non-empty
  //       ruleset; it is also the only DQ-bearing template of the five.
  //   agent_type: "data_quality" — the on-demand data quality agent
  //       (data_quality_agent/main.py:914), which runs Athena queries against
  //       the target table, i.e. strictly AFTER the load.
  //
  // The agent's write is a put_item on the same key, so an on-demand check
  // REPLACED an inline report. The on-demand trigger was removed on 2026-10-11,
  // so no new agent row is written, but rows the agent wrote earlier remain in
  // the table. That is why the label must still be derived from the row that
  // is actually on screen rather than from the pipeline's target type: the
  // pipeline config would say "enforced" for a row the agent produced after
  // the fact.
  /** `"glue_inline_dq"` when the Glue job's pre-write evaluation wrote this row. */
  source?: string;
  /** `"data_quality"` when the on-demand agent wrote this row. */
  agent_type?: string;
  /** Rule-level failures (inline producer only). */
  rules_failed?: number;
  rules_total?: number;
  /**
   * Rows removed from the frame and written to quarantine before the target
   * write (inline producer only, added by P28 R11.1). Absent on inline rows
   * written by a script generated before that split — which is NOT the same as
   * zero, so absence must not be reported as "nothing failed".
   */
  rows_quarantined?: number;
  quarantine_path?: string | null;
  /**
   * Rows that passed the inline evaluation and were handed to the target write
   * (inline producer only). The template writes
   * `"rows_passed": transformed_count` (`glue_job_s3_tables.py.j2:307`), so for
   * that one case it IS Rows_Written — `df_transformed` is the Passed split.
   *
   * **It is not a general Rows_Written source and must not be used as one**
   * (p37 R1.11): it exists for one of five template/target combinations, and the
   * four without an `EvaluateDataQuality` block can drop every row in their
   * transform just as effectively. A run's Load_Outcome carries the counts;
   * `LoadOutcomeLabel` reads this only as corroborating detail, and says so when
   * the two disagree.
   *
   * Declared here as a consequence of R1.8 — the run detail page could not state
   * Rows_Written even for the one case the backend already records it.
   */
  rows_passed?: number;
}

interface QualityReportResponse {
  report: QualityReport | null;
}

export const qualityReportKeys = {
  all: ['quality-report'] as const,
  run: (runId: string) => ['quality-report', runId] as const,
};

async function fetchQualityReport(runId: string): Promise<QualityReport | null> {
  try {
    const data = await apiClient.get<QualityReportResponse>(`/pipelines/_/runs/${runId}/quality-report`);
    return data.report ?? null;
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Fetch data quality report for a specific run.
 *  When pollUntilReady is true and the report is null, refetch every 5s until it appears.
 */
export function useQualityReport(runId: string | undefined, pollUntilReady = false) {
  const result = useQuery({
    queryKey: qualityReportKeys.run(runId!),
    queryFn: () => fetchQualityReport(runId!),
    enabled: !!runId,
    staleTime: 30_000,
    retry: false,
    refetchInterval: (query) => {
      if (pollUntilReady && query.state.data === null) return 5_000;
      return false;
    },
  });
  return result;
}

// The on-demand trigger (`useTriggerQualityCheck`, POST .../quality-report) was
// removed on 2026-10-11 with the route behind it; see handlers/agents.py.
