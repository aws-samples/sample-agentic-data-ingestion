// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The model that produced the mapping a schema-mapping approval payload carries.
 *
 * Read once here for every surface that renders that payload, because
 * `LGR-4854b3d-1` (**template / ratified**) rules that a payload expression is
 * not rewritten per display site. The sibling readers `approval-mapping-count.ts`,
 * `approval-unsourced-target-fields.ts` and `approval-reused-mapping.ts` follow
 * the same rule.
 *
 * ## Which key, and why not the pipeline's model
 *
 * `create_approval_payload` (`schema_mapping/lambda_function.py`) writes
 * `generated_by` into the payload: the Bedrock model id, or the
 * `fallback_substring_match` marker when no model produced the mapping
 * (P28 R3.1, R3.5). The payload travels with the mapping, so a run that reused an
 * earlier run's mapping still carries the model that produced it.
 *
 * The pipeline's current `ai_model` answers a different question: which model
 * the next generation will use. It is wrong for a reused mapping, and wrong for
 * any mapping produced before the pipeline's model changed or by the fallback.
 * Showing it there named a model that never saw the mapping. On the reuse
 * surface the run detail page showed "Unknown" instead, because nothing read
 * `generated_by` there.
 *
 * `ai_model_used` is read first because some payloads carry it beside
 * `generated_by`; on the mappings record the two are written together.
 *
 * ## Absence
 *
 * A blank or non-string value is absent, and the answer is `undefined`, which
 * `getModelDisplayName` renders as "Unknown". A blank is not a model id, and
 * returning it would hide a populated `generated_by` behind it.
 */
export function approvalMappingModel(payload: Record<string, unknown> | undefined | null): string | undefined {
  for (const key of ['ai_model_used', 'generated_by'] as const) {
    const value = payload?.[key];
    if (typeof value === 'string' && value.trim() !== '') return value;
  }
  return undefined;
}
