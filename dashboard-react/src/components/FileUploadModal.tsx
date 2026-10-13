// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useRef, useCallback } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import Alert from '@cloudscape-design/components/alert';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import type { Pipeline } from '../types';
import { apiClient } from '../services/api-client';
import { useToast } from '../contexts/ToastContext';

export interface FileUploadModalProps {
  visible: boolean;
  onDismiss: () => void;
  pipeline: Pipeline;
  onUploadComplete?: (key: string) => void;
}

interface PresignResponse {
  url: string;
  fields: Record<string, string>;
  key: string;
  /**
   * p64 hotfix — a **signed** HEAD URL for the R4.4 landing check, minted by
   * `handlers/upload.py::_confirmation_url` on the same response as the POST
   * presign (no second route — 1d.3's do-NOT, `LGR-4863153-1`).
   *
   * Optional because the server returns `null` when it could not be minted, and
   * because a Hosting-stack deploy can land ahead of a Compute-stack one, in
   * which case the field is absent entirely. Both cases mean *unconfirmed*, and
   * must never be read as *confirmed*.
   */
  confirm_url?: string | null;
}

interface UploadProgress {
  loaded: number;
  total: number;
  percentage: number;
  startTime: number;
}

type UploadState = 'idle' | 'validating' | 'uploading' | 'success' | 'error';

const MAX_FILE_SIZE = 500 * 1024 * 1024; // 500 MB

/**
 * p55 R6.6 — **exactly** the Format_Registry's extension set, one dot-prefixed
 * entry per key of `FORMAT_MAP` in
 * `lambda/agentic_data_ingestion_file_validation/lambda_function.py`, in that
 * literal's own order.
 *
 * Feeds both readers: `validateFile` below, and the file input's `accept`
 * attribute. Those are the two places the accepted set is expressed to a user —
 * one as a refusal, one as a picker filter — and they must be the same set or the
 * picker offers what the validator rejects.
 *
 * **What this was, and why "correct by accident" was not good enough.** It read
 * `['.csv', '.json', '.parquet', '.tsv', '.xlsx']` — five entries. `.xlsx` was
 * already among them and was, until p55's W3 landed, the original defect
 * `requirements.md` opens with: *"frontend accepts what the backend rejects"*,
 * since `detect_format` returned `"unknown"` for it and `file_validation` raised.
 * W3 made `.xlsx` real, so that line became true by accident. The other five
 * entries here are the mirror-image defect and were never fixed by anything:
 * `.jsonl`, `.ndjson`, `.pq`, `.avro` and `.orc` are all in `FORMAT_MAP`, all
 * fully supported end to end, and all were **hidden** — a user with an Avro file
 * was told the format was unsupported by a picker, not by the system.
 *
 * **A transcription, held to the registry by a test rather than by care.** A
 * TypeScript module cannot import a Python dict, so the honest options are a
 * transcription checked by a contract test or a build step that generates this
 * file. This repository has already chosen the first for exactly this problem —
 * `_SOURCE_FORMAT_BY_EXTENSION` in `test_code/glue_session_runner.py` is a
 * deliberate duplicate of `FORMAT_MAP` kept in sync by
 * `tests/contracts/test_format_vocabulary_cross_lambda.py`, which AST-parses the
 * producer rather than restating the format names. That same contract file now
 * parses **this** literal and asserts extension-set equality, which is what makes
 * R6.6's "one fact" enforceable instead of aspirational. `LGR-28b4d75-5`
 * (**template / ratified**) is the reasoning: a test written from a producer's own
 * output cannot detect the producer drifting, so the oracle has to be the other
 * declaration.
 *
 * Deliberately **not** derived from an API call. The set is needed before any
 * request is made — the picker's `accept` attribute is rendered on mount — and a
 * fetch would make an offline or slow first paint silently accept everything or
 * nothing.
 */
const ACCEPTED_FORMATS = [
  '.csv',
  '.tsv',
  // p64 R3.1/R3.3 — `.txt` joins the registry as a pipe-delimited member of the
  // `csv` family. Listed here in the same change as `FORMAT_MAP`, because guard 7
  // of the contract test asserts set equality in both directions and fails until
  // all four declarations agree.
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

/**
 * p64 R4.4 — the message shown when S3 returned a 2xx but the object could not be
 * confirmed afterwards.
 *
 * It MUST stay distinguishable from a plain upload failure. Those are different
 * facts with different next actions: "the upload failed" means retry, while this
 * means *the transfer reported success and the object still is not there* — the
 * one state in which the old code told the user everything was fine. Collapsing
 * the two back into one message re-hides exactly what R4.4 exists to surface.
 *
 * Exported so tests assert against this declaration rather than transcribing the
 * copy; a test written from its own copy of a string cannot detect the string
 * drifting (`LGR-28b4d75-5`, the same reasoning `ACCEPTED_FORMATS` above relies on).
 *
 * **No longer advises a retry, and must not start again.** The original wording
 * ended "It may not have been stored — please try again", which was misleading on
 * both halves. Re-uploading the same bytes hits the content-based duplicate check
 * in `shared/processed_file_ledger.py`, which fingerprints `sha256(size:etag)` and
 * **skips** a file it has already seen — so the retry it asked for is the one
 * action guaranteed to appear to do nothing. Point the operator at the evidence
 * instead of at a no-op.
 */
export const UNCONFIRMED_UPLOAD_MESSAGE =
  'Upload reported success, but the file could not be confirmed in S3. Do not re-upload — ' +
  'identical content is skipped as a duplicate, so a retry will appear to do nothing. ' +
  "Check the pipeline's Files list to see whether it arrived, and report this if it did not.";

/**
 * p64 hotfix — shown when the upload finished but no signed confirmation URL was
 * available to check it with (`confirm_url` absent or `null`).
 *
 * Distinct from `UNCONFIRMED_UPLOAD_MESSAGE` on purpose, and rendered as a warning
 * beside the success state rather than as an error. The facts differ: there, a
 * check ran and did not confirm the object; here, no check was possible. Only the
 * first is evidence of anything being wrong.
 *
 * This is the fail-open half of the guard. A missing `confirm_url` means either an
 * older Compute stack than the Hosting stack serving this bundle, or a server-side
 * signing failure — neither is a reason to block an operator from uploading, and
 * blocking would recreate the outage this hotfix removes. It is still said out
 * loud, because "not checked" rendering as a clean success is the pre-p64 lie
 * `LGR-4863153-2` exists to end.
 */
export const CONFIRMATION_UNAVAILABLE_MESSAGE =
  'The upload completed, but this deployment returned no signed confirmation URL, so the ' +
  "file's presence in S3 was not verified. Check the pipeline's Files list to confirm it arrived.";

/** Outcome of the R4.4 landing check. Exactly one field is ever set. */
type ConfirmationOutcome =
  /** The object was confirmed present. */
  | { kind: 'confirmed' }
  /** A check ran and did not confirm it — an error, shown in the error state. */
  | { kind: 'failed'; message: string }
  /** No check was possible — a warning, shown beside the success state. */
  | { kind: 'unavailable'; message: string };

/**
 * p64 R4.4 — confirm the uploaded object exists, using the **signed** HEAD URL the
 * presign response carried.
 *
 * **What this replaced, and why the replaced version could never work.** Task 1d.5
 * fetched the bare object URL — `objectUrlFor(presigned.url, presigned.key)` — with
 * no credentials at all. The source bucket is `BlockPublicAccess.BLOCK_ALL`
 * (`foundation-stack.ts:64`, applied to `SourceFiles` at `:74`) and no
 * `AnyPrincipal` or public policy exists anywhere in `deploy-cdk/lib/`, so that
 * anonymous HEAD returned **403 for every object, whether or not it existed** — S3
 * answers 403 rather than 404 to unauthorized callers so that keys cannot be
 * enumerated. Every upload therefore reported failure, and the upload it reported
 * on had almost certainly succeeded.
 *
 * **The reasoning that shipped it is the thing to not repeat.** Its docblock
 * justified an anonymous request on the grounds that the bucket's CORS already
 * permitted `HEAD` (`allowedMethods`/`allowedOrigins: ['*']`,
 * `foundation-stack.ts:75-82`), verified against the deployed configuration by
 * phase 0 task 0.2. That verification was accurate and **irrelevant**: CORS governs
 * whether the browser will expose a response to JavaScript. It confers no S3
 * authorization. Do NOT reintroduce an unsigned confirmation on CORS grounds.
 *
 * The client no longer composes the target at all — it fetches exactly the URL the
 * server signed. That is what makes the anonymous-URL defect unrepresentable here
 * rather than merely fixed: there is no longer any code path that builds a
 * confirmation URL out of a bucket endpoint and a key. `objectUrlFor` was deleted
 * for the same reason.
 *
 * Still a browser-side `HEAD`, so it adds no route (1d.3's do-NOT,
 * `LGR-4863153-1`) and stays out of Lambda entirely (`LGR-b46a5bb-1`). CORS is
 * unchanged and needs no change: `HEAD` is already allowed from any origin and
 * `ETag` is already exposed.
 *
 * A throw is a failure to confirm, not a confirmation: a network or CORS error
 * means the object's existence is *unknown*, and "could not check" must never
 * render as "checked, and it is fine".
 */
async function confirmObjectExists(confirmUrl: string | null | undefined): Promise<ConfirmationOutcome> {
  if (!confirmUrl) {
    // Fail open, and say so. See CONFIRMATION_UNAVAILABLE_MESSAGE for why this is
    // a warning rather than an error, and why it must not block the upload.
    return { kind: 'unavailable', message: CONFIRMATION_UNAVAILABLE_MESSAGE };
  }
  try {
    const res = await fetch(confirmUrl, { method: 'HEAD' });
    if (res.ok) return { kind: 'confirmed' };
    return {
      kind: 'failed',
      message: `${UNCONFIRMED_UPLOAD_MESSAGE} (confirmation returned HTTP ${res.status})`,
    };
  } catch (err) {
    return {
      kind: 'failed',
      message: `${UNCONFIRMED_UPLOAD_MESSAGE} (confirmation request failed: ${(err as Error).message})`,
    };
  }
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatTimeRemaining(ms: number): string {
  if (ms < 1000) return 'less than a second';
  const seconds = Math.ceil(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

function getFileExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot).toLowerCase() : '';
}

function estimateRecordCount(file: File): string {
  const ext = getFileExtension(file.name);
  if (ext === '.csv' || ext === '.tsv') {
    // Rough estimate: ~100 bytes per row for typical CSV
    const estimate = Math.round(file.size / 100);
    return `~${estimate.toLocaleString()} rows (estimated)`;
  }
  if (ext === '.json') {
    // Rough estimate: ~200 bytes per record for JSON
    const estimate = Math.round(file.size / 200);
    return `~${estimate.toLocaleString()} records (estimated)`;
  }
  return 'N/A';
}

function validateFile(file: File): string | null {
  const ext = getFileExtension(file.name);
  if (!ACCEPTED_FORMATS.includes(ext)) {
    return `Unsupported file format "${ext}". Accepted: ${ACCEPTED_FORMATS.join(', ')}`;
  }
  if (file.size > MAX_FILE_SIZE) {
    return `File size (${formatFileSize(file.size)}) exceeds the 500 MB limit.`;
  }
  if (file.size === 0) {
    return 'File is empty.';
  }
  return null;
}

export default function FileUploadModal({
  visible,
  onDismiss,
  pipeline,
  onUploadComplete,
}: FileUploadModalProps) {
  const { showToast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const xhrRef = useRef<XMLHttpRequest | null>(null);
  // The Cancel Upload button stays rendered while the R4.4 confirmation is in
  // flight (`uploadState` is still 'uploading'), and by then `xhrRef` has been
  // cleared by `xhr.onload` — so `abort()` is a no-op and no 'Upload cancelled'
  // rejection can arrive. This flag is what stops that window from turning a
  // deliberate cancel into a confirmation error.
  const cancelledRef = useRef(false);

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [uploadState, setUploadState] = useState<UploadState>('idle');
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedKey, setUploadedKey] = useState<string | null>(null);
  // Set only when the upload succeeded and no confirmation was possible. Rendered
  // as a warning alongside the success alert rather than instead of it.
  const [unconfirmedNotice, setUnconfirmedNotice] = useState<string | null>(null);

  const resetState = useCallback(() => {
    setSelectedFile(null);
    setValidationError(null);
    setUploadState('idle');
    setProgress(null);
    setUploadError(null);
    setUploadedKey(null);
    setUnconfirmedNotice(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
  }, []);

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0] ?? null;
    setUploadError(null);
    setUploadState('idle');
    setProgress(null);
    setUploadedKey(null);
    setUnconfirmedNotice(null);

    if (!file) {
      setSelectedFile(null);
      setValidationError(null);
      return;
    }

    const error = validateFile(file);
    setValidationError(error);
    setSelectedFile(file);
  }

  async function handleUpload() {
    if (!selectedFile || validationError) return;

    setUploadState('uploading');
    setUploadError(null);
    cancelledRef.current = false;

    try {
      // 1. Get presigned POST URL
      const presigned = await apiClient.post<PresignResponse>('/upload/presign', {
        pipeline_id: pipeline.pipeline_key,
        file_name: selectedFile.name,
      });

      // 2. Build FormData with presigned fields
      const formData = new FormData();
      Object.entries(presigned.fields).forEach(([k, v]) => formData.append(k, v));
      formData.append('file', selectedFile);

      // 3. Upload with XHR for progress tracking
      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhrRef.current = xhr;

        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            setProgress((prev) => ({
              loaded: e.loaded,
              total: e.total,
              percentage: Math.round((e.loaded / e.total) * 100),
              startTime: prev?.startTime ?? Date.now(),
            }));
          }
        };

        xhr.onload = () => {
          xhrRef.current = null;
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
          } else {
            reject(new Error(`Upload failed with status ${xhr.status}`));
          }
        };

        xhr.onerror = () => {
          xhrRef.current = null;
          reject(new Error('Network error during upload'));
        };

        xhr.onabort = () => {
          xhrRef.current = null;
          reject(new Error('Upload cancelled'));
        };

        xhr.open('POST', presigned.url);
        xhr.send(formData);
      });

      // 4. Confirm the object actually landed (p64 R4.4). A 2xx from S3 is the
      // transfer reporting success; it is not evidence the object persisted, and
      // that gap is the only path in this UI that can report success over an empty
      // bucket. The target is the server-signed `confirm_url` — never a URL this
      // component composes; see `confirmObjectExists` for the 403 that cost.
      const outcome = await confirmObjectExists(presigned.confirm_url);

      if (cancelledRef.current) {
        // Cancelled while confirmation was in flight — `handleCancel` already
        // returned the modal to idle. Report neither success nor a confirmation
        // error for an upload the user abandoned.
        return;
      }

      if (outcome.kind === 'failed') {
        throw new Error(outcome.message);
      }

      // 5. Success — with the notice attached when nothing could be checked, so
      // "unverified" never renders as a clean confirmation.
      setUnconfirmedNotice(outcome.kind === 'unavailable' ? outcome.message : null);
      setUploadState('success');
      setUploadedKey(presigned.key);
      setProgress((prev) => (prev ? { ...prev, percentage: 100 } : null));
      showToast(`File "${selectedFile.name}" uploaded successfully`, 'success');
      onUploadComplete?.(presigned.key);

      // Pipeline is triggered automatically by S3 EventBridge notification
    } catch (err) {
      if ((err as Error).message === 'Upload cancelled') {
        setUploadState('idle');
        setProgress(null);
      } else {
        setUploadState('error');
        setUploadError((err as Error).message || 'Upload failed');
      }
    }
  }

  function handleCancel() {
    cancelledRef.current = true;
    xhrRef.current?.abort();
    xhrRef.current = null;
    setUploadState('idle');
    setProgress(null);
  }

  function handleDismiss() {
    if (uploadState === 'uploading') return; // Prevent closing during upload
    resetState();
    onDismiss();
  }

  // Compute estimated time remaining
  const etaDisplay =
    progress && progress.startTime && progress.percentage > 0 && progress.percentage < 100
      ? (() => {
          const elapsed = Date.now() - progress.startTime;
          const rate = progress.loaded / elapsed; // bytes per ms
          const remaining = (progress.total - progress.loaded) / rate;
          return formatTimeRemaining(remaining);
        })()
      : null;

  const progressDescription =
    progress
      ? `${formatFileSize(progress.loaded)} / ${formatFileSize(progress.total)}${etaDisplay ? ` — ${etaDisplay} remaining` : ''}`
      : undefined;

  return (
    <Modal
      visible={visible}
      onDismiss={handleDismiss}
      header="Upload File"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {uploadState === 'uploading' ? (
              <Button variant="normal" onClick={handleCancel}>
                Cancel Upload
              </Button>
            ) : (
              <>
                <Button variant="link" onClick={handleDismiss}>
                  {uploadState === 'success' ? 'Close' : 'Cancel'}
                </Button>
                {uploadState !== 'success' && (
                  <Button
                    variant="primary"
                    onClick={handleUpload}
                    disabled={!selectedFile || !!validationError}
                  >
                    Upload
                  </Button>
                )}
              </>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {/* Pipeline info */}
        <ColumnLayout columns={2}>
          <div>
            <Box variant="awsui-key-label">Pipeline</Box>
            <Box>{pipeline.pipeline_name || pipeline.pipeline_key}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Target</Box>
            <Box>{pipeline.target_type || 'S3'}</Box>
          </div>
        </ColumnLayout>

        {/* File input */}
        {uploadState !== 'success' && (
          <div>
            <Box variant="awsui-key-label">Select File</Box>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_FORMATS.join(',')}
              onChange={handleFileChange}
              disabled={uploadState === 'uploading'}
              data-testid="file-input"
              style={{ marginTop: 4 }}
            />
          </div>
        )}

        {/* Validation error */}
        {validationError && (
          <Alert type="error" data-testid="validation-error">
            {validationError}
          </Alert>
        )}

        {/* Pre-upload file validation display */}
        {selectedFile && !validationError && uploadState === 'idle' && (
          <Box>
            <Box variant="awsui-key-label">File Details</Box>
            <ColumnLayout columns={3} data-testid="file-details">
              <div>
                <Box color="text-body-secondary">Format</Box>
                <Box>{getFileExtension(selectedFile.name).toUpperCase().slice(1) || 'Unknown'}</Box>
              </div>
              <div>
                <Box color="text-body-secondary">Size</Box>
                <Box>{formatFileSize(selectedFile.size)}</Box>
              </div>
              <div>
                <Box color="text-body-secondary">Records</Box>
                <Box>{estimateRecordCount(selectedFile)}</Box>
              </div>
            </ColumnLayout>
          </Box>
        )}

        {/* Progress bar during upload */}
        {uploadState === 'uploading' && progress && (
          <ProgressBar
            value={progress.percentage}
            label="Uploading file"
            description={progressDescription}
            data-testid="upload-progress"
          />
        )}

        {/* Upload error */}
        {uploadState === 'error' && uploadError && (
          <Alert type="error" data-testid="upload-error">
            {uploadError}
          </Alert>
        )}

        {/* Post-upload success message */}
        {uploadState === 'success' && (
          <Alert type="success" data-testid="upload-success">
            File uploaded successfully to <Box variant="code">{uploadedKey}</Box>
            <Box>Pipeline execution will start automatically.</Box>
          </Alert>
        )}

        {/* Upload succeeded but nothing could verify it landed (p64 hotfix). */}
        {uploadState === 'success' && unconfirmedNotice && (
          <Alert type="warning" data-testid="upload-unconfirmed">
            {unconfirmedNotice}
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
}
