// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for AuthGuard component.
 *
 * Validates: Requirements 6.2, 6.4
 *
 * AuthGuard redirects unauthenticated users to the Cognito Hosted UI and
 * renders children when authenticated.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import AuthGuard from '../auth/AuthGuard';

// Mock react-oidc-context
const mockSigninRedirect = vi.fn();

vi.mock('react-oidc-context', () => ({
  useAuth: vi.fn(),
}));

// Import after mocking so we can control the return value per test.
import { useAuth } from 'react-oidc-context';
const mockedUseAuth = vi.mocked(useAuth);

describe('AuthGuard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calls signinRedirect when user is not authenticated', () => {
    mockedUseAuth.mockReturnValue({
      isLoading: false,
      isAuthenticated: false,
      activeNavigator: undefined,
      signinRedirect: mockSigninRedirect,
    } as unknown as ReturnType<typeof useAuth>);

    render(
      <AuthGuard>
        <div>Protected content</div>
      </AuthGuard>,
    );

    expect(mockSigninRedirect).toHaveBeenCalledTimes(1);
    // Should NOT render the protected content
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument();
  });

  it('renders children when user is authenticated', () => {
    mockedUseAuth.mockReturnValue({
      isLoading: false,
      isAuthenticated: true,
      activeNavigator: undefined,
      signinRedirect: mockSigninRedirect,
    } as unknown as ReturnType<typeof useAuth>);

    render(
      <AuthGuard>
        <div>Protected content</div>
      </AuthGuard>,
    );

    expect(screen.getByText('Protected content')).toBeInTheDocument();
    expect(mockSigninRedirect).not.toHaveBeenCalled();
  });

  it('shows loading state while auth is in progress', () => {
    mockedUseAuth.mockReturnValue({
      isLoading: true,
      isAuthenticated: false,
      activeNavigator: undefined,
      signinRedirect: mockSigninRedirect,
    } as unknown as ReturnType<typeof useAuth>);

    render(
      <AuthGuard>
        <div>Protected content</div>
      </AuthGuard>,
    );

    expect(screen.queryByText('Protected content')).not.toBeInTheDocument();
    expect(screen.getByText('Authenticating…')).toBeInTheDocument();
    expect(mockSigninRedirect).not.toHaveBeenCalled();
  });

  it('does not call signinRedirect while navigator is active', () => {
    mockedUseAuth.mockReturnValue({
      isLoading: false,
      isAuthenticated: false,
      activeNavigator: 'signinRedirect',
      signinRedirect: mockSigninRedirect,
    } as unknown as ReturnType<typeof useAuth>);

    render(
      <AuthGuard>
        <div>Protected content</div>
      </AuthGuard>,
    );

    // When activeNavigator is set, the guard shows the loading state and
    // does NOT fire another signinRedirect.
    expect(mockSigninRedirect).not.toHaveBeenCalled();
  });
});
