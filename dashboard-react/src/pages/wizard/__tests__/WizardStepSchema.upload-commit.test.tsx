// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Upload mode must write `state.schemaFields`. Nothing asserted this before:
// WizardStepSchema.test.tsx only checks that the upload widget renders, and
// CreatePipelinePage.schema-upload.test.tsx seeds `schemaFields` from a
// sessionStorage draft, so the upload UI was never exercised end to end. The
// gap let a defect ship where a parsed schema stayed in SchemaUploadWizard's
// local state, the review step read "Schema Source: Uploaded / Fields: 0
// fields", and Create failed with 400 "target_schema is required".
//
// These tests drive the real widget through a stateful harness so the commit
// (or lack of one) is observable in wizard state.
import { useState } from 'react';
import { describe, it, expect, vi, beforeAll } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepSchema } from '../WizardStepSchema';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

vi.mock('../../../hooks', () => ({
  useSchemas: () => ({ data: [], isLoading: false }),
}));

// jsdom's File doesn't implement .text() — polyfill it (same shim as
// components/__tests__/SchemaUploadWizard.test.tsx).
beforeAll(() => {
  if (!File.prototype.text) {
    File.prototype.text = function () {
      return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.readAsText(this);
      });
    };
  }
});

/**
 * Holds real wizard state so an `onChange` from the step is visible on the
 * next render — a `vi.fn()` spy would show the call but not prove the value
 * lands in `schemaFields`.
 */
function Harness({ initial }: { initial: Partial<WizardState> }) {
  const [state, setState] = useState<WizardState>({ ...INITIAL_WIZARD_STATE, ...initial });
  return (
    <>
      <WizardStepSchema state={state} onChange={setState} />
      <div data-testid="wizard-schema-fields">{JSON.stringify(state.schemaFields)}</div>
    </>
  );
}

function renderUploadMode() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      {/* `schemaSourceExplicitlySet` keeps the pre-selection effect from
          resetting the mode back to 'detected'. */}
      <Harness initial={{ schemaSource: 'upload', schemaSourceExplicitlySet: true }} />
    </QueryClientProvider>,
  );
}

function committedFields(): WizardState['schemaFields'] {
  return JSON.parse(screen.getByTestId('wizard-schema-fields').textContent || '[]');
}

async function uploadFile(file: File) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    fireEvent.change(input);
  });
}

function jsonFile(content: unknown, name = 'schema.json'): File {
  return new File([typeof content === 'string' ? content : JSON.stringify(content)], name, {
    type: 'application/json',
  });
}

describe('WizardStepSchema — upload mode commits to wizard state', () => {
  it('a successful parse populates schemaFields without a second click', async () => {
    renderUploadMode();
    expect(committedFields()).toEqual([]);

    await uploadFile(
      jsonFile({
        properties: {
          id: { type: 'integer', description: 'Primary key' },
          email: { type: 'string', description: '' },
        },
        required: ['id'],
      }),
    );

    await waitFor(() => {
      expect(committedFields()).toHaveLength(2);
    });
    expect(committedFields()).toEqual([
      { name: 'id', type: 'integer', nullable: false, description: 'Primary key' },
      { name: 'email', type: 'string', nullable: true, description: '' },
    ]);

    // The commit happened on the parse alone — "Use This Schema" is rendered
    // but was never clicked.
    expect(screen.getByTestId('use-schema-button')).toBeInTheDocument();
  });

  it('a failed parse does not populate schemaFields', async () => {
    renderUploadMode();

    await uploadFile(jsonFile('not valid json {{{'));

    await waitFor(() => {
      expect(screen.getByTestId('schema-parse-error')).toBeInTheDocument();
    });
    expect(committedFields()).toEqual([]);
    // No preview, so no commit affordance either.
    expect(screen.queryByTestId('use-schema-button')).not.toBeInTheDocument();
  });

  it('an inline edit followed by "Use This Schema" overwrites the auto-committed fields', async () => {
    renderUploadMode();

    await uploadFile(jsonFile({ properties: { amount: { type: 'string' } } }));

    await waitFor(() => {
      expect(committedFields()).toEqual([
        { name: 'amount', type: 'string', nullable: true, description: '' },
      ]);
    });

    // Inline edits mutate SchemaUploadWizard's local state only, so the button
    // is still how an edited schema reaches the wizard.
    const typeInput = screen.getByTestId('field-type-0').querySelector('input') as HTMLInputElement;
    fireEvent.change(typeInput, { target: { value: 'double' } });
    const descInput = screen.getByTestId('field-desc-0').querySelector('input') as HTMLInputElement;
    fireEvent.change(descInput, { target: { value: 'Order total' } });

    fireEvent.click(screen.getByTestId('use-schema-button'));

    expect(committedFields()).toEqual([
      { name: 'amount', type: 'double', nullable: true, description: 'Order total' },
    ]);
  });
});
