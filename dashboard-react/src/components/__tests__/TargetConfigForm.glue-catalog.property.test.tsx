// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 task 1.7 — Properties 1, 2, 3, 4 and 6 against the **EDIT MODAL's**
 * `GlueCatalogForm` (`components/TargetConfigForm.tsx`), plus Property 1's
 * cross-component agreement half, which needs both pickers and therefore lives
 * in exactly one file.
 *
 * Validates: Requirements 1.1, 1.3, 1.4, 1.5, 2.1, 2.2, 2.4, 4.1, 4.3, 5.3
 *
 * The sibling file
 * `pages/wizard/__tests__/WizardStepTarget.glue-catalog.property.test.tsx`
 * carries the same properties against the wizard picker, plus the two example
 * tests. Both exist because R1.4 and R4.1 make "the two pickers cannot disagree
 * about which databases are offerable" the criterion, and a claim about two
 * implementations means nothing asserted on one.
 *
 * **What this file adds that task 1.3 does not already own.**
 * `utils/__tests__/glue-target-eligibility.property.test.ts` owns Property 1's
 * **predicate** half — what `partitionGlueTargetDatabases` selects, over
 * generated lists, with the generator as oracle. Nothing there renders. This
 * file owns the **rendered** half: that each picker's option list *is* that
 * sublist, and that the two agree. The predicate tests are not duplicated here,
 * and no assertion below re-tests `startsWith`.
 *
 * **Property 4 asserts against RENDERED OUTPUT, not against the prop.** Design
 * §2.1 and §5.2 establish Cloudscape's mutual exclusion by reading the
 * installed package's source — `internal/components/dropdown-status/index.js`'s
 * `useDropdownStatus` is an if/else-if chain (`loading`, else `error`, else
 * `isEmpty && empty`) — and §5.2 records that as "stronger than an assumption
 * and weaker than an observation", specifying a `FormField`-description fallback
 * if it does not behave as read. Verified at
 * `@cloudscape-design/components@3.0.1204` by rendering: the source read holds,
 * so **the fallback is not applied**. Property 4 is written against the DOM
 * rather than against the prop precisely so that this stays checked — design
 * §5.8 names "Cloudscape changes `useDropdownStatus`' branch order" as a thing
 * that would invalidate the design, and this is what would catch it.
 *
 * A second, independent mechanism enforces the same exclusion and is worth
 * knowing about before reading Property 4: `selectAsyncStatus`
 * (`components/common/select-async-status.ts:50-54`) collapses the query to a
 * **single** `statusType` value, resolving `isError` before `isLoading`. So a
 * simultaneously-loading-and-erroring query cannot ask Cloudscape for two states
 * in the first place. Property 4 draws that cell deliberately.
 *
 * **Query data is supplied explicitly per case, never inherited from a
 * module-level mock.** The mock factory below returns whatever the current test
 * set; there is no module-scope `data: []` default to fall through to. Design
 * §5.3: a state read off a mock that omits it reads `undefined` and is silently
 * falsy, which is the fixture defect this whole spec sits downstream of. Both
 * query mocks carry every field their readers touch — `isLoading`, `isError` and
 * `refetch` are the whole of `AsyncStatusQuery`
 * (`select-async-status.ts:24-28`) and exactly what the helper body reads
 * (`:50`, `:52`, `:65`); `data` is what `GlueCatalogForm` destructures.
 *
 * `fast-check ^3.15.0`, an existing dependency. `numRuns` is 100 for the cheap
 * properties, matching this repo's convention
 * (`utils/__tests__/unsourced-target-fields.property.test.ts:388`,
 * `components/__tests__/MappingViewer.property.test.tsx:118`), and is lowered
 * with the arithmetic stated wherever one run mounts several React trees —
 * Property 4 renders 8 of them per run.
 *
 * Coverage note: `.kiro/steering/test-mapping.md`'s `fileMatchPattern` is
 * scoped to Lambda Python, AgentCore Python and shell files, and the table
 * carries no frontend rows at all, so it names no gate here. The gate is
 * `make test-frontend` — **both** legs, vitest and `tsc --noEmit`.
 *
 * This file touches no production code.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import * as fc from 'fast-check';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type SelectWrapper from '@cloudscape-design/components/test-utils/dom/select';
import TargetConfigForm from '../TargetConfigForm';
import { TargetForm } from '../../pages/wizard/TargetForms';
import { INITIAL_WIZARD_STATE } from '../../pages/wizard/types';
import type { WizardState } from '../../pages/wizard/types';
import type { TargetConfig } from '../../types';
import {
  GLUE_TARGET_DATABASE_PREFIX,
  GLUE_TARGET_EXCLUSION_NOTE,
  GLUE_TARGET_NONE_ELIGIBLE,
  GLUE_TARGET_NO_DATABASES,
  GLUE_TARGET_NO_TABLES_NEXT_STEP,
  glueTargetNoTablesEmpty,
} from '../../utils/glue-target-eligibility';

// ---------------------------------------------------------------------------
// Query mocks. `vi.mock`'s path is resolved relative to THIS file, but the
// module registry keys on the resolved id — `../../hooks` here is `src/hooks`,
// the same module `pages/wizard/TargetForms.tsx` imports as `../../hooks`. So
// one factory serves both components, which is what lets Property 1's
// agreement half feed identical data to each.
// ---------------------------------------------------------------------------
const useGlueDatabasesMock = vi.fn();
const useTablesInDatabaseMock = vi.fn();

vi.mock('../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../hooks');
  return {
    ...actual,
    useGlueDatabases: () => useGlueDatabasesMock(),
    useTablesInDatabase: (database?: string) => useTablesInDatabaseMock(database),
    // Neither file mounts the S3 Tables subforms (the modal always gets
    // `schemaSourceType="glue_data_catalog"`, the wizard always
    // `targetType: 'glue_catalog'`), but both modules import these at top
    // level, so they are stubbed rather than left to reach real React Query.
    useS3TablesBuckets: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
    useTablesInBucket: () => ({ data: [], isLoading: false, isError: false, refetch: vi.fn() }),
  };
});

/** The whole of what a `<Select>`'s readers take off a React Query result. */
interface QueryStub {
  data: unknown;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
}

function queryStub(over: Partial<QueryStub> = {}): QueryStub {
  return { data: [], isLoading: false, isError: false, refetch: vi.fn(), ...over };
}

interface DbOption {
  database_name: string;
}

// ---------------------------------------------------------------------------
// Name generators. Deliberately NOT reusing task 1.3's arbitraries: those are
// tuned to stress the predicate (leading spaces, upper case, empty names), and
// this file's subject is rendering. Names here are drawn from the character set
// a real Glue database name uses, so an assertion that fails is failing about
// the filter rather than about how Cloudscape renders an exotic label.
// ---------------------------------------------------------------------------
const suffixArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'.split('')),
  { minLength: 1, maxLength: 12 },
);

/** A name the prefix rule accepts. */
const eligibleNameArb = suffixArb.map((s) => `${GLUE_TARGET_DATABASE_PREFIX}${s}`);

/**
 * A name the prefix rule refuses. The pre-rename `agentglue_` shape is drawn
 * most often because it is 84 of the 93 databases in the measured account; the
 * near misses are drawn too, and the `.filter` is a guard rather than the rule —
 * it cannot mislabel a draw.
 */
const ineligibleNameArb = fc
  .oneof(
    suffixArb.map((s) => `agentglue_${s}`),
    suffixArb.map((s) => `legacy_${GLUE_TARGET_DATABASE_PREFIX}${s}`),
    fc.constant('agentic_data_ingestion'),
    suffixArb.map((s) => `${GLUE_TARGET_DATABASE_PREFIX.toUpperCase()}${s}`),
  )
  .filter((n) => !n.startsWith(GLUE_TARGET_DATABASE_PREFIX));

/**
 * A drawn database list plus the verdict the **generator** assigned, in source
 * order. `eligibleNames` is accumulated while walking the drawn specs in order;
 * it is never recovered by re-filtering the input, which would make the
 * production rule its own oracle (`LGR-28b4d75-5`).
 *
 * Names are drawn unique so that an option-list equality is an assertion about
 * the filter and not about how Cloudscape treats two options sharing a value.
 */
interface Draw {
  input: DbOption[];
  eligibleNames: string[];
}

const drawArb: fc.Arbitrary<Draw> = fc
  .uniqueArray(
    fc.record({ isEligible: fc.boolean(), eligible: eligibleNameArb, ineligible: ineligibleNameArb }),
    {
      minLength: 0,
      maxLength: 8,
      selector: (spec) => (spec.isEligible ? spec.eligible : spec.ineligible),
    },
  )
  .map((specs) => {
    const input: DbOption[] = [];
    const eligibleNames: string[] = [];
    for (const spec of specs) {
      const database_name = spec.isEligible ? spec.eligible : spec.ineligible;
      input.push({ database_name });
      if (spec.isEligible) eligibleNames.push(database_name);
    }
    return { input, eligibleNames };
  });

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

interface Rendered {
  container: HTMLElement;
  onChange: ReturnType<typeof vi.fn>;
  unmount: () => void;
}

/** Mount the edit modal's Glue Catalog subform. */
function renderModal(opts: {
  value?: Partial<TargetConfig>;
  databases?: QueryStub;
  tables?: QueryStub;
}): Rendered {
  useGlueDatabasesMock.mockReturnValue(opts.databases ?? queryStub());
  useTablesInDatabaseMock.mockReturnValue(opts.tables ?? queryStub());
  const onChange = vi.fn();
  const { container, unmount } = render(
    <TargetConfigForm
      schemaSourceType="glue_data_catalog"
      value={opts.value ?? {}}
      onChange={onChange}
    />,
  );
  return { container, onChange, unmount };
}

/**
 * Mount the wizard's Glue Catalog subform through its real entry point, the
 * `TargetForm` dispatcher — used here only by Property 1's agreement half.
 */
function renderWizard(opts: {
  targetConfig?: Record<string, string>;
  databases?: QueryStub;
  tables?: QueryStub;
}): Rendered {
  useGlueDatabasesMock.mockReturnValue(opts.databases ?? queryStub());
  useTablesInDatabaseMock.mockReturnValue(opts.tables ?? queryStub());
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    targetType: 'glue_catalog',
    targetConfig: opts.targetConfig ?? {},
  };
  const onChange = vi.fn();
  const { container, unmount } = render(<TargetForm state={state} onChange={onChange} />);
  return { container, onChange, unmount };
}

/**
 * The modal's `<Select>`s carry no `data-testid`, and task 1.7 must not add one
 * to a production file, so they are resolved by their `FormField` label. That
 * also means a future reorder of the two fields cannot silently swap which
 * control an assertion is about.
 */
function modalSelects(container: HTMLElement): { database: SelectWrapper; table: SelectWrapper } {
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

function wizardSelects(container: HTMLElement): { database: SelectWrapper; table: SelectWrapper } {
  const wrapper = createWrapper(container);
  const database = wrapper.findSelect('[data-testid="glue-db-select"]');
  const table = wrapper.findSelect('[data-testid="glue-table-select"]');
  expect(database, 'expected the glue-db-select <Select>').toBeTruthy();
  expect(table, 'expected the glue-table-select <Select>').toBeTruthy();
  return { database: database!, table: table! };
}

/** Option labels the dropdown offers, in rendered order. */
function offeredOptions(select: SelectWrapper): string[] {
  select.openDropdown();
  return select
    .findDropdown()
    .findOptions()
    .map((o) => o.getElement().textContent?.trim() ?? '');
}

/**
 * Everything the OPEN dropdown renders, as text. This is Property 4's subject:
 * whatever Cloudscape actually put on screen, not the props it was handed.
 * Returns `null` when the dropdown did not open — a disabled `<Select>` has no
 * dropdown content at all, which is a distinct state from an empty one.
 */
function openDropdownText(select: SelectWrapper): string | null {
  select.openDropdown();
  const open = select.findDropdown().findOpenDropdown();
  return open ? (open.getElement().textContent ?? '') : null;
}

/** The text the trigger renders — what the user sees as "selected". */
function triggerText(select: SelectWrapper): string {
  return select.findTrigger().getElement().textContent ?? '';
}

/** `selectAsyncStatus`' copy for a given noun (`select-async-status.ts:58-59`). */
const loadingTextFor = (noun: string) => `Loading ${noun}\u2026`;
const errorTextFor = (noun: string) => `Couldn't load ${noun}`;

beforeEach(() => {
  vi.clearAllMocks();
  cleanup();
});

// ===========================================================================
// Property 1 — rendered half
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 1: both pickers offer exactly the eligible sublist, in source order, and offer the same set', () => {
  /**
   * **Validates: Requirements 1.1, 1.4, 4.1**
   *
   * The edit modal's Database picker offers exactly the eligible sublist of
   * whatever the endpoint returned, in the endpoint's own order.
   *
   * A list equality, not a membership check: a `.sort()` anywhere on the path
   * from query data to `options` fails here rather than passing something
   * weaker. Source order matters because R1.1 offers "the sublist" — a picker
   * that reordered would be offering a different list from the one the endpoint
   * returned.
   */
  it('the modal offers exactly the eligible databases, in source order', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames }) => {
        const { container, unmount } = renderModal({
          databases: queryStub({ data: input }),
        });
        try {
          const { database } = modalSelects(container);
          expect(offeredOptions(database)).toEqual(eligibleNames);
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });

  /**
   * **Validates: Requirements 1.1, 1.4, 4.1**
   *
   * The agreement half, and the reason this test exists in only one file: it is
   * a claim about **two** components, so it must feed both the same data in the
   * same run. R1.4's acceptance criterion is that the two Database pickers
   * cannot disagree about which databases are offerable; task 1.3 made that
   * *structurally* likely by putting the rule in one module, and this is what
   * makes it **tested**. A second `startsWith` spelling, or a stray `.filter`,
   * appearing in either component fails here.
   *
   * Asserted as an ordered list rather than as a set, which is strictly
   * stronger: it catches two pickers that offer the same names in different
   * orders, and design §6's statement wants both the order and the agreement.
   */
  it('the wizard and the modal offer the identical list for the identical data', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames }) => {
        const modal = renderModal({ databases: queryStub({ data: input }) });
        let modalOffered: string[];
        try {
          modalOffered = offeredOptions(modalSelects(modal.container).database);
        } finally {
          modal.unmount();
        }

        const wizard = renderWizard({ databases: queryStub({ data: input }) });
        let wizardOffered: string[];
        try {
          wizardOffered = offeredOptions(wizardSelects(wizard.container).database);
        } finally {
          wizard.unmount();
        }

        expect(wizardOffered).toEqual(modalOffered);
        // Both against the generator's verdict too, so a run in which the two
        // components agree on the WRONG list still fails.
        expect(modalOffered).toEqual(eligibleNames);
        expect(wizardOffered).toEqual(eligibleNames);
      }),
      { numRuns: 60 },
    );
  });
});

// ===========================================================================
// Property 2
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 2: the Database picker’s rendered state is a total function of (totalCount, eligible.length)', () => {
  /**
   * **Validates: Requirements 1.3, 1.5**
   *
   * Exactly one of three states renders, and which one is determined by the two
   * counts: options when `eligible.length > 0`; `GLUE_TARGET_NONE_ELIGIBLE`
   * when `totalCount > 0 && eligible.length === 0`; `GLUE_TARGET_NO_DATABASES`
   * when `totalCount === 0`.
   *
   * **Total** is the load-bearing word, so all three arms are asserted
   * positively *and* the other two are asserted absent in each. An
   * implementation that showed the right sentence plus a stale one would pass a
   * one-sided check and fails here. The `totalCount > 0 && eligible.length === 0`
   * cell is the arm R1.5 exists for and the one a plain `.filter()` destroys.
   */
  it('renders exactly the one statement the two counts select', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames }) => {
        const { container, unmount } = renderModal({
          databases: queryStub({ data: input }),
        });
        try {
          const { database } = modalSelects(container);
          const text = openDropdownText(database) ?? '';
          const offered = database
            .findDropdown()
            .findOptions()
            .map((o) => o.getElement().textContent?.trim() ?? '');

          const hasNoneEligible = text.includes(GLUE_TARGET_NONE_ELIGIBLE);
          const hasNoDatabases = text.includes(GLUE_TARGET_NO_DATABASES);

          if (eligibleNames.length > 0) {
            expect(offered).toEqual(eligibleNames);
            expect(hasNoneEligible).toBe(false);
            expect(hasNoDatabases).toBe(false);
          } else if (input.length > 0) {
            expect(offered).toEqual([]);
            expect(hasNoneEligible).toBe(true);
            expect(hasNoDatabases).toBe(false);
          } else {
            expect(offered).toEqual([]);
            expect(hasNoDatabases).toBe(true);
            expect(hasNoneEligible).toBe(false);
          }

          // The two sentences are never both on screen, whichever arm ran.
          // Stated separately because it is the "exactly one" half of the
          // property and does not follow from the per-arm checks above alone.
          expect([hasNoneEligible, hasNoDatabases].filter(Boolean).length).toBeLessThanOrEqual(1);
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ===========================================================================
// Property 3
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 3: the Table picker’s empty state names the selected database', () => {
  /**
   * **Validates: Requirements 2.1, 2.4**
   *
   * For any valid Glue database name, when that database is selected and the
   * tables endpoint resolves to zero tables, the modal's Table picker renders
   * an empty state containing **that exact name** together with
   * `GLUE_TARGET_NO_TABLES_NEXT_STEP`.
   *
   * The name is asserted by containment of the drawn string rather than by a
   * regex, so a generated name carrying regex metacharacters cannot make the
   * assertion pass or fail for the wrong reason. A generic "No tables found" —
   * one sentence standing in for several distinct meanings, which
   * `LGR-2bcdd0d-3` rejected — fails on the name check.
   *
   * The next-step sentence is asserted in the same breath because R2.4 makes it
   * part of the same state: stating the fact without the remedy leaves the
   * author with nothing to do.
   */
  it('names the selected database and states the next step', () => {
    fc.assert(
      fc.property(eligibleNameArb, (database) => {
        const { container, unmount } = renderModal({
          value: { database },
          databases: queryStub({ data: [{ database_name: database }] }),
          tables: queryStub({ data: [] }),
        });
        try {
          const { table } = modalSelects(container);
          // R2.3 keeps the picker disabled with no database; with one selected
          // it must be reachable, or the empty state below is unobservable and
          // the assertion would be vacuous.
          expect(table.isDisabled()).toBe(false);

          const text = openDropdownText(table);
          expect(text, 'the Table dropdown should open with a database selected').not.toBeNull();
          expect(text!).toContain(glueTargetNoTablesEmpty(database));
          expect(text!).toContain(database);
          expect(text!).toContain(GLUE_TARGET_NO_TABLES_NEXT_STEP);
        } finally {
          unmount();
        }
      }),
      { numRuns: 60 },
    );
  });

  /**
   * **Validates: Requirements 2.1**
   *
   * The name is the *selected* database's, not just any name in scope. Drawn as
   * two distinct databases where the one selected is the second, so an
   * implementation that named the first offered database — or the first in the
   * query data — fails. Without this, "contains the name" could be satisfied by
   * a sentence naming the wrong database.
   *
   * **The substring precondition is load-bearing, and it was found by a mutation
   * run rather than reasoned about up front.** `uniqueArray` only guarantees the
   * two names differ, and two *different* names can still nest — `…_a` inside
   * `…_ab` — in which case `not.toContain(other)` fails on a correct component,
   * because the sentence naming `selected` contains `other` as a substring. That
   * is a defect in the assertion, not in the picker, and it made this test
   * seed-dependently red. Rejecting nested pairs keeps the assertion about which
   * database was named.
   */
  it('names the database that is selected, not another one in the list', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(eligibleNameArb, { minLength: 2, maxLength: 2 }),
        ([other, selected]) => {
          fc.pre(!selected.includes(other) && !other.includes(selected));
          const { container, unmount } = renderModal({
            value: { database: selected },
            databases: queryStub({
              data: [{ database_name: other }, { database_name: selected }],
            }),
            tables: queryStub({ data: [] }),
          });
          try {
            const { table } = modalSelects(container);
            const text = openDropdownText(table) ?? '';
            expect(text).toContain(glueTargetNoTablesEmpty(selected));
            expect(text).not.toContain(other);
          } finally {
            unmount();
          }
        },
      ),
      { numRuns: 60 },
    );
  });
});

// ===========================================================================
// Property 4
// ===========================================================================

/**
 * The whole `(isLoading, isError, isEmpty)` triple, enumerated rather than
 * sampled. Design §6's statement is "for any combination", and eight cells is
 * small enough to exhaust — a drawn boolean triple would *probably* cover all
 * eight over 100 runs, and probably is a weaker claim than the property makes.
 * The database names are still generated, so the property is genuinely
 * quantified; only the triple is exhaustive.
 */
const ASYNC_TRIPLE: ReadonlyArray<{ isLoading: boolean; isError: boolean; isEmpty: boolean }> = [
  { isLoading: false, isError: false, isEmpty: false },
  { isLoading: false, isError: false, isEmpty: true },
  { isLoading: false, isError: true, isEmpty: false },
  { isLoading: false, isError: true, isEmpty: true },
  { isLoading: true, isError: false, isEmpty: false },
  { isLoading: true, isError: false, isEmpty: true },
  { isLoading: true, isError: true, isEmpty: false },
  { isLoading: true, isError: true, isEmpty: true },
];

/**
 * Which single statement the two mechanisms between them should put on screen.
 * Derived from the requirement (R2.2 — exactly one) and from the two reads
 * design §2.1 and §5.2 record, NOT from observed output: `selectAsyncStatus`
 * resolves `isError` before `isLoading` into one `statusType`
 * (`select-async-status.ts:50-54`), and Cloudscape's `useDropdownStatus` is
 * `loading`, else `error`, else `isEmpty && empty`.
 */
function expectedStatement(t: { isLoading: boolean; isError: boolean; isEmpty: boolean }) {
  if (t.isError) return 'error' as const;
  if (t.isLoading) return 'loading' as const;
  if (t.isEmpty) return 'empty' as const;
  return 'options' as const;
}

describe('Feature: p66-glue-catalog-target-selection, Property 4: a dropdown never renders two async states at once', () => {
  /**
   * **Validates: Requirements 2.2**
   *
   * For every `(isLoading, isError, isEmpty)` combination, the modal's Database
   * `<Select>` renders exactly one of its loading, error and empty statements —
   * never the empty statement together with `Couldn't load databases`, and
   * never a recovery link in the empty state.
   *
   * **Asserted against the rendered DOM.** Design §5.2 reached Cloudscape's
   * mutual exclusion by reading `useDropdownStatus`, and specified a
   * `FormField`-description fallback if it did not behave as read. It does
   * behave as read at `3.0.1204`, so no fallback is in the tree — and this test
   * is what would notice if that changed (§5.8's fourth invalidation).
   *
   * The recovery link is asserted in both directions. C8's "no false
   * affordance" — `LGR-2bcdd0d-3` rejected a Retry button for a condition
   * re-requesting cannot fix — means Retry must be **absent** from the empty
   * state; but asserting only its absence would also pass in a build where the
   * error state lost its Retry too, which is a different regression wearing the
   * same output.
   */
  it('renders exactly one async statement in the Database picker, over the whole triple', () => {
    fc.assert(
      fc.property(eligibleNameArb, (name) => {
        for (const t of ASYNC_TRIPLE) {
          // `isEmpty` is realised through the DATA, not by poking a prop: an
          // empty option list is what Cloudscape's third branch keys on.
          const data = t.isEmpty ? [] : [{ database_name: name }];
          const { container, unmount } = renderModal({
            databases: queryStub({ data, isLoading: t.isLoading, isError: t.isError }),
          });
          try {
            const { database } = modalSelects(container);
            const text = openDropdownText(database) ?? '';

            const hasLoading = text.includes(loadingTextFor('databases'));
            const hasError = text.includes(errorTextFor('databases'));
            const hasEmpty = text.includes(GLUE_TARGET_NO_DATABASES);
            const present = [hasLoading, hasError, hasEmpty].filter(Boolean).length;
            const label = `triple ${JSON.stringify(t)} rendered ${JSON.stringify(text)}`;

            switch (expectedStatement(t)) {
              case 'error':
                expect(hasError, label).toBe(true);
                expect(present, label).toBe(1);
                expect(database.findErrorRecoveryButton(), label).not.toBeNull();
                break;
              case 'loading':
                expect(hasLoading, label).toBe(true);
                expect(present, label).toBe(1);
                expect(database.findErrorRecoveryButton(), label).toBeNull();
                break;
              case 'empty':
                expect(hasEmpty, label).toBe(true);
                expect(present, label).toBe(1);
                // The false affordance C8 forbids: no Retry on a condition
                // re-requesting cannot fix.
                expect(database.findErrorRecoveryButton(), label).toBeNull();
                break;
              case 'options':
                // A healthy load renders no status statement at all — the
                // options are the content.
                expect(present, label).toBe(0);
                expect(database.findErrorRecoveryButton(), label).toBeNull();
                expect(
                  database
                    .findDropdown()
                    .findOptions()
                    .map((o) => o.getElement().textContent?.trim()),
                  label,
                ).toEqual([name]);
                break;
            }
          } finally {
            unmount();
          }
        }
      }),
      // 8 React trees per run, so 12 runs is ~96 mounts. The triple is
      // exhausted every run; only the name varies, and the name is not what
      // this property is about.
      { numRuns: 12 },
    );
  });

  /**
   * **Validates: Requirements 2.2**
   *
   * The same claim for the Table picker, whose empty statement is the one the
   * reported symptom is about. Its noun is `tables`, so its loading and error
   * copy differ, and its `empty` node is built per-database — three reasons the
   * Database picker's result does not carry over.
   *
   * A database is always selected here: R2.3 keeps the picker disabled without
   * one, and a disabled `<Select>` renders no dropdown content at all, so every
   * assertion would be vacuously true.
   */
  it('renders exactly one async statement in the Table picker, over the whole triple', () => {
    fc.assert(
      fc.property(eligibleNameArb, (database) => {
        const emptyStatement = `${glueTargetNoTablesEmpty(database)} ${GLUE_TARGET_NO_TABLES_NEXT_STEP}`;
        for (const t of ASYNC_TRIPLE) {
          const { container, unmount } = renderModal({
            value: { database },
            databases: queryStub({ data: [{ database_name: database }] }),
            tables: queryStub({
              data: t.isEmpty ? [] : [{ table_name: 'orders' }],
              isLoading: t.isLoading,
              isError: t.isError,
            }),
          });
          try {
            const { table } = modalSelects(container);
            const text = openDropdownText(table);
            const label = `triple ${JSON.stringify(t)} rendered ${JSON.stringify(text)}`;
            expect(text, label).not.toBeNull();

            const hasLoading = text!.includes(loadingTextFor('tables'));
            const hasError = text!.includes(errorTextFor('tables'));
            const hasEmpty = text!.includes(emptyStatement);
            const present = [hasLoading, hasError, hasEmpty].filter(Boolean).length;

            switch (expectedStatement(t)) {
              case 'error':
                expect(hasError, label).toBe(true);
                expect(present, label).toBe(1);
                expect(table.findErrorRecoveryButton(), label).not.toBeNull();
                break;
              case 'loading':
                expect(hasLoading, label).toBe(true);
                expect(present, label).toBe(1);
                expect(table.findErrorRecoveryButton(), label).toBeNull();
                break;
              case 'empty':
                expect(hasEmpty, label).toBe(true);
                expect(present, label).toBe(1);
                expect(table.findErrorRecoveryButton(), label).toBeNull();
                break;
              case 'options':
                expect(present, label).toBe(0);
                expect(table.findErrorRecoveryButton(), label).toBeNull();
                break;
            }
          } finally {
            unmount();
          }
        }
      }),
      { numRuns: 12 },
    );
  });
});

// ===========================================================================
// Property 6
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 6: a stored database survives the filter', () => {
  /**
   * **Validates: Requirements 4.3**
   *
   * For any stored `value.database` — eligible or not — the edit modal renders
   * it as the Database `<Select>`'s selected option and does not clear it.
   *
   * The ineligible arm is the load-bearing one, and it is drawn rather than
   * exampled because "ineligible" has several shapes. It holds only because
   * `selectedOption` is built from `value.database` through `selectedDb`, and
   * **not** from the filtered `dbOptions`; if that is ever rewired, this fails.
   *
   * "does not clear it" is asserted from both sides: the stored value is on
   * screen, *and* the component wrote nothing back to its parent. A component
   * that cleared its own state on mount could still leave the old label
   * rendered for a frame, and `onChange` is what distinguishes the two.
   *
   * Task 1.2's characterization covers two fixed cases of this; the property
   * covers the space, and both are kept because 1.2's file is the R5.1 artifact
   * that had to exist before 1.6 and must keep passing unchanged.
   */
  it('renders any stored database as the selected option and writes nothing back', () => {
    fc.assert(
      fc.property(
        fc.oneof(eligibleNameArb, ineligibleNameArb),
        eligibleNameArb,
        (stored, offered) => {
          // Keeps the comment below true on every draw: the two arbitraries can
          // both produce the same eligible name, and a stored value that IS in
          // the option list exercises nothing this property is about.
          fc.pre(stored !== offered);
          const { container, onChange, unmount } = renderModal({
            value: { database: stored },
            // The stored name is deliberately absent from the query data, which
            // is exactly the shape the post-1.6 world produces for a database
            // the prefix rule excludes.
            databases: queryStub({ data: [{ database_name: offered }] }),
          });
          try {
            const { database } = modalSelects(container);
            expect(triggerText(database)).toContain(stored);
            expect(database.findPlaceholder()).toBeNull();
            expect(onChange).not.toHaveBeenCalled();

            // Keeping the stored value must never be implemented by widening
            // the offered list — R1.1 still excludes it from what a user may
            // newly pick.
            expect(offeredOptions(database)).toEqual([offered]);
          } finally {
            unmount();
          }
        },
      ),
      { numRuns: 60 },
    );
  });

  /**
   * **Validates: Requirements 4.3**
   *
   * The Table half of the same property: a stored `value.table` absent from the
   * fetched tables still renders as selected. Its `selectedOption` reads
   * `value.table` directly, so the same rewiring hazard applies.
   */
  it('renders any stored table as the selected option and writes nothing back', () => {
    fc.assert(
      fc.property(eligibleNameArb, suffixArb, suffixArb, (database, storedTable, fetchedTable) => {
        fc.pre(storedTable !== fetchedTable);
        const { container, onChange, unmount } = renderModal({
          value: { database, table: storedTable },
          databases: queryStub({ data: [{ database_name: database }] }),
          tables: queryStub({ data: [{ table_name: fetchedTable }] }),
        });
        try {
          const { table } = modalSelects(container);
          expect(triggerText(table)).toContain(storedTable);
          expect(table.findPlaceholder()).toBeNull();
          expect(onChange).not.toHaveBeenCalled();
        } finally {
          unmount();
        }
      }),
      { numRuns: 60 },
    );
  });
});
