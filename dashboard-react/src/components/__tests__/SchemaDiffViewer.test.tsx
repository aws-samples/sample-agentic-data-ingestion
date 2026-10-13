// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import SchemaDiffViewer from '../SchemaDiffViewer';
import type { SchemaDifference } from '../../types';

const SAMPLE_DIFFS: SchemaDifference[] = [
  { field_name: 'email', change_type: 'added', new_type: 'string' },
  { field_name: 'legacy_id', change_type: 'removed', old_type: 'integer' },
  { field_name: 'age', change_type: 'modified', old_type: 'string', new_type: 'integer' },
  { field_name: 'name', change_type: 'unchanged', new_type: 'string' },
];

describe('SchemaDiffViewer', () => {
  it('renders all diff rows', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const rows = screen.getAllByTestId('diff-row');
    expect(rows).toHaveLength(4);
  });

  it('applies green background for added fields', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const rows = screen.getAllByTestId('diff-row');
    const addedRow = rows.find((r) => r.getAttribute('data-change-type') === 'added')!;
    // Design token (P29 R8.2) — adaptive success surface, not a hardcoded hex.
    expect(addedRow.style.backgroundColor).toContain('color-background-status-success');
  });

  it('shows "+" prefix for added fields in inline mode', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const prefixes = screen.getAllByTestId('diff-prefix');
    const addedPrefix = prefixes.find((p) => p.textContent?.includes('+'));
    expect(addedPrefix).toBeTruthy();
  });

  it('applies red background for removed fields', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const rows = screen.getAllByTestId('diff-row');
    const removedRow = rows.find((r) => r.getAttribute('data-change-type') === 'removed')!;
    expect(removedRow.style.backgroundColor).toContain('color-background-status-error');
  });

  it('shows "−" prefix for removed fields in inline mode', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const prefixes = screen.getAllByTestId('diff-prefix');
    const removedPrefix = prefixes.find((p) => p.textContent?.includes('−'));
    expect(removedPrefix).toBeTruthy();
  });

  it('applies yellow background for modified fields', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const rows = screen.getAllByTestId('diff-row');
    const modifiedRow = rows.find((r) => r.getAttribute('data-change-type') === 'modified')!;
    expect(modifiedRow.style.backgroundColor).toContain('color-background-status-warning');
  });

  it('shows old → new type for modified fields', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const typeChange = screen.getByTestId('diff-type-change');
    expect(typeChange.textContent).toContain('string');
    expect(typeChange.textContent).toContain('integer');
    expect(typeChange.textContent).toContain('→');
  });

  it('applies no background highlight for unchanged fields', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    const rows = screen.getAllByTestId('diff-row');
    const unchangedRow = rows.find((r) => r.getAttribute('data-change-type') === 'unchanged')!;
    // unchanged should have no backgroundColor set (undefined or empty)
    expect(unchangedRow.style.backgroundColor).toBe('');
  });

  it('toggles between inline and side-by-side view modes', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);

    // Initially in inline mode — no side-by-side headers
    expect(screen.queryByText('Old version')).not.toBeInTheDocument();

    // Switch to side-by-side
    fireEvent.click(screen.getByText('Side by side'));

    // Side-by-side headers should appear
    expect(screen.getByText('Old version')).toBeInTheDocument();
    expect(screen.getByText('New version')).toBeInTheDocument();

    // Switch back to inline
    fireEvent.click(screen.getByText('Inline'));
    expect(screen.queryByText('Old version')).not.toBeInTheDocument();
  });

  it('shows "schemas are identical" message when differences array is empty', () => {
    render(<SchemaDiffViewer differences={[]} />);
    expect(screen.getByTestId('diff-empty')).toBeInTheDocument();
    expect(screen.getByText(/schemas are identical/i)).toBeInTheDocument();
  });

  it('renders side-by-side view with left/right columns', () => {
    render(<SchemaDiffViewer differences={SAMPLE_DIFFS} />);
    fireEvent.click(screen.getByText('Side by side'));

    const leftCols = screen.getAllByTestId('diff-left');
    const rightCols = screen.getAllByTestId('diff-right');
    expect(leftCols).toHaveLength(4);
    expect(rightCols).toHaveLength(4);

    // Added field: left should be empty, right should have the field
    const addedRow = screen.getAllByTestId('diff-row').find((r) => r.getAttribute('data-change-type') === 'added')!;
    const addedLeft = addedRow.querySelector('[data-testid="diff-left"]')!;
    const addedRight = addedRow.querySelector('[data-testid="diff-right"]')!;
    expect(addedLeft.textContent).toBe('');
    expect(addedRight.textContent).toContain('email');

    // Removed field: right should be empty, left should have the field
    const removedRow = screen.getAllByTestId('diff-row').find((r) => r.getAttribute('data-change-type') === 'removed')!;
    const removedLeft = removedRow.querySelector('[data-testid="diff-left"]')!;
    const removedRight = removedRow.querySelector('[data-testid="diff-right"]')!;
    expect(removedLeft.textContent).toContain('legacy_id');
    expect(removedRight.textContent).toBe('');
  });
});
