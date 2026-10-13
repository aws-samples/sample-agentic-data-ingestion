// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// P29 R11.2 — STEP_ORDER comes from constants.ts `PIPELINE_STEP_ORDER` ONLY.
//
// The review found local, mixed-case, rebuilt-per-render copies of a
// step-order array scattered across pages/components. Those were removed and
// every consumer now imports the single canonical `PIPELINE_STEP_ORDER`. These
// tests lock that in two ways:
//   1. content lock — `PIPELINE_STEP_ORDER` is the expected canonical ordered
//      list and every entry aligns to a canonical step name (p28 R8.2).
//   2. no-local-copy guard — no page/component re-declares its own step-order
//      array (detected by the co-occurrence of the ordering's first and last
//      step tokens inside a single non-sanctioned source file).
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { PIPELINE_STEP_ORDER } from '../constants';
import { CANONICAL_STEP_ORDER, normalizeStepName } from '../utils/step-names';

describe('PIPELINE_STEP_ORDER single-source content lock (P29 R11.2)', () => {
  it('is the expected canonical ordered list of SFN state ids', () => {
    expect([...PIPELINE_STEP_ORDER]).toEqual([
      'FileValidation', 'SchemaExtract', 'SchemaCompare', 'CheckSchemaAction',
      'SchemaMapping', 'CheckSchemaMappingApproval', 'CreateCode', 'TestCode',
      'CheckCodeApproval', 'CreateInfrastructure', 'CreateGluePipeline',
      'CreateQualityChecks', 'RunPipeline',
    ]);
  });

  it('aligns every entry to a canonical step name (p28 R8.2)', () => {
    for (const id of PIPELINE_STEP_ORDER) {
      expect(CANONICAL_STEP_ORDER).toContain(normalizeStepName(id));
    }
  });
});

/**
 * Recursively collect production `.ts`/`.tsx` files under `src/`, excluding
 * test files and the two sanctioned step-order sources.
 */
function collectSourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      collectSourceFiles(full, acc);
      continue;
    }
    if (!/\.(ts|tsx)$/.test(entry)) continue;
    if (/\.test\.|\.property\.test\.|\.spec\./.test(entry)) continue;
    acc.push(full);
  }
  return acc;
}

describe('no local STEP_ORDER copies outside constants.ts (P29 R11.2)', () => {
  const srcRoot = join(process.cwd(), 'src');

  // The ONLY sanctioned modules that may declare an ordered pipeline step
  // list: constants.ts (PIPELINE_STEP_ORDER, the single source) and
  // utils/step-names.ts (CANONICAL_STEP_ORDER, the snake_case canonical mirror
  // of shared/step_names.py that constants.ts aligns to).
  const SANCTIONED = new Set([
    join(srcRoot, 'constants.ts'),
    join(srcRoot, 'utils', 'step-names.ts'),
  ]);

  const files = collectSourceFiles(srcRoot).filter((f) => !SANCTIONED.has(f));

  it('finds no rebuilt step-order array (PascalCase) in any page/component', () => {
    // A local order copy would list the ordering's endpoints together. Their
    // co-occurrence in a single non-sanctioned file is the tell.
    const offenders = files.filter((f) => {
      const src = readFileSync(f, 'utf8');
      return src.includes("'FileValidation'") && src.includes("'RunPipeline'");
    });
    expect(offenders).toEqual([]);
  });

  it('finds no rebuilt step-order array (snake_case) in any page/component', () => {
    const offenders = files.filter((f) => {
      const src = readFileSync(f, 'utf8');
      return src.includes("'file_validation'") && src.includes("'run_pipeline'");
    });
    expect(offenders).toEqual([]);
  });

  it('declares STEP_ORDER-named arrays only in sanctioned modules', () => {
    // `\bconst|let|var <name>STEP_ORDER =` or `<name>StepOrder = [` outside the
    // sanctioned sources indicates a reintroduced local copy.
    const localDecl = /\b(?:const|let|var)\s+\w*(?:STEP_ORDER|StepOrder)\s*=\s*\[/;
    const offenders = files.filter((f) => localDecl.test(readFileSync(f, 'utf8')));
    expect(offenders).toEqual([]);
  });
});
