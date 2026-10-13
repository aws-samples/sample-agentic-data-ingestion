// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import BulkActionBar from '../BulkActionBar';
import type { Pipeline } from '../../types';

const makePipeline = (overrides: Partial<Pipeline> = {}): Pipeline => ({
  pipeline_key: 'p1',
  pipeline_name: 'Pipeline 1',
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  ...overrides,
});

describe('BulkActionBar', () => {
  it('renders archive and delete buttons with selection count', () => {
    const items = [makePipeline(), makePipeline({ pipeline_key: 'p2', pipeline_name: 'Pipeline 2' })];
    render(
      <BulkActionBar
        selectedItems={items}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    expect(screen.getByTestId('bulk-archive-button')).toHaveTextContent('Archive Selected (2)');
    expect(screen.getByTestId('bulk-delete-button')).toHaveTextContent('Delete Selected (2)');
  });

  it('enables delete button only when all selected are archived', () => {
    const archivedItems = [
      makePipeline({ status: 'archived' }),
      makePipeline({ pipeline_key: 'p2', status: 'archived' }),
    ];
    render(
      <BulkActionBar
        selectedItems={archivedItems}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    expect(screen.getByTestId('bulk-delete-button')).not.toBeDisabled();
  });

  it('disables delete button when not all selected are archived', () => {
    const mixedItems = [
      makePipeline({ status: 'archived' }),
      makePipeline({ pipeline_key: 'p2', status: 'active' }),
    ];
    render(
      <BulkActionBar
        selectedItems={mixedItems}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    expect(screen.getByTestId('bulk-delete-button')).toBeDisabled();
  });

  it('disables delete button when no items are archived', () => {
    const activeItems = [makePipeline({ status: 'active' })];
    render(
      <BulkActionBar
        selectedItems={activeItems}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    expect(screen.getByTestId('bulk-delete-button')).toBeDisabled();
  });

  it('calls onArchive when archive button is clicked', () => {
    const onArchive = vi.fn();
    render(
      <BulkActionBar
        selectedItems={[makePipeline()]}
        onArchive={onArchive}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    fireEvent.click(screen.getByTestId('bulk-archive-button'));
    expect(onArchive).toHaveBeenCalledOnce();
  });

  it('calls onDelete when delete button is clicked', () => {
    const onDelete = vi.fn();
    render(
      <BulkActionBar
        selectedItems={[makePipeline({ status: 'archived' })]}
        onArchive={vi.fn()}
        onDelete={onDelete}
        progress={null}
        isExecuting={false}
      />,
    );

    fireEvent.click(screen.getByTestId('bulk-delete-button'));
    expect(onDelete).toHaveBeenCalledOnce();
  });

  it('shows progress indicator during execution', () => {
    render(
      <BulkActionBar
        selectedItems={[makePipeline()]}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={{ completed: 2, total: 5 }}
        isExecuting={true}
      />,
    );

    const progressEl = screen.getByTestId('bulk-progress');
    expect(progressEl).toHaveTextContent('2/5');
  });

  it('does not show progress indicator when not executing', () => {
    render(
      <BulkActionBar
        selectedItems={[makePipeline()]}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={null}
        isExecuting={false}
      />,
    );

    expect(screen.queryByTestId('bulk-progress')).not.toBeInTheDocument();
  });

  it('disables buttons during execution', () => {
    render(
      <BulkActionBar
        selectedItems={[makePipeline({ status: 'archived' })]}
        onArchive={vi.fn()}
        onDelete={vi.fn()}
        progress={{ completed: 1, total: 3 }}
        isExecuting={true}
      />,
    );

    expect(screen.getByTestId('bulk-archive-button')).toBeDisabled();
    expect(screen.getByTestId('bulk-delete-button')).toBeDisabled();
  });
});
