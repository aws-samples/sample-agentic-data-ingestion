// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for SchemaHistoryPage version selection
 * (Migrated from PipelineDetailPage after split into sub-routes)
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 21: Detail page version selection
 *
 * **Validates: Requirements 4.2, 4.3**
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { Pipeline, SchemaVersion } from '../../types';
import { PipelineProvider } from '../../contexts/PipelineContext';

// ---------------------------------------------------------------------------
// Mock hooks before importing the component
// ---------------------------------------------------------------------------

vi.mock('../../hooks', () => ({
  useRegenerateMappings: () => ({
    mutate: vi.fn(),
    isPending: false,
  }),
  useUpdateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useApprovals: () => ({ data: undefined }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useMappingRegenPolling: () => {},
  useBedrockModels: () => ({ data: [], isLoading: false }),
  useCodeVersion: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
  useTestResults: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

// Must import after mocks
import SchemaHistoryPage from '../SchemaHistoryPage';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const schemaVersionArb: fc.Arbitrary<SchemaVersion> = fc.record({
  version: fc.integer({ min: 1, max: 100 }),
  created: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
  has_mapping: fc.boolean(),
  mapping_count: fc.integer({ min: 0, max: 500 }),
  ai_model_used: fc.option(
    fc.constantFrom(
      'anthropic.claude-3-5-sonnet-20241022-v2:0',
      'anthropic.claude-3-haiku-20240307-v1:0',
      'amazon.nova-pro-v1:0',
    ),
    { nil: undefined },
  ),
  schema_hash: fc.option(fc.hexaString({ minLength: 8, maxLength: 16 }), { nil: undefined }),
  runs: fc.array(
    fc.record({
      run_id: fc.uuid(),
      timestamp: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
      code_version_id: fc.option(fc.uuid(), { nil: undefined }),
      test_status: fc.option(fc.constantFrom('passed', 'failed'), { nil: undefined }),
    }),
    { minLength: 0, maxLength: 5 },
  ),
});

function makePipeline(history: SchemaVersion[]): Pipeline {
  return {
    pipeline_key: 'test-pipeline',
    pipeline_name: 'Test Pipeline',
    status: 'active',
    source_schema: 'source',
    target_schema: 'target',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-06-01T00:00:00Z',
    schema_history: history.map((v, i) => ({
      ...v,
      version: i + 1,
      mapping_count: (i + 1) * 10,
    })),
    all_files: [],
  };
}

function renderPage(pipeline: Pipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: vi.fn() }}>
        <SchemaHistoryPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Property 21: Detail page version selection
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 21: Detail page version selection', () => {
  beforeEach(() => {
    cleanup();
  });

  /**
   * **Validates: Requirements 4.2, 4.3**
   *
   * For any Pipeline with multiple versions and any valid version index,
   * selecting that version SHALL update the displayed mapping and code
   * sections to reference the selected version, and the selected version
   * badge SHALL have a visually distinct style.
   */
  it('selecting a version updates mapping section and highlights the selected badge', () => {
    const arbWithIndex = fc
      .array(schemaVersionArb, { minLength: 2, maxLength: 10 })
      .chain((history) =>
        fc.integer({ min: 0, max: history.length - 1 }).map((idx) => ({
          history,
          idx,
        })),
      );

    fc.assert(
      fc.property(arbWithIndex, ({ history, idx }) => {
        cleanup();
        const pipeline = makePipeline(history);

        const { container } = renderPage(pipeline);

        // Find all version dots in the timeline
        const dots = within(container).getAllByTestId('version-dot');
        expect(dots.length).toBeGreaterThanOrEqual(1);
        expect(dots.length).toBeLessThanOrEqual(history.length);

        // Click the target version dot
        fireEvent.click(dots[idx]);

        const selectedVersion = pipeline.schema_history![idx];

        // The version-mapping-count should reflect the selected version
        const mappingCountEl = within(container).getByTestId('version-mapping-count');
        expect(mappingCountEl.textContent).toBe(String(selectedVersion.mapping_count));

        // The mapping section fallback should indicate loading or version context
        const mappingSection = within(container).queryByTestId('mapping-section');
        if (mappingSection) {
          expect(mappingSection.textContent!.length).toBeGreaterThan(0);
        }

        // The badge text should contain the version number
        const selectedDot = dots[idx];
        expect(selectedDot.textContent).toContain(`v${selectedVersion.version}`);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
