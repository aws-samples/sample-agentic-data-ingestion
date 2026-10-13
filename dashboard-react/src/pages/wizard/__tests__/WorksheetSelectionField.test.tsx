// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet control, and where it appears.
 *
 * Five things are asserted, and the second and third carry the requirement
 * weight:
 *
 * | # | Requirement | What |
 * |---|---|---|
 * | 1 | R8.4 | the control exists in the Source step, under `s3_upload` only |
 * | 2 | R8.1 | it is **optional** — an empty value is not an error |
 * | 3 | R8.4 residual | the step states the run-time consequence of leaving it blank |
 * | 4 | R8.4 | the typed value reaches wizard state **verbatim**, padding included |
 * | 5 | accessibility | labelled, and reachable/operable by keyboard |
 *
 * Test 3 exists because the honest answer to "where does the wizard get the sheet
 * list from" is *nowhere* — no workbook exists at pipeline-creation time (see
 * `utils/worksheet-selection.ts` for the three structural reasons). The user
 * therefore has to be told, before Create, that a multi-sheet workbook with no
 * selection fails the run and lists its worksheets. Without that statement the
 * blank field looks like a harmless default.
 *
 * The stateful harness and the `../../../hooks` barrel mock are lifted from
 * `WizardStepSource.test.tsx`: rendering the real `WizardStepSource` (rather than
 * `WorksheetSelectionField` in isolation) is deliberate, because *which step the
 * control lands in* and *whether it survives a source-type switch* are both part
 * of what this task decided.
 */
import { describe, it, expect, vi } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepSource } from '../WizardStepSource';
import { INITIAL_WIZARD_STATE, type WizardState } from '../types';
// The oracle for the two error assertions. The expected strings are built by the
// validator rather than transcribed, so a reworded message cannot make an
// "an error is shown" assertion pass vacuously (`LGR-28b4d75-5`).
import {
  MAX_WORKSHEET_NAME_LENGTH,
  worksheetSelectionError,
} from '../../../utils/worksheet-selection';

const FORBIDDEN_NAME = 'Q3/Q4';
const TOO_LONG_NAME = 'a'.repeat(MAX_WORKSHEET_NAME_LENGTH + 1);

function forbiddenCharMessage(): string {
  return worksheetSelectionError(FORBIDDEN_NAME)!;
}

function tooLongMessage(): string {
  return worksheetSelectionError(TOO_LONG_NAME)!;
}

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useJdbcDetectSchemaStart: () => ({ mutate: vi.fn(), isPending: false }),
    useJdbcDetectSchemaStatus: () => ({ data: undefined, isError: false, error: null }),
  };
});

function StatefulHarness({
  initial,
  onState,
}: {
  initial: Partial<WizardState>;
  onState?: (s: WizardState) => void;
}) {
  const [state, setState] = useState<WizardState>({ ...INITIAL_WIZARD_STATE, ...initial });
  return (
    <WizardStepSource
      state={state}
      onChange={(next) => {
        setState(next);
        onState?.(next);
      }}
    />
  );
}

function renderHarness(initial: Partial<WizardState> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onState = vi.fn();
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <StatefulHarness initial={initial} onState={onState} />
    </QueryClientProvider>,
  );
  return { onState, ...utils };
}

/** The `<input>` inside the worksheet control, found by its accessible name. */
function worksheetInput(): HTMLInputElement {
  return screen.getByLabelText(
    'Name of the Excel worksheet to ingest (optional)',
  ) as HTMLInputElement;
}

describe('WorksheetSelectionField inside the Source step', () => {
  it('renders for an S3 upload source', () => {
    renderHarness({ sourceType: 's3_upload' });
    expect(screen.getByTestId('wizard-selected-worksheet')).toBeInTheDocument();
  });

  // An Excel_Workbook can only arrive through `S3_UPLOAD`, so offering the control
  // for a JDBC source would invite a selection nothing ever reads
  // (`LGR-eee8ac8-6`).
  it('does not render for a JDBC source', () => {
    renderHarness({ sourceType: 'jdbc' });
    expect(screen.queryByTestId('wizard-selected-worksheet')).not.toBeInTheDocument();
  });

  it('is optional — an empty value shows no error (R8.1)', () => {
    renderHarness({ selectedWorksheet: '' });
    expect(worksheetInput().value).toBe('');
    // The two messages the validator can produce, built from the validator
    // itself so a reworded message cannot make this pass vacuously.
    for (const message of [tooLongMessage(), forbiddenCharMessage()]) {
      expect(screen.queryByText(message)).not.toBeInTheDocument();
    }
  });

  it('states that a multi-sheet workbook with no selection fails the run and lists its worksheets', () => {
    renderHarness();
    const residual = screen.getByTestId('worksheet-selection-residual');
    expect(residual).toHaveTextContent(/fails the run/i);
    expect(residual).toHaveTextContent(/lists every worksheet/i);
    // And that it is never guessed — the behaviour R8.3 exists to prevent.
    expect(residual).toHaveTextContent(/never guessed/i);
  });

  it('surfaces the validation failure for a name Excel could not accept', () => {
    renderHarness({ selectedWorksheet: FORBIDDEN_NAME });
    expect(screen.getByText(forbiddenCharMessage())).toBeInTheDocument();
  });

  it('surfaces the validation failure for a name past Excel’s length limit', () => {
    renderHarness({ selectedWorksheet: TOO_LONG_NAME });
    expect(screen.getByText(tooLongMessage())).toBeInTheDocument();
  });

  it('reports the typed value upward verbatim, padding included', () => {
    const { onState } = renderHarness();
    fireEvent.change(worksheetInput(), { target: { value: ' Data ' } });
    expect(onState).toHaveBeenCalledWith(
      expect.objectContaining({ selectedWorksheet: ' Data ' }),
    );
  });

  // p55 R8.4 — a worksheet name left behind by a switch to JDBC would be
  // persisted as a key nothing reads, so the source-type handler clears it for
  // the same reason it already clears `sourceConfig` and `schedule`.
  it('is cleared when the source type changes to JDBC', () => {
    const { onState } = renderHarness({ sourceType: 's3_upload', selectedWorksheet: 'Sheet1' });
    fireEvent.click(screen.getByLabelText('JDBC Database'));
    expect(onState).toHaveBeenCalledWith(
      expect.objectContaining({ sourceType: 'jdbc', selectedWorksheet: '' }),
    );
  });

  it('is keyboard-reachable and has an accessible name', () => {
    renderHarness();
    const input = worksheetInput();
    // A native text input is in the tab order unless explicitly removed.
    expect(input.tabIndex).toBeGreaterThanOrEqual(0);
    expect(input).toHaveAccessibleName();
    input.focus();
    expect(document.activeElement).toBe(input);
  });
});
