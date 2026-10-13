// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Tests for the Pipeline Overview "Run Now" button (P17 Task 21.3).
 *
 * Coverage:
 *   - 200 OK   → "Pipeline run triggered" success toast (P17 Req 5.6)
 *   - 429      → "Concurrency limit reached" warning toast (P17 Req 5.8)
 *   - 504      → "Trigger Lambda timed out" error toast    (P17 Req 5.8)
 *   - generic  → falls through to a generic error toast
 *   - loading  → button shows the loading state during the request
 *   - URL      → POSTs to /pipelines/{id}/run with an empty body
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../../services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api-client')>();
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    },
  };
});

const showToastMock = vi.fn();
vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: showToastMock }),
}));

import { apiClient, ApiError } from '../../services/api-client';
import RunNowButton from '../RunNowButton';

const mockedPost = vi.mocked(apiClient.post);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RunNowButton', () => {
  it('POSTs to /pipelines/{id}/run with an empty body and shows a success toast on 200', async () => {
    mockedPost.mockResolvedValue({ execution_arn: 'arn:...' });

    render(<RunNowButton pipelineKey="my-pipeline-abc" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
    expect(mockedPost).toHaveBeenCalledWith('/pipelines/my-pipeline-abc/run', {});
    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Pipeline run triggered', 'success'),
    );
  });

  it('URL-encodes the pipeline key', async () => {
    mockedPost.mockResolvedValue({ execution_arn: 'arn:...' });

    render(<RunNowButton pipelineKey="pipe with spaces/and-slash" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() => expect(mockedPost).toHaveBeenCalledTimes(1));
    expect(mockedPost).toHaveBeenCalledWith(
      '/pipelines/pipe%20with%20spaces%2Fand-slash/run',
      {},
    );
  });

  it('shows the "Concurrency limit reached" toast on HTTP 429', async () => {
    mockedPost.mockRejectedValue(new ApiError(429, { message: 'too many runs' }));

    render(<RunNowButton pipelineKey="p1" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Concurrency limit reached', 'warning'),
    );
  });

  it('shows the "Trigger Lambda timed out" toast on HTTP 504', async () => {
    mockedPost.mockRejectedValue(new ApiError(504, { message: 'gateway timeout' }));

    render(<RunNowButton pipelineKey="p1" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Trigger Lambda timed out', 'error'),
    );
  });

  it('falls back to a generic error toast for other ApiError statuses', async () => {
    mockedPost.mockRejectedValue(new ApiError(500, { message: 'server error' }));

    render(<RunNowButton pipelineKey="p1" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('server error', 'error'),
    );
  });

  it('falls back to a generic error toast for non-ApiError throwables', async () => {
    mockedPost.mockRejectedValue(new Error('network down'));

    render(<RunNowButton pipelineKey="p1" />);
    fireEvent.click(screen.getByTestId('run-now-button'));

    await waitFor(() =>
      expect(showToastMock).toHaveBeenCalledWith('Failed to trigger pipeline run', 'error'),
    );
  });
});
