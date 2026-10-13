// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useAuth } from 'react-oidc-context';
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import Spinner from '@cloudscape-design/components/spinner';
import Box from '@cloudscape-design/components/box';

/**
 * Route component for `/auth/callback`. Handles the OIDC redirect callback.
 * react-oidc-context processes the authorization code automatically when it
 * detects the callback URL. This component shows a loading state while that
 * exchange completes, then redirects to the app root.
 */
export default function AuthCallback() {
  const auth = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!auth.isLoading && auth.isAuthenticated) {
      navigate('/', { replace: true });
    }
  }, [auth.isLoading, auth.isAuthenticated, navigate]);

  return (
    <Box textAlign="center" padding={{ top: 'xxxl' }}>
      <Spinner size="large" />
      <Box variant="p" padding={{ top: 's' }}>
        Completing sign-in…
      </Box>
    </Box>
  );
}
