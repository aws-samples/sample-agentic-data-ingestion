// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import fc from 'fast-check';
import FileUploadModal from '../FileUploadModal';
import type { Pipeline } from '../../types';

/**
 * p64 hotfix — the landing confirmation targets a URL the **server signed**, for
 * every presign response, never one the client composed.
 *
 * **Validates: Requirements R4.4**
 *
 * This is the property task 1d.5 violated. It fetched `presign.url + '/' + key`
 * anonymously; the source bucket is `BlockPublicAccess.BLOCK_ALL`
 * (`foundation-stack.ts:64`) with no public policy in `deploy-cdk/lib/`, so that
 * request returned 403 for every object whether or not it existed, and every upload
 * reported failure. The bucket's CORS allowing `HEAD` was the justification given
 * and it authorizes nothing — CORS decides what JavaScript may *read*, not what S3
 * will *serve*.
 *
 * Why a property test and not another example. 1d.5's examples all mocked `fetch`
 * and asserted the response handling, so they could not observe a request that was
 * unauthorized by construction — they passed against the broken code. Quantifying
 * over presign URLs and keys removes the escape hatch where one particular URL
 * happens not to be composed: the assertion is that the fetched URL **is** the
 * server's `confirm_url` and is **never** the concatenation, whatever the inputs.
 */

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

vi.mock('../../services/api-client', () => ({
  apiClient: { post: vi.fn() },
}));

import { apiClient } from '../../services/api-client';

const mockPipeline: Pipeline = {
  pipeline_key: 'test-pipeline',
  pipeline_name: 'Test Pipeline',
  status: 'active',
  source_schema: '',
  target_schema: '',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  target_type: 's3_tables',
};

interface MockXhr {
  open: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  abort: ReturnType<typeof vi.fn>;
  upload: { onprogress: ((e: unknown) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  onabort: (() => void) | null;
  status: number;
}

function stubXhr(): { current: MockXhr | null } {
  const ref: { current: MockXhr | null } = { current: null };
  vi.stubGlobal(
    'XMLHttpRequest',
    vi.fn().mockImplementation(function () {
      const xhr: MockXhr = {
        open: vi.fn(),
        send: vi.fn(),
        abort: vi.fn(),
        upload: { onprogress: null },
        onload: null,
        onerror: null,
        onabort: null,
        status: 204,
      };
      ref.current = xhr;
      return xhr;
    })
  );
  return ref;
}

/**
 * Generators constrained to the input space the server actually produces.
 *
 * `generate_presigned_post` returns a bucket endpoint in one of two addressing
 * styles, with or without a trailing slash — the two shapes the deleted
 * `objectUrlFor` had to normalise, kept here so the property covers the cases that
 * motivated it. Keys are always `{pipeline_id}/{file_name}`
 * (`handlers/upload.py`), and the filename half may contain spaces, which is what
 * made the old encoding logic non-trivial.
 */
const presignUrlArb = fc
  .tuple(
    fc.constantFrom(
      'https://s3.us-west-2.amazonaws.com/adi-source-files-123456789012-us-west-2',
      'https://adi-source-files-123456789012-us-west-2.s3.us-west-2.amazonaws.com',
      'https://adi-source-files-123456789012-us-west-2.s3.amazonaws.com'
    ),
    fc.boolean()
  )
  .map(([base, trailingSlash]) => (trailingSlash ? `${base}/` : base));

const segmentArb = fc
  .stringMatching(/^[A-Za-z0-9 _-]{1,12}$/)
  .filter((s) => s.trim().length > 0);

const keyArb = fc
  .tuple(segmentArb, segmentArb, fc.constantFrom('csv', 'json', 'parquet', 'txt'))
  .map(([pipeline, file, ext]) => `${pipeline}/${file}.${ext}`);

/** A SigV4 query-signed HEAD URL, as `_confirmation_url` returns. */
const confirmUrlArb = fc
  .tuple(presignUrlArb, keyArb, fc.hexaString({ minLength: 64, maxLength: 64 }))
  .map(([base, key, sig]) => {
    const root = base.endsWith('/') ? base.slice(0, -1) : base;
    return (
      `${root}/${key.split('/').map(encodeURIComponent).join('/')}` +
      `?X-Amz-Algorithm=AWS4-HMAC-SHA256` +
      `&X-Amz-Credential=ASIAEXAMPLE%2F20260915%2Fus-west-2%2Fs3%2Faws4_request` +
      `&X-Amz-Date=20260915T164115Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host` +
      `&X-Amz-Signature=${sig}`
    );
  });

describe('FileUploadModal — confirmation target is server-signed (property)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('fetches exactly the server-signed confirm_url, never a client-composed URL', async () => {
    await fc.assert(
      fc.asyncProperty(presignUrlArb, keyArb, confirmUrlArb, async (presignUrl, key, confirmUrl) => {
        vi.clearAllMocks();
        const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
        vi.stubGlobal('fetch', fetchMock);
        vi.mocked(apiClient.post).mockResolvedValueOnce({
          url: presignUrl,
          fields: { key },
          key,
          confirm_url: confirmUrl,
        });

        const xhrRef = stubXhr();
        const { unmount } = render(
          <FileUploadModal visible onDismiss={vi.fn()} pipeline={mockPipeline} />
        );
        const input = screen.getByTestId('file-input') as HTMLInputElement;
        fireEvent.change(input, {
          target: { files: [new File([new ArrayBuffer(64)], 'data.csv', { type: 'text/csv' })] },
        });
        await act(async () => {
          fireEvent.click(screen.getByRole('button', { name: /^upload$/i }));
        });
        await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
        act(() => {
          xhrRef.current?.onload?.();
        });
        await waitFor(() => expect(fetchMock).toHaveBeenCalled());

        const target = fetchMock.mock.calls[0][0] as string;

        // 1. It is the server's URL, unmodified.
        expect(target).toBe(confirmUrl);

        // 2. It carries a credential. Without one, S3 answers 403 for every
        //    object against this bucket — the 1d.5 defect.
        const params = new URL(target).searchParams;
        expect(params.get('X-Amz-Signature')).toBeTruthy();
        expect(params.get('X-Amz-Credential')).toBeTruthy();

        // 3. It is never the bare concatenation the deleted `objectUrlFor` built.
        const root = presignUrl.endsWith('/') ? presignUrl.slice(0, -1) : presignUrl;
        const anonymous = `${root}/${key.split('/').map(encodeURIComponent).join('/')}`;
        expect(target).not.toBe(anonymous);

        // 4. And the method is HEAD, which SigV4 binds into the signature — a
        //    GET against a head_object URL would itself 403.
        expect(fetchMock.mock.calls[0][1]).toEqual({ method: 'HEAD' });

        unmount();
        vi.unstubAllGlobals();
      }),
      { numRuns: 10 }
    );
  });
});
