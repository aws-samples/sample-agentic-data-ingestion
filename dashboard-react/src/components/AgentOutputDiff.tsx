// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMemo } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import {
  colorBorderDividerDefault,
  colorBorderDividerSecondary,
  colorTextBodySecondary,
} from '@cloudscape-design/design-tokens';

export interface AgentOutputDiffProps {
  /** Label for the left (previous) version */
  labelA: string;
  /** Label for the right (current) version */
  labelB: string;
  /** Content of version A (previous) */
  contentA: string;
  /** Content of version B (current) */
  contentB: string;
  /** Content type for display purposes */
  contentType?: 'code' | 'mapping';
}

interface DiffLine {
  lineNum: number;
  textA: string;
  textB: string;
  status: 'unchanged' | 'added' | 'removed' | 'modified';
}

function computeLineDiff(textA: string, textB: string): DiffLine[] {
  const linesA = textA.split('\n');
  const linesB = textB.split('\n');
  const maxLen = Math.max(linesA.length, linesB.length);
  const result: DiffLine[] = [];

  for (let i = 0; i < maxLen; i++) {
    const a = i < linesA.length ? linesA[i] : '';
    const b = i < linesB.length ? linesB[i] : '';

    let status: DiffLine['status'] = 'unchanged';
    if (i >= linesA.length) status = 'added';
    else if (i >= linesB.length) status = 'removed';
    else if (a !== b) status = 'modified';

    result.push({ lineNum: i + 1, textA: a, textB: b, status });
  }
  return result;
}

const STATUS_COLORS: Record<DiffLine['status'], string> = {
  unchanged: 'transparent',
  added: 'rgba(0, 180, 0, 0.1)',
  removed: 'rgba(220, 0, 0, 0.1)',
  modified: 'rgba(255, 165, 0, 0.1)',
};

export default function AgentOutputDiff({ labelA, labelB, contentA, contentB }: AgentOutputDiffProps) {
  const diffLines = useMemo(() => computeLineDiff(contentA, contentB), [contentA, contentB]);

  const changeCount = diffLines.filter((l) => l.status !== 'unchanged').length;

  const codeStyle: React.CSSProperties = {
    fontFamily: 'monospace',
    fontSize: '12px',
    lineHeight: '1.6',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    margin: 0,
    padding: '2px 4px',
  };

  return (
    <Container
      header={
        <Header variant="h3" description={`${changeCount} line${changeCount !== 1 ? 's' : ''} changed`}>
          Side-by-Side Comparison
        </Header>
      }
      data-testid="agent-output-diff"
    >
      <SpaceBetween size="s">
        {/* Legend */}
        <SpaceBetween direction="horizontal" size="m">
          <StatusIndicator type="success">Added</StatusIndicator>
          <StatusIndicator type="error">Removed</StatusIndicator>
          <StatusIndicator type="warning">Modified</StatusIndicator>
        </SpaceBetween>

        {/* Side-by-side headers */}
        <ColumnLayout columns={2}>
          <Box variant="h4">{labelA}</Box>
          <Box variant="h4">{labelB}</Box>
        </ColumnLayout>

        {/* Diff content */}
        <div style={{ maxHeight: '500px', overflow: 'auto', border: `1px solid ${colorBorderDividerDefault}`, borderRadius: '4px' }}>
          {diffLines.map((line) => (
            <div
              key={line.lineNum}
              style={{
                display: 'grid',
                gridTemplateColumns: '40px 1fr 40px 1fr',
                backgroundColor: STATUS_COLORS[line.status],
                borderBottom: `1px solid ${colorBorderDividerSecondary}`,
              }}
            >
              <span style={{ ...codeStyle, color: colorTextBodySecondary, textAlign: 'right', paddingRight: '8px' }}>
                {line.status !== 'added' ? line.lineNum : ''}
              </span>
              <span style={codeStyle}>{line.textA}</span>
              <span style={{ ...codeStyle, color: colorTextBodySecondary, textAlign: 'right', paddingRight: '8px' }}>
                {line.status !== 'removed' ? line.lineNum : ''}
              </span>
              <span style={codeStyle}>{line.textB}</span>
            </div>
          ))}
        </div>
      </SpaceBetween>
    </Container>
  );
}
