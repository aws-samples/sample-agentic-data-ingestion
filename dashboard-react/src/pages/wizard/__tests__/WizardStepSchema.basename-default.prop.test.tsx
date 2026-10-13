// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 5.7 — **Property 9: the basename defaults the label and
 * never overwrites a typed one.**
 *
 * **Validates: Requirements 2.9**
 *
 * > WHERE the user has chosen a file in the Wizard_Upload_View and has supplied no
 * > Display_Label, THE wizard SHALL offer the chosen file's basename as the
 * > Display_Label.
 *
 * Feature: p36-schema-naming, Property 9: The basename defaults the label and
 * never overwrites a typed one — for any chosen filename and any pre-existing
 * value in the wizard's Display_Label input, the input holds the pre-existing
 * value when it is non-empty and the filename's basename otherwise.
 *
 * ## Why generation beats two examples here
 *
 * The don't-clobber half is **order-dependent**, so this file generates both
 * orderings rather than one: *type-then-choose-file* (the default must not land)
 * and *choose-file-then-type-then-choose-again* (the default lands, the typed
 * value replaces it, and a later file selection must not take it back).
 *
 * Two edges a handwritten pair would miss, both generated:
 *
 * - **A filename that is nothing but an extension.** `UploadSchemaView` strips the
 *   extension and returns without writing when what is left trims to empty
 *   (`WizardStepSchema.tsx`, `onFileChosen`), so `.gitignore` and `.json` leave the
 *   label alone rather than blanking it.
 * - **A whitespace-only pre-existing label.** The gate is `.trim()`, so blank text
 *   is "no label" and the basename still defaults over it — but the *value* is
 *   never trimmed in place, which is why the assertion compares the exact string.
 *
 * ## The parse-completes case, and why it is asserted rather than assumed
 *
 * `SchemaUploadWizard.handleFileChange` is a `useCallback` that fires
 * `onFileChosen` **synchronously** and `onSchemaReady` only *after*
 * `await parseSchemaFile` (`SchemaUploadWizard.tsx:41-79`), both from props
 * captured at the render in which the file was chosen. A callback that spread the
 * render-time `state` would rebuild the wizard state from a snapshot taken
 * *before* the basename default landed and silently undo it. Task 5.3 routes both
 * callbacks through a `stateRef` for exactly that reason.
 *
 * That regression is invisible to an ordering-only property, so every iteration
 * here **awaits the parse** and asserts whether fields were committed — which is
 * what makes "the label survived `onSchemaReady`" a real assertion rather than a
 * statement about a callback that never fired. Whether the parse commits is not
 * this property's subject; it is read from the parser's own dispatch rule
 * (`schema-parser.ts:22-26`: the final dot-segment, lower-cased, must be `json` or
 * `csv`) so the check is non-vacuous on every `.json` / `.csv` draw.
 *
 * ## What this file mocks
 *
 * `apiClient` only — never `../../../hooks`. Upload mode calls no hook
 * (`useSchemas` and `useSchemaDetail` are reached from the `existing` branch
 * alone), and a `vi.mock` factory that **replaced** the hooks module would make
 * every export it omits `undefined`, so a component that later calls one throws a
 * `TypeError` **during render, inside the test that renders it** rather than
 * failing an assertion — held-assertion inventory row 5's mechanism. Amend the
 * factory before adding a hook to a component these tests render.
 */
import { useState } from 'react';
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import type { ReactNode } from 'react';

vi.mock('../../../services/api-client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { WizardStepSchema } from '../WizardStepSchema';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';

/* ------------------------------------------------------------------ */
/*  Oracles                                                            */
/* ------------------------------------------------------------------ */

/**
 * The basename Requirement 2.9 offers: the filename with its final extension
 * removed, then trimmed, because this is a name a person reads.
 *
 * Expressed with `lastIndexOf` rather than by re-typing the production regex, so
 * the test is not its own oracle. A trailing dot is **not** an extension — there
 * is nothing after it to drop — so `"name."` keeps its dot, matching "strip the
 * extension" read literally.
 */
function basenameOf(fileName: string): string {
  const dot = fileName.lastIndexOf('.');
  if (dot < 0 || dot === fileName.length - 1) return fileName.trim();
  return fileName.slice(0, dot).trim();
}

/**
 * Whether `parseSchemaFile` will parse this filename at all — the parser's own
 * dispatch rule (`schema-parser.ts:22-26`), not a rule of this property. Used
 * only to know whether `onSchemaReady` fires, which is what makes the
 * parse-completes assertion non-vacuous.
 */
function parseSupported(fileName: string): boolean {
  const ext = fileName.split('.').pop()?.toLowerCase();
  return ext === 'json' || ext === 'csv';
}

/** The Display_Label the wizard should hold after a file selection. */
function expectedLabel(pre: string, fileName: string): string {
  if (pre.trim()) return pre; // a typed name is never overwritten
  return basenameOf(fileName) || pre; // nothing to offer leaves the label alone
}

/* ------------------------------------------------------------------ */
/*  Generators                                                         */
/* ------------------------------------------------------------------ */

/** Filename characters, including `.` so multi-dot and leading-dot names arise. */
const NAME_CHARS = [
  ...'abcdefghijklmnopqrstuvwxyz',
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  ...'0123456789',
  ...' _-.()',
  'é', 'Ü', 'ñ', '売', '上', '데', '이', '터',
];

const stemArb = fc
  .array(fc.constantFrom(...NAME_CHARS), { minLength: 0, maxLength: 12 })
  .map((cs) => cs.join(''));

const fileNameArb: fc.Arbitrary<string> = fc.oneof(
  {
    weight: 3,
    arbitrary: fc
      .record({ stem: stemArb, ext: fc.constantFrom('json', 'JSON', 'csv', 'txt', 'tar.gz', '') })
      .map(({ stem, ext }) => (ext ? `${stem}.${ext}` : stem))
      // A `File` needs a name; the empty draw is the only one excluded.
      .filter((name) => name.length > 0),
  },
  {
    weight: 1,
    arbitrary: fc.constantFrom(
      // Nothing but an extension — the basename trims to empty, so the label is
      // left alone rather than blanked.
      '.gitignore',
      '.json',
      '   .json',
      // ...and the ordinary shapes, plus a name whose "extension" is a second
      // suffix and one with no extension at all.
      'schema.json',
      'orders.csv',
      'Customer Master.json',
      'data.tar.gz',
      'name.',
      'no-extension',
    ),
  },
);

/** Whitespace that survives an `<input type="text">` unchanged. */
const BLANK_CHARS = [' ', '\t', '\u00a0', '\u2003', '\u3000'];

const NONBLANK_CHARS = [
  ...'abcdefghijklmnopqrstuvwxyz',
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
  ...'0123456789',
  ...'_-()',
  'é', 'Ü', 'ñ', '売', '上', '데', '이', '터',
];

const blankArb = fc
  .array(fc.constantFrom(...BLANK_CHARS), { minLength: 1, maxLength: 4 })
  .map((cs) => cs.join(''));

const padArb = fc.oneof(fc.constant(''), blankArb);

/** A label whose trimmed form is non-empty, with arbitrary padding kept intact. */
const typedLabelArb = fc
  .tuple(
    padArb,
    fc
      .array(fc.constantFrom(...NONBLANK_CHARS, ' '), { minLength: 1, maxLength: 10 })
      .map((cs) => cs.join('')),
    padArb,
  )
  .map(([before, core, after]) => `${before}${core}${after}`)
  .filter((label) => label.trim().length > 0);

/**
 * The pre-existing input value: absent, blank, or a real name.
 *
 * Newlines are deliberately **not** generated. `<input type="text">` strips CR and
 * LF under the HTML value-sanitization algorithm, so a generated `"\n"` would
 * produce counterexamples about the platform rather than about this rule.
 */
const preLabelArb: fc.Arbitrary<string> = fc.oneof(
  { weight: 1, arbitrary: fc.constant('') },
  { weight: 1, arbitrary: blankArb },
  { weight: 2, arbitrary: typedLabelArb },
);

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

// jsdom's `File` does not implement `.text()` — the same shim
// `SchemaUploadWizard.test.tsx` and `WizardStepSchema.upload-commit.test.tsx` use.
beforeAll(() => {
  if (!File.prototype.text) {
    File.prototype.text = function () {
      return new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.readAsText(this);
      });
    };
  }
});

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

/**
 * Real wizard state, so the label the component holds is observable rather than
 * spied — a `vi.fn()` would show the calls but not which one won.
 */
function Harness() {
  const [state, setState] = useState<WizardState>({
    ...INITIAL_WIZARD_STATE,
    schemaSource: 'upload',
    schemaSourceExplicitlySet: true,
  });
  return (
    <>
      <WizardStepSchema state={state} onChange={setState} />
      {/* JSON-encoded so a whitespace-only label is legible in a failure. */}
      <div data-testid="probe-display-label">{JSON.stringify(state.schemaDisplayLabel ?? null)}</div>
      <div data-testid="probe-field-count">{String(state.schemaFields.length)}</div>
    </>
  );
}

function renderUploadMode() {
  render(
    <Providers>
      <Harness />
    </Providers>,
  );
}

function labelInput(): HTMLInputElement {
  return screen.getByTestId('wizard-display-label').querySelector('input') as HTMLInputElement;
}

/** The label as wizard state holds it — the value `handleSubmit` would forward. */
function labelState(): string | null {
  return JSON.parse(screen.getByTestId('probe-display-label').textContent || 'null');
}

function committedFieldCount(): number {
  return Number(screen.getByTestId('probe-field-count').textContent);
}

function typeLabel(value: string) {
  fireEvent.change(labelInput(), { target: { value } });
}

/** A JSON Schema body, so a `.json` or `.csv` draw parses and commits. */
const SCHEMA_BODY = JSON.stringify({
  properties: { id: { type: 'integer', description: 'Primary key' } },
});

/**
 * Choose a file and let the parse settle, so `onSchemaReady` has fired or not by
 * the time the label is read.
 *
 * `act`'s own flush is **not** enough. `parseSchemaFile` awaits `File.text()`,
 * which resolves on a macrotask in jsdom (a `FileReader` load event), so the
 * commit lands after the `act` around the change event returns — which is why
 * `WizardStepSchema.upload-commit.test.tsx` needs a `waitFor` after its
 * equivalent helper. Turning the macrotask queue here instead keeps the property
 * free of a poll whose timeout would read as flake.
 *
 * The turn count is not a guess left unchecked: every iteration asserts whether
 * fields were committed against the parser's dispatch rule, so a flush too short
 * to let a supported parse finish fails the property rather than passing quietly.
 */
async function chooseFile(fileName: string) {
  const file = new File([SCHEMA_BODY], fileName, { type: 'application/json' });
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    fireEvent.change(input);
  });
  for (let turn = 0; turn < 6; turn += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

afterEach(() => {
  cleanup();
});

/* ================================================================== */
/*  Property 9                                                         */
/* ================================================================== */

// Feature: p36-schema-naming, Property 9: The basename defaults the label and
// never overwrites a typed one
describe('Property 9 — the basename defaults the Display_Label (R2.9)', () => {
  it('type-then-choose-file: a typed name survives, and a blank one takes the basename', async () => {
    await fc.assert(
      fc.asyncProperty(preLabelArb, fileNameArb, async (pre, fileName) => {
        cleanup();
        renderUploadMode();

        typeLabel(pre);
        expect(labelState(), 'the typed value should land verbatim').toBe(pre);

        await chooseFile(fileName);

        const expected = expectedLabel(pre, fileName);
        expect(
          labelState(),
          `pre=${JSON.stringify(pre)} file=${JSON.stringify(fileName)} ` +
            `basename=${JSON.stringify(basenameOf(fileName))}`,
        ).toBe(expected);
        // The property is about what the *input* holds, so assert the DOM too and
        // not only the state behind it.
        expect(labelInput().value).toBe(expected);

        // The parse-completes half: when the parse commits fields, the label
        // above was read *after* `onSchemaReady` rebuilt the wizard state.
        expect(
          committedFieldCount() > 0,
          `parse should ${parseSupported(fileName) ? '' : 'not '}have committed for ` +
            JSON.stringify(fileName),
        ).toBe(parseSupported(fileName));
      }),
      {
        numRuns: 100,
        examples: [
          // The plain default: nothing typed, an ordinary name, a parse that
          // completes. If `onSchemaReady` rebuilt state from a pre-default
          // snapshot, this is where the basename would vanish.
          ['', 'Customer Master.json'],
          // A typed name is not overwritten, and the parse still completes.
          ['Q3 orders', 'schema.json'],
          // Blank counts as no label, so the basename defaults over it.
          ['   ', 'orders.csv'],
          // A name that is nothing but an extension leaves the label alone rather
          // than blanking it — and its parse still completes.
          ['', '.json'],
          ['', '.gitignore'],
          // Padding on a typed name is kept exactly as typed.
          ['  Customer master  ', 'anything.json'],
          // Two extensions: only the last one is dropped.
          ['', 'data.tar.gz'],
        ],
      },
    );
  }, 300_000);

  it('choose-file-then-type: the typed value replaces the default and a later file does not take it back', async () => {
    await fc.assert(
      fc.asyncProperty(
        preLabelArb,
        fileNameArb,
        fileNameArb,
        async (pre, firstName, secondName) => {
          cleanup();
          renderUploadMode();

          // 1. A file first, with the label empty: the basename defaults in.
          await chooseFile(firstName);
          expect(
            labelState(),
            `the default should be the basename of ${JSON.stringify(firstName)}`,
          ).toBe(basenameOf(firstName));
          expect(committedFieldCount() > 0).toBe(parseSupported(firstName));

          // 2. The user types over it. This is the value that must not be clobbered.
          typeLabel(pre);
          expect(labelState(), 'typing should replace the default verbatim').toBe(pre);

          // 3. A second selection. `onFileChosen` fires again, and the gate is
          //    what stops it from overwriting what the user just typed.
          await chooseFile(secondName);

          const expected = expectedLabel(pre, secondName);
          expect(
            labelState(),
            `pre=${JSON.stringify(pre)} second=${JSON.stringify(secondName)} ` +
              `basename=${JSON.stringify(basenameOf(secondName))}`,
          ).toBe(expected);
          expect(labelInput().value).toBe(expected);
        },
      ),
      {
        numRuns: 100,
        examples: [
          // The don't-clobber case in full: a default landed, the user renamed it,
          // and a second file must not win.
          ['Q3 orders', 'schema.json', 'other.json'],
          // Clearing the input entirely re-opens the default for the next file.
          ['', 'first.json', 'second.csv'],
          // Blank is no label, so the second file's basename defaults over it.
          ['\t', 'first.json', 'second.json'],
          // A second file that is nothing but an extension leaves the typed name
          // alone from both directions.
          ['Customer master', 'first.json', '.gitignore'],
          ['', 'first.json', '.json'],
        ],
      },
    );
  }, 300_000);
});
