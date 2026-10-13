// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p50 task 5.3 — `utils/approval-unsourced-target-fields.ts`.
 *
 * The module exists because two approval surfaces read `unmapped_fields` and
 * both read it wrongly: `ApprovalsPage.tsx:42` cast the producer's dict to
 * `number` and rendered it raw (the whole review modal threw once task 2.1
 * populated the key), and `ApprovalWorkflowSection.tsx:241` called `String(...)`
 * on it and rendered `"[object Object]"`.
 *
 * Expectations come from the producer —
 * `create_approval_payload`'s `unmapped_fields` dict literal at
 * `schema_mapping/lambda_function.py:1740-1744` — and from the ratified reader's
 * documented behaviour, never from what either surface used to render. Both of
 * those outputs were the defect.
 *
 * What is asserted here is the **adapter**: which payloads establish nothing,
 * which establish an empty roster, and that the requirement words come from
 * `field-requirement.ts` rather than from a second spelling. The join itself —
 * roster, order, `default_value`, `reason` — is `unsourcedTargetFields`' own and
 * is already covered by `utils/__tests__/unsourced-target-fields.property.test.ts`;
 * re-asserting it here would create the second authority `LGR-9e76249-7`,
 * `LGR-7561acb-1` and `LGR-7561acb-4` all rule out.
 */
import { describe, it, expect } from 'vitest';
import {
  approvalUnsourcedTargetFields,
  unsourcedTargetFieldLine,
  unsourcedTargetFieldsText,
  UNSOURCED_NONE,
  UNSOURCED_NOT_ESTABLISHED,
} from '../approval-unsourced-target-fields';

/**
 * The producer's shape, verbatim from `:1740-1744`, on the reported run: one
 * unsourced target field, present in both rosters, with the normaliser's own
 * `reason` constant and a recorded `null` default.
 */
const PRODUCER_PAYLOAD = {
  source: ['legacy_notes'],
  target: [
    { field: 'mobile_phone', default_value: null, reason: 'No source field in generated mapping' },
  ],
  target_unsourced: ['mobile_phone'],
  target_required: ['mobile_phone'],
};

describe('approvalUnsourcedTargetFields — a payload that was read', () => {
  it('returns one entry per roster name for the producer shape', () => {
    const entries = approvalUnsourcedTargetFields({ unmapped_fields: PRODUCER_PAYLOAD });

    expect(entries).toHaveLength(1);
    expect(entries![0].field).toBe('mobile_phone');
    expect(entries![0].requirement).toBe('required');
  });

  it('returns an empty array — not null — when the dict records no unsourced target field', () => {
    // A real, positive fact: every target field has a source. It must stay
    // distinguishable from a payload that established nothing, which is the same
    // distinction R1.5 draws for `mapping_count` (do-NOT 7).
    const entries = approvalUnsourcedTargetFields({
      unmapped_fields: { source: [], target: [], target_unsourced: [], target_required: [] },
    });

    expect(entries).toEqual([]);
    expect(entries).not.toBeNull();
  });

  it('reports not_established when the payload predates the Unsourced_Keys', () => {
    // The common case rather than an edge: p37 is not deployed, so
    // `target_unsourced` / `target_required` are absent from every payload every
    // account produces today. Nothing may read as "optional" here (p45 do-NOT 5).
    const entries = approvalUnsourcedTargetFields({
      unmapped_fields: { source: [], target: ['mobile_phone'] },
    });

    expect(entries).toHaveLength(1);
    expect(entries![0].requirement).toBe('not_established');
  });
});

describe('approvalUnsourcedTargetFields — a payload that established nothing', () => {
  it('returns null for the literal null every pre-task-2.1 approval row carries', () => {
    expect(approvalUnsourcedTargetFields({ unmapped_fields: null })).toBeNull();
  });

  it('returns null when the key is absent — the degraded payload', () => {
    // `create_approval_payload`'s blanket-except return (`:1435-1439`) carries no
    // `unmapped_fields` at all. Not pinned as intended behaviour (p45 do-NOT 10,
    // `LGR-b2f79ef-13`) — only that it is reported as establishing nothing.
    expect(approvalUnsourcedTargetFields({ generated_by: 'us.anthropic.claude-sonnet-4-6' })).toBeNull();
  });

  it('returns null for details that are absent entirely', () => {
    expect(approvalUnsourcedTargetFields(undefined)).toBeNull();
  });

  it('returns null for an array, which is an object and would otherwise read as empty', () => {
    // The `Array.isArray` rejection earns its place here: without it
    // `unsourcedTargetFields` reads `target` off the array as `undefined` and
    // returns `[]`, reporting "no unsourced target fields" for a shape that said
    // nothing at all.
    expect(approvalUnsourcedTargetFields({ unmapped_fields: ['mobile_phone'] })).toBeNull();
  });

  it('returns null for a scalar left by an older writer', () => {
    // The shape the old `as number` cast claimed. It never existed on the wire,
    // and if it ever appears it establishes nothing rather than a count.
    expect(approvalUnsourcedTargetFields({ unmapped_fields: 3 })).toBeNull();
  });
});

describe('unsourcedTargetFieldLine — the requirement vocabulary, not a second spelling', () => {
  it.each([
    ['required' as const, 'mobile_phone (Required)'],
    ['optional' as const, 'mobile_phone (Optional)'],
    ['not_established' as const, 'mobile_phone (Not established)'],
  ])('renders %s using field-requirement.ts words', (requirement, expected) => {
    expect(unsourcedTargetFieldLine({ field: 'mobile_phone', requirement })).toBe(expected);
  });
});

describe('unsourcedTargetFieldsText — total, and never blank', () => {
  it('names every field on one line for the compact card', () => {
    expect(
      unsourcedTargetFieldsText({
        unmapped_fields: {
          source: [],
          target: ['mobile_phone', 'nickname'],
          target_unsourced: ['mobile_phone', 'nickname'],
          target_required: ['mobile_phone'],
        },
      }),
    ).toBe('mobile_phone (Required), nickname (Optional)');
  });

  it('says None for a read payload with an empty roster', () => {
    expect(
      unsourcedTargetFieldsText({ unmapped_fields: { source: [], target: [] } }),
    ).toBe(UNSOURCED_NONE);
  });

  it('says not-established for a payload that could not be read', () => {
    expect(unsourcedTargetFieldsText({ unmapped_fields: null })).toBe(UNSOURCED_NOT_ESTABLISHED);
  });

  it('never renders the two absences alike, and never renders "[object Object]"', () => {
    // The whole of the defect in two assertions: the pre-fix sibling site
    // rendered `"[object Object]"` for the producer's dict, and an unreadable
    // payload must not read like an empty one.
    const producer = unsourcedTargetFieldsText({ unmapped_fields: PRODUCER_PAYLOAD });
    const unreadable = unsourcedTargetFieldsText({ unmapped_fields: null });
    const emptyRoster = unsourcedTargetFieldsText({ unmapped_fields: { source: [], target: [] } });

    expect(producer).not.toContain('[object Object]');
    expect(producer).toBe('mobile_phone (Required)');
    expect(unreadable).not.toBe(emptyRoster);
  });

  it('is never the empty string, which is indistinguishable from a failed read', () => {
    for (const payload of [null, undefined, 3, [], {}, PRODUCER_PAYLOAD]) {
      expect(unsourcedTargetFieldsText({ unmapped_fields: payload }).length).toBeGreaterThan(0);
    }
  });
});
