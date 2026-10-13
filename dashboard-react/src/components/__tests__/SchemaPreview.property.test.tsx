// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for SchemaPreview
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 24: Schema preview rendering
 *
 * **Validates: Requirements 17.5**
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup } from '@testing-library/react';
import { SchemaPreview } from '../SchemaSourceSelector';
import type { Schema, SchemaField } from '../../types';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const fieldNameArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz_'.split('')),
  { minLength: 1, maxLength: 20 },
);

const fieldTypeArb = fc.constantFrom(
  'string', 'integer', 'float', 'boolean', 'date', 'timestamp',
  'array', 'map', 'struct', 'decimal', 'bigint', 'binary',
);

const schemaFieldArb: fc.Arbitrary<SchemaField> = fc.record({
  field_name: fieldNameArb,
  field_type: fieldTypeArb,
  description: fc.option(fc.string({ minLength: 1, maxLength: 50 }), { nil: undefined }),
});

const schemaArb: fc.Arbitrary<Schema> = fc.record({
  schema_id: fc.uuid(),
  schema_name: fc.string({ minLength: 1, maxLength: 30 }),
  source_type: fc.constantFrom('uploaded', 'glue_data_catalog', 's3_table_bucket'),
  fields: fc.array(schemaFieldArb, { minLength: 1, maxLength: 15 }),
  created_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
});

// ---------------------------------------------------------------------------
// Property 24: Schema preview rendering
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 24: Schema preview rendering', () => {
  /**
   * **Validates: Requirements 17.5**
   *
   * For any Schema object with fields, the schema preview SHALL display
   * all field names and types.
   */
  it('displays all field names and types for any schema with fields', () => {
    fc.assert(
      fc.property(schemaArb, (schema) => {
        cleanup();
        const { container } = render(<SchemaPreview schema={schema} />);
        const text = container.textContent ?? '';

        for (const field of schema.fields) {
          expect(text).toContain(field.field_name);
          expect(text).toContain(field.field_type);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
