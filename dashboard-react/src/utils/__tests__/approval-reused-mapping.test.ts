// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 4.4 — the shared reuse reader both approval surfaces call.
 *
 * Expectations derive from R1.4, R2.1, R2.2 and from the producer's three states
 * as `_reused_mapping_payload` (`approval_handler/lambda_function.py`) actually
 * emits them — never from what either display site happened to render before,
 * because before this task neither site read these keys at all.
 *
 * The interesting case is the last describe block: task 2.3's Property 1 found a
 * **resolved** payload that can carry a non-list `proposed_mappings` with all
 * thirteen keys, `mapping_count: null`, and no `mapping_unreadable`. Keyed on the
 * marker alone that renders as a mapping table with no rows beside a payload
 * asserting it is readable — do-NOT 7's collapse, `LGR-b2f79ef-14`'s named
 * reference to something absent. The reader classifies it `unreadable`; these
 * tests pin that decision so it cannot be "simplified" back to a marker test.
 *
 * Requirements: R1.4, R2.1, R2.2
 */
import { describe, it, expect } from 'vitest';
import {
  reusedMappingDisplay,
  reusedMappingIdText,
  REUSED_MAPPING_ID_NOT_ESTABLISHED,
} from '../approval-reused-mapping';

/** The producer's **resolved** state, reduced to the keys this reader looks at. */
const RESOLVED = {
  proposed_mappings: [{ source_field: 'signup_date', target_field: 'registration_date' }],
  mapping_count: 1,
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  reused_from_run_id: 'run-2026-08-24-13-48-26-4fccdf03',
};

/** The producer's **unreadable** state — exactly the five keys it emits. */
const UNREADABLE = {
  reused_mapping: true,
  reused_mapping_id: 'mapping-abc123',
  mapping_unreadable: true,
  proposed_mappings: null,
  mapping_count: null,
};

describe('reusedMappingDisplay — the no-reuse state says nothing (do-NOT 7)', () => {
  it('reports nothing stated for the empty payload the producer emits for no reuse', () => {
    // `{}` is a genuinely different fact — no Mapping_State_Row and no
    // `mapping_id` — and not p54's to change. `false` would assert the run
    // generated its own mapping.
    expect(reusedMappingDisplay({})).toEqual({
      stated: false,
      reused: false,
      mappingId: null,
      unreadable: false,
    });
  });

  it('is total over an absent details object', () => {
    // Both callers coalesce to `{}` first, but a reader that only works on
    // coalesced input is a reader the third caller gets wrong.
    expect(reusedMappingDisplay(undefined).stated).toBe(false);
  });

  it('says nothing about a non-reuse payload that carries its own mappings', () => {
    // A run that generated its own mapping has a populated `proposed_mappings`
    // and no `reused_mapping`. The reuse block must not appear on it.
    const own = { proposed_mappings: [{ source_field: 'a', target_field: 'b' }], mapping_count: 1 };
    expect(reusedMappingDisplay(own).stated).toBe(false);
  });

  it('says nothing about a non-reuse payload whose own mapping list is unreadable', () => {
    // `proposed_mappings` on a non-reuse payload is the run's own proposal and is
    // no business of this reader — the third-route test below applies only where
    // reuse is stated.
    expect(reusedMappingDisplay({ mapping_count: null }).stated).toBe(false);
  });
});

describe('reusedMappingDisplay — the resolved state (R2.1, R2.2)', () => {
  it('states the reuse and names the mapping, with nothing unreadable', () => {
    expect(reusedMappingDisplay(RESOLVED)).toEqual({
      stated: true,
      reused: true,
      mappingId: 'mapping-abc123',
      unreadable: false,
    });
  });

  it('accepts an empty mapping list as readable — zero mappings is a read fact', () => {
    // R1.5's other direction, and the reason the third-route test is
    // `Array.isArray` and not `length > 0`: a readable payload carrying no
    // mappings is a different fact from one that could not be read.
    const empty = { ...RESOLVED, proposed_mappings: [], mapping_count: 0 };
    expect(reusedMappingDisplay(empty).unreadable).toBe(false);
  });
});

describe('reusedMappingDisplay — the unreadable state (R1.4)', () => {
  it('reports the producer marker, and still names which mapping', () => {
    // R2.2 holds on this state too: the approver is told which mapping the gate
    // is about even though its fields could not be read.
    expect(reusedMappingDisplay(UNREADABLE)).toEqual({
      stated: true,
      reused: true,
      mappingId: 'mapping-abc123',
      unreadable: true,
    });
  });

  it('does not treat the null count as an established zero', () => {
    // The consumer half of the producer's rule. `mapping_count` itself is read by
    // `approval-mapping-count.ts`; what this reader must not do is make the
    // unreadable state look resolved so the em dash sits beside a mapping panel.
    expect(reusedMappingDisplay(UNREADABLE).unreadable).toBe(true);
  });
});

describe('reusedMappingDisplay — a stated id that names nothing', () => {
  it('reports a blank id as not established rather than as an empty name', () => {
    expect(reusedMappingDisplay({ reused_mapping: true, reused_mapping_id: '   ' }).mappingId).toBe(
      null,
    );
  });

  it('reports a non-string id as not established', () => {
    // `details` is `Record<string, unknown>` (`LGR-69ca78b-4`), so a number here
    // is representable and must not be stringified into a mapping name.
    expect(reusedMappingDisplay({ reused_mapping: true, reused_mapping_id: 42 }).mappingId).toBe(
      null,
    );
  });

  it('still states the reuse when the id is missing', () => {
    // R2.1 and R2.2 are separate facts; losing the id must not suppress the
    // statement that a mapping was reused.
    const display = reusedMappingDisplay({ reused_mapping: true, proposed_mappings: [] });
    expect(display.reused).toBe(true);
    expect(display.mappingId).toBe(null);
  });
});

describe('reusedMappingIdText — the rendering both sites share', () => {
  it('renders the id', () => {
    expect(reusedMappingIdText(RESOLVED)).toBe('mapping-abc123');
  });

  it('renders not-established for a payload that named no mapping', () => {
    expect(reusedMappingIdText({ reused_mapping: true, proposed_mappings: [] })).toBe(
      REUSED_MAPPING_ID_NOT_ESTABLISHED,
    );
  });

  it('never renders the empty string, which reads as a failed render', () => {
    expect(reusedMappingIdText({ reused_mapping: true, reused_mapping_id: '' })).not.toBe('');
  });
});

describe('reusedMappingDisplay — task 2.3s THIRD route (do-NOT 7)', () => {
  // A resolved payload with a non-list `proposed_mappings`: thirteen keys,
  // `mapping_count: null`, and NO `mapping_unreadable`. The producer's
  // post-projection guard (`lambda_function.py:481`) keeps `None` specifically
  // out of this state, so these are the shapes that actually reach it.
  //
  // `'not-a-list'` is deliberately included even though a string is the one
  // non-list shape that also reaches `ApprovalsPage`'s untouched duplicate table
  // with a truthy `.length` — the render tests avoid it for that reason, and the
  // reader must classify it anyway.
  it.each([
    ['a string', 'not-a-list'],
    ['an object', {}],
    ['a number', 4],
    ['a boolean', false],
  ])('classifies a %s proposed_mappings as unreadable', (_label, value) => {
    const display = reusedMappingDisplay({
      ...RESOLVED,
      proposed_mappings: value,
      mapping_count: null,
    });
    expect(display.unreadable).toBe(true);
    // Still a reused mapping, and still named — the fact that failed is the
    // mapping's fields, not the reuse.
    expect(display.reused).toBe(true);
    expect(display.mappingId).toBe('mapping-abc123');
  });

  it('classifies an absent proposed_mappings on a reuse payload as unreadable', () => {
    // Not a shape today's producer emits — both its reuse states carry the key —
    // but the arm has to be total over the payload, because the alternative is a
    // mapping panel with no rows beside a payload asserting it is readable.
    expect(reusedMappingDisplay({ reused_mapping: true, reused_mapping_id: 'm-1' }).unreadable).toBe(
      true,
    );
  });
});

describe('reusedMappingDisplay — a truthy string is not a boolean', () => {
  it('does not read the string "false" as a stated reuse', () => {
    // The reason both flags are `=== true` rather than truthy. Invariant
    // `LGR-69ca78b-1` keeps `IsBoolean` in the governance Choice's conjuncts for
    // exactly this: `"false"` is a truthy string and would invert the fact.
    expect(reusedMappingDisplay({ reused_mapping: 'false' }).stated).toBe(false);
  });

  it('does not read the string "false" as a stated unreadable marker', () => {
    expect(
      reusedMappingDisplay({ reused_mapping: true, proposed_mappings: [], mapping_unreadable: 'false' })
        .unreadable,
    ).toBe(false);
  });
});
