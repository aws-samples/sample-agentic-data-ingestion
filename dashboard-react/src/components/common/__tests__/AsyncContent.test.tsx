// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for AsyncContent — the Async_State_Triple primitive (P29 R1).
 *
 * Validates: Requirements 1.1
 *
 * Covers: loading renders a spinner; error renders an error + Retry (and Retry
 * calls refetch); empty renders the provided empty copy; data renders children.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import AsyncContent, { type AsyncContentQuery } from '../AsyncContent';

/** Builds a query-like object with sane defaults for the fields under test. */
function makeQuery<TData>(overrides: Partial<AsyncContentQuery<TData>>): AsyncContentQuery<TData> {
  return {
    data: undefined,
    isLoading: false,
    isError: false,
    error: undefined,
    refetch: vi.fn(),
    ...overrides,
  };
}

describe('AsyncContent', () => {
  it('renders a loading spinner while the query is loading', () => {
    const query = makeQuery<string[]>({ isLoading: true });

    render(
      <AsyncContent query={query} loadingText="Loading executions…">
        {(data) => <div>{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('async-content-loading')).toBeInTheDocument();
    expect(screen.getByText('Loading executions…')).toBeInTheDocument();
    // The render-prop must NOT run while loading.
    expect(screen.queryByTestId('async-content-error')).not.toBeInTheDocument();
  });

  it('renders an error alert with the message and a Retry button', () => {
    const query = makeQuery<string[]>({
      isError: true,
      error: new Error('Boom: API is down'),
    });

    render(
      <AsyncContent query={query} errorTitle="Couldn't load executions">
        {(data) => <div>{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('async-content-error')).toBeInTheDocument();
    expect(screen.getByText("Couldn't load executions")).toBeInTheDocument();
    expect(screen.getByText('Boom: API is down')).toBeInTheDocument();
    expect(screen.getByTestId('async-content-retry')).toBeInTheDocument();
  });

  it('calls query.refetch when Retry is clicked', () => {
    const refetch = vi.fn();
    const query = makeQuery<string[]>({
      isError: true,
      error: new Error('nope'),
      refetch,
    });

    render(
      <AsyncContent query={query} errorTitle="Failed">
        {(data) => <div>{data.join(',')}</div>}
      </AsyncContent>,
    );

    fireEvent.click(screen.getByTestId('async-content-retry'));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('falls back to a generic message when the error has none', () => {
    const query = makeQuery<string[]>({ isError: true, error: {} });

    render(
      <AsyncContent query={query} errorTitle="Failed">
        {(data) => <div>{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByText('An unexpected error occurred.')).toBeInTheDocument();
  });

  it('omits the Retry button when no refetch is provided', () => {
    const query = makeQuery<string[]>({ isError: true, error: 'x', refetch: undefined });

    render(
      <AsyncContent query={query} errorTitle="Failed">
        {(data) => <div>{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.queryByTestId('async-content-retry')).not.toBeInTheDocument();
  });

  it('renders the empty node for an empty array (never as an error)', () => {
    const query = makeQuery<string[]>({ data: [] });

    render(
      <AsyncContent query={query} empty={<div data-testid="empty-copy">No executions yet</div>}>
        {(data) => <div data-testid="rows">{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('empty-copy')).toBeInTheDocument();
    expect(screen.getByText('No executions yet')).toBeInTheDocument();
    expect(screen.queryByTestId('rows')).not.toBeInTheDocument();
  });

  it('renders the empty node when data is undefined and an empty slot is given', () => {
    const query = makeQuery<string[]>({ data: undefined });

    render(
      <AsyncContent query={query} empty={<div data-testid="empty-copy">Nothing</div>}>
        {(data) => <div data-testid="rows">{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('empty-copy')).toBeInTheDocument();
  });

  it('renders children with the resolved data on success', () => {
    const query = makeQuery<string[]>({ data: ['a', 'b', 'c'] });

    render(
      <AsyncContent query={query} empty={<div>empty</div>}>
        {(data) => <div data-testid="rows">{data.join(',')}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('rows')).toHaveTextContent('a,b,c');
  });

  it('supports a custom isEmpty predicate for non-array data', () => {
    const query = makeQuery<{ items: number[] }>({ data: { items: [] } });

    render(
      <AsyncContent
        query={query}
        empty={<div data-testid="empty-copy">empty object</div>}
        isEmpty={(d) => d.items.length === 0}
      >
        {(data) => <div data-testid="rows">{data.items.length}</div>}
      </AsyncContent>,
    );

    expect(screen.getByTestId('empty-copy')).toBeInTheDocument();
  });

  it('renders children for empty data when no empty slot is provided', () => {
    const query = makeQuery<string[]>({ data: [] });

    render(
      <AsyncContent query={query}>
        {(data) => <div data-testid="rows">count:{data.length}</div>}
      </AsyncContent>,
    );

    // Falls through to children so a Table can show its own empty state.
    expect(screen.getByTestId('rows')).toHaveTextContent('count:0');
  });
});
