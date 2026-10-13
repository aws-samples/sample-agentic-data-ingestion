// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p50 task 4.2 — the three-state mapping count, at the one place it is derived.
 *
 * CP2's two halves are the whole point of this file: an unreadable payload never
 * yields `0`, and an established zero never yields not-established. The
 * expression under test exists because the two display sites had already
 * diverged on exactly that distinction — `ApprovalWorkflowSection` read
 * `?? '\u2014'` and `ApprovalsPage` read `?? 0`.
 *
 * Expectations come from `requirements.md` R1.4/R1.5 and from the producer's own
 * derivation at `approval_handler/lambda_function.py:343`, not from either
 * reader's previous output — one of those readers was the defect.
 *
 * **Validates: Requirements 1.4, 1.5**
 *
 * Requirements: R1.4, R1.5 — Design: C2, CP2
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { approvalMappingCount, mappingCountText } from '../approval-mapping-count';

/** The em dash both surfaces render for a count that was never established. */
const NOT_ESTABLISHED = '\u2014';

describe('approvalMappingCount — a stated count wins, including zero', () => {
  it('returns a positive stated count', () => {
    expect(approvalMappingCount({ mapping_count: 7 })).toBe(7);
  });

  /** The value a `??` chain swallows, and the one R1.5 turns on. */
  it('returns 0 when the payload was read and carries no mappings', () => {
    expect(approvalMappingCount({ mapping_count: 0 })).toBe(0);
  });
});

describe('approvalMappingCount — not established stays not established', () => {
  it('returns null for the null the producer sends for an unreadable payload', () => {
    // `len(proposed_mappings) if isinstance(proposed_mappings, list) else None`
    // — `None` reaches the wire as `null` (do-NOT 7).
    expect(approvalMappingCount({ mapping_count: null })).toBeNull();
  });

  it('returns null when the key is absent entirely', () => {
    // The state the spec was reported from: `mapping_count` was not a key at all,
    // because the branch read nine keys off the wrong level (`LGR-86698d4-1`).
    expect(approvalMappingCount({ generated_by: 'us.anthropic.claude-sonnet-4-6' })).toBeNull();
  });

  it('returns null for absent details', () => {
    expect(approvalMappingCount(undefined)).toBeNull();
  });

  it('returns null rather than stringifying a non-numeric value as a count', () => {
    // `details` is `Record<string, unknown>` and stays loose (`LGR-99050e6-5`),
    // so a malformed value is reachable through the type. It established no
    // count, so it is not established — it is not a count to render.
    expect(approvalMappingCount({ mapping_count: '7' as unknown as number })).toBeNull();
    expect(approvalMappingCount({ mapping_count: [] as unknown as number })).toBeNull();
    expect(approvalMappingCount({ mapping_count: NaN })).toBeNull();
  });
});

describe('mappingCountText — the rendering both sites share', () => {
  it('renders an established count as the number, including zero', () => {
    expect(mappingCountText({ mapping_count: 7 })).toBe('7');
    expect(mappingCountText({ mapping_count: 0 })).toBe('0');
  });

  it('renders an em dash that asserts nothing when no count was established', () => {
    expect(mappingCountText({ mapping_count: null })).toBe(NOT_ESTABLISHED);
    expect(mappingCountText({})).toBe(NOT_ESTABLISHED);
    expect(mappingCountText(undefined)).toBe(NOT_ESTABLISHED);
  });

  it('never renders a bare "0" for a payload that established nothing (do-NOT 7)', () => {
    for (const details of [undefined, {}, { mapping_count: null }]) {
      expect(mappingCountText(details)).not.toBe('0');
    }
  });
});

// ---------------------------------------------------------------------------
// CP2 as a property
// ---------------------------------------------------------------------------

describe('CP2 — absent and empty stay distinguishable for every input', () => {
  it('never yields 0 for an unreadable payload, and never not-established for a real count', () => {
    // The generator spans what the producer can send plus the malformed arm the
    // loose `details` type admits: a finite number, `null`, an absent key, and a
    // non-number. `0` is generated deliberately and separately from `null`
    // because keeping those two apart IS the property.
    const countArb = fc.oneof(
      fc.integer({ min: 0, max: 5000 }),
      fc.constant(null),
      fc.constant(undefined),
      fc.constant('3'),
      fc.constant(NaN),
    );

    fc.assert(
      fc.property(countArb, (raw) => {
        const details =
          raw === undefined ? {} : ({ mapping_count: raw } as Record<string, unknown>);
        const count = approvalMappingCount(details);
        const text = mappingCountText(details);

        if (typeof raw === 'number' && Number.isFinite(raw)) {
          // Established. The number survives exactly, and 0 is never an em dash.
          expect(count).toBe(raw);
          expect(text).toBe(String(raw));
          expect(text).not.toBe(NOT_ESTABLISHED);
        } else {
          // Not established. Never `0`, in either representation.
          expect(count).toBeNull();
          expect(text).toBe(NOT_ESTABLISHED);
          expect(count).not.toBe(0);
          expect(text).not.toBe('0');
        }
      }),
      { numRuns: 200 },
    );
  });
});
