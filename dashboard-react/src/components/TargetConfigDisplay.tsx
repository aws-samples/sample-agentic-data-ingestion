// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import SpaceBetween from '@cloudscape-design/components/space-between';
import type { Pipeline } from '../types';
import { maskJdbcUrl, maskSecret } from './configDisplayMask';

export interface TargetConfigDisplayProps {
  pipeline: Pipeline;
}

export default function TargetConfigDisplay({ pipeline }: TargetConfigDisplayProps) {
  const config = pipeline.target_config;

  if (!config) {
    return (
      <Box color="text-body-secondary" data-testid="target-config-empty">
        No target configuration available
      </Box>
    );
  }

  return (
    <SpaceBetween size="s" data-testid="target-config-display">
      <div>
        <Box variant="awsui-key-label">Target Type</Box>
        <Box data-testid="target-type">{config.type}</Box>
      </div>

      {config.type === 'NEW_S3_TABLE_BUCKET' && (
        <ColumnLayout columns={3} variant="text-grid" data-testid="target-new-s3">
          <div>
            <Box variant="awsui-key-label">Bucket Name</Box>
            <Box data-testid="target-bucket-name">{config.bucket_name || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Namespace</Box>
            <Box data-testid="target-namespace">{config.namespace || 'default'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Table Name</Box>
            <Box data-testid="target-table-name">{config.table_name || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Provisioning</Box>
            <Box data-testid="target-created-note" color="text-body-secondary">
              Created on first run
            </Box>
          </div>
        </ColumnLayout>
      )}

      {config.type === 'EXISTING_S3_TABLE_BUCKET' && (
        <ColumnLayout columns={3} variant="text-grid" data-testid="target-existing-s3">
          <div>
            <Box variant="awsui-key-label">Bucket Name</Box>
            <Box data-testid="target-bucket-name">{config.bucket_name || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Namespace</Box>
            <Box data-testid="target-namespace">{config.namespace || 'default'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Table Name</Box>
            <Box data-testid="target-table-name">{config.table_name || '—'}</Box>
          </div>
        </ColumnLayout>
      )}

      {config.type === 'GLUE_CATALOG' && (
        <ColumnLayout columns={3} variant="text-grid" data-testid="target-glue">
          <div>
            <Box variant="awsui-key-label">Database</Box>
            <Box data-testid="target-database">{config.database || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Table</Box>
            <Box data-testid="target-table">{config.table || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Format</Box>
            <Box data-testid="target-format">{config.format || '—'}</Box>
          </div>
        </ColumnLayout>
      )}

      {(config.type === 'JDBC' || config.type === 'RDS_DATABASE') && (
        <ColumnLayout columns={2} variant="text-grid" data-testid="target-jdbc">
          <div>
            <Box variant="awsui-key-label">Engine</Box>
            <Box data-testid="target-engine">{config.engine || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">JDBC URL</Box>
            <Box data-testid="target-jdbc-url">{maskJdbcUrl(config.jdbc_url)}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">JDBC Driver</Box>
            <Box data-testid="target-jdbc-driver">{config.jdbc_driver || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Table</Box>
            <Box data-testid="target-jdbc-table">
              {config.jdbc_table ||
                (config.database_name && config.table_name
                  ? `${config.database_name}.${config.table_name}`
                  : config.table_name) ||
                '—'}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Write Mode</Box>
            <Box data-testid="target-write-mode">{config.write_mode || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Glue Connection</Box>
            <Box data-testid="target-glue-connection">
              {config.glue_connection_name || '—'}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Secret</Box>
            <Box data-testid="target-secret">{maskSecret(config.secret_arn)}</Box>
          </div>
        </ColumnLayout>
      )}

      {config.type !== 'NEW_S3_TABLE_BUCKET' &&
        config.type !== 'EXISTING_S3_TABLE_BUCKET' &&
        config.type !== 'GLUE_CATALOG' &&
        config.type !== 'JDBC' &&
        config.type !== 'RDS_DATABASE' && (
          <div data-testid="target-unknown">
            <Box variant="awsui-key-label">Configuration</Box>
            <Box>{config.type}</Box>
          </div>
        )}
    </SpaceBetween>
  );
}
