// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for MappingViewer — context pre-population, character count,
 * model selector, and defaultModelId support.
 *
 * Feature: p15-rerun-mapping
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import MappingViewer from '../MappingViewer';
import type { MappingData } from '../../types';

// ---------------------------------------------------------------------------
// Mock useBedrockModels so we don't need a real API
// ---------------------------------------------------------------------------
const MOCK_MODELS = [
  { model_id: 'anthropic.claude-3-5-sonnet-20241022-v2:0', model_name: 'Claude 3.5 Sonnet v2' },
  { model_id: 'amazon.nova-lite-v1:0', model_name: 'Amazon Nova Lite' },
  { model_id: 'amazon.nova-pro-v1:0', model_name: 'Amazon Nova Pro' },
];

vi.mock('../../hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../hooks')>();
  return {
    ...actual,
    useBedrockModels: () => ({ data: MOCK_MODELS, isLoading: false, error: null }),
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

const baseMappingData: MappingData = {
  mappings: [{ source_field: 'fname', target_field: 'first_name', confidence: 0.95 }],
  unmapped_source: [],
  // p45 task 13.2 — `unmapped_target` carries the Unmapped_Entry's object shape
  // (`UnsourcedTargetField`), not a bare name. Amended because the content was
  // **wrong**, not because anything complained: an empty array is assignable to
  // both the old `string[]` and the new element type, so task 7.2 measured this
  // site as permanently silent under every gate — `tsconfig.json` excludes
  // `src/**/__tests__/**`, and vitest cannot see a shape a fixture never
  // exercises. A fixture that says nothing about the element type is the shape
  // that let three flattening sites throw `default_value` and `reason` away
  // unobserved. This one is the producer's own object
  // (`_normalise_mapping_structure:1122-1128`), so the surface these tests render
  // is the surface production renders.
  //
  // The entry is `required` rather than `not_established` deliberately: it
  // exercises the primary path — the requirement label, the R2.4 rule sentence,
  // the consequence and the reason — so this file's seven context/model-selector
  // tests run against a fully-populated viewer rather than an empty one.
  unmapped_target: [
    {
      field: 'mobile_phone',
      requirement: 'required',
      default_value: null,
      reason: 'No source field in generated mapping',
    },
  ],
  schema_hash: 'abc123',
  version_index: 0,
  ai_model_used: 'amazon.nova-lite-v1:0',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('MappingViewer context and model selector', () => {
  it('pre-populates text area from initialContext prop', () => {
    const Wrapper = createWrapper();
    const onRerun = vi.fn();
    render(
      <Wrapper>
        <MappingViewer
          mappingData={baseMappingData}
          initialContext="Map fname to first_name"
          onRerunMapping={onRerun}
        />
      </Wrapper>,
    );

    const textarea = screen.getByPlaceholderText(/additional context/i);
    expect(textarea).toHaveValue('Map fname to first_name');
  });

  it('shows empty text area when initialContext is not provided', () => {
    const Wrapper = createWrapper();
    const onRerun = vi.fn();
    render(
      <Wrapper>
        <MappingViewer mappingData={baseMappingData} onRerunMapping={onRerun} />
      </Wrapper>,
    );

    const textarea = screen.getByPlaceholderText(/additional context/i);
    expect(textarea).toHaveValue('');
  });

  it('displays character count matching text area length', () => {
    const Wrapper = createWrapper();
    const context = 'Hello world';
    render(
      <Wrapper>
        <MappingViewer
          mappingData={baseMappingData}
          initialContext={context}
          onRerunMapping={vi.fn()}
        />
      </Wrapper>,
    );

    expect(screen.getByText(`${context.length}/2000`)).toBeInTheDocument();
  });

  it('updates character count when typing', () => {
    const Wrapper = createWrapper();
    render(
      <Wrapper>
        <MappingViewer
          mappingData={baseMappingData}
          initialContext=""
          onRerunMapping={vi.fn()}
        />
      </Wrapper>,
    );

    // Initially 0/2000
    expect(screen.getByText('0/2000')).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText(/additional context/i);
    fireEvent.change(textarea, { target: { value: 'new context' } });

    expect(screen.getByText('11/2000')).toBeInTheDocument();
  });

  it('renders model selector with options from useBedrockModels', () => {
    const Wrapper = createWrapper();
    render(
      <Wrapper>
        <MappingViewer mappingData={baseMappingData} onRerunMapping={vi.fn()} />
      </Wrapper>,
    );

    // The Rerun Mapping section header should be present
    const headings = screen.getAllByText('Rerun Mapping');
    expect(headings.length).toBeGreaterThanOrEqual(1);

    // Verify the model selector placeholder is present (when no default selected)
    expect(screen.getByText(/Select model/)).toBeInTheDocument();
  });

  it('defaults model selector to pipeline model via defaultModelId', () => {
    const Wrapper = createWrapper();
    render(
      <Wrapper>
        <MappingViewer
          mappingData={baseMappingData}
          defaultModelId="amazon.nova-lite-v1:0"
          onRerunMapping={vi.fn()}
        />
      </Wrapper>,
    );

    // When defaultModelId matches a model, its name should appear as the selected option
    expect(screen.getByText('Amazon Nova Lite')).toBeInTheDocument();
  });

  it('does not render rerun section when onRerunMapping is not provided', () => {
    const Wrapper = createWrapper();
    render(
      <Wrapper>
        <MappingViewer mappingData={baseMappingData} />
      </Wrapper>,
    );

    expect(screen.queryByText('Rerun Mapping')).not.toBeInTheDocument();
    expect(screen.queryByText(/\/2000/)).not.toBeInTheDocument();
  });
});
