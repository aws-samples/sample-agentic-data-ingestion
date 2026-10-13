// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `PipelineFormModal`'s edit branch: the Quality_Config hydrate-and-save round
 * trip.
 *
 * Validates: Requirements R6.6, R7.2, R7.4.
 *
 * The modal had **no covering tests at all** before this file, which is worth
 * stating: it is the second editor of the pipeline object and the one an operator
 * reaches from the Overview page's Edit action. The round trip below is the
 * regression that matters most — a pipeline with no stored `data_quality_enabled`
 * must survive being opened and saved without gaining one, because gaining one
 * means gaining an explicit opt-OUT, which drops the row-level split and
 * quarantine write an S3 Tables pipeline has had since P28 and moves its verdict
 * from `enforced` to `advisory` with nothing reporting it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import PipelineFormModal from '../PipelineFormModal';
import { ToastProvider } from '../../contexts/ToastContext';
import type { Pipeline } from '../../types';

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

/**
 * A pipeline as `usePipelineDetail` hands it over — i.e. already through
 * `normalizePipeline`, which passes `quality_config` across whole.
 */
function pipelineWith(quality_config?: Pipeline['quality_config']): Pipeline {
  return {
    pipeline_key: 'pipeline-1',
    pipeline_name: 'claims_ingest',
    status: 'active',
    source_schema: '',
    target_schema: 'claims_v3',
    created_at: '',
    updated_at: '',
    ai_model: 'anthropic.claude-3-haiku-20240307-v1:0',
    file_count: 0,
    all_files: [],
    schema_version: 1,
    schema_history: [],
    quality_config,
  } as Pipeline;
}

function renderModal(pipeline: Pipeline) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <PipelineFormModal visible onDismiss={() => {}} pipeline={pipeline} />
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function save() {
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
}

/** The `quality_config` the last save put on the wire. */
function submittedQuality(): Record<string, unknown> {
  const body = mocks.editMutate.mock.calls.at(-1)?.[0] as Record<string, unknown>;
  return body.quality_config as Record<string, unknown>;
}

beforeEach(() => {
  mocks.editMutate.mockReset().mockResolvedValue({});
  mocks.createMutate.mockReset().mockResolvedValue({});
});

describe('PipelineFormModal — the quality settings are present at all', () => {
  it('renders the shared controls in the edit branch', () => {
    // The asymmetry this change exists to remove: before it, the modal had two
    // approval controls and NO quality controls, while the wizard had the full set.
    renderModal(pipelineWith({ dedup_enabled: true, dedup_columns: [] }));

    expect(screen.getByText('Data Quality')).toBeInTheDocument();
    expect(screen.getByTestId('data-quality-select')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-enabled-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-columns-input')).toBeInTheDocument();
  });

  it('leaves the approval toggles alone', () => {
    // Their polarity, keys and `auto-approve-toggle` test id are governed elsewhere
    // (p63 R2.9) and were not touched here.
    //
    // **Amended, not made to pass.** This test used to assert `Require approval for
    // schema changes` was NOT in the document, and its comment recorded that gap as
    // "missing on purpose: same class of fix, different sub-object". That fix has
    // since landed — the `approval_config` subset divergence is closed by
    // `ApprovalGatesFields`, so the third gate is now here. The confidence threshold
    // is still absent and now permanently so, for the reason its own test in
    // `PipelineFormModal.approval.test.tsx` states. That file owns the approval
    // round trip; this one only checks the two sub-objects coexist.
    renderModal(pipelineWith());

    expect(screen.getByText('Require approval for schema mapping')).toBeInTheDocument();
    expect(screen.getByText('Require approval for code generation')).toBeInTheDocument();
    expect(screen.getByText('Require approval for schema changes')).toBeInTheDocument();
    expect(screen.queryByText(/Confidence Threshold/)).not.toBeInTheDocument();
  });
});

describe('PipelineFormModal — hydrate and save round trip', () => {
  it('does NOT gain a data_quality_enabled that the record never had', async () => {
    // THE regression test. `_default_quality_config` seeds `dedup_enabled` and
    // `dedup_columns` and deliberately no gate, so this is the shape of a normal
    // record — including every existing S3 Tables pipeline, which is on by template
    // default. Open, touch nothing, save.
    renderModal(pipelineWith({ dedup_enabled: false, dedup_columns: [] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    // Key-set equality on the absence, not a value assertion: `data_quality_enabled:
    // undefined` and an absent key are the same after `JSON.stringify` but NOT to
    // `Object.keys`, and the per-key merge in `edit_pipeline` writes one document
    // path per key the body carries.
    expect(Object.keys(submittedQuality())).not.toContain('data_quality_enabled');
  });

  it('shows "Template default" for a record with no gate, rather than "Off"', () => {
    renderModal(pipelineWith({ dedup_enabled: false, dedup_columns: [] }));

    expect(screen.getByTestId('data-quality-select').textContent).toContain('Template default');
  });

  it('does not gain a gate when the record has no quality_config at all', async () => {
    // A pre-p64 record. The hydrate call is unconditional precisely so this resets to
    // the documented defaults instead of leaving a previous pipeline's state in place.
    renderModal(pipelineWith(undefined));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(Object.keys(submittedQuality())).not.toContain('data_quality_enabled');
  });

  it('preserves an explicit opt-out across the round trip', async () => {
    // `false` is a real recorded decision and must survive being re-saved, the same
    // way absence does. This is the direction a `?? false` reader gets right by
    // accident and the one above it gets wrong.
    renderModal(pipelineWith({ data_quality_enabled: false, dedup_enabled: false, dedup_columns: [] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedQuality().data_quality_enabled).toBe(false);
  });

  it('preserves an explicit opt-in across the round trip', async () => {
    renderModal(pipelineWith({ data_quality_enabled: true, dedup_enabled: false, dedup_columns: [] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedQuality().data_quality_enabled).toBe(true);
  });

  it('preserves stored dedup settings across the round trip', async () => {
    renderModal(pipelineWith({ dedup_enabled: true, dedup_columns: ['patient_id', 'visit_date'] }));

    expect(screen.getByTestId('dedup-columns-input').querySelector('input')!).toHaveValue(
      'patient_id, visit_date',
    );

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedQuality().dedup_enabled).toBe(true);
    expect(submittedQuality().dedup_columns).toEqual(['patient_id', 'visit_date']);
  });

  it('sends an operator-made change through in snake_case', async () => {
    renderModal(pipelineWith({ dedup_enabled: false, dedup_columns: [] }));

    fireEvent.click(screen.getByTestId('dedup-enabled-toggle').querySelector('input')!);
    fireEvent.change(screen.getByTestId('dedup-columns-input').querySelector('input')!, {
      target: { value: 'claim_id' },
    });
    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    const quality = submittedQuality();
    expect(quality.dedup_enabled).toBe(true);
    expect(quality.dedup_columns).toEqual(['claim_id']);
    // Turning dedup on must not express an opinion about its independent neighbour.
    expect(Object.keys(quality)).not.toContain('data_quality_enabled');
  });

  it('still submits target_config and approval_config alongside', async () => {
    // The new key is an addition, not a replacement: `edit_pipeline` merges per key,
    // so a body that stopped naming `approval_config` would leave those settings
    // stale rather than erased — but the controls would then be inert.
    renderModal(pipelineWith({ dedup_enabled: false, dedup_columns: [] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    const body = mocks.editMutate.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(body.id).toBe('pipeline-1');
    expect(body.approval_config).toBeDefined();
    expect(body.target_config).toBeDefined();
  });
});

describe('PipelineFormModal — the save gate on dedup columns', () => {
  it('refuses to submit a column name the boundary would refuse', async () => {
    // The boundary would 400 it anyway; the point of the control is that the
    // operator is told which name and why, here, rather than after a failed Save.
    renderModal(pipelineWith({ dedup_enabled: true, dedup_columns: ['ok', 'not-ok'] }));

    expect(screen.getByText(/not-ok/)).toBeInTheDocument();

    save();

    await waitFor(() => expect(screen.getAllByText(/not-ok/).length).toBeGreaterThan(0));
    expect(mocks.editMutate).not.toHaveBeenCalled();
  });

  it('does not block on an empty column list', async () => {
    // R7.2: empty MEANS every mapped target column, and is the default.
    renderModal(pipelineWith({ dedup_enabled: true, dedup_columns: [] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
  });

  it('does not block on bad columns while dedup is off', async () => {
    // The control is not rendered, so blocking Save would be a dead end with no
    // visible field to correct — unreachable for a keyboard or screen-reader user in
    // particular. The columns are inert while dedup is off.
    renderModal(pipelineWith({ dedup_enabled: false, dedup_columns: ['not-ok'] }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
  });
});
