// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, ApiError } from '../api-client';

function mockFetch(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  const ok = init.ok ?? true;
  const status = init.status ?? 200;
  const makeResponse = () => ({
    ok,
    status,
    json: () => Promise.resolve(body),
    clone() { return makeResponse(); },
  });
  return vi.fn().mockResolvedValue(makeResponse());
}

describe('ApiClient', () => {
  let client: ApiClient;
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    client = new ApiClient('');
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  describe('get<T>()', () => {
    it('unwraps Lambda proxy response with string body', async () => {
      globalThis.fetch = mockFetch({
        statusCode: 200,
        body: JSON.stringify({ items: [1, 2, 3] }),
      });

      const result = await client.get<{ items: number[] }>('/test');
      expect(result).toEqual({ items: [1, 2, 3] });
    });

    it('unwraps Lambda proxy response with object body', async () => {
      globalThis.fetch = mockFetch({
        statusCode: 200,
        body: { items: ['a', 'b'] },
      });

      const result = await client.get<{ items: string[] }>('/test');
      expect(result).toEqual({ items: ['a', 'b'] });
    });

    it('passes through plain JSON (no statusCode wrapper)', async () => {
      globalThis.fetch = mockFetch({ items: [] });

      const result = await client.get<{ items: unknown[] }>('/test');
      expect(result).toEqual({ items: [] });
    });

    it('throws ApiError on Lambda proxy 4xx', async () => {
      globalThis.fetch = mockFetch({
        statusCode: 404,
        body: JSON.stringify({ error: 'not found' }),
      });

      await expect(client.get('/missing')).rejects.toThrow(ApiError);
      try {
        await client.get('/missing2');
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(404);
      }
    });

    it('throws ApiError on Lambda proxy 5xx', async () => {
      globalThis.fetch = mockFetch({
        statusCode: 500,
        body: JSON.stringify({ message: 'Internal server error' }),
      });

      await expect(client.get('/fail')).rejects.toThrow(ApiError);
      try {
        await client.get('/fail2');
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(500);
        expect((err as ApiError).message).toBe('Internal server error');
      }
    });

    it('throws ApiError on plain JSON with non-ok response', async () => {
      globalThis.fetch = mockFetch(
        { detail: 'forbidden' },
        { ok: false, status: 403 },
      );

      await expect(client.get('/forbidden')).rejects.toThrow(ApiError);
    });
  });

  describe('post<T>()', () => {
    it('sends JSON body and unwraps response', async () => {
      globalThis.fetch = mockFetch({
        statusCode: 200,
        body: JSON.stringify({ id: 'new-123' }),
      });

      const result = await client.post<{ id: string }>('/create', { name: 'test' });
      expect(result).toEqual({ id: 'new-123' });
      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/create',
        expect.objectContaining({
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: 'test' }),
        }),
      );
    });
  });

  describe('in-flight deduplication', () => {
    it('deduplicates concurrent identical GET requests', async () => {
      let resolvePromise: (value: Response) => void;
      const pending = new Promise<Response>((resolve) => {
        resolvePromise = resolve;
      });

      globalThis.fetch = vi.fn().mockReturnValue(pending);

      const p1 = client.get<{ ok: boolean }>('/dedup');
      const p2 = client.get<{ ok: boolean }>('/dedup');

      expect(globalThis.fetch).toHaveBeenCalledTimes(1);
      expect(client.inflightSize).toBe(1);

      // Resolve the single fetch
      resolvePromise!({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ ok: true }),
        clone() {
          return {
            ok: true,
            status: 200,
            json: () => Promise.resolve({ ok: true }),
            clone() { return this; },
          };
        },
      } as unknown as Response);

      const [r1, r2] = await Promise.all([p1, p2]);
      expect(r1).toEqual({ ok: true });
      expect(r2).toEqual({ ok: true });
      expect(client.inflightSize).toBe(0);
    });
  });

  describe('requestRaw()', () => {
    it('returns raw Response object', async () => {
      const mockResponse = {
        ok: true,
        status: 200,
        json: () => Promise.resolve({ raw: true }),
      };
      globalThis.fetch = vi.fn().mockResolvedValue(mockResponse);

      const res = await client.requestRaw('/raw');
      expect(res).toBe(mockResponse);
    });
  });

  describe('setTokenAccessor()', () => {
    it('attaches Authorization header when token is available', async () => {
      client.setTokenAccessor(() => 'test-jwt-token');
      globalThis.fetch = mockFetch({ statusCode: 200, body: '{}' });

      await client.get('/protected');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/protected',
        expect.objectContaining({
          headers: expect.objectContaining({
            Authorization: 'test-jwt-token',
          }),
        }),
      );
    });

    it('does not attach Authorization header when token accessor returns null', async () => {
      client.setTokenAccessor(() => null);
      globalThis.fetch = mockFetch({ statusCode: 200, body: '{}' });

      await client.get('/public');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/public',
        expect.objectContaining({
          headers: {},
        }),
      );
    });

    it('does not attach Authorization header when no accessor is set', async () => {
      globalThis.fetch = mockFetch({ statusCode: 200, body: '{}' });

      await client.get('/no-auth');

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/no-auth',
        expect.objectContaining({
          headers: {},
        }),
      );
    });

    it('merges Authorization with existing Content-Type header on POST', async () => {
      client.setTokenAccessor(() => 'my-token');
      globalThis.fetch = mockFetch({ statusCode: 200, body: JSON.stringify({ ok: true }) });

      await client.post('/data', { key: 'value' });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/data',
        expect.objectContaining({
          headers: expect.objectContaining({
            'Content-Type': 'application/json',
            Authorization: 'my-token',
          }),
        }),
      );
    });
  });
});
