// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { memo, useEffect } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table, { type TableProps } from '@cloudscape-design/components/table';
import CodeViewer from '../CodeViewer';
import MappingViewer from '../MappingViewer';
import AgentOutputDiff from '../AgentOutputDiff';
import { useCodeVersion } from '../../hooks';
import { approvalMappingModel } from '../../utils/approval-mapping-model';
import {
  unsourcedTargetFields,
  type UnsourcedTargetField,
} from '../../utils/unsourced-target-fields';
import {
  approvalProposedRenames,
  displayName,
  type ProposedRename,
} from '../../utils/rename-manifest';
import type { Approval, FieldMapping, MappingData, UnmappedFieldsPayload } from '../../types';

/**
 * ApprovalArtifact — renders the *thing being approved* (P29 R2).
 *
 * Switches on `approval.approval_type` and shows the real artifact BEFORE the
 * approve/reject buttons, so sign-off is informed rather than blind:
 *
 *   - `code_generation` → {@link CodeGenerationArtifact}: a banner naming what
 *     testing established and what approving does, the shared {@link CodeViewer}
 *     fed the approval's `code_version_id` — or a side-by-side
 *     {@link AgentOutputDiff} when a `previous_code_version_id` is present
 *     (R2.1) — and the per-check list from `details.test_result.checks`
 *     (p34 R3.6). Its copy is its own; see that component for why none of
 *     `TestFailureArtifact`'s strings are reused.
 *   - `schema_mapping` → the shared {@link MappingViewer} mapping table. When
 *     the payload carries fallback provenance (`generated_by` starting with
 *     `fallback_`) a warning badge is shown (R2.5). Coordination: p28 R3.5 adds
 *     `generated_by`; until then the field is absent and the badge is simply
 *     not rendered.
 *   - `test_failure` → a per-check report table from
 *     `(details.test_result ?? details.report).checks[]` (R2.6; p32 2.1/2.2).
 *     Coordination: p28 R4.2 introduces this approval type. Until it lands this
 *     is dead-but-tested code behind the type switch — and it is safe (no
 *     crash) when the type/fields are absent.
 *
 * Both p28-dependent renderers are guarded on type/field presence so they are
 * safe before p28 deploys.
 *
 * Anything else — a type this switch does not handle, or a missing type — falls
 * to {@link UnknownArtifact}, which says so explicitly (p30 R14.5). It must NOT
 * render nothing: this component sits directly above live approve/reject
 * buttons, and a blank space reads as "nothing to approve" when the truth is
 * "this UI cannot show you what you are approving".
 */

export interface ApprovalArtifactProps {
  approval: Approval;
}

/** Reads a string field from `details` first, then the approval top-level. */
function detailStr(approval: Approval, ...keys: string[]): string | undefined {
  const details = (approval.details ?? {}) as Record<string, unknown>;
  const top = approval as unknown as Record<string, unknown>;
  for (const key of keys) {
    const value = details[key] ?? top[key];
    if (typeof value === 'string' && value) return value;
  }
  return undefined;
}

/** Extracts renderable code text from a CodeDetail-ish object. */
function codeText(data: unknown): string {
  const rec = (data ?? {}) as Record<string, unknown>;
  return (rec.code_content as string) ?? (rec.code as string) ?? '';
}

/**
 * Coerces an unknown value into a list of display strings.
 *
 * p45 — this **stays**, and it serves `unmapped_source` only (do-NOT 19). Both
 * producers write bare names on that key — `generate_simple_mapping:893` is
 * `unmapped_source = source_fields` — so an object shape there would be
 * invented by the frontend. The **target** half moved to
 * {@link unsourcedTargetFields}, which is the one place the two wire shapes are
 * tolerated (do-NOT 2); do not route the source half through it.
 */
function toStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    if (typeof entry === 'string') return entry;
    const rec = entry as Record<string, unknown>;
    return String(rec?.field ?? rec?.field_name ?? rec?.name ?? JSON.stringify(entry));
  });
}

/**
 * Builds the {@link MappingData} shape the shared MappingViewer expects from an
 * approval payload. Defensive about the two mapping shapes seen across the
 * codebase: `{source, target}` and `{source_field, target_field}`.
 *
 * **p45 R1 — the Approval_Payload's two Unsourced_Keys reach the approver.**
 * `unmapped_fields.target_unsourced` (R1.1) and `unmapped_fields.target_required`
 * (R1.2) had **no frontend consumer at all** before this; the target list is now
 * joined by {@link unsourcedTargetFields}, so `requirement`, `default_value` and
 * `reason` survive the trip to the component instead of being flattened away
 * (R4.3).
 *
 * **`Approval.details` is NOT widened** (R1.6, C8, do-NOT 11). `LGR-69ca78b-4`
 * (**template / ratified**, BROAD/59): *"`Approval.details` was NOT narrowed to
 * tidy the reader, per `LGR-99050e6-5`; it is narrowed at the point of use."*
 * This file already does that four times over — {@link detailStr} (`:55-63`),
 * {@link codeText} (`:66-69`), {@link toStringList} (`:72-79` pre-p45) and the
 * `Record<string, unknown>` coercion on the first line below — and the
 * {@link UnmappedFieldsPayload} cast is the fifth. Naming the **nested** shape is
 * not narrowing the container.
 *
 * **Required-ness comes from `target_required` only** (R1.3, do-NOT 2). Nothing
 * here reads `proposed_mappings` or `target_schema.required` for it, and nothing
 * re-derives it in TypeScript: `LGR-9e76249-7` (**template / ratified**) records
 * *"one shared definition: `unsourced_target_field_names` reads both lists … and
 * the approval decision, the approval payload and the normaliser all call it, so
 * they cannot drift about what 'unsourced' means"* — a second definition here
 * would put that drift back. Order and roster are the producer's, as received
 * (R1.5, do-NOT 13), and every name renders, optional ones included (R1.7).
 */
function toMappingData(approval: Approval): MappingData {
  const details = (approval.details ?? {}) as Record<string, unknown>;
  const rawMappings = (details.proposed_mappings ?? details.mappings ?? []) as unknown;
  const mappings: FieldMapping[] = Array.isArray(rawMappings)
    ? rawMappings.map((entry) => {
        const m = (entry ?? {}) as Record<string, unknown>;
        return {
          source_field: String(m.source_field ?? m.source ?? ''),
          target_field: String(m.target_field ?? m.target ?? ''),
          confidence: typeof m.confidence === 'number' ? m.confidence : undefined,
          transformation: typeof m.transformation === 'string' ? m.transformation : undefined,
          hint: typeof m.hint === 'string' ? m.hint : undefined,
        };
      })
    : [];

  // Unmapped fields may be `{source: [], target: []}` or flat `*_fields` lists.
  let unmappedSource: string[] = [];
  let unmappedTarget: UnsourcedTargetField[] = [];
  const unmapped = details.unmapped_fields;
  if (unmapped && typeof unmapped === 'object' && !Array.isArray(unmapped)) {
    const u = unmapped as Record<string, unknown>;
    unmappedSource = toStringList(u.source);
    // R1.1, R1.2, R1.6 — the cast is local and the nested payload is the only
    // thing it names; `Approval.details` above stays `Record<string, unknown>`.
    // `unsourcedTargetFields` reads `target`, `target_unsourced` and
    // `target_required` and performs the whole join (design §4.8's four steps).
    unmappedTarget = unsourcedTargetFields(unmapped as UnmappedFieldsPayload);
  }
  if (unmappedSource.length === 0) {
    unmappedSource = toStringList(details.unmapped_source ?? details.unmapped_source_fields);
  }
  if (unmappedTarget.length === 0) {
    // The flat pre-p37 spelling. It carries names only — no Unsourced_Keys exist
    // to read beside it — so it is handed to the same reader as a payload with
    // `target` alone, which is exactly R1.4's branch: names in the order
    // received, every entry `not_established`, and nothing presented as
    // optional (R2.6, do-NOT 5). Routing it through `unsourcedTargetFields`
    // rather than building entries here keeps one reader of the wire (do-NOT 2).
    const flatTarget = details.unmapped_target ?? details.unmapped_target_fields;
    unmappedTarget = unsourcedTargetFields(
      Array.isArray(flatTarget) ? { target: flatTarget } : undefined,
    );
  }

  const aiModelUsed = approvalMappingModel(details);

  return {
    mappings,
    unmapped_source: unmappedSource,
    unmapped_target: unmappedTarget,
    schema_hash: String(details.schema_hash ?? ''),
    version_index: 0,
    ai_model_used: aiModelUsed,
  };
}

/**
 * Code artifact. Reuses CodeViewer for the single-version case and the shared
 * AgentOutputDiff when a previous version id is present. Both `useCodeVersion`
 * calls run unconditionally (the second is disabled internally when its id is
 * undefined) to keep hook order stable.
 */
function CodeArtifact({
  codeVersionId,
  previousCodeVersionId,
}: {
  codeVersionId: string;
  previousCodeVersionId?: string;
}) {
  const hasPrevious = !!previousCodeVersionId;
  const current = useCodeVersion(codeVersionId);
  const previous = useCodeVersion(hasPrevious ? previousCodeVersionId : undefined);

  if (hasPrevious) {
    if (current.isLoading || previous.isLoading) {
      return (
        <Box textAlign="center" padding="l" data-testid="approval-artifact-code-loading">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary">
            Loading code diff…
          </Box>
        </Box>
      );
    }
    if (!current.isError && !previous.isError && current.data && previous.data) {
      return (
        <div data-testid="approval-artifact-code-diff">
          <AgentOutputDiff
            labelA="Previous version"
            labelB="Current version"
            contentA={codeText(previous.data)}
            contentB={codeText(current.data)}
            contentType="code"
          />
        </div>
      );
    }
    // Diff data unavailable — fall through to the single-version viewer.
  }

  return <CodeViewer codeVersionId={codeVersionId} />;
}

/**
 * True for a check that verified NOTHING — the pandas proxy refused the
 * construct, so the transform never ran (p32 2.20).
 *
 * The discriminator is the explicit `verified: false` key, which
 * `aggregate_check_results` puts ONLY on a not-verified entry
 * (`test_code/lambda_function.py:183-192`, emitted at `:308-318`). A check that
 * verified something carries no `verified` key at all, so this is a three-state
 * read off a payload whose `passed` is only ever two-state.
 *
 * `passed` is `false` on such an entry — a boolean cannot express three states —
 * which is why a reader keying only on `passed` renders a refusal as an error.
 * That is the state 2.20/2.21 forbid: "not verified" is neither "failed" nor
 * "not reported".
 */
function isNotVerified(check: Record<string, unknown>): boolean {
  return check.verified === false;
}

/**
 * Labels for EVERY `not_verified_reason` a producer writes — all six of them
 * (p40 R4 added the sixth).
 *
 * The set was enumerated from the producers rather than from memory, because a
 * reason with no entry here does not fail loudly: it renders the bare
 * {@link NOT_VERIFIED_LABEL_FALLBACK} "not verified", which is indistinguishable
 * from a reason nobody has ever emitted. The six, with where each is written
 * (the first five as cited when they were added; the sixth re-read for p40):
 *
 *   - `permanently_inexpressible` — `code_interpreter_runner.py:64`, returned by
 *     `classify_conversion_refusal` (`:152`), put on the result at `:632`, and
 *     carried onto the check at `test_code/lambda_function.py:444`.
 *   - `not_yet_expressible` — `code_interpreter_runner.py:67`, returned `:153`,
 *     result `:632`, check `lambda_function.py:444` (also that branch's default,
 *     `:421`).
 *   - `scalar_literal_receiver` — owned by the converter, because the converter
 *     cannot import the runner: `pyspark_to_pandas.py:93`, raised by
 *     `_refuse_scalar` (`:662`), aliased as
 *     `code_interpreter_runner.NOT_VERIFIED_REASON_SCALAR_LITERAL` (`:87`) and
 *     put on the result at `:664` — where `exc.reason` takes precedence over
 *     `classify_conversion_refusal` (`:649`) — then spelled a second time as
 *     `lambda_function.SCALAR_LITERAL_RECEIVER_REASON` (`:253`) and carried onto
 *     the check at `:549`.
 *   - `test_frame_truncated` — `code_interpreter_runner.py:77`, returned `:301`,
 *     result `:681`, check `lambda_function.py:484` (also that branch's default,
 *     `:455`).
 *   - `no_source_schema` — `code_interpreter_runner.py:81`, returned `:298`,
 *     result `:681`, check `lambda_function.py:484`.
 *   - `no_target_schema` — `lambda_function.py:235`, emitted `:534-537`.
 *
 * The first two are p32 2.23's, keyed by what `classify_conversion_refusal`
 * writes. They must stay distinguishable ON SCREEN, because they mean opposite
 * things to a reviewer: the first is permanent and correct behaviour with
 * nothing to fix (3.7's list — `groupBy`, `agg`, `spark.`, UDFs, `initcap`,
 * joins, windows); the second is a gap in this system's own converter (1.7-1.9)
 * and is a defect. A single "not verified" label for both would re-create
 * exactly the ambiguity the backend classification removed.
 *
 * `scalar_literal_receiver` is p40 R4's, and it is a THIRD proxy reason rather
 * than a variant of either p32 one — which is why it needs its own text and not
 * a shared "the proxy refused" label. The transform applies a pandas member that
 * only exists on a column to a constant value; the converter's receiver-shape
 * fence declines rather than emit pandas that raises, or pandas that silently
 * disagrees with Spark. So it is neither `permanently_inexpressible` (that is
 * about a construct pandas can never express — this one pandas expresses fine on
 * a column) nor `not_yet_expressible` (that is a converter gap and a defect —
 * this is the converter working as designed). The reviewer's takeaway is the
 * proxy's own choice: the generated PySpark is valid and there is nothing in it
 * to fix, which is what "(nothing to fix in the code)" carries.
 *
 * `no_target_schema` is p34's (task 6.4). `schema_compliance` emits
 * `not_verified_reason = NO_TARGET_SCHEMA_REASON = "no_target_schema"` when the
 * pipeline has no target schema to compare against. Before this entry existed
 * the row fell to the fallback and read as a bare "not verified" — which, in
 * front of a human about to approve generated code, understates it: the check
 * never ran, so there is nothing in the code to fix and nothing to reassure
 * either. p34 R3.6 puts that row in front of a reviewer for the first time
 * (`CodeGenerationArtifact` below), which is why the label is added here rather
 * than left to the fallback.
 *
 * `test_frame_truncated` and `no_source_schema` are p33 2.11/D12's — the reasons
 * a non-verification is the TEST HARNESS's rather than the pandas proxy's,
 * reported under `not_verified_by_harness`. They are the same argument, and were
 * left to the fallback by 6.4 only because widening the map was outside that
 * task's stated scope. Nothing about them is residual: both are reachable on the
 * `code_generation` path p34 task 8.1 made the default outcome of every clean
 * run, and both were already reachable on `test_failure`. Their register matches
 * `no_target_schema`'s — what did not happen, and why — and they name the
 * harness, because that is the one thing a reviewer needs from the Status column:
 * the cause is the test frame, not the generated PySpark, so there is nothing in
 * the code to fix. The full statement of that is in the `detail` the producer
 * writes (`lambda_function.py:456-481`), which the Details cell renders; the
 * Status column is the summary a reviewer scans, and per `LGR-99050e6-1`
 * (template / ratified) it has to carry the state in TEXT, because the indicator
 * colour alone fails WCAG 1.4.1 and `stopped` looks like any other neutral row.
 *
 * Kept as a map with a fallback, not a total `Record`: an unrecognised reason
 * must still render something honest. See {@link NOT_VERIFIED_LABEL_FALLBACK}.
 */
const NOT_VERIFIED_LABELS: Record<string, string | undefined> = {
  permanently_inexpressible: 'not verified — no pandas equivalent (nothing to fix)',
  not_yet_expressible: 'not verified — converter gap (defect in the checker)',
  // p40 R4 — the third proxy reason. Its own text, not either neighbour's: this
  // is neither a permanent gap in pandas nor a gap in this converter.
  scalar_literal_receiver:
    'not verified — a column operation applied to a constant; the proxy declined (nothing to fix in the code)',
  no_target_schema: 'not verified — no target schema to compare against (the check did not run)',
  test_frame_truncated:
    'not verified — source schema wider than the test harness frame (the transform never ran)',
  no_source_schema: 'not verified — no source schema to build a test frame from (the transform never ran)',
  // p42 R8.1 — the third HARNESS reason, and the only not-verified reason that
  // is about the ACCOUNT rather than about the code or the harness's own
  // limits. Its copy says "try again later" because that is the action, which
  // neither width reason above has.
  session_capacity_exhausted:
    'not verified — no Spark session available (account at its limit; nothing to fix, retry later)',
  // p42 R7.3 — the FOURTH harness reason. Its own text rather than either width
  // reason's: the schema is present and inside the ceiling, so "wider than the
  // harness frame" would be a false claim about the schema. What went wrong is a
  // declared TYPE the frame builder cannot represent.
  test_frame_unbuildable:
    'not verified — a declared source type the test harness cannot build a frame for (the transform never ran)',
  // p44 R2.1 — the FIFTH harness reason, and the first one about the run's own
  // DATA rather than about its schema, its types or the account. Its own text,
  // not a neighbour's, because all four of those say something else and point
  // somewhere else: the two width reasons say the schema was too wide,
  // `session_capacity_exhausted` says try again later, and
  // `test_frame_unbuildable` says a declared TYPE could not be represented.
  // Here the schema is fine and a session was created and billed — what failed
  // is opening the object, so the action is to check the object and the
  // verification session's read access, which is different from all four.
  source_read_failed:
    'not verified — the run\'s source object could not be read (nothing to fix in the code; check the object and the session\'s read access)',
};

/**
 * Fallback when a not-verified check carries no recognised reason.
 *
 * Residual by design, and now residual in fact: every reason the producers emit
 * has an entry above, so reaching this line means a reason was added to a
 * producer without one being added here. It is deliberately NOT removed — a
 * not-verified check must still render its state in text even when its reason is
 * unknown to this file, because the alternative is an empty Status cell.
 */
const NOT_VERIFIED_LABEL_FALLBACK = 'not verified';

/**
 * Status indicator type for a single test-report check.
 *
 * THREE states, not two (p32 2.20). `stopped` is the not-verified indicator:
 * distinct from `success`, from `error`, and from the `info` fall-through.
 *
 * Note which way this failed before the third branch existed. The state was
 * expected to fall through to `info`/"unknown", but the emitted check carries
 * `passed: false` (a boolean cannot express three states), so it matched the
 * `passed === false` branch: a refusal rendered as `error` and was labelled
 * "failed". Not a lost state — an actively wrong one, and exactly the claim
 * 2.21 forbids.
 *
 * The indicator type is deliberately secondary here. Per `LGR-99050e6-1`'s house
 * pattern the state is carried by {@link checkStatusLabel}'s TEXT, because
 * colour alone fails WCAG 1.4.1 and would leave a refusal indistinguishable from
 * any other neutral row.
 */
function checkStatusType(check: Record<string, unknown>): 'success' | 'error' | 'warning' | 'info' | 'stopped' {
  // First, because `passed` is `false` on a not-verified check and the error
  // branch below would otherwise claim a refusal failed. The key is read inline
  // rather than through `isNotVerified` on purpose: the payload contract test
  // (`tests/contracts/api_responses/test_test_failure_approval_payload.py`)
  // parses the `check.<key>` reads out of this function and out of
  // `checkStatusLabel` to learn which per-check keys reach the screen, so a key
  // read only behind a helper would look unrenderable to it.
  if (check.verified === false) return 'stopped';
  const status = String(check.status ?? '').toLowerCase();
  const passed = check.passed;
  if (passed === true || status === 'passed' || status === 'pass' || status === 'success') return 'success';
  if (passed === false || status === 'failed' || status === 'fail' || status === 'error') return 'error';
  if (status === 'warning' || status === 'warn') return 'warning';
  return 'info';
}

/**
 * Human label for a check's status — the text surface for p32 2.20/2.23.
 *
 * The not-verified case is read FIRST and names which of the two reasons
 * applies, because everything below it is two-state: the `passed === false`
 * branch labelled a refusal "failed", and the final fall-through — reachable for
 * a check carrying no `passed` at all — labelled it the literal "unknown".
 * Neither is true of a transform that never ran.
 */
function checkStatusLabel(check: Record<string, unknown>): string {
  if (check.verified === false) {
    return NOT_VERIFIED_LABELS[String(check.not_verified_reason ?? '')] ?? NOT_VERIFIED_LABEL_FALLBACK;
  }
  if (typeof check.status === 'string' && check.status) return check.status;
  if (check.passed === true) return 'passed';
  if (check.passed === false) return 'failed';
  return 'unknown';
}

/**
 * The per-check table columns, shared by the two surfaces that render a check
 * list: {@link TestFailureArtifact} (a failing run) and
 * {@link CodeGenerationArtifact} (a passing run, p34 R3.6).
 *
 * Shared deliberately rather than copied. The three-state status column is the
 * whole of p32 2.20/2.21 and the `detail`-first chain is `bugfix.md` 1.2 — two
 * copies would let a passing run's rendering of a Not_Verified_Check drift away
 * from a failing run's, which is the ambiguity both fixes removed.
 *
 * The `check.<key>` reads stay inline in {@link checkStatusType} /
 * {@link checkStatusLabel} and in the cells below, NOT behind a new helper: the
 * payload contract test (`tests/contracts/api_responses/test_test_failure_approval_payload.py`)
 * parses those expressions out of this file to learn which per-check keys reach
 * the screen, and a key read only behind a helper would look unrenderable to it.
 */
const CHECK_COLUMN_DEFINITIONS: ReadonlyArray<TableProps.ColumnDefinition<Record<string, unknown>>> = [
  {
    id: 'check',
    header: 'Check',
    cell: (check: Record<string, unknown>) => String(check.name ?? check.check ?? '—'),
  },
  {
    id: 'status',
    header: 'Status',
    cell: (check: Record<string, unknown>) => (
      <StatusIndicator type={checkStatusType(check)}>{checkStatusLabel(check)}</StatusIndicator>
    ),
  },
  {
    id: 'details',
    header: 'Details',
    // `detail` first — that is the key `aggregate_check_results` writes on every
    // check (`test_code/lambda_function.py:234`, `:244`, `:253`, `:261`, `:273`),
    // and reading only `message`/`details` rendered `—` in every cell on every
    // real payload (`bugfix.md` 1.2). The other two stay in the chain per 2.2.
    cell: (check: Record<string, unknown>) => String(check.detail ?? check.message ?? check.details ?? '—'),
  },
];

/**
 * Reads the first present overall-reason string off a test report. Checks, in
 * order: `summary`, `overall_status`, `message`, `error`. Returns undefined when
 * none are present as a non-empty string (defensive — nothing is rendered then).
 */
function testFailureReason(report: Record<string, unknown>): string | undefined {
  for (const key of ['summary', 'overall_status', 'message', 'error'] as const) {
    const value = report[key];
    if (typeof value === 'string' && value) return value;
  }
  return undefined;
}

/**
 * Test-failure artifact (p28 R4.2). ALWAYS leads with a prominent banner making
 * the state explicit — failed, or (p32 2.21) not verified — then surfaces an
 * overall reason string (when present) and the per-check report table. When no
 * checks are recorded it keeps the failure banner and shows a neutral note in
 * place of the table. Guarded — never crashes when the report/checks are absent.
 *
 * The check list is read from `details.test_result` first, then `details.report`
 * (p32 C1 / 2.1). `test_result` is the key the producer actually writes —
 * `approval_handler/lambda_function.py:248-267`, carrying the
 * `{status, checks, report_s3_uri}` object built at
 * `test_code/lambda_function.py:940-944` — and reading only `report` is what
 * made every real payload render the no-checks note (`bugfix.md` 1.1). `report`
 * is kept in the chain because 2.2 requires any payload that carries it to keep
 * working. The `??`-chain form is this file's own idiom, matching `detailStr`
 * above and `toMappingData`, not a new concession.
 *
 * p32 2.20/2.21: a check may also be NOT VERIFIED — see {@link isNotVerified}.
 * Those entries are not evidence of failure, so when they are the only
 * non-passing rows the banner says what actually happened instead of asserting a
 * failure that did not occur. The house pattern is kept either way: a header
 * plus an explicit statement of what approving means (`LGR-78f241b-1`).
 */
function TestFailureArtifact({ approval }: { approval: Approval }) {
  const details = (approval.details ?? {}) as Record<string, unknown>;
  const report = (details.test_result ?? details.report ?? {}) as Record<string, unknown>;
  const checks = Array.isArray(report.checks) ? (report.checks as Array<Record<string, unknown>>) : [];
  const reason = testFailureReason(report);

  // Refusals versus real failures. A refusal reports `passed: false`, so the two
  // have to be separated here as well as in the status column, or the banner
  // renders a non-verification as an execution failure (2.21).
  const notVerifiedChecks = checks.filter(isNotVerified);
  const failedChecks = checks.filter((check) => checkStatusType(check) === 'error');
  const notVerifiedOnly = notVerifiedChecks.length > 0 && failedChecks.length === 0;

  return (
    <SpaceBetween size="m">
      <Alert
        type={notVerifiedOnly ? 'warning' : 'error'}
        header={notVerifiedOnly ? 'Code testing did not verify this transform' : 'Code testing failed'}
        data-testid="approval-artifact-test-failure-alert"
      >
        {notVerifiedOnly
          ? 'No check failed. The pandas proxy could not express part of the transform, so the transform never ran — nothing here says the generated PySpark is wrong, and nothing here says it is right. Read the not-verified rows below for the construct, the line, and whether there is anything to fix before deciding.'
          : 'Code testing failed on this run. Review the checks below before deciding — approving proceeds with code that did not pass testing.'}
      </Alert>
      {reason && (
        <Box variant="small" data-testid="approval-artifact-test-failure-reason">
          {reason}
        </Box>
      )}
      {checks.length > 0 ? (
        <Container header={<Header variant="h3">Test Report</Header>}>
          <Table
            data-testid="approval-artifact-test-report"
            variant="embedded"
            items={checks}
            wrapLines
            columnDefinitions={CHECK_COLUMN_DEFINITIONS}
            empty={
              <Box textAlign="center" color="text-body-secondary">
                No checks reported
              </Box>
            }
          />
        </Container>
      ) : (
        <Box variant="small" color="text-body-secondary" data-testid="approval-artifact-empty">
          No per-check details were recorded for this failure.
        </Box>
      )}
    </SpaceBetween>
  );
}

/**
 * Code-generation artifact (p34 R3.6 and R5's user story).
 *
 * Three things, in the order a reviewer needs them: a banner naming what testing
 * established and what approving does, the generated code, and the per-check
 * list the payload carries.
 *
 * WHY THIS EXISTS. Until p34 this branch rendered the CodeViewer and nothing
 * else, so `details.test_result.checks` sat one field away from the code being
 * judged and reached no surface. p34 task 8.1 makes a code approval the default
 * outcome of every clean run, and design §3 records the one such approval that
 * had ever existed at that point: approved, with seven `null`s in its
 * `approval_data`. A human approved generated code having been shown nothing
 * about it. This is the screen that has to exist before that gate can fire.
 *
 * ITS OWN COPY, DELIBERATELY. {@link TestFailureArtifact}'s strings are NOT
 * reused. On a `code_generation` approval the check list is evidence *in favour*,
 * so `Code testing failed` and the pandas-proxy sentence would be a
 * misattribution created by this change rather than discovered by it (design
 * §4.5). That banner is also unreachable from here — it lives inside
 * `TestFailureArtifact`, which the switch below enters only for
 * `type === 'test_failure'` — and this component must not make it reachable.
 * `ApprovalArtifact.code-generation.test.tsx` guards all four of its strings.
 *
 * The banner carries the check state in TEXT, in its header, not in the alert
 * colour: `LGR-99050e6-1` (template / ratified) requires it, and colour alone
 * fails WCAG 1.4.1. The body states what approving and rejecting each do, which
 * is `LGR-78f241b-1`'s house pattern for an approval banner (**backfilled** — the
 * author's words, not a ratified decision). Both consequences are read off the
 * definition: approval resumes the token and `waitForCodeApproval.next(
 * createInfrastructure)` (`orchestration-stack.ts:908`) continues to
 * infrastructure creation, then `createGluePipeline` (`:909`); rejection sends a
 * task failure that the state's `Catch[States.ALL] → RecordFailure` (`:672`)
 * routes to the terminal failure path.
 *
 * `Approval.details` is read and narrowed here rather than typed at the
 * boundary. `LGR-99050e6-5` (template / ratified) rejected narrowing a frontend
 * type to accommodate a reader, and `types.ts:138` keeps
 * `details?: Record<string, unknown>` on purpose. The reads mirror
 * {@link detailStr} above, and `?? {}` means an absent or null `details` cannot
 * throw.
 *
 * The check list is read from `test_result` only — the key the producer writes
 * (`test_code/lambda_function.py:1722`, carried onto the payload by
 * `approval_handler.get_approval_data` and into the request by
 * `WaitForCodeApproval`'s `Payload`). Unlike the `test_failure` chain there is no
 * legacy `report` shape to keep working: this branch has never rendered a check
 * list, so there is no older payload to be compatible with.
 */
function CodeGenerationArtifact({ approval }: { approval: Approval }) {
  const details = (approval.details ?? {}) as Record<string, unknown>;
  const testResult = (details.test_result ?? {}) as Record<string, unknown>;
  const checks = Array.isArray(testResult.checks) ? (testResult.checks as Array<Record<string, unknown>>) : [];

  const codeVersionId = detailStr(approval, 'code_version_id', 'codeVersionId');
  const previousCodeVersionId = detailStr(approval, 'previous_code_version_id', 'previousCodeVersionId');

  // Three counts, not two. A Not_Verified_Check reports `passed: false` because a
  // boolean cannot express three states (see {@link isNotVerified}), so counting
  // it as a failure would tell a reviewer a check failed when it never ran.
  const notVerifiedCount = checks.filter(isNotVerified).length;
  const failedCount = checks.filter((check) => checkStatusType(check) === 'error').length;
  const passedCount = checks.filter((check) => checkStatusType(check) === 'success').length;

  // The state, as text. Each branch says only what the counts support: no branch
  // claims the code is correct, and none claims it failed.
  let stateHeader: string;
  let stateType: 'success' | 'warning';
  if (checks.length === 0) {
    stateHeader = 'No test checks were recorded for this code';
    stateType = 'warning';
  } else if (failedCount > 0) {
    stateHeader = `${failedCount} of ${checks.length} checks did not pass`;
    stateType = 'warning';
  } else if (notVerifiedCount > 0) {
    stateHeader = `${passedCount} of ${checks.length} checks passed, ${notVerifiedCount} not verified`;
    stateType = 'warning';
  } else {
    stateHeader = `All ${checks.length} checks passed`;
    stateType = 'success';
  }

  return (
    <SpaceBetween size="m">
      <Alert type={stateType} header={stateHeader} data-testid="approval-artifact-code-generation-alert">
        Approve to accept this generated code: the run continues to infrastructure creation and the
        remaining steps run. Reject to stop the run at this gate — the failure is recorded and no later
        step runs. The check list below is what testing established about this code; read it, and the
        code itself, before deciding.
      </Alert>
      {codeVersionId ? (
        <div data-testid="approval-artifact-code">
          <CodeArtifact codeVersionId={codeVersionId} previousCodeVersionId={previousCodeVersionId} />
        </div>
      ) : (
        <Box variant="small" color="text-body-secondary" data-testid="approval-artifact-empty" padding="s">
          The generated code is not available for preview, so nothing here shows the code itself.
        </Box>
      )}
      {checks.length > 0 ? (
        <Container header={<Header variant="h3">Test checks</Header>}>
          <Table
            data-testid="approval-artifact-test-report"
            variant="embedded"
            items={checks}
            wrapLines
            columnDefinitions={CHECK_COLUMN_DEFINITIONS}
            empty={
              <Box textAlign="center" color="text-body-secondary">
                No checks reported
              </Box>
            }
          />
        </Container>
      ) : (
        <Box variant="small" color="text-body-secondary" data-testid="approval-artifact-no-checks">
          No per-check results were recorded for this approval, so nothing here says what testing
          covered.
        </Box>
      )}
    </SpaceBetween>
  );
}

/**
 * The Rename_Manifest, as a sub-payload of the schema-mapping artifact — p55
 * R10.1, R10.2, design decision W4-D3.
 *
 * A sub-payload of the **existing** artifact and not a new approval kind: R10.1
 * says the existing Schema_Mapping_Approval presents the manifest, and a new kind
 * would mean a new `waitForTaskToken` state and a new Choice inheriting
 * `LGR-69ca78b-1` (standing invariant) for information the approver is already
 * looking at.
 *
 * **Rendered only when a rename is actually proposed.** R10.1's condition is "at
 * least one Non_Conforming_Column_Name", and both of the other states are
 * genuinely nothing to say on this surface: an empty `renames` list means every
 * source column name already conforms, and a manifest the payload never carried
 * means nothing was ever proposed. The two summary surfaces
 * (`ApprovalsPage`, `ApprovalWorkflowSection`) DO distinguish them, because each
 * has a labelled field that would otherwise render blank — a blank beside a label
 * reads as a measured "none". Here there is no label without the panel.
 *
 * **The house pattern for a panel above live approve/reject buttons is a header
 * plus an explicit statement of what approving does** (`LGR-78f241b-1`), which
 * `TestFailureArtifact` and `UnknownArtifact` above both follow. This panel is an
 * instance of it: the approver is not being shown a fact, they are being asked to
 * authorise a change of name.
 *
 * **Accessibility, and what is relied on rather than hand-rolled.** Cloudscape's
 * `Table` renders a real `<table>` with `<th scope="col">` headers and is keyboard
 * navigable, which is why the two check-report tables above use it rather than a
 * `div` grid; `ariaLabels.tableLabel` gives it an accessible name, since the
 * visible `Header` is on the enclosing `Container` and is not programmatically
 * associated with the table. The state is carried in the column **text**, not by
 * colour — `LGR-99050e6-1` (template / ratified) records that colour alone fails
 * WCAG 1.4.1, and this panel has no status colour at all for the same reason.
 *
 * **Both names are text content, and that is deliberate** (p55 do-NOT 17).
 * `original_name` is an attacker-controlled CSV header. React escapes text
 * children, so a cell is a safe sink; a `title` attribute, a URL or
 * `dangerouslySetInnerHTML` would not be, and none is used here. Length is capped
 * and the truncation is marked by `displayName` — see `utils/rename-manifest.ts`
 * for why the cap is its own number rather than `identifier_utils._DISPLAY_LIMIT`'s
 * 300.
 */
function ProposedRenamesArtifact({ approval }: { approval: Approval }) {
  const renames = approvalProposedRenames(approval.details);
  if (renames === null || renames.length === 0) return null;

  return (
    <div data-testid="approval-artifact-rename-manifest">
      <Container
        header={
          <Header variant="h3" counter={`(${renames.length})`}>
            Proposed source column renames
          </Header>
        }
      >
        <SpaceBetween size="s">
          <Alert type="info" header="Approving accepts these names">
            {renames.length === 1 ? 'This source column name' : 'These source column names'} cannot be used
            as a target identifier, so the generated job renames{' '}
            {renames.length === 1 ? 'it' : 'them'} immediately after reading the source. The mapping below
            still refers to the original {renames.length === 1 ? 'name' : 'names'}. Rejecting this approval
            ends the run instead.
          </Alert>
          <Table
            data-testid="approval-artifact-rename-manifest-table"
            variant="embedded"
            wrapLines
            items={renames}
            ariaLabels={{ tableLabel: 'Proposed source column renames' }}
            columnDefinitions={RENAME_COLUMN_DEFINITIONS}
            empty={
              <Box textAlign="center" color="text-body-secondary">
                No renames proposed
              </Box>
            }
          />
        </SpaceBetween>
      </Container>
    </div>
  );
}

/**
 * The two columns of a Proposed_Rename — R10.2 requires both names, so there are
 * two columns and neither is optional.
 *
 * Module-level rather than inline, matching {@link CHECK_COLUMN_DEFINITIONS}
 * above: a definition rebuilt per render defeats `Table`'s own memoisation, and
 * `ApprovalArtifact` is one of exactly two components measured into a
 * `React.memo` wrapper (`LGR-8b6e6a1-2`, template / ratified).
 */
const RENAME_COLUMN_DEFINITIONS: ReadonlyArray<TableProps.ColumnDefinition<ProposedRename>> = [
  {
    id: 'original',
    header: 'Source column name',
    cell: (entry: ProposedRename) => displayName(entry.original_name),
  },
  {
    id: 'target',
    header: 'Proposed target name',
    cell: (entry: ProposedRename) => displayName(entry.target_name),
  },
];

/**
 * Approval types already reported once this page-session. A new approval type
 * reaching production unrendered is a defect worth learning about without
 * waiting for a user report, but the branch is reached routinely (`first_run`
 * and `infrastructure` are live backend types with no artifact renderer here),
 * so the warning is deduplicated per distinct type rather than fired per render.
 */
const reportedUnknownTypes = new Set<string>();

/**
 * Fallback artifact for an approval type this component cannot render (R14.5).
 *
 * Deliberately an Alert, not a quiet note: the mappings fallback-provenance
 * warning and the test-failure banner above set the house pattern for "something
 * about this artifact is not fully trustworthy" — a warning header plus an
 * explicit statement of the consequence of approving anyway (`LGR-78f241b-1`).
 * This case is the strongest instance of it, because the missing information is
 * the artifact itself.
 *
 * The raw `approval_type` is shown verbatim so a user reporting the problem can
 * quote it, and it is the fastest route to diagnosing a backend that has started
 * emitting a type the frontend does not know about.
 */
function UnknownArtifact({ approvalType }: { approvalType: string }) {
  useEffect(() => {
    const key = approvalType || '(absent)';
    if (reportedUnknownTypes.has(key)) return;
    reportedUnknownTypes.add(key);
    console.warn(
      'ApprovalArtifact: no artifact renderer for approval type — review modal shown without an artifact',
      { approval_type: approvalType || null },
    );
  }, [approvalType]);

  const subject = approvalType
    ? `approval type "${approvalType}"`
    : 'this approval, which records no approval type';
  const reportHint = approvalType
    ? 'report the approval type named above'
    : 'report this approval';

  return (
    <Alert
      type="warning"
      header="Cannot display what is being approved"
      data-testid="approval-artifact-unknown-type"
    >
      This dashboard cannot render an artifact for {subject}, so nothing above describes what you are
      approving — an empty preview does not mean there is nothing to approve. The approve and reject
      buttons below are still live and take effect immediately. Reject unless you can confirm
      elsewhere what this approval authorises, and {reportHint} so it can be rendered here.
    </Alert>
  );
}

function ApprovalArtifact({ approval }: ApprovalArtifactProps) {
  const type = (approval.approval_type ?? '').toLowerCase();

  if (type === 'code_generation') {
    // p34 task 6.4: the code alone was never enough. See
    // {@link CodeGenerationArtifact} for what changed and why the copy is its own.
    return (
      <div data-testid="approval-artifact-code-generation">
        <CodeGenerationArtifact approval={approval} />
      </div>
    );
  }

  if (type === 'schema_mapping') {
    const generatedBy = detailStr(approval, 'generated_by');
    const isFallback = !!generatedBy && generatedBy.startsWith('fallback_');
    const mappingData = toMappingData(approval);
    return (
      <div data-testid="approval-artifact-mapping">
        <SpaceBetween size="m">
          {isFallback && (
            <Alert type="warning" header="Fallback mapping" data-testid="approval-artifact-fallback-badge">
              Fallback mapping — generated by substring matching, not AI. Review each field carefully before
              approving.
            </Alert>
          )}
          {/*
            p55 R10.1 — above the mapping table, because approving this approval
            approves the renaming as well as the mapping, and the mapping below is
            expressed in terms of the ORIGINAL column names
            (`mappings[*].source_field` keeps naming the original — design W4-D1,
            which is what keeps `LGR-a419843-2`'s declared-source-field check
            biting). So a reader who meets the mapping first meets names this panel
            is about to say are being changed.
          */}
          <ProposedRenamesArtifact approval={approval} />
          <MappingViewer mappingData={mappingData} />
        </SpaceBetween>
      </div>
    );
  }

  if (type === 'test_failure') {
    return (
      <div data-testid="approval-artifact-test-failure">
        <TestFailureArtifact approval={approval} />
      </div>
    );
  }

  // Unrecognised or absent type — say so (R14.5). Never render nothing here:
  // the approve/reject buttons below this component stay live either way.
  // `approval_type` is passed raw, not lower-cased, so the reported value is the
  // one the backend actually sent.
  return (
    <div data-testid="approval-artifact-unknown">
      <UnknownArtifact approvalType={approval.approval_type ?? ''} />
    </div>
  );
}

/**
 * Memoized because a measurement says so, not on principle (p47 R16.1).
 *
 * The sole prop is `approval`, an element of the structurally-shared `approvals`
 * array React Query hands the parents, and there are no callback props — so the
 * shallow comparison actually succeeds on the case that matters. That case is a
 * **sibling-only arrival**: `updated_at` / `current_step` move on every
 * `execution_updater` write while `approvals` sits still, so the parent
 * re-renders and this subtree does not have to. Measured, on the
 * `ApprovalWorkflowSection` fixture with four artifact instances and today's
 * unstable parent callbacks: 84 child renders over 20 parent re-renders become
 * 4, and the parent's own per-update cost drops from 4.56–4.97 ms to
 * 2.30–2.52 ms — a **2.13–2.53 ms** saving per render. On a real cache arrival
 * the child renders 5 times of 5 without this wrapper and 0 times with it.
 *
 * Output-neutral by construction: `React.memo` changes when the body runs, never
 * what it returns, and every existing test for this component passes unmodified.
 *
 * NOT extended to the parent. `ApprovalWorkflowSection` re-renders on fresh
 * callback identities from `RunDetailPage:444`/`:451`, so memoizing it alone
 * skips nothing and only adds a comparison — the R16.1 anti-pattern, measured
 * rather than assumed (`f3-render-cost-baseline.md` §4.1).
 */
export default memo(ApprovalArtifact);
