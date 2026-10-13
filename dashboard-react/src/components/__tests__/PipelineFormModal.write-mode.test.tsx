// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `PipelineFormModal`'s edit branch offers only the write modes the pipeline's
 * target implements (2026-10-10).
 *
 * Before this, the dialog offered "Merge (Upsert)" for every target and
 * `edit_pipeline` validated no write mode, so a JDBC pipeline could be switched
 * to a merge that ran as append and an S3 Parquet one to a merge that ran as
 * overwrite, both reporting success. The server now refuses those pairs
 * (`shared/write_modes.py`); these tests pin that the dialog does not offer
 * them, and that a pipeline already stored with one says so instead of showing
 * a different option while still submitting the stored value.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import PipelineFormModal from '../PipelineFormModal';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline, TargetConfig } from '../../types';

const mocks = vi.hoisted(() => ({
  editMutate: vi.fn(),
  createMutate: vi.fn(),
}));

vi.mock('../../hooks', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../hooks')>()),
  useBedrockModels: () => ({
    data: [{ model_id: 'anthropic.claude-3-haiku-20240307-v1:0', model_name: 'Claude 3 Haiku' }],
    isLoading: false,
    isError: false,
  }),
  useEditPipeline: () => ({ mutateAsync: mocks.editMutate }),
  useCreatePipeline: () => ({ mutateAsync: mocks.createMutate }),
}));

function pipelineWith(target_config: Partial<TargetConfig>): Pipeline {
  return {
    pipeline_key: 'pipeline-1',
    pipeline_name: 'orders_ingest',
    status: 'active',
    source_schema: '',
    target_schema: 'orders_v1',
    created_at: '',
    updated_at: '',
    ai_model: 'anthropic.claude-3-haiku-20240307-v1:0',
    file_count: 0,
    all_files: [],
    schema_version: 1,
    schema_history: [],
    target_config,
  } as Pipeline;
}

function renderModal(pipeline: Pipeline) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <PipelineFormModal visible onDismiss={() => {}} pipeline={pipeline} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/** The labels the write-mode dropdown offers once opened. */
function offeredWriteModes(): string[] {
  const select = createWrapper(document.body).findSelect('[data-testid="edit-write-mode-select"]');
  expect(select, 'the edit branch renders a write-mode select').not.toBeNull();
  select!.openDropdown();
  return select!
    .findDropdown()
    .findOptions()
    .map((option) => option.getElement().textContent ?? '');
}

beforeEach(() => {
  mocks.editMutate.mockReset().mockResolvedValue({});
  mocks.createMutate.mockReset().mockResolvedValue({});
});

describe('PipelineFormModal — write modes per target', () => {
  it.each(['JDBC', 'RDS_DATABASE', 'S3_PARQUET', 'GLUE_CATALOG'])('does not offer Merge for a %s target', (type) => {
    renderModal(pipelineWith({ type, write_mode: 'append' }));
    const offered = offeredWriteModes().join(' | ');
    expect(offered).toContain('Append');
    expect(offered).toContain('Overwrite');
    expect(offered).not.toContain('Merge');
  });

  it.each(['NEW_S3_TABLE_BUCKET', 'EXISTING_S3_TABLE_BUCKET', 's3_tables'])('offers Merge for an S3 Tables target (%s)', (type) => {
    renderModal(pipelineWith({ type, write_mode: 'append' }));
    expect(offeredWriteModes().join(' | ')).toContain('Merge');
  });
});

describe('PipelineFormModal — a stored mode the target cannot run', () => {
  it('says so on the control and refuses to save until a supported mode is chosen', async () => {
    // A legacy record: JDBC with `merge`, which ran as append.
    renderModal(pipelineWith({ type: 'JDBC', write_mode: 'merge', merge_key: 'id' }));
    expect(screen.getByText(/'merge' is not supported for this target/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(screen.getByText(/Choose a write mode this target supports/)).toBeInTheDocument());
    expect(mocks.editMutate).not.toHaveBeenCalled();
  });

  it('saves once the user picks a supported mode', async () => {
    renderModal(pipelineWith({ type: 'JDBC', write_mode: 'merge', merge_key: 'id' }));
    const select = createWrapper(document.body).findSelect('[data-testid="edit-write-mode-select"]')!;
    select.openDropdown();
    select.selectOptionByValue('append');
    expect(screen.queryByText(/is not supported for this target/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    const body = mocks.editMutate.mock.calls[0][0] as { target_config: Record<string, unknown> };
    expect(body.target_config.write_mode).toBe('append');
    expect(body.target_config.merge_key).toBeUndefined();
  });
});
