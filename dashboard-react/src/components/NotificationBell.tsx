// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Button from '@cloudscape-design/components/button';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Popover from '@cloudscape-design/components/popover';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { useNotifications, useMarkNotificationRead } from '../hooks';
import { formatRelativeTime } from '../utils/formatting';
import type { Notification } from '../types';

export function NotificationItem({ notification, onDismiss }: { notification: Notification; onDismiss?: () => void }) {
  const isRead = notification.dashboard_read === 'true';
  return (
    <div
      data-testid={`notification-item-${notification.notification_id}`}
      style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8 }}
    >
      <Box color={isRead ? 'text-body-secondary' : undefined} fontSize="body-s">
        <SpaceBetween size="xxs" direction="horizontal">
          <span>{notification.message}</span>
          <Badge color="blue">{notification.notification_type}</Badge>
          <Box color="text-body-secondary" fontSize="body-s">
            {formatRelativeTime(notification.created_at)}
          </Box>
        </SpaceBetween>
      </Box>
      {!isRead && onDismiss && (
        <Button
          variant="icon"
          iconName="close"
          ariaLabel="Dismiss notification"
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          data-testid={`dismiss-${notification.notification_id}`}
        />
      )}
    </div>
  );
}

export default function NotificationBell() {
  const { data, isError } = useNotifications();
  const markRead = useMarkNotificationRead();

  const notifications = data?.notifications ?? [];
  const unreadNotifications = notifications.filter((n) => n.dashboard_read === 'false');
  const unreadCount = unreadNotifications.length;

  const handleMarkAllRead = () => {
    unreadNotifications.forEach((n) => markRead.mutate(n.notification_id));
  };

  const bellContent = (
    <div style={{ maxHeight: 300, overflowY: 'auto', minWidth: 300 }}>
      {isError ? (
        <StatusIndicator type="error">Unable to load notifications</StatusIndicator>
      ) : unreadNotifications.length === 0 ? (
        <Box color="text-body-secondary" padding="s">No new notifications</Box>
      ) : (
        <SpaceBetween size="xs">
          {unreadCount > 1 && (
            <div style={{ display: 'flex', justifyContent: 'flex-end', paddingBottom: 4 }}>
              <Button variant="inline-link" onClick={handleMarkAllRead}>
                Mark all read ({unreadCount})
              </Button>
            </div>
          )}
          {unreadNotifications.map((n) => (
            <NotificationItem
              key={n.notification_id}
              notification={n}
              onDismiss={() => markRead.mutate(n.notification_id)}
            />
          ))}
        </SpaceBetween>
      )}
    </div>
  );

  return (
    <div data-testid="notification-bell" style={{ display: 'inline-flex', alignItems: 'center' }}>
      <Popover
        header="Notifications"
        content={bellContent}
        position="bottom"
        size="large"
        triggerType="custom"
      >
        <div style={{ position: 'relative', display: 'inline-block' }}>
          <Button iconName="notification" variant="icon" ariaLabel="Notifications" />
          {!isError && unreadCount > 0 && (
            <span data-testid="notification-badge" style={{ position: 'absolute', top: -6, right: -6 }}>
              <Badge color="red">{String(unreadCount)}</Badge>
            </span>
          )}
        </div>
      </Popover>
    </div>
  );
}
