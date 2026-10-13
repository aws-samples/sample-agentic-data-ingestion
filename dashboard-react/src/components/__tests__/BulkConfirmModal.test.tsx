// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import BulkConfirmModal from '../BulkConfirmModal';
import type { Pipeline } from '../../types';

const makePipeline = (key: string, name: string): Pipeline => ({
  pipeline_key: key,
  pipeline_name: name,
  status: 'active',
  source_schema: 'src',
  target_schema: 'tgt',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
});

const PIPELINES = [
  makePipeline('p1', 'Alpha Pipeline'),
  makePipeline('p2', 'Beta Pipeline'),
  makePipeline('p3', 'Gamma Pipeline'),
];

describe('BulkConfirmModal', () => {
  it('renders archive modal with correct header and pipeline list', () => {
    render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText('Archive 3 pipelines')).toBeInTheDocument();
    expect(screen.getByText(/Are you sure you want to archive the following 3 pipelines/)).toBeInTheDocument();
    expect(screen.getByText('Alpha Pipeline')).toBeInTheDocument();
    expect(screen.getByText('Beta Pipeline')).toBeInTheDocument();
    expect(screen.getByText('Gamma Pipeline')).toBeInTheDocument();
  });

  it('renders delete modal with correct header', () => {
    render(
      <BulkConfirmModal
        visible={true}
        action="delete"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText('Delete 3 pipelines')).toBeInTheDocument();
    expect(screen.getByText(/Are you sure you want to delete the following 3 pipelines/)).toBeInTheDocument();
  });

  it('uses singular grammar for single pipeline', () => {
    render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={[PIPELINES[0]]}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText('Archive 1 pipeline')).toBeInTheDocument();
  });

  it('lists all pipeline names', () => {
    render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    const list = screen.getByTestId('bulk-confirm-list');
    const items = list.querySelectorAll('li');
    expect(items).toHaveLength(3);
  });

  it('calls onConfirm when confirm button is clicked', () => {
    const onConfirm = vi.fn();
    render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={onConfirm}
        onDismiss={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTestId('bulk-confirm-submit'));
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('calls onDismiss when cancel button is clicked', () => {
    const onDismiss = vi.fn();
    render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={onDismiss}
      />,
    );

    fireEvent.click(screen.getByTestId('bulk-confirm-cancel'));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it('shows correct action label on confirm button', () => {
    const { rerender } = render(
      <BulkConfirmModal
        visible={true}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByTestId('bulk-confirm-submit')).toHaveTextContent('Archive');

    rerender(
      <BulkConfirmModal
        visible={true}
        action="delete"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByTestId('bulk-confirm-submit')).toHaveTextContent('Delete');
  });

  it('does not render content when not visible', () => {
    const { container } = render(
      <BulkConfirmModal
        visible={false}
        action="archive"
        pipelines={PIPELINES}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    // Cloudscape Modal keeps content in DOM when not visible but hides it.
    // Verify the modal dialog is not displayed (aria-modal not present on visible dialog)
    const dialog = container.querySelector('[role="dialog"]');
    expect(dialog).toBeNull();
  });
});
