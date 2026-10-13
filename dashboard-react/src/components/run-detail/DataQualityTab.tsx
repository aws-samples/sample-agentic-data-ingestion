// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import QualityReportCard from '../QualityReportCard';
import type { QualityReport } from '../../hooks/use-quality-report';

interface DataQualityTabProps {
  qualityReport?: QualityReport | null;
  qualityError?: boolean;
  onRetry: () => void;
}

/**
 * Data Quality tab content extracted from RunDetailPage (P29 R11.5).
 * Preserves the Async_State_Triple (report card / error + Retry / empty copy).
 *
 * Read-only. It used to forward an on-demand "Run Quality Check" trigger to
 * QualityReportCard; that check was disabled on 2026-10-11 because it could
 * only replace the inline Glue report with a failing one (docs/AGENTS.md § 3).
 */
export default function DataQualityTab({ qualityReport, qualityError, onRetry }: DataQualityTabProps) {
  return (
    <SpaceBetween size="l">
      {qualityReport ? (
        <QualityReportCard report={qualityReport} />
      ) : qualityError ? (
        <Alert
          type="error"
          data-testid="quality-error"
          action={<Button data-testid="quality-retry" onClick={() => onRetry()}>Retry</Button>}
        >
          Failed to load data quality results for this run.
        </Alert>
      ) : (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No data quality results available for this run.
        </Box>
      )}
    </SpaceBetween>
  );
}
