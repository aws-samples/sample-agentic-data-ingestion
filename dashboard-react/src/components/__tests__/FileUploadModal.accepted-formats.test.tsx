// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R6.6 — the Upload_Modal accepts **exactly** the Format_Registry's extension
 * set, and both readers of that set agree.
 *
 * The set is `FORMAT_MAP`'s keys in
 * `lambda/agentic_data_ingestion_file_validation/lambda_function.py` — eleven of
 * them as of p64's R3.1, which added `txt`: `csv tsv txt json jsonl ndjson parquet
 * pq avro orc xlsx`. It is
 * transcribed here as those dot-prefixed extensions **on purpose**: a TypeScript
 * test cannot import a Python dict either, so the cross-language guard is the
 * contract test `tests/contracts/test_format_vocabulary_cross_lambda.py`, which
 * AST-parses the producer and the modal and asserts set equality. That is where a
 * registry change fails. This file asserts the second half of R6.6 — that the two
 * places the set is *expressed to a user* both honour it.
 *
 * **Two readers, and they had to be checked separately.** `validateFile` turns a
 * rejected extension into a message; the input's `accept` attribute filters what
 * the picker offers. A set honoured by one and not the other means the picker
 * offers a file the validator then refuses, or hides a file the validator would
 * accept — which is the shape of the original defect in both directions.
 *
 * **What was hidden, and it is the larger half of R6.6.** The old literal was
 * `['.csv', '.json', '.parquet', '.tsv', '.xlsx']`. `.xlsx` was the case
 * `requirements.md` opens with — *"frontend accepts what the backend rejects"* —
 * and W3 made it true. The five that were **missing** are the mirror image and
 * were never anyone's defect to fix: `.jsonl`, `.ndjson`, `.pq`, `.avro` and
 * `.orc` are all in `FORMAT_MAP`, all supported end to end, and were all refused
 * by this modal. A user with an Avro file was told the format was unsupported by
 * a picker rather than by the system.
 *
 * **Validates: Requirements 6.6**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import FileUploadModal from '../FileUploadModal';
import type { Pipeline } from '../../types';

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../services/api-client', () => ({
  apiClient: { post: vi.fn() },
}));

/**
 * The Format_Registry's extensions, dot-prefixed — `FORMAT_MAP`'s keys, in that
 * literal's order. Held together with the Python side by
 * `tests/contracts/test_format_vocabulary_cross_lambda.py`, not by this file.
 *
 * `.txt` joined in p64 1c.1 (R3.1). It moved OUT of the refusal list below in the
 * same change, which is the pair a reader should check together: a registry entry
 * that is not removed from the refusal list makes two tests contradict each other
 * over the same extension.
 */
const REGISTRY_EXTENSIONS = [
  '.csv',
  '.tsv',
  '.txt',
  '.json',
  '.jsonl',
  '.ndjson',
  '.parquet',
  '.pq',
  '.avro',
  '.orc',
  '.xlsx',
];

/** The five p55 exposed. Named separately because they are the point of R6.6. */
const PREVIOUSLY_HIDDEN = ['.jsonl', '.ndjson', '.pq', '.avro', '.orc'];

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipeline',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: '',
  target_schema: '',
  created_at: '2026-08-30T00:00:00Z',
  updated_at: '2026-08-30T00:00:00Z',
  target_type: 's3_tables',
};

const defaultProps = { visible: true, onDismiss: vi.fn(), pipeline: mockPipeline };

function createMockFile(name: string, size = 1024): File {
  return new File([new ArrayBuffer(size)], name, { type: 'application/octet-stream' });
}

function selectFile(file: File) {
  fireEvent.change(screen.getByTestId('file-input'), { target: { files: [file] } });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the picker offers exactly the Format_Registry set', () => {
  it("lists every registry extension in the input's accept attribute, and nothing else", () => {
    render(<FileUploadModal {...defaultProps} />);
    const input = screen.getByTestId('file-input') as HTMLInputElement;

    const offered = input
      .getAttribute('accept')!
      .split(',')
      .map((entry) => entry.trim())
      .filter((entry) => entry.length > 0);

    // Set equality, both directions. A superset offers what the backend rejects;
    // a subset hides what it supports. R6.6 says "exactly".
    expect(new Set(offered)).toEqual(new Set(REGISTRY_EXTENSIONS));
  });

  it('offers the five extensions that were supported and hidden', () => {
    render(<FileUploadModal {...defaultProps} />);
    const accept = screen.getByTestId('file-input').getAttribute('accept')!;

    for (const extension of PREVIOUSLY_HIDDEN) {
      expect(accept).toContain(extension);
    }
  });
});

describe('the validator accepts exactly the Format_Registry set', () => {
  it.each(REGISTRY_EXTENSIONS)('accepts a %s file', (extension) => {
    render(<FileUploadModal {...defaultProps} />);
    selectFile(createMockFile(`data${extension}`));

    expect(screen.queryByTestId('validation-error')).not.toBeInTheDocument();
  });

  it('accepts an uppercase extension, because the validator lower-cases before matching', () => {
    // Not a new tolerance: `getFileExtension` already lower-cased. Asserted because
    // a set written in lower case would otherwise look like it required one.
    render(<FileUploadModal {...defaultProps} />);
    selectFile(createMockFile('DATA.XLSX'));

    expect(screen.queryByTestId('validation-error')).not.toBeInTheDocument();
  });

  it.each(['.png', '.xls', '.xlsm', '.zip', ''])(
    'still refuses %s, which the Format_Registry does not contain',
    (extension) => {
      // `.xls` and `.xlsm` are the honest cases here: `FORMAT_MAP`'s `xlsx` entry
      // maps to the Canonical_Format `"excel"`, so a future `.xls` key would need
      // no new value — but the key does not exist, so the modal must not offer it.
      // Accepting a format the registry does not contain is the original defect
      // with a different extension.
      //
      // `.txt` was in this list until p64 1c.1 and is now in REGISTRY_EXTENSIONS
      // above (R3.1). Removed rather than left here: R3.1 makes it a supported
      // pipe-delimited member of the `csv` family, so refusing it would be the
      // hidden-format half of the R6.6 defect reappearing on a new extension.
      render(<FileUploadModal {...defaultProps} />);
      selectFile(createMockFile(`data${extension}`));

      expect(screen.getByTestId('validation-error')).toBeInTheDocument();
    },
  );

  it('names the accepted set in the refusal, so the message is actionable', () => {
    render(<FileUploadModal {...defaultProps} />);
    selectFile(createMockFile('image.png'));

    const message = screen.getByTestId('validation-error').textContent!;
    for (const extension of REGISTRY_EXTENSIONS) {
      expect(message).toContain(extension);
    }
  });
});
