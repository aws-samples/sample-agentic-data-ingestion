// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The cancel hook hands its caller the server's answer, and the two toasts say
 * what actually happened.
 *
 * `POST /runs/{runId}/cancel` answers 200 `CANCELLED` when the run was stopped,
 * and 202 `CANCELLING` when its Glue job is being stopped and the run is not
 * cancelled yet. Until 2026-10-11 the hook discarded the body and both callers
 * toasted "Run cancelled" for any 2xx, and "Failed to cancel run" with no reason
 * for any error, including a 409 for a run that had already finished.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const post = vi.fn();
vi.mock('../../services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../services/api-client')>();
  return { ...actual, apiClient: { post: (...args: unknown[]) => post(...args) } };
});

import { ApiError } from '../../services/api-client';
import { cancelFailureMessage, cancelOutcomeToast, useCancelRun, type CancelRunResponse } from '../use-cancel-run';

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return React.createElement(QueryClientProvider, { client: queryClient }, children);
}

const cancelling: CancelRunResponse = {
  success: true,
  run_id: 'run-1',
  status: 'CANCELLING',
  message: 'Stopping the Glue job; the run becomes CANCELLED when it has stopped',
};

describe('useCancelRun', () => {
  beforeEach(() => post.mockReset());

  it('posts to the run and resolves with the server body', async () => {
    post.mockResolvedValue(cancelling);
    const { result } = renderHook(() => useCancelRun(), { wrapper });

    const response = await result.current.mutateAsync('run-1');

    expect(post).toHaveBeenCalledWith('/runs/run-1/cancel', {});
    expect(response).toEqual(cancelling);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });
});

describe('cancelOutcomeToast', () => {
  it('does not call a run that is still stopping cancelled', () => {
    const toast = cancelOutcomeToast(cancelling);
    expect(toast.type).toBe('info');
    expect(toast.message).not.toMatch(/^Run cancelled/);
    expect(toast.message).toContain('CANCELLED once');
  });

  it('confirms a run that was stopped', () => {
    expect(cancelOutcomeToast({ ...cancelling, status: 'CANCELLED', message: 'Run cancelled' })).toEqual({
      message: 'Run cancelled',
      type: 'success',
    });
  });
});

describe('cancelFailureMessage', () => {
  it("carries the server's reason for a refused cancel", () => {
    const error = new ApiError(409, {
      error: 'Run is not active (status SUCCEEDED); nothing to cancel',
      run_id: 'run-1',
      status: 'SUCCEEDED',
    });
    expect(cancelFailureMessage(error)).toBe(
      'Failed to cancel run: Run is not active (status SUCCEEDED); nothing to cancel',
    );
  });

  it('falls back to a plain sentence when there is no reason', () => {
    expect(cancelFailureMessage('boom')).toBe('Failed to cancel run');
  });
});
