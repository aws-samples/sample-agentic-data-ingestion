// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { AuthProvider as OidcAuthProvider } from 'react-oidc-context';
import type { WebStorageStateStore } from 'oidc-client-ts';
import type { ReactNode } from 'react';
import { getConfig } from '../config';

interface Props {
  children: ReactNode;
}

/**
 * Wraps the app in the OIDC AuthProvider configured from runtime config.
 * Must be rendered AFTER `loadConfig()` has resolved.
 */
export default function AuthProvider({ children }: Props) {
  const config = getConfig();

  const authority = `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`;

  const oidcConfig = {
    authority,
    client_id: config.userPoolClientId,
    redirect_uri: window.location.origin + '/auth/callback',
    post_logout_redirect_uri: window.location.origin,
    response_type: 'code',
    scope: 'openid email profile',
    automaticSilentRenew: true,
    // Cognito-specific: use sessionStorage to avoid cross-tab interference
    userStore: undefined as unknown as WebStorageStateStore,
  };

  return <OidcAuthProvider {...oidcConfig}>{children}</OidcAuthProvider>;
}
