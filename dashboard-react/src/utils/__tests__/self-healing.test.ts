// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { describeSelfHealing } from '../self-healing';

/**
 * p30 task 2.5 — the run-list heal indicator (R14.3).
 *
 * The substance of these tests is that the states do NOT collapse. Only
 * `success` may read as healed; the other outcomes are separate facts and a
 * single "Healed" badge would misreport them.
 *
 * Every status string below is transcribed from the agent's own
 * `rerun_status` values (agentcore/self_healing_agent/main.py) rather than
 * imported, so a divergence between producer and consumer fails here.
 *
 * Validates: Requirements 14.3
 */

const ALL_STATUSES = [
  'running',
  'success',
  'failed',
  'start_failed',
  'skipped_partial_write_guard',
  'skipped',
  'skipped_heal_guard',
] as const;

describe('describeSelfHealing — no heal, no indicator', () => {
  it.each([undefined, null, '', '   '])('renders nothing for %p', (input) => {
    expect(describeSelfHealing(input)).toBeNull();
  });
});

describe('describeSelfHealing — only success reads as healed', () => {
  it('labels a succeeded re-run as healed, in green', () => {
    const result = describeSelfHealing('success');
    expect(result?.label).toBe('Self-healed');
    expect(result?.color).toBe('green');
  });

  it.each(ALL_STATUSES.filter((s) => s !== 'success'))(
    'does not label %s as healed',
    (status) => {
      const result = describeSelfHealing(status);
      expect(result).not.toBeNull();
      expect(result!.label).not.toBe('Self-healed');
    },
  );

  it('gives every status a distinct label', () => {
    const labels = ALL_STATUSES.map((s) => describeSelfHealing(s)!.label);
    expect(new Set(labels).size).toBe(ALL_STATUSES.length);
  });
});

describe('describeSelfHealing — the four states R14.3 must tell apart', () => {
  it('an in-flight re-run says the outcome is not known yet', () => {
    const result = describeSelfHealing('running');
    expect(result?.label).toBe('Self-heal re-running');
    expect(result?.color).toBe('blue');
    expect(result?.detail).toContain('not known yet');
  });

  it('a failed re-run says the re-run failed too', () => {
    const result = describeSelfHealing('failed');
    expect(result?.color).toBe('red');
    expect(result?.detail).toContain('failed');
  });

  it('a re-run that never started says no re-run exists', () => {
    const result = describeSelfHealing('start_failed');
    expect(result?.color).toBe('red');
    expect(result?.detail).toContain('No re-run exists');
    // An applied worker-type upgrade is not rolled back by a failed start.
    expect(result?.detail).toContain('not rolled back');
  });

  it('a guard decline says why, without claiming a partial write was detected', () => {
    const result = describeSelfHealing('skipped_partial_write_guard');
    expect(result?.label).toBe('Self-heal declined');
    expect(result?.detail).toContain('may have written rows');
    expect(result?.detail.toLowerCase()).not.toContain('detected');
  });

  it('a diagnosis with no applicable fix says nothing was re-run', () => {
    const result = describeSelfHealing('skipped');
    expect(result?.color).toBe('grey');
    expect(result?.detail).toContain('Nothing was re-run');
  });
});

describe('describeSelfHealing — an unknown status is reported, not guessed', () => {
  it('surfaces the raw status rather than defaulting to a known state', () => {
    const result = describeSelfHealing('rerun_status_from_the_future');
    expect(result?.label).toContain('rerun_status_from_the_future');
    expect(result?.color).toBe('grey');
    expect(result?.detail).toContain('does not recognise');
  });

  it('never renders an unrecognised status as healed', () => {
    expect(describeSelfHealing('healed')?.label).not.toBe('Self-healed');
    expect(describeSelfHealing('SUCCESS')?.label).not.toBe('Self-healed');
  });
});

describe('describeSelfHealing — the remediation is carried into the hover text', () => {
  it('appends the remediation when the row has one', () => {
    const result = describeSelfHealing('running', 'Upgraded worker type from G.1X to G.2X');
    expect(result?.detail).toContain('Remediation: Upgraded worker type from G.1X to G.2X');
  });

  it('leaves the detail alone when there is no remediation', () => {
    const withNone = describeSelfHealing('running');
    expect(describeSelfHealing('running', '')?.detail).toBe(withNone?.detail);
    expect(describeSelfHealing('running', '   ')?.detail).toBe(withNone?.detail);
    expect(describeSelfHealing('running', null)?.detail).toBe(withNone?.detail);
  });

  it('does not change the label or colour', () => {
    const plain = describeSelfHealing('success')!;
    const withRemediation = describeSelfHealing('success', 'anything')!;
    expect(withRemediation.label).toBe(plain.label);
    expect(withRemediation.color).toBe(plain.color);
  });
});
