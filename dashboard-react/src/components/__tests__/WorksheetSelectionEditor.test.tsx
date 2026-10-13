// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet's read and edit surface on an existing pipeline.
 *
 * The defect this closes: `create_pipeline` wrote the attribute and nothing read
 * it back or updated it, so a pipeline created without a worksheet had no route
 * to one — while `schema_extract`'s R8.4 refusal instructed the operator to set
 * it. Delete-and-recreate was the only way to comply.
 *
 * ## What carries the requirement weight here
 *
 * | # | Requirement | What |
 * |---|---|---|
 * | 1 | R8.4 read | the configured worksheet is **shown**, so the operator can see what is set |
 * | 2 | R8.1 / R8.5 | **absent**, **recorded-empty** and **set** render as three distinct states |
 * | 3 | R8.3 | a name can be set and changed, and reaches the caller **verbatim** |
 * | 4 | R8.5 | clearing submits `''`, which the server REMOVEs — it never stores `''` |
 * | 5 | R8.4 | validation is `worksheetSelectionError`'s, not a second rule |
 * | 6 | accessibility | labelled, keyboard-operable, errors associated with the input |
 *
 * Tests 2 and 4 are the load-bearing ones. The reader
 * (`schema_extract._configured_worksheet`) uses a **membership** test on the
 * record, so absent and empty have opposite run-time consequences: absent lets a
 * single-sheet workbook proceed (R8.1) while empty is a *recorded selection
 * naming no sheet* and refuses every run (R8.5). A UI that rendered
 * `configured || 'none'` would show a pipeline that cannot run as though nothing
 * were configured, and a clear that stored `''` would create that state.
 *
 * Expected error strings are produced by `worksheetSelectionError` rather than
 * transcribed, so a reworded message cannot make an "an error is shown"
 * assertion pass vacuously (`LGR-28b4d75-5`).
 *
 * ## Whether the control is shown at all is not tested here
 *
 * It renders whatever `configured` it is handed, deliberately — the caller
 * decides whether it belongs on the page. `SchemaHistoryPage` withholds it from a
 * JDBC pipeline with no worksheet and collapses it for an S3-upload pipeline with
 * none configured, and both are asserted in
 * `pages/__tests__/SchemaHistoryPage.selected-worksheet.test.tsx`. Note the
 * consequence for the states below: `''` counts as **configured** on both sides
 * of that boundary, so the recorded-empty tests here are also what keep the one
 * state that refuses every run out of the collapsed arm.
 */
import { describe, it, expect, vi } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import WorksheetSelectionEditor from '../WorksheetSelectionEditor';
import {
  MAX_WORKSHEET_NAME_LENGTH,
  worksheetSelectionError,
} from '../../utils/worksheet-selection';

function renderEditor(configured: string | undefined, onSave = vi.fn()) {
  render(<WorksheetSelectionEditor configured={configured} onSave={onSave} />);
  return onSave;
}

/** A harness that reflects a save back into `configured`, as the page does. */
function StatefulEditor({ initial, onSave }: { initial: string | undefined; onSave: (v: string) => void }) {
  const [configured, setConfigured] = useState<string | undefined>(initial);
  return (
    <WorksheetSelectionEditor
      configured={configured}
      onSave={(next) => {
        onSave(next);
        // The server REMOVEs on `''`, so the record comes back with no attribute.
        setConfigured(next === '' ? undefined : next);
      }}
    />
  );
}

function input(): HTMLInputElement {
  return screen.getByTestId('worksheet-selection-input').querySelector('input')!;
}

/** The DOM `<button>` a Cloudscape `Button`'s `data-testid` lands on or wraps. */
function button(testId: string): HTMLButtonElement {
  const el = screen.getByTestId(testId);
  return (el.tagName === 'BUTTON' ? el : el.querySelector('button')!) as HTMLButtonElement;
}

function type(value: string) {
  fireEvent.change(input(), { target: { value } });
}

describe('WorksheetSelectionEditor — the read path (R8.4)', () => {
  it('shows the configured worksheet name', () => {
    renderEditor('Q3 Actuals');

    expect(screen.getByTestId('worksheet-configured-value')).toHaveTextContent('Q3 Actuals');
  });

  it('seeds the input with the configured name, so the operator edits rather than retypes', () => {
    renderEditor('Q3 Actuals');

    expect(input().value).toBe('Q3 Actuals');
  });

  it('shows padding in the configured name rather than trimming it for display', () => {
    // A sheet may legitimately be named `" Data "`. Trimming the DISPLAY would
    // make a correctly-configured pipeline look misconfigured.
    renderEditor(' Data ');

    expect(input().value).toBe(' Data ');
  });
});

describe('WorksheetSelectionEditor — absent, empty and set are three states (R8.1 / R8.5)', () => {
  it('renders the absent state when no attribute is configured', () => {
    renderEditor(undefined);

    expect(screen.getByTestId('worksheet-state-absent')).toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-recorded-empty')).not.toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-set')).not.toBeInTheDocument();
  });

  it('renders a recorded-empty selection as its own state, NOT as absent', () => {
    // The trap. `''` is a selection naming no sheet and refuses every run, so
    // collapsing it into "nothing configured" hides a pipeline that cannot run.
    renderEditor('');

    expect(screen.getByTestId('worksheet-state-recorded-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-absent')).not.toBeInTheDocument();
  });

  it('renders the set state when a name is configured', () => {
    renderEditor('Sheet1');

    expect(screen.getByTestId('worksheet-state-set')).toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-absent')).not.toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-recorded-empty')).not.toBeInTheDocument();
  });

  it('states the multi-worksheet consequence in the absent state', () => {
    // R8.4's refusal is the only place the sheet names come from, so an operator
    // has to know in advance that a blank selection fails a multi-sheet workbook.
    renderEditor(undefined);

    expect(screen.getByTestId('worksheet-state-absent').textContent).toMatch(/fails the run/i);
  });

  it('never renders absent and recorded-empty at the same time, for any state', () => {
    for (const configured of [undefined, '', 'Sheet1', ' Data ']) {
      const { unmount } = render(
        <WorksheetSelectionEditor configured={configured} onSave={vi.fn()} />,
      );
      const shown = [
        'worksheet-state-absent',
        'worksheet-state-recorded-empty',
        'worksheet-state-set',
      ].filter((id) => screen.queryByTestId(id) !== null);
      expect(shown).toHaveLength(1);
      unmount();
    }
  });
});

describe('WorksheetSelectionEditor — setting and changing (R8.3)', () => {
  it('submits a typed name to onSave', async () => {
    const onSave = renderEditor(undefined);

    type('Q3 Actuals');
    fireEvent.click(screen.getByTestId('worksheet-selection-save'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith('Q3 Actuals'));
  });

  it('submits the name VERBATIM, padding included', async () => {
    // Trimming here would make a sheet named `" Data "` permanently
    // unselectable. The gate trims; the value does not.
    const onSave = renderEditor(undefined);

    type(' Data ');
    fireEvent.click(screen.getByTestId('worksheet-selection-save'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(' Data '));
  });

  it('changes an already-configured name', async () => {
    const onSave = renderEditor('Sheet One');

    type('Sheet Two');
    fireEvent.click(screen.getByTestId('worksheet-selection-save'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith('Sheet Two'));
  });

  it('does not offer a save when nothing changed', () => {
    renderEditor('Sheet1');

    expect(button('worksheet-selection-save').disabled).toBe(true);
  });

  it('re-seeds the input when the configured value changes underneath it', () => {
    const { rerender } = render(<WorksheetSelectionEditor configured="Sheet One" onSave={vi.fn()} />);
    expect(input().value).toBe('Sheet One');

    rerender(<WorksheetSelectionEditor configured="Sheet Two" onSave={vi.fn()} />);

    expect(input().value).toBe('Sheet Two');
  });
});

describe('WorksheetSelectionEditor — clearing (R8.5)', () => {
  it('submits an empty string to clear, which the server REMOVEs', async () => {
    const onSave = renderEditor('Sheet1');

    fireEvent.click(screen.getByTestId('worksheet-selection-clear'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(''));
  });

  it('offers no clear when there is nothing configured', () => {
    renderEditor(undefined);

    expect(button('worksheet-selection-clear').disabled).toBe(true);
  });

  it('offers a clear for a recorded-empty selection, which is the way out of it', () => {
    // `''` on the record refuses every run. Clearing it REMOVEs the attribute and
    // restores single-worksheet handling (R8.1), so this control must be live.
    renderEditor('');

    expect(button('worksheet-selection-clear').disabled).toBe(false);
  });

  it('clearing an emptied field through Save submits the same clear intent', async () => {
    const onSave = renderEditor('Sheet1');

    type('');
    fireEvent.click(screen.getByTestId('worksheet-selection-save'));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith(''));
  });

  it('a cleared worksheet reads back as absent, not as recorded-empty', async () => {
    // The round trip. REMOVE means the next read reports absence, so the UI must
    // land on the absent state — the state that lets a single-sheet workbook run.
    const onSave = vi.fn();
    render(<StatefulEditor initial="Sheet1" onSave={onSave} />);

    fireEvent.click(screen.getByTestId('worksheet-selection-clear'));

    await waitFor(() => expect(screen.getByTestId('worksheet-state-absent')).toBeInTheDocument());
    expect(screen.queryByTestId('worksheet-state-recorded-empty')).not.toBeInTheDocument();
    expect(onSave).toHaveBeenCalledWith('');
  });

  it('a set worksheet reads back as set', async () => {
    const onSave = vi.fn();
    render(<StatefulEditor initial={undefined} onSave={onSave} />);

    type('Q3 Actuals');
    fireEvent.click(screen.getByTestId('worksheet-selection-save'));

    await waitFor(() => expect(screen.getByTestId('worksheet-configured-value')).toHaveTextContent('Q3 Actuals'));
  });
});

describe('WorksheetSelectionEditor — validation is reused, not restated (R8.4)', () => {
  it('rejects a name past Excel\u2019s length cap with the shared message', () => {
    renderEditor(undefined);
    const tooLong = 'x'.repeat(MAX_WORKSHEET_NAME_LENGTH + 1);

    type(tooLong);

    const expected = worksheetSelectionError(tooLong);
    expect(expected).toBeDefined();
    expect(screen.getByText(expected!)).toBeInTheDocument();
  });

  it('rejects a name containing a character Excel forbids, with the shared message', () => {
    renderEditor(undefined);

    type('Q3/Actuals');

    const expected = worksheetSelectionError('Q3/Actuals');
    expect(expected).toBeDefined();
    expect(screen.getByText(expected!)).toBeInTheDocument();
  });

  it('blocks the save while the name is impossible', () => {
    renderEditor(undefined);

    type('Q3/Actuals');

    expect(button('worksheet-selection-save').disabled).toBe(true);
  });

  it('treats an empty value as valid, because no selection is valid (R8.1)', () => {
    renderEditor('Sheet1');

    type('');

    expect(worksheetSelectionError('')).toBeUndefined();
    expect(button('worksheet-selection-save').disabled).toBe(false);
  });

  it('accepts a padded name — padding is not an error', () => {
    renderEditor(undefined);

    type(' Data ');

    expect(worksheetSelectionError(' Data ')).toBeUndefined();
    expect(button('worksheet-selection-save').disabled).toBe(false);
  });
});

describe('WorksheetSelectionEditor — accessibility', () => {
  it('names the input for a screen reader independently of layout', () => {
    renderEditor(undefined);

    expect(
      screen.getByLabelText('Name of the Excel worksheet this pipeline ingests'),
    ).toBeInTheDocument();
  });

  it('is reachable and operable by keyboard', () => {
    renderEditor(undefined);
    const el = input();

    el.focus();
    expect(document.activeElement).toBe(el);
    expect(el).not.toHaveAttribute('tabindex', '-1');
  });

  it('associates the validation error with the input rather than only showing it', () => {
    renderEditor(undefined);

    type('Q3/Actuals');

    // Cloudscape's FormField wires errorText to the control via aria-describedby.
    const describedBy = input().getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    const described = describedBy!.split(' ').map((id) => document.getElementById(id)?.textContent ?? '');
    expect(described.join(' ')).toContain(worksheetSelectionError('Q3/Actuals')!);
  });
});
