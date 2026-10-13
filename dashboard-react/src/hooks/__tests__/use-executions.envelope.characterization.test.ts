// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `PaginatedExecutions` (`use-executions.ts:89`) and
 * `usePipelineExecutions` (`:205`).
 *
 * Feature: p48-run-history-order-and-status-filter, task 1.2
 *
 * codegraph flags both symbols as having **no covering tests** — re-confirmed at
 * task 1.2: `PaginatedExecutions` 2 references (both in-file),
 * `usePipelineExecutions` 7 references across `ExecutionsPage.tsx`,
 * `PipelineOverviewPage.tsx`, `RunDetailPage.tsx` and `hooks/index.ts`. Those are
 * **reference counts, not call-site counts** (design §O7, p47 finding 61). The
 * reason the coverage flag is right despite six `ExecutionsPage` test files is
 * that every one of them replaces the hook wholesale — `vi.mock('../../hooks',
 * () => ({ usePipelineExecutions: () => ({ data: mockExecData }) }))` — so no
 * existing test has ever executed this module's `fetchPipelineExecutions`,
 * `useQuery` wiring, or read a value through `PaginatedExecutions`.
 *
 * ---
 *
 * ## ✅ EXPECTED-RED WINDOW — CLOSED by task 4.1. All five tests are green.
 *
 * Recorded here rather than hidden, per `LGR-60c4985-6` (**template /
 * ratified**): a deliberate reversal is documented *at the assertion* so a later
 * reader does not read the red as a defect and revert the only thing that would
 * have caught one. The window is kept in the file after closing for the same
 * reason — a reader who finds these two tests failing later should know they were
 * red on purpose once, and what closed them.
 *
 * | test | at task 1.2 | after task 4.1 |
 * |---|---|---|
 * | the declared pagination paths agree with the wire's | ❌ RED | ✅ green |
 * | every declared pagination path resolves to a value | ❌ RED | ✅ green |
 * | `executions` / `queued_files` stay top-level | ✅ green | ✅ green |
 * | the hook does not reshape the body | ✅ green | ✅ green |
 * | `page` reaches the request | ✅ green | ✅ green |
 *
 * Both reds were marked `it.fails`, so the suite stayed green **and** the window
 * was machine-checked in both directions: when task 4.1 landed C4 Arm B in
 * `use-executions.ts`, `it.fails` began failing *because the body passed*, which
 * forced the markers off rather than leaving stale ones behind. That is what
 * happened; the markers are gone and the two tests are ordinary `it`s.
 *
 * ---
 *
 * ## Expectations come from `requirements.md` R3, never from current output
 *
 * `LGR-28b4d75-5` (**template / ratified**): *"a test written from the
 * producer's output cannot detect the producer drifting, because the output is
 * its own oracle."* Current output **is** the defect here — R3.1 records that all
 * four of `total_count`, `page`, `page_size`, `has_more` read `undefined`. So a
 * faithful characterization of today (`expect(data.total_count).toBeUndefined()`)
 * would pin the bug and go red the moment task 4.1 fixed it. Nothing below
 * asserts an `undefined`.
 *
 * ## The frontend half of this defect is type-level, and that shapes the file
 *
 * Found while writing these tests, and it is worth stating because it is not
 * obvious from the requirement: `fetchPipelineExecutions` (`:192-203`) is a
 * **pass-through cast** — `apiClient.get<PaginatedExecutions>(...)` with no
 * mapping. Under C4 **Arm B** (keep the nested wire envelope, correct the
 * frontend type) the wire does not move, so the hook's *runtime* output is
 * byte-identical before and after task 4.1. A runtime value assertion therefore
 * cannot detect Arm B landing.
 *
 * The two formerly-red tests below consequently assert the **declaration** — parsed out of
 * `use-executions.ts` with the TypeScript compiler API — because that is where
 * the defect and the fix both live. The second one then *drives its runtime read
 * from the parsed declaration* rather than hardcoding either shape, so it flips
 * exactly when the declaration is corrected and cannot become its own oracle in
 * either direction.
 *
 * ## The wire fixture is the producer's shape, transcribed
 *
 * `PIPELINE_EXECUTIONS_BODY` is `get_pipeline_executions`' success envelope
 * (`handlers/pipelines_runs.py`): `executions` and `queued_files` at the top
 * level, **eight** pagination facts nested under `pagination`. The *values* are
 * post-task-3.3 (`total_count` 23 > `page_size` 10, `total_pages` derived to 3),
 * because R2.2 and R2.3 are what make R2.6's pager renderable and a fixture
 * carrying today's `len(executions)` / literal `1` could not express that.
 *
 * The count has moved twice and both moves were forced by the same reasoning, so
 * it is stated rather than left to be recounted: the two tests below compare the
 * interface's declared paths against **this fixture**, so a fixture short of what
 * the producer emits would green-light an interface that still could not read the
 * wire. It was **six** when written at task 1.2; task 3.2 added `next_cursor` when
 * C2 Arm A landed; task 4.1 added `partial_counts`, which task 3.3 had put on
 * *both* producer arms (`pipelines_runs.py:800`, `:842`) without reaching this
 * file.
 *
 * ## `computeExecutionsRefetchInterval` is read, never touched
 *
 * `LGR-e800696-1` (**template / ratified**) scopes itself to
 * `EXECUTIONS_POLL_MATRIX`, that function's *signature*, and
 * `usePipelineExecutions`' `refetchInterval` callback, and its `connected`
 * parameter is deliberately required-not-defaulted. Its own test file is
 * `use-executions.polling.test.ts`; this is a separate file and changes none of
 * that. Rendering the hook with no `WebSocketProvider` yields `connected: false`
 * (the context default is a disconnected no-op, `WebSocketProvider.tsx:131-134`),
 * so the callback returns a positive cadence — 8 s or 30 s, neither of which
 * elapses inside these tests.
 *
 * _Requirements: R3.1 — Design: O4, C4_
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import ts from 'typescript';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import type { ReactNode } from 'react';

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

import { usePipelineExecutions } from '../use-executions';

/* ------------------------------------------------------------------ */
/*  The wire fixture — the producer's envelope, values post-3.3.      */
/* ------------------------------------------------------------------ */

const PIPELINE_KEY = 'latesttesting-19ca826a';

/**
 * `GET /pipeline/{key}/executions` as `get_pipeline_executions` builds it
 * (`pipelines_runs.py:425-437`).
 *
 * Two rows, deliberately a `skipped-` and a `run-`, because that pair is the
 * operator's report (requirements §Introduction) and it keeps the fixture
 * recognisable as run history rather than as an anonymous list. Ordering is
 * task 2.2's subject, not this file's — nothing here asserts row order.
 */
const PIPELINE_EXECUTIONS_BODY = {
  executions: [
    {
      execution_id: 'run-2026-08-24-13-04-01-1a2b3c4d',
      run_id: 'run-2026-08-24-13-04-01-1a2b3c4d',
      status: 'RUNNING',
      started_at: '2026-08-24T13:04:01+00:00',
      file_name: 'customer_data_1.csv',
    },
    {
      execution_id: 'skipped-7412636fccb938719673179658c05773',
      run_id: 'skipped-7412636fccb938719673179658c05773',
      status: 'SKIPPED',
      started_at: '2026-08-24T13:04:04+00:00',
      file_name: 'customer_data_3.csv',
      skip_reason: 'duplicate_content',
    },
  ],
  queued_files: [
    {
      file_name: 'customer_data_6.csv',
      source_key: 'uploads/customer_data_6.csv',
      queued_at: '2026-08-24T13:27:46+00:00',
      status: 'QUEUED',
    },
  ],
  pagination: {
    page: 2,
    page_size: 10,
    // R2.2 — describes the set being paged, so it exceeds `page_size`. Today the
    // producer writes `len(executions)`, which cannot; task 3.3 fixes that.
    total_count: 23,
    // R2.3 — derived from the count, not the literal `1` the producer writes now.
    total_pages: 3,
    has_next: true,
    has_prev: true,
    // Task 3.2 (C2 **Arm A**, chosen by the user at 3.1): keyed pagination. The
    // producer now emits `next_cursor` as a seventh pagination fact — an opaque
    // base64url token, or `null` on the last page, ALWAYS present so the envelope's
    // key set stays assertable with `==`.
    //
    // Added to this fixture by task 3.2 rather than left to 4.1, because the whole
    // design of the tests below is that the fixture is *the producer's shape*: the
    // red test compares the declared paths against `WIRE_PAGINATION_PATHS`, so a
    // fixture missing a field the producer emits would give 4.1 a green light for
    // an interface that still could not read the wire. Seven paths at 3.2; eight
    // once 4.1 added `partial_counts` below, for the same reason.
    //
    // The value is a real cursor rather than `null` because this fixture is page 2
    // of 3 — `has_next: true` above — and a `null` here would make "every declared
    // path resolves to a value" pass for the wrong reason on the one field whose
    // absence 5.1's pager depends on.
    // Not invented: produced by `handlers.run_cursor.encode_cursor` from the
    // `LastEvaluatedKey` a live `RunsByTimeIndex` query returned for this pipeline
    // (the validation account, read-only), so it is 199 characters of real cursor rather than a
    // placeholder that would understate the URL length a client has to carry.
    next_cursor:
      'eyJwYWdlIjozLCJwaXBlbGluZV9pZCI6ImxhdGVzdHRlc3RpbmctMTljYTgyNmEiLCJydW5faWQiOiJza2lwcGVkLTc0MTI2MzZmY2NiOTM4NzE5NjczMTc5NjU4YzA1NzczIiwic3RhcnRlZF9hdCI6IjIwMjYtMDgtMjRUMTM6MDQ6MDQuODY0Nzg0KzAwOjAwIn0',
    // Task 4.1, and it is the producer catching up with this fixture rather than a
    // new idea: task 3.3 emits `partial_counts` on BOTH arms
    // (`pipelines_runs.py:800`, `:842`) and did not add it here, so the two tests
    // below were comparing seven declared paths against an eight-key wire. Left
    // out, the first test would have passed over an interface that could not read
    // the whole envelope — the exact green light the `next_cursor` note above
    // exists to prevent.
    //
    // `[]` rather than `["total_count", "total_pages"]` because that is the value
    // consistent with the rest of this block: `total_count: 23` is an exact count
    // and `total_pages: 3` derives from it, which is the complete-count path, and
    // `[]` is what the producer writes there. It does not weaken the "resolves to a
    // defined value" test — `[]` is a value; only an absent key is not.
    partial_counts: [],
  },
} as const;

/** Every leaf path the wire carries under `pagination`. */
const WIRE_PAGINATION_PATHS = Object.keys(PIPELINE_EXECUTIONS_BODY.pagination)
  .map((k) => `pagination.${k}`)
  .sort();

/**
 * Every name that is a pagination fact rather than a payload array, in either
 * spelling — the flat four the interface declares today plus the four the producer
 * additionally emits. Used to separate the pagination block from
 * `executions` / `queued_files` without assuming which shape is in force.
 *
 * `next_cursor` (task 3.2) and `partial_counts` (task 4.1) are in the list so the
 * set stays complete even for a declaration that spells them flat. Omitting either
 * would make the filter below silently drop a declared field, and the first test
 * would then compare too few declared paths against the wire's and report the
 * wrong difference.
 *
 * `has_more` stays in the list although task 4.1 **removed** it from the
 * interface. It is not vestigial: this set is what tells the filter "a field by
 * this name is a pagination fact, not a payload array", so if `has_more` ever
 * comes back — flat, or as an alias inside `pagination` — the first test must see
 * it and fail, because the producer emits `has_next`/`has_prev` and never
 * `has_more`. Dropping it from here would make that reappearance invisible.
 */
const PAGINATION_FACT_NAMES = new Set([
  'total_count',
  'page',
  'page_size',
  'has_more',
  'total_pages',
  'has_next',
  'has_prev',
  'next_cursor',
  'partial_counts',
]);

/* ------------------------------------------------------------------ */
/*  Reading the declaration.                                          */
/* ------------------------------------------------------------------ */

/**
 * Every leaf path `PaginatedExecutions` declares, one level of nesting deep.
 *
 * Parsed rather than imported: the interface is module-private, and exporting it
 * is a change to `use-executions.ts`, which task 4.1 owns and this task must not
 * touch. Parsing is also the only way to observe the *declaration* at all —
 * interfaces are erased at runtime, and a `tsc`-level assertion would fail the
 * whole `make test-frontend` gate rather than reporting one red test.
 *
 * The TypeScript compiler API is not a new mechanism here:
 * `components/__tests__/MappingViewer.unsourced-target.test.tsx:346-377` already
 * walks a source file this way, including the `fileURLToPath(import.meta.url)` +
 * `node:path` resolution that file records as necessary (under jsdom the global
 * `URL` resolves a relative specifier against the document origin, and
 * `fileURLToPath` then throws `ERR_INVALID_URL_SCHEME`).
 */
function declaredLeafPaths(): string[] {
  const here = dirname(fileURLToPath(import.meta.url));
  const label = 'hooks/use-executions.ts';
  const source = readFileSync(resolve(here, '../use-executions.ts'), 'utf-8');
  const sf = ts.createSourceFile(label, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);

  const memberName = (m: ts.TypeElement): string | undefined =>
    ts.isPropertySignature(m) && m.name && ts.isIdentifier(m.name) ? m.name.text : undefined;

  /** Every object-shaped declaration in the file, by name. */
  const shapes = new Map<string, ts.NodeArray<ts.TypeElement>>();
  const collectShapes = (node: ts.Node): void => {
    if (ts.isInterfaceDeclaration(node)) {
      shapes.set(node.name.text, node.members);
    } else if (ts.isTypeAliasDeclaration(node) && ts.isTypeLiteralNode(node.type)) {
      shapes.set(node.name.text, node.type.members);
    }
    ts.forEachChild(node, collectShapes);
  };
  ts.forEachChild(sf, collectShapes);

  /**
   * The members of a property's type when that type is an object shape —
   * whether written inline as a type literal or referenced by name.
   *
   * Both spellings are accepted because task 4.1 may reasonably write Arm B
   * either way, and which one it picks is a style choice this test has no
   * business constraining. Resolution is one level deep and same-file only:
   * enough for `pagination`, and short of reimplementing the type checker.
   */
  const nestedMembers = (type: ts.TypeNode | undefined) => {
    if (!type) return undefined;
    if (ts.isTypeLiteralNode(type)) return type.members;
    if (ts.isTypeReferenceNode(type) && ts.isIdentifier(type.typeName)) {
      return shapes.get(type.typeName.text);
    }
    return undefined;
  };

  const members = shapes.get('PaginatedExecutions');
  if (!members) {
    throw new Error(
      `Could not find an interface named PaginatedExecutions in ${label}. ` +
        'If it was renamed, this test must be updated deliberately, not deleted.',
    );
  }

  const paths: string[] = [];
  for (const member of members) {
    const name = memberName(member);
    if (!name) continue;
    const nested = nestedMembers(ts.isPropertySignature(member) ? member.type : undefined);
    if (nested) {
      // A nested object shape — record its members as `parent.child`, which is
      // how Arm B spells the pagination block.
      for (const child of nested) {
        const childName = memberName(child);
        if (childName) paths.push(`${name}.${childName}`);
      }
    } else {
      paths.push(name);
    }
  }
  return paths.sort();
}

/** The pagination leaf paths `PaginatedExecutions` declares, in either shape. */
function declaredPaginationPaths(): string[] {
  return declaredLeafPaths().filter(
    (p) => p.startsWith('pagination.') || PAGINATION_FACT_NAMES.has(p),
  );
}

/** Resolve a dotted path against a value, without asserting anything about it. */
function resolvePath(root: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc === null || typeof acc !== 'object') return undefined;
    return (acc as Record<string, unknown>)[key];
  }, root);
}

/* ------------------------------------------------------------------ */
/*  Harness.                                                          */
/* ------------------------------------------------------------------ */

/**
 * Local wrapper, matching the sibling
 * `use-schemas.nullable.characterization.test.ts:createWrapper` and every other
 * hook test in this directory. There is no shared React Query harness in this
 * repo to reuse — checked; each test file declares its own — so this follows the
 * incumbent shape rather than introducing a second, divergent one, which is the
 * class of mistake `LGR-99050e6-5` (**template / ratified**) fences on the
 * Python side.
 *
 * No `WebSocketProvider`: `useWebSocketContext` returns a disconnected no-op
 * outside one by design (`WebSocketProvider.tsx:615-618`), which is all
 * `usePipelineExecutions` needs from it here.
 */
function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

/** Render the real hook against the real envelope and hand back its `data`. */
async function fetchedData(page?: number): Promise<unknown> {
  const { result } = renderHook(() => usePipelineExecutions(PIPELINE_KEY, page), {
    wrapper: createWrapper(),
  });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  return result.current.data;
}

beforeEach(() => {
  getMock.mockReset();
  // A fresh deep copy per call. Handing back the fixture object itself would
  // make the pass-through assertion below compare a reference with itself, which
  // no reshape could fail.
  getMock.mockImplementation(() =>
    Promise.resolve(JSON.parse(JSON.stringify(PIPELINE_EXECUTIONS_BODY))),
  );
});

/* ------------------------------------------------------------------ */
/*  R3.1 — the declaration and the wire must agree.                   */
/* ------------------------------------------------------------------ */

describe('Feature: p48-run-history-order-and-status-filter, task 1.2: PaginatedExecutions agrees with the wire', () => {
  /**
   * **Validates: Requirements 3.1**
   *
   * ✅ Green since task 4.1. Was `it.fails` until then; the marker came off when
   * the body started passing, which is the window closing.
   *
   * R3.1: *"The response envelope and the frontend's declared type MUST
   * agree."* Stated as a set comparison over leaf paths, in both directions,
   * because R3.1 has two halves and each failed on its own at task 1.2:
   *
   *  - every pagination fact the producer emits must be declared — none of the
   *    `pagination.*` paths appeared in the interface at all; and
   *  - the interface must declare no pagination path the producer never emits —
   *    it declared flat `total_count`, `page`, `page_size` and `has_more`, and
   *    `has_more` is not even the backend's spelling (`has_next` / `has_prev`
   *    are). That mismatch is what task 4.1 had to *"reconcile explicitly"*, and
   *    it did so by **removing** `has_more` rather than aliasing it: there were no
   *    app-code readers, and an alias is a second spelling of a fact the producer
   *    does not send.
   *
   * Derived from the requirement rather than from either artifact: the expected
   * set is computed from the fixture transcribed off
   * `pipelines_runs.py:778-801`, and the actual set is parsed out of
   * `use-executions.ts`. Neither side is the other's oracle
   * (`LGR-28b4d75-5`, design §P4).
   *
   * Do NOT satisfy this by flattening the producer. That is C4 **Arm A**, and
   * task 1.1 rejected it on measured grounds: three test files already assert the
   * nested shape (`test_executions_response_contract.py`,
   * `contracts/api_responses/schemas.py`'s `PAGINATED_EXECUTIONS_SCHEMA`,
   * `test_api_contract_props.py`), so flattening breaks all three to match the
   * newer, never-exercised artifact, and discards `total_pages` and `has_prev`,
   * which task 3.3 had just made meaningful. The pass-through fence below is what
   * keeps Arm A from creeping back in client-side.
   */
  it('declares exactly the pagination leaf paths the producer emits', () => {
    expect(declaredPaginationPaths()).toEqual(WIRE_PAGINATION_PATHS);
  });

  /**
   * **Validates: Requirements 3.1, 3.3**
   *
   * ✅ Green today, and it must stay green.
   *
   * Design §Data Models: *"Both `executions` and `queued_files` sit at the top
   * level in both shapes and are read correctly today. Only the pagination block
   * diverges."* This is the half of the envelope that is **not** broken, pinned
   * so task 4.1 cannot move it while fixing the half that is — and R3.3 depends
   * on it directly, since `get_queued_files` (`pipelines_runs.py:458-470`) reads
   * `queued_files` off this same body and must keep working (do-NOT 3).
   */
  it('keeps `executions` and `queued_files` as top-level declared members', () => {
    const declared = declaredLeafPaths();

    expect(declared).toContain('executions');
    expect(declared).toContain('queued_files');
  });
});

/* ------------------------------------------------------------------ */
/*  R3.1 — and the declared read must actually resolve.               */
/* ------------------------------------------------------------------ */

describe('Feature: p48-run-history-order-and-status-filter, task 1.2: usePipelineExecutions yields real pagination values', () => {
  /**
   * **Validates: Requirements 3.1, 2.2, 2.3**
   *
   * ✅ Green since task 4.1. Was `it.fails` until then.
   *
   * The requirement in its most direct form: a consumer that follows the
   * declared type to a pagination fact must arrive at a value. R3.1 records that
   * *"every one of those four reads `undefined` today"* — so this test resolves
   * each path the interface declares against the response the hook returns and
   * requires every one to be defined.
   *
   * **The read location is derived from the declaration, not hardcoded.** That
   * is the whole design of this test, and it is why the flip needed no edit here.
   * At task 1.2 the declaration said `total_count | page | page_size | has_more`
   * at the top level, and resolving those against a nested envelope gave four
   * `undefined`s — red. Task 4.1 changed the declaration to `pagination.*`, the
   * same code resolved those instead, and they are the wire's own values — green.
   * Hardcoding either spelling would have made the test its own oracle in one
   * direction or broken `tsc` in the other, and asserting the `undefined`
   * directly would have pinned the defect.
   *
   * The last two assertions are R2.2 and R2.3 as the pager needs them
   * (`ExecutionsPage.tsx` gates on
   * `Math.ceil((data.pagination?.total_count ?? 0) / 10) > 1` — task 4.1
   * repointed that read; task 5.1 owns the rest of it): the count must be a
   * number that exceeds one page, and `total_pages` must agree with it. Both were
   * `undefined` at task 1.2, so both were part of the same red.
   */
  it('resolves every declared pagination path to a defined value', async () => {
    const data = await fetchedData(2);
    const declaredPagination = declaredPaginationPaths();

    // Guard: if the interface declared no pagination fact at all, "every one is
    // defined" would be vacuously true.
    expect(declaredPagination.length).toBeGreaterThan(0);

    const unresolved = declaredPagination.filter(
      (p) => resolvePath(data, p) === undefined,
    );
    expect(unresolved).toEqual([]);

    // R2.2 / R2.3 — the two facts R2.6's pager is computed from. Located by the
    // declared path's own leaf name, so this follows the declaration too.
    const readLeaf = (leaf: string) =>
      declaredPagination
        .filter((p) => p === leaf || p.endsWith(`.${leaf}`))
        .map((p) => resolvePath(data, p))
        .find((v) => v !== undefined);
    const totalCount = readLeaf('total_count');
    const totalPages = readLeaf('total_pages');

    expect(typeof totalCount).toBe('number');
    expect(totalCount as number).toBeGreaterThan(
      PIPELINE_EXECUTIONS_BODY.pagination.page_size,
    );
    expect(totalPages).toBe(
      Math.ceil(
        PIPELINE_EXECUTIONS_BODY.pagination.total_count /
          PIPELINE_EXECUTIONS_BODY.pagination.page_size,
      ),
    );
  });

  /**
   * **Validates: Requirements 3.1**
   *
   * ✅ Green today, and load-bearing rather than incidental.
   *
   * `fetchPipelineExecutions` (`:192-203`) is a pass-through cast — it types the
   * body and reshapes nothing. This pins that, which is what makes C4 **Arm B**
   * enforceable from the frontend side: the honest fix corrects the *type*, and
   * a client-side flatten in this function would satisfy the red test above
   * while quietly making the frontend a second authority on the envelope's
   * shape. R10.4 forbids that pattern for ordering; the same argument applies
   * here.
   *
   * It is also the reason the two reds are assertions about the declaration:
   * because this pass-through holds, the hook's *runtime* output is identical
   * before and after task 4.1 under Arm B, so no runtime value assertion could
   * detect the fix landing.
   */
  it('returns the producer body unchanged, adding and renaming nothing', async () => {
    const data = await fetchedData(2);

    expect(data).toEqual(PIPELINE_EXECUTIONS_BODY);
  });

  /**
   * **Validates: Requirements 2.1**
   *
   * ✅ Green today. The client half of R2.1, asserted so that the server half
   * has a witness: `page` does reach the query string, which is why R2.1's
   * defect is precisely that `get_pipeline_executions` accepts the parameter and
   * *"references [it] nowhere in the body"*.
   *
   * `page_size` is asserted as sent rather than as a particular number. The
   * literal `10` is duplicated across `use-executions.ts:198`,
   * `ExecutionsPage.tsx:252` and `:255`, and task 5.1 is what derives it from
   * one place — pinning the value here would make that task edit this file for
   * no gain.
   */
  it('puts the requested page on the request, and defaults it to 1', async () => {
    await fetchedData(2);
    expect(getMock).toHaveBeenLastCalledWith(
      expect.stringContaining(`/pipeline/${PIPELINE_KEY}/executions?`),
    );
    const withPageTwo = String(getMock.mock.lastCall?.[0]);
    expect(new URL(withPageTwo, 'https://example.invalid').searchParams.get('page')).toBe(
      '2',
    );
    expect(
      new URL(withPageTwo, 'https://example.invalid').searchParams.get('page_size'),
    ).not.toBeNull();

    getMock.mockClear();
    await fetchedData();
    const withDefault = String(getMock.mock.lastCall?.[0]);
    expect(new URL(withDefault, 'https://example.invalid').searchParams.get('page')).toBe(
      '1',
    );
  });
});
