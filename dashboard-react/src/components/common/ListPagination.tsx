// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Pagination from '@cloudscape-design/components/pagination';
import type { PaginationProps } from '@cloudscape-design/components/pagination';

/**
 * ListPagination — the shared list pager (P29 R3.3).
 *
 * Every list surface (ApprovalsPage, ExecutionsPage, PipelineListPage) renders
 * its pager through this ONE component so the "stranded on the last page" bug
 * is fixed in a single place.
 *
 * The bug it fixes: pages used to gate the pager on a per-page `has_more` flag
 * (`{has_more && <Pagination/>}`). Once a user reached the last page,
 * `has_more` became false, the pager UNMOUNTED, and there was no way back to
 * earlier pages. This component instead renders `<Pagination>` UNCONDITIONALLY
 * and lets Cloudscape disable the "next" arrow on the last page — the pager
 * stays mounted so "previous" is always reachable.
 *
 * For token/lazy-loaded lists where more pages may exist beyond what has been
 * fetched, pass `openEnd` (Cloudscape keeps "next" enabled and shows the "…"
 * affordance); the caller fetches the next page when `onChange` advances past
 * the currently known page count.
 */

export interface ListPaginationProps {
  /** 1-based index of the current page. */
  currentPageIndex: number;
  /** Number of known/fetched pages (clamped to a minimum of 1). */
  pagesCount: number;
  /** Invoked with the 1-based page index the user navigated to. */
  onChange: (pageIndex: number) => void;
  /**
   * Open-ended pagination: keep "next" enabled and show the lazy-load "…"
   * affordance because more pages may exist beyond `pagesCount` (e.g. behind a
   * server pagination token). Defaults to false (bounded list).
   */
  openEnd?: boolean;
  /** Disable all pager controls (e.g. while a page is loading). */
  disabled?: boolean;
  /** Override the default accessible labels. */
  ariaLabels?: PaginationProps['ariaLabels'];
}

const DEFAULT_ARIA_LABELS: PaginationProps['ariaLabels'] = {
  nextPageLabel: 'Next page',
  previousPageLabel: 'Previous page',
  pageLabel: (pageNumber: number) => `Page ${pageNumber}`,
};

export default function ListPagination({
  currentPageIndex,
  pagesCount,
  onChange,
  openEnd = false,
  disabled,
  ariaLabels = DEFAULT_ARIA_LABELS,
}: ListPaginationProps) {
  return (
    <Pagination
      currentPageIndex={currentPageIndex}
      // Cloudscape requires pagesCount >= 1. A bounded list with a single page
      // still renders the pager (next disabled), so the pager never vanishes.
      pagesCount={Math.max(pagesCount, 1)}
      openEnd={openEnd}
      disabled={disabled}
      ariaLabels={ariaLabels}
      onChange={({ detail }) => onChange(detail.currentPageIndex)}
    />
  );
}
