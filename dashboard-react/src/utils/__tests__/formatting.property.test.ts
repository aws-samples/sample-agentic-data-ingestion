// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for utility functions in formatting.ts
 * Uses fast-check with Vitest. Minimum 100 iterations per property.
 *
 * Feature: react-dashboard-parity
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  getConfidenceLevel,
  computeLastActivity,
  sortPipelines,
  computeApprovalMetrics,
  statusType,
} from '../formatting';
import type { Pipeline, PipelineFile, Approval } from '../../types';

// ---------------------------------------------------------------------------
// Arbitraries (smart generators)
// ---------------------------------------------------------------------------

/** Generates a valid ISO date string within a reasonable range. */
const isoDateArb = fc
  .date({
    min: new Date('2020-01-01T00:00:00Z'),
    max: new Date('2030-12-31T23:59:59Z'),
  })
  .map((d) => d.toISOString());

/** Generates a pipeline key like "pipeline-<id>". */
const pipelineKeyArb = fc.stringMatching(/^pipeline-[a-z0-9]{4,8}$/);

// Active and archived pipelines get disjoint key namespaces. In production
// `pipeline_key` is a unique primary key, so an active and an archived pipeline
// can never share one; without this split fast-check occasionally generates a
// collision, letting an archived pipeline's file be attributed to an active
// pipeline (files are matched to pipelines by key in computeLastActivity).
const activePipelineKeyArb = fc.stringMatching(/^active-[a-z0-9]{4,8}$/);
const archivedPipelineKeyArb = fc.stringMatching(/^archived-[a-z0-9]{4,8}$/);

/** Generates a pipeline name (non-empty alphanumeric with spaces). */
const pipelineNameArb = fc
  .stringOf(fc.constantFrom(...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '.split('')), { minLength: 1, maxLength: 30 })
  .filter((s) => s.trim().length > 0);

/** Generates a minimal active Pipeline object. */
const activePipelineArb = fc.record({
  pipeline_key: activePipelineKeyArb,
  pipeline_name: pipelineNameArb,
  status: fc.constantFrom('active', 'Active', 'ACTIVE', 'running', 'completed'),
  source_schema: fc.constant('src'),
  target_schema: fc.constant('tgt'),
  created_at: isoDateArb,
  updated_at: isoDateArb,
  file_count: fc.nat({ max: 500 }),
}) as fc.Arbitrary<Pipeline>;

/** Generates an archived Pipeline object. */
const archivedPipelineArb = fc.record({
  pipeline_key: archivedPipelineKeyArb,
  pipeline_name: pipelineNameArb,
  status: fc.constantFrom('archived', 'Archived', 'ARCHIVED'),
  source_schema: fc.constant('src'),
  target_schema: fc.constant('tgt'),
  created_at: isoDateArb,
  updated_at: isoDateArb,
  file_count: fc.nat({ max: 500 }),
}) as fc.Arbitrary<Pipeline>;

/** Generates an Approval with optional response_time_ms. */
const approvalArb: fc.Arbitrary<Approval> = fc.record({
  approval_id: fc.stringMatching(/^appr-[a-z0-9]{4,8}$/),
  pipeline_key: pipelineKeyArb,
  approval_type: fc.constantFrom('schema_mapping', 'code_generation', 'first_run'),
  status: fc.constantFrom('approved', 'rejected', 'pending'),
  created_at: isoDateArb,
  response_time_ms: fc.option(fc.integer({ min: 100, max: 600000 }), { nil: undefined }),
});

// ---------------------------------------------------------------------------
// Property 1: Last activity computation correctness
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 1: Last activity computation correctness', () => {
  /**
   * Validates: Requirements 1.3
   *
   * For any set of pipelines (some active, some archived) and associated files,
   * the computed last activity timestamp SHALL equal the maximum date among
   * active pipeline dates and their associated file dates, excluding all
   * archived pipelines.
   */
  it('computed last activity equals the max date among active pipelines and their files', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 10 }),
        fc.array(archivedPipelineArb, { minLength: 0, maxLength: 5 }),
        (activePipelines, archivedPipelines) => {
          const allPipelines = [...activePipelines, ...archivedPipelines];

          // Generate files: some for active pipelines, some for archived
          // Build files deterministically from pipeline data
          const activeFiles: PipelineFile[] = activePipelines.map((p) => ({
            file_key: `file-for-${p.pipeline_key}`,
            pipeline_key: p.pipeline_key,
            processed_at: p.updated_at, // reuse date for simplicity
          }));
          const archivedFiles: PipelineFile[] = archivedPipelines.map((p) => ({
            file_key: `file-for-${p.pipeline_key}`,
            pipeline_key: p.pipeline_key,
            processed_at: '2099-12-31T23:59:59.999Z', // very far future — should be excluded
          }));
          const allFiles = [...activeFiles, ...archivedFiles];

          const result = computeLastActivity(allPipelines, allFiles);

          if (activePipelines.length === 0) {
            // No active pipelines → no valid dates → '—'
            expect(result).toBe('—');
            return;
          }

          // Collect all dates that should be considered
          const expectedDates: number[] = [];
          for (const p of activePipelines) {
            expectedDates.push(new Date(p.updated_at).getTime());
            expectedDates.push(new Date(p.created_at).getTime());
          }
          for (const f of activeFiles) {
            expectedDates.push(new Date(f.processed_at).getTime());
          }

          const expectedMax = new Date(Math.max(...expectedDates)).toISOString();
          expect(result).toBe(expectedMax);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('archived pipeline dates and their files are never included', () => {
    fc.assert(
      fc.property(
        archivedPipelineArb,
        isoDateArb,
        (archivedPipeline, fileDate) => {
          const files: PipelineFile[] = [
            {
              file_key: 'f1',
              pipeline_key: archivedPipeline.pipeline_key,
              processed_at: fileDate,
            },
          ];

          const result = computeLastActivity([archivedPipeline], files);
          // Only archived pipelines → should return '—'
          expect(result).toBe('—');
        },
      ),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 6: Confidence classification
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 6: Confidence classification', () => {
  /**
   * Validates: Requirements 5.2
   *
   * For any confidence value c (0 ≤ c ≤ 1), getConfidenceLevel(c) SHALL return
   * "high" if c ≥ 0.8, "medium" if 0.6 ≤ c < 0.8, and "low" if c < 0.6.
   */
  it('classifies confidence values into correct levels across the full [0,1] range', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0, max: 1, noNaN: true }),
        (confidence) => {
          const level = getConfidenceLevel(confidence);

          if (confidence >= 0.8) {
            expect(level).toBe('high');
          } else if (confidence >= 0.6) {
            expect(level).toBe('medium');
          } else {
            expect(level).toBe('low');
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('boundary: 0.8 is always "high"', () => {
    expect(getConfidenceLevel(0.8)).toBe('high');
  });

  it('boundary: 0.6 is always "medium"', () => {
    expect(getConfidenceLevel(0.6)).toBe('medium');
  });

  it('boundary: values just below 0.6 are "low"', () => {
    fc.assert(
      fc.property(
        fc.double({ min: 0, max: 0.5999999999, noNaN: true }),
        (confidence) => {
          expect(getConfidenceLevel(confidence)).toBe('low');
        },
      ),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 14: Approval performance metrics computation
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 14: Approval performance metrics computation', () => {
  /**
   * Validates: Requirements 10.4
   *
   * For any set of Approval objects with response times, the computed average
   * response time SHALL equal the arithmetic mean of all response times, and
   * the approval rate SHALL equal the count of approved approvals divided by
   * the total count.
   */
  it('average response time equals arithmetic mean of response_time_ms values', () => {
    fc.assert(
      fc.property(
        fc.array(approvalArb, { minLength: 1, maxLength: 20 }),
        (approvals) => {
          const result = computeApprovalMetrics(approvals);

          const withResponseTime = approvals.filter(
            (a) => a.response_time_ms != null,
          );

          if (withResponseTime.length === 0) {
            expect(result.averageResponseTime).toBe(0);
          } else {
            const expectedMean =
              withResponseTime.reduce((sum, a) => sum + (a.response_time_ms ?? 0), 0) /
              withResponseTime.length;
            expect(result.averageResponseTime).toBeCloseTo(expectedMean, 5);
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('approval rate equals approved count / total count', () => {
    fc.assert(
      fc.property(
        fc.array(approvalArb, { minLength: 1, maxLength: 20 }),
        (approvals) => {
          const result = computeApprovalMetrics(approvals);

          const approvedCount = approvals.filter(
            (a) => a.status?.toLowerCase() === 'approved',
          ).length;
          const expectedRate = approvedCount / approvals.length;

          expect(result.approvalRate).toBeCloseTo(expectedRate, 10);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('empty approvals returns zero for both metrics', () => {
    const result = computeApprovalMetrics([]);
    expect(result.averageResponseTime).toBe(0);
    expect(result.approvalRate).toBe(0);
  });

  it('approval rate is always between 0 and 1 inclusive', () => {
    fc.assert(
      fc.property(
        fc.array(approvalArb, { minLength: 1, maxLength: 20 }),
        (approvals) => {
          const result = computeApprovalMetrics(approvals);
          expect(result.approvalRate).toBeGreaterThanOrEqual(0);
          expect(result.approvalRate).toBeLessThanOrEqual(1);
        },
      ),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 18: Pipeline sorting correctness
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 18: Pipeline sorting correctness', () => {
  /**
   * Validates: Requirements 16.2
   *
   * For any list of Pipeline objects, sorting by "name" SHALL produce
   * alphabetical order by pipeline_name, sorting by "activity" SHALL produce
   * descending order by updated_at date, and sorting by "files" SHALL produce
   * descending order by file count.
   */
  it('sorting by "name" produces alphabetical order (case-insensitive)', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 20 }),
        (pipelines) => {
          const sorted = sortPipelines(pipelines, 'name');

          // Verify pairwise ordering
          for (let i = 1; i < sorted.length; i++) {
            const cmp = (sorted[i - 1].pipeline_name ?? '').localeCompare(
              sorted[i].pipeline_name ?? '',
              undefined,
              { sensitivity: 'base' },
            );
            expect(cmp).toBeLessThanOrEqual(0);
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('sorting by "activity" produces descending order by updated_at', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 20 }),
        (pipelines) => {
          const sorted = sortPipelines(pipelines, 'activity');

          // Verify pairwise descending by date
          for (let i = 1; i < sorted.length; i++) {
            const dateA = new Date(sorted[i - 1].updated_at ?? 0).getTime();
            const dateB = new Date(sorted[i].updated_at ?? 0).getTime();
            expect(dateA).toBeGreaterThanOrEqual(dateB);
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('sorting by "files" produces descending order by file_count', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 20 }),
        (pipelines) => {
          const sorted = sortPipelines(pipelines, 'files');

          // Verify pairwise descending by file count
          for (let i = 1; i < sorted.length; i++) {
            expect(sorted[i - 1].file_count ?? 0).toBeGreaterThanOrEqual(
              sorted[i].file_count ?? 0,
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('sorting does not mutate the original array', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 20 }),
        fc.constantFrom('name' as const, 'activity' as const, 'files' as const),
        (pipelines, sortBy) => {
          const originalKeys = pipelines.map((p) => p.pipeline_key);
          sortPipelines(pipelines, sortBy);
          const afterKeys = pipelines.map((p) => p.pipeline_key);
          expect(afterKeys).toEqual(originalKeys);
        },
      ),
      { numRuns: 100 },
    );
  });

  it('sorting preserves all elements (same length, same set)', () => {
    fc.assert(
      fc.property(
        fc.array(activePipelineArb, { minLength: 0, maxLength: 20 }),
        fc.constantFrom('name' as const, 'activity' as const, 'files' as const),
        (pipelines, sortBy) => {
          const sorted = sortPipelines(pipelines, sortBy);
          expect(sorted.length).toBe(pipelines.length);

          // Same set of pipeline keys
          const originalKeys = pipelines.map((p) => p.pipeline_key).sort();
          const sortedKeys = sorted.map((p) => p.pipeline_key).sort();
          expect(sortedKeys).toEqual(originalKeys);
        },
      ),
      { numRuns: 100 },
    );
  });
});


// ---------------------------------------------------------------------------
// Property: statusType always returns a valid CloudscapeStatusType
// ---------------------------------------------------------------------------

describe('Feature: p1-fixes, Property: statusType returns valid CloudscapeStatusType', () => {
  /**
   * Validates: Requirements 5.1
   *
   * For any arbitrary string input, statusType() SHALL return one of the 5
   * valid Cloudscape StatusIndicator types: 'success', 'error', 'warning',
   * 'info', or 'in-progress'.
   */
  const VALID_TYPES = new Set(['success', 'error', 'warning', 'info', 'in-progress']);

  it('for any string, returns one of the 5 valid CloudscapeStatusType values', () => {
    fc.assert(
      fc.property(
        fc.string(),
        (input) => {
          const result = statusType(input);
          expect(VALID_TYPES.has(result)).toBe(true);
        },
      ),
      { numRuns: 100 },
    );
  });
});
