// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * JdbcTargetForm — consolidated JDBC target form (replaces both legacy
 * `JdbcForm` and the P14 `RdsDatabaseForm` in `WizardStepTarget.tsx`).
 *
 * Validates: P17 Requirements 14.1, 14.2.
 *
 * The form is a thin wrapper around the shared `JdbcConnectionSelector`
 * (Task 18) — which already owns the connection-mode toggle, RDS dropdown,
 * Glue-connection dropdown, secret selector, and JDBC URL/driver/port inputs
 * with auto-population — plus the two target-specific inputs (`database_name`
 * and `table_name`) that the connection selector intentionally does not
 * include because they do not apply to source-side discovery.
 *
 * Per Requirement 14.4 the persisted `target_config.type` is always `'JDBC'`
 * (uppercase). The legacy `'RDS_DATABASE'` value is no longer produced; the
 * back-end's create_pipeline normaliser still accepts it for backward
 * compatibility with already-persisted pipelines.
 *
 * `jdbc_table` is derived as `database_name.table_name` whenever both are
 * populated — this matches the canonical key shape consumed by
 * `create_glue_pipeline.build_jdbc_arguments` / the JDBC PySpark templates.
 *
 * P19 Tasks 8.1-8.3 — Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.7,
 * 6.8. The form now owns its own `<Tiles>` write_mode selector positioned
 * after Database/Table Name. The parent `WizardStepTarget`'s legacy
 * target-agnostic write_mode `<Select>` is suppressed when targetType is
 * `'jdbc'` (Task 7.4) so this tile selector is the source of truth for
 * JDBC pipelines. Pipelines persisted with the legacy `'merge'` write_mode
 * surface an inline info alert and force the user to make an explicit choice
 * (Decision 4 — no silent backfill).
 */

import { useEffect } from 'react';
import Alert from '@cloudscape-design/components/alert';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tiles from '@cloudscape-design/components/tiles';
import { JdbcConnectionSelector } from './JdbcConnectionSelector';
import type { WizardStepProps } from './types';

/**
 * Build the canonical `jdbc_table` value as `database_name.table_name`.
 * Matches the P14 `buildJdbcTable` helper (kept here so we avoid an import
 * from the now-deleted RdsDatabaseForm code path).
 */
export function buildJdbcTable(databaseName: string, tableName: string): string {
  return `${databaseName}.${tableName}`;
}

/**
 * P19 Task 8.1 — Validates: Requirements 6.1, 6.7. Two-tile write-mode
 * selector exposed to the user for new and existing JDBC pipelines.
 * `'merge'` is intentionally NOT included — it remains a valid persisted
 * value (legacy P17 pipelines) but is not user-selectable in V1.
 */
export const WRITE_MODE_TILES = [
  {
    value: 'overwrite',
    label: 'Overwrite',
    description: 'Replaces table on every run',
  },
  {
    value: 'append',
    label: 'Append',
    description: 'Adds rows to existing table; columns must match',
  },
];

export function JdbcTargetForm({ state, onChange }: WizardStepProps) {
  /**
   * Apply a partial update to `targetConfig`, always keeping
   *   - `type = 'JDBC'` (Requirement 14.4)
   *   - `jdbc_table = database_name.table_name` whenever both are present
   *
   * `detectedTargetSchema` is reset on every update because the user may
   * have switched the underlying connection / table identity. The
   * source-side `detectedSourceSchema` is intentionally NOT cleared —
   * editing the target must not invalidate a source-side detection that
   * the user just performed.
   */
  function update(patch: Record<string, string>) {
    const merged = { ...state.targetConfig, ...patch };
    merged.type = 'JDBC';
    const dbName = patch.database_name ?? merged.database_name ?? '';
    const tblName = patch.table_name ?? merged.table_name ?? '';
    if (dbName && tblName) {
      merged.jdbc_table = buildJdbcTable(dbName, tblName);
    }
    onChange({ ...state, targetConfig: merged, detectedTargetSchema: null });
  }

  /**
   * P19 Task 8.2 — Validates: Requirement 6.4. Default `write_mode` to
   * `'overwrite'` for new JDBC pipelines. The effect skips when:
   *  - the user is editing a legacy `'merge'` pipeline (Task 8.3 handles
   *    that path — the user must make an explicit choice), or
   *  - `write_mode` is already populated (e.g. user picked a tile, or the
   *    pipeline was loaded for editing with a non-merge value).
   */
  useEffect(() => {
    if (state.targetType !== 'jdbc') return;
    const current = state.targetConfig.write_mode;
    if (current === 'merge') return; // Task 8.3 — force explicit re-pick
    if (current) return; // already set (user choice or edit-load)
    onChange({
      ...state,
      targetConfig: { ...state.targetConfig, write_mode: 'overwrite', type: 'JDBC' },
    });
    // We intentionally key only on `state.targetType` per the task description:
    // the effect should fire when the user enters the JDBC branch and not
    // re-fire on every targetConfig keystroke. The guards above prevent
    // overwriting a user choice that was made earlier in the same session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.targetType]);

  const isLegacyMerge = state.targetConfig.write_mode === 'merge';
  const tileValue = isLegacyMerge
    ? null
    : (state.targetConfig.write_mode ?? null);

  // Required-field feedback. The Step-2 navigation guard in CreatePipelinePage
  // blocks Next until the target table identity is resolvable (jdbc_table, or
  // both database_name + table_name). Surface that requirement inline so the
  // user sees WHY Next is doing nothing rather than silently failing.
  const dbNameValue = state.targetConfig.database_name ?? '';
  const tableNameValue = state.targetConfig.table_name ?? '';
  const dbNameError = dbNameValue.trim() ? undefined : 'Database name is required';
  const tableNameError = tableNameValue.trim() ? undefined : 'Table name is required';

  return (
    <SpaceBetween size="m">
      {/* Shared connection selector — owns mode toggle, RDS / Glue-connection /
          secret dropdowns, and the JDBC URL / driver / port inputs. */}
      <JdbcConnectionSelector
        config={state.targetConfig}
        onChange={(next) => {
          // Preserve the canonical `type = 'JDBC'` flag and recompute
          // `jdbc_table` if the user already typed a database/table.
          const merged = { ...next };
          merged.type = 'JDBC';
          const dbName = merged.database_name ?? '';
          const tblName = merged.table_name ?? '';
          if (dbName && tblName) {
            merged.jdbc_table = buildJdbcTable(dbName, tblName);
          }
          onChange({ ...state, targetConfig: merged, detectedTargetSchema: null });
        }}
        testIdPrefix="jdbc-target-conn"
      />

      {/* Target-specific inputs that the shared selector deliberately does
          NOT own (they don't apply to source-side discovery). Both are
          REQUIRED — the pipeline needs a concrete write destination. */}
      <FormField
        label="Database Name"
        description="The database/schema in the target that the pipeline writes to"
        errorText={dbNameError}
      >
        <Input
          value={dbNameValue}
          onChange={({ detail }) => update({ database_name: detail.value })}
          placeholder="mydb"
          data-testid="jdbc-target-database-name"
        />
      </FormField>
      <FormField label="Table Name" errorText={tableNameError}>
        <Input
          value={tableNameValue}
          onChange={({ detail }) => update({ table_name: detail.value })}
          placeholder="customers"
          data-testid="jdbc-target-table-name"
        />
      </FormField>

      {/* P19 Tasks 8.1, 8.3 — Validates: Requirements 6.1, 6.2, 6.3, 6.5, 6.7,
          6.8. Write-mode tiles immediately after database/table identity, so
          they group visually with the destination rather than the connection
          details. Legacy `'merge'` shows an info alert above the tiles and
          leaves both tiles unselected until the user picks. */}
      <FormField label="Write Mode">
        <SpaceBetween size="s">
          {isLegacyMerge && (
            <Alert type="info" data-testid="jdbc-write-mode-legacy-merge-alert">
              This pipeline uses the legacy &apos;merge&apos; write mode. Saving
              will reset the write mode to a value you select below.
            </Alert>
          )}
          <Tiles
            value={tileValue}
            onChange={({ detail }) =>
              onChange({
                ...state,
                targetConfig: {
                  ...state.targetConfig,
                  type: 'JDBC',
                  write_mode: detail.value,
                },
              })
            }
            items={WRITE_MODE_TILES}
            data-testid="jdbc-write-mode-tiles"
          />
        </SpaceBetween>
      </FormField>
    </SpaceBetween>
  );
}

export default JdbcTargetForm;
