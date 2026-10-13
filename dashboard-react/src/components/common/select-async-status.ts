// SPDX-License-Identifier: MIT-0
/**
 * selectAsyncStatus — maps a React Query result to Cloudscape Select /
 * Multiselect async-status props (P29 R1.3).
 *
 * Resource fetchers no longer swallow errors into `[]` (P29 R1.2), so a failed
 * dropdown load must render the Async_State_Triple inline rather than showing
 * an empty option list. Cloudscape's native async-status props are the
 * preferred mechanism for dropdowns: when `statusType === 'error'` the
 * dropdown renders `errorText` plus the `recoveryText` recovery link, and
 * clicking that link fires `onLoadItems`, which re-triggers the query via
 * `refetch()`.
 *
 * Usage:
 *   const query = useS3TablesBuckets();
 *   <Select
 *     options={options}
 *     {...selectAsyncStatus(query, 'buckets', NO_S3_TABLE_BUCKETS)}
 *     {...selectFiltering('buckets', options.length)}
 *   />
 *
 * The return shape is intentionally structural (plain literal-union
 * `statusType`, `() => void` `onLoadItems`) so it spreads cleanly into both
 * `<Select>` and `<Multiselect>` without fighting Cloudscape's event-handler
 * generics.
 */
import type { ReactNode } from 'react';

/** Minimal React Query result shape consumed by {@link selectAsyncStatus}. */
export interface AsyncStatusQuery {
  isLoading: boolean;
  isError: boolean;
  refetch?: () => unknown;
}

/** Cloudscape Select/Multiselect async-status props produced by the helper. */
export interface AsyncSelectStatusProps {
  statusType: 'loading' | 'error' | 'finished';
  loadingText: string;
  errorText: string;
  recoveryText: string;
  onLoadItems: () => void;
  /** Present only when the caller passed an empty statement. */
  empty?: ReactNode;
}

/**
 * Build the Cloudscape async-status props for a dropdown backed by `query`.
 *
 * @param query React Query result (needs `isLoading`, `isError`, `refetch`).
 * @param noun  Human-readable noun for the loaded items, e.g. "buckets" —
 *              interpolated into the loading and error copy.
 * @param empty What the open dropdown says when the list loaded and holds
 *              nothing. The CALLER writes it, because only the caller knows
 *              what was searched and what the user can do next; the helper
 *              never invents it from `noun` (LGR-88b210b-1, whose revisit
 *              condition this parameter is). Without it, a finished empty list
 *              renders a blank popover that reads the same as a failure
 *              (LGR-2bcdd0d-3). Cloudscape shows `empty` only when the list is
 *              empty and `statusType` is `finished`, so it can never appear
 *              together with the loading or error text. Pass `undefined` while
 *              the dropdown is disabled for lack of a parent choice: a
 *              statement there would claim a search that never ran.
 */
export function selectAsyncStatus(
  query: AsyncStatusQuery,
  noun: string,
  empty?: ReactNode,
): AsyncSelectStatusProps {
  const statusType: AsyncSelectStatusProps['statusType'] = query.isError
    ? 'error'
    : query.isLoading
      ? 'loading'
      : 'finished';

  return {
    statusType,
    loadingText: `Loading ${noun}\u2026`,
    errorText: `Couldn't load ${noun}`,
    recoveryText: 'Retry',
    onLoadItems: () => {
      // Only the error-recovery link should re-trigger a fetch. Cloudscape
      // also fires onLoadItems on first open when statusType !== 'finished',
      // so guarding on isError keeps those calls no-ops.
      if (query.isError) query.refetch?.();
    },
    // Spread only when given, so an explicit `empty=` prop on the same
    // element is never overwritten with `undefined`.
    ...(empty !== undefined ? { empty } : {}),
  };
}

/** Cloudscape Select/Multiselect filtering props produced by {@link selectFiltering}. */
export interface SelectFilteringProps {
  filteringType: 'auto' | 'none';
  filteringPlaceholder?: string;
  filteringAriaLabel?: string;
  filteringClearAriaLabel?: string;
  noMatch?: ReactNode;
}

/**
 * Type-to-filter for a resource dropdown.
 *
 * `auto` filters on each option's label and description as the user types.
 * It is switched on only when the dropdown holds options: with nothing to
 * filter, a filter box above "nothing was found" or above the loading and
 * error text is a control that cannot do anything, which the empty states
 * deliberately avoid (`WizardStepTarget.glue-catalog.property.test.tsx`
 * asserts an empty dropdown offers nothing to click).
 *
 * `noMatch` is the one statement this helper writes itself. Unlike an empty
 * list, "the filter excluded everything" has the same meaning in every
 * dropdown, so it needs no caller-specific words.
 *
 * @param noun        Plural noun for the options, e.g. "secrets".
 * @param optionCount How many options the dropdown currently offers.
 */
export function selectFiltering(noun: string, optionCount: number): SelectFilteringProps {
  if (optionCount === 0) return { filteringType: 'none' };
  return {
    filteringType: 'auto',
    filteringPlaceholder: `Filter ${noun}`,
    filteringAriaLabel: `Filter ${noun}`,
    filteringClearAriaLabel: 'Clear filter',
    noMatch: `No ${noun} match the filter.`,
  };
}
