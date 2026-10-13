// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for useWizardDraft — Wizard_Draft persistence (P29 R5.2 / R5.3).
 *
 * Covers the hook's contract in isolation with fake timers driving the debounce:
 *  - debounced write of state to sessionStorage (coalesced across rapid changes)
 *  - restore round-trip across a remount (draft found → showRestorePrompt → restore)
 *  - persistence suspended while the restore prompt is unresolved (no clobber)
 *  - discard clears the draft and re-arms persistence
 *  - clear removes the draft and stops further writes
 *
 * Requirements: 5.2, 5.3
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWizardDraft, WIZARD_DRAFT_KEY } from '../useWizardDraft';

interface DraftState {
  pipelineName: string;
}

function mount(initial: DraftState = { pipelineName: '' }) {
  return renderHook(({ state }: { state: DraftState }) => useWizardDraft(state), {
    initialProps: { state: initial },
  });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  sessionStorage.clear();
});

describe('useWizardDraft', () => {
  it('uses sessionStorage, not localStorage', () => {
    const { rerender } = mount();
    act(() => rerender({ state: { pipelineName: 'abc' } }));
    act(() => vi.advanceTimersByTime(400));

    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).not.toBeNull();
    expect(localStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
  });

  it('persists the latest state only after the debounce window', () => {
    const { rerender } = mount();

    act(() => rerender({ state: { pipelineName: 'abc' } }));
    // Nothing written before the debounce window elapses.
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();

    act(() => vi.advanceTimersByTime(400));
    expect(JSON.parse(sessionStorage.getItem(WIZARD_DRAFT_KEY)!)).toEqual({
      pipelineName: 'abc',
    });
  });

  it('does not persist the initial mount state (nothing to save yet)', () => {
    mount({ pipelineName: '' });
    act(() => vi.advanceTimersByTime(400));
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
  });

  it('coalesces rapid changes into a single write of the last value', () => {
    const { rerender } = mount();

    act(() => rerender({ state: { pipelineName: 'a' } }));
    act(() => vi.advanceTimersByTime(100));
    act(() => rerender({ state: { pipelineName: 'ab' } }));
    act(() => vi.advanceTimersByTime(100));
    act(() => rerender({ state: { pipelineName: 'abc' } }));
    // Not yet written — each change reset the debounce.
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();

    act(() => vi.advanceTimersByTime(400));
    expect(JSON.parse(sessionStorage.getItem(WIZARD_DRAFT_KEY)!)).toEqual({
      pipelineName: 'abc',
    });
  });

  it('restores a saved draft on a fresh mount (round-trip)', () => {
    // First session: write a draft, then tear the hook down.
    const first = mount();
    act(() => first.rerender({ state: { pipelineName: 'saved-name' } }));
    act(() => vi.advanceTimersByTime(400));
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).not.toBeNull();
    first.unmount();

    // Second session (e.g. after the Cognito redirect): the draft is offered.
    const second = mount();
    expect(second.result.current.showRestorePrompt).toBe(true);

    let restored: DraftState | null = null;
    act(() => {
      restored = second.result.current.restore();
    });
    expect(restored).toEqual({ pipelineName: 'saved-name' });
    expect(second.result.current.showRestorePrompt).toBe(false);
  });

  it('suspends persistence while the restore prompt is unresolved, then arms on discard', () => {
    sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify({ pipelineName: 'saved-name' }));

    const { result, rerender } = mount({ pipelineName: '' });
    expect(result.current.showRestorePrompt).toBe(true);

    // The fresh wizard state changes while the prompt is still up — this must
    // NOT clobber the stored draft the user might still want to restore.
    act(() => rerender({ state: { pipelineName: 'fresh-typing' } }));
    act(() => vi.advanceTimersByTime(400));
    expect(JSON.parse(sessionStorage.getItem(WIZARD_DRAFT_KEY)!)).toEqual({
      pipelineName: 'saved-name',
    });

    // Discarding clears the draft and re-arms persistence.
    act(() => result.current.discard());
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
    expect(result.current.showRestorePrompt).toBe(false);

    act(() => rerender({ state: { pipelineName: 'after-discard' } }));
    act(() => vi.advanceTimersByTime(400));
    expect(JSON.parse(sessionStorage.getItem(WIZARD_DRAFT_KEY)!)).toEqual({
      pipelineName: 'after-discard',
    });
  });

  it('clear() removes the draft and stops further writes', () => {
    const { result, rerender } = mount();

    act(() => rerender({ state: { pipelineName: 'a' } }));
    act(() => vi.advanceTimersByTime(400));
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).not.toBeNull();

    act(() => result.current.clear());
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();

    // Post-clear edits must not resurrect the draft.
    act(() => rerender({ state: { pipelineName: 'b' } }));
    act(() => vi.advanceTimersByTime(400));
    expect(sessionStorage.getItem(WIZARD_DRAFT_KEY)).toBeNull();
  });

  it('starts with no restore prompt when no draft exists', () => {
    const { result } = mount();
    expect(result.current.showRestorePrompt).toBe(false);
  });
});
