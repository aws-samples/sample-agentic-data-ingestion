// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect } from 'react';

/**
 * Registers global keyboard event listeners for the given shortcut map.
 * Each key in the map is a descriptor like "ctrl+k" or "?" and the value
 * is the callback to invoke.
 *
 * Supported modifiers: ctrl, meta (Cmd on macOS).
 * The hook automatically prevents default browser behaviour for matched shortcuts.
 */
export function useKeyboardShortcuts(shortcuts: Record<string, () => void>): void {
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      // Skip if user is typing in an input/textarea/select
      const tag = (e.target as HTMLElement)?.tagName?.toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

      for (const [combo, handler] of Object.entries(shortcuts)) {
        if (matchesCombo(e, combo)) {
          e.preventDefault();
          handler();
          return;
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [shortcuts]);
}

function matchesCombo(e: KeyboardEvent, combo: string): boolean {
  const parts = combo.toLowerCase().split('+');
  const key = parts[parts.length - 1];
  const needsCtrl = parts.includes('ctrl') || parts.includes('meta') || parts.includes('mod');

  if (needsCtrl && !(e.ctrlKey || e.metaKey)) return false;
  if (!needsCtrl && (e.ctrlKey || e.metaKey)) return false;

  return e.key.toLowerCase() === key;
}
