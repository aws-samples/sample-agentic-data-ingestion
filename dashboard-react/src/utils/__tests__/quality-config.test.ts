// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for `utils/quality-config.ts` — the Quality_Config's wire keys, its
 * dedup-column rule, and the three-state read of `data_quality_enabled`.
 *
 * Validates: Requirements R7.1, R7.2, R7.5 (and R6.6, whose "no existing
 * pipeline's behaviour changes" is what `readDataQualityEnabled` protects).
 *
 * The interesting test in this file is the last group. `readDataQualityEnabled`
 * exists because `?? false` is the obvious thing to write here and it is wrong: an
 * S3 Tables pipeline has data quality on by template default and stores no
 * `data_quality_enabled` key, so reading absence as `false` and re-submitting
 * writes an explicit opt-OUT and moves that pipeline's verdict from `enforced` to
 * `advisory`. The tests below pin the distinction so the simplification cannot be
 * reintroduced silently.
 */
import { describe, it, expect } from 'vitest';
import {
  DATA_QUALITY_ENABLED_REQUEST_KEY,
  DEDUP_COLUMNS_REQUEST_KEY,
  DEDUP_ENABLED_REQUEST_KEY,
  MAX_DEDUP_COLUMN_LENGTH,
  QUALITY_CONFIG_REQUEST_KEY,
  dedupColumnsError,
  formatDedupColumns,
  parseDedupColumns,
  readDataQualityEnabled,
} from '../quality-config';

describe('the wire keys', () => {
  it('spell exactly what shared/quality_verdict.py spells', () => {
    // `LGR-eee8ac8-5`: writer and readers live in different Lambdas and are only
    // ever compared through these strings, so a second spelling would not fail —
    // it would make every opt-in silently never match. These four literals are the
    // TypeScript end of that hop and have to agree with `QUALITY_CONFIG_KEY`,
    // `DATA_QUALITY_ENABLED_KEY`, `DEDUP_ENABLED_KEY` and `DEDUP_COLUMNS_KEY`.
    expect(QUALITY_CONFIG_REQUEST_KEY).toBe('quality_config');
    expect(DATA_QUALITY_ENABLED_REQUEST_KEY).toBe('data_quality_enabled');
    expect(DEDUP_ENABLED_REQUEST_KEY).toBe('dedup_enabled');
    expect(DEDUP_COLUMNS_REQUEST_KEY).toBe('dedup_columns');
  });

  it('are snake_case, because the wizard is the one normalisation authority', () => {
    // p63 R1.2 permits exactly one normaliser and p64 2a.1b placed it on this
    // side. A camelCase key here would need a server-side translator, which
    // `_submitted_quality_config`'s docstring explicitly forbids adding.
    for (const key of [
      QUALITY_CONFIG_REQUEST_KEY,
      DATA_QUALITY_ENABLED_REQUEST_KEY,
      DEDUP_ENABLED_REQUEST_KEY,
      DEDUP_COLUMNS_REQUEST_KEY,
    ]) {
      expect(key).toMatch(/^[a-z][a-z0-9_]*$/);
    }
  });
});

describe('parseDedupColumns', () => {
  it('splits on commas and on whitespace', () => {
    expect(parseDedupColumns('patient_id, visit_date')).toEqual(['patient_id', 'visit_date']);
    expect(parseDedupColumns('patient_id visit_date')).toEqual(['patient_id', 'visit_date']);
    expect(parseDedupColumns('a,  b ,\nc')).toEqual(['a', 'b', 'c']);
  });

  it('reads an empty or absent field as an empty list, not as a sentinel', () => {
    // R7.2: `[]` MEANS every mapped target column, which is what
    // `dropDuplicates()` with no argument already does. So absence and emptiness
    // agree here and neither needs a sentinel — the opposite of the
    // Selected_Worksheet, where `''` is a recorded selection naming no sheet.
    expect(parseDedupColumns('')).toEqual([]);
    expect(parseDedupColumns('   ')).toEqual([]);
    expect(parseDedupColumns(undefined)).toEqual([]);
    expect(parseDedupColumns(null)).toEqual([]);
  });

  it('discards separators only — never a non-empty token', () => {
    // The line between parsing an input and filtering a value.
    // `_validate_quality_config` REFUSES an offending entry rather than dropping
    // it (`LGR-4ea43f3-4`'s reason: a dedup key list quietly missing a column
    // deduplicates on the wrong identity), so a bad name must survive parsing and
    // reach `dedupColumnsError`, which reports it.
    expect(parseDedupColumns('ok, 9bad, also-bad')).toEqual(['ok', '9bad', 'also-bad']);
  });

  it('does not de-duplicate or case-fold', () => {
    // Both would be silent alterations of a value bound for generated code.
    expect(parseDedupColumns('Id, id, ID')).toEqual(['Id', 'id', 'ID']);
  });

  it('round-trips through formatDedupColumns', () => {
    const columns = ['patient_id', 'visit_date', '_internal'];
    expect(parseDedupColumns(formatDedupColumns(columns))).toEqual(columns);
  });
});

describe('formatDedupColumns', () => {
  it('reads a missing list defensively (R7.5)', () => {
    // A draft saved before `qualityConfig` existed restores it as `undefined`
    // despite the type, because `readDraft` is a bare `JSON.parse(raw) as T` and
    // `handleRestoreDraft` replaces rather than merges.
    expect(formatDedupColumns(undefined)).toBe('');
    expect(formatDedupColumns(null)).toBe('');
    expect(formatDedupColumns([])).toBe('');
  });
});

describe('dedupColumnsError', () => {
  it('accepts an empty or absent list — emptiness is the default, not an error', () => {
    expect(dedupColumnsError([])).toBeUndefined();
    expect(dedupColumnsError(undefined)).toBeUndefined();
    expect(dedupColumnsError(null)).toBeUndefined();
  });

  it('accepts every shape the Identifier_Allowlist accepts', () => {
    expect(dedupColumnsError(['id', '_id', 'Patient_Id_2', 'A'])).toBeUndefined();
  });

  it('names the offending column rather than reporting a count', () => {
    // The whole point of checking on this step: the operator has to know WHICH
    // name. A generic "invalid columns" message sends them back to guess.
    expect(dedupColumnsError(['ok', 'has-hyphen'])).toContain('has-hyphen');
    expect(dedupColumnsError(['9leading_digit'])).toContain('9leading_digit');
    expect(dedupColumnsError(['has space'])).toContain('has space');
    expect(dedupColumnsError(['dotted.name'])).toContain('dotted.name');
  });

  it('refuses the injection shapes the allowlist exists for', () => {
    // These names are interpolated into `dropDuplicates([...])` in the generated
    // Glue job, so a column name is a generated-code sink.
    expect(dedupColumnsError(['id"], ["evil'])).toBeDefined();
    expect(dedupColumnsError(['id); import os; ('])).toBeDefined();
    expect(dedupColumnsError(['id\n'])).toBeDefined();
  });

  it('enforces the same 255-character ceiling shared/identifier_utils.py does', () => {
    expect(dedupColumnsError(['a'.repeat(MAX_DEDUP_COLUMN_LENGTH)])).toBeUndefined();
    expect(dedupColumnsError(['a'.repeat(MAX_DEDUP_COLUMN_LENGTH + 1)])).toBeDefined();
  });

  it('rejects a non-string entry without throwing', () => {
    // Reached only from a record predating `_validate_quality_config`, but the
    // function is exported and a caller can hand it anything.
    expect(dedupColumnsError([42 as unknown as string])).toBeDefined();
    expect(dedupColumnsError([''])).toBeDefined();
  });
});

describe('readDataQualityEnabled', () => {
  it('keeps an explicit boolean', () => {
    expect(readDataQualityEnabled(true)).toBe(true);
    expect(readDataQualityEnabled(false)).toBe(false);
  });

  it('reads an ABSENT value as undefined, never as false', () => {
    // The trap this module exists for. An existing S3 Tables pipeline has data
    // quality on by template default and carries NO `data_quality_enabled` key —
    // `_default_quality_config` omits it deliberately. Returning `false` here
    // would let the edit flow write an explicit opt-OUT, removing the row-level
    // split and quarantine write that target type has had since P28 and reporting
    // the pipeline's verdict as `advisory` where it used to say `enforced`. R6.6
    // forbids exactly that, and nothing would report it.
    expect(readDataQualityEnabled(undefined)).toBeUndefined();
    expect(readDataQualityEnabled(undefined)).not.toBe(false);
  });

  it('reads a non-boolean as undefined, matching resolve_data_quality_enabled', () => {
    // `code_assembly.resolve_data_quality_enabled` honours `isinstance(explicit,
    // bool)` and otherwise falls back to the per-template default. A record
    // carrying a non-boolean predates `_validate_quality_config`'s 400, so the
    // template default is what it would have got at the time. Note `1` and `0`
    // land here too, which is the same reason the Python validator uses
    // `isinstance(value, bool)` — `isinstance(1, bool)` is False.
    for (const raw of [null, 'true', 'false', '', 0, 1, {}, []]) {
      expect(readDataQualityEnabled(raw)).toBeUndefined();
    }
  });
});
