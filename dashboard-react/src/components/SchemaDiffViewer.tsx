// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import Box from '@cloudscape-design/components/box';
import SegmentedControl from '@cloudscape-design/components/segmented-control';
import SpaceBetween from '@cloudscape-design/components/space-between';
import {
  colorBackgroundStatusSuccess,
  colorBackgroundStatusError,
  colorBackgroundStatusWarning,
  colorTextStatusSuccess,
  colorTextStatusError,
  colorTextStatusWarning,
  colorBorderDividerDefault,
} from '@cloudscape-design/design-tokens';
import type { SchemaDifference } from '../types';

export interface SchemaDiffViewerProps {
  differences: SchemaDifference[];
}

const CHANGE_STYLES: Record<string, { bg: string; color: string; prefix: string }> = {
  added:     { bg: colorBackgroundStatusSuccess, color: colorTextStatusSuccess, prefix: '+' },
  removed:   { bg: colorBackgroundStatusError, color: colorTextStatusError, prefix: '−' },
  modified:  { bg: colorBackgroundStatusWarning, color: colorTextStatusWarning, prefix: '~' },
  unchanged: { bg: 'transparent', color: 'inherit', prefix: '' },
};

function DiffRow({ diff, viewMode }: { diff: SchemaDifference; viewMode: string }) {
  const style = CHANGE_STYLES[diff.change_type] ?? CHANGE_STYLES.unchanged;
  const bg = diff.change_type === 'unchanged' ? undefined : style.bg;
  const color = diff.change_type === 'unchanged' ? undefined : style.color;

  if (viewMode === 'side-by-side') {
    return (
      <div
        data-testid="diff-row"
        data-change-type={diff.change_type}
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: 8,
          padding: '6px 12px',
          backgroundColor: bg,
          color,
          borderBottom: `1px solid ${colorBorderDividerDefault}`,
          fontFamily: 'monospace',
          fontSize: 13,
        }}
      >
        <span data-testid="diff-left">
          {diff.change_type === 'added' ? '' : `${style.prefix ? style.prefix + ' ' : ''}${diff.field_name}: ${diff.old_type ?? diff.new_type ?? ''}`}
        </span>
        <span data-testid="diff-right">
          {diff.change_type === 'removed' ? '' : `${style.prefix ? style.prefix + ' ' : ''}${diff.field_name}: ${diff.new_type ?? diff.old_type ?? ''}`}
        </span>
      </div>
    );
  }

  // Inline view
  return (
    <div
      data-testid="diff-row"
      data-change-type={diff.change_type}
      style={{
        padding: '6px 12px',
        backgroundColor: bg,
        color,
        borderBottom: `1px solid ${colorBorderDividerDefault}`,
        fontFamily: 'monospace',
        fontSize: 13,
      }}
    >
      {style.prefix && <span data-testid="diff-prefix">{style.prefix} </span>}
      <span data-testid="diff-field">{diff.field_name}</span>
      {diff.change_type === 'modified' && diff.old_type && diff.new_type && (
        <span data-testid="diff-type-change"> ({diff.old_type} → {diff.new_type})</span>
      )}
      {diff.change_type !== 'modified' && (diff.new_type || diff.old_type) && (
        <span data-testid="diff-type">: {diff.new_type ?? diff.old_type}</span>
      )}
    </div>
  );
}

export default function SchemaDiffViewer({ differences }: SchemaDiffViewerProps) {
  const [viewMode, setViewMode] = useState<string>('inline');

  if (differences.length === 0) {
    return (
      <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="diff-empty">
        Schemas are identical — no differences found.
      </Box>
    );
  }

  return (
    <SpaceBetween size="m" data-testid="schema-diff-viewer">
      <SegmentedControl
        selectedId={viewMode}
        onChange={({ detail }) => setViewMode(detail.selectedId)}
        options={[
          { id: 'inline', text: 'Inline' },
          { id: 'side-by-side', text: 'Side by side' },
        ]}
        data-testid="diff-view-toggle"
      />

      {viewMode === 'side-by-side' && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, fontWeight: 'bold', padding: '4px 8px', borderBottom: `2px solid ${colorBorderDividerDefault}` }}>
          <span>Old version</span>
          <span>New version</span>
        </div>
      )}

      <div data-testid="diff-list">
        {differences.map((diff, i) => (
          <DiffRow key={`${diff.field_name}-${i}`} diff={diff} viewMode={viewMode} />
        ))}
      </div>
    </SpaceBetween>
  );
}
