// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';

/**
 * CursorPagination — Previous / Next for a keyed (cursor) list (p48 C2 Arm A).
 *
 * WHY THIS IS NOT `ListPagination`
 * -------------------------------
 * `ListPagination` wraps Cloudscape `<Pagination>`, whose entire model is
 * *numbered* pages: it renders a clickable button per page and reports the index
 * the user jumped to. That is the right control for its two existing consumers —
 * `ApprovalsPage` and `PipelineListPage` both page by offset and can honour a
 * jump to page 5 — and it is the wrong control here. DynamoDB has no offset
 * addressing, so under C2 Arm A the only reachable pages are "the one after this
 * key" and "the ones already visited". A numbered pager would offer jumps this
 * data source cannot serve.
 *
 * *Rejected: adding a mode to `ListPagination`.* Cloudscape `<Pagination>` has no
 * prop that hides the page numbers, so "cursor mode" would mean not rendering
 * that component at all — a second component inside the first, sharing only a
 * name. *Rejected: keeping `ListPagination` and clamping out-of-range jumps.*
 * That renders a control which advertises page 7 and silently does something
 * else, which is the class of quiet wrongness this spec opened on.
 *
 * THE FENCE THIS CARRIES FORWARD
 * ------------------------------
 * `ListPagination.tsx:11-16` records a real past bug: pages gated the pager's
 * **visibility** on a per-page `has_more`, so on the last page the control
 * unmounted and there was no way back to earlier pages. The fix was to render
 * unconditionally and let Cloudscape *disable* the next arrow. That principle is
 * preserved here and is why both buttons are always mounted and only ever
 * `disabled` — never conditionally absent. The caller's own visibility gate is
 * `has_next || has_prev`, which is a different predicate from the one that caused
 * the bug: it stays true on the last page whenever a previous page exists, so it
 * cannot strand anyone. See `ExecutionsPage.tsx` for the single-page case.
 *
 * STATE IS IN WORDS, NOT ONLY IN STYLING
 * --------------------------------------
 * `LGR-99050e6-1` (**template / ratified**) records that conveying state by
 * colour alone fails WCAG 1.4.1. The position is therefore a text label, and
 * "there is no further page" is carried by the buttons' `disabled` state — which
 * assistive technology reports — rather than by their appearance.
 */
export interface CursorPaginationProps {
  /**
   * 1-based label for the page on screen. Read from the response
   * (`pagination.page`), not counted locally: the producer derives it from the
   * cursor so the number and the rows cannot disagree.
   */
  page: number;
  /**
   * `pagination.total_pages`, read from the wire rather than recomputed from a
   * count and a page size. Recomputing would make the frontend a second
   * authority on the page count (R10.4's argument, one field over).
   */
  totalPages: number;
  /**
   * True when `pagination.partial_counts` names `total_pages` — the count walk
   * hit a bound, so `totalPages` is a **floor** and must not be rendered as a
   * total (R4.3).
   */
  totalPagesIsFloor?: boolean;
  /** `pagination.has_next`. Under a status filter this is an upper bound — see `ExecutionsPage`. */
  hasNext: boolean;
  /** `pagination.has_prev`. */
  hasPrev: boolean;
  onNext: () => void;
  onPrevious: () => void;
  /** Disable both controls, e.g. while the next page is in flight. */
  disabled?: boolean;
}

/**
 * "Page 2 of 3", or "Page 2 of at least 3" when the count is a floor.
 *
 * When the floor reads *below* the page on screen, the floor is widened to the
 * page number. That is not the `total_pages: 1` defect wearing a new number: the
 * old literal claimed to be exact, whereas both operands here are genuine lower
 * bounds — being on page N is itself proof that N pages exist — and the "at
 * least" wording keeps the claim true of the wider one. An exact count can never
 * fall below the current page, so the widening is unreachable on that path.
 */
export function describePagePosition(
  page: number,
  totalPages: number,
  totalPagesIsFloor: boolean,
): string {
  if (totalPagesIsFloor) {
    return `Page ${page} of at least ${Math.max(totalPages, page)}`;
  }
  // A total below the current page cannot happen; a total of 0 means the caller
  // rendered this control over an empty set, where the page number is the only
  // honest thing to say.
  if (totalPages < page) return `Page ${page}`;
  return `Page ${page} of ${totalPages}`;
}

export default function CursorPagination({
  page,
  totalPages,
  totalPagesIsFloor = false,
  hasNext,
  hasPrev,
  onNext,
  onPrevious,
  disabled = false,
}: CursorPaginationProps) {
  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      <Button
        iconName="angle-left"
        onClick={onPrevious}
        // Always mounted, only disabled — the `ListPagination.tsx:11-16` fence.
        disabled={disabled || !hasPrev}
        ariaLabel="Previous page"
        data-testid="cursor-pagination-previous"
      >
        Previous
      </Button>
      <Box variant="small" color="text-body-secondary" data-testid="cursor-pagination-position">
        {describePagePosition(page, totalPages, totalPagesIsFloor)}
      </Box>
      <Button
        iconName="angle-right"
        iconAlign="right"
        onClick={onNext}
        disabled={disabled || !hasNext}
        ariaLabel="Next page"
        data-testid="cursor-pagination-next"
      >
        Next
      </Button>
    </SpaceBetween>
  );
}
