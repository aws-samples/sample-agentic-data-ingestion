// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet's gate, its validation and its wire key.
 *
 * Expectations derive from **R8.1 / R8.4 / R8.5 and from Excel's own worksheet
 * naming constraints**, not from what these functions currently return.
 *
 * The gate test is the one that matters, and it is not a formatting question.
 * `schema_extract._configured_worksheet` reads the pipeline record with a
 * MEMBERSHIP test, so an empty *recorded* selection refuses the run under R8.5
 * while an absent attribute lets a single-sheet workbook proceed under R8.1.
 * `worksheetSelectionIsSet` is what decides which of those two a blank input
 * becomes, so a `.trim()` in the wrong place here breaks every single-sheet Excel
 * pipeline created through the wizard.
 */
import {
  MAX_WORKSHEET_NAME_LENGTH,
  SELECTED_WORKSHEET_REQUEST_KEY,
  worksheetSelectionError,
  worksheetSelectionIsSet,
} from '../worksheet-selection';

describe('SELECTED_WORKSHEET_REQUEST_KEY', () => {
  // Must equal `SELECTED_WORKSHEET_ATTRIBUTE` in
  // `shared/pipeline_record_read.py`, which `create_pipeline` uses for BOTH the
  // request read and the record write. Pinned here so a rename on this side is a
  // failing test rather than a selection that silently never matches
  // (`LGR-eee8ac8-5`).
  it('is the attribute name the Python reader and writer share', () => {
    expect(SELECTED_WORKSHEET_REQUEST_KEY).toBe('selected_worksheet');
  });
});

describe('worksheetSelectionIsSet — absent and empty are different facts', () => {
  it.each([
    ['undefined', undefined],
    ['null', null],
    ['an empty string', ''],
    ['a single space', ' '],
    ['tabs and newlines', '\t\n  '],
  ])('treats %s as no selection, so the key is omitted rather than sent empty', (_label, input) => {
    expect(worksheetSelectionIsSet(input)).toBe(false);
  });

  it.each([
    ['a plain name', 'Sheet1'],
    ['a name with an inner space', 'Q3 Actuals'],
    ['a padded name', ' Data '],
    ['a name that is only punctuation', '-'],
  ])('treats %s as a selection', (_label, input) => {
    expect(worksheetSelectionIsSet(input)).toBe(true);
  });

  // The gate trims and the value does not. A sheet named with padding is legal
  // in Excel, so ` Data ` must both pass the gate and reach the wire unchanged —
  // the forwarding half is asserted in
  // `CreatePipelinePage.selected-worksheet.test.tsx`, which is where the value
  // actually reaches a request body.
  it('passes a padded name through the gate rather than rejecting it as empty', () => {
    expect(worksheetSelectionIsSet(' Data ')).toBe(true);
  });
});

describe('worksheetSelectionError — only what is impossible in every workbook', () => {
  it.each([
    ['undefined', undefined],
    ['an empty string', ''],
    ['whitespace only', '   '],
  ])('reports no error for %s, because no selection is valid (R8.1)', (_label, input) => {
    expect(worksheetSelectionError(input)).toBeUndefined();
  });

  it('accepts an ordinary worksheet name', () => {
    expect(worksheetSelectionError('Sheet1')).toBeUndefined();
  });

  it('accepts a name at exactly Excel’s length limit', () => {
    expect(worksheetSelectionError('a'.repeat(MAX_WORKSHEET_NAME_LENGTH))).toBeUndefined();
  });

  it('rejects a name one character past Excel’s length limit', () => {
    const error = worksheetSelectionError('a'.repeat(MAX_WORKSHEET_NAME_LENGTH + 1));
    expect(error).toBeDefined();
    expect(error).toContain(String(MAX_WORKSHEET_NAME_LENGTH));
  });

  it.each([['\\'], ['/'], ['?'], ['*'], ['['], [']'], [':']])(
    'rejects a name containing %s, which Excel forbids',
    (char) => {
      const error = worksheetSelectionError(`Sheet${char}1`);
      expect(error).toBeDefined();
      expect(error).toContain(char);
    },
  );

  // The validation is deliberately narrow: it cannot know which sheets exist,
  // because no workbook exists at wizard time. A name that is merely *absent*
  // from the workbook is R8.5's subject and must pass here, or the wizard would
  // be refusing on a fact it does not have.
  it('accepts a plausible name it cannot verify exists', () => {
    expect(worksheetSelectionError('Sheet That Does Not Exist')).toBeUndefined();
  });
});
