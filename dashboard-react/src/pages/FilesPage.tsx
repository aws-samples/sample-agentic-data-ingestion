// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import Pagination from '@cloudscape-design/components/pagination';
import Button from '@cloudscape-design/components/button';
import { formatRelativeTime } from '../utils/formatting';
import { usePipelineContext } from '../contexts/PipelineContext';
import FileDetailModal from '../components/FileDetailModal';
import type { PipelineFile } from '../types';

const FILES_PER_PAGE = 10;

export default function FilesPage() {
  const { pipeline } = usePipelineContext();
  const [filePage, setFilePage] = useState(1);
  const [selectedFileKey, setSelectedFileKey] = useState<string | null>(null);

  // Every entry in all_files is an uploaded source object (the API derives
  // file_key from original_s3_uri on file_validation steps, never from the
  // converted json_s3_uri), so no extension filter is applied — .json is a
  // supported input format and must be listed like any other upload.
  const sortedFiles = useMemo(() => {
    return [...(pipeline.all_files ?? [])].sort(
      (a: PipelineFile, b: PipelineFile) => new Date(b.processed_at).getTime() - new Date(a.processed_at).getTime(),
    );
  }, [pipeline.all_files]);

  const totalFilePages = Math.max(1, Math.ceil(sortedFiles.length / FILES_PER_PAGE));
  const pagedFiles = sortedFiles.slice((filePage - 1) * FILES_PER_PAGE, filePage * FILES_PER_PAGE);

  return (
    <SpaceBetween size="s">
      <Table
        items={pagedFiles}
        columnDefinitions={[
          {
            id: 'file_key', header: 'File Name',
            cell: (item: PipelineFile) => (
              <Button variant="link" onClick={() => setSelectedFileKey(item.file_key)}>
                {item.file_key.split('/').pop() || item.file_key}
              </Button>
            ),
          },
          { id: 'processed_at', header: 'Processed', cell: (item: PipelineFile) => formatRelativeTime(item.processed_at) },
          { id: 'execution_id', header: 'Execution', cell: (item: PipelineFile) => item.execution_id || '—' },
        ]}
        empty={<Box textAlign="center" color="text-body-secondary">No files processed yet</Box>}
      />
      {totalFilePages > 1 && (
        <Pagination currentPageIndex={filePage} pagesCount={totalFilePages} onChange={({ detail }) => setFilePage(detail.currentPageIndex)} />
      )}
      {selectedFileKey && (
        <FileDetailModal pipelineKey={pipeline.pipeline_key} fileKey={selectedFileKey} visible={!!selectedFileKey} onDismiss={() => setSelectedFileKey(null)} />
      )}
    </SpaceBetween>
  );
}
