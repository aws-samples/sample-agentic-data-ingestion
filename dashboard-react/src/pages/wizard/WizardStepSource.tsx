// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Tiles from '@cloudscape-design/components/tiles';
import { JdbcSourceForm } from './JdbcSourceForm';
import { WorksheetSelectionField } from './WorksheetSelectionField';
import type { WizardState, WizardStepProps } from './types';

/**
 * P17 — Wizard Step: Source.
 *
 * The JDBC source subform (connection selector + schema/table + read-mode +
 * watermark fields + schedule + schema-detection panel) lives in
 * `JdbcSourceForm.tsx`; the schema-detection orchestration lives in
 * `JdbcSourceDetectionPanel.tsx`; and the identifier / schedule validators
 * live in `source-validation.ts` (P29 R11.5 decomposition — this file was
 * split for size, behavior-preserving).
 *
 * The validators are re-exported below so existing importers
 * (`import { validateIdentifier, ... } from './WizardStepSource'`) keep
 * working unchanged.
 *
 * Validates: P17 Requirements 1.1-1.6, 2.1, 3.1-3.6, 4.1, 4.6, 4.7,
 *            5.1, 5.2, 6.3, 22.1.
 */

// Re-export the source-form validators from their extracted module so the
// existing import path (`../WizardStepSource`) remains stable for tests and
// any other consumers.
export {
  IDENTIFIER_RE,
  EB_CRON_RE,
  EB_RATE_RE,
  validateScheduleExpression,
  validateIdentifier,
} from './source-validation';

type SourceType = WizardState['sourceType'];

const SOURCE_TYPE_OPTIONS: Array<{
  value: SourceType;
  label: string;
  description: string;
}> = [
  {
    value: 's3_upload',
    label: 'S3 Upload',
    description: 'Files uploaded to the Agentic Data Ingestion source bucket trigger the pipeline',
  },
  {
    value: 'jdbc',
    label: 'JDBC Database',
    description:
      'Read from Postgres, MySQL, Oracle, or SQL Server on a schedule or on demand',
  },
];

export function WizardStepSource({ state, onChange }: WizardStepProps) {
  function handleSourceTypeChange(newType: SourceType) {
    if (newType === state.sourceType) return;
    // Reset source-specific config and (for JDBC) the schedule when switching
    // source types so stale fields from a previous selection do not leak into
    // the pipeline record. Schedule is required for JDBC and optional for
    // S3 upload (P17 Requirement 5.1), so clearing it on type change keeps
    // the wizard's validation state consistent.
    //
    // p55 R8.4 — `selectedWorksheet` is cleared for the same stated reason, and
    // it matters more here than for the other fields: an Excel_Workbook can only
    // arrive through `S3_UPLOAD`, so a worksheet name left behind by a switch to
    // JDBC would be persisted as a key nothing ever reads (`LGR-eee8ac8-6`).
    onChange({
      ...state,
      sourceType: newType,
      sourceConfig: {},
      schedule: null,
      selectedWorksheet: '',
      detectedSourceSchema: null,
    });
  }

  return (
    <Container header={<Header variant="h2">Source</Header>}>
      <SpaceBetween size="l">
        <FormField
          label="Source Type"
          description="Where should the pipeline read data from?"
        >
          <Tiles
            value={state.sourceType}
            onChange={({ detail }) =>
              handleSourceTypeChange(detail.value as SourceType)
            }
            items={SOURCE_TYPE_OPTIONS}
            data-testid="jdbc-source-tiles"
          />
        </FormField>

        {state.sourceType === 's3_upload' && (
          <>
            <Alert type="info" data-testid="s3-upload-alert" header="Upload location">
              <Box>
                Once this pipeline is created, upload files to{' '}
                <code>{'s3://{source_bucket}/{pipeline_id}/'}</code> to trigger a
                pipeline run. The exact bucket name is shown on the pipeline
                overview page after creation.
              </Box>
            </Alert>

            {/* p55 R8.4 — the Selected_Worksheet, immediately beside the alert
                that explains why no workbook exists yet to read a sheet list
                from. Only under `s3_upload`: that is the one source type an
                Excel_Workbook can arrive through. */}
            <WorksheetSelectionField state={state} onChange={onChange} />
          </>
        )}

        {state.sourceType === 'jdbc' && (
          <JdbcSourceForm state={state} onChange={onChange} />
        )}
      </SpaceBetween>
    </Container>
  );
}
