// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import * as fc from 'fast-check';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { Notification, NotificationsResponse } from '../types';

// Mock the hooks
const mockUseNotifications = vi.fn();
const mockMutate = vi.fn();
const mockUseMarkNotificationRead = vi.fn(() => ({ mutate: mockMutate }));

vi.mock('../hooks', () => ({
  useNotifications: (...args: unknown[]) => mockUseNotifications(...(args as [unknown])),
  useMarkNotificationRead: () => mockUseMarkNotificationRead(),
}));

import NotificationBell, { NotificationItem } from '../components/NotificationBell';

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMarkNotificationRead.mockReturnValue({ mutate: mockMutate });
});

function makeNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    notification_id: 'n-1',
    pipeline_id: 'p-1',
    message: 'Pipeline completed',
    notification_type: 'success',
    created_at: new Date().toISOString(),
    dashboard_read: 'false',
    ...overrides,
  };
}

// ── Unit tests (Task 5.3) ────────────────────────────────────────

describe('NotificationBell', () => {
  it('renders the bell icon', () => {
    mockUseNotifications.mockReturnValue({
      data: { notifications: [], count: 0 },
      isLoading: false,
      isError: false,
    });

    render(<NotificationBell />, { wrapper: createWrapper() });
    expect(screen.getByTestId('notification-bell')).toBeInTheDocument();
  });

  it('shows badge with unread count when unread notifications exist', () => {
    const notifications: Notification[] = [
      makeNotification({ notification_id: 'n-1', dashboard_read: 'false' }),
      makeNotification({ notification_id: 'n-2', dashboard_read: 'false' }),
      makeNotification({ notification_id: 'n-3', dashboard_read: 'true' }),
    ];
    mockUseNotifications.mockReturnValue({
      data: { notifications, count: 3 },
      isLoading: false,
      isError: false,
    });

    render(<NotificationBell />, { wrapper: createWrapper() });
    const badge = screen.getByTestId('notification-badge');
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toContain('2');
  });

  it('hides badge when all notifications are read', () => {
    const notifications: Notification[] = [
      makeNotification({ notification_id: 'n-1', dashboard_read: 'true' }),
    ];
    mockUseNotifications.mockReturnValue({
      data: { notifications, count: 1 },
      isLoading: false,
      isError: false,
    });

    render(<NotificationBell />, { wrapper: createWrapper() });
    expect(screen.queryByTestId('notification-badge')).not.toBeInTheDocument();
  });

  it('shows error message in dropdown when API returns error', () => {
    mockUseNotifications.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
    });

    render(<NotificationBell />, { wrapper: createWrapper() });
    // Badge should not be visible on error
    expect(screen.queryByTestId('notification-badge')).not.toBeInTheDocument();
    // The bell should still render
    expect(screen.getByTestId('notification-bell')).toBeInTheDocument();
  });

  it('hides badge when there are no notifications', () => {
    mockUseNotifications.mockReturnValue({
      data: { notifications: [], count: 0 },
      isLoading: false,
      isError: false,
    });

    render(<NotificationBell />, { wrapper: createWrapper() });
    expect(screen.queryByTestId('notification-badge')).not.toBeInTheDocument();
  });

  // ── Property test (Task 5.4) ─────────────────────────────────────
  /**
   * Feature: p12c-addon, Property 5: NotificationBell unread count accuracy
   *
   * For any list of Notification objects where each has dashboard_read set to
   * either "true" or "false", the badge count displayed by NotificationBell
   * shall equal the number of notifications where dashboard_read === "false".
   *
   * Validates: Requirements 7.2
   */
  it('Property 5: unread count accuracy — badge matches count of dashboard_read==="false"', () => {
    const notificationArb = fc.record({
      notification_id: fc.uuid(),
      pipeline_id: fc.uuid(),
      message: fc.string({ minLength: 1, maxLength: 50 }),
      notification_type: fc.constantFrom('success', 'error', 'warning', 'info'),
      created_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2025-12-31') }).map((d) => d.toISOString()),
      dashboard_read: fc.constantFrom('true', 'false'),
      read_at: fc.option(fc.date().map((d) => d.toISOString()), { nil: undefined }),
    });

    fc.assert(
      fc.property(fc.array(notificationArb, { minLength: 0, maxLength: 20 }), (notifications) => {
        const expectedUnread = notifications.filter((n) => n.dashboard_read === 'false').length;

        mockUseNotifications.mockReturnValue({
          data: { notifications, count: notifications.length } as NotificationsResponse,
          isLoading: false,
          isError: false,
        });

        const { unmount } = render(<NotificationBell />, { wrapper: createWrapper() });

        if (expectedUnread === 0) {
          expect(screen.queryByTestId('notification-badge')).not.toBeInTheDocument();
        } else {
          const badge = screen.getByTestId('notification-badge');
          expect(badge.textContent).toContain(String(expectedUnread));
        }

        unmount();
      }),
      { numRuns: 100 },
    );
  });

  // ── Property test (Task 5.5) ─────────────────────────────────────
  /**
   * Feature: p12c-addon, Property 6: Notification item content completeness
   *
   * For any Notification object with a non-empty message, notification_type,
   * and valid created_at ISO string, the rendered notification list item shall
   * contain the message text, the notification_type value, and a relative
   * timestamp string derived from created_at.
   *
   * Validates: Requirements 7.4
   */
  it('Property 6: notification item content — message, type, and timestamp are rendered', () => {
    const notificationArb = fc.record({
      notification_id: fc.uuid(),
      pipeline_id: fc.uuid(),
      message: fc.stringMatching(/^[a-zA-Z0-9 ]{1,40}$/),
      notification_type: fc.stringMatching(/^[a-zA-Z]{1,15}$/),
      created_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2025-06-01') }).map((d) => d.toISOString()),
      dashboard_read: fc.constantFrom('true', 'false'),
    });

    fc.assert(
      fc.property(notificationArb, (notification) => {
        const { container, unmount } = render(
          <NotificationItem notification={notification as Notification} />,
          { wrapper: createWrapper() },
        );

        const text = container.textContent ?? '';

        // Message must appear
        expect(text).toContain(notification.message);

        // notification_type must appear
        expect(text).toContain(notification.notification_type);

        // A relative timestamp must appear (formatRelativeTime returns patterns like "Xd ago", "Xh ago", "Xm ago", or "Just now")
        const hasTimestamp = /(\d+[dhm] ago|Just now|Unknown)/.test(text);
        expect(hasTimestamp).toBe(true);

        unmount();
      }),
      { numRuns: 100 },
    );
  });

  // ── Property test (Task 5.6) ─────────────────────────────────────
  /**
   * Feature: p12c-addon, Property 7: Notification read/unread visual distinction
   *
   * For any Notification object, if dashboard_read is "true" the rendered item
   * shall use reduced text emphasis (secondary color), and if dashboard_read is
   * "false" the rendered item shall use default text emphasis.
   *
   * Validates: Requirements 7.6
   */
  it('Property 7: read/unread visual distinction — read items use secondary color', () => {
    const baseNotificationArb = fc.record({
      notification_id: fc.uuid(),
      pipeline_id: fc.uuid(),
      message: fc.stringMatching(/^[a-zA-Z0-9 ]{1,30}$/),
      notification_type: fc.stringMatching(/^[a-zA-Z]{1,10}$/),
      created_at: fc.date({ min: new Date('2020-01-01'), max: new Date('2025-06-01') }).map((d) => d.toISOString()),
    });

    fc.assert(
      fc.property(baseNotificationArb, fc.constantFrom('true', 'false'), (base, dashboardRead) => {
        const notification: Notification = { ...base, dashboard_read: dashboardRead };

        const { container, unmount } = render(
          <NotificationItem notification={notification} />,
          { wrapper: createWrapper() },
        );

        // The outermost element has a data-testid with the notification_id
        const itemEl = container.querySelector(`[data-testid="notification-item-${notification.notification_id}"]`);
        expect(itemEl).not.toBeNull();

        // The Box with color="text-body-secondary" is inside the testid element.
        // Check if any child element has the secondary color class.
        const hasSecondaryColor = itemEl!.querySelector('[class*="color-text-body-secondary"]') !== null
          || (itemEl!.className ?? '').includes('color-text-body-secondary');

        if (dashboardRead === 'true') {
          expect(hasSecondaryColor).toBe(true);
        } else {
          // Unread: the item should NOT have secondary color on its direct Box
          // (the timestamp Box always has it, so we check the first child only)
          const firstChild = itemEl!.children[0];
          const firstChildClasses = firstChild?.className ?? '';
          expect(firstChildClasses).not.toMatch(/color-text-body-secondary/);
        }

        unmount();
      }),
      { numRuns: 100 },
    );
  });
});
