// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 Task 1.2 — characterization tests for the EDIT MODAL's `GlueCatalogForm`
 * (`components/TargetConfigForm.tsx:142-186`), written BEFORE task 1.6 edits it.
 *
 * Validates: Requirements 2.3, 4.3, 4.4, 5.1, 5.2
 *
 * Why this file exists at all (R5.1). `TargetConfigForm.tsx` had **no test file**
 * before this one, and codegraph reports no covering tests for either
 * `GlueCatalogForm` (`:142`) or the `TargetConfigForm` default export (`:188`).
 * `chesterton why` on the file returns three commits — `0176ccc686`, `7ad6ca1ad9`,
 * `f06bb5560f` — every one of them with `records: []`, the first two being the
 * documented broad snapshots. So this component is unconstrained by any prior
 * ruling, **not permitted by one**. R5.1 requires a characterization test to land
 * before the modification, and this is it.
 *
 * Where the expectations come from (R5.2). Every assertion below is derived from
 * the acceptance criteria in `.kiro/specs/p66-glue-catalog-target-selection/
 * requirements.md`, NOT from what the component currently renders. Current output
 * contains the defect this spec exists to fix, so reading expectations off it
 * would pin the defect. The house precedent for that discipline is
 * `LGR-28b4d75-5` (template): "a test written from the producer's output cannot
 * detect the producer drifting, because the output is its own oracle."
 *
 * The R4.3 cases are the load-bearing ones. R4.3 is *trivially* true today —
 * nothing filters the option list yet — so a test that merely re-observed today's
 * render would be worthless. These cases are written so that they FAIL if task
 * 1.6 ever wires `selectedOption` from the (soon to be filtered) `dbOptions`
 * instead of from `value.database` / `value.table`: each supplies a stored value
 * that is **absent from the options list entirely**, which is exactly the shape
 * the post-1.6 world produces for a database the prefix rule excludes.
 *
 * Query data is supplied explicitly per test rather than inherited from a
 * module-level `data: []` mock. That empty-list mock is the fixture defect this
 * spec sits downstream of (`design.md` §5.3), and inheriting it here would
 * reintroduce the thing task 1.1 removed.
 *
 * This file touches no production code.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type SelectWrapper from '@cloudscape-design/components/test-utils/dom/select';
import TargetConfigForm from '../TargetConfigForm';
import type { TargetConfig } from '../../types';

// ---------------------------------------------------------------------------
// Reconfigurable query mocks. Each test sets its own data, so nothing is
// inherited (design.md §5.3). Both carry every field their readers touch:
// `isLoading`, `isError` and `refetch` are the whole of `AsyncStatusQuery`
// (`components/common/select-async-status.ts:24-28`) and exactly what the helper
// body reads (`:50`, `:52`, `:65`); `data` is destructured by `GlueCatalogForm`
// at `TargetConfigForm.tsx:145` and `:148`.
// ---------------------------------------------------------------------------
const useGlueDatabasesMock = vi.fn();
const useTablesInDatabaseMock = vi.fn();

vi.mock('../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../hooks');
  return {
    ...actual,
    useGlueDatabases: () => useGlueDatabasesMock(),
    useTablesInDatabase: (database?: string) => useTablesInDatabaseMock(database),
    // The other two subforms are never mounted here (this file always passes
    // `schemaSourceType="glue_data_catalog"`), but `TargetConfigForm.tsx:9-10`
    // imports them, so they are stubbed rather than left to hit real React Query.
    useS3TablesBuckets: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
    useTablesInBucket: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
  };
});

/**
 * A database name the prefix rule ACCEPTS. Spelled out rather than imported from
 * `utils/glue-target-eligibility.ts`, which task 1.3 has not created yet — and
 * deliberately kept as a literal here even after it does, because this file is
 * about the component's behaviour toward a stored value, not about the rule.
 */
const ELIGIBLE_DB = 'agentic_data_ingestion_analytics';

/**
 * A database name the prefix rule REJECTS: it does not start with
 * `agentic_data_ingestion_`. This is the pre-rename `agentglue_*` shape that
 * accounts for 84 of the 93 databases in the measured account, so a pipeline
 * record naming one is the realistic case R4.3 protects.
 */
const INELIGIBLE_DB = 'agentglue_legacy_warehouse';

function renderForm(opts: {
  value?: Partial<TargetConfig>;
  databases?: Array<{ database_name: string }>;
  tables?: Array<{ table_name: string }>;
}) {
  useGlueDatabasesMock.mockReturnValue({
    data: opts.databases ?? [],
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  useTablesInDatabaseMock.mockReturnValue({
    data: opts.tables ?? [],
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });

  const onChange = vi.fn();
  const { container } = render(
    <TargetConfigForm
      schemaSourceType="glue_data_catalog"
      value={opts.value ?? {}}
      onChange={onChange}
    />,
  );
  return { container, onChange };
}

/**
 * Resolve each `<Select>` by its `FormField` label rather than by DOM position.
 * The modal's Selects carry no `data-testid` and this task must not add one to a
 * production file, so the label is the addressable handle — and resolving by
 * label means a future reorder of the two fields cannot silently swap which
 * control an assertion is about.
 */
function glueSelects(container: HTMLElement): {
  database: SelectWrapper;
  table: SelectWrapper;
} {
  const wrapper = createWrapper(container);
  const byLabel = (label: string): SelectWrapper => {
    const field = wrapper
      .findAllFormFields()
      .find((f) => f.findLabel()?.getElement().textContent?.trim() === label);
    expect(field, `expected a FormField labelled "${label}"`).toBeTruthy();
    const select = createWrapper(field!.getElement()).findSelect();
    expect(select, `expected a <Select> inside the "${label}" FormField`).toBeTruthy();
    return select!;
  };
  return { database: byLabel('Glue Database'), table: byLabel('Table') };
}

/** The text the trigger renders — i.e. what the user sees as "selected". */
function triggerText(select: SelectWrapper): string {
  return select.findTrigger().getElement().textContent ?? '';
}

describe('TargetConfigForm — Glue Catalog subform (p66 characterization)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  // =========================================================================
  // R2.3 — WHILE no database is selected, THE Table_Picker SHALL remain
  //        disabled. (`TargetConfigForm.tsx:180`)
  // =========================================================================
  describe('R2.3 — the Table picker is disabled until a database is chosen', () => {
    it('disables the Table select while no database is selected', () => {
      const { container } = renderForm({
        value: {},
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }],
      });
      const { database, table } = glueSelects(container);

      expect(table.isDisabled()).toBe(true);
      // The Database select must NOT be disabled in the same state. Without this
      // the assertion above would also pass in a build where everything is
      // disabled, which is a different bug wearing the same output.
      expect(database.isDisabled()).toBe(false);
    });

    it('enables the Table select once a database is selected, and offers that database\'s tables', () => {
      const { container } = renderForm({
        value: { database: ELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }, { table_name: 'customers' }],
      });
      const { table } = glueSelects(container);

      expect(table.isDisabled()).toBe(false);

      // Enabled is a means, not the end: assert the control is actually usable
      // and that what it offers came from the selected database. A regression
      // that hardcoded `disabled={false}` would pass the line above and fail
      // here.
      table.openDropdown();
      const offered = table
        .findDropdown()
        .findOptions()
        .map((o) => o.getElement().textContent?.trim());
      expect(offered).toEqual(['orders', 'customers']);
      expect(useTablesInDatabaseMock).toHaveBeenCalledWith(ELIGIBLE_DB);
    });
  });

  // =========================================================================
  // R4.3 — WHEN a pipeline record already names a database that the rule would
  //        exclude, THE edit modal SHALL continue to display the stored value
  //        rather than silently clearing it.
  //        (`:146` useState seed, `:150-152` useEffect re-sync, `:161`
  //         Database selectedOption, `:174` Table selectedOption)
  // =========================================================================
  describe('R4.3 — a stored value renders as the selected option', () => {
    it('renders the stored value.database as the selected option', () => {
      const { container } = renderForm({
        value: { database: ELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }, { database_name: 'agentic_data_ingestion_other' }],
      });
      const { database } = glueSelects(container);

      expect(triggerText(database)).toContain(ELIGIBLE_DB);
      expect(database.findPlaceholder()).toBeNull();
    });

    it('renders a stored database the prefix rule EXCLUDES, even though it is absent from the options', () => {
      // This is the case task 1.6 could break, and the reason this file is
      // ordered before it. `agentglue_legacy_warehouse` fails the prefix rule,
      // so after 1.6 filters `dbOptions` it will not be among the options —
      // modelled here by simply not supplying it. R4.3 requires it to render as
      // selected regardless, which holds only because `selectedOption` (`:161`)
      // is built from `value.database` via `selectedDb` (`:146`, `:150-152`) and
      // NOT from `dbOptions` (`:154`). If 1.6 ever sources `selectedOption` from
      // the filtered option list, this test fails — which is its entire purpose.
      const { container, onChange } = renderForm({
        value: { database: INELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
      });
      const { database } = glueSelects(container);

      expect(triggerText(database)).toContain(INELIGIBLE_DB);
      expect(database.findPlaceholder()).toBeNull();

      // "rather than silently clearing it" — the component must not write a
      // cleared config back to its parent just because the stored value is
      // ineligible.
      expect(onChange).not.toHaveBeenCalled();

      // And the ineligible name must not have been quietly added to the offered
      // list either: R1.1 excludes it from what a user may newly pick. Pinned
      // here so that "keep the stored value" is never implemented by widening
      // the options.
      database.openDropdown();
      const offered = database
        .findDropdown()
        .findOptions()
        .map((o) => o.getElement().textContent?.trim());
      expect(offered).toEqual([ELIGIBLE_DB]);
    });

    it('renders the stored value.table as the selected option, even when absent from the fetched tables', () => {
      // Same argument for the Table `<Select>`, whose `selectedOption` at `:174`
      // reads `value.table` directly.
      const { container, onChange } = renderForm({
        value: { database: ELIGIBLE_DB, table: 'legacy_orders' },
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }],
      });
      const { table } = glueSelects(container);

      expect(triggerText(table)).toContain('legacy_orders');
      expect(table.findPlaceholder()).toBeNull();
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  // =========================================================================
  // R4.4 — THE two subforms' differing wire keys SHALL be left as they are:
  //        the edit modal writes `value.database` / `value.table`
  //        (`:165`, `:176`). p66 does not converge them with the wizard's
  //        `database_name` / `table_name`.
  // =========================================================================
  describe('R4.4 — the modal writes `database` / `table`, never the wizard\'s keys', () => {
    it('writes value.database (and no database_name) when a database is picked', () => {
      const { container, onChange } = renderForm({
        value: {},
        databases: [{ database_name: ELIGIBLE_DB }],
      });
      const { database } = glueSelects(container);

      database.openDropdown();
      database.selectOptionByValue(ELIGIBLE_DB);

      expect(onChange).toHaveBeenCalledTimes(1);
      const payload = onChange.mock.calls[0][0];
      expect(payload).toEqual({ type: 'GLUE_CATALOG', database: ELIGIBLE_DB });
      // Named explicitly, because the divergence is the requirement: converging
      // on the wizard's key would satisfy `toEqual` above only by breaking it,
      // but a reader needs to see WHICH key is forbidden here.
      expect(payload).not.toHaveProperty('database_name');
    });

    it('writes value.table (and no table_name) when a table is picked', () => {
      const { container, onChange } = renderForm({
        value: { database: ELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }],
      });
      const { table } = glueSelects(container);

      table.openDropdown();
      table.selectOptionByValue('orders');

      expect(onChange).toHaveBeenCalledTimes(1);
      const payload = onChange.mock.calls[0][0];
      expect(payload).toEqual({
        type: 'GLUE_CATALOG',
        database: ELIGIBLE_DB,
        table: 'orders',
      });
      expect(payload).not.toHaveProperty('table_name');
    });
  });
});
