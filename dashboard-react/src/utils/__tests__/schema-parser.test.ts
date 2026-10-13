// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import {
  parseJsonSchema,
  parseCsvSchema,
  inferTypeFromSamples,
} from '../schema-parser';

describe('parseJsonSchema', () => {
  it('parses JSON Schema with properties', () => {
    const input = JSON.stringify({
      properties: {
        id: { type: 'integer', description: 'Primary key' },
        name: { type: 'string', description: 'User name' },
        active: { type: 'boolean' },
      },
      required: ['id', 'name'],
    });

    const result = parseJsonSchema(input);
    expect(result.success).toBe(true);
    expect(result.fields).toHaveLength(3);
    expect(result.fields[0]).toEqual({
      name: 'id',
      type: 'integer',
      nullable: false,
      description: 'Primary key',
    });
    expect(result.fields[1]).toEqual({
      name: 'name',
      type: 'string',
      nullable: false,
      description: 'User name',
    });
    expect(result.fields[2]).toEqual({
      name: 'active',
      type: 'boolean',
      nullable: true,
      description: '',
    });
  });

  it('parses array-of-objects', () => {
    const input = JSON.stringify([
      { user_id: 42, email: 'test@example.com', verified: true },
      { user_id: 43, email: 'other@example.com', verified: false },
    ]);

    const result = parseJsonSchema(input);
    expect(result.success).toBe(true);
    expect(result.fields).toHaveLength(3);
    expect(result.fields[0]).toEqual({
      name: 'user_id',
      type: 'number',
      nullable: true,
      description: '',
    });
    expect(result.fields[1]).toEqual({
      name: 'email',
      type: 'string',
      nullable: true,
      description: '',
    });
    expect(result.fields[2]).toEqual({
      name: 'verified',
      type: 'boolean',
      nullable: true,
      description: '',
    });
  });

  it('returns error for plain object without properties', () => {
    const result = parseJsonSchema(JSON.stringify({ foo: 'bar' }));
    expect(result.success).toBe(false);
    expect(result.error).toContain('properties');
  });

  it('returns error for empty array', () => {
    const result = parseJsonSchema(JSON.stringify([]));
    expect(result.success).toBe(false);
    expect(result.error).toContain('properties');
  });

  it('returns error for invalid JSON', () => {
    const result = parseJsonSchema('not json {{{');
    expect(result.success).toBe(false);
    expect(result.error).toContain('Invalid JSON');
  });

  it('defaults type to string when missing', () => {
    const input = JSON.stringify({
      properties: { col: {} },
    });
    const result = parseJsonSchema(input);
    expect(result.success).toBe(true);
    expect(result.fields[0].type).toBe('string');
  });

  it('treats null values in array-of-objects as string type', () => {
    const input = JSON.stringify([{ val: null }]);
    const result = parseJsonSchema(input);
    expect(result.success).toBe(true);
    expect(result.fields[0].type).toBe('string');
  });
});

describe('fields array format (AgenticDataIngestion native)', () => {
  it('parses object with fields array', () => {
    const json = JSON.stringify({
      schema_name: 'test',
      fields: [
        { name: 'id', type: 'integer', required: true, description: 'Primary key' },
        { name: 'email', type: 'string', required: false, description: 'Email' },
      ],
    });
    const result = parseJsonSchema(json);
    expect(result.success).toBe(true);
    expect(result.fields).toHaveLength(2);
    expect(result.fields[0]).toEqual({ name: 'id', type: 'integer', nullable: false, description: 'Primary key' });
    expect(result.fields[1]).toEqual({ name: 'email', type: 'string', nullable: true, description: 'Email' });
  });

  it('maps field_name and field_type aliases', () => {
    const json = JSON.stringify({
      fields: [
        { field_name: 'col_a', field_type: 'string', description: 'Column A' },
      ],
    });
    const result = parseJsonSchema(json);
    expect(result.success).toBe(true);
    expect(result.fields[0].name).toBe('col_a');
    expect(result.fields[0].type).toBe('string');
  });

  it('treats required: true as nullable: false', () => {
    const json = JSON.stringify({
      fields: [
        { name: 'id', type: 'integer', required: true },
        { name: 'notes', type: 'string', required: false },
        { name: 'other', type: 'string' },
      ],
    });
    const result = parseJsonSchema(json);
    expect(result.fields[0].nullable).toBe(false);
    expect(result.fields[1].nullable).toBe(true);
    expect(result.fields[2].nullable).toBe(true);
  });
});

describe('parseCsvSchema', () => {
  it('parses CSV with header and data rows', () => {
    const csv = 'id,name,score\n1,Alice,95.5\n2,Bob,87.0\n3,Carol,92.3';
    const result = parseCsvSchema(csv);
    expect(result.success).toBe(true);
    expect(result.fields).toHaveLength(3);
    expect(result.fields[0]).toEqual({ name: 'id', type: 'integer', nullable: true, description: '' });
    expect(result.fields[1]).toEqual({ name: 'name', type: 'string', nullable: true, description: '' });
    expect(result.fields[2]).toEqual({ name: 'score', type: 'number', nullable: true, description: '' });
  });

  it('returns error for empty CSV', () => {
    const result = parseCsvSchema('');
    expect(result.success).toBe(false);
    expect(result.error).toContain('Empty CSV');
  });

  it('handles header-only CSV (no data rows)', () => {
    const result = parseCsvSchema('col_a,col_b');
    expect(result.success).toBe(true);
    expect(result.fields).toHaveLength(2);
    // No data rows → defaults to string
    expect(result.fields[0].type).toBe('string');
  });

  it('strips quotes from header names', () => {
    const csv = '"first_name","last_name"\nAlice,Smith';
    const result = parseCsvSchema(csv);
    expect(result.fields[0].name).toBe('first_name');
    expect(result.fields[1].name).toBe('last_name');
  });
});

describe('inferTypeFromSamples', () => {
  it('infers integer from whole numbers', () => {
    expect(inferTypeFromSamples(['1', '2', '3'])).toBe('integer');
  });

  it('infers number from decimals', () => {
    expect(inferTypeFromSamples(['1.5', '2.7', '3.0'])).toBe('number');
  });

  it('infers boolean from true/false', () => {
    expect(inferTypeFromSamples(['true', 'false', 'True'])).toBe('boolean');
  });

  it('infers string for mixed types', () => {
    expect(inferTypeFromSamples(['hello', '42', 'true'])).toBe('string');
  });

  it('returns string for empty samples', () => {
    expect(inferTypeFromSamples([])).toBe('string');
  });

  it('ignores undefined and empty values', () => {
    expect(inferTypeFromSamples([undefined, '', '10', '20'])).toBe('integer');
  });
});
