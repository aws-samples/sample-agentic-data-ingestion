// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';

/**
 * AsyncContent — the Async_State_Triple primitive (P29 R1).
 *
 * Wraps a React Query result and renders exactly one of three states so that a
 * failure never masquerades as "empty":
 *   - loading  → spinner
 *   - error    → Alert with the error message + a Retry button (query.refetch)
 *   - empty    → the caller-provided `empty` node (when data is empty)
 *   - else     → children(data) render-prop
 *
 * Usage:
 *   <AsyncContent query={query} empty={<EmptyState … />} errorTitle="Couldn't load executions">
 *     {(data) => <Table items={data} … />}
 *   </AsyncContent>
 */

/**
 * Minimal structural shape of a React Query result consumed by AsyncContent.
 *
 * Kept structural (rather than the full `UseQueryResult` generic) so any
 * query-like object can be wrapped without fighting React Query's discriminated
 * union — a real `UseQueryResult<TData>` is structurally assignable to this.
 */
export interface AsyncContentQuery<TData> {
  data: TData | undefined;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
  refetch?: () => unknown;
}

export interface AsyncContentProps<TData> {
  /** The React Query result (or any query-like object) to render. */
  query: AsyncContentQuery<TData>;
  /** Render-prop invoked with the resolved, non-empty data. */
  children: (data: TData) => ReactNode;
  /**
   * Rendered when the resolved data is considered empty. When omitted, the
   * children render-prop is used even for empty data (e.g. a Table that shows
   * its own empty slot).
   */
  empty?: ReactNode;
  /** Header text for the error Alert. */
  errorTitle?: string;
  /**
   * Custom emptiness predicate. Defaults to treating `null`/`undefined` and
   * zero-length arrays as empty.
   */
  isEmpty?: (data: TData) => boolean;
  /** Text shown beneath the loading spinner. */
  loadingText?: string;
}

/** Extracts a human-readable message from an unknown React Query error. */
function extractErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'string' && error.trim()) return error;
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  return 'An unexpected error occurred.';
}

function defaultIsEmpty<TData>(data: TData): boolean {
  if (data == null) return true;
  if (Array.isArray(data)) return data.length === 0;
  return false;
}

export default function AsyncContent<TData>({
  query,
  children,
  empty,
  errorTitle = 'Something went wrong',
  isEmpty = defaultIsEmpty,
  loadingText = 'Loading…',
}: AsyncContentProps<TData>) {
  if (query.isLoading) {
    return (
      <Box textAlign="center" padding="l" data-testid="async-content-loading">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary">
          {loadingText}
        </Box>
      </Box>
    );
  }

  if (query.isError) {
    return (
      <Alert
        type="error"
        header={errorTitle}
        data-testid="async-content-error"
        action={
          query.refetch ? (
            <Button onClick={() => query.refetch?.()} data-testid="async-content-retry">
              Retry
            </Button>
          ) : undefined
        }
      >
        {extractErrorMessage(query.error)}
      </Alert>
    );
  }

  const { data } = query;

  if (empty !== undefined && (data === undefined || isEmpty(data))) {
    return <>{empty}</>;
  }

  // Not loading, not error, no data, and no empty slot provided: render nothing
  // rather than crashing the children render-prop with undefined.
  if (data === undefined) {
    return null;
  }

  return <>{children(data)}</>;
}
