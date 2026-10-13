// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `approvalMappingModel` — the model that produced a schema-mapping payload's
 * mapping. Expectations come from the producer's contract:
 * `create_approval_payload` writes `generated_by` (a Bedrock model id, or the
 * fallback marker), and a blank or missing value is not a model.
 */
import { describe, it, expect } from 'vitest';
import { approvalMappingModel } from '../approval-mapping-model';
import { FALLBACK_MODEL_SENTINEL } from '../formatting';

describe('approvalMappingModel', () => {
  it('reads generated_by, the key the producer writes', () => {
    expect(approvalMappingModel({ generated_by: 'us.anthropic.claude-sonnet-5-5' })).toBe(
      'us.anthropic.claude-sonnet-5-5',
    );
  });

  it('passes the fallback marker through, so it renders as "no model used"', () => {
    expect(approvalMappingModel({ generated_by: FALLBACK_MODEL_SENTINEL })).toBe(FALLBACK_MODEL_SENTINEL);
  });

  it('prefers ai_model_used when a payload carries both', () => {
    expect(approvalMappingModel({ ai_model_used: 'anthropic.claude-sonnet-5-5', generated_by: 'x.y' })).toBe(
      'anthropic.claude-sonnet-5-5',
    );
  });

  it('does not let a blank ai_model_used hide a populated generated_by', () => {
    expect(approvalMappingModel({ ai_model_used: '', generated_by: 'us.anthropic.claude-sonnet-5-5' })).toBe(
      'us.anthropic.claude-sonnet-5-5',
    );
    expect(approvalMappingModel({ ai_model_used: '  ', generated_by: 'us.anthropic.claude-sonnet-5-5' })).toBe(
      'us.anthropic.claude-sonnet-5-5',
    );
  });

  it.each([
    ['no payload', undefined],
    ['null', null],
    ['no keys', {}],
    ['a blank generated_by', { generated_by: '' }],
    ['a non-string generated_by', { generated_by: 4 }],
    ['a null generated_by', { generated_by: null }],
  ])('answers undefined for %s', (_label, payload) => {
    expect(approvalMappingModel(payload as Record<string, unknown> | undefined | null)).toBeUndefined();
  });
});
