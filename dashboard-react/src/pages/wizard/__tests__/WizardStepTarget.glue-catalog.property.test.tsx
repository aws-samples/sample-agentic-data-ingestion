// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p66 task 1.7 — Properties 1, 2, 3, 4 and 6 against the **CREATE WIZARD's**
 * `GlueCatalogForm` (`pages/wizard/TargetForms.tsx`), plus the task's two
 * example tests.
 *
 * Validates: Requirements 1.1, 1.3, 1.4, 1.5, 2.1, 2.2, 2.4, 4.1, 4.3, 5.3
 *
 * The sibling file
 * `components/__tests__/TargetConfigForm.glue-catalog.property.test.tsx`
 * carries the same properties against the edit modal, and owns Property 1's
 * cross-component **agreement** half — that claim is about two components, so
 * it is asserted in exactly one place rather than half-asserted in each. What
 * lives here is the wizard's own rendered behaviour, plus the two example tests,
 * both of which belong on this side:
 *
 * - **C10** — the exclusion note must not become a fourth statement of the three
 *   preconditions. Only the wizard has the preconditions Alert to be a fourth
 *   statement *of*; the modal renders two `FormField`s and nothing else, an
 *   asymmetry design §4.3 records as pre-existing and deliberately unfixed
 *   (`LGR-8a4fefa-2`'s shape). So the duplication this asserts against is
 *   structurally impossible on the modal and live here.
 * - **C1** — the empty state must offer no create-table control. Asserted on
 *   **both** pickers in the one test, because the copy comes from one constant
 *   and a control could be added to either surface.
 *
 * **Property 4 asserts against rendered output, not against the prop.** Design
 * §2.1 and §5.2 establish Cloudscape's mutual exclusion by reading the installed
 * package's `internal/components/dropdown-status/index.js` — `useDropdownStatus`
 * is an if/else-if chain (`loading`, else `error`, else `isEmpty && empty`) —
 * and §5.2 specifies a `FormField`-description fallback if it does not behave as
 * read. Verified at `@cloudscape-design/components@3.0.1204` by rendering: it
 * behaves as read, so **the fallback is not applied**. The assertions stay
 * against the DOM so that design §5.8's fourth invalidation condition —
 * Cloudscape reordering those branches — fails loudly instead of silently.
 *
 * **Query data is supplied explicitly per case.** `WizardStepTarget.test.tsx`
 * and `WizardStepTarget.dropdown-error.test.tsx` both mock these hooks at module
 * scope, and that empty list is the fixture defect this spec sits downstream of
 * (design §5.3) — a state read off a mock that omits it reads `undefined` and is
 * silently falsy. Nothing here inherits a default: the factory returns whatever
 * the current case set, and every stub carries `data`, `isLoading`, `isError`
 * and `refetch`, which is the whole of what `GlueCatalogForm` and
 * `selectAsyncStatus` (`components/common/select-async-status.ts:24-28`, read at
 * `:50`, `:52`, `:65`) take off a query result.
 *
 * Task 1.3's `utils/__tests__/glue-target-eligibility.property.test.ts` owns
 * Property 1's **predicate** half and is not duplicated here; no assertion below
 * re-tests `startsWith`.
 *
 * `fast-check ^3.15.0`, an existing dependency. `numRuns` is stated per property
 * with its arithmetic wherever one run mounts several React trees.
 *
 * Coverage note: `.kiro/steering/test-mapping.md`'s `fileMatchPattern` is scoped
 * to Lambda Python, AgentCore Python and shell files and the table carries no
 * frontend rows, so it names no gate here. The gate is `make test-frontend` —
 * **both** legs, vitest and `tsc --noEmit`.
 *
 * This file touches no production code.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import * as fc from 'fast-check';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type SelectWrapper from '@cloudscape-design/components/test-utils/dom/select';
import { TargetForm } from '../TargetForms';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';
import TargetConfigForm from '../../../components/TargetConfigForm';
import type { TargetConfig } from '../../../types';
import {
  GLUE_TARGET_DATABASE_PREFIX,
  GLUE_TARGET_EXCLUSION_NOTE,
  GLUE_TARGET_NONE_ELIGIBLE,
  GLUE_TARGET_NO_DATABASES,
  GLUE_TARGET_NO_TABLES_NEXT_STEP,
  glueTargetNoTablesEmpty,
} from '../../../utils/glue-target-eligibility';

const useGlueDatabasesMock = vi.fn();
const useTablesInDatabaseMock = vi.fn();

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../../hooks');
  return {
    ...actual,
    useGlueDatabases: () => useGlueDatabasesMock(),
    useTablesInDatabase: (database?: string) => useTablesInDatabaseMock(database),
    // Neither component mounts the S3 Tables subforms here, but both modules
    // import these at top level, so they are stubbed rather than left to reach
    // real React Query.
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
// Name generators. Drawn from the character set a real Glue database name uses,
// so a failure is about the filter rather than about how Cloudscape renders an
// exotic label. Task 1.3's arbitraries are deliberately not reused — they are
// tuned to stress the predicate (leading spaces, upper case, the empty name),
// and this file's subject is rendering.
// ---------------------------------------------------------------------------
const suffixArb = fc.stringOf(
  fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz0123456789_'.split('')),
  { minLength: 1, maxLength: 12 },
);

const eligibleNameArb = suffixArb.map((s) => `${GLUE_TARGET_DATABASE_PREFIX}${s}`);

/**
 * A name the prefix rule refuses. `agentglue_` is drawn most often because it is
 * 84 of the 93 databases in the measured account; the `.filter` is a guard, not
 * the rule, so it cannot mislabel a draw.
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
 * order — accumulated while walking the drawn specs, never recovered by
 * re-filtering the input, which would make the production rule its own oracle
 * (`LGR-28b4d75-5`). Names are unique so an option-list equality is an assertion
 * about the filter and not about how Cloudscape treats a repeated value.
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

/**
 * Mount the wizard subform through its real entry point — the `TargetForm`
 * dispatcher, whose `case 'glue_catalog'` is `GlueCatalogForm`'s only caller.
 * Rendering the dispatcher exercises that edge without pulling in the JDBC
 * panels a full `WizardStepTarget` render would mount.
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

/** Mount the edit modal's Glue Catalog subform — used only by the C1 example. */
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

function wizardSelects(container: HTMLElement): { database: SelectWrapper; table: SelectWrapper } {
  const wrapper = createWrapper(container);
  const database = wrapper.findSelect('[data-testid="glue-db-select"]');
  const table = wrapper.findSelect('[data-testid="glue-table-select"]');
  expect(database, 'expected the glue-db-select <Select>').toBeTruthy();
  expect(table, 'expected the glue-table-select <Select>').toBeTruthy();
  return { database: database!, table: table! };
}

/**
 * The modal's `<Select>`s carry no `data-testid`, and this task must not add one
 * to a production file, so they are resolved by their `FormField` label.
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

function offeredOptions(select: SelectWrapper): string[] {
  select.openDropdown();
  return select
    .findDropdown()
    .findOptions()
    .map((o) => o.getElement().textContent?.trim() ?? '');
}

/**
 * Everything the OPEN dropdown renders, as text — Property 4's subject: what
 * Cloudscape actually put on screen, not the props it was handed. `null` when
 * the dropdown did not open, which a disabled `<Select>` never does; that is a
 * distinct state from an empty one and must not read as one.
 */
function openDropdownText(select: SelectWrapper): string | null {
  select.openDropdown();
  const open = select.findDropdown().findOpenDropdown();
  return open ? (open.getElement().textContent ?? '') : null;
}

/** The open dropdown's element, for structural assertions about controls. */
function openDropdownElement(select: SelectWrapper): HTMLElement | null {
  select.openDropdown();
  return select.findDropdown().findOpenDropdown()?.getElement() ?? null;
}

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
// Property 1 — rendered half, wizard side
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 1: both pickers offer exactly the eligible sublist, in source order, and offer the same set', () => {
  /**
   * **Validates: Requirements 1.1, 1.4, 4.1**
   *
   * The wizard's Database picker offers exactly the eligible sublist of whatever
   * the endpoint returned, in the endpoint's own order.
   *
   * A list equality, not a membership check: a `.sort()` anywhere between the
   * query data and `options` fails here rather than passing something weaker.
   * R1.1 offers "the sublist", and a picker that reordered its options would be
   * offering a different list from the one the endpoint returned.
   *
   * The agreement half of Property 1 — that this list is byte-identical to the
   * modal's for the same data — is asserted in the sibling file, which renders
   * both components in one run. Splitting it across two files would leave
   * neither able to make the comparison.
   */
  it('the wizard offers exactly the eligible databases, in source order', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames }) => {
        const { container, unmount } = renderWizard({
          databases: queryStub({ data: input }),
        });
        try {
          const { database } = wizardSelects(container);
          expect(offeredOptions(database)).toEqual(eligibleNames);
        } finally {
          unmount();
        }
      }),
      { numRuns: 100 },
    );
  });
});

// ===========================================================================
// Property 2 — wizard side
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 2: the Database picker’s rendered state is a total function of (totalCount, eligible.length)', () => {
  /**
   * **Validates: Requirements 1.3, 1.5**
   *
   * Exactly one of three states renders in the wizard's Database picker, and
   * which one is determined by `(totalCount, eligible.length)`: options when
   * `eligible.length > 0`; `GLUE_TARGET_NONE_ELIGIBLE` when `totalCount > 0 &&
   * eligible.length === 0`; `GLUE_TARGET_NO_DATABASES` when `totalCount === 0`.
   *
   * **Total** is the load-bearing word, so every arm asserts its own statement
   * present *and* the other absent. A build that showed the right sentence
   * alongside a stale one would pass a one-sided check and fails here. The
   * `totalCount > 0 && eligible.length === 0` arm is the one R1.5 exists for and
   * the one a plain `.filter()` destroys by leaving a single number behind.
   */
  it('renders exactly the one statement the two counts select', () => {
    fc.assert(
      fc.property(drawArb, ({ input, eligibleNames }) => {
        const { container, unmount } = renderWizard({
          databases: queryStub({ data: input }),
        });
        try {
          const { database } = wizardSelects(container);
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

          // The "exactly one" half, stated separately because it does not
          // follow from the per-arm checks above on their own.
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
// Property 3 — wizard side
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 3: the Table picker’s empty state names the selected database', () => {
  /**
   * **Validates: Requirements 2.1, 2.4**
   *
   * For any valid Glue database name, when that database is selected in the
   * wizard and the tables endpoint resolves to zero tables, the Table picker
   * renders an empty state containing that exact name together with
   * `GLUE_TARGET_NO_TABLES_NEXT_STEP`.
   *
   * Asserted by containment of the drawn string rather than by a regex, so a
   * generated name carrying metacharacters cannot pass or fail for the wrong
   * reason. A generic "No tables found" — one sentence for several distinct
   * meanings, which `LGR-2bcdd0d-3` rejected — fails the name check.
   */
  it('names the selected database and states the next step', () => {
    fc.assert(
      fc.property(eligibleNameArb, (database) => {
        const { container, unmount } = renderWizard({
          targetConfig: { database_name: database },
          databases: queryStub({ data: [{ database_name: database }] }),
          tables: queryStub({ data: [] }),
        });
        try {
          const { table } = wizardSelects(container);
          // R2.3 disables the picker without a database; with one selected it
          // must be reachable, or the empty state is unobservable and every
          // assertion below would be vacuous.
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
   * The name is the *selected* database's, not merely a name in scope. Two
   * distinct databases are drawn and the second is selected, so a build that
   * named the first offered database — or the first row of query data — fails.
   * Without this, "contains the name" could be satisfied by a sentence naming
   * the wrong database.
   *
   * **The substring precondition is load-bearing, and it was found by a mutation
   * run rather than reasoned about up front.** `uniqueArray` only guarantees the
   * two names differ, and two *different* names can still nest — `…_a` inside
   * `…_ab` — in which case `not.toContain(other)` fails on a correct component,
   * because the sentence naming `selected` contains `other` as a substring. That
   * is a defect in the assertion, not in the picker, and it made the sibling
   * file's copy of this test seed-dependently red. Rejecting nested pairs keeps
   * the assertion about which database was named.
   */
  it('names the database that is selected, not another one in the list', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(eligibleNameArb, { minLength: 2, maxLength: 2 }),
        ([other, selected]) => {
          fc.pre(!selected.includes(other) && !other.includes(selected));
          const { container, unmount } = renderWizard({
            targetConfig: { database_name: selected },
            databases: queryStub({
              data: [{ database_name: other }, { database_name: selected }],
            }),
            tables: queryStub({ data: [] }),
          });
          try {
            const { table } = wizardSelects(container);
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
// Property 4 — wizard side
// ===========================================================================

/**
 * The whole `(isLoading, isError, isEmpty)` triple, enumerated rather than
 * sampled. Design §6's statement is "for any combination", and eight cells is
 * small enough to exhaust — a drawn boolean triple would *probably* cover all
 * eight over 100 runs, and probably is a weaker claim than the property makes.
 * The database name is still generated, so the property remains quantified.
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
 * Derived from R2.2 (exactly one) and from the two source reads design §2.1 and
 * §5.2 record — `selectAsyncStatus` resolves `isError` before `isLoading` into
 * one `statusType` (`select-async-status.ts:50-54`), and Cloudscape's
 * `useDropdownStatus` is `loading`, else `error`, else `isEmpty && empty` — and
 * **not** from observed output, which would make the components their own
 * oracle.
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
   * For every `(isLoading, isError, isEmpty)` combination, the wizard's Database
   * `<Select>` renders exactly one of its loading, error and empty statements —
   * never the empty statement together with `Couldn't load databases`, and never
   * a recovery link in the empty state.
   *
   * The recovery link is asserted in **both** directions. C8's "no false
   * affordance" — `LGR-2bcdd0d-3` rejected a Retry button for a condition
   * re-requesting cannot fix — requires Retry to be absent from the empty state;
   * asserting only its absence would also pass in a build where the error state
   * lost its Retry, which is a different regression with the same output.
   *
   * `isEmpty` is realised through the **data**, never by poking a prop: an empty
   * option list is what Cloudscape's third branch keys on, so driving it any
   * other way would test a state the component cannot actually reach.
   */
  it('renders exactly one async statement in the Database picker, over the whole triple', () => {
    fc.assert(
      fc.property(eligibleNameArb, (name) => {
        for (const t of ASYNC_TRIPLE) {
          const data = t.isEmpty ? [] : [{ database_name: name }];
          const { container, unmount } = renderWizard({
            databases: queryStub({ data, isLoading: t.isLoading, isError: t.isError }),
          });
          try {
            const { database } = wizardSelects(container);
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
   * The same claim for the wizard's Table picker, which the reported symptom is
   * actually about. Its noun is `tables`, so its loading and error copy differ,
   * and its `empty` node is built per-database — three reasons the Database
   * picker's result does not carry over.
   *
   * A database is always selected: R2.3 keeps the picker disabled without one,
   * and a disabled `<Select>` renders no dropdown content, so every assertion
   * would otherwise be vacuously true.
   */
  it('renders exactly one async statement in the Table picker, over the whole triple', () => {
    fc.assert(
      fc.property(eligibleNameArb, (database) => {
        const emptyStatement = `${glueTargetNoTablesEmpty(database)} ${GLUE_TARGET_NO_TABLES_NEXT_STEP}`;
        for (const t of ASYNC_TRIPLE) {
          const { container, unmount } = renderWizard({
            targetConfig: { database_name: database },
            databases: queryStub({ data: [{ database_name: database }] }),
            tables: queryStub({
              data: t.isEmpty ? [] : [{ table_name: 'orders' }],
              isLoading: t.isLoading,
              isError: t.isError,
            }),
          });
          try {
            const { table } = wizardSelects(container);
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
// Property 6 — wizard analogue
// ===========================================================================

describe('Feature: p66-glue-catalog-target-selection, Property 6: a stored database survives the filter', () => {
  /**
   * **Validates: Requirements 4.3**
   *
   * Design §6 states Property 6 over the **edit modal**, because that is the
   * surface a pipeline record is loaded into; the sibling file carries it there.
   * Task 1.7 asks for the properties "across both pickers", and the wizard has
   * the same hazard for the same reason — its `selectedOption` is built from
   * `state.targetConfig.database_name`, not from the filtered `dbOptions` — so
   * the analogue is asserted here rather than assumed to follow.
   *
   * "Does not clear it" is asserted from both sides: the stored value is on
   * screen, *and* nothing was written back to the parent. A component that
   * cleared its own state on mount could still leave the old label rendered for
   * a frame, and `onChange` is what tells the two apart.
   *
   * Task 1.2's characterization covers two fixed cases of this; the property
   * covers the space. Both are kept — 1.2's file is the R5.1 artifact that had
   * to exist before 1.5 and must keep passing unchanged.
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
          const { container, onChange, unmount } = renderWizard({
            targetConfig: { database_name: stored },
            // The stored name is deliberately absent from the query data, which
            // is the shape the post-1.5 world produces for a database the
            // prefix rule excludes.
            databases: queryStub({ data: [{ database_name: offered }] }),
          });
          try {
            const { database } = wizardSelects(container);
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
   * The Table half: a stored `table_name` absent from the fetched tables still
   * renders as selected, because its `selectedOption` reads
   * `state.targetConfig.table_name` directly.
   */
  it('renders any stored table as the selected option and writes nothing back', () => {
    fc.assert(
      fc.property(eligibleNameArb, suffixArb, suffixArb, (database, storedTable, fetchedTable) => {
        fc.pre(storedTable !== fetchedTable);
        const { container, onChange, unmount } = renderWizard({
          targetConfig: { database_name: database, table_name: storedTable },
          databases: queryStub({ data: [{ database_name: database }] }),
          tables: queryStub({ data: [{ table_name: fetchedTable }] }),
        });
        try {
          const { table } = wizardSelects(container);
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

// ===========================================================================
// Example tests — the two things no property can express
// ===========================================================================

/**
 * The Alert's three bullets, each identified by a clause that appears in **that
 * bullet and nowhere else**. Transcribed from `TargetForms.tsx`'s Alert
 * (rendered under `data-testid="glue-catalog-preconditions"`).
 *
 * Bullet 2's *second* clause — "Glue role grants access only to databases and
 * tables under that prefix" — is shared with `GLUE_TARGET_EXCLUSION_NOTE` **on
 * purpose**: `glue-target-eligibility.ts`'s copy block reuses it verbatim so a
 * refusal reads as one rule (O4), and C10 forbids a fourth *statement of the
 * three preconditions*, not a reused clause. So bullet 2 is keyed on its own
 * opening — "The database name must start with" — which the note does not use.
 */
const ALERT_BULLET_MARKERS = {
  tableMustExist: 'must already exist in the Glue Data Catalog',
  databaseNamePrefix: 'The database name must start with',
  s3LocationBucket: 'adi-processed-files-',
} as const;

describe('p66 example: the exclusion note is present and does not restate the Alert (C10)', () => {
  /**
   * **Validates: Requirements 1.2, and C10**
   *
   * C10's constraint is that the exclusion copy must not become a **fourth**
   * statement of the three preconditions. The "Change one, change all three"
   * comment above the Alert governs three phrasings — the target-type tile copy,
   * this Alert, and the pre-flight's 400 messages — and a fourth would be one
   * more place to drift.
   *
   * Asserted three ways, because "not duplicated" needs a positive shape to be
   * checkable at all:
   *
   * 1. The note **is** rendered, as the Database `FormField`'s description — R1.2
   *    requires the rule stated wherever a database is excluded, and an
   *    empty-state-only note would say nothing in the ordinary case where some
   *    databases are offered and others were silently dropped.
   * 2. The description contains **neither** of the other two bullets' own
   *    clauses. Bullet 2's shared clause is excluded from this check by design
   *    (see `ALERT_BULLET_MARKERS`), so the check is about the two bullets the
   *    note has no business restating.
   * 3. There is exactly **one** bullet list, with exactly **three** items. This
   *    is what catches the literal failure C10 describes — a second `<ul>`
   *    beneath the note repeating the preconditions — which the text checks
   *    above would miss if it were worded differently.
   */
  it('renders the note as the field description, with the Alert’s bullets not repeated beneath it', () => {
    const { container, unmount } = renderWizard({
      databases: queryStub({ data: [{ database_name: `${GLUE_TARGET_DATABASE_PREFIX}analytics` }] }),
    });
    try {
      const wrapper = createWrapper(container);

      // (1) the note is present, and it is on the Database field specifically.
      const databaseField = wrapper
        .findAllFormFields()
        .find((f) => f.findLabel()?.getElement().textContent?.trim() === 'Glue Database');
      expect(databaseField, 'expected a FormField labelled "Glue Database"').toBeTruthy();
      const description = databaseField!.findDescription();
      expect(description, 'expected the Database field to carry a description').not.toBeNull();
      const descriptionText = description!.getElement().textContent ?? '';
      expect(descriptionText).toContain(GLUE_TARGET_EXCLUSION_NOTE);

      // (2) it does not restate the two bullets it has no business restating.
      expect(descriptionText).not.toContain(ALERT_BULLET_MARKERS.tableMustExist);
      expect(descriptionText).not.toContain(ALERT_BULLET_MARKERS.s3LocationBucket);
      // Nor is it a bullet list of its own.
      expect(description!.getElement().querySelectorAll('li')).toHaveLength(0);

      // (3) exactly one list, of exactly three items, and it is the Alert's.
      const alert = wrapper.find('[data-testid="glue-catalog-preconditions"]');
      expect(alert, 'expected the preconditions Alert').not.toBeNull();
      expect(container.querySelectorAll('ul')).toHaveLength(1);
      const bullets = container.querySelectorAll('li');
      expect(bullets).toHaveLength(3);
      const bulletText = Array.from(bullets)
        .map((li) => li.textContent ?? '')
        .join('\n');
      // Each of the three markers occurs exactly once across the whole render,
      // so no bullet was copied anywhere — into the description or elsewhere.
      const wholeText = container.textContent ?? '';
      for (const marker of Object.values(ALERT_BULLET_MARKERS)) {
        expect(bulletText, `bullet marker ${marker}`).toContain(marker);
        expect(wholeText.split(marker).length - 1, `occurrences of ${marker}`).toBe(1);
      }
      // And the note itself appears once, not once per surface.
      expect(wholeText.split(GLUE_TARGET_EXCLUSION_NOTE).length - 1).toBe(1);
    } finally {
      unmount();
    }
  });
});

describe('p66 example: the Table picker’s empty state offers no create-table control (C1)', () => {
  /**
   * **Validates: Requirements 2.4, and C1**
   *
   * C1 (`LGR-2682c7f-1`, four recorded grounds) means the pipeline will never
   * create the catalog table, so a "Create table" control in this empty state
   * would be one the system cannot honour — the false affordance
   * `LGR-2bcdd0d-3` rejected when it turned down a Retry button for a condition
   * re-requesting cannot fix. `GLUE_TARGET_NO_TABLES_NEXT_STEP` therefore states
   * the next step in prose and offers nothing to click.
   *
   * Asserted **structurally as well as textually**, and on **both** pickers. The
   * copy lives in one constant, but a control is a JSX change at a call site, so
   * either surface could gain one independently — and a text-only check would
   * miss an icon-only button, which is also exactly what C8 forbids (meaning in
   * words, not in an affordance).
   */
  it('renders the next step as prose with no interactive control, on both pickers', () => {
    const database = `${GLUE_TARGET_DATABASE_PREFIX}analytics`;
    const expectedEmpty = `${glueTargetNoTablesEmpty(database)} ${GLUE_TARGET_NO_TABLES_NEXT_STEP}`;

    const surfaces: Array<{ name: string; render: () => Rendered; select: (c: HTMLElement) => SelectWrapper }> = [
      {
        name: 'wizard',
        render: () =>
          renderWizard({
            targetConfig: { database_name: database },
            databases: queryStub({ data: [{ database_name: database }] }),
            tables: queryStub({ data: [] }),
          }),
        select: (c) => wizardSelects(c).table,
      },
      {
        name: 'edit modal',
        render: () =>
          renderModal({
            value: { database },
            databases: queryStub({ data: [{ database_name: database }] }),
            tables: queryStub({ data: [] }),
          }),
        select: (c) => modalSelects(c).table,
      },
    ];

    for (const surface of surfaces) {
      const { container, unmount } = surface.render();
      try {
        const table = surface.select(container);
        const dropdown = openDropdownElement(table);
        expect(dropdown, `${surface.name}: the Table dropdown should open`).not.toBeNull();

        // The statement is there — otherwise "no control" would be trivially
        // true of a dropdown showing nothing at all, which is the pre-p66
        // defect.
        expect(dropdown!.textContent ?? '', surface.name).toContain(expectedEmpty);

        // No control of any kind inside the empty dropdown.
        const controls = dropdown!.querySelectorAll(
          'button, a, input, [role="button"], [role="link"]',
        );
        expect(
          Array.from(controls).map((el) => el.outerHTML),
          `${surface.name}: the empty state must offer nothing to click`,
        ).toEqual([]);
        expect(table.findErrorRecoveryButton(), surface.name).toBeNull();

        // And no create-table wording, which would promise the affordance even
        // without rendering one.
        expect((dropdown!.textContent ?? '').toLowerCase(), surface.name).not.toContain(
          'create table',
        );
        expect((dropdown!.textContent ?? '').toLowerCase(), surface.name).not.toContain(
          'create a table',
        );
      } finally {
        unmount();
      }
    }
  });
});
