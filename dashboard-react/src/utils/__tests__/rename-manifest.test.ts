// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 task 6.2 — `utils/rename-manifest.ts`, the single reader of the
 * Approval_Payload's Rename_Manifest.
 *
 * The three states are the subject, and the third one is why this file exists.
 * Task 1.2's characterization of the producer recorded an obligation on *this*
 * reader in as many words: the manifest key "lands on the happy path only … so any
 * consumer of the new key must tolerate its absence." That tolerance cannot be
 * asserted on the Python side without pinning the producer's degraded `except` arm
 * as intended behaviour, which do-NOT 10 and `LGR-b2f79ef-13` both decline. So it
 * is asserted here, on the consumer, which is where it is actually a requirement.
 *
 * **Expectations come from the producer and from design §Data Models, never from
 * this module's own output.** The four manifest keys and the two rename keys are
 * design's; the `null`-versus-empty distinction is `LGR-4854b3d-1`'s three-state
 * ruling applied to a roster; the verbatim original is R10.2 plus p55 do-NOT 1.
 *
 * The rename RULE — targets conforming and pairwise distinct (R10.3, R10.4) — is
 * not tested here. It is the producer's, property-tested over ≥100 generated
 * inputs in `tests/unit/lambdas/schema_mapping/test_rename_manifest_derivation_prop.py`
 * and re-checked by `derive_rename_manifest`'s own closing invariant. A frontend
 * copy would be a second, weaker definition of "conforming" in a second language,
 * which is what `LGR-9e76249-7` exists to prevent.
 *
 * **Validates: Requirements 10.1, 10.2**
 */
import { describe, expect, it } from 'vitest';
import {
  approvalProposedRenames,
  displayName,
  proposedRenameLine,
  proposedRenames,
  proposedRenamesText,
  RENAMES_NONE,
  RENAMES_NOT_ESTABLISHED,
  RENAME_DISPLAY_LIMIT,
} from '../rename-manifest';

/** The producer's shape, as `create_approval_payload` writes it. */
const PRODUCER_MANIFEST = {
  manifest_id: 'mapping-hash-abc123',
  source_schema_hash: 'sha256:abc123',
  approval_id: null,
  renames: [
    { original_name: 'first name', target_name: 'first_name' },
    { original_name: 'first-name', target_name: 'first_name_2' },
    { original_name: '2024-total', target_name: '_2024_total' },
  ],
};

describe('proposedRenames — a payload that was read', () => {
  it('returns one entry per Proposed_Rename, in the order received', () => {
    const entries = proposedRenames(PRODUCER_MANIFEST);
    expect(entries?.map((e) => e.original_name)).toEqual(['first name', 'first-name', '2024-total']);
  });

  it('carries BOTH names of every rename (R10.2)', () => {
    for (const entry of proposedRenames(PRODUCER_MANIFEST) ?? []) {
      expect(Object.keys(entry).sort()).toEqual(['original_name', 'target_name']);
      expect(typeof entry.original_name).toBe('string');
      expect(typeof entry.target_name).toBe('string');
    }
  });

  it('does not re-sort, so the producer stays the ordering authority', () => {
    // R10.4's collision suffix is defined in SOURCE-COLUMN order, so the order is
    // part of the names' meaning: `first_name` belongs to whichever original came
    // first in the header row. A reader that sorted would present a different rule.
    const reversed = {
      ...PRODUCER_MANIFEST,
      renames: [...PRODUCER_MANIFEST.renames].reverse(),
    };
    expect(proposedRenames(reversed)?.map((e) => e.target_name)).toEqual([
      '_2024_total',
      'first_name_2',
      'first_name',
    ]);
  });

  it('carries the original name VERBATIM — no trim, no case fold, no sanitization', () => {
    // do-NOT 1: the manifest is a PROPOSAL reaching a human, and the whole point
    // of showing both names is that the original is the one the source actually
    // has. A reader that tidied it would show a third string that is neither.
    const entries = proposedRenames({
      renames: [{ original_name: '  Mixed Case  ', target_name: 'mixed_case' }],
    });
    expect(entries?.[0].original_name).toBe('  Mixed Case  ');
  });
});

describe('proposedRenames — a payload that established nothing', () => {
  it('returns null for an absent manifest', () => {
    expect(proposedRenames(undefined)).toBeNull();
  });

  it('returns null for a literal null — the shape every pre-p55 row projects', () => {
    // `approval_handler._schema_mapping_payload` reads the key with `.get`, so a
    // mapping row written before p55 arrives present-and-null rather than absent.
    expect(proposedRenames(null)).toBeNull();
  });

  it('returns null when the manifest carries no renames key at all', () => {
    expect(proposedRenames({ manifest_id: 'm', source_schema_hash: 'h', approval_id: null })).toBeNull();
  });

  it('returns null for an ARRAY, which typeof calls an object', () => {
    // Not redundant with the typeof test: reading `renames` off an array yields
    // undefined, and reporting "no renames proposed" for a shape that stated
    // nothing is the collapse the three-state discipline exists to prevent.
    expect(proposedRenames([{ original_name: 'a', target_name: 'b' }] as never)).toBeNull();
  });

  it('returns null for a scalar', () => {
    expect(proposedRenames(3 as never)).toBeNull();
  });
});

describe('proposedRenames — an entry it cannot present', () => {
  it('skips an entry missing either name rather than inventing the other half', () => {
    // R10.2 requires both names. `String(undefined)` or echoing the original as
    // the target would put a claim on screen the wire does not support; skipping
    // is the only answer that makes no claim.
    const entries = proposedRenames({
      renames: [
        { original_name: 'first name' },
        { target_name: 'first_name' },
        { original_name: 'ok name', target_name: 'ok_name' },
      ],
    });
    expect(entries).toEqual([{ original_name: 'ok name', target_name: 'ok_name' }]);
  });

  it('skips a non-string name and a non-object entry', () => {
    const entries = proposedRenames({
      renames: ['first name', null, 42, { original_name: 1, target_name: 'x' }],
    });
    expect(entries).toEqual([]);
  });

  it('skips an empty name — an empty cell is indistinguishable from a missing one', () => {
    expect(proposedRenames({ renames: [{ original_name: '', target_name: 'x' }] })).toEqual([]);
  });

  it('distinguishes "read, proposes nothing" from "established nothing"', () => {
    // The distinction all three surfaces render differently, and the reason this
    // returns a nullable array rather than an always-empty one.
    expect(proposedRenames({ renames: [] })).toEqual([]);
    expect(proposedRenames(null)).toBeNull();
  });
});

describe('approvalProposedRenames — the Approval details adapter', () => {
  it('reads the manifest off details.rename_manifest', () => {
    const entries = approvalProposedRenames({ rename_manifest: PRODUCER_MANIFEST });
    expect(entries).toHaveLength(3);
  });

  it('answers null for details that carry no manifest, and for no details at all', () => {
    expect(approvalProposedRenames({ generated_by: 'us.anthropic.claude-sonnet-4-6' })).toBeNull();
    expect(approvalProposedRenames(undefined)).toBeNull();
  });

  it('answers null for the degraded producer arm, whose three keys include no manifest', () => {
    // `create_approval_payload`'s `except` arm returns exactly
    // {approval_type, error, generated_by}. Task 1.2 recorded that this reader
    // must tolerate it; this is that tolerance, asserted on the consumer.
    expect(
      approvalProposedRenames({
        approval_type: 'schema_mapping',
        error: 'Failed to create approval payload: KeyError',
        generated_by: '',
      }),
    ).toBeNull();
  });
});

describe('displayName — length is capped and the truncation is MARKED', () => {
  it('leaves a real column name untouched', () => {
    expect(displayName('first name')).toBe('first name');
  });

  it('leaves a name exactly at the limit untouched', () => {
    const exact = 'c'.repeat(RENAME_DISPLAY_LIMIT);
    expect(displayName(exact)).toBe(exact);
  });

  it('never returns more characters than the limit', () => {
    // A 2 MB column name in a table cell is the same class of problem as a 2 MB
    // error string, which is what `identifier_utils._DISPLAY_LIMIT`'s 300 exists
    // for. The number here is its own; the reasoning is borrowed.
    expect(displayName('c'.repeat(2_000_000)).length).toBeLessThanOrEqual(RENAME_DISPLAY_LIMIT);
  });

  it('says that it truncated, and how long the real name is', () => {
    // An elision the reader cannot see is a false statement about what the column
    // is called — and this table exists so an approver can compare two names.
    const rendered = displayName('c'.repeat(500));
    expect(rendered).toContain('truncated');
    expect(rendered).toContain('500');
  });
});

describe('proposedRenameLine and proposedRenamesText', () => {
  it('puts both names on the line, in the direction of the change', () => {
    expect(proposedRenameLine({ original_name: 'first name', target_name: 'first_name' })).toBe(
      'first name → first_name',
    );
  });

  it('caps both halves of the line, so one long name cannot hide the other', () => {
    const line = proposedRenameLine({ original_name: 'c'.repeat(500), target_name: 'ok_name' });
    expect(line).toContain('truncated');
    expect(line.endsWith('→ ok_name')).toBe(true);
  });

  it('is total over all three states and never blank', () => {
    // A blank is indistinguishable from a failed read, which is what the em dash
    // exists to remove.
    expect(proposedRenamesText({ rename_manifest: PRODUCER_MANIFEST })).toContain('first name → first_name');
    expect(proposedRenamesText({ rename_manifest: { renames: [] } })).toBe(RENAMES_NONE);
    expect(proposedRenamesText({ rename_manifest: null })).toBe(RENAMES_NOT_ESTABLISHED);
    expect(proposedRenamesText(undefined)).toBe(RENAMES_NOT_ESTABLISHED);

    for (const details of [
      { rename_manifest: PRODUCER_MANIFEST },
      { rename_manifest: { renames: [] } },
      { rename_manifest: null },
      {},
      undefined,
    ]) {
      expect(proposedRenamesText(details).length).toBeGreaterThan(0);
    }
  });

  it('gives the three states three distinct renderings', () => {
    const read = proposedRenamesText({ rename_manifest: PRODUCER_MANIFEST });
    const none = proposedRenamesText({ rename_manifest: { renames: [] } });
    const unread = proposedRenamesText({ rename_manifest: null });
    expect(new Set([read, none, unread]).size).toBe(3);
  });
});
