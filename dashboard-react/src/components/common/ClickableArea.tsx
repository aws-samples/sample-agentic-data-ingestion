// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { CSSProperties, KeyboardEvent, MouseEvent, ReactNode } from 'react';

/** Activation event: a real click or an Enter/Space keypress. */
export type ClickableActivation = MouseEvent<HTMLElement> | KeyboardEvent<HTMLElement>;

/**
 * ClickableArea — the shared keyboard-accessible clickable wrapper (P29 R8.1).
 *
 * Several surfaces need a *region* (a card, or a `<Badge>`-wrapping element)
 * to be clickable without the Cloudscape `<Button>`/`<Link>` chrome that would
 * change its appearance. A bare `<div onClick>` / `<span onClick>` is a
 * keyboard trap: it is not focusable and cannot be activated with the keyboard.
 *
 * This component gives such regions full keyboard semantics in one place:
 *  - `role="button"` so assistive technology announces it as actionable,
 *  - `tabIndex={0}` so it is reachable in the tab order,
 *  - an Enter/Space `onKeyDown` handler that mirrors the click (Space is
 *    `preventDefault`-ed so it activates rather than scrolling the page),
 *  - `cursor: pointer` for the visual affordance.
 *
 * When `onClick` is omitted the element renders as a plain, non-interactive
 * `div`/`span` (no role, not focusable) — this keeps callers that only
 * conditionally attach a handler (e.g. StepFlowVisualization's `onStepClick`)
 * correct.
 *
 * Rendering as a design-token-styled div/span (rather than a real <button>)
 * is intentional: it avoids the UA button reset fighting Cloudscape's
 * `<Badge>`/`<Container>` children, while still satisfying the keyboard
 * contract that R8.1 requires.
 */
export interface ClickableAreaProps {
  /** Element tag to render. Defaults to `div`. */
  as?: 'div' | 'span';
  /**
   * Activation handler. Receives the originating click or keyboard event so
   * nested clickable regions can call `stopPropagation()`. When omitted the
   * element is non-interactive.
   */
  onClick?: (e: ClickableActivation) => void;
  /** Accessible name for screen readers (maps to `aria-label`). */
  ariaLabel?: string;
  /** Extra inline styles merged after the cursor affordance. */
  style?: CSSProperties;
  /** Test hook forwarded verbatim to the rendered element. */
  'data-testid'?: string;
  children: ReactNode;
}

export default function ClickableArea({
  as = 'div',
  onClick,
  ariaLabel,
  style,
  children,
  'data-testid': dataTestId,
}: ClickableAreaProps) {
  const interactive = typeof onClick === 'function';

  const handleKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (!interactive) return;
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      // Space would otherwise scroll the page; Enter is harmless but we
      // prevent-default both so activation is the only effect.
      e.preventDefault();
      onClick!(e);
    }
  };

  const Tag = as;
  return (
    <Tag
      data-testid={dataTestId}
      onClick={interactive ? onClick : undefined}
      onKeyDown={interactive ? handleKeyDown : undefined}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={ariaLabel}
      style={{ cursor: interactive ? 'pointer' : undefined, ...style }}
    >
      {children}
    </Tag>
  );
}
