// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Mapping view + editor, with the save wiring in one place (p30 R14.2).
 *
 * `SchemaHistoryPage` wired `MappingViewer`'s `onEditMode` to
 * `DragDropMappingEditor` and a real `useUpdateMappings` mutation. R14.2 adds the
 * same affordance to the run-detail Schema tab. Rather than a second copy of that
 * ~35-line switch (two copies of an editor invocation drift), both surfaces render
 * this component.
 *
 * What it owns: the view/edit toggle, the `PUT /pipelines/{id}/mappings` mutation,
 * the success/failure toasts, and the local override that keeps just-saved
 * mappings on screen. The override is necessary because the write lands on the
 * mappings table while both callers read their mappings from state rows, so a
 * refetch would show the pre-edit values.
 *
 * What it does NOT own: whether editing is appropriate. Mappings are
 * pipeline-level state keyed by `mapping_id`, so the caller passes `editable` —
 * see `run-detail/SchemaTab.tsx` for the run-scoped rule.
 */

import { useEffect, useState } from 'react';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import MappingViewer from './MappingViewer';
import DragDropMappingEditor from './DragDropMappingEditor';
import { useUpdateMappings } from '../hooks';
import { useToast } from '../contexts/ToastContext';
import type { FieldMapping, MappingData } from '../types';

export interface MappingEditorSectionProps {
  mappingData: MappingData;
  /** Pipeline the mappings belong to — the `{id}` in PUT /pipelines/{id}/mappings. */
  pipelineKey: string;
  /** Mapping record the save targets. Also the reset key for the local override. */
  mappingId?: string;
  versionIndex?: number;
  /** Withhold the Edit Mappings affordance. Defaults to offering it. */
  editable?: boolean;
  /** Why editing is unavailable, so its absence is explained rather than silent. */
  notEditableReason?: string;
  initialContext?: string;
  defaultModelId?: string;
  isRegenerating?: boolean;
  onSaveContext?: (context: string) => Promise<void>;
  onRerunMapping?: (context: string, modelId?: string) => void;
}

export default function MappingEditorSection({
  mappingData,
  pipelineKey,
  mappingId,
  versionIndex = 0,
  editable = true,
  notEditableReason,
  initialContext,
  defaultModelId,
  isRegenerating,
  onSaveContext,
  onRerunMapping,
}: MappingEditorSectionProps) {
  const { showToast } = useToast();
  const updateMappings = useUpdateMappings();
  const [editMode, setEditMode] = useState(false);
  const [savedMappings, setSavedMappings] = useState<FieldMapping[] | null>(null);

  // Drop the local override when the mapping record changes — a regeneration
  // produces a new mapping_id, at which point the server values are the fresh
  // ones and the override is stale.
  useEffect(() => {
    setSavedMappings(null);
  }, [mappingId]);

  const displayed: MappingData = savedMappings
    ? { ...mappingData, mappings: savedMappings }
    : mappingData;

  if (editMode) {
    return (
      <DragDropMappingEditor
        mappings={displayed.mappings}
        unmappedSource={displayed.unmapped_source}
        // p45 C11 / do-NOT 15 — extract the field **names** here, and do NOT
        // "simplify" this back to `displayed.unmapped_target`. The editor's
        // `unmappedTarget` prop is `string[]` (`DragDropMappingEditor.tsx:20`):
        // it spreads the list into a `Set<string>` (`:64-70`) and uses each
        // member as a Cloudscape `Select` option's `label` **and** `value`
        // (`:183-187`). An `UnsourcedTargetField` reaching it therefore renders
        // `[object Object]` as a selectable target field, and choosing it saves
        // that string into `FieldMapping.target_field` via `handleSave`
        // (`:105-115`).
        //
        // Extracting at the call site rather than inside the editor keeps the
        // editor's own props **and its subject — field names — unchanged**,
        // which is what R7.6 asks of the existing surfaces. The entry's
        // `requirement`, `default_value` and `reason` are for the read
        // surfaces; a dropdown of target field names has no use for them.
        unmappedTarget={displayed.unmapped_target.map((e) => e.field)}
        sourceSchemaFields={displayed.source_schema_fields}
        targetSchemaFields={displayed.target_schema_fields}
        pipelineKey={pipelineKey}
        versionIndex={versionIndex}
        onSave={(updated) => {
          updateMappings.mutate(
            { pipelineKey, mappings: updated, mappingId },
            {
              onSuccess: () => {
                setSavedMappings(updated);
                setEditMode(false);
                showToast('Mappings saved', 'success');
              },
              onError: () => showToast('Failed to save mappings', 'error'),
            },
          );
        }}
        onCancel={() => setEditMode(false)}
      />
    );
  }

  return (
    <SpaceBetween size="xs">
      {!editable && notEditableReason ? (
        <Box variant="small" color="text-body-secondary" data-testid="mapping-edit-unavailable">
          {notEditableReason}
        </Box>
      ) : null}
      <MappingViewer
        mappingData={displayed}
        onEditMode={editable ? () => setEditMode(true) : undefined}
        initialContext={initialContext}
        defaultModelId={defaultModelId}
        isRegenerating={isRegenerating}
        onSaveContext={onSaveContext}
        onRerunMapping={onRerunMapping}
      />
    </SpaceBetween>
  );
}
