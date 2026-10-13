// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';
import { notificationKeys } from './query-keys';
import type { NotificationsResponse } from '../types';

export function useNotifications(limit?: number, unreadOnly?: boolean) {
  return useQuery<NotificationsResponse>({
    queryKey: notificationKeys.list({ limit, unreadOnly }),
    queryFn: () => {
      const params = new URLSearchParams();
      if (limit != null) params.set('limit', String(limit));
      if (unreadOnly != null) params.set('unread_only', String(unreadOnly));
      const qs = params.toString();
      const url = `${API_ENDPOINTS.notifications.list}${qs ? `?${qs}` : ''}`;
      return apiClient.get<NotificationsResponse>(url);
    },
    refetchInterval: 30_000,
  });
}

export function useMarkNotificationRead() {
  const queryClient = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (notificationId: string) =>
      apiClient.put<void>(API_ENDPOINTS.notifications.markRead(notificationId), {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: notificationKeys.all });
    },
  });
}
