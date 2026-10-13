// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useEffect } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import Textarea from '@cloudscape-design/components/textarea';
import Select from '@cloudscape-design/components/select';
import { getConfidenceLevel, getModelDisplayName } from '../utils/formatting';
import { useBedrockModels } from '../hooks';
import { selectAsyncStatus, selectFiltering } from './common/select-async-status';
import { NO_BEDROCK_MODELS } from '../utils/resource-empty-states';
import { requirementLabel, requirementRule } from '../utils/field-requirement';
import { consequenceStatement, reasonAttribution } from '../utils/unsourced-target-fields';
import type { FieldMapping, MappingData } from '../types';

/**
 * p45 R1.4 / R2.6 — the fallback statement, rendered **once above the list**.
 *
 * `unsourcedTargetFields` sets `not_established` for every entry exactly when
 * the payload carried no `target_required` (`utils/unsourced-target-fields.ts`,
 * step 4), so a uniformly-`not_established` list *is* the absent-authority
 * branch. Deriving the branch from the entries rather than from a new prop is
 * what keeps R7.6 true by construction: neither existing caller
 * (`ApprovalArtifact.tsx:767`, `MappingEditorSection.tsx:127`) gains a prop.
 *
 * **Per C15 this is the COMMON case today, not an edge case.** p37 is not
 * deployed (`backlog.md` item 20: *"Needs Compute + AgentCore"*) and the
 * Unsourced_Keys ship in that deploy, so until it lands every payload every
 * account produces takes this branch. Its copy gets the same care as the primary
 * path for that reason.
 *
 * `every` on an empty list is `true`, hence the length guard — the statement is
 * about "these fields" and there are none to be about.
 */
const REQUIREMENT_NOT_ESTABLISHED_STATEMENT =
  "The target schema's requirement for these fields was not established, so none of them is shown as required or optional.";

export interface MappingViewerProps {
  mappingData: MappingData;
  onRerunMapping?: (context: string, modelId?: string) => void;
  onEditMode?: () => void;
  initialContext?: string;
  defaultModelId?: string;
  isRegenerating?: boolean;
  onSaveContext?: (context: string) => Promise<void>;
}

function confidenceColor(level: 'high' | 'medium' | 'low'): 'green' | 'blue' | 'red' {
  if (level === 'high') return 'green';
  if (level === 'medium') return 'blue';
  return 'red';
}

export default function MappingViewer({
  mappingData,
  onRerunMapping,
  onEditMode,
  initialContext,
  defaultModelId,
  isRegenerating,
  onSaveContext,
}: MappingViewerProps) {
  const [rerunContext, setRerunContext] = useState(initialContext ?? '');
  const [storedContext, setStoredContext] = useState(initialContext ?? '');
  const [editingContext, setEditingContext] = useState(false);
  const [savingContext, setSavingContext] = useState(false);

  // Sync storedContext when initialContext changes (e.g., after refetch)
  useEffect(() => {
    setStoredContext(initialContext ?? '');
    setRerunContext(initialContext ?? '');
  }, [initialContext]);
  const [selectedModel, setSelectedModel] = useState(defaultModelId ?? '');
  const modelsQuery = useBedrockModels();
  const models = modelsQuery.data;

  const { mappings = [], unmapped_source = [], unmapped_target = [], ai_model_used } = mappingData ?? {};

  // Compute average confidence
  const mappingsWithConfidence = mappings.filter((m) => m.confidence != null);
  const avgConfidence =
    mappingsWithConfidence.length > 0
      ? mappingsWithConfidence.reduce((sum, m) => sum + (m.confidence ?? 0), 0) /
        mappingsWithConfidence.length
      : 0;

  const columnDefs = [
    {
      id: 'source',
      header: 'Source Field',
      cell: (item: FieldMapping) => item.source_field,
    },
    {
      id: 'target',
      header: 'Target Field',
      cell: (item: FieldMapping) => item.target_field,
    },
    {
      id: 'confidence',
      header: 'Confidence',
      cell: (item: FieldMapping) => {
        if (item.confidence == null) return '—';
        const level = getConfidenceLevel(item.confidence);
        const pct = Math.round(item.confidence * 100);
        return (
          <Badge color={confidenceColor(level)} data-testid="confidence-badge">
            {pct}% ({level})
          </Badge>
        );
      },
    },
    {
      id: 'hint',
      header: 'Hint',
      cell: (item: FieldMapping) =>
        item.hint ? (
          <span data-testid="hint-indicator" title={item.hint}>
            💡
          </span>
        ) : (
          '—'
        ),
    },
  ];

  return (
    <div data-testid="mapping-viewer">
      <SpaceBetween size="l">
        {/* Summary header */}
        <Container
          header={
            <Header
              variant="h3"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  {onEditMode && (
                    <Button onClick={onEditMode}>Edit Mappings</Button>
                  )}
                </SpaceBetween>
              }
            >
              Field Mappings
            </Header>
          }
        >
          <SpaceBetween direction="horizontal" size="l">
            <div key="model">
              <Box variant="awsui-key-label">AI Model</Box>
              <Box>{getModelDisplayName(ai_model_used)}</Box>
            </div>
            <div key="total">
              <Box variant="awsui-key-label">Total Mappings</Box>
              <Box>{mappings.length}</Box>
            </div>
            <div key="confidence">
              <Box variant="awsui-key-label">Average Confidence</Box>
              <Box>{mappingsWithConfidence.length > 0 ? `${Math.round(avgConfidence * 100)}%` : '—'}</Box>
            </div>
          </SpaceBetween>
        </Container>

        {/* Mappings table */}
        <Table
          items={mappings}
          columnDefinitions={columnDefs}
          trackBy="source_field"
          variant="embedded"
          wrapLines
          empty={
            <Box textAlign="center" color="text-body-secondary">
              No mappings available
            </Box>
          }
          header={<Header variant="h3">Mapped Fields ({mappings.length})</Header>}
        />

        {/* Unmapped source fields */}
        {unmapped_source.length > 0 && (
          <Container header={<Header variant="h3">Unmapped Source Fields ({unmapped_source.length})</Header>}>
            <SpaceBetween size="xxs">
              {unmapped_source.map((field, idx) => {
                const label = typeof field === 'string' ? field : (field as Record<string, unknown>)?.field ?? JSON.stringify(field);
                return (
                  <Box key={String(label) + idx} data-testid="unmapped-source">
                    <Badge color="grey">{String(label)}</Badge>
                  </Box>
                );
              })}
            </SpaceBetween>
          </Container>
        )}

        {/* Unmapped target fields — p45 R1.7, R2.1-R2.4, R2.6, R3.1-R3.6, R4.1,
            R4.6-R4.8.

            Three facts per entry, all of them **text** (C1, `LGR-99050e6-1`,
            **template / ratified**: *"Colour alone fails WCAG 1.4.1 and makes the
            inversion invisible"*): what the schema demands, what the column will
            contain, and why the mapper left the field alone. The `Badge` colour
            accompanies the name and carries nothing on its own; no icon and no
            emoji is introduced (do-NOT 3). `MappingViewer`'s bare `💡` on the
            Hint column above is C1's own explicit carve-out and was not copied
            here.

            **Every roster name renders, optional ones included** (R1.7). An
            approver must not be shown a filtered list whose filter is invisible,
            so nothing below drops an entry.

            **The cast is gone, and that is the point of this block.** The old
            body read `typeof field === 'string' ? field : (field as
            Record<string, unknown>)?.field` — an unsound cast that `tsc` reported
            as TS2352 once `unmapped_target` became `UnsourcedTargetField[]`,
            because the interface has no index signature. The tolerance now lives
            in exactly one place, `utils/unsourced-target-fields.ts` (do-NOT 2,
            design §4.1). The byte-identical twin in the
            `unmapped_source` block just above **stays**: for `string[]` the
            `typeof` false branch narrows to `never` and casts freely, so it never
            errored — which also means no gate will ever prompt cleaning it up
            (task 7.2's §2 artefact measured this). That asymmetry is
            do-NOT 19 working — `unmapped_source` is not widened here or anywhere,
            because both producers write bare names.

            **One rendered element per entry** (`data-testid="unmapped-target"`),
            unchanged, because `MappingViewer.property.test.tsx`'s count assertion
            is the only existing pin on this surface (C16, do-NOT 20). */}
        {unmapped_target.length > 0 && (
          <Container header={<Header variant="h3">Unmapped Target Fields ({unmapped_target.length})</Header>}>
            <SpaceBetween size="m">
              {unmapped_target.every((entry) => entry.requirement === 'not_established') && (
                <Box variant="p" data-testid="unmapped-target-requirement-not-established">
                  {REQUIREMENT_NOT_ESTABLISHED_STATEMENT}
                </Box>
              )}
              {unmapped_target.map((entry, idx) => {
                // R2.4 — the **rule**, not a per-run derivation, and only for a
                // required field. `requirementRule` returns `undefined` for the
                // other two states; the routing gate itself stays in
                // `determine_approval_requirement:1290-1296` and is not
                // re-derived here (do-NOT 2).
                const rule = requirementRule(entry.requirement);
                return (
                  <Box key={entry.field + idx} data-testid="unmapped-target">
                    <SpaceBetween size="xxs">
                      <Badge color="grey">{entry.field}</Badge>
                      {/* The testid is the total derivation
                          `requirement-${requirement.replace('_', '-')}`
                          documented in `utils/field-requirement.ts`'s own table.
                          Written at the render site because that module exports
                          the words, not the handles — the same split
                          `SchemaManagementPage.tsx:236` uses for the Fields-table
                          cell. */}
                      <Box data-testid={`requirement-${entry.requirement.replace('_', '-')}`}>
                        {requirementLabel(entry.requirement)}
                      </Box>
                      {rule && (
                        <Box variant="small" data-testid="unmapped-target-requirement-rule">
                          {rule}
                        </Box>
                      )}
                      <Box data-testid="unmapped-target-consequence">{consequenceStatement(entry)}</Box>
                      {entry.reason !== undefined && (
                        /* R4.8, C5, `LGR-acca05d-3` — the **third** prose
                           authority on this row, under its own heading and never
                           merged with the other two. `LGR-acca05d-3` accepted *"a
                           second authority for a field's description alongside the
                           schema's own, which Requirement 1.6 keeps legible by
                           putting them under distinct headings"*; the schema's
                           `description` and the Context_Sidecar's `context` are
                           those two, and `_schema_detail:1568-1573` keeps them
                           apart on the server. This is the third, and
                           `reasonAttribution` puts the provenance in the heading
                           precisely so the body can be the mapper's own words
                           **verbatim** — no wrapper, no clamp, no `slice`
                           (R4.1, do-NOT 14). */
                        <div>
                          <Box variant="awsui-key-label" data-testid="unmapped-target-reason-heading">
                            {reasonAttribution(entry.reason)}
                          </Box>
                          <Box data-testid="unmapped-target-reason">{entry.reason}</Box>
                        </div>
                      )}
                    </SpaceBetween>
                  </Box>
                );
              })}
            </SpaceBetween>
          </Container>
        )}

        {/* Stored Mapping Context — persisted on the pipeline record */}
        {onSaveContext && (
          <Container header={
            <Header variant="h3" actions={
              <SpaceBetween direction="horizontal" size="xs">
                {!editingContext && storedContext && (
                  <Button key="edit" variant="icon" iconName="edit" ariaLabel="Edit context" onClick={() => setEditingContext(true)} />
                )}
                {!editingContext && storedContext && (
                  <Button key="clear" variant="icon" iconName="remove" ariaLabel="Clear context" onClick={async () => {
                    setSavingContext(true);
                    try { await onSaveContext(''); setStoredContext(''); setRerunContext(''); } finally { setSavingContext(false); }
                  }} loading={savingContext} />
                )}
                {!editingContext && !storedContext && (
                  <Button key="add" onClick={() => setEditingContext(true)}>Add Context</Button>
                )}
              </SpaceBetween>
            }>Mapping Context</Header>
          }>
            {editingContext ? (
              <SpaceBetween size="s">
                <Textarea
                  value={storedContext}
                  onChange={({ detail }) => setStoredContext(detail.value)}
                  placeholder="Instructions for the AI when generating mappings (e.g., 'Map state to status, mobile to phone')"
                  rows={3}
                />
                <Box variant="small" color="text-body-secondary">{storedContext.length}/2000</Box>
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="primary" loading={savingContext} onClick={async () => {
                    setSavingContext(true);
                    try { await onSaveContext(storedContext); setRerunContext(storedContext); setEditingContext(false); } finally { setSavingContext(false); }
                  }}>Save</Button>
                  <Button variant="link" onClick={() => { setStoredContext(initialContext ?? ''); setEditingContext(false); }}>Cancel</Button>
                </SpaceBetween>
              </SpaceBetween>
            ) : storedContext ? (
              <Box variant="p">{storedContext}</Box>
            ) : (
              <Box color="text-body-secondary">No mapping context set. Add instructions to guide the AI during mapping generation.</Box>
            )}
          </Container>
        )}

        {/* Rerun Mapping action */}
        {onRerunMapping && (
          <Container header={<Header variant="h3">Rerun Mapping</Header>}>
            <SpaceBetween size="s">
              <Textarea
                value={rerunContext}
                onChange={({ detail }) => setRerunContext(detail.value)}
                placeholder="Provide additional context for AI-driven mapping regeneration..."
                rows={3}
              />
              <Box variant="small" color="text-body-secondary">
                {rerunContext.length}/2000
              </Box>
              <Select
                selectedOption={selectedModel ? { value: selectedModel, label: (models ?? []).find(m => m.model_id === selectedModel)?.model_name || selectedModel } : null}
                onChange={({ detail }) => setSelectedModel(detail.selectedOption.value ?? '')}
                options={(models ?? []).map(m => ({ value: m.model_id, label: m.model_name || m.model_id }))}
                placeholder="Select model (default: pipeline model)"
                data-testid="model-selector"
                {...selectAsyncStatus(modelsQuery, 'models', NO_BEDROCK_MODELS)}
                {...selectFiltering('models', (models ?? []).length)}
              />
              <Button variant="primary" onClick={() => onRerunMapping(rerunContext, selectedModel || undefined)} loading={isRegenerating} disabled={isRegenerating}>
                {isRegenerating ? 'Regenerating...' : 'Rerun Mapping'}
              </Button>
            </SpaceBetween>
          </Container>
        )}
      </SpaceBetween>
    </div>
  );
}
