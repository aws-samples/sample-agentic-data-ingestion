// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ToastType } from '../contexts/ToastContext';

/**
 * Module-level toast ref — set by App.tsx once ToastProvider mounts.
 * Used by QueryCache.onError in main.tsx to show toast notifications
 * for ApiError instances without requiring React context access.
 */
let globalToastFn: ((message: string, type: ToastType) => void) | null = null;

export function setGlobalToast(fn: (message: string, type: ToastType) => void) {
  globalToastFn = fn;
}

export function showGlobalToast(message: string, type: ToastType) {
  globalToastFn?.(message, type);
}
