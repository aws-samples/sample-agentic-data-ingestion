// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 0.1 — characterization of `_unwrap`'s five current outcomes.
 *
 * **Validates: Requirements 3.4**
 *
 * `_unwrap` (`services/api-client.ts:149`) is a chokepoint: **83 non-test call
 * sites across 36 files reach it through `get`/`post`/`put`/`delete`, 30 of those
 * files being hooks**, and `apiClient.get` alone accounts for 48 call sites in 25
 * hook files. Task 1.1 moves the body read inside a `try` and makes the status the
 * classifier. R3.4 is the clause that constrains that change:
 *
 * > THE API client SHALL continue to unwrap a Lambda proxy envelope and to trigger
 * > the one-shot unauthorized handler for both a real 401 and a wrapped
 * > `statusCode: 401`.
 *
 * This file is the *showing* that 1.1 does not alter what those callers rely on.
 * It must pass **unmodified before and after** task 1.1.
 *
 * **Expectations here derive from R3.4 and from design §4.3's "parse succeeded →
 * the existing code path, unchanged" arm — not from reading current output.** The
 * five outcomes design §4.3 enumerates for that arm, in its order: envelope
 * detection, the wrapped-401 one-shot redirect, `raw.statusCode >= 400` →
 * `ApiError`, `!res.ok` → `ApiError`, else the body.
 *
 * **Deliberately absent: any assertion about a Non_JSON_Body.** That is the one
 * thing 1.1 changes (`SyntaxError` propagating today, `ApiError` after), so
 * pinning it here would make this file fail after the change it exists to
 * protect. R3.1–R3.3 are covered by task 1.4's Property 3, and the
 * `fetchTestResults` half by task 0.2.
 *
 * **`ApiError` is the discriminator the callers use** (`err instanceof ApiError`,
 * then `err.status`), so every rejection assertion below checks the class *and*
 * the status *and* `body`, which is what `use-code.ts:56` and
 * `utils/schema-delete-failure.ts:43-46` actually read.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, ApiError } from '../api-client';

/** A real `Response`, so `clone()` and `json()` behave as they do in the browser. */
const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

/**
 * Stub `fetch` with a **factory**, so each call gets a fresh `Response`. A single
 * shared `Response` would let one test's body read affect the next request's.
 */
function stubFetch(make: () => Response) {
  const mockFetch = vi.fn(() => Promise.resolve(make()));
  vi.stubGlobal('fetch', mockFetch);
  return mockFetch;
}

describe('_unwrap characterization (p54 0.1)', () => {
  let client: ApiClient;

  beforeEach(() => {
    vi.restoreAllMocks();
    // A fresh client per test: `isRedirecting` is per-instance one-shot state, so
    // a shared client would make "fires exactly once" pass for the wrong reason.
    client = new ApiClient('');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ── Outcome 1 — a Lambda proxy envelope is unwrapped ────────────────────────

  describe('outcome 1: a Lambda proxy envelope resolves to its inner body', () => {
    it('unwraps an envelope whose body is a JSON string', async () => {
      stubFetch(() =>
        jsonResponse({ statusCode: 200, body: JSON.stringify({ runs: [1, 2] }) }),
      );

      await expect(client.get('/envelope-string')).resolves.toEqual({ runs: [1, 2] });
    });

    it('unwraps an envelope whose body is already an object', async () => {
      stubFetch(() => jsonResponse({ statusCode: 200, body: { runs: ['a'] } }));

      await expect(client.get('/envelope-object')).resolves.toEqual({ runs: ['a'] });
    });

    it('unwraps the envelope on every wrapper, not only get', async () => {
      stubFetch(() =>
        jsonResponse({ statusCode: 200, body: JSON.stringify({ ok: true }) }),
      );

      // All four wrappers delegate to `_unwrap`; the callers reach it through
      // whichever one their hook uses, so all four are part of the contract.
      await expect(client.get('/w-get')).resolves.toEqual({ ok: true });
      await expect(client.post('/w-post', { a: 1 })).resolves.toEqual({ ok: true });
      await expect(client.put('/w-put', { a: 1 })).resolves.toEqual({ ok: true });
      await expect(client.delete('/w-delete')).resolves.toEqual({ ok: true });
    });

    it('treats a 2xx envelope carrying statusCode 201 as an envelope', async () => {
      // Envelope detection is by the presence of `statusCode`, not by its value —
      // a created-resource envelope must still be unwrapped rather than returned
      // whole. `use-pipeline-mutations.ts` depends on this.
      stubFetch(() =>
        jsonResponse({ statusCode: 201, body: JSON.stringify({ pipeline_id: 'p-1' }) }),
      );

      await expect(client.post('/created', {})).resolves.toEqual({ pipeline_id: 'p-1' });
    });
  });

  // ── Outcome 2 — a plain JSON body passes through ────────────────────────────

  describe('outcome 2: a plain JSON body (no envelope) is returned as-is', () => {
    it('returns an object body unchanged', async () => {
      stubFetch(() => jsonResponse({ items: [], total: 0 }));

      await expect(client.get('/plain-object')).resolves.toEqual({ items: [], total: 0 });
    });

    it('returns an array body unchanged', async () => {
      // An array has no `statusCode`, so it is not an envelope. Several hooks
      // (approvals, notifications) receive a bare array and would break if the
      // envelope branch claimed it.
      stubFetch(() => jsonResponse([{ id: 'a' }, { id: 'b' }]));

      await expect(client.get('/plain-array')).resolves.toEqual([{ id: 'a' }, { id: 'b' }]);
    });

    it('does not invoke the unauthorized handler for a healthy plain response', async () => {
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() => jsonResponse({ ok: true }));

      await client.get('/healthy');

      expect(onUnauthorized).not.toHaveBeenCalled();
    });
  });

  // ── Outcome 3 — a real HTTP 401 ─────────────────────────────────────────────

  describe('outcome 3: a real HTTP 401', () => {
    it('rejects with ApiError carrying status 401 and the parsed body', async () => {
      stubFetch(() => jsonResponse({ message: 'Unauthorized' }, 401));

      const err = await client.get('/real-401').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(401);
      expect((err as ApiError).body).toEqual({ message: 'Unauthorized' });
    });

    it('triggers the unauthorized handler', async () => {
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() => jsonResponse({ message: 'Unauthorized' }, 401));

      await client.get('/real-401-handler').catch(() => undefined);

      expect(onUnauthorized).toHaveBeenCalled();
    });
  });

  // ── Outcome 4 — a wrapped `statusCode: 401` ─────────────────────────────────

  describe('outcome 4: a wrapped statusCode 401 inside an HTTP 200 envelope', () => {
    it('rejects with ApiError carrying status 401 and the inner body', async () => {
      // HTTP 200 on the wire — `requestRaw`'s `response.status === 401` check
      // cannot see this one, so the envelope branch is the only thing that does.
      stubFetch(() =>
        jsonResponse({ statusCode: 401, body: JSON.stringify({ error: 'expired' }) }, 200),
      );

      const err = await client.get('/wrapped-401').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(401);
      expect((err as ApiError).body).toEqual({ error: 'expired' });
    });

    it('triggers the unauthorized handler', async () => {
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() =>
        jsonResponse({ statusCode: 401, body: JSON.stringify({ error: 'expired' }) }, 200),
      );

      await client.get('/wrapped-401-handler').catch(() => undefined);

      expect(onUnauthorized).toHaveBeenCalled();
    });
  });

  // ── Outcome 5 — `>= 400` in both shapes ─────────────────────────────────────

  describe('outcome 5: a status of 400 or above, in both shapes', () => {
    it('classifies by the real HTTP status when the body is plain JSON', async () => {
      stubFetch(() => jsonResponse({ message: 'Internal server error' }, 500));

      const err = await client.get('/real-500').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(500);
      expect((err as ApiError).body).toEqual({ message: 'Internal server error' });
      // The message is derived from the body so a toast shows the server's reason.
      expect((err as ApiError).message).toBe('Internal server error');
    });

    it('classifies by the wrapped raw.statusCode when the envelope carries it', async () => {
      // HTTP 200 on the wire, 404 in the envelope: `res.ok` is true, so only
      // `raw.statusCode >= 400` can classify this.
      stubFetch(() =>
        jsonResponse({ statusCode: 404, body: JSON.stringify({ error: 'not found' }) }, 200),
      );

      const err = await client.get('/wrapped-404').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(404);
      expect((err as ApiError).body).toEqual({ error: 'not found' });
      expect((err as ApiError).message).toBe('not found');
    });

    it('classifies by the wrapped statusCode even when the two shapes disagree', async () => {
      // A 500 envelope delivered over an HTTP 502. The envelope's status is the
      // one the handler chose, and it is what the callers discriminate on.
      stubFetch(() =>
        jsonResponse({ statusCode: 500, body: JSON.stringify({ error: 'handler blew up' }) }, 502),
      );

      const err = await client.get('/disagree').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(500);
    });

    it('rejects on every wrapper, not only get', async () => {
      stubFetch(() => jsonResponse({ error: 'forbidden' }, 403));

      for (const call of [
        () => client.get('/e-get'),
        () => client.post('/e-post', { a: 1 }),
        () => client.put('/e-put', { a: 1 }),
        () => client.delete('/e-delete'),
      ]) {
        const err = await call().catch((e: unknown) => e);
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(403);
      }
    });
  });

  // ── R3.4's one-shot clause, stated per response ─────────────────────────────

  describe('the one-shot unauthorized handler fires exactly once per response', () => {
    it('fires exactly once for a single real 401', async () => {
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() => jsonResponse({ message: 'Unauthorized' }, 401));

      await client.get('/once-real').catch(() => undefined);

      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });

    it('fires exactly once for a single wrapped 401', async () => {
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() =>
        jsonResponse({ statusCode: 401, body: JSON.stringify({ error: 'expired' }) }, 200),
      );

      await client.get('/once-wrapped').catch(() => undefined);

      expect(onUnauthorized).toHaveBeenCalledTimes(1);
    });

    it('fires exactly once when ONE response carries both 401 shapes at once', async () => {
      // The case that makes "per response" a real assertion rather than a
      // restatement of the cross-request guard: an HTTP 401 whose body is *also*
      // a wrapped `statusCode: 401` envelope reaches both trigger sites —
      // `requestRaw` (`api-client.ts:129`) and `_unwrap` (`:160`) — for one
      // response. `isRedirecting` is what collapses the two into one redirect.
      const onUnauthorized = vi.fn();
      client.setUnauthorizedHandler(onUnauthorized);
      stubFetch(() =>
        jsonResponse({ statusCode: 401, body: JSON.stringify({ error: 'expired' }) }, 401),
      );

      const err = await client.get('/once-both').catch((e: unknown) => e);

      expect(onUnauthorized).toHaveBeenCalledTimes(1);
      // The envelope branch still owns the classification.
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(401);
    });

    it('does not throw when no unauthorized handler has been registered', async () => {
      // `onUnauthorized` is null until the auth layer wires it at startup, and a
      // 401 arriving before that must still reject with ApiError rather than a
      // TypeError. Every hook mounted before wiring depends on this.
      stubFetch(() => jsonResponse({ message: 'Unauthorized' }, 401));

      const err = await client.get('/no-handler').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(401);
    });
  });
});
