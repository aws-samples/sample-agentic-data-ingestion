// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Runtime configuration loader.
 *
 * Fetches `/config.json` (deployed by CDK to the S3 origin) at startup and
 * exposes typed config for the rest of the app. In local development, falls
 * back to Vite env vars (`VITE_*`) or a local `public/config.json`.
 *
 * This module is framework-agnostic — no React dependency. Call `loadConfig()`
 * from `main.tsx` before rendering; then use `getConfig()` anywhere.
 */

export interface AppConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  hostedUiDomain: string;
  apiBaseUrl: string;
  wsUrl: string;
}

const REQUIRED_KEYS: readonly (keyof AppConfig)[] = [
  'region',
  'userPoolId',
  'userPoolClientId',
  'hostedUiDomain',
  'apiBaseUrl',
  'wsUrl',
];

let _config: AppConfig | null = null;

/**
 * Validate that the given object has all required AppConfig fields and that
 * each is a non-empty string.
 */
function validateConfig(data: unknown): AppConfig {
  if (typeof data !== 'object' || data === null) {
    throw new Error('Config must be a non-null object');
  }

  const obj = data as Record<string, unknown>;
  const missing: string[] = [];

  for (const key of REQUIRED_KEYS) {
    if (typeof obj[key] !== 'string' || obj[key] === '') {
      missing.push(key);
    }
  }

  if (missing.length > 0) {
    throw new Error(
      `Invalid config: missing or empty fields: ${missing.join(', ')}`,
    );
  }

  return obj as unknown as AppConfig;
}

/**
 * Attempt to build config from Vite environment variables.
 * Returns `null` if the required vars are not all present.
 */
function configFromEnv(): AppConfig | null {
  const env = import.meta.env;

  const region = env.VITE_REGION as string | undefined;
  const userPoolId = env.VITE_USER_POOL_ID as string | undefined;
  const userPoolClientId = env.VITE_USER_POOL_CLIENT_ID as string | undefined;
  const hostedUiDomain = env.VITE_HOSTED_UI_DOMAIN as string | undefined;
  const apiBaseUrl = (env.VITE_API_BASE_URL as string | undefined) ?? '/api';
  const wsUrl = env.VITE_WS_URL as string | undefined;

  if (!region || !userPoolId || !userPoolClientId || !hostedUiDomain || !wsUrl) {
    return null;
  }

  return { region, userPoolId, userPoolClientId, hostedUiDomain, apiBaseUrl, wsUrl };
}

/**
 * Load the runtime configuration.
 *
 * Resolution order:
 * 1. Vite env vars (local dev shortcut — no network request needed).
 * 2. Fetch `/config.json` (works in production and local dev with public/config.json).
 *
 * Throws if config cannot be loaded or is invalid.
 */
export async function loadConfig(): Promise<AppConfig> {
  // Already loaded — return cached value.
  if (_config) return _config;

  // Try env vars first (local dev).
  const fromEnv = configFromEnv();
  if (fromEnv) {
    _config = fromEnv;
    return _config;
  }

  // Fetch config.json (production path, or local dev with public/config.json).
  let response: Response;
  try {
    response = await fetch('/config.json');
  } catch (err) {
    throw new Error(
      `Failed to fetch /config.json: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (!response.ok) {
    throw new Error(
      `Failed to load /config.json: HTTP ${response.status} ${response.statusText}`,
    );
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error('Failed to parse /config.json: invalid JSON');
  }

  _config = validateConfig(data);
  return _config;
}

/**
 * Get the loaded config synchronously.
 * Must be called after `loadConfig()` resolves; throws otherwise.
 */
export function getConfig(): AppConfig {
  if (!_config) {
    throw new Error(
      'Config not loaded yet. Call and await loadConfig() before accessing config.',
    );
  }
  return _config;
}

/**
 * Reset internal state. Exposed for testing only.
 * @internal
 */
export function _resetConfigForTesting(): void {
  _config = null;
}
