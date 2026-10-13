// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { ApiError } from '../services/api-client';
import type { RawSchemaDeleteError, ReferencingPipeline } from '../types';

/**
 * The four ways a delete can end badly, kept as four — p35 R3.6, R3.9 and design
 * §5 risk 6.
 *
 * `useDeleteSchema` had no `onError` at all, so the page toasted one fixed
 * message for every failure. R3.6 forbids that outright:
 *
 * > WHEN a delete fails, THE Schemas_Page SHALL report the reason the Schemas_API
 * > gave rather than a fixed message.
 *
 * **`check_incomplete` is the branch to watch.** It is the only one with no user
 * story behind it, which is why design §5 risk 6 predicts it as the first to be
 * collapsed into a generic error — and collapsing it puts the page back to
 * "always toasts Failed to delete schema" for the one case where the correct
 * answer is *retry*. It is `retryable: true` at the producer because every cause
 * classified as incomplete is transient.
 */
export type SchemaDeleteFailureKind =
  | 'in_use'
  | 'catalog_derived'
  | 'check_incomplete'
  | 'unknown';

export interface SchemaDeleteFailure {
  kind: SchemaDeleteFailureKind;
  /** The reason **the API gave**, not a message composed here (R3.6). */
  message: string;
  /** Non-empty only for `in_use`. R3.9 requires these to be presented. */
  referencingPipelines: ReferencingPipeline[];
  /** `glue_catalog` | `s3_table_bucket`, only for `catalog_derived`. */
  source?: string;
  /** Which reference lookups completed. Absent on `catalog_derived`. */
  checked: string[];
  details?: string;
  /** True only where trying again can change the answer. */
  retryable: boolean;
}

function asDeleteErrorBody(error: unknown): RawSchemaDeleteError {
  if (!(error instanceof ApiError)) return {};
  return typeof error.body === 'object' && error.body !== null
    ? (error.body as RawSchemaDeleteError)
    : {};
}

/**
 * Classify a failed `DELETE /api/schemas/{id}`.
 *
 * **`reason` is read before `status`.** The reason string is the producer's own
 * name for the outcome and is unique per branch, while the status is not: `400`
 * is `catalog_derived_schema` here but is also what a malformed request would
 * answer. Falling back to the status keeps the three branches reachable if a
 * response ever arrives without a reason.
 *
 * Anything unrecognised becomes `unknown` and carries the API's own message
 * through — a `500` is not silently reported as one of the three refusals,
 * because the three refusals all guarantee the object still exists (R3.10 orders
 * the check before the delete) and a `500` guarantees nothing.
 */
export function classifySchemaDeleteFailure(error: unknown): SchemaDeleteFailure {
  const body = asDeleteErrorBody(error);
  const status = error instanceof ApiError ? error.status : undefined;
  const message =
    body.error ??
    (error instanceof Error && error.message ? error.message : 'The schema could not be deleted.');
  const checked = Array.isArray(body.checked) ? body.checked : [];

  if (body.reason === 'schema_in_use' || (body.reason === undefined && status === 409)) {
    return {
      kind: 'in_use',
      message,
      referencingPipelines: Array.isArray(body.referencingPipelines) ? body.referencingPipelines : [],
      checked,
      // Trying again cannot help: the pipelines have to change first.
      retryable: false,
    };
  }

  if (body.reason === 'catalog_derived_schema' || (body.reason === undefined && status === 400)) {
    return {
      kind: 'catalog_derived',
      message,
      referencingPipelines: [],
      source: body.source,
      checked,
      retryable: false,
    };
  }

  if (body.reason === 'reference_check_incomplete' || (body.reason === undefined && status === 503)) {
    return {
      kind: 'check_incomplete',
      message,
      referencingPipelines: [],
      checked,
      details: body.details,
      // The producer says so, and it is honest: nothing was deleted, and the
      // causes classified as incomplete are transient.
      retryable: body.retryable ?? true,
    };
  }

  return {
    kind: 'unknown',
    message,
    referencingPipelines: [],
    checked,
    details: body.details,
    retryable: false,
  };
}
