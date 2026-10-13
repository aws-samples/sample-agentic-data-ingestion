// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Interactive mapping editor for creating, modifying, and removing field mappings.
 * Uses a list-based UI with select dropdowns for source-to-target mapping.
 * (Simplified implementation — a full drag-and-drop version would use @dnd-kit.)
 */

import { useState } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Select from '@cloudscape-design/components/select';
import { colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import type { FieldMapping } from '../types';

export interface DragDropMappingEditorProps {
  mappings: FieldMapping[];
  unmappedSource: string[];
  unmappedTarget: string[];
  sourceSchemaFields?: string[];
  targetSchemaFields?: string[];
  pipelineKey: string;
  versionIndex: number;
  onSave: (updatedMappings: FieldMapping[]) => void;
  onCancel?: () => void;
}

interface EditableMapping {
  id: string;
  source_field: string;
  target_field: string;
  confidence?: number;
  hint?: string;
}

export default function DragDropMappingEditor({
  mappings,
  unmappedSource,
  unmappedTarget,
  sourceSchemaFields,
  targetSchemaFields,
  onSave,
  onCancel,
}: DragDropMappingEditorProps) {
  const [editMappings, setEditMappings] = useState<EditableMapping[]>(
    mappings.map((m, i) => ({
      id: `mapping-${i}`,
      source_field: m.source_field,
      target_field: m.target_field,
      confidence: m.confidence,
      hint: m.hint,
    })),
  );

  // Collect all available source and target fields — include full schema fields if available
  const allSourceFields = [
    ...new Set([
      ...(sourceSchemaFields ?? []),
      ...mappings.map((m) => m.source_field),
      ...unmappedSource,
    ]),
  ];
  const allTargetFields = [
    ...new Set([
      ...(targetSchemaFields ?? []),
      ...mappings.map((m) => m.target_field),
      ...unmappedTarget,
    ]),
  ];

  // Currently used fields
  const usedSources = new Set(editMappings.map((m) => m.source_field));
  const usedTargets = new Set(editMappings.map((m) => m.target_field));

  function handleRemove(id: string) {
    setEditMappings((prev) => prev.filter((m) => m.id !== id));
  }

  function handleSourceChange(id: string, value: string) {
    setEditMappings((prev) =>
      prev.map((m) => (m.id === id ? { ...m, source_field: value } : m)),
    );
  }

  function handleTargetChange(id: string, value: string) {
    setEditMappings((prev) =>
      prev.map((m) => (m.id === id ? { ...m, target_field: value } : m)),
    );
  }

  function handleAdd() {
    const availableSource = allSourceFields.find((f) => !usedSources.has(f)) ?? '';
    const availableTarget = allTargetFields.find((f) => !usedTargets.has(f)) ?? '';
    setEditMappings((prev) => [
      ...prev,
      {
        id: `mapping-new-${Date.now()}`,
        source_field: availableSource,
        target_field: availableTarget,
      },
    ]);
  }

  function handleSave() {
    const result: FieldMapping[] = editMappings
      .filter((m) => m.source_field && m.target_field)
      .map((m) => ({
        source_field: m.source_field,
        target_field: m.target_field,
        confidence: m.confidence,
        hint: m.hint,
      }));
    onSave(result);
  }

  return (
    <Container
      header={
        <Header
          variant="h3"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={handleAdd}>Add Mapping</Button>
              {onCancel && <Button variant="link" onClick={onCancel}>Cancel</Button>}
              <Button variant="primary" onClick={handleSave}>Save Mappings</Button>
            </SpaceBetween>
          }
        >
          Edit Mappings
        </Header>
      }
    >
      <SpaceBetween size="s">
        {editMappings.length === 0 && (
          <Box textAlign="center" color="text-body-secondary">
            No mappings. Click "Add Mapping" to create one.
          </Box>
        )}
        {editMappings.map((mapping) => (
          <div
            key={mapping.id}
            data-testid="editable-mapping-row"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px',
              border: `1px solid ${colorBorderDividerDefault}`,
              borderRadius: '4px',
            }}
          >
            <div style={{ flex: 1 }}>
              <Select
                selectedOption={
                  mapping.source_field
                    ? { label: mapping.source_field, value: mapping.source_field }
                    : null
                }
                onChange={({ detail }) =>
                  handleSourceChange(mapping.id, detail.selectedOption.value ?? '')
                }
                options={allSourceFields.map((f) => ({
                  label: f,
                  value: f,
                  description: usedSources.has(f) ? 'In use' : 'Available',
                }))}
                filteringType="auto"
                placeholder="Source field"
              />
            </div>
            <Box variant="awsui-key-label">→</Box>
            <div style={{ flex: 1 }}>
              <Select
                selectedOption={
                  mapping.target_field
                    ? { label: mapping.target_field, value: mapping.target_field }
                    : null
                }
                onChange={({ detail }) =>
                  handleTargetChange(mapping.id, detail.selectedOption.value ?? '')
                }
                options={allTargetFields.map((f) => ({
                  label: f,
                  value: f,
                  description: usedTargets.has(f) ? 'In use' : 'Available',
                }))}
                filteringType="auto"
                placeholder="Target field"
              />
            </div>
            <Button
              iconName="remove"
              variant="icon"
              onClick={() => handleRemove(mapping.id)}
            />
          </div>
        ))}
      </SpaceBetween>
    </Container>
  );
}
