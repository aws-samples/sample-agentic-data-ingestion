// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import type { Pipeline } from '../types';
import { maskJdbcUrl, maskSecret } from './configDisplayMask';

export interface SourceConfigDisplayProps {
  pipeline: Pipeline;
}

const CONNECTION_MODE_LABELS: Record<string, string> = {
  select_rds: 'Select RDS Instance',
  glue_connection: 'Use Glue Connection',
  manual: 'External or self-managed database',
};

const READ_MODE_LABELS: Record<string, string> = {
  full_table: 'Full table',
  incremental: 'Incremental',
};

const SOURCE_TYPE_LABELS: Record<string, string> = {
  S3_UPLOAD: 'S3 Upload',
  JDBC: 'JDBC Database',
};

/**
 * Truncate a free-form watermark value to ``max`` characters.
 *
 * Mirrors :func:`WizardStepReview.truncate` (P17 Req 18.1) so the
 * post-create dashboard renders the same shape as the wizard review.
 */
function truncate(value: string | undefined | null, max = 30): string {
  if (!value) return '—';
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

/**
 * Render the source-side configuration block for a pipeline detail view.
 *
 * Symmetric with :func:`TargetConfigDisplay` — both components read the
 * persisted pipeline record and surface the per-type fields the user
 * configured. JDBC sources show engine, JDBC URL (credentials masked),
 * driver, schema/table identifiers, secret (last 4 chars), and the
 * connection mode plus any watermark configuration. Other source types
 * fall back to a single Source Type row.
 *
 * Validates: P17 Requirement 18.2 / 18.3 — credential masking parity
 * with the wizard review step.
 */
export default function SourceConfigDisplay({ pipeline }: SourceConfigDisplayProps) {
  const sourceType = pipeline.source_type ?? 'S3_UPLOAD';
  const sourceTypeLabel = SOURCE_TYPE_LABELS[sourceType] ?? sourceType;
  const config = pipeline.source_config ?? {};

  if (sourceType !== 'JDBC') {
    return (
      <SpaceBetween size="s" data-testid="source-config-display">
        <div>
          <Box variant="awsui-key-label">Source Type</Box>
          <Box data-testid="source-type">{sourceTypeLabel}</Box>
        </div>
      </SpaceBetween>
    );
  }

  const readMode = config.read_mode || 'full_table';
  const isIncremental = readMode === 'incremental';
  // Pre-P17 records may carry the wizard form-input keys (`schema`,
  // `table`); P17+ records carry `jdbc_schema` / `jdbc_table`. Read
  // both to keep edit-flow and detail-view rendering uniform.
  const schemaName = config.jdbc_schema || config.schema;
  const tableName = config.jdbc_table || config.table;

  return (
    <SpaceBetween size="s" data-testid="source-config-display">
      <div>
        <Box variant="awsui-key-label">Source Type</Box>
        <Box data-testid="source-type">{sourceTypeLabel}</Box>
      </div>
      <ColumnLayout columns={2} variant="text-grid" data-testid="source-jdbc">
        <div>
          <Box variant="awsui-key-label">Engine</Box>
          <Box data-testid="source-engine">{config.engine || '—'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Connection Mode</Box>
          <Box data-testid="source-connection-mode">
            {config._connection_mode
              ? CONNECTION_MODE_LABELS[config._connection_mode] ?? config._connection_mode
              : '—'}
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">JDBC URL</Box>
          <Box data-testid="source-jdbc-url">{maskJdbcUrl(config.jdbc_url)}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">JDBC Driver</Box>
          <Box data-testid="source-jdbc-driver">{config.jdbc_driver || '—'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Schema / Database</Box>
          <Box data-testid="source-schema">{schemaName || '—'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Table</Box>
          <Box data-testid="source-table">{tableName || '—'}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Read Mode</Box>
          <Box data-testid="source-read-mode">
            {READ_MODE_LABELS[readMode] ?? readMode}
          </Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Secret</Box>
          <Box data-testid="source-secret">{maskSecret(config.secret_arn)}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Glue Connection</Box>
          <Box data-testid="source-glue-connection">
            {config.glue_connection_name || '—'}
          </Box>
        </div>
        {pipeline.schedule && (
          <div>
            <Box variant="awsui-key-label">Schedule</Box>
            <Box data-testid="source-schedule">{pipeline.schedule}</Box>
          </div>
        )}
        {isIncremental && (
          <>
            <div>
              <Box variant="awsui-key-label">Watermark Column</Box>
              <Box data-testid="source-watermark-column">
                {config.watermark_column || '—'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Watermark Type</Box>
              <Box data-testid="source-watermark-type">
                {config.watermark_type || '—'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Initial Watermark</Box>
              <Box data-testid="source-watermark-initial">
                {truncate(config.watermark_initial_value)}
              </Box>
            </div>
          </>
        )}
        {config._connection_mode === 'manual' && (
          <>
            <div>
              <Box variant="awsui-key-label">VPC</Box>
              <Box data-testid="source-vpc-id">{config.vpc_id || '—'}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Subnet</Box>
              <Box data-testid="source-subnet-id">{config.subnet_id || '—'}</Box>
            </div>
          </>
        )}
      </ColumnLayout>
    </SpaceBetween>
  );
}
