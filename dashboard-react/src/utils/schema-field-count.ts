// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { Schema } from '../types';

/**
 * p35 DD4's three-way field count, defined **once**.
 *
 * Task 4.4 moved field resolution off `GET /api/schemas`, so a listing row
 * reports one of three things and they are not interchangeable:
 *
 * | `field_count` | meaning | R5.1 / R5.2 |
 * |---|---|---|
 * | a positive number | the source stated a count (Glue, free from the `get_tables` page) | fine |
 * | `0` | the source stated a count and it is zero | refuse the schema |
 * | `null` | no count was established — every uploaded, every S3 Tables row | must NOT be refused |
 *
 * **Why this is a module and not three expressions.** Design §5 risk 3 counted
 * the consumers of `fields?.length ?? 0` and found one more than it expected;
 * there are now three display sites plus a guard (`SchemaManagementPage`'s Fields
 * column, the wizard `Schema_Picker`'s option description,
 * `SchemaSourceSelector`'s three tabs, and the picker's zero-field verdict). Each
 * one written by hand is another chance to write `?? 0` and collapse `null` into
 * "declares nothing", which breaks R5.1's guard in the worst direction — it
 * refuses every schema a user uploads.
 */
export function schemaFieldCount(schema: Pick<Schema, 'field_count' | 'fields'>): number | null {
  // A stated count wins, **including 0** — the one value a `??` chain would
  // silently swallow.
  if (typeof schema.field_count === 'number') return schema.field_count;
  // Otherwise a present field list counts itself. A list that is present and
  // empty is a stated zero; an absent one (which `normalizeSchema` resolves to
  // `[]`) is not.
  return schema.fields?.length ? schema.fields.length : null;
}

/**
 * The count as an option description reads it — R5.6's "field count and source
 * type", with DD4's third state spelled out rather than guessed at.
 *
 * Never "0 fields" for a count that was never established: that is the sentence
 * the three `SchemaSourceSelector` sites and the wizard picker used to print for
 * every uploaded and every S3 Tables schema.
 */
export function fieldCountLabel(schema: Pick<Schema, 'field_count' | 'fields'>): string {
  const count = schemaFieldCount(schema);
  return count === null ? '\u2014 fields not loaded' : `${count} fields`;
}

/**
 * The count as a table cell reads it: the number, or an em dash that asserts
 * nothing. R1.3 forbids a displayed count that contradicts the detail modal, and
 * "0" for an unestablished count is exactly that contradiction.
 */
export function fieldCountCellText(schema: Pick<Schema, 'field_count' | 'fields'>): string {
  const count = schemaFieldCount(schema);
  return count === null ? '\u2014' : String(count);
}
