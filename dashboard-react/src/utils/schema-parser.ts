// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Utilities for parsing schema files (JSON Schema, JSON array-of-objects, CSV). */

export interface SchemaField {
  name: string;
  type: string;
  nullable: boolean;
  description: string;
}

export interface SchemaParseResult {
  success: boolean;
  fields: SchemaField[];
  error?: string;
}

/**
 * Parse a schema file based on its extension.
 * Supports .json (JSON Schema or array-of-objects) and .csv files.
 */
export async function parseSchemaFile(file: File): Promise<SchemaParseResult> {
  const ext = file.name.split('.').pop()?.toLowerCase();

  if (ext === 'json') return parseJsonSchema(await file.text());
  if (ext === 'csv') return parseCsvSchema(await file.text());
  return { success: false, fields: [], error: `Unsupported file type: .${ext}` };
}

/**
 * Parse a JSON string as either a JSON Schema (with `properties`) or
 * an array-of-objects (infer schema from first item).
 */
export function parseJsonSchema(text: string): SchemaParseResult {
  try {
    const schema = JSON.parse(text);

    // JSON Schema with properties
    if (schema.properties && typeof schema.properties === 'object') {
      const fields: SchemaField[] = Object.entries(schema.properties).map(
        ([name, def]: [string, any]) => ({
          name,
          type: def.type || 'string',
          nullable: !(schema.required || []).includes(name),
          description: def.description || '',
        }),
      );
      return { success: true, fields };
    }

    // Array of objects — infer from first item
    if (Array.isArray(schema) && schema.length > 0 && typeof schema[0] === 'object' && schema[0] !== null) {
      const fields: SchemaField[] = Object.entries(schema[0]).map(([name, value]) => ({
        name,
        type: typeof value === 'number'
          ? 'number'
          : typeof value === 'boolean'
            ? 'boolean'
            : value === null
              ? 'string'
              : typeof value,
        nullable: true,
        description: '',
      }));
      return { success: true, fields };
    }

    // Object with "fields" array — AgenticDataIngestion native schema format
    if (Array.isArray(schema.fields) && schema.fields.length > 0 && typeof schema.fields[0] === 'object') {
      const fields: SchemaField[] = schema.fields.map((f: any) => ({
        name: f.name || f.field_name || '',
        type: f.type || f.field_type || 'string',
        nullable: f.required === true ? false : (f.nullable ?? true),
        description: f.description || '',
      }));
      return { success: true, fields };
    }

    return {
      success: false,
      fields: [],
      error: 'Schema must have "properties" or be an array of objects',
    };
  } catch (e) {
    return { success: false, fields: [], error: `Invalid JSON: ${(e as Error).message}` };
  }
}

/**
 * Parse a CSV string — extract column names from the header row and
 * infer types from the first 5 data rows.
 */
export function parseCsvSchema(text: string): SchemaParseResult {
  const lines = text.split('\n').filter((l) => l.trim());
  if (lines.length === 0) return { success: false, fields: [], error: 'Empty CSV file' };

  const headers = lines[0].split(',').map((h) => h.trim().replace(/^"|"$/g, ''));
  const sampleRows = lines.slice(1, 6); // First 5 data rows

  const fields: SchemaField[] = headers.map((name, i) => ({
    name,
    type: inferTypeFromSamples(sampleRows.map((r) => r.split(',')[i]?.trim())),
    nullable: true,
    description: '',
  }));

  return { success: true, fields };
}

/**
 * Infer a column type from sample values.
 * Checks for integer, number, boolean; defaults to string.
 */
export function inferTypeFromSamples(samples: (string | undefined)[]): string {
  const values = samples.filter((s): s is string => s != null && s !== '');
  if (values.length === 0) return 'string';

  const allInteger = values.every((v) => /^-?\d+$/.test(v));
  if (allInteger) return 'integer';

  const allNumber = values.every((v) => /^-?\d+(\.\d+)?$/.test(v));
  if (allNumber) return 'number';

  const allBoolean = values.every((v) => ['true', 'false'].includes(v.toLowerCase()));
  if (allBoolean) return 'boolean';

  return 'string';
}
