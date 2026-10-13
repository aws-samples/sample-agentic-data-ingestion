// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * PipelineCard — `.json` uploads appear in Recent Files (p30 task 5.6 / 5.7).
 *
 * R13.6: JSON is a supported input format, so an uploaded `.json` file must
 * appear in file lists. `all_files` only ever holds uploaded source objects —
 * the API derives `file_key` from `original_s3_uri`, not the converted
 * `json_s3_uri` — so the extension filter was hiding real uploads.
 *
 * Scope: PipelineCard only. `FilesPage.test.tsx` already covers the page-level
 * clause ("lists JSON uploads instead of filtering them out"), so it is not
 * re-asserted here. PipelineCard's own repair under 5.6 was *subtractive* — the
 * property test's Property 5 dropped its JSON-exclusion clause and kept only
 * the "at most 3" limit, which a card that filtered every `.json` out would
 * still satisfy. These tests supply the positive assertion that was removed
 * rather than replaced.
 *
 * Requirements: 13.6, 13.7
 */
import { describe, it, expect } from 'vitest';
import { render, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import PipelineCard from '../PipelineCard';
import type { Pipeline, PipelineFile } from '../../types';

function file(key: string, processedAt: string): PipelineFile {
  return { file_key: key, pipeline_key: 'json-pipe', processed_at: processedAt };
}

function pipelineWith(files: PipelineFile[]): Pipeline {
  return {
    pipeline_key: 'json-pipe',
    pipeline_name: 'JSON Pipeline',
    status: 'active',
    source_schema: 'src',
    target_schema: 'tgt',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-06-01T00:00:00Z',
    file_count: files.length,
    mapping_count: 0,
    schema_version: 1,
    schema_history: [],
    all_files: files,
  };
}

function renderCard(files: PipelineFile[]) {
  const { container } = render(
    <MemoryRouter>
      <PipelineCard pipeline={pipelineWith(files)} />
    </MemoryRouter>,
  );
  return container;
}

describe('PipelineCard — JSON uploads are listed (R13.6)', () => {
  it('lists a .json upload alongside other formats in Recent Files', () => {
    const container = renderCard([
      file('uploads/orders.json', '2024-06-03T10:00:00Z'),
      file('uploads/customers.csv', '2024-06-02T10:00:00Z'),
    ]);

    const recent = within(container).getByTestId('recent-files');
    expect(recent.textContent).toContain('orders.json');
    expect(recent.textContent).toContain('customers.csv');
  });

  it('renders the Recent Files section when every upload is .json', () => {
    // Under the removed filter this card had no eligible files at all, so the
    // whole section was absent — the strongest form of "silently filtered out".
    const container = renderCard([
      file('uploads/a.json', '2024-06-03T10:00:00Z'),
      file('uploads/b.json', '2024-06-02T10:00:00Z'),
    ]);

    const recent = within(container).getByTestId('recent-files');
    expect(recent.textContent).toContain('a.json');
    expect(recent.textContent).toContain('b.json');
  });

  it('does not spend a .json upload\'s slot on an older non-JSON file', () => {
    // The 3-file limit is by recency only. With four uploads the newest three
    // win; a filter that skipped .json would promote the oldest .csv instead.
    const container = renderCard([
      file('uploads/newest.json', '2024-06-04T10:00:00Z'),
      file('uploads/second.json', '2024-06-03T10:00:00Z'),
      file('uploads/third.json', '2024-06-02T10:00:00Z'),
      file('uploads/oldest.csv', '2024-06-01T10:00:00Z'),
    ]);

    const recent = within(container).getByTestId('recent-files');
    expect(recent.textContent).toContain('newest.json');
    expect(recent.textContent).toContain('second.json');
    expect(recent.textContent).toContain('third.json');
    expect(recent.textContent).not.toContain('oldest.csv');
  });

  it('still renders no Recent Files section when there are no uploads', () => {
    const container = renderCard([]);
    expect(within(container).queryByTestId('recent-files')).toBeNull();
  });
});
