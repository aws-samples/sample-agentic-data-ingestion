// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for AuthProvider — the OIDC configuration builder.
 *
 * **Validates: Requirements 16.2**
 *
 * AuthProvider was the one symbol in p30 R16.2's zero-coverage list that really
 * had none: `src/auth/` had no `__tests__` directory and no test file referenced
 * it (AuthGuard is covered by `src/__tests__/auth-guard.test.tsx`, AuthProvider
 * was not).
 *
 * Expectations here are derived from the requirements and the recorded hosting
 * decision, NOT from reading the current output back (R16.3):
 *
 *  - `dashboard-hosting-cognito` R3.1 — the app client uses the Authorization
 *    Code grant with PKCE, so `response_type` must be `'code'`. An implicit-flow
 *    (`'token'`) or hybrid value would not match the provisioned client.
 *  - R3.6 — an expired token is refreshed silently, so silent renew must be on.
 *  - R3.7 — the Cognito callback and logout URLs are this deployment's own
 *    CloudFront URL, so both redirect URIs must be built from
 *    `window.location.origin` rather than any baked-in domain.
 *  - R7.1 / R7.3 — Cognito IDs and the region come from the runtime
 *    `config.json`; one JS bundle serves every environment, so nothing
 *    environment-specific may be frozen in at module load.
 *  - R7.2 — `config.json` loads before authenticated content renders. Rendering
 *    ahead of `loadConfig()` must fail loudly, not silently produce a
 *    misconfigured provider.
 *  - `LGR-2d64f7a-1` (backfilled) — the REST API takes the Cognito **ID token**
 *    as the raw `Authorization` header value. Cognito only issues an ID token
 *    when `openid` is requested, so the scope is load-bearing for the API
 *    contract, not cosmetic.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import AuthProvider from '../AuthProvider';
import type { AppConfig } from '../../config';

// ---------------------------------------------------------------------------
// Capture the props handed to react-oidc-context's AuthProvider. The real one
// would try to reach Cognito's discovery endpoint; the double records the
// configuration and renders children so composition is observable too.
// ---------------------------------------------------------------------------

const mocks = vi.hoisted(() => ({
  captured: null as Record<string, unknown> | null,
  config: null as AppConfig | null,
  configError: null as Error | null,
}));

vi.mock('react-oidc-context', () => ({
  AuthProvider: (props: { children?: ReactNode }) => {
    const { children, ...rest } = props;
    mocks.captured = rest;
    return children ?? null;
  },
}));

vi.mock('../../config', () => ({
  getConfig: () => {
    if (mocks.configError) throw mocks.configError;
    return mocks.config;
  },
}));

function deployedConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    region: 'us-west-2',
    userPoolId: 'us-west-2_AbCdEf123',
    userPoolClientId: '1h57kf5cpq17m0eml12EXAMPLE',
    hostedUiDomain: 'agentic-data-ingestion-444455556666-dev',
    apiBaseUrl: '/api',
    wsUrl: 'wss://abc123.execute-api.us-west-2.amazonaws.com/prod',
    ...overrides,
  };
}

/** Render AuthProvider and return the OIDC settings it built. */
function renderAndCapture(config: AppConfig): Record<string, unknown> {
  mocks.config = config;
  render(
    <AuthProvider>
      <div>dashboard</div>
    </AuthProvider>,
  );
  expect(mocks.captured).not.toBeNull();
  return mocks.captured as Record<string, unknown>;
}

beforeEach(() => {
  mocks.captured = null;
  mocks.config = null;
  mocks.configError = null;
});

describe('AuthProvider — OIDC configuration from runtime config', () => {
  it('builds the authority from the runtime region and user pool id', () => {
    // R7.1 / R7.3: nothing environment-specific is baked into the bundle.
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.authority).toBe(
      'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_AbCdEf123',
    );
  });

  it('takes the client id from runtime config', () => {
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.client_id).toBe('1h57kf5cpq17m0eml12EXAMPLE');
  });

  it('serves a different environment without a rebuild', () => {
    // R7.3: the same module, given different runtime config, must produce a
    // different authority/client — i.e. config is read at render, not frozen at
    // import.
    const first = renderAndCapture(deployedConfig());
    expect(first.authority).toContain('us-west-2_AbCdEf123');

    mocks.captured = null;
    const second = renderAndCapture(
      deployedConfig({
        region: 'eu-west-1',
        userPoolId: 'eu-west-1_ZzYyXx987',
        userPoolClientId: 'other-client-id',
      }),
    );

    expect(second.authority).toBe(
      'https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_ZzYyXx987',
    );
    expect(second.client_id).toBe('other-client-id');
  });

  it('uses the authorization code flow', () => {
    // R3.1: the app client is provisioned for code grant + PKCE. Implicit flow
    // would be rejected by that client.
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.response_type).toBe('code');
  });

  it('requests the openid scope so Cognito issues an ID token', () => {
    // LGR-2d64f7a-1: the REST API authorizer consumes the ID token as the raw
    // Authorization header value. No `openid` scope → no ID token → every
    // authenticated API call fails.
    const cfg = renderAndCapture(deployedConfig());

    const scopes = String(cfg.scope).split(/\s+/);
    expect(scopes).toContain('openid');
    expect(scopes).toContain('email');
    expect(scopes).toContain('profile');
  });

  it('points both redirect URIs at this deployment origin', () => {
    // R3.7: callback and logout URLs are the CloudFront URL of *this*
    // deployment, so they must be derived from the served origin.
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.redirect_uri).toBe(`${window.location.origin}/auth/callback`);
    expect(cfg.post_logout_redirect_uri).toBe(window.location.origin);
  });

  it('renews the session silently rather than bouncing the user to login', () => {
    // R3.6: an expiring token is refreshed with the refresh token; a redirect is
    // the failure path, not the routine one.
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.automaticSilentRenew).toBe(true);
  });

  it('leaves the user store unset so the session stays tab-scoped', () => {
    // oidc-client-ts defaults userStore to a sessionStorage-backed store when
    // none is supplied, which is the intent recorded at the call site (avoid
    // cross-tab interference). Passing a localStorage store here would share the
    // session across tabs.
    const cfg = renderAndCapture(deployedConfig());

    expect(cfg.userStore).toBeUndefined();
  });

  it('renders children inside the OIDC provider', () => {
    renderAndCapture(deployedConfig());

    expect(screen.getByText('dashboard')).toBeInTheDocument();
  });

  it('fails loudly when rendered before the runtime config is loaded', () => {
    // R7.2: config.json loads before authenticated content renders. Rendering
    // early must surface the misconfiguration, not build a provider pointing at
    // an undefined user pool.
    mocks.configError = new Error(
      'Config not loaded yet. Call and await loadConfig() before accessing config.',
    );

    // React logs the boundary-less render error; silence it so the expected
    // throw does not look like a test failure in the output.
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      expect(() =>
        render(
          <AuthProvider>
            <div>dashboard</div>
          </AuthProvider>,
        ),
      ).toThrow(/Config not loaded yet/);
    } finally {
      consoleError.mockRestore();
    }
    expect(mocks.captured).toBeNull();
  });
});
