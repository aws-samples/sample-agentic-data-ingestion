// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 8.7 — `classifySchemaDeleteFailure`.
 *
 * The page test asserts what each branch **renders**; this asserts that the four
 * branches are distinguished at all, from the response bodies tasks 8.4–8.6
 * actually landed (`handlers/schemas.py:2027-2072`).
 *
 * Design §5 risk 6 is the reason this has its own file: the `503` is the branch
 * with no user story, so it is the one a generic-error collapse loses first, and
 * a collapse here would be invisible in a rendering test that only checked "some
 * error appeared".
 *
 * Requirements: 3.6, 3.9
 */
import { describe, it, expect } from 'vitest';
import { ApiError } from '../../services/api-client';
import { classifySchemaDeleteFailure } from '../schema-delete-failure';

const SCHEMA_ID = 'uploaded_schema_1785844048926';

describe('classifySchemaDeleteFailure', () => {
  it('classifies the 409 and carries the pipelines the API named, not a count', () => {
    const failure = classifySchemaDeleteFailure(
      new ApiError(409, {
        error: 'Schema is referenced by 2 pipeline(s) and was not deleted.',
        reason: 'schema_in_use',
        schemaId: SCHEMA_ID,
        checked: ['target_schema_name-index query'],
        referencingPipelines: [
          { pipelineId: 'testing-39eddb17', pipelineName: 'nightly orders' },
          { pipelineId: 'hello123-c4827272', pipelineName: 'hello123-c4827272' },
        ],
      }),
    );

    expect(failure.kind).toBe('in_use');
    expect(failure.referencingPipelines).toHaveLength(2);
    expect(failure.referencingPipelines[0].pipelineName).toBe('nightly orders');
    // The API's own sentence, not one composed here (R3.6).
    expect(failure.message).toMatch(/was not deleted/);
    // Trying again cannot change the answer — the pipelines have to change first.
    expect(failure.retryable).toBe(false);
  });

  it('classifies the 400 and keeps the catalog source', () => {
    const failure = classifySchemaDeleteFailure(
      new ApiError(400, {
        error: 'Schema is a view of a catalog object (s3_table_bucket).',
        reason: 'catalog_derived_schema',
        schemaId: 's3_table_testingbucket_ns_tn',
        source: 's3_table_bucket',
      }),
    );

    expect(failure.kind).toBe('catalog_derived');
    expect(failure.source).toBe('s3_table_bucket');
    expect(failure.retryable).toBe(false);
    // This response carries no `checked` — the reference check never runs.
    expect(failure.checked).toEqual([]);
  });

  /** The branch design §5 risk 6 is about. */
  it('classifies the 503 as retryable and keeps only the lookups that completed', () => {
    const failure = classifySchemaDeleteFailure(
      new ApiError(503, {
        error: 'Could not establish whether any pipeline references the schema. Please try again.',
        reason: 'reference_check_incomplete',
        retryable: true,
        checked: ['target_schema_name-index query'],
        schemaId: SCHEMA_ID,
        details: 'reference scan budget exhausted',
      }),
    );

    expect(failure.kind).toBe('check_incomplete');
    expect(failure.retryable).toBe(true);
    expect(failure.checked).toEqual(['target_schema_name-index query']);
    expect(failure.details).toBe('reference scan budget exhausted');
    // Not one of the other two, and it names no pipelines because none were found.
    expect(failure.referencingPipelines).toEqual([]);
  });

  /**
   * A `500` is deliberately **not** one of the three. The three refusals all
   * guarantee the object still exists (R3.10 orders the reference check before the
   * delete); a `500` guarantees nothing, and it must not be re-offered as
   * retryable, because retrying a delete that may have half-happened is not the
   * same safe operation the `503` retry is.
   */
  it('does not fold an unclassified failure into one of the three refusals', () => {
    const failure = classifySchemaDeleteFailure(
      new ApiError(500, { error: 'Failed to delete schema', details: 'AccessDenied' }),
    );

    expect(failure.kind).toBe('unknown');
    expect(failure.retryable).toBe(false);
    expect(failure.details).toBe('AccessDenied');
  });

  /**
   * `reason` is read **before** `status`, because the reason is unique per branch
   * and the status is not — `400` is `catalog_derived_schema` here but is also
   * what a malformed request answers.
   */
  it('prefers the reason over the status when they could disagree', () => {
    const failure = classifySchemaDeleteFailure(
      new ApiError(400, { error: 'x', reason: 'reference_check_incomplete', checked: [] }),
    );
    expect(failure.kind).toBe('check_incomplete');
  });

  /** Falling back to the status keeps the branches reachable without a reason. */
  it('falls back to the status when the body carries no reason', () => {
    expect(classifySchemaDeleteFailure(new ApiError(409, { error: 'x' })).kind).toBe('in_use');
    expect(classifySchemaDeleteFailure(new ApiError(400, { error: 'x' })).kind).toBe('catalog_derived');
    expect(classifySchemaDeleteFailure(new ApiError(503, { error: 'x' })).kind).toBe('check_incomplete');
  });

  /** A network failure is not an `ApiError` and must still classify. */
  it('handles a non-ApiError without throwing', () => {
    const failure = classifySchemaDeleteFailure(new Error('Failed to fetch'));
    expect(failure.kind).toBe('unknown');
    expect(failure.message).toBe('Failed to fetch');
    expect(failure.referencingPipelines).toEqual([]);
  });
});
