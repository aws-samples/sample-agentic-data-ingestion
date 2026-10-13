// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 task 1.3 — unit model for `utils/glue-target-eligibility.ts`.
 *
 * **Expectations come from the requirements and from the Python pre-flight, not
 * from this module's own output.** `LGR-28b4d75-5` (**template / ratified**):
 * "a test written from the producer's output cannot detect the producer
 * drifting, because the output is its own oracle." The two literals below are
 * transcribed from `dashboard_api/handlers/resources.py:338-340`
 * (`_GLUE_DATABASE_PREFIX`, `_REASON_GLUE_DATABASE_PREFIX`) and the predicate's
 * exact shape from `_glue_database_prefix_refusal` (`resources.py:420`) —
 * a bare `str.startswith`, with no trimming and no case folding.
 *
 * That transcription is a **convenience for reading, not the pin**: p66 task 1.4
 * adds `tests/contracts/test_glue_target_prefix_cross_language.py`, which parses
 * both sources and compares them, so drift is caught there rather than here. The
 * assertions in this file would still pass if both sides moved together, which
 * is why 1.4 exists.
 *
 * Coverage note: `.kiro/steering/test-mapping.md`'s `fileMatchPattern` covers
 * Python files under `lambda/` and `agentcore/` plus shell scripts, and carries
 * no frontend rows at all, so it names no gate for this file. The gate is
 * `make test-frontend`, whose **two legs both gate** (vitest and
 * `tsc --noEmit`). The adjacent precedent is
 * `utils/__tests__/schema-field-count.test.ts`, the sibling util's unit model.
 */
import { describe, it, expect } from 'vitest';
import {
  GLUE_TARGET_DATABASE_PREFIX,
  GLUE_TARGET_EXCLUSION_REASON,
  GLUE_TARGET_EXCLUSION_NOTE,
  GLUE_TARGET_NONE_ELIGIBLE,
  GLUE_TARGET_NO_DATABASES,
  GLUE_TARGET_NO_TABLES_NEXT_STEP,
  glueTargetNoTablesEmpty,
  isEligibleGlueTargetDatabase,
  partitionGlueTargetDatabases,
} from '../glue-target-eligibility';

/** As written at `resources.py:338`. */
const PYTHON_PREFIX = 'agentic_data_ingestion_';
/** As written at `resources.py:340` (`_REASON_GLUE_DATABASE_PREFIX`). */
const PYTHON_REASON = 'glue_catalog_database_prefix';

function db(database_name: string) {
  return { database_name };
}

describe('the two literals R1.4 shares with the pre-flight', () => {
  it('spells the prefix exactly as the Python pre-flight does', () => {
    expect(GLUE_TARGET_DATABASE_PREFIX).toBe(PYTHON_PREFIX);
  });

  it('uses the pre-flight’s own reason token, so a refusal reads as one rule (O4)', () => {
    expect(GLUE_TARGET_EXCLUSION_REASON).toBe(PYTHON_REASON);
  });
});

describe('isEligibleGlueTargetDatabase — R1.1', () => {
  it('accepts a prefixed name', () => {
    expect(isEligibleGlueTargetDatabase('agentic_data_ingestion_kjljlkj_ba061ab9')).toBe(true);
  });

  it('accepts the bare prefix, because the pre-flight’s startswith does', () => {
    // Not a judgement about whether such a database is useful — only that the
    // picker and the pre-flight agree about it. Disagreeing here would produce
    // the false accept O4 exists to prevent.
    expect(isEligibleGlueTargetDatabase(GLUE_TARGET_DATABASE_PREFIX)).toBe(true);
  });

  it('refuses the pre-rename agentglue_ prefix', () => {
    // The 84 of 93 databases the measured account holds (requirements.md).
    expect(isEligibleGlueTargetDatabase('agentglue_verify172_s3tables_73f77e82')).toBe(false);
  });

  it('refuses a name that is the prefix minus its trailing underscore', () => {
    expect(isEligibleGlueTargetDatabase('agentic_data_ingestion')).toBe(false);
  });

  it('refuses a name that merely contains the prefix', () => {
    expect(isEligibleGlueTargetDatabase('legacy_agentic_data_ingestion_x')).toBe(false);
  });

  it('does NOT trim — a leading space makes it ineligible, exactly as in Python', () => {
    // `resources.py:420` is `database_name.startswith(...)` with no `.strip()`.
    // Trimming here would offer a name the pre-flight then refuses.
    expect(isEligibleGlueTargetDatabase(' agentic_data_ingestion_x')).toBe(false);
  });

  it('does NOT fold case', () => {
    expect(isEligibleGlueTargetDatabase('AGENTIC_DATA_INGESTION_x')).toBe(false);
    expect(isEligibleGlueTargetDatabase('Agentic_Data_Ingestion_x')).toBe(false);
  });

  it('refuses the empty name fetchGlueDatabases produces for an unnamed row', () => {
    // `use-resources.ts:104` maps `d.database_name || d.name || ''`.
    expect(isEligibleGlueTargetDatabase('')).toBe(false);
  });
});

describe('partitionGlueTargetDatabases — R1.1 and R1.5', () => {
  it('returns the eligible sublist in source order', () => {
    const input = [
      db('agentglue_a'),
      db('agentic_data_ingestion_b'),
      db('zzz_c'),
      db('agentic_data_ingestion_a'),
    ];
    const { eligible } = partitionGlueTargetDatabases(input);
    // `_b` before `_a` — the endpoint's order, not alphabetical. A sort or a
    // reverse anywhere on this path fails here.
    expect(eligible.map((d) => d.database_name)).toEqual([
      'agentic_data_ingestion_b',
      'agentic_data_ingestion_a',
    ]);
  });

  it('reports the input length as totalCount, not the eligible length', () => {
    const { eligible, totalCount } = partitionGlueTargetDatabases([
      db('agentglue_a'),
      db('agentic_data_ingestion_b'),
      db('other'),
    ]);
    expect(totalCount).toBe(3);
    expect(eligible).toHaveLength(1);
  });

  it('distinguishes "databases exist, none eligible" from "none at all" — R1.5', () => {
    // The whole reason this returns two numbers. A filter-only helper makes
    // these two cases indistinguishable, and R1.5 requires two sentences.
    const noneEligible = partitionGlueTargetDatabases([db('agentglue_a'), db('agentglue_b')]);
    expect(noneEligible.eligible).toEqual([]);
    expect(noneEligible.totalCount).toBe(2);

    const noneAtAll = partitionGlueTargetDatabases([]);
    expect(noneAtAll.eligible).toEqual([]);
    expect(noneAtAll.totalCount).toBe(0);

    expect(noneEligible.totalCount).not.toBe(noneAtAll.totalCount);
  });

  it('returns every element when all are eligible', () => {
    const input = [db('agentic_data_ingestion_a'), db('agentic_data_ingestion_b')];
    const { eligible, totalCount } = partitionGlueTargetDatabases(input);
    expect(eligible).toEqual(input);
    expect(totalCount).toBe(2);
  });

  it('preserves the caller’s own element shape, so both components can map it', () => {
    const input = [{ database_name: 'agentic_data_ingestion_a', extra: 7 }];
    const { eligible } = partitionGlueTargetDatabases(input);
    expect(eligible[0].extra).toBe(7);
  });

  it('does not mutate its input', () => {
    const input = [db('agentglue_a'), db('agentic_data_ingestion_b')];
    const snapshot = input.map((d) => d.database_name);
    partitionGlueTargetDatabases(input);
    expect(input.map((d) => d.database_name)).toEqual(snapshot);
  });
});

describe('the copy — R1.2, R1.3, R1.5, R2.1, R2.4, C8, C10', () => {
  it('the exclusion note names the prefix rule — R1.2', () => {
    expect(GLUE_TARGET_EXCLUSION_NOTE).toContain(GLUE_TARGET_DATABASE_PREFIX);
    // The clause the info Alert and the pre-flight's 400 already share, so this
    // is the same phrasing rather than a fourth one (C10).
    expect(GLUE_TARGET_EXCLUSION_NOTE).toContain(
      'grants access only to databases and tables under that prefix',
    );
  });

  it('the none-eligible state says databases exist AND names the prefix rule — R1.3, R1.5', () => {
    expect(GLUE_TARGET_NONE_ELIGIBLE).toContain('has Glue databases');
    expect(GLUE_TARGET_NONE_ELIGIBLE).toContain(GLUE_TARGET_DATABASE_PREFIX);
  });

  it('the none-eligible state does not restate the Alert’s other two bullets — C10', () => {
    // The Alert's first and third preconditions. Naming one rule is the whole
    // point; repeating all three would be the fourth phrasing C10 forbids.
    expect(GLUE_TARGET_NONE_ELIGIBLE).not.toContain('S3 location');
    expect(GLUE_TARGET_NONE_ELIGIBLE).not.toContain('adi-processed-files');
    expect(GLUE_TARGET_NONE_ELIGIBLE).not.toContain('adi-source-files');
  });

  it('the none-eligible state stands alone — no locative pointer that would be false in the modal', () => {
    // `TargetConfigForm.tsx:142-185` renders two FormFields and no Alert, so
    // "see above" would be a false reference on that surface (design §4.3).
    expect(GLUE_TARGET_NONE_ELIGIBLE).not.toMatch(/\babove\b/i);
  });

  it('the no-databases state asserts nothing about the prefix — R1.5’s other arm', () => {
    // Nothing was excluded here, so naming the rule would state a cause this
    // state did not check.
    expect(GLUE_TARGET_NO_DATABASES).not.toContain(GLUE_TARGET_DATABASE_PREFIX);
    expect(GLUE_TARGET_NO_DATABASES).toContain('No Glue databases');
  });

  it('the two database empty states are different sentences — R1.5', () => {
    expect(GLUE_TARGET_NONE_ELIGIBLE).not.toBe(GLUE_TARGET_NO_DATABASES);
  });

  it('the table empty state names the selected database — R2.1', () => {
    expect(glueTargetNoTablesEmpty('agentic_data_ingestion_kjljlkj_ba061ab9')).toContain(
      'agentic_data_ingestion_kjljlkj_ba061ab9',
    );
  });

  it('the table empty state carries its meaning in words — C8', () => {
    // Not an icon, not a colour, not the absence of options: a sentence.
    expect(glueTargetNoTablesEmpty('db_a')).toBe('The database db_a contains no tables.');
  });

  it('the next step names the one action C1 leaves available — R2.4', () => {
    expect(GLUE_TARGET_NO_TABLES_NEXT_STEP).toContain('does not create catalog tables');
    expect(GLUE_TARGET_NO_TABLES_NEXT_STEP).toContain('Register the table');
  });

  it('the next step offers no control and no retry — C1, and LGR-2bcdd0d-3’s false affordance', () => {
    // A create-table button cannot be honoured (C1) and a Retry cannot fix a
    // database that genuinely holds no tables. The copy must not imply either.
    expect(GLUE_TARGET_NO_TABLES_NEXT_STEP).not.toMatch(/\bretry\b/i);
    expect(GLUE_TARGET_NO_TABLES_NEXT_STEP).not.toMatch(/\btry again\b/i);
    expect(GLUE_TARGET_NO_TABLES_NEXT_STEP).not.toMatch(/\bcreate (the |a )?table\b/i);
  });

  it('every string is non-empty prose — C8’s "not absence"', () => {
    for (const copy of [
      GLUE_TARGET_EXCLUSION_NOTE,
      GLUE_TARGET_NONE_ELIGIBLE,
      GLUE_TARGET_NO_DATABASES,
      GLUE_TARGET_NO_TABLES_NEXT_STEP,
      glueTargetNoTablesEmpty('db_a'),
    ]) {
      expect(copy.trim().length).toBeGreaterThan(20);
    }
  });
});
