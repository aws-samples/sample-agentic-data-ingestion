// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useEffect, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import FormField from '@cloudscape-design/components/form-field';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import {
  MAX_WORKSHEET_NAME_LENGTH,
  worksheetSelectionError,
  worksheetSelectionIsSet,
} from '../utils/worksheet-selection';

export interface WorksheetSelectionEditorProps {
  /**
   * The configured Selected_Worksheet as the pipeline detail response reports
   * it: `undefined` when the record carries no attribute, `''` when it carries
   * an empty one. **The two are not interchangeable** — see the component
   * docstring.
   */
  configured: string | undefined;
  /** Whether a save is in flight. */
  saving?: boolean;
  /**
   * Persist a new value. `''` means **clear** — the caller forwards it as-is and
   * the server REMOVEs the attribute. Any other string is sent verbatim,
   * untrimmed.
   */
  onSave: (next: string) => void | Promise<void>;
}

/**
 * p55 R8.4 — read and edit the Selected_Worksheet on an **existing** pipeline.
 *
 * ## Why this exists at all
 *
 * `create_pipeline` wrote this attribute and nothing ever read it back or
 * updated it. So a pipeline created without a worksheet — the default, because
 * the wizard field is optional and no workbook exists at creation time — had no
 * route to one, while `schema_extract`'s R8.4 refusal instructed the operator to
 * "set the worksheet selection on the pipeline". Delete-and-recreate was the only
 * way to follow that instruction. This is the surface that makes the instruction
 * true, and the reason it lives on the Schema tab: that is where a run's schema
 * extraction — the step that refuses — is displayed, so it is where an operator
 * reading the refusal already is.
 *
 * ## This control does not decide whether it is shown
 *
 * It renders whatever it is handed, and the caller decides whether it belongs on
 * the page at all. `SchemaHistoryPage` withholds it entirely from a JDBC pipeline
 * with no worksheet — no workbook, so nothing to choose — and collapses it for an
 * S3-upload pipeline with none configured, while rendering it uncollapsed the
 * moment `configured` is anything other than `undefined`. The three states below
 * are therefore also the caller's gate: a configured value, `''` included, is
 * never hidden. See the comment at the render site for the precedence.
 *
 * ## Absent, empty, and set are three states, and the UI says which
 *
 * The pipeline record's attribute is read downstream with a **membership test**
 * (`SELECTED_WORKSHEET_ATTRIBUTE in record`), never a truth test, so:
 *
 * - **absent** (`configured === undefined`) → no human has chosen. A single-sheet
 *   workbook is ingested from its one sheet (R8.1); a multi-sheet workbook
 *   **fails the run** and names every sheet it contains (R8.4).
 * - **empty** (`configured === ''`) → a *recorded* selection that names no sheet,
 *   which fails **every** run under R8.5 rather than falling back to the active
 *   sheet. No in-tree writer can produce this — clearing REMOVEs the attribute
 *   (`LGR-44d601e-1`) — so it can only arrive from a hand-edited record. It is
 *   rendered as the fault it is rather than as "not configured", because
 *   collapsing it into absent would hide a pipeline that cannot run.
 * - **set** → that sheet on every run (R8.3).
 *
 * Which is why nothing here reads `configured` with `||`: the falsy branch would
 * cover both absent and empty and the two have opposite consequences.
 *
 * ## Validation is reused, not restated
 *
 * `worksheetSelectionError` is the only check that is sound with no workbook in
 * hand — Excel's length cap and its forbidden characters — and it is shared with
 * the wizard's `WorksheetSelectionField` rather than re-expressed here. The
 * server deliberately applies no equivalent rule (see
 * `_submitted_worksheet_edit`), so this is the one definition, and a second one
 * here would be the drift `LGR-4854b3d-1` moved `schema-field-count.ts` into a
 * module to prevent.
 *
 * The name is submitted **verbatim and untrimmed** — a sheet may legitimately be
 * named `" Data "` — while `worksheetSelectionIsSet` trims to answer "has the
 * user typed anything", which is the same asymmetry the wizard draws.
 *
 * ## Accessibility
 *
 * The `FormField` label names the control and associates `errorText` with it, so
 * a validation failure is announced rather than only shown; `ariaLabel` names the
 * input independently of layout; the Save and Clear buttons are ordinary
 * Cloudscape buttons and so keyboard-operable and in tab order by construction.
 */
export default function WorksheetSelectionEditor({
  configured,
  saving = false,
  onSave,
}: WorksheetSelectionEditorProps) {
  // `?? ''` so the input is controlled in all three states. The draft is local;
  // `configured` remains the only statement about what is persisted.
  const [draft, setDraft] = useState(configured ?? '');

  // Re-seed when the server's value changes (a save landing, or a refetch), so
  // the field does not keep showing a stale draft after the record moved.
  useEffect(() => {
    setDraft(configured ?? '');
  }, [configured]);

  const errorText = worksheetSelectionError(draft);
  const isConfigured = configured !== undefined;
  const isRecordedEmpty = configured === '';
  const dirty = draft !== (configured ?? '');

  return (
    <Container
      header={
        <Header
          variant="h3"
          description="Which worksheet of an uploaded .xlsx workbook this pipeline ingests. Every run reads the same worksheet."
        >
          Excel worksheet
        </Header>
      }
      data-testid="worksheet-selection-editor"
    >
      <SpaceBetween size="s">
        {/* The read path, stated as three distinct facts rather than one
            truthiness test. `data-testid`s are per-state so a test cannot pass by
            finding the wrong one. */}
        {!isConfigured && (
          <Box color="text-body-secondary" data-testid="worksheet-state-absent">
            No worksheet is configured. A workbook with a single worksheet is ingested from that
            worksheet. A workbook with several worksheets <strong>fails the run</strong> and lists
            every worksheet it contains — set the name below and upload again.
          </Box>
        )}
        {isRecordedEmpty && (
          <Alert type="warning" data-testid="worksheet-state-recorded-empty">
            This pipeline records an <strong>empty</strong> worksheet selection, which names no
            worksheet, so every run fails. That is not the same as having none configured. Enter a
            worksheet name, or clear the selection to fall back to single-worksheet handling.
          </Alert>
        )}
        {isConfigured && !isRecordedEmpty && (
          <Box data-testid="worksheet-state-set">
            Configured worksheet: <strong data-testid="worksheet-configured-value">{configured}</strong>
          </Box>
        )}

        <FormField
          label="Worksheet name"
          errorText={errorText}
          // Worded so it shares no phrase with `worksheetSelectionError`'s
          // messages: the constraint is always visible and the error is not, so an
          // overlapping phrase would make "an error is shown" untestable.
          constraintText={`Maximum ${MAX_WORKSHEET_NAME_LENGTH} characters. The characters \\ / ? * [ ] : are not permitted by Excel.`}
        >
          <Input
            value={draft}
            onChange={({ detail }) => setDraft(detail.value)}
            placeholder="Worksheet name"
            ariaLabel="Name of the Excel worksheet this pipeline ingests"
            disabled={saving}
            data-testid="worksheet-selection-input"
          />
        </FormField>

        <SpaceBetween size="xs" direction="horizontal">
          <Button
            variant="primary"
            loading={saving}
            // Blocked on a name that could never match any workbook, and on a
            // no-op. NOT blocked on an empty draft: submitting empty is how the
            // selection is cleared, and the Clear button below is the explicit
            // route to the same intent.
            disabled={saving || !!errorText || !dirty}
            onClick={() => {
              // Verbatim. No trim — see the component docstring.
              void onSave(draft);
            }}
            data-testid="worksheet-selection-save"
          >
            Save worksheet
          </Button>
          <Button
            loading={saving}
            // Only offered when there is something to clear. Clearing REMOVEs the
            // attribute; it does not store an empty string.
            disabled={saving || !isConfigured}
            onClick={() => {
              setDraft('');
              void onSave('');
            }}
            data-testid="worksheet-selection-clear"
          >
            Clear selection
          </Button>
        </SpaceBetween>

        {dirty && worksheetSelectionIsSet(draft) && !errorText && (
          <Box color="text-status-info" data-testid="worksheet-selection-unsaved">
            Unsaved change. The new worksheet applies to runs started after you save.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}
