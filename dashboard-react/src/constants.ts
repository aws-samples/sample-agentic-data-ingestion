// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Shared constants for the AgenticDataIngestion dashboard. */

import type { StepDetail } from './types';
import { normalizeStepName } from './utils/step-names';

/**
 * Pipeline step execution order as Step Functions state ids (PascalCase).
 * Must match the state machine definition and the backend STEP_ORDER in
 * retry_utils.py (the retry endpoint accepts any known variant via
 * shared/step_names.from_any, so these ids remain valid POST values).
 */
export const PIPELINE_STEP_ORDER = [
  'FileValidation', 'SchemaExtract', 'SchemaCompare', 'CheckSchemaAction',
  'SchemaMapping', 'CheckSchemaMappingApproval', 'CreateCode', 'TestCode',
  'CheckCodeApproval', 'CreateInfrastructure', 'CreateGluePipeline',
  'CreateQualityChecks', 'RunPipeline',
] as const;

export type PipelineStepName = (typeof PIPELINE_STEP_ORDER)[number];

/** Option shape compatible with Cloudscape SelectProps.Option. */
export interface StepOption {
  label: string;
  value: string;
}

/**
 * Build the retry step selector options from a run's steps.
 *
 * Returns only completed + failed steps, ordered by PIPELINE_STEP_ORDER, with no duplicates.
 * Each option has a human-readable label (spaced PascalCase) and the step name as value.
 *
 * Eligibility matching runs in canonical step-name space (P28 R8.2, task
 * 14.2): a run's steps carry canonical snake_case names on new runs and
 * legacy PascalCase names on pre-rename runs — both must match their
 * PIPELINE_STEP_ORDER slot.
 */
export function getRetryStepOptions(
  steps: StepDetail[],
  stepOrder: readonly string[] = PIPELINE_STEP_ORDER,
): StepOption[] {
  const eligible = new Set(
    steps
      .filter((s) => {
        const st = (s.status ?? '').toLowerCase();
        return st === 'success' || st === 'succeeded' || st === 'completed'
            || st === 'error' || st === 'failed';
      })
      .map((s) => normalizeStepName(s.step_name)),
  );

  return stepOrder
    .filter((name) => eligible.has(normalizeStepName(name)))
    .map((name) => ({
      label: name.replace(/([a-z])([A-Z])/g, '$1 $2'),
      value: name,
    }));
}
