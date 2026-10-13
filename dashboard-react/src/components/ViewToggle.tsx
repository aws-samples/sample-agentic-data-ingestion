// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SegmentedControl from '@cloudscape-design/components/segmented-control';

export interface ViewToggleProps {
  view: 'table' | 'cards';
  onChange: (view: 'table' | 'cards') => void;
}

const VIEW_OPTIONS = [
  { id: 'table', text: 'Table' },
  { id: 'cards', text: 'Cards' },
];

export default function ViewToggle({ view, onChange }: ViewToggleProps) {
  return (
    <SegmentedControl
      selectedId={view}
      onChange={({ detail }) => onChange(detail.selectedId as 'table' | 'cards')}
      label="View mode"
      options={VIEW_OPTIONS}
    />
  );
}
