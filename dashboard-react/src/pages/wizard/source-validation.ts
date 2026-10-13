// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Validation helpers for the JDBC source form (P17 Tasks 19.5 + 19.6),
 * extracted from `WizardStepSource.tsx` (P29 R11.5 decomposition). These are
 * re-exported from `WizardStepSource.tsx` so existing import paths stay stable.
 */

// ─── Validation regexes (Tasks 19.5 + 19.6) ─────────────────────

/** P17 Req 3.5/3.6 — SQL identifier (schema, table, watermark column). */
export const IDENTIFIER_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * P17 Req 5.2 — EventBridge cron(...) accepts six space-delimited fields:
 * minutes, hours, day-of-month, month, day-of-week, year. The regex is
 * intentionally lenient on the field contents (any non-whitespace token)
 * since the server-side `events:PutRule` call performs the authoritative
 * field validation; this client-side check is a pre-flight guard against
 * the common typos (5-field crontabs and missing parentheses).
 */
export const EB_CRON_RE = /^cron\(\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*\)$/;
export const EB_RATE_RE =
  /^rate\(\s*\d+\s+(minute|minutes|hour|hours|day|days)\s*\)$/;

export function validateScheduleExpression(value: string): string | undefined {
  const trimmed = (value ?? '').trim();
  if (!trimmed) return 'Schedule is required for JDBC sources';
  if (EB_CRON_RE.test(trimmed) || EB_RATE_RE.test(trimmed)) return undefined;
  return 'Must be an EventBridge cron(...) or rate(...) expression';
}

export function validateIdentifier(value: string): string | undefined {
  if (!value) return 'Required';
  if (!IDENTIFIER_RE.test(value)) {
    return 'Letters, digits, and underscores only — must start with a letter or underscore';
  }
  return undefined;
}
