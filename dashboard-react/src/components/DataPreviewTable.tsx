// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Box from '@cloudscape-design/components/box';
import { useDataPreview } from '../hooks';
import { ApiError } from '../services/api-client';

interface DataPreviewTableProps {
  pipelineId: string;
  runId?: string;
  /**
   * Source type of the pipeline. Data preview reads the run's uploaded S3
   * source file, which only exists for S3_UPLOAD pipelines. JDBC sources
   * have no S3 file (file_validation writes a synthetic `jdbc://` URI), so
   * the preview is not applicable — we show an informational note instead
   * of firing a request that always fails.
   */
  sourceType?: string;
}

export default function DataPreviewTable({ pipelineId, runId, sourceType }: DataPreviewTableProps) {
  const isJdbcSource = sourceType === 'JDBC';
  const { data, isLoading, isError, error } = useDataPreview(pipelineId, runId, undefined, {
    enabled: !isJdbcSource,
  });

  // Data preview is only meaningful for S3-uploaded sources. For JDBC
  // sources there is no uploaded file to preview, so surface a clear note
  // rather than an error.
  if (isJdbcSource) {
    return (
      <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="data-preview-na">
        Preview not available for JDBC sources
      </Box>
    );
  }

  if (isLoading) {
    return (
      <Box textAlign="center" padding="l" data-testid="data-preview-loading">
        <Spinner size="large" />
      </Box>
    );
  }

  // A 404 from the preview endpoint means "there is nothing to preview here",
  // not "the read failed" — no file uploaded yet, no run whose source data can
  // be read, or a source type the preview does not support. The intent is
  // stated at data_preview.py:63-66 and the contract is already honoured on
  // both sides of this component: use-data-preview.ts:26-31 suppresses the
  // retry AND the error toast on 404. react-query still reports
  // `isError: true`, so without this branch the generic failure text below
  // renders for a case that is not a failure (p48 R12.4).
  //
  // The distinction is carried in WORDS — LGR-99050e6-1 (template / ratified)
  // records that colour alone fails WCAG 1.4.1. This is why the branch renders
  // a statement rather than the earlier `return null` (commit 2eaf8a30e3,
  // "return null instead of error when no files uploaded"): no words at all
  // left an empty region indistinguishable from a panel that failed to load,
  // and both call sites render this component bare, with no surrounding
  // Container to explain the gap (p48 R12.5).
  //
  // Deliberately NO Retry, following RunDetailPage.tsx:391-397: re-requesting
  // something that correctly 404s cannot succeed, so the button would be a
  // false affordance. The server's own 404 sentence is deliberately not
  // rendered either — the variants all mean the same thing and two of them
  // name internal ids (a pipeline key, a `skipped-<fingerprint>` run id), so
  // they are diagnostics rather than display copy.
  const is404 = error instanceof ApiError && error.status === 404;
  if (is404) {
    return (
      <Box
        textAlign="center"
        color="text-body-secondary"
        padding="l"
        data-testid="data-preview-none"
      >
        No source data is available to preview
      </Box>
    );
  }

  const columns = data?.columns ?? [];
  const columnDefinitions = columns.map((col) => ({
    id: col,
    header: col,
    cell: (item: Record<string, unknown>) => {
      const val = item[col];
      if (val === null || val === undefined) return '';
      if (typeof val === 'object') return JSON.stringify(val);
      return String(val);
    },
  }));

  const fileName = data?.source_file?.split('/').pop() ?? data?.source_file;
  const headerText = data
    ? `Latest file upload: ${fileName}`
    : 'Data Preview';

  return (
    <Table
      data-testid="data-preview-table"
      columnDefinitions={columnDefinitions}
      items={data?.rows ?? []}
      header={<Header variant="h2">{headerText}</Header>}
      empty={
        isError ? (
          <StatusIndicator type="error">Failed to load data preview</StatusIndicator>
        ) : columns.length === 0 ? (
          <Box textAlign="center" color="text-body-secondary">No columns</Box>
        ) : (
          <Box textAlign="center" color="text-body-secondary">No data</Box>
        )
      }
    />
  );
}
