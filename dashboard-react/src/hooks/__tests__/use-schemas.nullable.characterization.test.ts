// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `normalizeField`'s `nullable` handling
 * (`use-schemas.ts:16-55`).
 *
 * Feature: p45-unmapped-target-field-visibility, task 3.1
 *
 * ---
 *
 * ## ✅ EXPECTED-RED WINDOW — CLOSED at task 10.1. All three tests pass.
 *
 * This file was written against the **post-§4.7 producer contract**, not against
 * what `normalizeField` returned when it was written: that version returned four
 * keys — `field_name`, `field_type`, `description`, `context` — and dropped
 * `nullable` at the one boundary it can cross (design C12). Task 10.1 carried it
 * (`typeof raw.nullable === 'boolean' ? raw.nullable : undefined`,
 * `use-schemas.ts:16-55`), which is what closed the window.
 *
 * **The red was the plan working, not a regression, and it is recorded here
 * rather than deleted.** `LGR-60c4985-6` (**template / ratified**) is the
 * practice being followed: a deliberate reversal is recorded *at the assertion*
 * so a later reader does not read it as a defect and revert the only thing that
 * would have caught one. The same argument applies to the reversal's *closing* —
 * a reader who finds these three greens should be able to see which change
 * earned them.
 *
 * | test | before task 10.1 | now |
 * |---|---|---|
 * | a stated `false` survives | ❌ RED — read `undefined` | ✅ green |
 * | a stated `true` survives | ❌ RED — read `undefined` | ✅ green |
 * | an unstated `nullable` reads back `undefined`, never `true` | ⚠️ FALSE green | ✅ green, and now load-bearing |
 *
 * **The third test was a FALSE GREEN, and that is why its green now means
 * something.** It used to pass because `normalizeField` dropped `nullable`
 * unconditionally — absence and presence were indistinguishable, so it could not
 * have failed for any input. Now the key is carried, so the *only* reason it
 * still reads `undefined` is that the ternary declined to answer for a producer
 * that said nothing. The same assertion now discriminates: a `?? true` written
 * here would turn it red.
 *
 * Design §7 places the going-green at design task 8; that is off by one. Tasks 8
 * and 9 made this third case *truthful about the wire* (`schemas.py:496` stopped
 * hardcoding `True`; `:1569` stopped defaulting to `True`); **task 10 is what
 * made the other two pass.**
 *
 * ---
 *
 * ## Expectations come from the producers, never from `normalizeField`'s return
 *
 * `LGR-28b4d75-5` (**template / ratified**): *"a test written from the
 * producer's output cannot detect the producer drifting, because the output is
 * its own oracle."* `normalizeField` is the thing being changed, so its current
 * return cannot be its own oracle. Every fixture below is a shape
 * `handlers/schemas.py` writes:
 *
 * | fixture | producer | what it states |
 * |---|---|---|
 * | `nullable: false` | `:1394` — `"nullable": field_name not in required` | a real boolean; the field IS in `required` |
 * | `nullable: true` | `:1365`, `:1458` — `"nullable": not field_def.get("required", False)` | a real boolean; the field is not required |
 * | **no `nullable` key** | `:496` after task 8 — the Glue branch stops emitting `"nullable": True`, because `StorageDescriptor.Columns[]` carries no nullability at all; `:1569`'s conditional key set then passes the absence through | nothing — the source never said |
 *
 * The third row is the one this spec exists for. Today `:496` hardcodes
 * `"nullable": True` and `:1569` defaults an absent key to `True`, so *"the
 * source declared it optional"* and *"the source said nothing"* arrive
 * identical — `LGR-4854b3d-1`'s collapse (**template / ratified**: a three-state
 * value *"is never collapsed"*, and the expression lives once), inside that
 * record's own `scope`, which names `handlers/schemas.py`.
 *
 * ## Absence, not `null`, is the third spelling
 *
 * Nothing here asserts `nullable: null` (do-NOT 17). `LGR-44d601e-1`
 * (**template / ratified**) clears a value by *removing the key*, because
 * *"a stored `""` would have to be distinguished from absence at every
 * reader"* — and `_schema_detail` already follows it for `displayLabel`
 * (*"absent, not null"*, `schemas.py:1554-1557`, `:1590-1592`). Three spellings
 * of two facts is the defect, not the fix.
 *
 * ## The `?? true` case is asserted ABSENT
 *
 * do-NOT 16 and do-NOT 5 in executable form. An input with no `nullable` must
 * **not** come back `true`. That assertion is what makes C12's *"there is
 * exactly one place in the frontend where `nullable` can enter"* load-bearing:
 * if a `?? true` is ever written here, or in a cell renderer, or in
 * `toWizardFields`, this test is the thing that says so.
 *
 * ## The API boundary is the only thing stubbed
 *
 * `normalizeField` is module-private and stays that way — exporting it is a
 * change to `use-schemas.ts`, and task 10.1 owns that file. So these tests
 * reach it the way `SchemaManagementPage.detail-modal.test.tsx:70-81` does:
 * mock `apiClient`, and let the real `useSchemaDetail` → `fetchSchemaDetail` →
 * `normalizeSchema` → `normalizeField` chain run. The fixture is DD9's **bare
 * schema object** (`:1577-1584`), not `{"schemas": [one]}`.
 *
 * _Requirements: 5.2, 5.3, 5.4_
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import type { ReactNode } from 'react';
import type { SchemaField } from '../../types';

const getMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: {
    get: getMock,
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { useSchemaDetail } from '../use-schemas';

/* ------------------------------------------------------------------ */
/*  The wire fixture — three fields, three nullability states.        */
/* ------------------------------------------------------------------ */

const SCHEMA_ID = 'uploaded_schema_alpha';

/**
 * `GET /api/schemas/{id}` as `_schema_detail` builds it (`schemas.py:1562-1584`):
 * a bare schema object whose `fields[]` carry `name` / `type` / `nullable` /
 * `description` / `context`.
 *
 * `region` deliberately carries **no `nullable` key at all**. That is the
 * post-task-8 Glue shape and the post-task-9 `_schema_detail` shape — not a
 * hypothetical. It is the only fixture in this file that a `??` chain can
 * silently answer for.
 */
const DETAIL_BODY = {
  schemaId: SCHEMA_ID,
  name: SCHEMA_ID,
  source: 'uploaded',
  targetType: 'Target Schema',
  lastModified: '2026-08-20T10:00:00+00:00',
  fields: [
    // `:1394` — `"nullable": field_name not in required`, with `order_id` IN
    // `required`. A real boolean stating "the schema demands this field".
    { name: 'order_id', type: 'string', nullable: false, description: 'Order key', context: '' },
    // `:1365` / `:1458` — `"nullable": not field_def.get("required", False)`,
    // with no `required`. A real boolean stating "the schema merely offers it".
    { name: 'order_date', type: 'date', nullable: true, description: '', context: '' },
    // `:496` after task 8 — the Glue branch emits no key, because
    // `StorageDescriptor.Columns[]` carries no nullability at all.
    { name: 'region', type: 'string', description: '', context: '' },
  ],
};

const INDEX_BY_NAME = { order_id: 0, order_date: 1, region: 2 } as const;

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

/** Run the real fetch/normalize chain and hand back `normalizeField`'s output. */
async function normalizedFields(): Promise<SchemaField[]> {
  const { result } = renderHook(() => useSchemaDetail(SCHEMA_ID), {
    wrapper: createWrapper(),
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  return result.current.data?.fields ?? [];
}

/*
 * The `observedNullable` scaffolding helper this file was written with is gone.
 * It existed to read `nullable` off a `SchemaField` that did not declare it, via
 * `field as unknown as Record<string, unknown>`, and its own comment set the
 * deletion trigger: *"Delete this helper once task 7.1 widens the type."* Task
 * 7.1 declared `SchemaField.nullable?: boolean` (`types.ts:404`), so the double
 * cast is now the only thing standing between the assertion and the type — and a
 * cast is exactly what should not be needed to observe a fact the interface
 * claims to carry.
 *
 * The assertions below therefore read `field.nullable` directly. **That is not a
 * narrowing.** `boolean | undefined` is the declared type and `toBe(false)` /
 * `toBe(true)` / `toBeUndefined()` compare the value `normalizeField` actually
 * produced. Do NOT reintroduce a `typeof` guard or a `?? ` at the read site: it
 * would launder a wrong value into a right-looking one inside the test, and the
 * test would then be its own oracle — exactly what `LGR-28b4d75-5` (**template /
 * ratified**) rejects.
 */

/* ------------------------------------------------------------------ */
/*  The three states.                                                 */
/* ------------------------------------------------------------------ */

describe("Feature: p45-unmapped-target-field-visibility, task 3.1: normalizeField's nullable is three-state", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockResolvedValue(DETAIL_BODY);
  });

  /**
   * **Validates: Requirements 5.2**
   *
   * ✅ Green since task 10.1. It was RED before it, because `normalizeField`
   * returned no `nullable` key and so read `undefined` where the producer had
   * stated `false`.
   *
   * `schemas.py:1394` writes `"nullable": field_name not in required`. For a
   * field named in `required` that is a real `false` — the schema *demands* this
   * field. R5.2 makes the Field_Nullable the API already emits the source of the
   * Schema_Detail_View's required/optional status, so a stated `false` has to
   * survive the one boundary it crosses or the view has nothing to read.
   */
  it('carries a producer-stated `nullable: false` through as `false`', async () => {
    const fields = await normalizedFields();
    expect(fields[INDEX_BY_NAME.order_id].nullable).toBe(false);
  });

  /**
   * **Validates: Requirements 5.2**
   *
   * ✅ Green since task 10.1. Same cause for the earlier red: the key was
   * dropped.
   *
   * `schemas.py:1365` and `:1458` write
   * `"nullable": not field_def.get("required", False)`. With no `required`
   * entry that is a real `true` — the schema *offers* the field. This is the one
   * state a `?? true` fallback would appear to get right, which is precisely why
   * it must be asserted separately from the absent case below: a green here and
   * a green there, with a `??` in between, would be two greens over one bug.
   */
  it('carries a producer-stated `nullable: true` through as `true`', async () => {
    const fields = await normalizedFields();
    expect(fields[INDEX_BY_NAME.order_date].nullable).toBe(true);
  });

  /**
   * **Validates: Requirements 5.3, 5.4**
   *
   * ✅ Green, and since task 10.1 green for the right reason. See the FALSE
   * GREEN note in the file docstring: it used to pass because the key was
   * dropped unconditionally, so no input could have failed it. Now the key is
   * carried, and the only thing making this read `undefined` is the ternary
   * declining to answer for a producer that said nothing.
   *
   * A producer that stated no nullability must reach the reader with no answer.
   * `:496` after task 8 emits no key for a Glue column; `:1569` after task 9
   * sets the key conditionally rather than defaulting it to `True`. R5.3 then
   * requires the view to render Not_Established in text and **not** "optional",
   * which it can only do if `undefined` arrives here instead of `true`.
   *
   * The second assertion is do-NOT 16 and do-NOT 5 in executable form, spelled
   * without a cast so it keeps holding after task 7.1 widens the type: whatever
   * else happens, an unstated `nullable` must **not** come back `true`.
   *
   * Do NOT fall back to `true` here. `true` and `undefined` are the distinction
   * R5.3 turns on, and collapsing them reports every Glue Data Catalog column as
   * "optional" on the strength of a value its source never supplied — the same
   * collapse `LGR-4854b3d-1` refused for `field_count`, one requirement away.
   */
  it('reports an unstated `nullable` as `undefined`, and never invents `true`', async () => {
    const fields = await normalizedFields();
    const region = fields[INDEX_BY_NAME.region];

    expect(region.nullable).toBeUndefined();
    expect(region).not.toHaveProperty('nullable', true);
  });
});
