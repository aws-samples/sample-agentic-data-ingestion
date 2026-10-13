// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import type { Pipeline, SchemaVersion } from '../types';
import { getModelDisplayName, formatRelativeTime, statusType } from '../utils/formatting';
import FileUploadModal from './FileUploadModal';
import ClickableArea from './common/ClickableArea';

export interface PipelineCardProps {
  pipeline: Pipeline;
}

export default function PipelineCard({ pipeline }: PipelineCardProps) {
  const navigate = useNavigate();
  const history = pipeline.schema_history ?? [];
  const [selectedVersionIdx, setSelectedVersionIdx] = useState<number | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const activeVersion: SchemaVersion | undefined =
    selectedVersionIdx !== null ? history[selectedVersionIdx] : history[history.length - 1];

  const mappingCount = activeVersion?.mapping_count || pipeline.mapping_count || 0;
  const aiModel = activeVersion?.ai_model_used ?? pipeline.ai_model ?? pipeline.model_id;
  const versionDate = activeVersion?.created ?? pipeline.updated_at;

  // Take the 3 most recently processed uploads. No extension filter: all_files
  // only ever contains uploaded source objects (the API derives file_key from
  // original_s3_uri, not from the converted json_s3_uri), and .json is a
  // supported input format.
  const recentFiles = [...(pipeline.all_files ?? [])]
    .sort((a, b) => new Date(b.processed_at).getTime() - new Date(a.processed_at).getTime())
    .slice(0, 3);

  return (
    <ClickableArea
      data-testid="pipeline-card"
      onClick={() => navigate(`/pipelines/${pipeline.pipeline_key}`)}
      ariaLabel={`Open pipeline ${pipeline.pipeline_name || pipeline.pipeline_key}`}
    >
      <Container>
      <SpaceBetween size="s">
        {/* Header: name + status */}
        <SpaceBetween direction="horizontal" size="xs">
          <Box variant="h3" data-testid="pipeline-name">
            {pipeline.pipeline_name || pipeline.pipeline_key}
          </Box>
          <span data-testid="pipeline-status">
            <StatusIndicator type={statusType(pipeline.status)}>
              {pipeline.status}
            </StatusIndicator>
          </span>
        </SpaceBetween>

        {/* Version badges */}
        {history.length > 0 && (
          <div data-testid="version-badges">
            <SpaceBetween direction="horizontal" size="xxs">
              {history.map((v, idx) => (
                <ClickableArea
                  as="span"
                  key={v.version}
                  data-testid="version-badge"
                  ariaLabel={`Select schema version ${v.version}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    setSelectedVersionIdx(idx);
                  }}
                  style={{
                    fontWeight: selectedVersionIdx === idx ? 'bold' : 'normal',
                  }}
                >
                  <Badge color={selectedVersionIdx === idx ? 'blue' : 'grey'}>
                    v{v.version}
                  </Badge>
                </ClickableArea>
              ))}
            </SpaceBetween>
          </div>
        )}

        {/* Stats row */}
        <SpaceBetween direction="horizontal" size="l">
          <Box variant="div">
            <Box variant="awsui-key-label">Files</Box>
            <span data-testid="file-count">{pipeline.file_count ?? 0}</span>
          </Box>
          <Box variant="div">
            <Box variant="awsui-key-label">Mappings</Box>
            <span data-testid="mapping-count">{mappingCount}</span>
          </Box>
          <Box variant="div">
            <Box variant="awsui-key-label">Schema Version</Box>
            <span data-testid="schema-version">
              {activeVersion ? `v${activeVersion.version}` : `v${pipeline.schema_version ?? 1}`}
            </span>
          </Box>
        </SpaceBetween>

        {/* AI model + date */}
        <SpaceBetween direction="horizontal" size="l">
          <Box variant="div">
            <Box variant="awsui-key-label">AI Model</Box>
            <span data-testid="ai-model">{getModelDisplayName(aiModel)}</span>
          </Box>
          <Box variant="div">
            <Box variant="awsui-key-label">Updated</Box>
            <span data-testid="last-updated">
              {versionDate ? formatRelativeTime(versionDate) : '—'}
            </span>
          </Box>
        </SpaceBetween>

        {/* Recent files */}
        {recentFiles.length > 0 && (
          <Box variant="div" data-testid="recent-files">
            <Box variant="awsui-key-label">Recent Files</Box>
            <SpaceBetween size="xxs">
              {recentFiles.map((f, idx) => (
                <Box key={`${f.file_key}-${idx}`} variant="small" color="text-body-secondary">
                  {f.file_key.split('/').pop()} — {formatRelativeTime(f.processed_at)}
                </Box>
              ))}
            </SpaceBetween>
          </Box>
        )}

        {/* Upload button */}
        <Box>
          <Button
            variant="normal"
            iconName="upload"
            onClick={(e) => {
              e.stopPropagation();
              setShowUpload(true);
            }}
          >
            Upload File
          </Button>
        </Box>
      </SpaceBetween>

      {showUpload && (
        <FileUploadModal
          visible={showUpload}
          onDismiss={() => setShowUpload(false)}
          pipeline={pipeline}
        />
      )}
      </Container>
    </ClickableArea>
  );
}
