// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the EventBridge schedule expression parsing utilities used
 * by the Pipeline Overview page (P17 Task 21.2 / Requirement 18.5).
 *
 * Coverage matrix (per Task 21.2 prompt):
 *   - rate(1 hour)
 *   - rate(15 minutes)
 *   - cron(0 2 * * ? *)              — daily at 02:00 UTC
 *   - cron(0 2 ? * MON *)            — weekly Monday at 02:00 UTC
 *   - invalid expression             — returns null
 *   - null / undefined / empty input — returns null (no-schedule case)
 *
 * Plus `formatNextRun` smoke-coverage and an `eventBridgeCronToStandard`
 * sanity check for the `?` ↔ `*` translation that distinguishes
 * EventBridge from POSIX cron.
 */
import { describe, it, expect } from 'vitest';
import {
  nextScheduledRun,
  formatNextRun,
  eventBridgeCronToStandard,
} from '../cron-utils';

// Anchor every test to a deterministic reference instant so the rate(...)
// branch and cron(...) branch produce stable timestamps.
const NOW = new Date('2024-06-15T10:00:00Z');

describe('cron-utils — nextScheduledRun', () => {
  describe('rate(...) expressions', () => {
    it('rate(1 hour) → exactly 1 hour after now', () => {
      const next = nextScheduledRun('rate(1 hour)', NOW);
      expect(next).not.toBeNull();
      expect(next!.toISOString()).toBe('2024-06-15T11:00:00.000Z');
    });

    it('rate(15 minutes) → exactly 15 minutes after now', () => {
      const next = nextScheduledRun('rate(15 minutes)', NOW);
      expect(next).not.toBeNull();
      expect(next!.toISOString()).toBe('2024-06-15T10:15:00.000Z');
    });

    it('accepts the singular forms (rate(1 minute), rate(1 day))', () => {
      const oneMin = nextScheduledRun('rate(1 minute)', NOW);
      expect(oneMin!.toISOString()).toBe('2024-06-15T10:01:00.000Z');

      const oneDay = nextScheduledRun('rate(1 day)', NOW);
      expect(oneDay!.toISOString()).toBe('2024-06-16T10:00:00.000Z');
    });

    it('rejects rate(0 minutes) (zero/negative is invalid per EventBridge)', () => {
      expect(nextScheduledRun('rate(0 minutes)', NOW)).toBeNull();
    });

    it('rejects rate with an unsupported unit', () => {
      expect(nextScheduledRun('rate(1 week)', NOW)).toBeNull();
      expect(nextScheduledRun('rate(1 second)', NOW)).toBeNull();
    });
  });

  describe('cron(...) expressions', () => {
    it('cron(0 2 * * ? *) → next 02:00 UTC (daily)', () => {
      // From 2024-06-15T10:00:00Z the next daily 02:00 UTC firing is
      // 2024-06-16T02:00:00Z.
      const next = nextScheduledRun('cron(0 2 * * ? *)', NOW);
      expect(next).not.toBeNull();
      expect(next!.toISOString()).toBe('2024-06-16T02:00:00.000Z');
    });

    it('cron(0 2 ? * MON *) → next Monday at 02:00 UTC', () => {
      // 2024-06-15 is a Saturday → next Monday at 02:00 UTC is
      // 2024-06-17T02:00:00Z.
      const next = nextScheduledRun('cron(0 2 ? * MON *)', NOW);
      expect(next).not.toBeNull();
      expect(next!.toISOString()).toBe('2024-06-17T02:00:00.000Z');
    });

    it('handles a 5-field cron body (no YEAR field)', () => {
      // EventBridge requires the YEAR field, but cron-parser tolerates
      // 5-field input directly. The translator should pass it through.
      const next = nextScheduledRun('cron(0 2 * * *)', NOW);
      expect(next).not.toBeNull();
      expect(next!.toISOString()).toBe('2024-06-16T02:00:00.000Z');
    });
  });

  describe('invalid input', () => {
    it('returns null for a plainly malformed expression', () => {
      expect(nextScheduledRun('every hour', NOW)).toBeNull();
      expect(nextScheduledRun('cron(this is not cron)', NOW)).toBeNull();
      expect(nextScheduledRun('rate(abc minutes)', NOW)).toBeNull();
      expect(nextScheduledRun('not-a-schedule', NOW)).toBeNull();
    });

    it('returns null for null / undefined / empty (no-schedule case)', () => {
      expect(nextScheduledRun(null, NOW)).toBeNull();
      expect(nextScheduledRun(undefined, NOW)).toBeNull();
      expect(nextScheduledRun('', NOW)).toBeNull();
      expect(nextScheduledRun('   ', NOW)).toBeNull();
    });
  });
});

describe('cron-utils — eventBridgeCronToStandard', () => {
  it('drops the YEAR field from a 6-field expression', () => {
    expect(eventBridgeCronToStandard('0 2 * * ? *')).toBe('0 2 * * *');
  });

  it('replaces "?" with "*" so cron-parser can parse it', () => {
    expect(eventBridgeCronToStandard('0 2 ? * MON *')).toBe('0 2 * * MON');
  });

  it('passes through a 5-field expression unchanged (modulo "?")', () => {
    expect(eventBridgeCronToStandard('*/5 * * * *')).toBe('*/5 * * * *');
  });
});

describe('cron-utils — formatNextRun', () => {
  it('returns a non-empty human-readable string', () => {
    const date = new Date('2024-06-15T11:00:00Z');
    const formatted = formatNextRun(date);
    // We don't pin the exact string because Intl output varies across
    // node/icu builds; we just assert it includes the year and a time.
    expect(formatted).toMatch(/2024/);
    expect(formatted).toMatch(/\d{1,2}:\d{2}/);
  });
});
