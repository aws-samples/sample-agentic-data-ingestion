// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SegmentedControl from '@cloudscape-design/components/segmented-control';

export interface PipelineSortControlsProps {
  currentSort: 'name' | 'activity' | 'files';
  onSortChange: (sort: 'name' | 'activity' | 'files') => void;
}

const SORT_OPTIONS = [
  { id: 'name', text: 'Name' },
  { id: 'activity', text: 'Activity' },
  { id: 'files', text: 'File count' },
];

export default function PipelineSortControls({ currentSort, onSortChange }: PipelineSortControlsProps) {
  return (
    <SegmentedControl
      selectedId={currentSort}
      onChange={({ detail }) => onSortChange(detail.selectedId as 'name' | 'activity' | 'files')}
      label="Sort pipelines"
      options={SORT_OPTIONS}
    />
  );
}
