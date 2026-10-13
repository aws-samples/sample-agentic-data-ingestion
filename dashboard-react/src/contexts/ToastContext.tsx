// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { createContext, useContext, useState, useCallback, useRef } from 'react';
import Flashbar, { type FlashbarProps } from '@cloudscape-design/components/flashbar';

export type ToastType = 'success' | 'error' | 'info' | 'warning';

interface ToastContextValue {
  showToast: (message: string, type: ToastType) => void;
}

const ToastContext = createContext<ToastContextValue | undefined>(undefined);

const TOAST_DURATION = 5000;

const toFlashType = (t: ToastType): FlashbarProps.Type =>
  t === 'error' ? 'error' : t === 'warning' ? 'warning' : t === 'info' ? 'info' : 'success';

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<FlashbarProps.MessageDefinition[]>([]);
  const idRef = useRef(0);

  const dismiss = useCallback((id: string) => {
    setItems(prev => prev.filter(i => i.id !== id));
  }, []);

  const showToast = useCallback((message: string, type: ToastType) => {
    idRef.current += 1;
    const id = `toast-${idRef.current}`;

    const item: FlashbarProps.MessageDefinition = {
      id,
      type: toFlashType(type),
      content: message,
      dismissible: true,
      dismissLabel: 'Dismiss',
      onDismiss: () => dismiss(id),
    };

    setItems(prev => [item, ...prev]);

    // Auto-dismiss after duration
    setTimeout(() => dismiss(id), TOAST_DURATION);
  }, [dismiss]);

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      <div
        data-testid="toast-container"
        data-toast-type={items[0]?.type}
        style={{
          position: 'fixed',
          top: 8,
          right: 16,
          left: 16,
          zIndex: 9999,
          pointerEvents: items.length ? 'auto' : 'none',
        }}
      >
        <Flashbar items={items} stackItems={items.length > 1} />
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return ctx;
}
