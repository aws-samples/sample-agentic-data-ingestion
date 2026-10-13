// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for src/config.ts runtime configuration loader.
 *
 * Validates: Requirements 7.2, 7.3, 9.2
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  loadConfig,
  getConfig,
  _resetConfigForTesting,
} from '../config';

// Stub fetch globally for these tests.
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

function validConfig() {
  return {
    region: 'us-west-2',
    userPoolId: 'us-west-2_abc123',
    userPoolClientId: 'client123',
    hostedUiDomain: 'agentic-data-ingestion-dev.auth.us-west-2.amazoncognito.com',
    apiBaseUrl: '/api',
    wsUrl: 'wss://abc.execute-api.us-west-2.amazonaws.com/prod',
  };
}

describe('config — loadConfig()', () => {
  beforeEach(() => {
    _resetConfigForTesting();
    mockFetch.mockReset();
    // Clear any Vite env stubs
    vi.unstubAllEnvs();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('fetches /config.json and returns validated config', async () => {
    const cfg = validConfig();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(cfg),
    });

    const result = await loadConfig();
    expect(result).toEqual(cfg);
    expect(mockFetch).toHaveBeenCalledWith('/config.json');
  });

  it('caches the config on subsequent calls', async () => {
    const cfg = validConfig();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(cfg),
    });

    await loadConfig();
    const second = await loadConfig();
    expect(second).toEqual(cfg);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('throws when fetch fails (network error)', async () => {
    mockFetch.mockRejectedValueOnce(new Error('Network failure'));

    await expect(loadConfig()).rejects.toThrow('Failed to fetch /config.json');
  });

  it('throws when response is not ok', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: 'Not Found',
    });

    await expect(loadConfig()).rejects.toThrow(
      'Failed to load /config.json: HTTP 404 Not Found',
    );
  });

  it('throws when response is not valid JSON', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    });

    await expect(loadConfig()).rejects.toThrow(
      'Failed to parse /config.json: invalid JSON',
    );
  });

  it('throws when a required field is missing', async () => {
    const incomplete = { region: 'us-west-2', apiBaseUrl: '/api' };
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(incomplete),
    });

    await expect(loadConfig()).rejects.toThrow(
      /missing or empty fields.*userPoolId/,
    );
  });

  it('throws when a required field is empty string', async () => {
    const cfg = { ...validConfig(), wsUrl: '' };
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(cfg),
    });

    await expect(loadConfig()).rejects.toThrow(/missing or empty fields.*wsUrl/);
  });

  it('throws when config is null', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(null),
    });

    await expect(loadConfig()).rejects.toThrow('Config must be a non-null object');
  });

  it('uses VITE_* env vars when all required vars are present', async () => {
    vi.stubEnv('VITE_REGION', 'eu-west-1');
    vi.stubEnv('VITE_USER_POOL_ID', 'eu-west-1_xyz');
    vi.stubEnv('VITE_USER_POOL_CLIENT_ID', 'client456');
    vi.stubEnv('VITE_HOSTED_UI_DOMAIN', 'myapp.auth.eu-west-1.amazoncognito.com');
    vi.stubEnv('VITE_WS_URL', 'wss://ws.example.com');

    const result = await loadConfig();
    expect(result.region).toBe('eu-west-1');
    expect(result.userPoolId).toBe('eu-west-1_xyz');
    expect(result.apiBaseUrl).toBe('/api'); // default
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('falls through to fetch when env vars are incomplete', async () => {
    vi.stubEnv('VITE_REGION', 'us-east-1');
    // Missing other required vars

    const cfg = validConfig();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(cfg),
    });

    const result = await loadConfig();
    expect(result).toEqual(cfg);
    expect(mockFetch).toHaveBeenCalled();
  });

  it('respects VITE_API_BASE_URL override', async () => {
    vi.stubEnv('VITE_REGION', 'us-west-2');
    vi.stubEnv('VITE_USER_POOL_ID', 'us-west-2_abc');
    vi.stubEnv('VITE_USER_POOL_CLIENT_ID', 'client789');
    vi.stubEnv('VITE_HOSTED_UI_DOMAIN', 'app.auth.us-west-2.amazoncognito.com');
    vi.stubEnv('VITE_WS_URL', 'wss://ws.example.com');
    vi.stubEnv('VITE_API_BASE_URL', '/custom-api');

    const result = await loadConfig();
    expect(result.apiBaseUrl).toBe('/custom-api');
  });
});

describe('config — getConfig()', () => {
  beforeEach(() => {
    _resetConfigForTesting();
    mockFetch.mockReset();
    vi.unstubAllEnvs();
  });

  it('throws when called before loadConfig()', () => {
    expect(() => getConfig()).toThrow(
      'Config not loaded yet. Call and await loadConfig() before accessing config.',
    );
  });

  it('returns config after loadConfig() resolves', async () => {
    const cfg = validConfig();
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve(cfg),
    });

    await loadConfig();
    expect(getConfig()).toEqual(cfg);
  });
});
