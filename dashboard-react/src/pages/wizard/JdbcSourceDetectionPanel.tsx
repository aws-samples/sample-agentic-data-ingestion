// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useRef, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import {
  useJdbcDetectSchemaStart,
  useJdbcDetectSchemaStatus,
  type JdbcDetectSchemaMode,
  type JdbcSchemaField,
} from '../../hooks';
import type { SchemaField, WizardState, WizardStepProps } from './types';
import { validateIdentifier } from './source-validation';

// ─── Schema-detection orchestration (Tasks 19.3 + 19.4) ─────────

/**
 * P17 Req 4.6 — total cap on a single detection attempt.
 *
 * Sized for Glue Crawler WALL-CLOCK time, not just crawl runtime. A JDBC
 * discovery crawl does ~2-3 min of actual work, but the crawler's `State`
 * stays `RUNNING` well beyond that while Glue provisions and tears down the
 * crawler infrastructure — end-to-end this routinely exceeds 5 minutes even
 * for a single small table. The earlier 5-minute cap fired before the
 * crawler flipped to `READY`, surfacing a false "did not complete" error
 * even though the crawl had succeeded. 12 minutes covers the observed
 * provisioning + teardown overhead with margin.
 */
const DETECTION_TIMEOUT_MS = 12 * 60 * 1000;
const DETECTION_TIMEOUT_MINUTES = DETECTION_TIMEOUT_MS / 60_000;

/**
 * The form stores schema/table under the user-friendly keys `schema` /
 * `table`, but the backend `detect_schema_start` endpoint and the shared
 * `jdbc_helpers` consume them as `jdbc_schema` / `jdbc_table` (matches
 * the canonical pipeline_record source_config shape — see
 * `handlers/jdbc_resources.py::detect_schema_start`). Build the POST
 * body by aliasing the two keys so the request reaches the backend in
 * its expected shape without forcing the form/state to use the longer
 * names.
 */
function buildDetectSchemaSourceConfig(
  cfg: Record<string, string>,
): Record<string, string> {
  const { schema, table, ...rest } = cfg;
  const out: Record<string, string> = { ...rest };
  if (schema) out.jdbc_schema = schema;
  if (table) out.jdbc_table = table;
  return out;
}

/**
 * Returns true when the source form has enough configured for the Detect
 * Schema button to be active (P17 Req 4.1):
 *   - schema + table populated AND pass identifier regex
 *   - jdbc_url + jdbc_driver populated
 *   - select_rds mode: a Secrets Manager secret selected
 *   - glue_connection mode: an existing Glue connection selected
 */
function canStartDetection(state: WizardState): boolean {
  const cfg = state.sourceConfig;
  const mode: JdbcDetectSchemaMode =
    (cfg._connection_mode as JdbcDetectSchemaMode | undefined) ?? 'select_rds';
  if (!cfg.schema || !cfg.table) return false;
  if (validateIdentifier(cfg.schema) || validateIdentifier(cfg.table)) return false;
  if (!cfg.jdbc_url || !cfg.jdbc_driver) return false;
  if (mode === 'select_rds') return !!cfg.secret_arn;
  return !!cfg.glue_connection_name;
}

/** Convert a detect-schema API JdbcSchemaField into the wizard's SchemaField. */
function toSchemaField(f: JdbcSchemaField): SchemaField {
  return {
    name: f.name,
    type: f.type,
    nullable: f.nullable ?? true,
    description: f.description ?? '',
  };
}

/**
 * Schema-detection panel — owned by `JdbcSourceForm` so that the polling
 * and timeout effects unmount cleanly when the user switches the source
 * type back to `s3_upload`. Extracted from `WizardStepSource.tsx`
 * (P29 R11.5 decomposition) with no behavior change.
 */
export function DetectionPanel({ state, onChange }: WizardStepProps) {
  const [crawlerName, setCrawlerName] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const startMutation = useJdbcDetectSchemaStart();
  const statusQuery = useJdbcDetectSchemaStatus(crawlerName);
  const status = statusQuery.data?.status;

  // Total timeout of DETECTION_TIMEOUT_MINUTES (P17 Req 4.6) — see that
  // constant for the sizing argument. This comment said "5-minute" long after
  // the constant became 12; read the constant, not a number written here.
  //
  // WALL CLOCK, deliberately, and it only stays honest because the status hook
  // polls in the background. `setTimeout` counts real time whether or not
  // anyone is looking, so if the poll ever pauses again (the hook's
  // `refetchIntervalInBackground` going back to false would do it) this timer
  // fires on a detection it never observed. `use-resources.ts` carries the full
  // account.
  //
  // The timer is set when a crawler name is registered and cleaned up either
  // when polling reaches a terminal status (READY / FAILED) or when the panel
  // unmounts. This satisfies the task's explicit `useEffect cleanup`
  // requirement.
  useEffect(() => {
    if (!crawlerName) return undefined;
    timeoutRef.current = setTimeout(() => {
      // Says what this code actually observed, which is that it gave up — NOT
      // that the crawler failed, which it never checked. The old wording
      // ("Schema detection did not complete within N minutes") asserted the
      // second, and was wrong the one time it was investigated: the crawl had
      // SUCCEEDED and the schema was sitting in the catalog. Naming the
      // distinction is what points the next reader at the crawler's real state
      // instead of at a phantom failure.
      setErrorMessage(
        `Still waiting after ${DETECTION_TIMEOUT_MINUTES} minutes, so this page stopped checking. ` +
          'The crawler may still be running, and may yet succeed — this is not a ' +
          'report that it failed. Retry to check again, or look at the crawler in ' +
          'the AWS console for its actual state.',
      );
      setCrawlerName(null);
    }, DETECTION_TIMEOUT_MS);
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    };
  }, [crawlerName, startedAt]);

  // React to terminal polling state. The hook stops refetching on
  // READY / FAILED via its `refetchInterval` callback, so we only need
  // to surface the error / persist results here.
  useEffect(() => {
    if (!statusQuery.data) return;
    if (status === 'FAILED') {
      const message =
        statusQuery.data.error_message ??
        'Schema detection failed. Check the Glue Crawler logs in the AWS console.';
      setErrorMessage(message);
      setCrawlerName(null);
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    } else if (status === 'READY') {
      // P17 Req 4.7 + 6.3 — persist detected_* into sourceConfig and the
      // schema fields into detectedSourceSchema (P19 Decision 1) so
      // downstream wizard steps have the discovered table identity and
      // column list available. Step 4's `mirror_source` mode reads
      // `detectedSourceSchema` directly; the deprecated `detectedSchema`
      // alias is left strictly alone so target-side state is not
      // overwritten by source-side detection.
      const data = statusQuery.data;
      const schemaFields = (data.schema ?? []).map(toSchemaField);
      onChange({
        ...state,
        sourceConfig: {
          ...state.sourceConfig,
          detected_glue_database: data.glue_database ?? '',
          detected_glue_table: data.glue_table ?? '',
          detected_table_prefix:
            data.table_prefix ?? state.sourceConfig.detected_table_prefix ?? '',
        },
        detectedSourceSchema: schemaFields,
      });
      setCrawlerName(null);
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    }
    // We deliberately omit `state`/`onChange` from the dep list so this
    // effect fires only when the polling result changes — re-running it
    // every time the parent state mutates would loop back to itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, statusQuery.data]);

  // Surface query-level errors (e.g. network failure or 5xx).
  useEffect(() => {
    if (statusQuery.isError && statusQuery.error) {
      setErrorMessage(
        statusQuery.error instanceof Error
          ? statusQuery.error.message
          : 'Schema detection failed',
      );
      setCrawlerName(null);
    }
  }, [statusQuery.isError, statusQuery.error]);

  function handleStart() {
    setErrorMessage(null);
    const cfg = state.sourceConfig;
    const mode: JdbcDetectSchemaMode =
      (cfg._connection_mode as JdbcDetectSchemaMode | undefined) ?? 'select_rds';
    startMutation.mutate(
      {
        source_config: buildDetectSchemaSourceConfig(cfg),
        mode,
      },
      {
        onSuccess: (data) => {
          setCrawlerName(data.crawler_name);
          setStartedAt(Date.now());
        },
        onError: (err) => {
          setErrorMessage(
            err instanceof Error ? err.message : 'Failed to start schema detection',
          );
        },
      },
    );
  }

  function handleRetry() {
    setErrorMessage(null);
    setCrawlerName(null);
    setStartedAt(null);
  }

  const isStarting = startMutation.isPending;
  const isPolling =
    !!crawlerName && status !== 'READY' && status !== 'FAILED' && !errorMessage;
  const isRunning = isStarting || isPolling;
  const startDisabled = !canStartDetection(state) || isRunning;
  const detectedFields = state.detectedSourceSchema ?? [];

  return (
    <SpaceBetween size="s">
      <Button
        onClick={handleStart}
        loading={isStarting}
        disabled={startDisabled}
        variant="primary"
        data-testid="jdbc-source-detect-schema-button"
      >
        Detect Schema
      </Button>

      {isRunning && (
        <ProgressBar
          status="in-progress"
          // Indeterminate — the Glue Crawler doesn't surface a percentage.
          value={0}
          label="Detecting source schema"
          description={`Discovering schema for ${state.sourceConfig.schema ?? ''}.${state.sourceConfig.table ?? ''} via Glue Crawler — this may take a couple of minutes.`}
          data-testid="jdbc-source-detect-progress"
        />
      )}

      {errorMessage && (
        <Alert
          type="error"
          header="Schema detection failed"
          data-testid="jdbc-source-detect-error"
          action={<Button onClick={handleRetry}>Retry</Button>}
        >
          {errorMessage}
        </Alert>
      )}

      {detectedFields.length > 0 && !errorMessage && (
        <SpaceBetween size="s">
          <Alert type="success" data-testid="jdbc-source-detect-success">
            Schema detected: {detectedFields.length} field
            {detectedFields.length !== 1 ? 's' : ''} found
            {state.sourceConfig.detected_glue_table
              ? ` (Glue table: ${state.sourceConfig.detected_glue_table})`
              : ''}
          </Alert>
          <Table
            data-testid="jdbc-source-detected-schema-table"
            columnDefinitions={[
              { id: 'name', header: 'Field Name', cell: (f) => f.name },
              { id: 'type', header: 'Type', cell: (f) => f.type },
              {
                id: 'nullable',
                header: 'Nullable',
                cell: (f) => (f.nullable ? 'Yes' : 'No'),
              },
              {
                id: 'description',
                header: 'Description',
                cell: (f) => f.description || '—',
              },
            ]}
            items={detectedFields}
            variant="embedded"
          />
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
