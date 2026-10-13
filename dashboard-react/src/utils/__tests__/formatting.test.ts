// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  getConfidenceLevel,
  computeLastActivity,
  sortPipelines,
  computeApprovalMetrics,
  getStatusIcon,
  getStatusColor,
  formatRelativeTime,
  formatStepName,
  getModelDisplayName,
  calculateWaitingTime,
  statusType,
  FALLBACK_MODEL_SENTINEL,
  FALLBACK_MODEL_DISPLAY,
} from '../formatting';
import type { Pipeline, PipelineFile, Approval } from '../../types';

// --- getConfidenceLevel ---
describe('getConfidenceLevel', () => {
  it('returns "high" for confidence >= 0.8', () => {
    expect(getConfidenceLevel(0.8)).toBe('high');
    expect(getConfidenceLevel(0.95)).toBe('high');
    expect(getConfidenceLevel(1.0)).toBe('high');
  });

  it('returns "medium" for 0.6 <= confidence < 0.8', () => {
    expect(getConfidenceLevel(0.6)).toBe('medium');
    expect(getConfidenceLevel(0.7)).toBe('medium');
    expect(getConfidenceLevel(0.79)).toBe('medium');
  });

  it('returns "low" for confidence < 0.6', () => {
    expect(getConfidenceLevel(0.0)).toBe('low');
    expect(getConfidenceLevel(0.59)).toBe('low');
    expect(getConfidenceLevel(0.3)).toBe('low');
  });
});

// --- computeLastActivity ---
describe('computeLastActivity', () => {
  it('returns "—" for empty inputs', () => {
    expect(computeLastActivity([], [])).toBe('—');
  });

  it('returns the most recent date from active pipelines', () => {
    const pipelines: Pipeline[] = [
      { pipeline_key: 'p1', pipeline_name: 'P1', status: 'active', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-06-01T00:00:00Z' },
      { pipeline_key: 'p2', pipeline_name: 'P2', status: 'active', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-03-01T00:00:00Z' },
    ];
    const result = computeLastActivity(pipelines, []);
    expect(result).toBe(new Date('2024-06-01T00:00:00Z').toISOString());
  });

  it('excludes archived pipelines', () => {
    const pipelines: Pipeline[] = [
      { pipeline_key: 'p1', pipeline_name: 'P1', status: 'archived', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      { pipeline_key: 'p2', pipeline_name: 'P2', status: 'active', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-03-01T00:00:00Z' },
    ];
    const result = computeLastActivity(pipelines, []);
    expect(result).toBe(new Date('2024-03-01T00:00:00Z').toISOString());
  });

  it('considers file dates from active pipelines', () => {
    const pipelines: Pipeline[] = [
      { pipeline_key: 'p1', pipeline_name: 'P1', status: 'active', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-03-01T00:00:00Z' },
    ];
    const files: PipelineFile[] = [
      { file_key: 'f1', pipeline_key: 'p1', processed_at: '2024-09-15T00:00:00Z' },
    ];
    const result = computeLastActivity(pipelines, files);
    expect(result).toBe(new Date('2024-09-15T00:00:00Z').toISOString());
  });

  it('excludes files belonging to archived pipelines', () => {
    const pipelines: Pipeline[] = [
      { pipeline_key: 'p1', pipeline_name: 'P1', status: 'archived', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z' },
      { pipeline_key: 'p2', pipeline_name: 'P2', status: 'active', source_schema: '', target_schema: '', created_at: '2024-01-01T00:00:00Z', updated_at: '2024-02-01T00:00:00Z' },
    ];
    const files: PipelineFile[] = [
      { file_key: 'f1', pipeline_key: 'p1', processed_at: '2025-01-01T00:00:00Z' },
    ];
    const result = computeLastActivity(pipelines, files);
    expect(result).toBe(new Date('2024-02-01T00:00:00Z').toISOString());
  });
});


// --- sortPipelines ---
describe('sortPipelines', () => {
  const pipelines: Pipeline[] = [
    { pipeline_key: 'p1', pipeline_name: 'Zebra', status: 'active', source_schema: '', target_schema: '', created_at: '', updated_at: '2024-01-01T00:00:00Z', file_count: 5 },
    { pipeline_key: 'p2', pipeline_name: 'Alpha', status: 'active', source_schema: '', target_schema: '', created_at: '', updated_at: '2024-06-01T00:00:00Z', file_count: 2 },
    { pipeline_key: 'p3', pipeline_name: 'Mango', status: 'active', source_schema: '', target_schema: '', created_at: '', updated_at: '2024-03-01T00:00:00Z', file_count: 10 },
  ];

  it('sorts by name alphabetically', () => {
    const sorted = sortPipelines(pipelines, 'name');
    expect(sorted.map((p) => p.pipeline_name)).toEqual(['Alpha', 'Mango', 'Zebra']);
  });

  it('sorts by activity (most recent first)', () => {
    const sorted = sortPipelines(pipelines, 'activity');
    expect(sorted.map((p) => p.pipeline_name)).toEqual(['Alpha', 'Mango', 'Zebra']);
  });

  it('sorts by files (most files first)', () => {
    const sorted = sortPipelines(pipelines, 'files');
    expect(sorted.map((p) => p.pipeline_name)).toEqual(['Mango', 'Zebra', 'Alpha']);
  });

  it('does not mutate the original array', () => {
    const original = [...pipelines];
    sortPipelines(pipelines, 'name');
    expect(pipelines).toEqual(original);
  });
});

// --- computeApprovalMetrics ---
describe('computeApprovalMetrics', () => {
  it('returns zeros for empty array', () => {
    const result = computeApprovalMetrics([]);
    expect(result.averageResponseTime).toBe(0);
    expect(result.approvalRate).toBe(0);
  });

  it('computes correct approval rate', () => {
    const approvals: Approval[] = [
      { approval_id: '1', pipeline_key: 'p1', approval_type: 'schema_mapping', status: 'approved', created_at: '' },
      { approval_id: '2', pipeline_key: 'p1', approval_type: 'code_generation', status: 'rejected', created_at: '' },
      { approval_id: '3', pipeline_key: 'p1', approval_type: 'first_run', status: 'approved', created_at: '' },
    ];
    const result = computeApprovalMetrics(approvals);
    expect(result.approvalRate).toBeCloseTo(2 / 3);
  });

  it('computes correct average response time', () => {
    const approvals: Approval[] = [
      { approval_id: '1', pipeline_key: 'p1', approval_type: 'schema_mapping', status: 'approved', created_at: '', response_time_ms: 1000 },
      { approval_id: '2', pipeline_key: 'p1', approval_type: 'code_generation', status: 'approved', created_at: '', response_time_ms: 3000 },
    ];
    const result = computeApprovalMetrics(approvals);
    expect(result.averageResponseTime).toBe(2000);
  });

  it('ignores approvals without response_time_ms for average', () => {
    const approvals: Approval[] = [
      { approval_id: '1', pipeline_key: 'p1', approval_type: 'schema_mapping', status: 'approved', created_at: '', response_time_ms: 1000 },
      { approval_id: '2', pipeline_key: 'p1', approval_type: 'code_generation', status: 'approved', created_at: '' },
    ];
    const result = computeApprovalMetrics(approvals);
    expect(result.averageResponseTime).toBe(1000);
  });
});

// --- getStatusIcon ---
describe('getStatusIcon', () => {
  it('maps known statuses to correct icons', () => {
    expect(getStatusIcon('completed')).toBe('✅');
    expect(getStatusIcon('success')).toBe('✅');
    expect(getStatusIcon('succeeded')).toBe('✅');
    expect(getStatusIcon('failed')).toBe('❌');
    expect(getStatusIcon('error')).toBe('❌');
    expect(getStatusIcon('running')).toBe('🔄');
    expect(getStatusIcon('pending')).toBe('⏳');
    expect(getStatusIcon('skipped')).toBe('⏭️');
  });

  it('is case-insensitive', () => {
    expect(getStatusIcon('COMPLETED')).toBe('✅');
    expect(getStatusIcon('Failed')).toBe('❌');
  });

  it('returns default icon for unknown status', () => {
    expect(getStatusIcon('unknown_status')).toBe('⚙️');
  });
});

// --- getStatusColor ---
describe('getStatusColor', () => {
  it('maps known statuses to Cloudscape color tokens', () => {
    expect(getStatusColor('completed')).toBe('text-status-success');
    expect(getStatusColor('failed')).toBe('text-status-error');
    expect(getStatusColor('running')).toBe('text-status-info');
    expect(getStatusColor('pending')).toBe('text-status-inactive');
  });

  it('returns inactive for unknown status', () => {
    expect(getStatusColor('something_else')).toBe('text-status-inactive');
  });
});

// --- formatRelativeTime ---
describe('formatRelativeTime', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2024-06-15T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns "Just now" for very recent dates', () => {
    expect(formatRelativeTime('2024-06-15T12:00:00Z')).toBe('Just now');
  });

  it('returns minutes ago', () => {
    expect(formatRelativeTime('2024-06-15T11:30:00Z')).toBe('30m ago');
  });

  it('returns hours ago', () => {
    expect(formatRelativeTime('2024-06-15T09:00:00Z')).toBe('3h ago');
  });

  it('returns days ago', () => {
    expect(formatRelativeTime('2024-06-13T12:00:00Z')).toBe('2d ago');
  });

  it('returns "Unknown" for invalid date', () => {
    expect(formatRelativeTime('not-a-date')).toBe('Unknown');
  });

  it('returns "Just now" for future dates', () => {
    expect(formatRelativeTime('2025-01-01T00:00:00Z')).toBe('Just now');
  });
});

// --- formatStepName ---
describe('formatStepName', () => {
  it('maps known step names', () => {
    expect(formatStepName('file_validation')).toBe('File Validation');
    expect(formatStepName('schema_extract')).toBe('Schema Extraction');
    expect(formatStepName('schema_mapping')).toBe('Schema Mapping');
    expect(formatStepName('code_generation')).toBe('Code Generation');
    expect(formatStepName('first_run')).toBe('First Run');
  });

  it('keeps established labels for the canonical P28 step names', () => {
    // Task 14.2 renamed the ROW keys, not the display labels.
    expect(formatStepName('create_code')).toBe('Code Generation');
    expect(formatStepName('test_code')).toBe('Code Testing');
  });

  it('converts unknown snake_case to Title Case', () => {
    expect(formatStepName('my_custom_step')).toBe('My Custom Step');
  });
});

// --- getModelDisplayName ---
describe('getModelDisplayName', () => {
  it('returns "Unknown" for null/undefined', () => {
    expect(getModelDisplayName(null)).toBe('Unknown');
    expect(getModelDisplayName(undefined)).toBe('Unknown');
    expect(getModelDisplayName('')).toBe('Unknown');
  });

  it('maps Claude 4 models', () => {
    expect(getModelDisplayName('anthropic.claude-4-sonnet-20250514')).toBe('Claude 4 Sonnet');
    expect(getModelDisplayName('anthropic.claude-4-opus-20250514')).toBe('Claude 4 Opus');
    expect(getModelDisplayName('anthropic.claude-4-haiku-20250514')).toBe('Claude 4 Haiku');
  });

  it('maps Claude 3.5 models (both dot and dash variants)', () => {
    expect(getModelDisplayName('anthropic.claude-3-5-sonnet-20241022-v2:0')).toBe('Claude 3.5 Sonnet');
    expect(getModelDisplayName('anthropic.claude-3.5-sonnet-20241022')).toBe('Claude 3.5 Sonnet');
    expect(getModelDisplayName('anthropic.claude-3-5-haiku-20241022-v1:0')).toBe('Claude 3.5 Haiku');
  });

  it('maps Claude 3 models', () => {
    expect(getModelDisplayName('anthropic.claude-3-haiku-20240307-v1:0')).toBe('Claude 3 Haiku');
    expect(getModelDisplayName('anthropic.claude-3-sonnet-20240229-v1:0')).toBe('Claude 3 Sonnet');
    expect(getModelDisplayName('anthropic.claude-3-opus-20240229-v1:0')).toBe('Claude 3 Opus');
  });

  it('maps other model families', () => {
    expect(getModelDisplayName('amazon.titan-text-express-v1')).toBe('Amazon Titan');
    expect(getModelDisplayName('meta.llama3-70b-instruct-v1:0')).toBe('Llama');
    expect(getModelDisplayName('mistral.mistral-7b-instruct-v0:2')).toBe('Mistral');
    expect(getModelDisplayName('amazon.nova-pro-v1:0')).toBe('Amazon Nova');
  });

  it('falls back to formatted model ID part', () => {
    expect(getModelDisplayName('vendor.some-model-v1')).toBe('Some Model V1');
  });

  it('truncates long IDs without dots', () => {
    const longId = 'a'.repeat(30);
    expect(getModelDisplayName(longId)).toBe('a'.repeat(20) + '...');
  });

  // Family-first ids (Claude 4 and later). The version shown must be the one in
  // the id. Until this was fixed, us.anthropic.claude-sonnet-4-6 (the default
  // model) displayed as "Claude Sonnet 4", and this test asserted that.
  it('maps Claude 4 dateless inference-profile ids, preserving the minor version', () => {
    expect(getModelDisplayName('us.anthropic.claude-sonnet-4-5-v1:0')).toBe('Claude Sonnet 4.5');
    expect(getModelDisplayName('us.anthropic.claude-sonnet-4-6')).toBe('Claude Sonnet 4.6');
    expect(getModelDisplayName('us.anthropic.claude-opus-4-v1:0')).toBe('Claude Opus 4');
    expect(getModelDisplayName('us.anthropic.claude-haiku-4-v1:0')).toBe('Claude Haiku 4');
  });

  it('never reads a date suffix as a minor version', () => {
    expect(getModelDisplayName('us.anthropic.claude-sonnet-4-20250514-v1:0')).toBe('Claude Sonnet 4');
    expect(getModelDisplayName('anthropic.claude-sonnet-4-5-20250929-v1:0')).toBe('Claude Sonnet 4.5');
    expect(getModelDisplayName('us.anthropic.claude-opus-4-1-20250805-v1:0')).toBe('Claude Opus 4.1');
    expect(getModelDisplayName('us.anthropic.claude-haiku-4-5-20251001-v1:0')).toBe('Claude Haiku 4.5');
  });

  it('names a model released after this list was written, with any profile prefix', () => {
    expect(getModelDisplayName('global.anthropic.claude-opus-4-7')).toBe('Claude Opus 4.7');
    expect(getModelDisplayName('us.anthropic.claude-sonnet-5')).toBe('Claude Sonnet 5');
    expect(getModelDisplayName('eu.anthropic.claude-sonnet-5-5-v1:0')).toBe('Claude Sonnet 5.5');
  });
});

// --- getModelDisplayName: fallback provenance (P28 R3.1) ---
//
// A mapping that fell back to substring matching is stamped
// `ai_model_used = 'fallback_substring_match'` so it never claims a Bedrock
// model. That value is correct and must stay stored as-is; only its DISPLAY was
// wrong. It matched no model branch and split on '.' into one part, so it fell to
// the trailing truncation and rendered `fallback_substring_m...` beneath a label
// reading "AI Model" — indistinguishable from data corruption.
//
// These assertions are at the formatter, which is where the fix is. All five
// rendering sites call it with no local formatting of their own:
// PipelineOverviewPage.tsx:460 and :586, MappingViewer.tsx:152,
// PipelineCard.tsx:109, ExecutionsPage.tsx:401.
describe('getModelDisplayName — fallback provenance', () => {
  it('renders the sentinel as an explicit "no model" statement', () => {
    expect(getModelDisplayName(FALLBACK_MODEL_SENTINEL)).toBe(FALLBACK_MODEL_DISPLAY);
  });

  it('no longer leaks the raw marker or its truncated form', () => {
    const rendered = getModelDisplayName(FALLBACK_MODEL_SENTINEL);

    // The exact string the user reported.
    expect(rendered).not.toBe('fallback_substring_m...');
    expect(rendered).not.toContain(FALLBACK_MODEL_SENTINEL);
    expect(rendered).not.toContain('substring_m');
    expect(rendered).not.toMatch(/\.\.\.$/);
  });

  it('states that no model was used without naming one', () => {
    // The point of P28 R3.1 is that a fallback mapping never claims a model, so
    // the display must not read like a model name either.
    expect(FALLBACK_MODEL_DISPLAY).not.toMatch(/claude|titan|llama|mistral|nova|sonnet|haiku|opus/i);
    expect(FALLBACK_MODEL_DISPLAY.toLowerCase()).toContain('no model');

    // Short enough for the narrow key-value slots the surfaces use, and made of
    // words so it wraps instead of truncating like an unbroken token would.
    expect(FALLBACK_MODEL_DISPLAY.length).toBeLessThanOrEqual(28);
    expect(FALLBACK_MODEL_DISPLAY).toContain(' ');
  });

  it('does not read as an error or a failure', () => {
    // The stored value is correct and the state is expected. Wording that
    // implied breakage would re-report honest provenance as a defect, which is
    // the reading this fix exists to remove.
    expect(FALLBACK_MODEL_DISPLAY).not.toMatch(/error|invalid|unknown|corrupt|fail|missing|none/i);
  });

  it('handles a sibling fallback marker the backend may add later', () => {
    // ApprovalArtifact.tsx:865 already classifies provenance by the `fallback_`
    // prefix. A new marker must not regress to the truncation branch.
    expect(getModelDisplayName('fallback_exact_match')).toBe(FALLBACK_MODEL_DISPLAY);
    expect(getModelDisplayName('fallback_')).toBe(FALLBACK_MODEL_DISPLAY);
  });

  it('leaves a real model id that merely mentions matching alone', () => {
    // The prefix rule is anchored at the start, so a vendor-prefixed id is not
    // swallowed by it.
    expect(getModelDisplayName('anthropic.claude-3-5-sonnet-20241022-v2:0')).toBe('Claude 3.5 Sonnet');
    expect(getModelDisplayName('vendor.fallback-tuned-model')).toBe('Fallback Tuned Model');
  });
});

// --- calculateWaitingTime ---
describe('calculateWaitingTime', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2024-06-15T14:30:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns hours and minutes format', () => {
    expect(calculateWaitingTime('2024-06-15T12:00:00Z')).toBe('2h 30m');
  });

  it('returns days and hours format', () => {
    expect(calculateWaitingTime('2024-06-13T14:30:00Z')).toBe('2d 0h');
  });

  it('returns minutes only for short waits', () => {
    expect(calculateWaitingTime('2024-06-15T14:15:00Z')).toBe('15m');
  });

  it('uses decidedAt when provided', () => {
    expect(calculateWaitingTime('2024-06-15T12:00:00Z', '2024-06-15T13:00:00Z')).toBe('1h 0m');
  });
});


// --- statusType ---
describe('statusType', () => {
  const cases: [string, ReturnType<typeof statusType>][] = [
    // success
    ['completed', 'success'],
    ['active', 'success'],
    ['succeeded', 'success'],
    ['ready', 'success'],
    ['approved', 'success'],
    // error
    ['failed', 'error'],
    ['error', 'error'],
    ['stopped', 'error'],
    ['rejected', 'error'],
    ['expired', 'error'],
    // in-progress
    ['running', 'in-progress'],
    ['in_progress', 'in-progress'],
    ['in-progress', 'in-progress'],
    // warning
    ['waiting_approval', 'warning'],
    ['pending', 'warning'],
    ['aborted', 'warning'],
    ['timed_out', 'warning'],
    // info (default)
    ['unknown', 'info'],
    ['', 'info'],
    ['something_random', 'info'],
  ];

  it.each(cases)('maps "%s" → "%s"', (input, expected) => {
    expect(statusType(input)).toBe(expected);
  });

  it('is case-insensitive', () => {
    expect(statusType('COMPLETED')).toBe('success');
    expect(statusType('Completed')).toBe('success');
    expect(statusType('completed')).toBe('success');
    expect(statusType('FAILED')).toBe('error');
    expect(statusType('Running')).toBe('in-progress');
    expect(statusType('PENDING')).toBe('warning');
  });
});
