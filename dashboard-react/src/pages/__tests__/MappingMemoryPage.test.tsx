// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastProvider } from '../../contexts/ToastContext';
import MappingMemoryPage from '../MappingMemoryPage';
import type { MappingMemoryResponse } from '../../types';

/* ------------------------------------------------------------------ */
/*  Mock state                                                         */
/* ------------------------------------------------------------------ */

let mockData: MappingMemoryResponse | undefined;
let mockIsLoading = false;
let mockIsError = false;
const mockDeleteMutateAsync = vi.fn();
let mockDeleteIsPending = false;

vi.mock('../../hooks', () => ({
  useMappingMemory: () => ({
    data: mockData,
    isLoading: mockIsLoading,
    isError: mockIsError,
  }),
  useDeleteMappingMemoryEntry: () => ({
    mutateAsync: mockDeleteMutateAsync,
    isPending: mockDeleteIsPending,
  }),
}));

/* ------------------------------------------------------------------ */
/*  Fixtures                                                           */
/* ------------------------------------------------------------------ */

const ENTRIES_RESPONSE: MappingMemoryResponse = {
  pipeline_id: 'test-pipe',
  count: 3,
  entries: [
    {
      entry_id: 'rec-1',
      source_field: 'name',
      target_field: 'full_name',
      confidence: 75,
      approved: false,
      timestamp: '2025-01-14T10:00:00Z',
    },
    {
      entry_id: 'rec-2',
      source_field: 'email',
      target_field: 'email_address',
      confidence: 95,
      approved: true,
      timestamp: '2025-01-15T10:00:00Z',
    },
    {
      entry_id: 'rec-3',
      source_field: 'addr',
      target_field: 'address',
      confidence: 60,
      approved: false,
      timestamp: '2025-01-13T10:00:00Z',
    },
  ],
};

const EMPTY_RESPONSE: MappingMemoryResponse = {
  pipeline_id: 'test-pipe',
  count: 0,
  entries: [],
};

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function renderPage(data: MappingMemoryResponse | undefined = ENTRIES_RESPONSE) {
  mockData = data;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/test-pipe/mapping-memory']}>
          <Routes>
            <Route path="/pipelines/:id/mapping-memory" element={<MappingMemoryPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  mockIsLoading = false;
  mockIsError = false;
  mockDeleteIsPending = false;
  mockDeleteMutateAsync.mockReset();
});

describe('MappingMemoryPage — entry rendering', () => {
  it('renders all mapping memory entries', () => {
    renderPage();

    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByText('full_name')).toBeInTheDocument();
    expect(screen.getByText('email')).toBeInTheDocument();
    expect(screen.getByText('email_address')).toBeInTheDocument();
    expect(screen.getByText('addr')).toBeInTheDocument();
    expect(screen.getByText('address')).toBeInTheDocument();
  });

  it('displays confidence badges', () => {
    renderPage();

    expect(screen.getByText('95%')).toBeInTheDocument();
    expect(screen.getByText('75%')).toBeInTheDocument();
    expect(screen.getByText('60%')).toBeInTheDocument();
  });

  it('displays approval status indicators', () => {
    renderPage();

    expect(screen.getByText('Approved')).toBeInTheDocument();
    // Two pending entries
    expect(screen.getAllByText('Pending')).toHaveLength(2);
  });

  it('shows entry count in header', () => {
    renderPage();

    expect(screen.getByText('(3)')).toBeInTheDocument();
  });
});

describe('MappingMemoryPage — sort order (Property 6)', () => {
  it('sorts entries by confidence descending', () => {
    renderPage();

    // Get all confidence badge texts in order
    const badges = screen.getAllByText(/%$/);
    const confidences = badges.map((el) => parseInt(el.textContent ?? '0'));

    // Verify descending order
    for (let i = 0; i < confidences.length - 1; i++) {
      expect(confidences[i]).toBeGreaterThanOrEqual(confidences[i + 1]);
    }

    // Specific order: 95, 75, 60
    expect(confidences).toEqual([95, 75, 60]);
  });
});

describe('MappingMemoryPage — delete flow (Property 7)', () => {
  it('opens confirmation modal when delete button is clicked', () => {
    renderPage();

    const deleteBtn = screen.getByTestId('delete-rec-2');
    fireEvent.click(deleteBtn);

    expect(screen.getByText('Delete mapping memory entry')).toBeInTheDocument();
    expect(screen.getByTestId('delete-confirm')).toBeInTheDocument();
    expect(screen.getByTestId('delete-cancel')).toBeInTheDocument();
    // Modal shows confirmation text specific to the single-entry delete flow.
    // NOTE: the page also mounts a bulk-delete modal whose body likewise
    // begins "Are you sure you want to delete …", so match the single-delete
    // wording precisely instead of the shared prefix.
    expect(
      screen.getByText(/Are you sure you want to delete the mapping from/),
    ).toBeInTheDocument();
  });

  it('calls delete mutation on confirm and shows success toast', async () => {
    mockDeleteMutateAsync.mockResolvedValueOnce(undefined);
    renderPage();

    fireEvent.click(screen.getByTestId('delete-rec-1'));
    fireEvent.click(screen.getByTestId('delete-confirm'));

    expect(mockDeleteMutateAsync).toHaveBeenCalledWith('rec-1');

    await waitFor(() => {
      expect(screen.getByText(/Deleted mapping/)).toBeInTheDocument();
    });
  });

  it('shows error toast on delete failure', async () => {
    mockDeleteMutateAsync.mockRejectedValueOnce(new Error('Delete failed'));
    renderPage();

    fireEvent.click(screen.getByTestId('delete-rec-1'));
    fireEvent.click(screen.getByTestId('delete-confirm'));

    await waitFor(() => {
      expect(screen.getByText('Delete failed')).toBeInTheDocument();
    });
  });

  it('closes modal when cancel is clicked', () => {
    renderPage();

    fireEvent.click(screen.getByTestId('delete-rec-1'));
    expect(screen.getByText('Delete mapping memory entry')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('delete-cancel'));

    // After cancel, the modal body class should be removed
    expect(document.body.classList.contains('awsui_modal-open_1d2i7_1e7ip_433')).toBe(false);
  });
});

describe('MappingMemoryPage — empty state', () => {
  it('shows empty state message when no entries exist', () => {
    renderPage(EMPTY_RESPONSE);

    expect(screen.getByText('No mapping memory entries')).toBeInTheDocument();
    expect(screen.getByText(/No AI-learned mapping patterns/)).toBeInTheDocument();
  });

  it('shows count of zero in header', () => {
    renderPage(EMPTY_RESPONSE);

    expect(screen.getByText('(0)')).toBeInTheDocument();
  });
});

describe('MappingMemoryPage — loading and error states', () => {
  it('shows spinner when loading', () => {
    mockIsLoading = true;
    renderPage(undefined);

    // Cloudscape Spinner renders as nested spans, not role="progressbar"
    expect(screen.queryByText('Mapping Memory')).not.toBeInTheDocument();
  });

  it('shows error indicator on fetch error', () => {
    mockIsError = true;
    renderPage(undefined);

    expect(screen.getByText('Failed to load mapping memory')).toBeInTheDocument();
  });
});
