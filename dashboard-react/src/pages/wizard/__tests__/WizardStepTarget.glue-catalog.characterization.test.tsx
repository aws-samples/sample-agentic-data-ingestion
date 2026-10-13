// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 Task 1.2 — characterization tests for the CREATE WIZARD's `GlueCatalogForm`
 * (`pages/wizard/TargetForms.tsx:200-282`), written BEFORE task 1.5 edits it.
 *
 * Validates: Requirements 2.3, 4.3, 4.4, 5.1, 5.2
 *
 * The sibling file `components/__tests__/TargetConfigForm.glue-catalog.test.tsx`
 * pins the same three behaviours on the edit modal. Both exist because R1.4 and
 * R4.1 require the two implementations to agree about eligibility while R4.4
 * requires them to keep disagreeing about wire keys — a pair of claims that only
 * means something if both sides are pinned. This file is the wizard half.
 *
 * Coverage state this addresses (R5.1). The wizard subform is *rendered* by
 * `WizardStepTarget.test.tsx`, but every case there runs against
 * `useGlueDatabases: () => ({ data: [] })`, so the dropdowns never held an option
 * and none of R2.3 / R4.3 / R4.4 was ever asserted. codegraph reports no covering
 * tests for the `TargetForm` dispatcher (`:306`) either. `chesterton why` on
 * `TargetForms.tsx` returns `LGR-2682c7f-1` and `LGR-2682c7f-2` — both govern the
 * detect-schema pre-flight and the preconditions Alert's wording, **neither
 * governs the two `<Select>`s' disabled state, selected option or wire keys**. So
 * these three behaviours are unconstrained by prior ruling, not permitted by one.
 *
 * Where the expectations come from (R5.2). From the acceptance criteria in
 * `.kiro/specs/p66-glue-catalog-target-selection/requirements.md`, not from the
 * current render — current output contains the defect. As with the modal file,
 * the R4.3 cases supply a stored value that is ABSENT from the options list, so
 * they fail if task 1.5 ever sources `selectedOption` from the filtered
 * `dbOptions` rather than from wizard state.
 *
 * Query data is supplied explicitly per test. `WizardStepTarget.test.tsx` and
 * `WizardStepTarget.dropdown-error.test.tsx` both mock these hooks to `data: []`
 * at module scope, and that empty list is the fixture defect this spec sits
 * downstream of (`design.md` §5.3) — so this file does not reuse it.
 *
 * This file touches no production code.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type SelectWrapper from '@cloudscape-design/components/test-utils/dom/select';
import { TargetForm } from '../TargetForms';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

const useGlueDatabasesMock = vi.fn();
const useTablesInDatabaseMock = vi.fn();

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../../hooks');
  return {
    ...actual,
    useGlueDatabases: () => useGlueDatabasesMock(),
    useTablesInDatabase: (database?: string) => useTablesInDatabaseMock(database),
    // `TargetForms.tsx:9-10` imports these for the S3 Tables subform, which this
    // file never mounts (`targetType` is always `glue_catalog`). Stubbed so no
    // real React Query client is needed.
    useS3TablesBuckets: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
    useTablesInBucket: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
  };
});

/** Accepted by the prefix rule. */
const ELIGIBLE_DB = 'agentic_data_ingestion_analytics';
/** Rejected by the prefix rule — the pre-rename `agentglue_*` shape. */
const INELIGIBLE_DB = 'agentglue_legacy_warehouse';

function renderGlueTarget(opts: {
  targetConfig?: Record<string, string>;
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

  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    targetType: 'glue_catalog',
    targetConfig: opts.targetConfig ?? {},
  };
  const onChange = vi.fn();
  // `TargetForm` is the dispatcher whose `case 'glue_catalog'` (`:309`) is the
  // wizard `GlueCatalogForm`'s only caller, so rendering it exercises the real
  // entry point without mounting the JDBC panels a full `WizardStepTarget`
  // render would pull in.
  const { container } = render(<TargetForm state={state} onChange={onChange} />);
  return { container, state, onChange };
}

function glueSelects(container: HTMLElement): {
  database: SelectWrapper;
  table: SelectWrapper;
} {
  const wrapper = createWrapper(container);
  const database = wrapper.findSelect('[data-testid="glue-db-select"]');
  const table = wrapper.findSelect('[data-testid="glue-table-select"]');
  expect(database, 'expected the glue-db-select <Select>').toBeTruthy();
  expect(table, 'expected the glue-table-select <Select>').toBeTruthy();
  return { database: database!, table: table! };
}

function triggerText(select: SelectWrapper): string {
  return select.findTrigger().getElement().textContent ?? '';
}

describe('wizard GlueCatalogForm (p66 characterization)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    cleanup();
  });

  // =========================================================================
  // R2.3 — WHILE no database is selected, THE Table_Picker SHALL remain
  //        disabled. (`TargetForms.tsx:275`)
  // =========================================================================
  describe('R2.3 — the Table picker is disabled until a database is chosen', () => {
    it('disables the Table select while no database is selected', () => {
      const { container } = renderGlueTarget({
        targetConfig: {},
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }],
      });
      const { database, table } = glueSelects(container);

      expect(table.isDisabled()).toBe(true);
      // Guards against "everything is disabled" passing as "the Table is
      // disabled" — a different bug with the same output.
      expect(database.isDisabled()).toBe(false);
    });

    it('enables the Table select once a database is selected, and offers that database\'s tables', () => {
      const { container } = renderGlueTarget({
        targetConfig: { database_name: ELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }, { table_name: 'customers' }],
      });
      const { table } = glueSelects(container);

      expect(table.isDisabled()).toBe(false);

      // Usable, and offering the selected database's tables — a hardcoded
      // `disabled={false}` passes the line above and fails here.
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
  // R4.3 — a stored database the rule would exclude keeps rendering as the
  //        selected value rather than being silently cleared.
  //        Wizard equivalent of the modal's `:146`/`:161`: `selectedDb` is read
  //        straight off `state.targetConfig.database_name` (`:204`) and fed to
  //        `selectedOption` (`:244`); the Table's `selectedOption` (`:261-265`)
  //        reads `state.targetConfig.table_name`. Neither derives from
  //        `dbOptions` (`:208`) or `tableOptions` (`:209`).
  // =========================================================================
  describe('R4.3 — a stored value renders as the selected option', () => {
    it('renders the stored database_name as the selected option', () => {
      const { container } = renderGlueTarget({
        targetConfig: { database_name: ELIGIBLE_DB },
        databases: [
          { database_name: ELIGIBLE_DB },
          { database_name: 'agentic_data_ingestion_other' },
        ],
      });
      const { database } = glueSelects(container);

      expect(triggerText(database)).toContain(ELIGIBLE_DB);
      expect(database.findPlaceholder()).toBeNull();
    });

    it('renders a stored database the prefix rule EXCLUDES, even though it is absent from the options', () => {
      // The case task 1.5 could break. `agentglue_legacy_warehouse` fails the
      // prefix rule, so after 1.5 filters `dbOptions` it will not be offered —
      // modelled here by not supplying it. It must still render as selected,
      // which holds only while `selectedOption` (`:244`) comes from
      // `state.targetConfig.database_name` (`:204`) rather than from `dbOptions`.
      const { container, onChange } = renderGlueTarget({
        targetConfig: { database_name: INELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
      });
      const { database } = glueSelects(container);

      expect(triggerText(database)).toContain(INELIGIBLE_DB);
      expect(database.findPlaceholder()).toBeNull();
      // Displaying the stored value must not mean writing wizard state back on
      // render — "rather than silently clearing it" cuts both ways.
      expect(onChange).not.toHaveBeenCalled();

      // And keeping the stored value must never be implemented by re-admitting
      // the ineligible name to the offered list (R1.1).
      database.openDropdown();
      const offered = database
        .findDropdown()
        .findOptions()
        .map((o) => o.getElement().textContent?.trim());
      expect(offered).toEqual([ELIGIBLE_DB]);
    });

    it('renders the stored table_name as the selected option, even when absent from the fetched tables', () => {
      const { container, onChange } = renderGlueTarget({
        targetConfig: { database_name: ELIGIBLE_DB, table_name: 'legacy_orders' },
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
  // R4.4 — THE wizard writes `targetConfig.database_name` / `table_name`
  //        (`:245-252`, `:266-272`). p66 does not converge these with the edit
  //        modal's `value.database` / `value.table`.
  // =========================================================================
  describe('R4.4 — the wizard writes `database_name` / `table_name`, never the modal\'s keys', () => {
    it('writes targetConfig.database_name (and no `database`) when a database is picked', () => {
      const { container, onChange } = renderGlueTarget({
        targetConfig: {},
        databases: [{ database_name: ELIGIBLE_DB }],
      });
      const { database } = glueSelects(container);

      database.openDropdown();
      database.selectOptionByValue(ELIGIBLE_DB);

      expect(onChange).toHaveBeenCalledTimes(1);
      const next = onChange.mock.calls[0][0] as WizardState;
      expect(next.targetConfig).toEqual({ database_name: ELIGIBLE_DB });
      // Named explicitly: the divergence from the modal IS the requirement.
      expect(next.targetConfig).not.toHaveProperty('database');
    });

    it('writes targetConfig.table_name (and no `table`) when a table is picked', () => {
      const { container, onChange } = renderGlueTarget({
        targetConfig: { database_name: ELIGIBLE_DB },
        databases: [{ database_name: ELIGIBLE_DB }],
        tables: [{ table_name: 'orders' }],
      });
      const { table } = glueSelects(container);

      table.openDropdown();
      table.selectOptionByValue('orders');

      expect(onChange).toHaveBeenCalledTimes(1);
      const next = onChange.mock.calls[0][0] as WizardState;
      expect(next.targetConfig).toEqual({
        database_name: ELIGIBLE_DB,
        table_name: 'orders',
      });
      expect(next.targetConfig).not.toHaveProperty('table');
    });
  });
});
