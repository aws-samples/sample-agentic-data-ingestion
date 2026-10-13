// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// P28 R8.2 (task 14.2): canonical step-name normalization for the dashboard.
// TS mirror of shared/step_names.py from_any — every variant the backend has
// ever written must normalize to the canonical snake_case name.
import { describe, it, expect } from 'vitest';
import {
  CANONICAL_STEP_ORDER,
  CANONICAL_TASK_STEPS,
  normalizeStepName,
  orderStepKeys,
  sameStep,
} from '../step-names';
import { PIPELINE_STEP_ORDER } from '../../constants';

describe('normalizeStepName', () => {
  it.each(CANONICAL_STEP_ORDER.map((s) => [s]))('canonical name %s maps to itself', (name) => {
    expect(normalizeStepName(name)).toBe(name);
  });

  // Cataloged variants (mirror of shared/step_names.py test table):
  // SFN PascalCase state ids + legacy state-row/approval variants.
  it.each([
    ['FileValidation', 'file_validation'],
    ['SchemaExtract', 'schema_extract'],
    ['SchemaCompare', 'schema_compare'],
    ['CheckSchemaAction', 'check_schema_action'],
    ['SchemaMapping', 'schema_mapping'],
    ['CheckSchemaMappingApproval', 'check_schema_mapping_approval'],
    ['CreateCode', 'create_code'],
    ['TestCode', 'test_code'],
    ['CheckCodeApproval', 'check_code_approval'],
    ['CreateInfrastructure', 'create_infrastructure'],
    ['CreateGluePipeline', 'create_glue_pipeline'],
    ['CreateQualityChecks', 'create_quality_checks'],
    ['RunPipeline', 'run_pipeline'],
    // Legacy state-row names (pre-rename runs persist these)
    ['CodeGeneration', 'create_code'],
    ['CodeTesting', 'test_code'],
    // approval_type / phantom-row / agent-trace variants
    ['code_generation', 'create_code'],
    ['code_testing', 'test_code'],
    ['infrastructure', 'create_infrastructure'],
    ['first_run', 'create_quality_checks'],
    ['schema_mapping_approval', 'check_schema_mapping_approval'],
    ['code_approval', 'check_code_approval'],
  ])('variant %s normalizes to %s', (variant, canonical) => {
    expect(normalizeStepName(variant)).toBe(canonical);
  });

  it('is case- and separator-insensitive for known names', () => {
    expect(normalizeStepName('Run-Pipeline')).toBe('run_pipeline');
    expect(normalizeStepName('FILE_VALIDATION')).toBe('file_validation');
    expect(normalizeStepName('codegeneration')).toBe('create_code');
  });

  it('passes unknown names through unchanged (tolerant display)', () => {
    expect(normalizeStepName('pipeline_failure')).toBe('pipeline_failure');
    expect(normalizeStepName('self_healing')).toBe('self_healing');
    expect(normalizeStepName('')).toBe('');
  });

  it('covers every PIPELINE_STEP_ORDER SFN state id', () => {
    for (const sfnId of PIPELINE_STEP_ORDER) {
      expect(CANONICAL_STEP_ORDER).toContain(normalizeStepName(sfnId));
    }
  });
});

describe('sameStep', () => {
  it('matches across naming generations', () => {
    expect(sameStep('CodeGeneration', 'create_code')).toBe(true);
    expect(sameStep('code_generation', 'CreateCode')).toBe(true);
    expect(sameStep('RunPipeline', 'run_pipeline')).toBe(true);
  });

  it('distinguishes different steps', () => {
    expect(sameStep('create_code', 'test_code')).toBe(false);
    expect(sameStep('pipeline_failure', 'run_pipeline')).toBe(false);
  });
});

describe('orderStepKeys', () => {
  it('orders new-run canonical keys by execution order', () => {
    const keys = ['run_pipeline', 'file_validation', 'create_code', 'schema_extract'];
    expect(orderStepKeys(keys)).toEqual(['file_validation', 'schema_extract', 'create_code', 'run_pipeline']);
  });

  it('orders old-run legacy keys by execution order, preserving raw keys', () => {
    const keys = ['RunPipeline', 'file_validation', 'CodeGeneration', 'CodeTesting'];
    expect(orderStepKeys(keys)).toEqual(['file_validation', 'CodeGeneration', 'CodeTesting', 'RunPipeline']);
  });

  it('dedupes naming variants of the same step, preferring canonical', () => {
    const keys = ['CodeGeneration', 'create_code', 'file_validation'];
    expect(orderStepKeys(keys)).toEqual(['file_validation', 'create_code']);
  });

  it('appends unknown keys after known steps', () => {
    const keys = ['self_healing', 'run_pipeline', 'file_validation'];
    expect(orderStepKeys(keys)).toEqual(['file_validation', 'run_pipeline', 'self_healing']);
  });

  it('covers all ten task steps in canonical order', () => {
    const shuffled = [...CANONICAL_TASK_STEPS].reverse();
    expect(orderStepKeys(shuffled)).toEqual([...CANONICAL_TASK_STEPS]);
  });
});
