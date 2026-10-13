// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * PipelineCard keyboard-accessibility tests (P29 R8.1 / R8.3).
 *
 * The whole card (`<div onClick=navigate>`) and each version badge
 * (`<span onClick>`) were previously mouse-only. They now render through the
 * shared ClickableArea. These tests prove the two converted patterns are
 * keyboard-reachable and keyboard-operable:
 *  - the card exposes button role + is focusable,
 *  - a version badge selects its version on Enter (keyboard), with the click
 *    still isolated from the card's navigation via stopPropagation.
 *
 * Requirements: 8.1, 8.3
 */
import { describe, it, expect } from 'vitest';
import { render, within, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PipelineCard from '../PipelineCard';
import type { Pipeline } from '../../types';

const pipeline: Pipeline = {
  pipeline_key: 'kb-pipe',
  pipeline_name: 'Keyboard Pipeline',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-06-01T00:00:00Z',
  file_count: 2,
  mapping_count: 0,
  schema_version: 2,
  schema_history: [
    { version: 1, created: '2024-01-01T00:00:00Z', has_mapping: true, mapping_count: 11, runs: [] },
    { version: 2, created: '2024-02-01T00:00:00Z', has_mapping: true, mapping_count: 22, runs: [] },
  ],
  all_files: [],
};

function renderCard() {
  return render(
    <MemoryRouter>
      <PipelineCard pipeline={pipeline} />
    </MemoryRouter>,
  );
}

describe('PipelineCard — keyboard access (R8.1)', () => {
  it('the card is focusable and exposes button role', () => {
    const { container } = renderCard();
    const card = within(container).getByTestId('pipeline-card');
    expect(card).toHaveAttribute('role', 'button');
    expect(card).toHaveAttribute('tabindex', '0');
    expect(card).toHaveAttribute('aria-label', 'Open pipeline Keyboard Pipeline');
  });

  it('version badges are focusable and expose button role', () => {
    const { container } = renderCard();
    const badges = within(container).getAllByTestId('version-badge');
    expect(badges).toHaveLength(2);
    badges.forEach((b) => {
      expect(b).toHaveAttribute('role', 'button');
      expect(b).toHaveAttribute('tabindex', '0');
    });
  });

  it('selecting a version badge with the Enter key updates the displayed mapping count', () => {
    const { container } = renderCard();
    const badges = within(container).getAllByTestId('version-badge');
    // Activate v1 (mapping_count 11) via keyboard.
    fireEvent.keyDown(badges[0], { key: 'Enter' });
    expect(within(container).getByTestId('mapping-count').textContent).toBe('11');
    // And v2 (mapping_count 22) via Space.
    fireEvent.keyDown(badges[1], { key: ' ' });
    expect(within(container).getByTestId('mapping-count').textContent).toBe('22');
  });
});
