// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Pipeline Overview "Run Now" button.
 *
 * P17 Task 21.1 — POSTs to ``/api/pipelines/{id}/run`` (added by Task 12)
 * and surfaces toast feedback for the three documented response codes:
 *
 *   - 200 OK         → "Pipeline run triggered" success toast
 *   - 429 Too Many   → "Concurrency limit reached" warning toast (P17 Req 5.8)
 *   - 504 Gateway TO → "Trigger Lambda timed out" error toast    (P17 Req 5.8)
 *   - other errors   → fall through to a generic error toast
 *
 * The button is rendered for ALL source types (S3_UPLOAD and JDBC) per
 * P17 Requirement 5.6. The existing "View Glue Job" → re-run flow remains
 * available for advanced users who want to bypass Step Functions.
 */
import { useState } from 'react';
import Button from '@cloudscape-design/components/button';
import { ApiError, apiClient } from '../services/api-client';
import { useToast } from '../contexts/ToastContext';

interface RunNowButtonProps {
  pipelineKey: string;
  /** Optional override for tests / non-default labels. */
  label?: string;
}

export default function RunNowButton({ pipelineKey, label = 'Run Now' }: RunNowButtonProps) {
  const { showToast } = useToast();
  const [loading, setLoading] = useState(false);

  async function handleClick() {
    setLoading(true);
    try {
      await apiClient.post<unknown>(`/pipelines/${encodeURIComponent(pipelineKey)}/run`, {});
      showToast('Pipeline run triggered', 'success');
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 429) {
          showToast('Concurrency limit reached', 'warning');
        } else if (err.status === 504) {
          showToast('Trigger Lambda timed out', 'error');
        } else {
          showToast(err.message || 'Failed to trigger pipeline run', 'error');
        }
      } else {
        showToast('Failed to trigger pipeline run', 'error');
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <Button
      iconName="play"
      onClick={handleClick}
      loading={loading}
      data-testid="run-now-button"
    >
      {label}
    </Button>
  );
}
