// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for ClickableArea — the shared keyboard-accessible clickable
 * primitive (P29 R8.1 / R8.3).
 *
 * Validates: Requirements 8.1, 8.3
 *
 * Covers the converted interactive pattern once, thoroughly: the element is
 * focusable and exposes `role="button"`; Enter AND Space activate it (with
 * Space prevented from scrolling); a plain click still works; and with no
 * handler it is inert (no role, not in the tab order).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ClickableArea from '../ClickableArea';

describe('ClickableArea', () => {
  it('exposes button role and is focusable when interactive', () => {
    render(
      <ClickableArea onClick={() => {}} data-testid="area" ariaLabel="Open thing">
        content
      </ClickableArea>,
    );
    const el = screen.getByTestId('area');
    expect(el).toHaveAttribute('role', 'button');
    expect(el).toHaveAttribute('tabindex', '0');
    expect(el).toHaveAttribute('aria-label', 'Open thing');
  });

  it('activates on Enter', () => {
    const onClick = vi.fn();
    render(<ClickableArea onClick={onClick} data-testid="area">x</ClickableArea>);
    fireEvent.keyDown(screen.getByTestId('area'), { key: 'Enter' });
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('activates on Space and prevents default (no page scroll)', () => {
    const onClick = vi.fn();
    render(<ClickableArea onClick={onClick} data-testid="area">x</ClickableArea>);
    const el = screen.getByTestId('area');
    const prevented = !fireEvent.keyDown(el, { key: ' ' });
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(prevented).toBe(true);
  });

  it('does not activate on other keys', () => {
    const onClick = vi.fn();
    render(<ClickableArea onClick={onClick} data-testid="area">x</ClickableArea>);
    fireEvent.keyDown(screen.getByTestId('area'), { key: 'a' });
    expect(onClick).not.toHaveBeenCalled();
  });

  it('still activates on a plain mouse click', () => {
    const onClick = vi.fn();
    render(<ClickableArea onClick={onClick} data-testid="area">x</ClickableArea>);
    fireEvent.click(screen.getByTestId('area'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('renders as a span when requested and forwards the test id', () => {
    render(<ClickableArea as="span" onClick={() => {}} data-testid="area">x</ClickableArea>);
    expect(screen.getByTestId('area').tagName).toBe('SPAN');
  });

  it('is inert (no role, not focusable) when no handler is supplied', () => {
    render(<ClickableArea data-testid="area">x</ClickableArea>);
    const el = screen.getByTestId('area');
    expect(el).not.toHaveAttribute('role');
    expect(el).not.toHaveAttribute('tabindex');
  });
});
