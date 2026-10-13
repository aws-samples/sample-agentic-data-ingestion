// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p35-schema-surfaces task 6.3 — the three-way field count, which is the crux of
 * the task and the one thing every display site and the picker's guard share.
 *
 * The cases are the wire shapes `handlers/schemas.py` actually sends after task
 * 4.4: a Glue row with a stated count (`:507`), an uploaded row with
 * `fieldCount: null` and no `fields` key (`:578`), an S3 Tables row the same
 * (`:641`), and DD9's detail object, which carries the list and no count.
 *
 * **Validates: Requirements 1.3, 5.1, 5.6**
 */
import { describe, it, expect } from 'vitest';
import {
  fieldCountCellText,
  fieldCountLabel,
  schemaFieldCount,
} from '../schema-field-count';

describe('schemaFieldCount — a stated count wins, including zero', () => {
  it('returns a positive stated count', () => {
    expect(schemaFieldCount({ field_count: 3, fields: [] })).toBe(3);
  });

  /** The value a `??` chain swallows, and the only one R5.1 refuses. */
  it('returns 0 when the source stated zero', () => {
    expect(schemaFieldCount({ field_count: 0, fields: [] })).toBe(0);
  });

  it('prefers the stated count over the length of a field list', () => {
    // The listing states the count; the detail supplies the list. They agree in
    // practice, and when they do not the source's own statement is the count.
    expect(
      schemaFieldCount({
        field_count: 2,
        fields: [{ field_name: 'a', field_type: 'string' }],
      }),
    ).toBe(2);
  });
});

describe('schemaFieldCount — no count established is null, never 0', () => {
  /** Every uploaded and every S3 Tables listing row after task 4.4. Reading this
   *  as 0 would refuse every schema a user uploads (R5.1's guard, inverted). */
  it('returns null for a row with no stated count and no field list', () => {
    expect(schemaFieldCount({ field_count: null, fields: [] })).toBeNull();
  });

  it('returns null when field_count is absent and fields resolved to empty', () => {
    // `normalizeSchema` resolves an absent `fields` key to `[]`, so an empty
    // list on its own cannot mean "declares zero".
    expect(schemaFieldCount({ fields: [] })).toBeNull();
  });

  it('counts a present field list when no count was stated — DD9 detail objects', () => {
    expect(
      schemaFieldCount({
        fields: [
          { field_name: 'region_code', field_type: 'string' },
          { field_name: 'customer_id', field_type: 'string' },
        ],
      }),
    ).toBe(2);
  });
});

describe('the two renderings', () => {
  it('labels an established count and refuses to invent one otherwise', () => {
    expect(fieldCountLabel({ field_count: 3, fields: [] })).toBe('3 fields');
    expect(fieldCountLabel({ field_count: 0, fields: [] })).toBe('0 fields');
    expect(fieldCountLabel({ field_count: null, fields: [] })).toBe('\u2014 fields not loaded');
  });

  it('renders a table cell as the number, or an em dash that asserts nothing', () => {
    expect(fieldCountCellText({ field_count: 3, fields: [] })).toBe('3');
    expect(fieldCountCellText({ field_count: 0, fields: [] })).toBe('0');
    expect(fieldCountCellText({ field_count: null, fields: [] })).toBe('\u2014');
  });
});
