// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p54 task 1.1 — the two arms `_unwrap` gained for a body it could not read.
 *
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
 *
 * Task 0.1's `api-client.unwrap-outcomes.characterization.test.ts` pins the
 * parse-succeeded arm and **deliberately asserts nothing about a Non_JSON_Body**,
 * because that is the one thing 1.1 changes. This file is the other half: the two
 * failure arms, by named example. Task 1.4's Property 3 sweeps status × body shape
 * generatively; these are the specific shapes R3 was written for, and they stand
 * whether or not 1.4 (optional) is implemented.
 *
 * Before 1.1, every case below rejected with `SyntaxError` — not an `ApiError`, so
 * invisible to all 19 non-test sites that discriminate with `err instanceof
 * ApiError`, reached from 83 call sites across 36 files (30 of them hooks). The
 * class is therefore asserted alongside the status in every test.
 *
 * **Expectations derive from R3.1–R3.4, not from current output** — the code was
 * written to these criteria in this same task.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, ApiError } from '../api-client';

/** A response whose body is not JSON at all — an edge/WAF HTML error page. */
const htmlResponse = (status: number): Response =>
  new Response('<html><body><h1>502 Bad Gateway</h1></body></html>', {
    status,
    headers: { 'Content-Type': 'text/html' },
  });

/** A response with no body at all. `.json()` throws on an empty body. */
const emptyResponse = (status: number): Response =>
  new Response(null, { status });

/** A factory, so each request gets a fresh `Response` rather than a used one. */
function stubFetch(make: () => Response) {
  const mockFetch = vi.fn(() => Promise.resolve(make()));
  vi.stubGlobal('fetch', mockFetch);
  return mockFetch;
}

describe('_unwrap: a body that could not be read as JSON (p54 1.1)', () => {
  let client: ApiClient;

  beforeEach(() => {
    vi.restoreAllMocks();
    // Fresh client per test — `isRedirecting` is per-instance one-shot state.
    client = new ApiClient('');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ── Arm 2: status >= 400 and an unreadable body (R3.1, R3.2) ───────────────

  describe('a status of 400 or above is classified by that status', () => {
    it('raises ApiError carrying 502 for an HTML error page, not SyntaxError', async () => {
      // The shape R3 was written for: CloudFront or the WAF answers with HTML.
      stubFetch(() => htmlResponse(502));

      const err = await client.get('/edge-html').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect(err).not.toBeInstanceOf(SyntaxError);
      expect((err as ApiError).status).toBe(502);
    });

    it('raises ApiError carrying 500 for an empty body', async () => {
      stubFetch(() => emptyResponse(500));

      const err = await client.get('/empty-500').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(500);
    });

    it('names the body as unreadable rather than inventing a server reason', async () => {
      // R3.2 says classify by the status; it does not license claiming the
      // handler gave a reason it did not give. `ApiError` lifts `body.error`
      // into `message`, which is what a toast shows.
      stubFetch(() => htmlResponse(400));

      const err = await client.get('/named-400').catch((e: unknown) => e);

      expect((err as ApiError).message).toMatch(/could not be read as JSON/i);
      expect((err as ApiError).message).toContain('400');
    });

    it('rejects on every wrapper, not only get', async () => {
      stubFetch(() => htmlResponse(503));

      for (const call of [
        () => client.get('/u-get'),
        () => client.post('/u-post', { a: 1 }),
        () => client.put('/u-put', { a: 1 }),
        () => client.delete('/u-delete'),
      ]) {
        const err = await call().catch((e: unknown) => e);
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(503);
      }
    });
  });

  // ── Arm 3: status below 400 and an unreadable body (R3.3) ──────────────────

  describe('a status below 400 raises an error naming the body as unparseable', () => {
    it('raises ApiError carrying the 2xx status for an HTML body on a 200', async () => {
      // A 200 whose body is HTML — e.g. the SPA's index.html served for an API
      // path that matched no route. Nothing about the status says "failure", so
      // the sentence is the only thing that says what went wrong.
      //
      // AMENDMENT — the cause named above has been removed at the edge, and this
      // assertion is retained deliberately rather than deleted. The dominant
      // producer of this exact shape was the Hosting distribution's
      // `errorResponses`, which mapped 403/404 to 200 + /index.html for the WHOLE
      // distribution, `/api/*` included. CloudFront custom error responses cannot
      // be scoped per behaviour, so a real `404 no test results yet`
      // (`handlers/pipelines_artifacts.py:89`) reached this method as HTML under a
      // 200 and could only be reported as an unreadable body. That mapping is gone
      // — the SPA fallback is now a viewer-request CloudFront Function on the
      // default (S3) behaviour only (`deploy-cdk/lib/spa-router-function.ts`,
      // pinned by `deploy-cdk/test/p56-spa-router-function.test.ts`).
      //
      // The arm this test covers stays correct and stays needed: `_unwrap` must
      // still classify a sub-400 response with an unreadable body honestly,
      // whatever produced it — a misconfigured proxy, a captive portal, a
      // truncated response. What changed is that the edge is no longer one of the
      // producers, so this should now be a rare shape rather than the routine one.
      stubFetch(() => htmlResponse(200));

      const err = await client.get('/spa-fallback').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(200);
      expect((err as ApiError).message).toMatch(/could not be read as JSON/i);
    });

    it('raises ApiError carrying 204 for a no-content response', async () => {
      // `.json()` throws on an empty body whatever the status. A 204 could not
      // have resolved before this change either (it threw SyntaxError), so
      // nothing depended on it resolving.
      stubFetch(() => emptyResponse(204));

      const err = await client.get('/no-content').catch((e: unknown) => e);

      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(204);
    });

    it('distinguishes the 2xx sentence from the >= 400 one', async () => {
      // The two arms are different facts: one is an HTTP error whose body was
      // also unreadable, the other is a success whose body was unreadable. The
      // status alone cannot tell an operator which, so the copy must differ.
      stubFetch(() => htmlResponse(200));
      const ok = (await client.get('/two-hundred').catch((e: unknown) => e)) as ApiError;

      vi.unstubAllGlobals();
      stubFetch(() => htmlResponse(500));
      const bad = (await client.get('/five-hundred').catch((e: unknown) => e)) as ApiError;

      expect(ok.message).not.toBe(bad.message);
    });
  });

  // ── R3.4: the one-shot 401 handler is not lost to the new arm ──────────────

  it('still triggers the one-shot unauthorized handler for a 401 with an unreadable body', async () => {
    // `requestRaw` fires the handler on `response.status === 401` (`:129`)
    // BEFORE `_unwrap` reads the body, so the redirect must survive a body that
    // never parses. Task 0.1 covers this for parseable 401 bodies only; an
    // expired session behind an edge that answers HTML is exactly the case where
    // losing the redirect would leave the operator stuck.
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);
    stubFetch(() => htmlResponse(401));

    const err = await client.get('/401-html').catch((e: unknown) => e);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
  });
});
