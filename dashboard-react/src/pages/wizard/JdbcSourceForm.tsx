// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tiles from '@cloudscape-design/components/tiles';
import { JdbcConnectionSelector } from './JdbcConnectionSelector';
import { DetectionPanel } from './JdbcSourceDetectionPanel';
import { validateIdentifier, validateScheduleExpression } from './source-validation';
import type { WizardStepProps } from './types';

const READ_MODE_OPTIONS = [
  {
    value: 'full_table',
    label: 'Full table',
    description: 'Read all rows on every run',
  },
  {
    value: 'incremental',
    label: 'Incremental',
    description: 'Read only new/updated rows since last run',
  },
];

const WATERMARK_TYPE_OPTIONS = [
  { value: 'timestamp', label: 'Timestamp' },
  { value: 'numeric', label: 'Numeric' },
];

const SCHEDULE_PRESETS = [
  {
    testId: 'jdbc-source-schedule-preset-hourly',
    label: 'Every hour',
    value: 'rate(1 hour)',
  },
  {
    testId: 'jdbc-source-schedule-preset-daily',
    label: 'Every day at 02:00 UTC',
    value: 'cron(0 2 * * ? *)',
  },
  {
    testId: 'jdbc-source-schedule-preset-weekly',
    label: 'Every Monday at 02:00 UTC',
    value: 'cron(0 2 ? * MON *)',
  },
];

// ─── JDBC Source Form ────────────────────────────────────────────

/**
 * JDBC source form — connection selector + schema/table inputs +
 * read-mode tiles + (when incremental) watermark fields + schedule field +
 * schema-detection panel.
 *
 * Extracted from `WizardStepSource.tsx` (P29 R11.5 decomposition) with no
 * behavior change.
 */
export function JdbcSourceForm({ state, onChange }: WizardStepProps) {
  const sourceConfig = state.sourceConfig;
  const readMode = sourceConfig.read_mode || 'full_table';

  // P17 Req 3.5 + 3.6 — identifier validation for schema, table,
  // watermark column. Only show errors once the user has typed something
  // (don't yell at empty fields the user hasn't visited yet).
  const schemaError = sourceConfig.schema
    ? validateIdentifier(sourceConfig.schema)
    : undefined;
  const tableError = sourceConfig.table
    ? validateIdentifier(sourceConfig.table)
    : undefined;
  const watermarkColumnError =
    readMode === 'incremental' && sourceConfig.watermark_column
      ? validateIdentifier(sourceConfig.watermark_column)
      : undefined;

  // P17 Req 5.2 — schedule expression validation.
  const scheduleError = state.schedule
    ? validateScheduleExpression(state.schedule)
    : undefined;

  function updateSourceConfig(patch: Record<string, string>) {
    onChange({
      ...state,
      sourceConfig: { ...state.sourceConfig, ...patch },
    });
  }

  function handleReadModeChange(newMode: string) {
    if (newMode === readMode) return;
    if (newMode === 'full_table') {
      // Clear watermark fields when switching back to full_table so stale values
      // don't leak into the persisted config.
      const {
        watermark_column: _wc,
        watermark_type: _wt,
        watermark_initial_value: _wi,
        ...rest
      } = state.sourceConfig;
      onChange({
        ...state,
        sourceConfig: { ...rest, read_mode: 'full_table' },
      });
    } else {
      updateSourceConfig({ read_mode: newMode });
    }
  }

  return (
    <SpaceBetween size="l">
      {/* P17 Req 2.1: connection selector at top of source form */}
      <JdbcConnectionSelector
        config={state.sourceConfig}
        onChange={(next) => onChange({ ...state, sourceConfig: next })}
        testIdPrefix="jdbc-source-conn"
      />

      {/* P17 Req 3.1 + 3.5 */}
      <FormField
        label="Schema / Database"
        description="Database namespace (e.g. public for Postgres, the database name for MySQL/SQL Server, the schema name for Oracle)"
        errorText={schemaError}
      >
        <Input
          value={sourceConfig.schema ?? ''}
          onChange={({ detail }) => updateSourceConfig({ schema: detail.value })}
          placeholder="public"
          data-testid="jdbc-source-schema"
        />
      </FormField>

      {/* P17 Req 3.2 + 3.5 */}
      <FormField label="Table" description="Source table name" errorText={tableError}>
        <Input
          value={sourceConfig.table ?? ''}
          onChange={({ detail }) => updateSourceConfig({ table: detail.value })}
          placeholder="users"
          data-testid="jdbc-source-table"
        />
      </FormField>

      {/* P17 Req 3.3 */}
      <FormField label="Read Mode">
        <Tiles
          value={readMode}
          onChange={({ detail }) => handleReadModeChange(detail.value)}
          items={READ_MODE_OPTIONS}
          data-testid="jdbc-source-read-mode"
        />
      </FormField>

      {/* P17 Req 3.4 + 3.6 — watermark fields appear only for incremental mode */}
      {readMode === 'incremental' && (
        <>
          <FormField
            label="Watermark Column"
            description="Column used to track which rows have already been read"
            errorText={watermarkColumnError}
          >
            <Input
              value={sourceConfig.watermark_column ?? ''}
              onChange={({ detail }) =>
                updateSourceConfig({ watermark_column: detail.value })
              }
              placeholder="updated_at"
              data-testid="jdbc-source-watermark-column"
            />
          </FormField>
          <FormField label="Watermark Type">
            <Select
              selectedOption={
                sourceConfig.watermark_type
                  ? WATERMARK_TYPE_OPTIONS.find(
                      (o) => o.value === sourceConfig.watermark_type,
                    ) ?? null
                  : null
              }
              onChange={({ detail }) =>
                updateSourceConfig({
                  watermark_type: detail.selectedOption.value!,
                })
              }
              options={WATERMARK_TYPE_OPTIONS}
              placeholder="Select watermark type"
              data-testid="jdbc-source-watermark-type"
            />
          </FormField>
          <FormField
            label="Initial Watermark Value"
            description="Optional. Used as the lower bound for the first run only"
          >
            <Input
              value={sourceConfig.watermark_initial_value ?? ''}
              onChange={({ detail }) =>
                updateSourceConfig({ watermark_initial_value: detail.value })
              }
              placeholder="2024-01-01T00:00:00Z"
              data-testid="jdbc-source-watermark-initial"
            />
          </FormField>
        </>
      )}

      {/* P17 Req 4.1 / 4.6 / 4.7 — schema-detection orchestration. */}
      <DetectionPanel state={state} onChange={onChange} />

      {/* P17 Req 5.1 + 5.2 — schedule field with three preset buttons.
          Schedule lives at the top level of WizardState, not in sourceConfig. */}
      <FormField
        label="Schedule"
        description="EventBridge cron(...) or rate(...) expression — required for JDBC sources"
        errorText={scheduleError}
      >
        <SpaceBetween size="xs">
          <Input
            value={state.schedule ?? ''}
            onChange={({ detail }) =>
              onChange({ ...state, schedule: detail.value })
            }
            placeholder="rate(1 hour)"
            data-testid="jdbc-source-schedule"
          />
          <SpaceBetween direction="horizontal" size="xs">
            {SCHEDULE_PRESETS.map((preset) => (
              <Button
                key={preset.value}
                onClick={() => onChange({ ...state, schedule: preset.value })}
                data-testid={preset.testId}
              >
                {preset.label}
              </Button>
            ))}
          </SpaceBetween>
        </SpaceBetween>
      </FormField>
    </SpaceBetween>
  );
}
