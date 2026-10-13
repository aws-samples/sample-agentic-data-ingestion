// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useAuth } from 'react-oidc-context';
import { useEffect } from 'react';
import type { ReactNode } from 'react';
import Spinner from '@cloudscape-design/components/spinner';
import Box from '@cloudscape-design/components/box';

interface Props {
  children: ReactNode;
}

/**
 * Guards authenticated routes. Redirects to the Cognito Hosted UI if the user
 * is not authenticated; shows a loading state while auth is in progress.
 */
export default function AuthGuard({ children }: Props) {
  const auth = useAuth();

  useEffect(() => {
    if (!auth.isLoading && !auth.isAuthenticated && !auth.activeNavigator) {
      auth.signinRedirect();
    }
  }, [auth.isLoading, auth.isAuthenticated, auth.activeNavigator, auth]);

  if (auth.isLoading || auth.activeNavigator) {
    return (
      <Box textAlign="center" padding={{ top: 'xxxl' }}>
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 's' }}>
          Authenticating…
        </Box>
      </Box>
    );
  }

  if (!auth.isAuthenticated) {
    // Redirect is in progress via the useEffect above
    return (
      <Box textAlign="center" padding={{ top: 'xxxl' }}>
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 's' }}>
          Redirecting to sign in…
        </Box>
      </Box>
    );
  }

  return <>{children}</>;
}
