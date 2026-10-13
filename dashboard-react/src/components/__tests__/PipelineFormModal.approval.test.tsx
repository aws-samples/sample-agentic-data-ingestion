// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `PipelineFormModal`'s edit branch: the Approval_Config hydrate-and-save round
 * trip, and the closing of the subset divergence.
 *
 * Before this, the modal rendered two of the wizard's four approval controls and
 * hand-built a two-key body. So an operator editing through the modal could not see
 * or change `always_approve_schema_changes` — a governance gate with a live reader
 * at `schema_mapping/lambda_function.py:1657` — and nothing indicated it existed.
 *
 * **The regression that matters most is `does NOT turn the schema-change gate off`
 * below.** `always_approve_schema_changes: true` means approval IS REQUIRED, and its
 * reader spells its own fallback `True`, so a record that stores no such key is a
 * record whose gate is ON. Hydrating that absence as `false` and saving writes an
 * explicit gate-off — a reduction in human oversight arriving as a side effect of
 * editing a description, with nothing reporting it. Mutation-checked: spelling the
 * hydrate `?? false` fails that test and several of its neighbours.
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
 * `normalizePipeline`, which passes `approval_config` across whole, preserving
 * absent, `{}` and populated as three distinct states (`LGR-7bea749-4`).
 */
function pipelineWith(approval_config?: Pipeline['approval_config']): Pipeline {
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
    approval_config,
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

/** The `approval_config` the last save put on the wire. */
function submittedApproval(): Record<string, unknown> {
  const body = mocks.editMutate.mock.calls.at(-1)?.[0] as Record<string, unknown>;
  return body.approval_config as Record<string, unknown>;
}

function checkbox(testId: string): HTMLInputElement {
  return screen.getByTestId(testId).querySelector('input')!;
}

beforeEach(() => {
  mocks.editMutate.mockReset().mockResolvedValue({});
  mocks.createMutate.mockReset().mockResolvedValue({});
});

describe('PipelineFormModal — the third gate is present at all', () => {
  it('renders all three boolean gates, not two', () => {
    // The divergence, stated as the thing that changed. `Require approval for schema
    // changes` was absent from this editor entirely.
    renderModal(pipelineWith({ schema_mapping_enabled: true, code_generation_enabled: true }));

    expect(screen.getByTestId('schema-mapping-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('code-generation-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('auto-approve-toggle')).toBeInTheDocument();
    expect(screen.getByText('Require approval for schema changes')).toBeInTheDocument();
  });

  it('renders no confidence-threshold control, on purpose', () => {
    // The fourth wizard control stays out of this editor. It has no reader, and on
    // this path — unlike the wizard's — a value would be written over a record that
    // may carry a different one or none. See `ApprovalGatesFields`' header and
    // `buildApprovalConfigRequest`'s `includeConfidenceThreshold`.
    renderModal(pipelineWith({ schema_mapping_confidence_threshold: 0.85 }));

    expect(screen.queryByTestId('confidence-slider')).not.toBeInTheDocument();
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();
    expect(screen.queryByText(/Confidence Threshold/)).not.toBeInTheDocument();
  });
});

describe('PipelineFormModal — hydrate and save round trip', () => {
  it('does NOT turn the schema-change gate off for a record that never stored it', async () => {
    // THE regression test. `_default_approval_config` seeds the key, but a record
    // predating it does not, and `schema_mapping:1657` reads
    // `approval_config.get("always_approve_schema_changes", True)` — so absent means
    // the gate is ON. Open, touch nothing, save.
    renderModal(pipelineWith({ schema_mapping_enabled: true, code_generation_enabled: true }));

    // What the operator sees must agree with what the backend enforces, before the
    // save is even considered.
    expect(checkbox('auto-approve-toggle').checked).toBe(true);

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(true);
  });

  it('does not turn it off when the record has no approval_config at all', async () => {
    // A pre-p63 record, and the case the removed `if (pipeline.approval_config)`
    // guard handled worst: it left whatever the previously-opened pipeline's toggles
    // were in state, and Save wrote those onto this pipeline.
    renderModal(pipelineWith(undefined));

    expect(checkbox('auto-approve-toggle').checked).toBe(true);

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(true);
  });

  it('does not turn it off for a record carrying an empty approval_config', async () => {
    // `{}` is a recorded "no overrides" and is preserved distinctly from absence by
    // `normalizePipeline` (`LGR-7bea749-4`). Both must fail closed.
    renderModal(pipelineWith({}));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(true);
  });

  it('preserves an explicit gate-off across the round trip', async () => {
    // `false` is a real recorded decision and must survive being re-saved, the same
    // way absence does. This is the direction a `?? false` reader gets right by
    // accident and the three tests above it get wrong.
    renderModal(pipelineWith({ always_approve_schema_changes: false }));

    expect(checkbox('auto-approve-toggle').checked).toBe(false);

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(false);
  });

  it('fails closed on a non-boolean stored value', async () => {
    // A record predating the boundary's validation could hold anything here. The
    // readers test these keys by TRUTHINESS, not identity — `schema_mapping:1669` is
    // `not schema_mapping_enabled` — so a stored `"false"` string keeps the gate on
    // at the backend. A `??` read would carry the string into `checked` and display
    // the opposite of what is enforced.
    renderModal(
      pipelineWith({
        always_approve_schema_changes: 'false' as unknown as boolean,
        schema_mapping_enabled: 0 as unknown as boolean,
      }),
    );

    expect(checkbox('auto-approve-toggle').checked).toBe(true);
    expect(checkbox('schema-mapping-toggle').checked).toBe(true);

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(true);
    expect(submittedApproval().schema_mapping_enabled).toBe(true);
  });

  it('preserves the two gates the modal already had', async () => {
    renderModal(
      pipelineWith({ schema_mapping_enabled: false, code_generation_enabled: false }),
    );

    expect(checkbox('schema-mapping-toggle').checked).toBe(false);
    expect(checkbox('code-generation-toggle').checked).toBe(false);

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().schema_mapping_enabled).toBe(false);
    expect(submittedApproval().code_generation_enabled).toBe(false);
  });

  it('sends an operator-made change to the third gate through', async () => {
    // The control being live is the point: it was invisible here, so this is the
    // first path by which an operator can change it through the modal at all.
    renderModal(pipelineWith({ always_approve_schema_changes: true }));

    fireEvent.click(checkbox('auto-approve-toggle'));
    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(submittedApproval().always_approve_schema_changes).toBe(false);
  });
});

describe('PipelineFormModal — the confidence threshold is left alone', () => {
  it('omits the key entirely rather than sending a value nobody chose', async () => {
    // Key-set absence, not a value assertion. `edit_pipeline` merges per key
    // (`LGR-7bea749-1`), so a key this body omits is genuinely untouched on the
    // record and a key it carries is genuinely written. The wizard seeds `80`
    // (= 0.8) while `_default_approval_config` seeds `Decimal("0.85")`; sending it
    // would resolve that knowing disagreement — p63 R9/D2's to resolve — as a side
    // effect of an unrelated edit.
    renderModal(pipelineWith({ schema_mapping_enabled: true }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(Object.keys(submittedApproval())).not.toContain(
      'schema_mapping_confidence_threshold',
    );
  });

  it('omits it even when the record already carries one', async () => {
    // The stored value is hydrated (the wizard needs it) but not re-submitted, so a
    // pipeline's stored threshold survives an edit through this modal unchanged
    // rather than being rewritten to whatever the round trip happened to produce.
    renderModal(pipelineWith({ schema_mapping_confidence_threshold: 0.85 }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(Object.keys(submittedApproval())).not.toContain(
      'schema_mapping_confidence_threshold',
    );
  });

  it('sends exactly the three keys that have readers', async () => {
    // Set equality. An extra key would be persisted verbatim by
    // `_submitted_approval_config` and read by nobody; a missing one makes its
    // control inert.
    renderModal(pipelineWith({ schema_mapping_enabled: true }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    expect(new Set(Object.keys(submittedApproval()))).toEqual(
      new Set([
        'schema_mapping_enabled',
        'code_generation_enabled',
        'always_approve_schema_changes',
      ]),
    );
  });

  it('sends no camelCase spelling', async () => {
    // The modal now routes through `buildApprovalConfigRequest` rather than
    // hand-building the body, and that function is the single normalisation
    // authority (p63 R1.2). A camelCase key would reach no reader.
    renderModal(pipelineWith({ schema_mapping_enabled: true }));

    save();

    await waitFor(() => expect(mocks.editMutate).toHaveBeenCalledTimes(1));
    for (const key of Object.keys(submittedApproval())) {
      expect(key).not.toMatch(/[A-Z]/);
    }
  });
});
