// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * EventBridge schedule expression parsing — converts AWS EventBridge
 * `cron(...)` and `rate(...)` expressions into the next scheduled run time.
 *
 * P17 Requirement 18.5: the Pipeline Overview page displays the next
 * scheduled run for pipelines that have a schedule configured.
 *
 * EventBridge cron syntax differs from standard 5-field cron:
 *   - 6 fields: MIN HOUR DOM MON DOW YEAR
 *   - `?` placeholder for "any" in DOM/DOW (mutually exclusive with the
 *     other one having a specific value)
 *
 * cron-parser only supports standard 5-field cron and uses `*` instead of
 * `?`. We translate before delegating: drop the YEAR field if present and
 * replace `?` with `*`.
 *
 * EventBridge rate syntax: `rate(N unit)` where unit ∈ {minute(s), hour(s),
 * day(s)}. We compute the next run as `now + rate` (a reasonable display
 * approximation; the actual EventBridge implementation anchors to rule
 * creation, which we don't have visibility into from the dashboard).
 */
import { CronExpressionParser } from 'cron-parser';

const CRON_RE = /^cron\(\s*(.+?)\s*\)$/;
const RATE_RE = /^rate\(\s*(\d+)\s+(minute|minutes|hour|hours|day|days)\s*\)$/;

/**
 * Translate an EventBridge `cron(...)` body into a 5-field standard cron
 * expression that cron-parser can parse.
 *
 * - 6-field input → drop the YEAR field
 * - `?` → `*` (cron-parser doesn't recognise the `?` placeholder)
 */
export function eventBridgeCronToStandard(body: string): string {
  const parts = body.trim().split(/\s+/);
  const fiveField = parts.length === 6 ? parts.slice(0, 5) : parts;
  return fiveField.map((p) => (p === '?' ? '*' : p)).join(' ');
}

/**
 * Compute the next scheduled run for an EventBridge schedule expression
 * relative to ``now``.
 *
 * @param expression - The EventBridge expression, e.g. ``cron(0 2 * * ? *)``
 *   or ``rate(1 hour)``.
 * @param now - Reference time (defaults to ``new Date()``). Exposed for
 *   deterministic tests.
 * @returns The next run as a ``Date``, or ``null`` if the expression
 *   cannot be parsed (returning ``null`` lets the caller render a
 *   graceful fallback rather than throwing).
 */
export function nextScheduledRun(expression: string | null | undefined, now: Date = new Date()): Date | null {
  if (!expression) return null;
  const trimmed = expression.trim();

  const cronMatch = trimmed.match(CRON_RE);
  if (cronMatch) {
    try {
      const standard = eventBridgeCronToStandard(cronMatch[1]);
      // EventBridge schedules are evaluated in UTC; reflect that here so the
      // computed next-run instant matches what AWS will actually fire.
      const interval = CronExpressionParser.parse(standard, { currentDate: now, tz: 'UTC' });
      return interval.next().toDate();
    } catch {
      return null;
    }
  }

  const rateMatch = trimmed.match(RATE_RE);
  if (rateMatch) {
    const value = parseInt(rateMatch[1], 10);
    if (!Number.isFinite(value) || value <= 0) return null;
    const unit = rateMatch[2].toLowerCase();
    let ms: number;
    if (unit.startsWith('minute')) ms = value * 60 * 1000;
    else if (unit.startsWith('hour')) ms = value * 60 * 60 * 1000;
    else if (unit.startsWith('day')) ms = value * 24 * 60 * 60 * 1000;
    else return null;
    return new Date(now.getTime() + ms);
  }

  return null;
}

/**
 * Format a ``Date`` for display in the Pipeline Overview "Next run" cell.
 * Uses the browser's locale and timezone so the user sees a familiar
 * representation of the UTC-anchored cron schedule.
 */
export function formatNextRun(date: Date): string {
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  });
}
