// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import {
  MAX_WORKSHEET_NAME_LENGTH,
  worksheetSelectionError,
} from '../../utils/worksheet-selection';
import type { WizardStepProps } from './types';

/**
 * p55 R8.4 — where the Selected_Worksheet is chosen.
 *
 * Rendered in **Step 2 (Source)**, under the `s3_upload` branch, because the
 * Worksheet is a property of the source object and `JdbcSourceForm` already
 * establishes that source-shaped configuration lives in that step. No seventh
 * wizard step is added; design §4.4 arm (a) was ratified partly because it needs
 * neither a new step nor a new Step Functions state.
 *
 * ## Why this is a text input and not a list
 *
 * `JdbcSourceDetectionPanel` is the named precedent for probing a live source and
 * showing the user what was found, and it is **structurally unavailable here**:
 * an Excel_Workbook can only arrive through `S3_UPLOAD`, whose upload prefix is
 * created by pipeline creation itself, and this wizard is create-only. The three
 * independent reasons are enumerated in `utils/worksheet-selection.ts`. A list
 * would have to be invented, and a guessed or hardcoded sheet list is worse than
 * no list: it would look authoritative while naming sheets the workbook may not
 * contain.
 *
 * So this control does the honest thing instead — it takes the name, validates
 * only what is true of every workbook, and **states the run-time consequence of
 * leaving it blank**, which is the part of design §4.4's residual the user has to
 * know about in advance:
 *
 * - blank + a single-sheet workbook → the run proceeds (R8.1);
 * - blank + a multi-sheet workbook → the run **fails and names every Worksheet
 *   present** (R8.4 residual), which is also how the user learns the names;
 * - set but naming a sheet the workbook lacks → the run fails and reports both
 *   the configured name and the names present (R8.5).
 *
 * The refusal is self-diagnosing by design, so a user who never read this text
 * still learns the sheet names and where the selection goes.
 *
 * ## Accessibility
 *
 * The `FormField` label names the control, `ariaLabel` names it for a screen
 * reader independently of layout, and Cloudscape's `Input` is keyboard-operable
 * and reachable in tab order by construction. `errorText` is associated with the
 * input by the `FormField`, so a validation failure is announced rather than only
 * shown.
 */
export function WorksheetSelectionField({ state, onChange }: WizardStepProps) {
  const value = state.selectedWorksheet ?? '';
  const errorText = worksheetSelectionError(value);

  return (
    <SpaceBetween size="s">
      <FormField
        label="Excel worksheet"
        description={
          'Optional. The name of the worksheet to ingest from an uploaded .xlsx workbook. ' +
          'Every run of this pipeline reads the same worksheet.'
        }
        errorText={errorText}
        // Deliberately worded so it shares no phrase with `worksheetSelectionError`'s
        // messages: the constraint is always visible and the error is not, so an
        // overlapping phrase makes "an error is shown" untestable.
        constraintText={`Maximum ${MAX_WORKSHEET_NAME_LENGTH} characters. The characters \\ / ? * [ ] : are not permitted by Excel.`}
      >
        <Input
          // `?? ''` rather than a bare read: a draft saved before this field
          // existed restores it as `undefined`, because `readDraft` is a bare
          // `JSON.parse(raw) as T` and `handleRestoreDraft` replaces the state
          // rather than merging into it. See the field's comment in `types.ts`.
          value={value}
          onChange={({ detail }) =>
            onChange({ ...state, selectedWorksheet: detail.value })
          }
          placeholder="Worksheet name (optional)"
          ariaLabel="Name of the Excel worksheet to ingest (optional)"
          data-testid="wizard-selected-worksheet"
        />
      </FormField>

      <Alert type="info" data-testid="worksheet-selection-residual">
        <Box>
          Leave this empty unless your workbook has more than one worksheet. A workbook with a
          single worksheet is ingested from that worksheet. A workbook with several worksheets and
          no selection <strong>fails the run</strong> and lists every worksheet it contains, so you
          can set the name here and upload again — it is never guessed for you.
        </Box>
      </Alert>
    </SpaceBetween>
  );
}
