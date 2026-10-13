// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import {
  colorBackgroundContainerContent,
  colorTextBodyDefault,
  colorBorderDividerDefault,
} from '@cloudscape-design/design-tokens';
import { useCodeVersion } from '../hooks';

export interface CodeViewerProps {
  codeVersionId: string;
  schemaVersion?: number;
}

export default function CodeViewer({ codeVersionId, schemaVersion }: CodeViewerProps) {
  const { data, isLoading, isError } = useCodeVersion(codeVersionId);

  if (isLoading) {
    return (
      <div data-testid="code-viewer">
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary">Loading code...</Box>
        </Box>
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div data-testid="code-viewer">
        <Container>
          <StatusIndicator type="error">
            Failed to load code. Please try again.
          </StatusIndicator>
        </Container>
      </div>
    );
  }

  return (
    <div data-testid="code-viewer">
      <Container
        header={
          <Header variant="h3">
            Generated Code ({data.language || 'PySpark'}){schemaVersion != null ? ` — Schema v${schemaVersion}` : ''}
          </Header>
        }
      >
        <pre
          data-testid="code-content"
          style={{
            backgroundColor: colorBackgroundContainerContent,
            color: colorTextBodyDefault,
            border: `1px solid ${colorBorderDividerDefault}`,
            padding: '16px',
            borderRadius: '4px',
            overflow: 'auto',
            maxHeight: '600px',
            fontSize: '13px',
            lineHeight: '1.5',
            fontFamily: 'monospace',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}
        >
          {(data as unknown as Record<string, unknown>).code_content as string ?? data.code}
        </pre>
      </Container>
    </div>
  );
}
