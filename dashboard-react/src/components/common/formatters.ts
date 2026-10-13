// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Shared display formatters for the dashboard (P29 R4.2, P30 R3.2).
 *
 * The review found raw milliseconds ("2700000ms") leaking into the UI.
 * `humanizeDuration` is the single formatter every elapsed-time render should
 * use so durations read as "45m", "2h 15m", or "3d 4h" — never raw ms.
 *
 * `formatUsd` is the same idea for money. Before P30 R3.2 there was no shared
 * currency formatter: money was rendered inline as `` `$${n.toFixed(d)}` `` with
 * `d` chosen per call site (2 in the cost-recommendations panel, 3 in the token
 * cost tile, 4 in the per-trace token costs, `toLocaleString()` with no decimals
 * at all in the Est. Glue Cost tile). Those call sites are left alone here;
 * new money renders should use `formatUsd` rather than adding a sixth variant.
 */

const MS_PER_SECOND = 1000;
const MS_PER_MINUTE = 60 * MS_PER_SECOND;
const MS_PER_HOUR = 60 * MS_PER_MINUTE;
const MS_PER_DAY = 24 * MS_PER_HOUR;

/**
 * Humanizes a duration given in milliseconds into a compact string using the
 * two most significant non-zero units.
 *
 * Examples:
 *   humanizeDuration(45_000)      → "45s"
 *   humanizeDuration(2_700_000)   → "45m"
 *   humanizeDuration(8_100_000)   → "2h 15m"
 *   humanizeDuration(273_600_000) → "3d 4h"
 *   humanizeDuration(0)           → "0s"
 *
 * Behavior:
 *   - Non-finite input (NaN/Infinity) → "—" (unknown, never fabricated).
 *   - Zero or negative input → "0s".
 *   - Days range shows days + hours (hours omitted when zero, e.g. "3d").
 *   - Hours range shows hours + minutes (minutes omitted when zero, e.g. "2h").
 *   - Sub-hour durations show a single unit (minutes, else seconds).
 *   - Values are floored to whole units (no fractional or millisecond output).
 */
export function humanizeDuration(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  if (ms <= 0) return '0s';

  const days = Math.floor(ms / MS_PER_DAY);
  const hours = Math.floor((ms % MS_PER_DAY) / MS_PER_HOUR);
  const minutes = Math.floor((ms % MS_PER_HOUR) / MS_PER_MINUTE);
  const seconds = Math.floor((ms % MS_PER_MINUTE) / MS_PER_SECOND);

  if (days > 0) {
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  }
  if (hours > 0) {
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  if (minutes > 0) {
    return `${minutes}m`;
  }
  return `${seconds}s`;
}

/**
 * Formats a US-dollar amount for display (P30 R3.2).
 *
 * Examples:
 *   formatUsd(0)          → "$0.00"
 *   formatUsd(7)          → "$7.00"
 *   formatUsd(1234.5)     → "$1,234.50"
 *   formatUsd(0.147)      → "$0.15"
 *   formatUsd(0.003)      → "$0.003"
 *   formatUsd(0.0004)     → "$0.0004"
 *   formatUsd(null)       → "—"
 *
 * Behavior:
 *   - `null` / `undefined` / non-finite input → "—" (unknown, never rendered
 *     as $0.00, which would read as "this was free").
 *   - Two decimal places is the default, matching how the cost-recommendations
 *     panel renders savings.
 *   - Sub-cent amounts widen to 3 or 4 decimals instead of collapsing to
 *     "$0.00". A single Glue run billed at the 10-minute minimum on 2 DPU is
 *     about $0.15, but per-run costs are stored rounded to 3 decimals, so
 *     genuinely-tiny figures do occur and must not be shown as free.
 *   - Below $0.0001 the value is reported as a bound ("< $0.0001") rather than
 *     rounded away.
 *   - Locale is pinned to en-US so the rendered string (grouping separators,
 *     symbol placement) is stable across machines and test runners; the
 *     platform's cost figures are USD by construction (Glue's published
 *     per-DPU-hour rate).
 */
export function formatUsd(amount: number | null | undefined): string {
  if (amount == null || !Number.isFinite(amount)) return '—';
  if (amount === 0) return '$0.00';

  const abs = Math.abs(amount);
  if (abs < 0.0001) return amount > 0 ? '< $0.0001' : '> -$0.0001';

  const digits = abs >= 0.01 ? 2 : abs >= 0.001 ? 3 : 4;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(amount);
}
