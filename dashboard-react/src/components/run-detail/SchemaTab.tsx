// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import MappingEditorSection from '../MappingEditorSection';
import { colorBackgroundContainerContent, colorTextBodyDefault, colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import type { MappingData } from '../../types';

interface SchemaTabProps {
  mappingData: MappingData | null;
  extractedSchema: string;
  /** Pipeline the run belongs to. Absent only before the route param resolves. */
  pipelineKey?: string;
  /** `mapping_id` recorded on this run's `schema_mapping` step. */
  runMappingId?: string;
  /** The pipeline's current `mapping_id`, from pipeline detail. */
  currentMappingId?: string;
  /**
   * Whether this run reused a mapping an earlier run produced (p54 R2.1).
   *
   * **Three states, and `false` is not the unknown one.** `false` means the run
   * generated its own mapping — a `schema_mapping` state row IS present, which
   * is knowledge. `true` means no such row and an id resolved. **Absent** means
   * the fact is not established, which includes the case where the producer's
   * own read raised. `RunDetail.reused_mapping` carries exactly these three
   * states for exactly this reason (`LGR-8e34f37-3`'s omit-on-failure shape,
   * `LGR-99050e6-2`'s rule that an absent value is reported as unknown rather
   * than defaulted), and this prop is optional so that absence survives the
   * trip rather than collapsing into `false` at the boundary.
   */
  reusedMapping?: boolean;
  /**
   * Which mapping was reused (p54 R2.2). Present **only** alongside
   * `reusedMapping: true` — the pair is exhaustive and mutually exclusive, and
   * the id is resolved from the approval record's own `reused_mapping_id`
   * rather than invented.
   *
   * Deliberately **not** forwarded as `runMappingId`: see the reuse arm below.
   */
  reusedMappingId?: string;
  initialContext?: string;
  defaultModelId?: string;
  isRegenerating: boolean;
  onSaveContext: (context: string) => Promise<void>;
  onRerunMapping: (context: string, modelId?: string) => void;
}

/* ────────────────────────────────────────────────────────────────────────
   The reuse arm's copy (p54 R2.1-R2.3)

   THREE SEPARATE SENTENCES, one per criterion, and not one combined sentence.
   R2.3 exists because R2.1 alone is ambiguous: "mapping reused" beside a step
   list with a gap in it still leaves the reader guessing whether the gap is
   the reuse or a lost record. So the reuse is stated (R2.1), the mapping is
   named (R2.2), and the absent step is stated (R2.3), each on its own line.

   WORDS CARRY THE DISTINCTION, NOT COLOUR. `LGR-99050e6-1` (template /
   ratified) records that colour alone fails WCAG 1.4.1, and `LGR-1f7892b-2`
   restates it — "a `warning` type *is* colour, so the sentence is the carrier
   and the type is not". Nothing below is a `StatusIndicator` or a `Badge`.

   Hoisted to module scope so the strings are one authority the tests assert
   against rather than literals duplicated into them — the same shape
   `ApprovalWorkflowSection`'s `NO_ACTOR_RECORDED` / `SYSTEM_TIMEOUT_ACTOR`
   take under `LGR-e59edd7-4`.
   ──────────────────────────────────────────────────────────────────────── */

/** R2.1 — that the mapping was reused. */
export const REUSED_MAPPING_STATEMENT =
  'This run reused a mapping an earlier run had already produced.';

/** R2.2 — which mapping. Composed with the id so nothing is invented. */
export function describeReusedMappingId(reusedMappingId: string | undefined): string {
  // Property 7 (p54 task 3.3) makes `true` beside a blank id unreachable on the
  // wire, so this fallback is not expected to fire from live data. It is here
  // because the alternative — rendering "The mapping it reused is ." — would
  // state an identity the component does not have, and a component cannot
  // assume its caller honoured a backend invariant.
  return reusedMappingId
    ? `The mapping it reused is ${reusedMappingId}.`
    : 'Which mapping it reused is not recorded.';
}

/** R2.3 — that the schema-mapping step did not run for this run. */
export const SCHEMA_MAPPING_STEP_ABSENT_STATEMENT =
  'The schema-mapping step did not run for this run.';

/**
 * The reuse arm's own `notEditableReason` — THE TRAP THIS SOLVES.
 *
 * `notEditableReason`'s third branch below emits "This run did not record which
 * mapping it used" whenever `runMappingId` is empty and `currentMappingId` is
 * set. On a Reuse_Run there is no `schema_mapping` state row, so `runMappingId`
 * is `''` — and the sentence is **false**: the run did record which mapping it
 * used, and `reusedMappingId` names it. Populating `mappingData` from the reused
 * mapping without overriding that sentence puts a false statement beside a
 * populated mapping.
 *
 * ROUTE TAKEN, AND WHY THE OTHER ONE WAS REJECTED. p54 task 4.3 permits either
 * this or forwarding `reused_mapping_id` as `runMappingId`. Forwarding was
 * rejected: `mappingIsCurrent` would then be `reusedMappingId ===
 * currentMappingId`, which on a Reuse_Run is *usually* true — the reused mapping
 * is generally the pipeline's current one — so the editor would silently become
 * available on a surface it has never been offered on, decided by a coincidence
 * rather than by a requirement. None of R2's criteria asks for editing; they ask
 * the page to *state* and to *identify*. Worse, `MappingEditorSection` would
 * then save `mappingData.mappings` against that id, and on this path
 * `mappingData` is the **origin approval payload's** snapshot of a mapping some
 * earlier run proposed — so a save would write an earlier run's proposal over a
 * record that may have been edited since. `LGR-99050e6-3` (template / ratified)
 * withholds the editor precisely to avoid a save whose outcome is "reverting the
 * pipeline to that run's older proposal", and it states that missing ids fail
 * closed. Withholding here is that posture honoured, not a limitation.
 */
export const REUSED_MAPPING_READ_ONLY =
  'This run reused an existing mapping rather than producing one, so its mappings are ' +
  "read-only here — edit the current mapping from the pipeline's Schema History.";

/**
 * The reuse arm with no mapping fields to show — an Honest_Absence, not an
 * empty mapping.
 *
 * Reached when the approval payload was marked unreadable, which
 * `_reused_mapping_payload` spells as `proposed_mappings: None`
 * (`approval_handler/lambda_function.py:441`). Rendering an empty `MappingViewer`
 * instead would present a named reference to something absent as *a mapping with
 * no fields* — the collapse `LGR-b2f79ef-14` prohibits and p54 do-NOT 7 forbids.
 * Falling through to "No mapping data available for this run." would be weaker
 * still: it is true, but it drops the reuse the three sentences above just
 * established. `LGR-2bcdd0d-3` is the in-tree precedent for spending a sentence
 * on exactly this distinction.
 */
export const REUSED_MAPPING_FIELDS_UNAVAILABLE =
  "The reused mapping's own fields could not be read, so none are shown here.";

/**
 * Schema & Mappings tab content extracted from RunDetailPage (P29 R11.5).
 * The regenerate/save-context callbacks (which use the pipeline mutation hooks)
 * stay owned by the parent and are forwarded through.
 *
 * p30 R14.2: offers the mapping editor, via the same `MappingEditorSection` that
 * `SchemaHistoryPage` renders — but conditionally, unlike that pipeline-scoped
 * page. See `canEditMappings` below.
 *
 * Note: `mappingData` here is built from the run's `schema_mapping` step and
 * carries no `source_schema_fields` / `target_schema_fields`, so the editor's
 * field pickers offer the fields already mapped or reported unmapped, not the
 * full schemas. Narrower than the Schema History surface, not broken.
 *
 * p54 R1.3 / R2.1-R2.3: a Reuse_Run has no `schema_mapping` step row at all, so
 * `mappingData` used to be `null` and this tab said "No mapping data available
 * for this run." — a true sentence that hid the reason. On that run the caller
 * passes the reused mapping's own fields as `mappingData` (sourced from the
 * approval payload, which is the origin run's own bytes) plus `reusedMapping` and
 * `reusedMappingId`, and a third arm states the reuse in three sentences. The
 * two-state absence rendering below survives unchanged for a run that genuinely
 * has no mapping data and no reuse.
 */
export default function SchemaTab({
  mappingData,
  extractedSchema,
  pipelineKey,
  runMappingId,
  currentMappingId,
  reusedMapping,
  reusedMappingId,
  initialContext,
  defaultModelId,
  isRegenerating,
  onSaveContext,
  onRerunMapping,
}: SchemaTabProps) {
  // Mappings are pipeline-level state, not run-level: PUT /pipelines/{id}/mappings
  // replaces the mappings array on ONE record in the mappings table, keyed by
  // mapping_id. This tab renders the mappings *this run proposed*, read from its
  // schema_mapping step row. Those are only the record the pipeline still uses
  // when this run is the one that produced it.
  //
  // So the affordance is offered only when the run's mapping_id is the pipeline's
  // current mapping_id. Offering it unconditionally would give one of two
  // confusing outcomes on an older run: saving against the pipeline's current
  // mapping_id silently reverts the pipeline to this run's proposal, and saving
  // against the run's own mapping_id edits a superseded record no later run
  // reads. Both look like a successful save. Missing ids fail closed.
  const mappingIsCurrent = Boolean(runMappingId && currentMappingId && runMappingId === currentMappingId);
  const canEditMappings = Boolean(pipelineKey) && mappingIsCurrent;
  const notEditableReason = canEditMappings
    ? undefined
    : runMappingId && currentMappingId
      ? 'These are the mappings this run proposed. The pipeline has since moved to a newer mapping, so they are read-only here — edit the current mapping from the pipeline\'s Schema History.'
      : currentMappingId && !runMappingId
        ? 'This run did not record which mapping it used, so its mappings are read-only here — edit them from the pipeline\'s Schema History.'
        : undefined;

  // ── p54 R1.3, R2.1-R2.3 — the reuse arm ──────────────────────────────────
  //
  // `mappingIsCurrent`, `canEditMappings` and `notEditableReason` above are
  // UNTOUCHED, so `LGR-99050e6-3`'s rule — the editor is offered only when the
  // run's mapping is the pipeline's current one — is preserved rather than
  // re-litigated. Nothing here forwards an id into `runMappingId`, which is what
  // would have changed their outcome without changing their code.
  //
  // FIRES ON `reusedMapping === true` ONLY, and the not-established case is
  // deliberately left to the existing absence rendering. Where `reusedMapping`
  // is absent, R2.1's and R2.2's sentences are both unsupported — nothing says
  // the mapping was reused and there is no id to name. R2.3's sentence happens
  // to be true whenever the step is absent, but this component cannot tell
  // "the step did not run" from "the step ran and proposed nothing": `mappingData`
  // is `null` in both cases, because its builder gates on `mappings.length > 0`
  // (`RunDetailPage.tsx`). Asserting R2.3 on `mappingData === null` would
  // therefore claim the step did not run for a run whose step did run — the
  // collapse p54 do-NOT 7 forbids. "No mapping data available for this run." is
  // already the right rendering there: it states that something is not present
  // without claiming why, which is `LGR-2bcdd0d-3`'s Honest_Absence.
  const mappingWasReused = reusedMapping === true;

  return (
    <SpaceBetween size="l">
      {/* R2.1, R2.2, R2.3 — three separate sentences, above the mapping they
          describe. See the constants at the top of this file for each one's
          criterion and for why they are not combined. */}
      {mappingWasReused ? (
        <Box color="text-body-secondary" data-testid="schema-reused-mapping">
          <SpaceBetween size="xxs">
            <Box variant="p">{REUSED_MAPPING_STATEMENT}</Box>
            <Box variant="p">{describeReusedMappingId(reusedMappingId)}</Box>
            <Box variant="p">{SCHEMA_MAPPING_STEP_ABSENT_STATEMENT}</Box>
          </SpaceBetween>
        </Box>
      ) : null}

      {/* Field Mappings — rerun, context, and (when current) the editor.
          THREE arms: the run's own mapping, a reused one with no readable
          fields, and the pre-existing two-state absence. */}
      {mappingData ? (
        <MappingEditorSection
          mappingData={mappingData}
          pipelineKey={pipelineKey ?? ''}
          mappingId={runMappingId}
          editable={canEditMappings}
          // The trap. On the reuse arm `runMappingId` is `''`, so the chain above
          // would emit "This run did not record which mapping it used" beside a
          // populated mapping — false, because `reusedMappingId` names it. See
          // `REUSED_MAPPING_READ_ONLY` for the route taken and the one rejected.
          notEditableReason={mappingWasReused ? REUSED_MAPPING_READ_ONLY : notEditableReason}
          initialContext={initialContext}
          defaultModelId={defaultModelId}
          isRegenerating={isRegenerating}
          onSaveContext={onSaveContext}
          onRerunMapping={onRerunMapping}
        />
      ) : mappingWasReused ? (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          {REUSED_MAPPING_FIELDS_UNAVAILABLE}
        </Box>
      ) : (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          {extractedSchema ? 'No mapping data available for this run.' : 'No schema was extracted in this run.'}
        </Box>
      )}

      {/* Extracted Schema — collapsible, below mappings */}
      {extractedSchema ? (
        <Container header={<Header variant="h3">Extracted Source Schema</Header>}>
          <ExpandableSection headerText="View raw schema JSON" variant="footer">
            <pre style={{
              backgroundColor: colorBackgroundContainerContent, color: colorTextBodyDefault,
              border: `1px solid ${colorBorderDividerDefault}`, padding: '12px',
              borderRadius: '4px', overflow: 'auto', maxHeight: '300px',
              fontSize: '12px', lineHeight: '1.4', fontFamily: 'monospace',
            }}>
              {(() => {
                try { return JSON.stringify(JSON.parse(extractedSchema), null, 2); }
                catch { return extractedSchema; }
              })()}
            </pre>
          </ExpandableSection>
        </Container>
      ) : null}
    </SpaceBetween>
  );
}
