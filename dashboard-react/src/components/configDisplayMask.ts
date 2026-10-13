// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Display-side masking helpers for sensitive JDBC config fields.
 *
 * Mirrors the byte-for-byte behaviour of the wizard's review-step helpers
 * in `pages/wizard/WizardStepReview.tsx` so the in-wizard preview and the
 * post-create dashboard render identical strings. Kept here as a separate
 * module so non-wizard components (`TargetConfigDisplay`,
 * `SourceConfigDisplay`) can import them without pulling in the entire
 * wizard module.
 *
 * Validates: P17 Requirement 18.3 — never echo a Secrets Manager ARN in
 * cleartext, and never echo `username:password@` from a JDBC URL.
 */

/**
 * Mask a Secrets Manager ARN to its last 4 characters.
 *
 * Example:
 *   `arn:aws:secretsmanager:us-west-2:123:secret:mydb-AbCdEf` →
 *   `****AbCdEf` (last 4 chars preserved for ops disambiguation).
 *
 * Returns `'—'` for null / undefined / empty input.
 * Returns `'****'` for inputs ≤ 4 characters (refuse to leak short values).
 */
export function maskSecret(value: string | undefined | null): string {
  if (!value) return '—';
  if (value.length <= 4) return '****';
  return `****${value.slice(-4)}`;
}

/**
 * Strip `username:password@` from a JDBC URL while preserving everything
 * else verbatim so operators can still see host / port / database in
 * the dashboard. Idempotent — URLs without inline credentials are
 * returned unchanged.
 */
export function maskJdbcUrl(value: string | undefined | null): string {
  if (!value) return '—';
  return value.replace(/(jdbc:[^:]+:\/\/)([^/@\s]+@)/, '$1');
}
