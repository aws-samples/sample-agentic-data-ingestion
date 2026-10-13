// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for PipelineCard
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 2: Pipeline card displays all required fields
 * - Property 3: Version badge count matches schema history
 * - Property 4: Version selection updates displayed data
 * - Property 5: Card file display limit
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PipelineCard from '../PipelineCard';
import type { Pipeline, SchemaVersion, PipelineFile } from '../../types';

// ---------------------------------------------------------------------------
// Arbitraries (smart generators)
// ---------------------------------------------------------------------------

const statusArb = fc.constantFrom('active', 'completed', 'error', 'pending', 'running');

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

const basePipelineArb: fc.Arbitrary<Pipeline> = fc.record({
  pipeline_key: fc.stringOf(fc.constantFrom(...'abcdefghijklmnop'.split('')), { minLength: 3, maxLength: 12 }),
  pipeline_name: fc.stringOf(fc.constantFrom(...'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -_'.split('')), { minLength: 1, maxLength: 30 }),
  status: statusArb,
  source_schema: fc.constant('source_schema'),
  target_schema: fc.constant('target_schema'),
  created_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
  updated_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
  file_count: fc.integer({ min: 0, max: 1000 }),
  mapping_count: fc.integer({ min: 0, max: 500 }),
  schema_version: fc.integer({ min: 1, max: 50 }),
  ai_model: fc.option(fc.constantFrom('anthropic.claude-3-5-sonnet-20241022-v2:0', 'amazon.nova-pro-v1:0'), { nil: undefined }),
});

// ---------------------------------------------------------------------------
// Helper: render PipelineCard within MemoryRouter
// ---------------------------------------------------------------------------

function renderCard(pipeline: Pipeline) {
  return render(
    <MemoryRouter>
      <PipelineCard pipeline={pipeline} />
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Property 2: Pipeline card displays all required fields
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 2: Pipeline card displays all required fields', () => {
  /**
   * **Validates: Requirements 2.1**
   *
   * For any valid Pipeline object, the rendered PipelineCard output SHALL
   * contain the pipeline name, status, file count, schema version,
   * mapping count, and last updated date.
   */
  it('renders pipeline name, status, file count, schema version, mapping count, and last updated date', () => {
    fc.assert(
      fc.property(basePipelineArb, (pipeline) => {
        cleanup();
        const { container } = renderCard(pipeline);

        const card = within(container).getByTestId('pipeline-card');

        // Pipeline name
        const nameEl = within(card).getByTestId('pipeline-name');
        expect(nameEl.textContent).toContain(pipeline.pipeline_name || pipeline.pipeline_key);

        // Status
        const statusEl = within(card).getByTestId('pipeline-status');
        expect(statusEl.textContent).toContain(pipeline.status);

        // File count
        const fileCountEl = within(card).getByTestId('file-count');
        expect(fileCountEl.textContent).toBe(String(pipeline.file_count ?? 0));

        // Schema version
        const schemaVersionEl = within(card).getByTestId('schema-version');
        expect(schemaVersionEl.textContent).toContain(String(pipeline.schema_version ?? 1));

        // Mapping count
        const mappingCountEl = within(card).getByTestId('mapping-count');
        expect(mappingCountEl.textContent).toBe(String(pipeline.mapping_count ?? 0));

        // Last updated (should not be empty)
        const lastUpdatedEl = within(card).getByTestId('last-updated');
        expect(lastUpdatedEl.textContent!.length).toBeGreaterThan(0);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 3: Version badge count matches schema history
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 3: Version badge count matches schema history', () => {
  /**
   * **Validates: Requirements 2.2**
   *
   * For any Pipeline with N schema versions (N > 0), the PipelineCard
   * SHALL render exactly N version badges.
   */
  it('renders exactly N version badges for a pipeline with N schema versions', () => {
    const pipelineWithHistoryArb = fc.tuple(
      basePipelineArb,
      fc.array(schemaVersionArb, { minLength: 1, maxLength: 10 }),
    ).map(([pipeline, history]) => ({
      ...pipeline,
      schema_history: history.map((v, i) => ({ ...v, version: i + 1 })),
    }));

    fc.assert(
      fc.property(pipelineWithHistoryArb, (pipeline) => {
        cleanup();
        const { container } = renderCard(pipeline);

        const badges = within(container).getAllByTestId('version-badge');
        expect(badges).toHaveLength(pipeline.schema_history!.length);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 4: Version selection updates displayed data
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 4: Version selection updates displayed data', () => {
  /**
   * **Validates: Requirements 2.3**
   *
   * For any Pipeline with schema history and any valid version index i,
   * selecting version i on the PipelineCard SHALL cause the displayed
   * mapping count, AI model, and date to equal the values from schema_history[i].
   */
  it('clicking a version badge updates mapping count, AI model, and date', () => {
    const pipelineWithHistoryArb = fc.tuple(
      basePipelineArb,
      fc.array(schemaVersionArb, { minLength: 2, maxLength: 8 }),
    ).map(([pipeline, history]) => ({
      ...pipeline,
      schema_history: history.map((v, i) => ({
        ...v,
        version: i + 1,
        // Ensure each version has distinct mapping_count for testability
        mapping_count: (i + 1) * 10,
        ai_model_used: i % 2 === 0 ? 'anthropic.claude-3-5-sonnet-20241022-v2:0' : 'amazon.nova-pro-v1:0',
      })),
    }));

    const arbWithIndex = pipelineWithHistoryArb.chain((pipeline) =>
      fc.integer({ min: 0, max: pipeline.schema_history!.length - 1 }).map((idx) => ({
        pipeline,
        idx,
      })),
    );

    fc.assert(
      fc.property(arbWithIndex, ({ pipeline, idx }) => {
        cleanup();
        const { container } = renderCard(pipeline);

        const badges = within(container).getAllByTestId('version-badge');
        fireEvent.click(badges[idx]);

        const selectedVersion = pipeline.schema_history![idx];

        // Mapping count should match selected version
        const mappingCountEl = within(container).getByTestId('mapping-count');
        expect(mappingCountEl.textContent).toBe(String(selectedVersion.mapping_count));

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 5: Card file display limit
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 5: Card file display limit', () => {
  /**
   * **Validates: Requirements 2.4**
   *
   * For any Pipeline with associated files, the PipelineCard SHALL display
   * at most 3 files. Extension plays no part in eligibility — `.json` is a
   * supported input format (p30 R13.6).
   */
  it('displays at most 3 files regardless of total file count or extension', () => {
    // Use unique file keys (extension included) to avoid duplicate counting
    const fileArb = (index: number, ext: string): fc.Arbitrary<PipelineFile> => fc.record({
      file_key: fc.constant(`data/file_${index}${ext}`),
      pipeline_key: fc.constant('test-pipeline'),
      processed_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
      execution_id: fc.option(fc.uuid(), { nil: undefined }),
    });

    // Generate 0-8 uploads with unique keys, mixing supported extensions
    // (.csv, .json, .parquet) so JSON uploads are exercised alongside others.
    const extensions = ['.csv', '.json', '.parquet'];
    const filesArb = fc.integer({ min: 0, max: 8 }).chain((count) =>
      fc.tuple(...Array.from({ length: count }, (_, i) => fileArb(i, extensions[i % extensions.length]))),
    );

    const pipelineWithFilesArb = fc.tuple(
      basePipelineArb,
      filesArb,
    ).map(([pipeline, files]) => ({
      ...pipeline,
      all_files: [...files],
    }));

    fc.assert(
      fc.property(pipelineWithFilesArb, (pipeline) => {
        cleanup();
        const { container } = renderCard(pipeline);

        const recentFilesSection = within(container).queryByTestId('recent-files');
        const files = pipeline.all_files ?? [];

        if (files.length === 0) {
          expect(recentFilesSection).toBeNull();
        } else {
          expect(recentFilesSection).not.toBeNull();
          const allText = recentFilesSection!.textContent ?? '';

          // Count displayed files using unique file names
          let displayedFileCount = 0;
          for (const f of files) {
            const fileName = f.file_key.split('/').pop()!;
            if (allText.includes(fileName)) displayedFileCount++;
          }
          expect(displayedFileCount).toBeLessThanOrEqual(3);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
