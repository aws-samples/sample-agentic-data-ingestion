// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * P29 R10 — API client correctness.
 *
 * Validates: Requirements 10.1, 10.2, 10.3
 *
 * R10.1 Request deduplication applies to GETs only — the method:URL dedup key
 *       must no longer collapse distinct POST/PUT/DELETE bodies into one
 *       in-flight response (mutations always dispatch).
 * R10.2 On a 401 response the client triggers the unauthorized handler
 *       (signinRedirect) exactly once — not once per failed request.
 * R10.3 Covered by the tests below: two POSTs with different bodies both sent;
 *       a 401 triggers redirect once (no toast storm).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiClient, ApiError } from '../services/api-client';

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });

describe('ApiClient — mutations are never deduplicated (R10.1)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('sends two POSTs with different bodies (both dispatched, not deduped)', async () => {
    const mockFetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', mockFetch);

    const client = new ApiClient('/api');
    // Fire both before awaiting so they overlap the in-flight window.
    const p1 = client.post('/things', { name: 'alpha' });
    const p2 = client.post('/things', { name: 'beta' });
    await Promise.all([p1, p2]);

    expect(mockFetch).toHaveBeenCalledTimes(2);
    const bodies = mockFetch.mock.calls.map((c) => c[1].body);
    expect(bodies).toContain(JSON.stringify({ name: 'alpha' }));
    expect(bodies).toContain(JSON.stringify({ name: 'beta' }));
    // Mutations never enter the in-flight map.
    expect(client.inflightSize).toBe(0);
  });

  it('sends two identical POSTs (mutations always dispatch)', async () => {
    const mockFetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', mockFetch);

    const client = new ApiClient('/api');
    await Promise.all([
      client.post('/things', { name: 'same' }),
      client.post('/things', { name: 'same' }),
    ]);

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it('does not deduplicate PUT or DELETE against each other', async () => {
    const mockFetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', mockFetch);

    const client = new ApiClient('/api');
    await Promise.all([
      client.put('/things/1', { v: 1 }),
      client.put('/things/1', { v: 2 }),
      client.delete('/things/1'),
      client.delete('/things/1'),
    ]);

    expect(mockFetch).toHaveBeenCalledTimes(4);
    expect(client.inflightSize).toBe(0);
  });

  it('still deduplicates concurrent identical GETs (single fetch)', async () => {
    let resolveFetch: ((r: Response) => void) | null = null;
    const mockFetch = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal('fetch', mockFetch);

    const client = new ApiClient('/api');
    const p1 = client.requestRaw('/list');
    const p2 = client.requestRaw('/list');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    expect(client.inflightSize).toBe(1);

    resolveFetch!(jsonResponse({ ok: true }));
    const [r1, r2] = await Promise.all([p1, p2]);
    expect(r1).toBe(r2);
    expect(client.inflightSize).toBe(0);
  });
});

describe('ApiClient — 401 triggers a single signinRedirect (R10.2/R10.3)', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('calls the unauthorized handler exactly once across a storm of 401s', async () => {
    const mockFetch = vi
      .fn()
      .mockResolvedValue(jsonResponse({ message: 'Unauthorized' }, 401));
    vi.stubGlobal('fetch', mockFetch);

    const onUnauthorized = vi.fn();
    const client = new ApiClient('/api');
    client.setUnauthorizedHandler(onUnauthorized);

    // Several concurrent requests to distinct URLs all fail with 401.
    const results = await Promise.allSettled([
      client.get('/a'),
      client.get('/b'),
      client.post('/c', { x: 1 }),
      client.put('/d', { y: 2 }),
      client.delete('/e'),
    ]);

    // Every request rejects with an ApiError(401)...
    expect(results.every((r) => r.status === 'rejected')).toBe(true);
    for (const r of results) {
      if (r.status === 'rejected') {
        expect(r.reason).toBeInstanceOf(ApiError);
        expect((r.reason as ApiError).status).toBe(401);
      }
    }
    // ...but the redirect fires exactly once (no toast/redirect storm).
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('does not trigger the handler when responses are healthy', async () => {
    const mockFetch = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal('fetch', mockFetch);

    const onUnauthorized = vi.fn();
    const client = new ApiClient('/api');
    client.setUnauthorizedHandler(onUnauthorized);

    await client.get('/ok');

    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('detects a Lambda-proxy-wrapped 401 and redirects once', async () => {
    const mockFetch = vi.fn().mockResolvedValue(
      // HTTP 200 envelope carrying a wrapped 401.
      jsonResponse({ statusCode: 401, body: JSON.stringify({ error: 'expired' }) }, 200),
    );
    vi.stubGlobal('fetch', mockFetch);

    const onUnauthorized = vi.fn();
    const client = new ApiClient('/api');
    client.setUnauthorizedHandler(onUnauthorized);

    await Promise.allSettled([client.get('/x'), client.get('/y')]);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
