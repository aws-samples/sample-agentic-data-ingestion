// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { ReactNode } from 'react';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Popover from '@cloudscape-design/components/popover';

/**
 * A question-mark button that opens explanatory detail for a tile or a
 * section, so the explanation is one click away rather than printed under it.
 *
 * Rendered through a portal, so the open popover's text is never inside the
 * element it explains: a tile's label and figure, or a section's header, read
 * the same whether or not the detail is open. `testId` goes on the content,
 * which exists only while the popover is open.
 *
 * For explanation only. A statement that something went wrong (a lost file, a
 * failed read) stays on the page, where it is seen without being looked for.
 */
export default function DetailPopover({
  header,
  ariaLabel,
  testId,
  children,
}: {
  header: string;
  ariaLabel: string;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <Popover
      header={header}
      triggerType="custom"
      size="large"
      position="top"
      renderWithPortal
      dismissAriaLabel="Close"
      content={<Box data-testid={testId}>{children}</Box>}
    >
      <Button variant="inline-icon" iconName="support" ariaLabel={ariaLabel} />
    </Popover>
  );
}
