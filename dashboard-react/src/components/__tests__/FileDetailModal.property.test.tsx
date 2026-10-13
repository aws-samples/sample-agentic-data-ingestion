// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for FileDetailModal
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 15: File detail modal rendering
 */

import { describe, it, expect, vi } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import FileDetailModal from '../FileDetailModal';
import type { FileDetail, FileTimelineStep, FileStepData } from '../../types';

// Mock the useFileDetail hook to inject test data directly
let mockFileDetail: FileDetail | undefined;
vi.mock('../../hooks', () => ({
  useFileDetail: () => ({
    data: mockFileDetail,
    isLoading: false,
    isError: false,
  }),
}));

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const timelineStepArb: fc.Arbitrary<FileTimelineStep> = fc.record({
  step: fc.constantFrom('file_validation', 'schema_extract', 'schema_mapping', 'code_generation'),
  status: fc.constantFrom('completed', 'failed', 'running', 'pending'),
  timestamp: fc.date({ min: new Date('2024-01-01'), max: new Date('2025-01-01') }).map((d) => d.toISOString()),
  duration: fc.option(fc.integer({ min: 10, max: 60000 }), { nil: undefined }),
});

const fileDetailArb: fc.Arbitrary<FileDetail> = fc.record({
  file_key: fc.constantFrom('data/report.csv', 'uploads/input.parquet', 'files/output.xlsx'),
  pipeline_key: fc.constantFrom('pipeline-alpha', 'pipeline-beta', 'pipeline-gamma'),
  execution_id: fc.option(fc.uuid(), { nil: undefined }),
  timeline: fc.array(timelineStepArb, { minLength: 1, maxLength: 5 }),
  steps: fc.constant({} as Record<string, FileStepData>),
});

const fileDetailWithSchemaArb: fc.Arbitrary<FileDetail> = fileDetailArb.map((fd) => ({
  ...fd,
  steps: {
    schema_extract: {
      extracted_schema: JSON.stringify({ fields: [{ name: 'id', type: 'int' }] }),
    },
  },
}));

const fileDetailWithMappingArb: fc.Arbitrary<FileDetail> = fileDetailArb.map((fd) => ({
  ...fd,
  steps: {
    schema_mapping: {
      mapping_result: JSON.stringify([
        { source: 'src_id', target: 'tgt_id', confidence: 0.95 },
      ]),
    },
  },
}));

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function renderModal(fileDetail: FileDetail) {
  mockFileDetail = fileDetail;
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <FileDetailModal
        pipelineKey={fileDetail.pipeline_key}
        fileKey={fileDetail.file_key}
        visible={true}
        onDismiss={() => {}}
      />
    </QueryClientProvider>,
  );
}

// ---------------------------------------------------------------------------
// Property 15: File detail modal rendering
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 15: File detail modal rendering', () => {
  /**
   * **Validates: Requirements 13.2, 13.3, 13.4**
   *
   * For any FileDetail object, the FileDetailModal SHALL render the file key,
   * pipeline key, execution ID, and all timeline steps. When extracted schema
   * data is present, the schema SHALL be displayed. When mapping results are
   * present, a mapping preview SHALL be displayed.
   */
  it('renders file key, pipeline key, execution ID, and timeline steps', () => {
    fc.assert(
      fc.property(fileDetailArb, (fileDetail) => {
        cleanup();
        renderModal(fileDetail);

        const content = screen.getByTestId('file-detail-content');

        // File key
        const fileKeyEl = within(content).getByTestId('file-key');
        expect(fileKeyEl.textContent).toBe(fileDetail.file_key);

        // Pipeline key
        const pipelineKeyEl = within(content).getByTestId('file-pipeline-key');
        expect(pipelineKeyEl.textContent).toBe(fileDetail.pipeline_key);

        // Execution ID
        const execIdEl = within(content).getByTestId('file-execution-id');
        expect(execIdEl.textContent).toBe(fileDetail.execution_id || '—');

        // Timeline steps
        const timeline = within(content).getByTestId('file-timeline');
        const timelineSteps = within(timeline).getAllByTestId('timeline-step');
        expect(timelineSteps).toHaveLength(fileDetail.timeline.length);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('displays extracted schema when present', () => {
    fc.assert(
      fc.property(fileDetailWithSchemaArb, (fileDetail) => {
        cleanup();
        renderModal(fileDetail);

        const content = screen.getByTestId('file-detail-content');
        const schemaSection = within(content).getByTestId('file-schema');
        expect(schemaSection).toBeTruthy();
        expect(schemaSection.textContent).toContain('id');

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('displays mapping preview when present', () => {
    fc.assert(
      fc.property(fileDetailWithMappingArb, (fileDetail) => {
        cleanup();
        renderModal(fileDetail);

        const content = screen.getByTestId('file-detail-content');
        const mappingSection = within(content).getByTestId('file-mapping-preview');
        expect(mappingSection).toBeTruthy();
        expect(mappingSection.textContent).toContain('src_id');
        expect(mappingSection.textContent).toContain('tgt_id');

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
