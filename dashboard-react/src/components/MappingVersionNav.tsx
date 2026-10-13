// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';

export interface MappingVersionNavProps {
  currentIndex: number;
  totalVersions: number;
  onPrevious: () => void;
  onNext: () => void;
}

export default function MappingVersionNav({
  currentIndex,
  totalVersions,
  onPrevious,
  onNext,
}: MappingVersionNavProps) {
  return (
    <div data-testid="version-nav">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Button
          iconName="angle-left"
          variant="icon"
          disabled={currentIndex === 0}
          onClick={onPrevious}
          data-testid="prev-button"
          ariaLabel="Previous version"
        />
        <Box variant="p" data-testid="version-counter">
          Version {currentIndex + 1} of {totalVersions}
        </Box>
        <Button
          iconName="angle-right"
          variant="icon"
          disabled={currentIndex === totalVersions - 1}
          onClick={onNext}
          data-testid="next-button"
          ariaLabel="Next version"
        />
      </SpaceBetween>
    </div>
  );
}
