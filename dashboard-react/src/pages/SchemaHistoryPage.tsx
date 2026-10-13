// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Modal from '@cloudscape-design/components/modal';
import Pagination from '@cloudscape-design/components/pagination';
import Select from '@cloudscape-design/components/select';
import Tabs from '@cloudscape-design/components/tabs';
import { useRegenerateMappings, useEditPipeline, useApprovals, useApprovalAction, useMappingRegenPolling } from '../hooks';
import { formatRelativeTime } from '../utils/formatting';
import { unsourcedTargetFields } from '../utils/unsourced-target-fields';
import { useToast } from '../contexts/ToastContext';
import { usePipelineContext } from '../contexts/PipelineContext';
import MappingEditorSection from '../components/MappingEditorSection';
import MappingVersionNav from '../components/MappingVersionNav';
import ClickableArea from '../components/common/ClickableArea';
import WorksheetSelectionEditor from '../components/WorksheetSelectionEditor';
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../utils/worksheet-selection';
import CodeViewer from '../components/CodeViewer';
import TestResultsViewer from '../components/TestResultsViewer';
import SchemaDiffViewer from '../components/SchemaDiffViewer';
import AgentOutputDiff from '../components/AgentOutputDiff';
import type { Approval, SchemaVersion, MappingData, SchemaDifference, FieldMapping } from '../types';

const VERSIONS_PER_PAGE = 10;

/**
 * Whether this pipeline reads a JDBC database, and so has no workbook and no
 * worksheet for the Selected_Worksheet card to be about.
 *
 * ## An absent `source_type` is deliberately NOT JDBC
 *
 * `Pipeline.source_type` is optional on the type, but every read path in
 * `handlers/pipelines_crud.py` projects it with an explicit
 * `pipeline_item.get("source_type", "S3_UPLOAD")` default — the pipeline detail
 * response this page's `pipeline` comes from at `:2808`, the list at `:2536`,
 * and the logs enrichment at `:1310` — because P17 R19 requires pre-P17 records
 * to keep reporting a valid source type. So absence cannot arrive from the
 * server at all; it can only come from a hand-built object or a response older
 * than that default. Reading it as S3_UPLOAD matches what the producer would
 * have said, and matches `SourceConfigDisplay.tsx:53`'s
 * `pipeline.source_type ?? 'S3_UPLOAD'` rather than adding a second reading of
 * the same absence. The asymmetry of the mistake settles it either way: showing
 * a collapsed section to a JDBC pipeline costs one unread line, while hiding it
 * from an S3-upload pipeline removes the only surface that can read or clear a
 * worksheet.
 *
 * ## Why the comparison is case-folded
 *
 * `create_pipeline` validates the submitted value against
 * `_ALLOWED_SOURCE_TYPES = frozenset({"S3_UPLOAD", "JDBC"})`
 * (`handlers/pipelines_crud.py:378`), so the only spelling that can be stored is
 * the uppercase literal, and `:3032` compares it exactly. The fold here follows
 * `CreatePipelinePage.tsx:231`, which already reads a persisted draft's
 * `source_type` with `.toString().toUpperCase() === 'JDBC'` — one reading of the
 * field rather than two of differing strictness. It cannot cost anything,
 * because a configured worksheet is tested **before** this: a wrong "yes" hides
 * a card that has nothing on it, never a card holding a recorded value.
 */
function isJdbcSource(sourceType: string | undefined): boolean {
  return (sourceType ?? '').trim().toUpperCase() === 'JDBC';
}

export default function SchemaHistoryPage() {
  const { pipeline, refetch } = usePipelineContext();
  const { showToast } = useToast();
  const regenerateMappings = useRegenerateMappings();
  const editPipeline = useEditPipeline();
  const { data: approvalsData } = useApprovals();
  const approvalAction = useApprovalAction();

  // Find pending schema_mapping approval for this pipeline
  const pendingApproval = useMemo(() => {
    const allApprovals = approvalsData?.pages.flatMap((p) => p.approvals) ?? [];
    if (!allApprovals.length || !pipeline.pipeline_key) return null;
    return allApprovals.find(
      (a: Approval) => {
        const approvalPipelineId = a.pipeline_key || (a as any).pipeline_id || '';
        return approvalPipelineId === pipeline.pipeline_key
          && a.approval_type === 'schema_mapping'
          && a.status === 'pending';
      },
    ) ?? null;
  }, [approvalsData, pipeline.pipeline_key]);

  const [selectedVersionIndex, setSelectedVersionIndex] = useState<number | null>(null);
  const [versionPage, setVersionPage] = useState(1);
  const [mappingVersionIndex, setMappingVersionIndex] = useState(0);
  const [waitingForRegen, setWaitingForRegen] = useState(false);
  const [activeTabId, setActiveTabId] = useState('mappings');
  const [selectedRunIndex, setSelectedRunIndex] = useState(0);
  const [compareModalVisible, setCompareModalVisible] = useState(false);
  const [compareVersionA, setCompareVersionA] = useState<string | null>(null);
  const [compareVersionB, setCompareVersionB] = useState<string | null>(null);

  // Mapping-regen polling (R7.1): effect-scoped so the interval is cleared on
  // unmount and stops when the pipeline's mapping_id changes (regeneration
  // complete), with a 60s safety-net cap — replacing the old orphaned inline
  // setInterval + blind 120s setTimeout in onRerunMapping below.
  useMappingRegenPolling({
    active: waitingForRegen,
    completionSignal: pipeline.mapping_id,
    onPoll: () => { refetch(); },
    onStop: () => { setWaitingForRegen(false); refetch(); },
  });

  const schemaHistory = pipeline.schema_history ?? [];
  const totalVersionPages = Math.max(1, Math.ceil(schemaHistory.length / VERSIONS_PER_PAGE));
  const pagedVersions = schemaHistory.slice((versionPage - 1) * VERSIONS_PER_PAGE, versionPage * VERSIONS_PER_PAGE);

  const activeVersion: SchemaVersion | undefined =
    selectedVersionIndex !== null ? schemaHistory[selectedVersionIndex] : schemaHistory[schemaHistory.length - 1];

  const mappingData: MappingData | null = useMemo(() => {
    if (!activeVersion?.has_mapping || !activeVersion?.mapping_details) return null;
    const details = activeVersion.mapping_details;
    return {
      mappings: details.mappings ?? [],
      unmapped_source: ((details.unmapped_source_fields ?? []) as unknown[]).map(
        (f) => (typeof f === 'string' ? f : (f as Record<string, unknown>).field as string),
      ),
      // p45 R4.5 — stop discarding the Unmapped_Entry object. The page's own
      // `typeof f === 'string' ? f : (f as Record<string, unknown>).field` test
      // used to live here; it was the evidence that
      // `SchemaVersion.mapping_details.unmapped_target_fields`' `string[]`
      // declaration did not match the wire, and both were fixed together (that
      // key is now `unknown[]` — a raw wire field, deliberately not the
      // normalised shape, see its doc comment in `types.ts`).
      //
      // **This surface has no Unsourced_Keys, and that is structural rather than
      // an omission.** `activeVersion.mapping_details` is a **mapping record**,
      // not an approval payload: `target_unsourced` and `target_required` are
      // computed in `create_approval_payload` (`:1395-1396`) and stored on the
      // approval, while the mapping record carries `unmapped_target_fields` plus
      // `target_schema_hash` — not the schema and not the derived name lists. So
      // every entry read here is `not_established`, and this page renders the
      // consequence and the reason while making **no required/optional claim at
      // all**. R2.6 is the criterion that makes that correct rather than broken
      // (design §4.8's per-surface table).
      //
      // Do NOT propose plumbing to close it. Writing the derived lists onto the
      // mapping record recreates the second-authority problem `LGR-b2f79ef-4`
      // (**template / ratified**) refused for `target_schema` on that same
      // record — *"The pipeline record stays the single authority"* — and
      // re-deriving them in TypeScript from `mappings` is do-NOT 2, a second
      // definition of "unsourced" in a second language. (`LGR-b2f79ef-4`'s
      // other ground, DynamoDB's 400 KB item cap, is about a schema-sized blob
      // and does not transfer to two short name lists; the single-authority
      // ground is the one that does.)
      unmapped_target: unsourcedTargetFields({ target: details.unmapped_target_fields }),
      schema_hash: activeVersion.schema_hash ?? '',
      version_index: activeVersion.version,
      ai_model_used: activeVersion.ai_model_used,
      created_at: activeVersion.created,
      source_schema_fields: (details.source_schema_fields as string[]) ?? undefined,
      target_schema_fields: (details.target_schema_fields as string[]) ?? undefined,
    };
  }, [activeVersion]);

  const activeRuns = activeVersion?.runs ?? [];
  const totalMappingVersions = Math.max(1, activeRuns.length);
  const activeRun = activeRuns[selectedRunIndex] ?? activeRuns[0];
  const activeCodeVersionId = activeRun?.code_version_id;

  // --- Compare modal helpers ---
  const versionOptions = schemaHistory.map((v) => ({
    value: String(v.version),
    label: `v${v.version} — ${formatRelativeTime(v.created)}`,
  }));

  const openCompareModal = () => {
    // Default to the two most recent versions
    if (schemaHistory.length >= 2) {
      setCompareVersionA(String(schemaHistory[schemaHistory.length - 2].version));
      setCompareVersionB(String(schemaHistory[schemaHistory.length - 1].version));
    }
    setCompareModalVisible(true);
  };

  const comparisonDifferences: SchemaDifference[] = useMemo(() => {
    if (!compareVersionA || !compareVersionB) return [];
    const versionA = schemaHistory.find((v) => String(v.version) === compareVersionA);
    const versionB = schemaHistory.find((v) => String(v.version) === compareVersionB);
    if (!versionA || !versionB) return [];

    // Client-side diff: compare extracted_schema fields between two versions
    const parseFields = (version: SchemaVersion): Record<string, string> => {
      const raw = version.extracted_schema;
      if (!raw) return {};
      try {
        const schema = typeof raw === 'string' ? JSON.parse(raw) : raw;
        const props = schema.properties ?? schema.fields ?? schema;
        if (typeof props !== 'object' || props === null) return {};
        const fields: Record<string, string> = {};
        for (const [key, val] of Object.entries(props)) {
          if (typeof val === 'object' && val !== null) {
            fields[key] = (val as Record<string, unknown>).type as string ?? 'unknown';
          } else {
            fields[key] = String(val ?? 'unknown');
          }
        }
        return fields;
      } catch {
        return {};
      }
    };

    const fieldsA = parseFields(versionA);
    const fieldsB = parseFields(versionB);
    const allFieldNames = new Set([...Object.keys(fieldsA), ...Object.keys(fieldsB)]);
    const diffs: SchemaDifference[] = [];

    for (const field of allFieldNames) {
      const inA = field in fieldsA;
      const inB = field in fieldsB;
      if (inA && !inB) {
        diffs.push({ field_name: field, change_type: 'removed', old_type: fieldsA[field] });
      } else if (!inA && inB) {
        diffs.push({ field_name: field, change_type: 'added', new_type: fieldsB[field] });
      } else if (fieldsA[field] !== fieldsB[field]) {
        diffs.push({ field_name: field, change_type: 'modified', old_type: fieldsA[field], new_type: fieldsB[field] });
      } else {
        diffs.push({ field_name: field, change_type: 'unchanged', new_type: fieldsB[field] });
      }
    }

    // Sort: added/removed/modified first, then unchanged
    const order: Record<string, number> = { added: 0, removed: 1, modified: 2, unchanged: 3 };
    diffs.sort((a, b) => (order[a.change_type] ?? 9) - (order[b.change_type] ?? 9));
    return diffs;
  }, [compareVersionA, compareVersionB, schemaHistory]);

  // Mapping diff for compare modal
  const mappingDiffData = useMemo(() => {
    if (!compareVersionA || !compareVersionB) return null;
    const versionA = schemaHistory.find((v) => String(v.version) === compareVersionA);
    const versionB = schemaHistory.find((v) => String(v.version) === compareVersionB);
    if (!versionA || !versionB) return null;

    const formatMappings = (version: SchemaVersion): string => {
      const details = version.mapping_details;
      if (!details?.mappings) return 'No mappings available';
      return (details.mappings as FieldMapping[])
        .map((m) => `${m.source_field ?? '?'} → ${m.target_field ?? '?'}${m.transformation ? ` (${m.transformation})` : ''}`)
        .join('\n');
    };

    return {
      contentA: formatMappings(versionA),
      contentB: formatMappings(versionB),
    };
  }, [compareVersionA, compareVersionB, schemaHistory]);

  // Which of the three shapes the worksheet card takes on this pipeline. See
  // the comment at its render site below for why each one.
  //
  // `!== undefined` rather than a truthiness test, for the same reason the
  // editor itself never uses `||`: `undefined` (no attribute) and `''` (recorded
  // empty) are opposite facts, and `''` is the one that refuses **every** run
  // under R8.5. Folding them together here would put the only route out of that
  // state behind a chevron, or — through the JDBC arm — remove it outright.
  const configuredWorksheet = pipeline[SELECTED_WORKSHEET_REQUEST_KEY];
  const worksheetIsConfigured = configuredWorksheet !== undefined;
  // Order is load-bearing: a configured value wins over the source type, so a
  // JDBC pipeline that somehow carries a worksheet still gets the card. It is
  // the only surface that can clear one.
  const showWorksheetCard = worksheetIsConfigured || !isJdbcSource(pipeline.source_type);
  const collapseWorksheetCard = !worksheetIsConfigured;

  const worksheetEditor = (
    <WorksheetSelectionEditor
      configured={configuredWorksheet}
      saving={editPipeline.isPending}
      onSave={async (next) => {
        if (!pipeline.pipeline_key) return;
        try {
          await editPipeline.mutateAsync({
            id: pipeline.pipeline_key,
            [SELECTED_WORKSHEET_REQUEST_KEY]: next,
          });
          refetch();
          // The two outcomes are reported separately because they are
          // different facts on the record: a name was set, or the attribute
          // was removed.
          showToast(next ? `Worksheet set to "${next}"` : 'Worksheet selection cleared', 'success');
        } catch {
          showToast('Failed to save the worksheet selection', 'error');
        }
      }}
    />
  );

  return (
    <SpaceBetween size="l">
      {/* p55 R8.4 — the Selected_Worksheet's read and edit surface.
          On the Schema tab because the worksheet decides which sheet the schema
          is extracted FROM, and because `schema_extract`'s R8.4 / R8.5 refusals
          are what send an operator here. Before this existed the attribute was
          write-only at creation time, so the refusal named a fix the product did
          not offer. That placement is unchanged, and `schema_extract`'s refusals
          still name this tab.
          `pipeline.selected_worksheet` is passed straight through: `undefined`
          (no attribute) and `''` (recorded empty) mean opposite things and the
          editor renders them as different states, so nothing may `||` them
          together on the way in.

          What IS new is that it no longer renders unconditionally. It used to be
          the first thing on this tab for every pipeline, including JDBC ones
          that have no workbook at all and CSV-only uploads that will never need
          it — which read as a required step in the way of the schema history.
          Three cases, in this precedence:

          1. **A worksheet is configured** (a name, or the recorded `''`) → the
             bare card, uncollapsed. Deliberately not `defaultExpanded` inside a
             collapsible: a value an operator has already set must be visible,
             editable and clearable with no clicks, and `''` in particular is a
             state that refuses every run, so hiding its only exit would make
             setting a worksheet a trap. Bare rather than expanded also avoids
             wrapping the editor's own `Container` header in a second one.
          2. **No worksheet, and the source is JDBC** → nothing. A JDBC source
             has no workbook, so there is no worksheet to choose and no refusal
             that could send anyone here.
          3. **No worksheet, any other source** → collapsed. Still discoverable,
             and still the place `schema_extract`'s refusal points at, without
             dominating the tab for the CSV case.

          No status affordance is attached to the collapsed header, so there is
          no distinction being drawn here for `LGR-99050e6-1` to govern. If one
          is ever added, it has to be carried in words and not by colour alone. */}
      {showWorksheetCard && (collapseWorksheetCard ? (
        <ExpandableSection
          headerText="Excel worksheet (optional)"
          headerDescription="Only used when this pipeline ingests an .xlsx workbook with more than one worksheet. CSV, JSON and Parquet uploads ignore it."
          data-testid="worksheet-selection-collapsible"
        >
          {worksheetEditor}
        </ExpandableSection>
      ) : (
        worksheetEditor
      ))}

      {/* Schema Evolution Timeline */}
      {schemaHistory.length === 0 ? (
        <Box color="text-body-secondary">No schema versions available</Box>
      ) : (
        <SpaceBetween size="s">
          <div data-testid="version-timeline">
            <SpaceBetween direction="horizontal" size="xxs">
              {pagedVersions.map((v) => {
                const globalIdx = schemaHistory.indexOf(v);
                const isSelected = selectedVersionIndex === globalIdx;
                const isLatest = globalIdx === schemaHistory.length - 1;
                return (
                  <ClickableArea
                    as="span"
                    key={v.version}
                    data-testid="version-dot"
                    ariaLabel={`Select schema version ${v.version}`}
                    onClick={() => { setSelectedVersionIndex(globalIdx); setMappingVersionIndex(0); setSelectedRunIndex(0); }}
                  >
                    <Badge color={isSelected ? 'blue' : 'grey'}>v{v.version}{isLatest && ' (current)'}</Badge>
                  </ClickableArea>
                );
              })}
            </SpaceBetween>
          </div>
          {totalVersionPages > 1 && (
            <Pagination currentPageIndex={versionPage} pagesCount={totalVersionPages} onChange={({ detail }) => setVersionPage(detail.currentPageIndex)} />
          )}
          {activeVersion && (
            <ColumnLayout columns={3} variant="text-grid">
              <div><Box variant="awsui-key-label">Version</Box><Box>v{activeVersion.version}</Box></div>
              <div><Box variant="awsui-key-label">Created</Box><Box>{formatRelativeTime(activeVersion.created)}</Box></div>
              <div><Box variant="awsui-key-label">Mappings</Box><Box data-testid="version-mapping-count">{activeVersion.mapping_count}</Box></div>
            </ColumnLayout>
          )}
          {schemaHistory.length >= 2 && (
            <Button data-testid="compare-button" onClick={openCompareModal}>Compare</Button>
          )}
        </SpaceBetween>
      )}

      {/* Mappings / Code / Tests inner Tabs */}
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'mappings', label: 'Mappings',
            content: (
              <SpaceBetween size="m">
                {pendingApproval && (
                  <Alert
                    type="warning"
                    header="Mapping approval required"
                    action={
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          variant="primary"
                          loading={approvalAction.isPending}
                          onClick={() => {
                            approvalAction.mutate(
                              { id: pendingApproval.approval_id, action: 'approve' },
                              {
                                onSuccess: () => { showToast('Mapping approved', 'success'); refetch(); },
                                onError: () => showToast('Failed to approve mapping', 'error'),
                              },
                            );
                          }}
                        >
                          Approve
                        </Button>
                        <Button
                          loading={approvalAction.isPending}
                          onClick={() => {
                            approvalAction.mutate(
                              { id: pendingApproval.approval_id, action: 'reject' },
                              {
                                onSuccess: () => { showToast('Mapping rejected', 'success'); refetch(); },
                                onError: () => showToast('Failed to reject mapping', 'error'),
                              },
                            );
                          }}
                        >
                          Reject
                        </Button>
                      </SpaceBetween>
                    }
                  >
                    This mapping is waiting for approval before the pipeline can continue.
                  </Alert>
                )}
                {totalMappingVersions > 1 && (
                  <MappingVersionNav currentIndex={mappingVersionIndex} totalVersions={totalMappingVersions}
                    onPrevious={() => setMappingVersionIndex((i) => Math.max(0, i - 1))}
                    onNext={() => setMappingVersionIndex((i) => Math.min(totalMappingVersions - 1, i + 1))} />
                )}
                {mappingData ? (
                  <MappingEditorSection mappingData={mappingData}
                    pipelineKey={pipeline.pipeline_key}
                    mappingId={pipeline.mapping_id}
                    versionIndex={mappingVersionIndex}
                    initialContext={pipeline.mapping_context}
                    defaultModelId={pipeline.ai_model}
                    isRegenerating={regenerateMappings.isPending || waitingForRegen}
                    onSaveContext={async (context) => {
                      if (pipeline.pipeline_key) {
                        await editPipeline.mutateAsync({ id: pipeline.pipeline_key, mapping_context: context });
                        refetch();
                        showToast(context ? 'Mapping context saved' : 'Mapping context cleared', 'success');
                      }
                    }}
                    onRerunMapping={(context, modelId) => {
                      if (pipeline.pipeline_key) {
                        setWaitingForRegen(true);
                        regenerateMappings.mutate({ pipelineKey: pipeline.pipeline_key, context, model_id: modelId },
                          {
                            onSuccess: () => {
                              // Polling is handled by useMappingRegenPolling (effect-scoped,
                              // status-based stop on mapping_id change, unmount-safe) — no
                              // orphaned inline setInterval/setTimeout here. The locally-saved
                              // mapping override is dropped by MappingEditorSection when the
                              // new mapping_id arrives.
                              showToast('Mapping regeneration started — will auto-refresh when complete', 'info');
                            },
                            onError: () => { setWaitingForRegen(false); showToast('Failed to start mapping regeneration', 'error'); },
                          });
                      }
                    }} />
                ) : (
                  <Box color="text-body-secondary" data-testid="mapping-section">
                    {activeVersion && activeVersion.has_mapping === false && activeVersion.mapping_count === 0
                      ? pipeline.schema_history && pipeline.schema_history.length > 1
                        ? 'Schema unchanged — mapping was reused from a previous run'
                        : 'Mapping not yet available — the pipeline is still processing'
                      : activeVersion
                        ? 'Loading mapping data...'
                        : 'Select a schema version to view mappings'}
                  </Box>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: 'code', label: `Code${activeRuns.length > 1 ? ` (${activeRuns.length} runs)` : ''}`,
            content: (
              <SpaceBetween size="m">
                {activeRuns.length > 1 && (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {activeRuns.map((run, idx) => (
                      <ClickableArea
                        as="span"
                        key={run.run_id}
                        ariaLabel={`View run ${activeRuns.length - idx}`}
                        onClick={(e) => { e.stopPropagation(); setSelectedRunIndex(idx); }}
                      >
                        <Badge color={selectedRunIndex === idx ? 'blue' : 'grey'}>Run {activeRuns.length - idx}{run.test_status ? ` (${run.test_status})` : ''}</Badge>
                      </ClickableArea>
                    ))}
                  </SpaceBetween>
                )}
                {activeCodeVersionId ? (
                  <CodeViewer codeVersionId={activeCodeVersionId} schemaVersion={activeVersion?.version} />
                ) : (
                  <Box color="text-body-secondary">No code version available for the selected schema version.</Box>
                )}
              </SpaceBetween>
            ),
          },
          {
            id: 'tests', label: 'Tests',
            content: activeCodeVersionId ? (
              <TestResultsViewer codeVersionId={activeCodeVersionId} />
            ) : (
              <Box color="text-body-secondary">No test results available for the selected schema version.</Box>
            ),
          },
        ]}
      />

      {/* Schema Compare Modal */}
      <Modal
        visible={compareModalVisible}
        onDismiss={() => setCompareModalVisible(false)}
        header="Compare Schema Versions"
        size="large"
        data-testid="compare-modal"
      >
        <SpaceBetween size="m">
          <SpaceBetween direction="horizontal" size="m">
            <div>
              <Box variant="awsui-key-label">Version A</Box>
              <Select
                selectedOption={compareVersionA ? versionOptions.find((o) => o.value === compareVersionA) ?? null : null}
                onChange={({ detail }) => setCompareVersionA(detail.selectedOption.value ?? null)}
                options={versionOptions}
                placeholder="Select version"
                data-testid="compare-version-a"
              />
            </div>
            <div>
              <Box variant="awsui-key-label">Version B</Box>
              <Select
                selectedOption={compareVersionB ? versionOptions.find((o) => o.value === compareVersionB) ?? null : null}
                onChange={({ detail }) => setCompareVersionB(detail.selectedOption.value ?? null)}
                options={versionOptions}
                placeholder="Select version"
                data-testid="compare-version-b"
              />
            </div>
          </SpaceBetween>
          <SchemaDiffViewer differences={comparisonDifferences} />
          {mappingDiffData && mappingDiffData.contentA !== mappingDiffData.contentB && (
            <AgentOutputDiff
              labelA={`v${compareVersionA} Mappings`}
              labelB={`v${compareVersionB} Mappings`}
              contentA={mappingDiffData.contentA}
              contentB={mappingDiffData.contentB}
              contentType="mapping"
            />
          )}
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
}
