// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import FileUploadModal, {
  UNCONFIRMED_UPLOAD_MESSAGE,
  CONFIRMATION_UNAVAILABLE_MESSAGE,
} from '../FileUploadModal';
import type { Pipeline } from '../../types';

// Mock ToastContext
vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

// Mock apiClient
vi.mock('../../services/api-client', () => ({
  apiClient: {
    post: vi.fn(),
  },
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

const defaultProps = {
  visible: true,
  onDismiss: vi.fn(),
  pipeline: mockPipeline,
};

// Helper to create a mock File
function createMockFile(name: string, size: number, type = 'text/csv'): File {
  const content = new ArrayBuffer(size);
  return new File([content], name, { type });
}

// Helper to simulate file selection
function selectFile(file: File) {
  const input = screen.getByTestId('file-input') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

describe('FileUploadModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders modal with pipeline name', () => {
    render(<FileUploadModal {...defaultProps} />);
    expect(screen.getByText('Test Pipeline')).toBeInTheDocument();
  });

  it('renders file input', () => {
    render(<FileUploadModal {...defaultProps} />);
    expect(screen.getByTestId('file-input')).toBeInTheDocument();
  });

  // --- File validation display ---

  it('displays file details after selecting a valid file', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 1024 * 100); // 100 KB
    selectFile(file);

    expect(screen.getByText('CSV')).toBeInTheDocument();
    expect(screen.getByText('100.0 KB')).toBeInTheDocument();
  });

  it('shows validation error for unsupported file format', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('image.png', 1024);
    selectFile(file);

    expect(screen.getByTestId('validation-error')).toBeInTheDocument();
    expect(screen.getByText(/unsupported file format/i)).toBeInTheDocument();
  });

  it('shows validation error for file exceeding 500 MB', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('huge.csv', 600 * 1024 * 1024); // 600 MB
    selectFile(file);

    expect(screen.getByTestId('validation-error')).toBeInTheDocument();
    expect(screen.getByText(/exceeds the 500 MB limit/i)).toBeInTheDocument();
  });

  it('shows validation error for empty file', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('empty.csv', 0);
    selectFile(file);

    expect(screen.getByTestId('validation-error')).toBeInTheDocument();
    expect(screen.getByText(/empty/i)).toBeInTheDocument();
  });

  it('disables upload button when validation error exists', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('bad.png', 1024);
    selectFile(file);

    const uploadBtn = screen.getByRole('button', { name: /upload/i });
    expect(uploadBtn).toBeDisabled();
  });

  it('enables upload button for valid file', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 1024);
    selectFile(file);

    const uploadBtn = screen.getByRole('button', { name: /upload/i });
    expect(uploadBtn).not.toBeDisabled();
  });

  // --- Progress bar during upload ---

  it('shows progress bar during upload', async () => {
    // Mock presign response
    vi.mocked(apiClient.post).mockResolvedValueOnce({
      url: 'https://s3.amazonaws.com/bucket',
      fields: { key: 'test-pipeline/data.csv' },
      key: 'test-pipeline/data.csv',
    });

    // Capture the XHR instance
    let capturedXhr: any;
    const MockXHR = vi.fn().mockImplementation(function () {
      const xhr = {
        open: vi.fn(),
        send: vi.fn(),
        abort: vi.fn(),
        upload: { onprogress: null as any },
        onload: null as any,
        onerror: null as any,
        onabort: null as any,
        status: 200,
      };
      capturedXhr = xhr;
      return xhr;
    });
    vi.stubGlobal('XMLHttpRequest', MockXHR);

    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 5 * 1024 * 1024); // 5 MB
    selectFile(file);

    // Click upload
    const uploadBtn = screen.getByRole('button', { name: /upload/i });
    await act(async () => {
      fireEvent.click(uploadBtn);
    });

    // Wait for presign call
    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith('/upload/presign', {
        pipeline_id: 'test-pipeline',
        file_name: 'data.csv',
      });
    });

    // Simulate progress event
    act(() => {
      capturedXhr.upload.onprogress({
        lengthComputable: true,
        loaded: 2.5 * 1024 * 1024,
        total: 5 * 1024 * 1024,
      });
    });

    expect(screen.getByTestId('upload-progress')).toBeInTheDocument();

    vi.unstubAllGlobals();
  });

  // --- Cancel button ---

  it('shows cancel button during upload and aborts XHR on click', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({
      url: 'https://s3.amazonaws.com/bucket',
      fields: { key: 'test-pipeline/data.csv' },
      key: 'test-pipeline/data.csv',
    });

    let capturedXhr: any;
    const MockXHR = vi.fn().mockImplementation(function () {
      const xhr = {
        open: vi.fn(),
        send: vi.fn(),
        abort: vi.fn(),
        upload: { onprogress: null as any },
        onload: null as any,
        onerror: null as any,
        onabort: null as any,
        status: 200,
      };
      capturedXhr = xhr;
      return xhr;
    });
    vi.stubGlobal('XMLHttpRequest', MockXHR);

    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 1024);
    selectFile(file);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /upload/i }));
    });

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalled();
    });

    // Cancel button should be visible during upload
    const cancelBtn = screen.getByRole('button', { name: /cancel upload/i });
    expect(cancelBtn).toBeInTheDocument();

    // Click cancel — should call xhr.abort()
    act(() => {
      fireEvent.click(cancelBtn);
    });

    // Trigger the onabort callback that XHR would fire
    act(() => {
      capturedXhr.onabort?.();
    });

    expect(capturedXhr.abort).toHaveBeenCalled();

    vi.unstubAllGlobals();
  });

  // --- File details on selection (auto-trigger is implicit) ---
  //
  // Per the dashboard-react steering doc, the modal does NOT expose a manual
  // trigger control: once the file lands in S3 the pipeline is triggered
  // automatically by the EventBridge notification. So instead of an
  // auto-trigger toggle, selecting a file surfaces the file-details panel,
  // and the post-upload success state explains that execution starts
  // automatically.
  it('renders file details (not an auto-trigger toggle) when a file is selected', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 1024);
    selectFile(file);

    // No manual auto-trigger control exists by design.
    expect(screen.queryByTestId('auto-trigger-toggle')).not.toBeInTheDocument();
    // File details panel is shown for a valid selection.
    expect(screen.getByTestId('file-details')).toBeInTheDocument();
  });

  // --- Success state ---

  it('shows success message after upload completes', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({
      url: 'https://s3.amazonaws.com/bucket',
      fields: { key: 'test-pipeline/data.csv' },
      key: 'test-pipeline/data.csv',
      // A signed confirmation URL must be present for this to exercise the
      // confirmed path; omitting it would route through the "unavailable" arm and
      // still reach success, making the test pass for the wrong reason.
      confirm_url: 'https://bucket.s3.us-west-2.amazonaws.com/test-pipeline/data.csv?X-Amz-Signature=deadbeef',
    });

    // p64 R4.4 — a 2xx alone no longer reaches the success state; the modal now
    // confirms the object with a HEAD first. Stubbing that as "object is there"
    // is what keeps this test about the success *state* rather than about
    // confirmation, and it is why the test needs a fetch stub at all. Without
    // one, jsdom would attempt a real request to the fake S3 host.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }));

    let capturedXhr: any;
    const MockXHR = vi.fn().mockImplementation(function () {
      const xhr = {
        open: vi.fn(),
        send: vi.fn(),
        abort: vi.fn(),
        upload: { onprogress: null as any },
        onload: null as any,
        onerror: null as any,
        onabort: null as any,
        status: 204,
      };
      capturedXhr = xhr;
      return xhr;
    });
    vi.stubGlobal('XMLHttpRequest', MockXHR);

    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.csv', 1024);
    selectFile(file);

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /upload/i }));
    });

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalled();
    });

    // Simulate successful upload completion
    act(() => {
      capturedXhr.onload?.();
    });

    await waitFor(() => {
      expect(screen.getByTestId('upload-success')).toBeInTheDocument();
    });

    vi.unstubAllGlobals();
  });

  // --- Accepted file formats ---

  it('accepts .json files', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('schema.json', 512);
    selectFile(file);

    expect(screen.queryByTestId('validation-error')).not.toBeInTheDocument();
    expect(screen.getByText('JSON')).toBeInTheDocument();
  });

  it('accepts .parquet files', () => {
    render(<FileUploadModal {...defaultProps} />);
    const file = createMockFile('data.parquet', 2048);
    selectFile(file);

    expect(screen.queryByTestId('validation-error')).not.toBeInTheDocument();
    expect(screen.getByText('PARQUET')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// p64 R4.4 — post-2xx object confirmation
// ---------------------------------------------------------------------------
//
// Expectations here are derived from R4.4 ("After a 2xx from S3, the upload modal
// MUST confirm the object exists before reporting success"), not from what the
// component used to do — the old behaviour *is* the defect, so it is not an oracle.
//
// **What this block did NOT catch, and now does.** Every test here mocked `fetch`.
// That made the *response* handling well covered and the *request* entirely
// uncovered — and the request was the defect: 1d.5 fetched the bare object URL with
// no credentials, against a BlockPublicAccess.BLOCK_ALL bucket, so the real call
// could only ever return 403. A mocked `fetch` cannot be unauthorized, so the suite
// stayed green while the feature was unusable in a deployed account. Mocking the
// thing that fails is how a test suite certifies a broken feature.
//
// The remedy is to assert the *shape of the request* as well as the handling of the
// reply: 'confirmation target carries a signature' and 'does not confirm against the
// bare presign-url-plus-key concatenation' fail on 1d.5's code with any `fetch` stub
// at all, because they read the URL the component chose rather than the answer it
// got. `FileUploadModal.confirmation.property.test.tsx` generalises the same
// property over arbitrary presign URLs and keys.

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

/** Stub XMLHttpRequest, exposing the instance the component constructs. */
function stubXhr(status: number): { current: MockXhr | null } {
  const ref: { current: MockXhr | null } = { current: null };
  const MockXHR = vi.fn().mockImplementation(function () {
    const xhr: MockXhr = {
      open: vi.fn(),
      send: vi.fn(),
      abort: vi.fn(),
      upload: { onprogress: null },
      onload: null,
      onerror: null,
      onabort: null,
      status,
    };
    ref.current = xhr;
    return xhr;
  });
  vi.stubGlobal('XMLHttpRequest', MockXHR);
  return ref;
}

const PRESIGN_URL = 'https://s3.amazonaws.com/adi-source-files';
const OBJECT_KEY = 'test-pipeline/data.csv';

/**
 * A signed HEAD URL shaped like the one `handlers/upload.py::_confirmation_url`
 * returns — SigV4 query parameters, regional virtual-host endpoint. Copied from a
 * real `generate_presigned_url('head_object', ...)` output (signature truncated),
 * so the assertions below are about the shape a server actually produces.
 */
const CONFIRM_URL =
  'https://adi-source-files-123456789012-us-west-2.s3.us-west-2.amazonaws.com/test-pipeline/data.csv' +
  '?X-Amz-Algorithm=AWS4-HMAC-SHA256' +
  '&X-Amz-Credential=ASIAEXAMPLE%2F20260915%2Fus-west-2%2Fs3%2Faws4_request' +
  '&X-Amz-Date=20260915T164115Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host' +
  '&X-Amz-Signature=8c7402f63a63cc3f2e1e653a1f0d9c2b7e4a55d0c1b8e3f6a9d2c4b7e0f3a6d9';

function mockPresign(url = PRESIGN_URL, key = OBJECT_KEY, confirmUrl: string | null = CONFIRM_URL) {
  vi.mocked(apiClient.post).mockResolvedValueOnce({
    url,
    fields: { key },
    key,
    confirm_url: confirmUrl,
  });
}

/** Select a valid file and click Upload. Returns the captured XHR ref. */
async function startUpload(status = 204, fileName = 'data.csv') {
  const xhrRef = stubXhr(status);
  render(<FileUploadModal {...defaultProps} />);
  selectFile(createMockFile(fileName, 1024));
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /^upload$/i }));
  });
  await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
  return xhrRef;
}

describe('FileUploadModal — object confirmation after 2xx (R4.4)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('issues a HEAD for the server-signed confirmation URL and reports success', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });

    await waitFor(() => {
      expect(screen.getByTestId('upload-success')).toBeInTheDocument();
    });

    // Byte-identical to what the server returned. The client composes nothing.
    expect(fetchMock).toHaveBeenCalledWith(CONFIRM_URL, { method: 'HEAD' });
  });

  // -------------------------------------------------------------------------
  // The guard the original tests lacked
  // -------------------------------------------------------------------------
  // 1d.5's tests mocked `fetch` and asserted the *response* handling, which is
  // exactly the layer that was never broken. They passed against code whose
  // request could only ever return 403, because a mocked `fetch` cannot be
  // unauthorized. These two assert the **request**, which is where the defect
  // lived.

  it('confirmation target carries a signature — never an anonymous object URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const target = new URL(fetchMock.mock.calls[0][0] as string);

    // The property 1d.5 violated: the source bucket is BlockPublicAccess.BLOCK_ALL
    // with no public policy, so an unsigned HEAD returns 403 for every object,
    // present or absent. A confirmation that carries no credential cannot ever
    // succeed — CORS permitting HEAD does not authorize anything.
    const signature =
      target.searchParams.get('X-Amz-Signature') ?? target.searchParams.get('Signature');
    expect(signature, 'confirmation URL must be presigned').toBeTruthy();
    const credential =
      target.searchParams.get('X-Amz-Credential') ?? target.searchParams.get('AWSAccessKeyId');
    expect(credential, 'confirmation URL must name a credential').toBeTruthy();
  });

  it('does not confirm against the bare presign-url-plus-key concatenation', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // The exact URL the deleted `objectUrlFor` produced. Pinned as a negative so
    // reintroducing that helper fails here rather than in a deployed account.
    expect(fetchMock.mock.calls[0][0]).not.toBe(`${PRESIGN_URL}/${OBJECT_KEY}`);
  });

  it('reports success with a warning — not an error — when no confirmation URL was issued', async () => {
    // Compute-stack older than the Hosting stack serving this bundle. Fail open,
    // but say so: blocking the upload would recreate the outage being fixed, and
    // a clean success would be the pre-p64 lie.
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    mockPresign(PRESIGN_URL, OBJECT_KEY, null);

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });

    await waitFor(() => {
      expect(screen.getByTestId('upload-success')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('upload-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('upload-unconfirmed').textContent).toContain(
      CONFIRMATION_UNAVAILABLE_MESSAGE
    );
    // Nothing to fetch, so nothing was fetched — and certainly not a guessed URL.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not advise a retry that the duplicate check would skip', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });

    await waitFor(() => expect(screen.getByTestId('upload-error')).toBeInTheDocument());

    // `shared/processed_file_ledger.py` fingerprints sha256(size:etag) and skips a
    // file it has already seen, so "please try again" pointed the operator at a
    // guaranteed no-op. Asserted on the exported declaration, not a transcription.
    expect(UNCONFIRMED_UPLOAD_MESSAGE).not.toMatch(/try again/i);
    expect(screen.getByTestId('upload-error').textContent).toContain(UNCONFIRMED_UPLOAD_MESSAGE);
  });

  it('reports an error — not success — when the object is absent (HEAD 404)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });

    await waitFor(() => {
      expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('upload-success')).not.toBeInTheDocument();

    // The message must be distinguishable from a plain upload failure, which is
    // the whole point of R4.4 — assert against the exported declaration rather
    // than a transcription of it.
    expect(screen.getByTestId('upload-error').textContent).toContain(UNCONFIRMED_UPLOAD_MESSAGE);
    expect(screen.getByTestId('upload-error').textContent).toContain('404');
  });

  it('reports an error when the confirmation request itself fails (network/CORS)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Failed to fetch')));
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });

    // "Could not check" must never render as "checked, and it is fine".
    await waitFor(() => {
      expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('upload-success')).not.toBeInTheDocument();
    expect(screen.getByTestId('upload-error').textContent).toContain(UNCONFIRMED_UPLOAD_MESSAGE);
  });

  it('does not call onUploadComplete when confirmation fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }));
    mockPresign();
    const onUploadComplete = vi.fn();

    const xhrRef = stubXhr(204);
    render(<FileUploadModal {...defaultProps} onUploadComplete={onUploadComplete} />);
    selectFile(createMockFile('data.csv', 1024));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /^upload$/i }));
    });
    await waitFor(() => expect(apiClient.post).toHaveBeenCalled());
    act(() => {
      xhrRef.current?.onload?.();
    });

    await waitFor(() => {
      expect(screen.getByTestId('upload-error')).toBeInTheDocument();
    });
    // Downstream consumers must not be told a file landed when it may not have.
    expect(onUploadComplete).not.toHaveBeenCalled();
  });

  it('never confirms an aborted upload — cancel during transfer stays idle', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 });
    vi.stubGlobal('fetch', fetchMock);
    mockPresign();

    const xhrRef = await startUpload();

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /cancel upload/i }));
    });
    act(() => {
      xhrRef.current?.onabort?.();
    });

    await waitFor(() => {
      expect(screen.queryByTestId('upload-progress')).not.toBeInTheDocument();
    });
    // A cancelled transfer has nothing to confirm, and must not read as a
    // confirmation error either.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId('upload-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('upload-success')).not.toBeInTheDocument();
  });

  it('cancelling while confirmation is in flight reports neither success nor error', async () => {
    // The Cancel Upload button is still rendered during confirmation, and by then
    // xhrRef is cleared, so abort() is a no-op and no 'Upload cancelled' rejection
    // arrives. This is the window the cancelledRef guard covers.
    let resolveHead: ((v: { ok: boolean; status: number }) => void) | undefined;
    const fetchMock = vi.fn(
      () =>
        new Promise<{ ok: boolean; status: number }>((res) => {
          resolveHead = res;
        })
    );
    vi.stubGlobal('fetch', fetchMock);
    mockPresign();

    const xhrRef = await startUpload();
    act(() => {
      xhrRef.current?.onload?.();
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // Cancel mid-confirmation, then let the HEAD come back saying "absent".
    act(() => {
      fireEvent.click(screen.getByRole('button', { name: /cancel upload/i }));
    });
    await act(async () => {
      resolveHead?.({ ok: false, status: 404 });
    });

    expect(screen.queryByTestId('upload-error')).not.toBeInTheDocument();
    expect(screen.queryByTestId('upload-success')).not.toBeInTheDocument();
  });
});
