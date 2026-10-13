// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Table from '@cloudscape-design/components/table';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import { colorBackgroundContainerContent, colorTextBodyDefault, colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import { useFileDetail } from '../hooks';
import { formatStepName, getConfidenceLevel, statusType } from '../utils/formatting';
import type { FileTimelineStep } from '../types';

export interface FileDetailModalProps {
  pipelineKey: string;
  fileKey: string;
  visible: boolean;
  onDismiss: () => void;
}

function confidenceColor(level: 'high' | 'medium' | 'low'): 'green' | 'blue' | 'red' {
  if (level === 'high') return 'green';
  if (level === 'medium') return 'blue';
  return 'red';
}

function tryParseJson(str: string | undefined): object | null {
  if (!str) return null;
  try {
    return JSON.parse(str);
  } catch {
    return null;
  }
}

function tryParseMappings(str: string | undefined): Array<{ source: string; target: string; confidence?: number }> {
  if (!str) return [];
  try {
    const parsed = JSON.parse(str);
    if (Array.isArray(parsed)) return parsed;
    if (parsed.mappings && Array.isArray(parsed.mappings)) return parsed.mappings;
    return [];
  } catch {
    return [];
  }
}

export default function FileDetailModal({ pipelineKey, fileKey, visible, onDismiss }: FileDetailModalProps) {
  const { data: fileDetail, isLoading } = useFileDetail(
    visible ? pipelineKey : undefined,
    visible ? fileKey : undefined,
  );

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="File Details"
      size="large"
    >
      {isLoading ? (
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
        </Box>
      ) : !fileDetail ? (
        <Box textAlign="center" color="text-body-secondary">
          No file details available
        </Box>
      ) : (
        <div data-testid="file-detail-content">
          <SpaceBetween size="l">
            {/* File metadata */}
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">File Key</Box>
                <Box data-testid="file-key">{fileDetail.file_key}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Pipeline</Box>
                <Box data-testid="file-pipeline-key">{fileDetail.pipeline_key}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Execution ID</Box>
                <Box data-testid="file-execution-id">{fileDetail.execution_id || '—'}</Box>
              </div>
            </ColumnLayout>

            {/* Processing timeline */}
            {fileDetail.timeline && fileDetail.timeline.length > 0 && (
              <Container header={<Header variant="h3">Processing Timeline</Header>}>
                <div data-testid="file-timeline">
                  <SpaceBetween size="xs">
                    {fileDetail.timeline.map((step: FileTimelineStep, idx: number) => (
                      <div key={`${step.step}-${idx}`} data-testid="timeline-step" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <StatusIndicator type={statusType(step.status)} />
                        <Box variant="strong">{formatStepName(step.step)}</Box>
                        <Box variant="small" color="text-body-secondary">{step.timestamp}</Box>
                        {step.duration != null && (
                          <Badge>{step.duration}ms</Badge>
                        )}
                      </div>
                    ))}
                  </SpaceBetween>
                </div>
              </Container>
            )}

            {/* Extracted schema */}
            {fileDetail.steps && Object.values(fileDetail.steps).some((s) => s.extracted_schema) && (
              <Container header={<Header variant="h3">Extracted Schema</Header>}>
                <div data-testid="file-schema">
                  {Object.entries(fileDetail.steps).map(([stepName, stepData]) => {
                    if (!stepData.extracted_schema) return null;
                    const parsed = tryParseJson(stepData.extracted_schema);
                    return (
                      <pre key={stepName} style={{
                        background: colorBackgroundContainerContent,
                        color: colorTextBodyDefault,
                        border: `1px solid ${colorBorderDividerDefault}`,
                        padding: 12,
                        borderRadius: 4,
                        fontSize: 12,
                        overflow: 'auto',
                        maxHeight: 300,
                      }}>
                        {parsed ? JSON.stringify(parsed, null, 2) : stepData.extracted_schema}
                      </pre>
                    );
                  })}
                </div>
              </Container>
            )}

            {/* Mapping preview */}
            {fileDetail.steps && Object.values(fileDetail.steps).some((s) => s.mapping_result) && (
              <Container header={<Header variant="h3">Mapping Preview</Header>}>
                <div data-testid="file-mapping-preview">
                  {Object.entries(fileDetail.steps).map(([stepName, stepData]) => {
                    if (!stepData.mapping_result) return null;
                    const mappings = tryParseMappings(stepData.mapping_result);
                    if (mappings.length === 0) return null;
                    return (
                      <Table
                        key={stepName}
                        items={mappings}
                        columnDefinitions={[
                          { id: 'source', header: 'Source', cell: (item) => item.source || '—' },
                          { id: 'target', header: 'Target', cell: (item) => item.target || '—' },
                          {
                            id: 'confidence',
                            header: 'Confidence',
                            cell: (item) => {
                              if (item.confidence == null) return '—';
                              const level = getConfidenceLevel(item.confidence);
                              return (
                                <Badge color={confidenceColor(level)}>
                                  {Math.round(item.confidence * 100)}%
                                </Badge>
                              );
                            },
                          },
                        ]}
                        variant="embedded"
                      />
                    );
                  })}
                </div>
              </Container>
            )}
          </SpaceBetween>
        </div>
      )}
    </Modal>
  );
}
