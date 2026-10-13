// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * useWizardDraft — Wizard_Draft persistence (P29 R5.2 / R5.3).
 *
 * Keeps the Create Pipeline Wizard's in-progress state alive across a full
 * page reload — critically, across the **Cognito Hosted UI redirect
 * round-trip** that happens mid-wizard when the session expires. Without this,
 * a user six steps into the wizard who is bounced to the Hosted UI loses
 * everything on return.
 *
 * Behavior contract:
 *  - **Debounced persistence**: every change to the wizard state is written to
 *    `sessionStorage` after `debounceMs` of quiet (default 400ms), so rapid
 *    typing produces one write, not one per keystroke.
 *  - **Restore/discard prompt on mount**: if a draft is found at load,
 *    `showRestorePrompt` is `true` so the container can render a Restore /
 *    Discard prompt. `restore()` returns the stored state (the caller rehydrates
 *    via `setState`); `discard()` clears it. Persistence stays *suspended* until
 *    the user resolves the prompt so the wizard's fresh initial state never
 *    clobbers the saved draft before the user can restore it.
 *  - **Cleared on submit/cancel**: `clear()` removes the draft and stops further
 *    writes (call it after a successful create AND on explicit cancel).
 *
 * `sessionStorage` (NOT `localStorage`) is deliberate: it survives the Hosted UI
 * redirect (same browser tab) yet does not linger across browser sessions, so a
 * stale draft never resurfaces days later.
 *
 * Generic over the persisted state shape `T` (the wizard passes `WizardState`);
 * `T` must be JSON-serializable.
 */

export const WIZARD_DRAFT_KEY = 'adi.wizard.draft';
const DEFAULT_DEBOUNCE_MS = 400;

export interface UseWizardDraftOptions {
  /** sessionStorage key. Defaults to `adi.wizard.draft`. */
  key?: string;
  /** Debounce window (ms) for persistence writes. Defaults to 400ms. */
  debounceMs?: number;
}

export interface UseWizardDraftResult<T> {
  /**
   * `true` when a persisted draft was found on mount and the user has not yet
   * chosen Restore or Discard. Drives the restore/discard prompt.
   */
  showRestorePrompt: boolean;
  /**
   * Rehydrate: returns the stored draft (or `null` if none), dismisses the
   * prompt, and arms persistence so subsequent edits are saved. The caller is
   * responsible for pushing the returned value into its state (`setState`).
   */
  restore: () => T | null;
  /** Discard the stored draft, dismiss the prompt, and arm persistence. */
  discard: () => void;
  /**
   * Clear the stored draft and stop persisting. Call after a successful submit
   * or on explicit cancel so the draft does not survive a completed/abandoned
   * wizard.
   */
  clear: () => void;
}

/** Safely read + parse the persisted draft; returns `null` on any failure. */
function readDraft<T>(storage: Storage | undefined, key: string): T | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw) as T;
  } catch {
    // Corrupt / unparseable draft — treat as absent rather than crashing mount.
    return null;
  }
}

export function useWizardDraft<T>(
  state: T,
  options: UseWizardDraftOptions = {},
): UseWizardDraftResult<T> {
  const { key = WIZARD_DRAFT_KEY, debounceMs = DEFAULT_DEBOUNCE_MS } = options;

  // sessionStorage (NOT localStorage) — see file header for rationale. Guarded
  // for non-DOM environments (SSR / bare unit contexts).
  const storage: Storage | undefined =
    typeof window !== 'undefined' ? window.sessionStorage : undefined;

  // Read the persisted draft exactly once, on the first render.
  const [storedDraft] = useState<T | null>(() => readDraft<T>(storage, key));

  // Prompt visible only while an unresolved draft exists.
  const [showRestorePrompt, setShowRestorePrompt] = useState<boolean>(
    () => storedDraft !== null,
  );

  // Persistence is "armed" only once the user has resolved the restore prompt
  // (or immediately when there was no draft to resolve). While disarmed we must
  // NOT write — otherwise the wizard's fresh initial state would overwrite the
  // saved draft before the user gets a chance to restore it.
  const armedRef = useRef<boolean>(storedDraft === null);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isFirstRunRef = useRef(true);

  // Latest state kept in a ref so the unmount flush can persist the final value
  // even if a keystroke is still inside the debounce window when a redirect or
  // navigation tears the wizard down.
  const latestStateRef = useRef<T>(state);
  latestStateRef.current = state;

  // Debounced write on state change (only while armed).
  useEffect(() => {
    // Skip the initial mount: the first `state` is either the empty wizard
    // default (nothing worth persisting yet) or a value the user has not touched
    // — so we do not clobber storage until a real change occurs.
    if (isFirstRunRef.current) {
      isFirstRunRef.current = false;
      return;
    }
    if (!armedRef.current || !storage) return;

    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      try {
        storage.setItem(key, JSON.stringify(state));
      } catch {
        // Best-effort: ignore quota / serialization failures.
      }
    }, debounceMs);

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [state, key, debounceMs, storage]);

  // Flush the latest state on unmount so a mid-wizard Cognito redirect (or any
  // navigation) does not lose the last keystrokes still sitting in the debounce
  // window. Skipped when disarmed (unresolved prompt, or after clear()).
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      if (!armedRef.current || !storage) return;
      try {
        storage.setItem(key, JSON.stringify(latestStateRef.current));
      } catch {
        // Best-effort.
      }
    };
    // Unmount-only: `storage`/`key` are stable for the hook's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const restore = useCallback((): T | null => {
    setShowRestorePrompt(false);
    armedRef.current = true;
    return storedDraft;
  }, [storedDraft]);

  const discard = useCallback(() => {
    try {
      storage?.removeItem(key);
    } catch {
      // Best-effort.
    }
    setShowRestorePrompt(false);
    armedRef.current = true;
  }, [storage, key]);

  const clear = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    // Stop persisting so the unmount flush and any in-flight debounce cannot
    // resurrect a draft the user just submitted or cancelled.
    armedRef.current = false;
    try {
      storage?.removeItem(key);
    } catch {
      // Best-effort.
    }
    setShowRestorePrompt(false);
  }, [storage, key]);

  return { showRestorePrompt, restore, discard, clear };
}
