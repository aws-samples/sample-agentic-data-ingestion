// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for ApiClient token attachment.
 *
 * Validates: Requirements 6.2, 9.3
 *
 * When a token accessor is set and returns a token, the raw Cognito token is
 * attached as the Authorization header (no `Bearer ` scheme prefix, as required
 * by the API Gateway Cognito User Pools authorizer). When no token accessor or
 * it returns null, no Authorization header is sent.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { ApiClient } from '../services/api-client';

describe('ApiClient — token attachment', () => {
  let client: ApiClient;

  beforeEach(() => {
    vi.restoreAllMocks();
    client = new ApiClient('/api');
  });

  it('attaches the raw token as Authorization (no Bearer prefix) when token accessor returns a token', async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    client.setTokenAccessor(() => 'my-jwt-token');

    await client.requestRaw('/test');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, options] = mockFetch.mock.calls[0];
    expect(options.headers).toEqual(
      expect.objectContaining({ Authorization: 'my-jwt-token' }),
    );
  });

  it('does not attach Authorization header when token accessor returns null', async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    client.setTokenAccessor(() => null);

    await client.requestRaw('/test');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, options] = mockFetch.mock.calls[0];
    expect(options.headers).not.toHaveProperty('Authorization');
  });

  it('does not attach Authorization header when no token accessor is set', async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    await client.requestRaw('/test');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, options] = mockFetch.mock.calls[0];
    expect(options.headers).not.toHaveProperty('Authorization');
  });

  it('preserves existing headers when attaching token', async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);

    client.setTokenAccessor(() => 'tok123');

    await client.requestRaw('/test', {
      headers: { 'Content-Type': 'application/json' },
    });

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const [, options] = mockFetch.mock.calls[0];
    expect(options.headers).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'tok123',
    });
  });
});
