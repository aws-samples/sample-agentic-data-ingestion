// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for shared display formatters (P29 R4.2).
 *
 * Validates: Requirements 4.2
 */
import { describe, it, expect } from 'vitest';
import { humanizeDuration, formatUsd } from '../formatters';

describe('humanizeDuration', () => {
  describe('seconds range', () => {
    it('formats whole seconds', () => {
      expect(humanizeDuration(1_000)).toBe('1s');
      expect(humanizeDuration(45_000)).toBe('45s');
      expect(humanizeDuration(59_000)).toBe('59s');
    });

    it('floors sub-second remainders to whole seconds', () => {
      expect(humanizeDuration(1_500)).toBe('1s');
      expect(humanizeDuration(999)).toBe('0s'); // < 1s, floored
    });
  });

  describe('minutes range', () => {
    it('formats a single minute unit (seconds dropped)', () => {
      expect(humanizeDuration(60_000)).toBe('1m');
      expect(humanizeDuration(2_700_000)).toBe('45m'); // 45 minutes
      expect(humanizeDuration(90_000)).toBe('1m'); // 1m30s → 1m
    });

    it('rolls 59m59s worth of ms into 59m (still sub-hour)', () => {
      expect(humanizeDuration(59 * 60_000 + 59_000)).toBe('59m');
    });
  });

  describe('hours range', () => {
    it('shows hours + minutes', () => {
      expect(humanizeDuration(8_100_000)).toBe('2h 15m'); // 2h15m
      expect(humanizeDuration(3_600_000 + 5 * 60_000)).toBe('1h 5m');
    });

    it('omits minutes when zero', () => {
      expect(humanizeDuration(3_600_000)).toBe('1h');
      expect(humanizeDuration(2 * 3_600_000)).toBe('2h');
    });

    it('drops trailing seconds within the hours range', () => {
      expect(humanizeDuration(2 * 3_600_000 + 15 * 60_000 + 30_000)).toBe('2h 15m');
    });
  });

  describe('days range', () => {
    it('shows days + hours', () => {
      expect(humanizeDuration(273_600_000)).toBe('3d 4h'); // 3d4h
      expect(humanizeDuration(MS(1, 5, 0, 0))).toBe('1d 5h');
    });

    it('omits hours when zero', () => {
      expect(humanizeDuration(MS(3, 0, 0, 0))).toBe('3d');
      expect(humanizeDuration(MS(1, 0, 0, 0))).toBe('1d');
    });

    it('drops trailing minutes/seconds within the days range', () => {
      expect(humanizeDuration(MS(2, 3, 45, 12))).toBe('2d 3h');
    });
  });

  describe('boundaries', () => {
    it('handles exact unit thresholds', () => {
      expect(humanizeDuration(1_000)).toBe('1s');
      expect(humanizeDuration(60_000)).toBe('1m');
      expect(humanizeDuration(3_600_000)).toBe('1h');
      expect(humanizeDuration(86_400_000)).toBe('1d');
    });
  });

  describe('zero and edge cases', () => {
    it('returns "0s" for zero', () => {
      expect(humanizeDuration(0)).toBe('0s');
    });

    it('clamps negative durations to "0s"', () => {
      expect(humanizeDuration(-1)).toBe('0s');
      expect(humanizeDuration(-500_000)).toBe('0s');
    });

    it('returns "—" for non-finite input', () => {
      expect(humanizeDuration(Number.NaN)).toBe('—');
      expect(humanizeDuration(Number.POSITIVE_INFINITY)).toBe('—');
      expect(humanizeDuration(Number.NEGATIVE_INFINITY)).toBe('—');
    });

    it('never emits raw milliseconds', () => {
      // A raw-ms leak would look like "2700000ms" — assert the humanized form.
      expect(humanizeDuration(2_700_000)).not.toContain('ms');
      expect(humanizeDuration(2_700_000)).toBe('45m');
    });
  });
});

/** Test helper: build a millisecond duration from days/hours/minutes/seconds. */
function MS(days: number, hours: number, minutes: number, seconds: number): number {
  return (
    days * 86_400_000 + hours * 3_600_000 + minutes * 60_000 + seconds * 1_000
  );
}

/**
 * formatUsd — shared currency formatter (P30 R3.2).
 *
 * Validates: Requirements 3.2
 */
describe('formatUsd', () => {
  it('renders money-scale amounts with two decimals', () => {
    expect(formatUsd(0)).toBe('$0.00');
    expect(formatUsd(7)).toBe('$7.00');
    expect(formatUsd(2.333)).toBe('$2.33');
    expect(formatUsd(99.99)).toBe('$99.99');
  });

  it('groups thousands', () => {
    expect(formatUsd(1234.5)).toBe('$1,234.50');
    expect(formatUsd(1_000_000)).toBe('$1,000,000.00');
  });

  it('widens precision for sub-cent amounts instead of showing $0.00', () => {
    // Per-run Glue costs are stored rounded to 3 decimals, so these occur.
    expect(formatUsd(0.003)).toBe('$0.003');
    expect(formatUsd(0.001)).toBe('$0.001');
    expect(formatUsd(0.0004)).toBe('$0.0004');
    expect(formatUsd(0.009)).not.toBe('$0.00');
  });

  it('reports a bound rather than rounding away amounts below $0.0001', () => {
    expect(formatUsd(0.00001)).toBe('< $0.0001');
    expect(formatUsd(-0.00001)).toBe('> -$0.0001');
  });

  it('renders negative amounts with a sign', () => {
    expect(formatUsd(-4.5)).toBe('-$4.50');
  });

  it('returns "—" for unknown values rather than a fabricated zero', () => {
    expect(formatUsd(null)).toBe('—');
    expect(formatUsd(undefined)).toBe('—');
    expect(formatUsd(Number.NaN)).toBe('—');
    expect(formatUsd(Number.POSITIVE_INFINITY)).toBe('—');
  });
});
