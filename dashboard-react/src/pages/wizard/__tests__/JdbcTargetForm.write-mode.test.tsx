// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Example tests for the `<Tiles>` write_mode selector in `JdbcTargetForm`.
 *
 * Covers P19 Task 8.5:
 *   - Both tiles render in the documented DOM order (Req 6.1, 6.2).
 *   - Default 'overwrite' is pre-selected for new JDBC pipelines (Req 6.4).
 *   - Clicking 'append' updates state.targetConfig.write_mode (Req 6.3).
 *   - Target-type reset clears targetConfig including write_mode (Req 6.6).
 *     This test lives next to the parent WizardStepTarget unit test which
 *     owns the actual reset wiring; here we just verify the form's
 *     contribution.
 *   - Legacy 'merge' renders an info alert and leaves both tiles
 *     unselected (Req 6.5, 6.8).
 *   - 'merge' is NOT one of the user-selectable tile options (Req 6.7).
 *
 * Validates: P19 Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup, within } from '@testing-library/react';
import { JdbcTargetForm, WRITE_MODE_TILES } from '../JdbcTargetForm';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
  };
});

function renderForm(overrides: Partial<WizardState> = {}) {
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    targetType: 'jdbc',
    ...overrides,
  };
  const onChange = vi.fn();
  render(<JdbcTargetForm state={state} onChange={onChange} />);
  return { state, onChange };
}

describe('JdbcTargetForm — write_mode tiles (P19)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  // -----------------------------------------------------------------
  // Req 6.1, 6.7 — both tiles render; 'merge' is not an option
  // -----------------------------------------------------------------

  it('renders both write_mode tiles (Overwrite + Append)', () => {
    renderForm();
    const tiles = screen.getByTestId('jdbc-write-mode-tiles');
    expect(within(tiles).getByText('Overwrite')).toBeInTheDocument();
    expect(within(tiles).getByText('Append')).toBeInTheDocument();
  });

  it('does NOT render a "Merge" tile (Req 6.7)', () => {
    renderForm();
    const tiles = screen.getByTestId('jdbc-write-mode-tiles');
    expect(within(tiles).queryByText('Merge')).not.toBeInTheDocument();
  });

  it('exports exactly two tile options with the documented values', () => {
    expect(WRITE_MODE_TILES.map((t) => t.value)).toEqual([
      'overwrite',
      'append',
    ]);
  });

  // -----------------------------------------------------------------
  // Req 6.2 — write_mode tiles render after Database Name + Table Name
  // -----------------------------------------------------------------

  it('renders Write Mode AFTER Database Name and Table Name', () => {
    const { container } = render(
      <JdbcTargetForm
        state={{ ...INITIAL_WIZARD_STATE, targetType: 'jdbc' }}
        onChange={vi.fn()}
      />,
    );
    // FormField labels appear in textual DOM order. Find them all and
    // confirm the documented order: Database Name → Table Name → Write Mode.
    const labels = Array.from(container.querySelectorAll('*')).filter(
      (el) => el.textContent === 'Database Name'
        || el.textContent === 'Table Name'
        || el.textContent === 'Write Mode',
    );
    // Take the first occurrence of each label to filter wrapper duplicates.
    const seen = new Map<string, number>();
    for (let i = 0; i < labels.length; i++) {
      const text = labels[i].textContent ?? '';
      if (!seen.has(text)) seen.set(text, i);
    }
    expect(seen.get('Database Name')).toBeLessThan(seen.get('Table Name')!);
    expect(seen.get('Table Name')).toBeLessThan(seen.get('Write Mode')!);
  });

  // -----------------------------------------------------------------
  // Req 6.4 — default 'overwrite' for new JDBC pipelines
  // -----------------------------------------------------------------

  it('defaults write_mode to "overwrite" via useEffect on mount when targetType=jdbc and write_mode is unset', () => {
    const { onChange } = renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC' },
    });
    // The Task 8.2 effect fires once on mount and writes
    // write_mode = 'overwrite'. Confirm the most recent onChange call
    // contains that update.
    const calls = onChange.mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    const writeModeCalls = calls.filter(
      (args) => args[0]?.targetConfig?.write_mode === 'overwrite',
    );
    expect(writeModeCalls.length).toBeGreaterThan(0);
  });

  it('does NOT reset write_mode when it is already populated', () => {
    const { onChange } = renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC', write_mode: 'append' },
    });
    // No call should set write_mode back to 'overwrite' — the effect
    // skips when current is truthy.
    const overwriteCalls = onChange.mock.calls.filter(
      (args) => args[0]?.targetConfig?.write_mode === 'overwrite',
    );
    expect(overwriteCalls).toHaveLength(0);
  });

  // -----------------------------------------------------------------
  // Req 6.3 — clicking a tile updates state.targetConfig.write_mode
  // -----------------------------------------------------------------

  it('clicking the Append tile updates state.targetConfig.write_mode to "append"', () => {
    const { onChange } = renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC', write_mode: 'overwrite' },
    });
    onChange.mockClear();

    // The Cloudscape <Tiles> component renders a radio-group with one
    // input per tile. Locate the 'append' input by value.
    const appendRadio = document.querySelector(
      'input[type="radio"][value="append"]',
    ) as HTMLInputElement | null;
    expect(appendRadio).not.toBeNull();
    fireEvent.click(appendRadio!);

    expect(onChange).toHaveBeenCalled();
    const last = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(last.targetConfig.write_mode).toBe('append');
    // type='JDBC' must be preserved on every update (Req 14.4 / Decision).
    expect(last.targetConfig.type).toBe('JDBC');
  });

  // -----------------------------------------------------------------
  // Req 6.5, 6.8 — legacy 'merge' info alert + neither tile selected
  // -----------------------------------------------------------------

  it('renders an info alert when the persisted write_mode is "merge"', () => {
    renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC', write_mode: 'merge' },
    });
    expect(
      screen.getByTestId('jdbc-write-mode-legacy-merge-alert'),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/legacy 'merge' write mode/),
    ).toBeInTheDocument();
  });

  it('leaves both tiles unselected when persisted write_mode is "merge"', () => {
    renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC', write_mode: 'merge' },
    });
    const overwriteRadio = document.querySelector(
      'input[type="radio"][value="overwrite"]',
    ) as HTMLInputElement | null;
    const appendRadio = document.querySelector(
      'input[type="radio"][value="append"]',
    ) as HTMLInputElement | null;
    expect(overwriteRadio).not.toBeNull();
    expect(appendRadio).not.toBeNull();
    expect(overwriteRadio!.checked).toBe(false);
    expect(appendRadio!.checked).toBe(false);
  });

  it('preserves "merge" until the user clicks a tile (keeps Step 2 Next disabled)', () => {
    // Step 2's Next-disabled predicate in CreatePipelinePage is
    //   isJdbcWriteModeBlocked = targetType === 'jdbc' &&
    //     !['overwrite', 'append'].includes(write_mode ?? '')
    // The form's contract for that predicate has two parts:
    //   (a) Do NOT silently rewrite a persisted 'merge' to a valid value
    //       on mount — that would unblock Next without an explicit user
    //       choice (Decision 4: no silent backfill).
    //   (b) When the user clicks a tile, write_mode flips to a value
    //       that the predicate accepts ('overwrite' or 'append'),
    //       unblocking Next.
    const { onChange } = renderForm({
      targetType: 'jdbc',
      targetConfig: { type: 'JDBC', write_mode: 'merge' },
    });

    // (a) No call should resolve write_mode to 'overwrite' or 'append'
    //     while it is still 'merge'.
    const autoResolves = onChange.mock.calls.filter((args) => {
      const wm = args[0]?.targetConfig?.write_mode;
      return wm === 'overwrite' || wm === 'append';
    });
    expect(autoResolves).toHaveLength(0);

    // (b) User clicks Append → write_mode becomes 'append'.
    onChange.mockClear();
    const appendRadio = document.querySelector(
      'input[type="radio"][value="append"]',
    ) as HTMLInputElement | null;
    fireEvent.click(appendRadio!);
    const last = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(last.targetConfig.write_mode).toBe('append');
    // Sanity: the new value is in the predicate's allow-list.
    expect(['overwrite', 'append']).toContain(last.targetConfig.write_mode);
  });

  // -----------------------------------------------------------------
  // Req 6.6 — target-type reset clears targetConfig (incl. write_mode)
  // -----------------------------------------------------------------

  it('the target-type reset path clears targetConfig (including write_mode)', () => {
    // The reset is owned by WizardStepTarget's target-type Select
    // onChange handler, not by JdbcTargetForm. We assert the
    // contract here so a regression in JdbcTargetForm that, e.g.,
    // re-populates write_mode in a way that survives the reset
    // would fail this test.
    //
    // Concretely: when the user changes target type from 'jdbc' to
    // anything else, the new state has `targetConfig: {}` and the
    // form is no longer rendered. We confirm the form's effect does
    // NOT re-populate write_mode for non-jdbc targetType.
    const onChange = vi.fn();
    render(
      <JdbcTargetForm
        state={{
          ...INITIAL_WIZARD_STATE,
          // targetType is NOT 'jdbc' — the effect should skip.
          targetType: 'glue_catalog',
          targetConfig: {},
        }}
        onChange={onChange}
      />,
    );
    expect(onChange).not.toHaveBeenCalled();
  });
});
