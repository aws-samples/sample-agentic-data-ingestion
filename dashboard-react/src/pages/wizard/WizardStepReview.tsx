// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
// p55 R8.4 — the same gate the request body uses, so the Review section and the
// wire cannot disagree about whether a selection was made.
import { worksheetSelectionIsSet } from '../../utils/worksheet-selection';
// p64 R7 — the same text form the Model step's field uses, so what the operator
// typed and what Review echoes cannot disagree.
import { formatDedupColumns } from '../../utils/quality-config';
import { INITIAL_WIZARD_STATE } from './types';
import type { WizardState } from './types';

// P17 Tasks 16.3 + 20.3 — Validates: Requirements 14.7, 18.2.
// Target type labels for the Review step. Per Requirement 14.1 the consolidated
// label for both `jdbc` and the legacy `rds_database` value is "JDBC Database
// (RDS, Aurora, or self-managed)". The `rds_database` row was a temporary
// placeholder while the legacy `RdsDatabaseForm` was being removed; it is no
// longer needed because `WizardState.targetType` no longer contains
// `'rds_database'` — persisted-but-legacy pipelines with
// `target_config.type == "RDS_DATABASE"` are mapped to `'jdbc'` for display
// in `CreatePipelinePage.tsx` per Task 20.4.
export const TARGET_TYPE_LABELS: Record<string, string> = {
  s3_tables: 'S3 Table Bucket',
  // Matches the tile label in `WizardStepTarget.tsx` — the Review step shows the
  // user the type they picked, so the two must not disagree.
  glue_catalog: 'Glue Catalog (existing table)',
  jdbc: 'JDBC Database (RDS, Aurora, or self-managed)',
  s3_parquet: 'S3 Parquet',
};

// P17 Task 16.3 — Validates: Requirements 14.7, 18.2.
// Source-type labels for the Source section of the Review step (Task 20.5).
export const SOURCE_TYPE_LABELS: Record<'s3_upload' | 'jdbc', string> = {
  s3_upload: 'S3 Upload',
  jdbc: 'JDBC Database',
};

const SCHEMA_SOURCE_LABELS: Record<string, string> = {
  detected: 'Auto-detected from target',
  upload: 'Uploaded',
  existing: 'Existing schema',
};

// P17 Task 20.5 — Validates: Requirement 18.3.
// Mask a Secrets Manager ARN (or any opaque secret string) by replacing
// everything except the trailing 4 characters with `****`. Empty or short
// values render as `—` so the Review section never leaks more than the
// requirement allows.
//
// Exported for unit-testing — see `__tests__/WizardStepReview.test.tsx`.
export function maskSecret(value: string | undefined | null): string {
  if (!value) return '—';
  if (value.length <= 4) return '****';
  return `****${value.slice(-4)}`;
}

// P17 Task 20.5 — Validates: Requirement 18.3.
// Strip any inline `username:password@` from a JDBC URL so credentials never
// reach the Review step display, even if a user pasted them into the URL
// override field. Returns `'—'` for empty input.
//
// Exported for unit-testing.
export function maskJdbcUrl(value: string | undefined | null): string {
  if (!value) return '—';
  return value.replace(/(jdbc:[^:]+:\/\/)([^/@\s]+@)/, '$1');
}

// P17 Task 20.5 — Truncate a free-form watermark value to 30 characters per
// Requirement 18.1 ("Initial Watermark (truncated to 30 chars)").
function truncate(value: string | undefined | null, max = 30): string {
  if (!value) return '—';
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}…`;
}

function KeyValue({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <div>{children}</div>
    </div>
  );
}

/**
 * Source section of the Review step. Validates: Requirement 18.1, 18.3.
 *
 * For S3 upload sources the section is intentionally minimal — the upload
 * destination is shown on the pipeline overview page after creation.
 *
 * For JDBC sources the section displays Engine, JDBC URL (with credentials
 * masked), Schema/Database, Table, Read Mode, watermark fields when
 * applicable, the masked Secrets Manager ARN, and the Schedule expression.
 */
function SourceSection({ state }: { state: WizardState }) {
  const sourceTypeLabel = SOURCE_TYPE_LABELS[state.sourceType] ?? state.sourceType;

  if (state.sourceType !== 'jdbc') {
    return (
      <ColumnLayout columns={2} variant="text-grid">
        <KeyValue label="Source Type">{sourceTypeLabel}</KeyValue>
        <KeyValue label="Schedule">{state.schedule || '—'}</KeyValue>
        {/* p55 R8.4 — the Selected_Worksheet, so the choice is visible before
            Create rather than only after a run refuses. Gated on the same
            `worksheetSelectionIsSet` the request body uses, so what this section
            shows and what is persisted cannot disagree: a whitespace-only value
            reads as "not set" here exactly because it is not sent. */}
        <KeyValue label="Excel Worksheet">
          {worksheetSelectionIsSet(state.selectedWorksheet)
            ? state.selectedWorksheet
            : 'Not set — a multi-sheet workbook will fail and list its worksheets'}
        </KeyValue>
      </ColumnLayout>
    );
  }

  const cfg = state.sourceConfig;
  const readMode = cfg.read_mode || 'full_table';
  const isIncremental = readMode === 'incremental';

  return (
    <SpaceBetween size="s">
      <ColumnLayout columns={2} variant="text-grid">
        <KeyValue label="Source Type">{sourceTypeLabel}</KeyValue>
        <KeyValue label="Engine">{cfg.engine || '—'}</KeyValue>
        <KeyValue label="JDBC URL">{maskJdbcUrl(cfg.jdbc_url)}</KeyValue>
        <KeyValue label="JDBC Driver">{cfg.jdbc_driver || '—'}</KeyValue>
        <KeyValue label="Schema / Database">{cfg.schema || '—'}</KeyValue>
        <KeyValue label="Table">{cfg.table || '—'}</KeyValue>
        <KeyValue label="Read Mode">{readMode === 'incremental' ? 'Incremental' : 'Full table'}</KeyValue>
        <KeyValue label="Secret">{maskSecret(cfg.secret_arn)}</KeyValue>
        {isIncremental && (
          <>
            <KeyValue label="Watermark Column">{cfg.watermark_column || '—'}</KeyValue>
            <KeyValue label="Watermark Type">{cfg.watermark_type || '—'}</KeyValue>
            <KeyValue label="Initial Watermark">{truncate(cfg.watermark_initial_value)}</KeyValue>
          </>
        )}
        <KeyValue label="Schedule">{state.schedule || '—'}</KeyValue>
      </ColumnLayout>
    </SpaceBetween>
  );
}

/**
 * Data Quality section of the Review step. p64 R7.1 / R7.2 / R6.6.
 *
 * Reads defensively (R7.5): a restored draft saved before `qualityConfig` existed
 * makes it `undefined` despite the type, so the whole sub-object falls back to
 * `INITIAL_WIZARD_STATE.qualityConfig` — which is exactly what such a draft would
 * submit, so the display and the wire agree.
 *
 * **"Template default" is rendered as itself, not resolved to On or Off.** The
 * resolution depends on which template `create_code` selects, and that selection
 * happens after Create — this page cannot know it, and guessing "off" here would
 * state the opposite of the truth for the default S3 Tables target. Naming the
 * state is honest; naming a value it does not have is the inert-control defect one
 * surface over.
 */
function QualitySection({ state }: { state: WizardState }) {
  const quality = state.qualityConfig ?? INITIAL_WIZARD_STATE.qualityConfig;
  const dqLabel =
    quality.dataQualityEnabled === true
      ? 'On'
      : quality.dataQualityEnabled === false
        ? 'Off'
        : 'Template default (on for S3 Table Buckets, off for other targets)';

  return (
    <ColumnLayout columns={2} variant="text-grid">
      <KeyValue label="Data Quality Checks">{dqLabel}</KeyValue>
      <KeyValue label="Drop Duplicate Rows">{quality.dedupEnabled ? 'Yes' : 'No'}</KeyValue>
      {quality.dedupEnabled && (
        <KeyValue label="Duplicate-identifying Columns">
          {formatDedupColumns(quality.dedupColumns) || 'All mapped target columns'}
        </KeyValue>
      )}
    </ColumnLayout>
  );
}

export function WizardStepReview({ state }: { state: WizardState }) {
  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Review &amp; Create</Header>}>
        <SpaceBetween size="l">
          <Header variant="h3">Pipeline Details</Header>
          <ColumnLayout columns={2} variant="text-grid">
            <KeyValue label="Pipeline Name">{state.pipelineName || '—'}</KeyValue>
            <KeyValue label="Description">{state.description || '—'}</KeyValue>
          </ColumnLayout>

          {/* P17 Task 20.5 — Source section. */}
          <Header variant="h3">Source</Header>
          <SourceSection state={state} />

          <Header variant="h3">Target Destination</Header>
          <ColumnLayout columns={2} variant="text-grid">
            <KeyValue label="Target Type">{TARGET_TYPE_LABELS[state.targetType] ?? state.targetType}</KeyValue>
            {Object.entries(state.targetConfig).map(([key, value]) => {
              // P17 Req 18.3 — never echo a Secrets Manager ARN in cleartext.
              const displayValue = key === 'secret_arn' ? maskSecret(value) : (value || '—');
              return (
                <KeyValue key={key} label={key.replace(/_/g, ' ')}>
                  {displayValue}
                </KeyValue>
              );
            })}
          </ColumnLayout>

          <Header variant="h3">Schema</Header>
          <ColumnLayout columns={2} variant="text-grid">
            <KeyValue label="Schema Source">{SCHEMA_SOURCE_LABELS[state.schemaSource] ?? state.schemaSource}</KeyValue>
            <KeyValue label="Fields">{state.schemaFields.length} fields</KeyValue>
          </ColumnLayout>

          {state.schemaFields.length > 0 && (
            <Table
              data-testid="review-schema-table"
              columnDefinitions={[
                { id: 'name', header: 'Field Name', cell: (f) => f.name },
                { id: 'type', header: 'Type', cell: (f) => f.type },
                { id: 'nullable', header: 'Nullable', cell: (f) => (f.nullable ? 'Yes' : 'No') },
              ]}
              items={state.schemaFields}
              variant="embedded"
            />
          )}

          <Header variant="h3">AI Model &amp; Approval</Header>
          <ColumnLayout columns={2} variant="text-grid">
            <KeyValue label="AI Model">{state.aiModel || '—'}</KeyValue>
            <KeyValue label="Schema Mapping Approval">
              {state.approvalConfig.schemaMappingEnabled ? 'Required' : 'Not required'}
            </KeyValue>
            <KeyValue label="Code Generation Approval">
              {state.approvalConfig.codeGenerationEnabled ? 'Required' : 'Not required'}
            </KeyValue>
            <KeyValue label="Confidence Threshold">
              {state.approvalConfig.schemaMappingConfidenceThreshold}%
            </KeyValue>
            {/* Kept in step with `WizardStepModel`'s toggle label: `true` means
                approval IS required, so "Auto-approve … : Yes" read as the
                opposite of the gate it describes. Label only — the value, the
                field name and the wire key are unchanged (p63 R2.9). */}
            <KeyValue label="Require Approval for Schema Changes">
              {state.approvalConfig.alwaysApproveSchemaChanges ? 'Yes' : 'No'}
            </KeyValue>
          </ColumnLayout>

          {/* p64 R7 / R6.6 — the data-quality settings are shown before Create so
              the choice is visible here rather than only after a run. */}
          <Header variant="h3">Data Quality</Header>
          <QualitySection state={state} />

          <Header variant="h3">Estimated Cost</Header>
          <Box color="text-body-secondary">
            Cost estimates depend on the selected model and data volume. Typical runs cost $0.01–$0.50 per execution.
          </Box>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
