// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useRef, useState } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Alert from '@cloudscape-design/components/alert';
import Table from '@cloudscape-design/components/table';
import Spinner from '@cloudscape-design/components/spinner';
import {
  useJdbcDetectTargetSchemaStart,
  useJdbcDetectTargetSchemaStatus,
} from '../../hooks';
import type { WizardStepProps, SchemaField } from './types';
import { canDetectSchema } from './target-detect';

/**
 * P19 Task 7.2 — Validates: Requirements 1.2, 1.4, 1.5.
 *
 * JDBC target-side Detect Schema panel. Owns the React Query hooks for the
 * new `/resources/jdbc/detect-target-schema` endpoint pair so they only
 * mount when the user has chosen a JDBC target. The parent
 * `WizardStepTarget` continues to handle the legacy GET path for
 * `s3_tables` / `glue_catalog` targets inline.
 *
 * Extracted from `WizardStepTarget.tsx` (P29 R11.5 decomposition) with no
 * behavior change.
 */
export function JdbcTargetDetectPanel({ state, onChange }: WizardStepProps) {
  const [detecting, setDetecting] = useState(false);
  const [detectError, setDetectError] = useState<string | null>(null);
  const [crawlerName, setCrawlerName] = useState<string | null>(null);
  const [tablePrefix, setTablePrefix] = useState<string | null>(null);

  const startMutation = useJdbcDetectTargetSchemaStart();
  const statusQuery = useJdbcDetectTargetSchemaStatus(crawlerName, tablePrefix);

  // Refs so the polling-result effect can read the latest wizard state /
  // onChange without listing them as dependencies — that would re-fire
  // the effect on every parent re-render and reset the crawler before
  // READY arrived.
  const stateRef = useRef(state);
  stateRef.current = state;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // React to terminal states from the polling query.
  useEffect(() => {
    if (!statusQuery.data) return;
    const status = statusQuery.data.status;
    if (status === 'READY') {
      const schema = (statusQuery.data.schema ?? []).map((f) => ({
        name: f.name,
        type: f.type,
        nullable: f.nullable ?? true,
        description: f.description ?? '',
      })) as SchemaField[];
      const s = stateRef.current;
      onChangeRef.current({
        ...s,
        // Decision 1 — keep the deprecated `detectedSchema` alias in
        // lock-step with `detectedTargetSchema` for one release so any
        // legacy reader continues to surface the target-side value.
        detectedTargetSchema: schema,
        detectedSchema: schema,
        schemaSource: 'detected',
        schemaSourceExplicitlySet: true,
        schemaFields: schema,
      });
      setDetecting(false);
      setCrawlerName(null);
      setTablePrefix(null);
    } else if (status === 'FAILED') {
      // Requirement 1.5 — surface inline error, do NOT mutate the
      // `detectedTargetSchema` / `schemaSource` / `schemaFields` fields.
      setDetectError(
        statusQuery.data.error_message ?? 'Schema detection failed',
      );
      setDetecting(false);
      setCrawlerName(null);
      setTablePrefix(null);
    }
  }, [statusQuery.data]);

  // Surface query-level transport errors (network / 5xx).
  useEffect(() => {
    if (statusQuery.isError && statusQuery.error) {
      setDetectError(
        statusQuery.error instanceof Error
          ? statusQuery.error.message
          : 'Failed to detect schema',
      );
      setDetecting(false);
      setCrawlerName(null);
      setTablePrefix(null);
    }
  }, [statusQuery.isError, statusQuery.error]);

  function handleDetectSchema() {
    // Requirement 1.2 — POST the new target-side endpoint with the full
    // target_config; the backend extracts the connection details + the
    // `database_name` / `table_name` to compute the discovery hash.
    setDetecting(true);
    setDetectError(null);
    startMutation.mutate(
      { target_config: state.targetConfig },
      {
        onSuccess: (data) => {
          setCrawlerName(data.crawler_name);
          setTablePrefix(data.table_prefix ?? null);
        },
        onError: (err) => {
          setDetectError(
            err instanceof Error ? err.message : 'Failed to detect schema',
          );
          setDetecting(false);
        },
      },
    );
  }

  const showDetect = canDetectSchema(state);

  return (
    <>
      {showDetect && (
        <SpaceBetween size="s">
          <Button
            onClick={handleDetectSchema}
            loading={detecting}
            disabled={detecting}
            data-testid="detect-schema-button"
          >
            {detecting ? 'Detecting...' : 'Detect Schema'}
          </Button>
          {detecting && <Spinner />}
        </SpaceBetween>
      )}

      {detectError && (
        <Alert type="error" data-testid="detect-error">
          {detectError}
        </Alert>
      )}

      {state.detectedTargetSchema && state.detectedTargetSchema.length > 0 && (
        <SpaceBetween size="s">
          <Alert type="success" data-testid="detect-success">
            Schema detected: {state.detectedTargetSchema.length} field
            {state.detectedTargetSchema.length !== 1 ? 's' : ''} found
          </Alert>
          <Table
            data-testid="detected-schema-preview"
            columnDefinitions={[
              { id: 'name', header: 'Field Name', cell: (f) => f.name },
              { id: 'type', header: 'Type', cell: (f) => f.type },
              { id: 'nullable', header: 'Nullable', cell: (f) => (f.nullable ? 'Yes' : 'No') },
              { id: 'description', header: 'Description', cell: (f) => f.description || '—' },
            ]}
            items={state.detectedTargetSchema}
            variant="embedded"
          />
        </SpaceBetween>
      )}
    </>
  );
}
