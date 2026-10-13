// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 5.5 — **Property 7: the gate trims and the value does not.**
 *
 * > *For any* string a user types as the wizard's Display_Label, the upload
 * > request body omits the Display_Label key exactly when the string's trimmed
 * > form is empty, and otherwise carries the string **verbatim**, with its
 * > original padding intact.
 *
 * **Validates: Requirements 2.2, 2.3, 2.4, 4.4**
 *
 * Two rules, both the producer's and both different. `appendDisplayLabel`
 * (`SchemaManagementPage.tsx:300-302`) and `uploadSchema` (`use-schemas.ts:129-133`)
 * gate on `trim()` and forward the **untrimmed** value; `_submitted_display_label`
 * treats absent, `null` and `""` alike as "no label"
 * (`handlers/schemas.py:1974-1994`) and `upload_schema` stores a surviving label
 * with its padding intact (`:2098-2105`). So the gate trims and the value does not,
 * and the wizard's inline upload — a *different* HTTP client from the Schemas page's
 * — has to obey the same pair.
 *
 * ## Why this is generated rather than three examples
 *
 * The interesting inputs are the ones a handwritten list omits: a tab, a newline,
 * `\u00a0`, `\u3000`. JS `trim()` treats all of them as whitespace and so does
 * Python's `str.strip()`, so a value made only of them is "no label" at both ends
 * of the wire — and any of them *inside* a label is padding the producer keeps.
 * `\ufeff` is deliberately **not** generated: JS `trim()` strips it, Python's
 * `strip()` does not, so it is a genuine frontend/backend disagreement and belongs
 * to whatever spec decides which side is right, not to this property.
 *
 * ## What is asserted, and against what
 *
 * The **request body actually posted**. `apiClient.post` is mocked and the
 * `/schemas/upload` call's payload is read back, so the assertion sits at the wire
 * rather than on wizard state — a state field that never reaches the body would
 * satisfy Requirement 2.2 in name only. The expected key is read from
 * `DISPLAY_LABEL_REQUEST_KEY` and never transcribed as `'displayLabel'`
 * (Requirement 4.7): a test written from the producer's own literal cannot detect
 * the producer drifting (`LGR-28b4d75-5`, **template / ratified**).
 *
 * ## Mocking
 *
 * `apiClient` only — never `../../hooks`. A `vi.mock` factory **replaces** the
 * module, so any export the factory omits reads back `undefined` and the component
 * calls `undefined()` **during render, inside this test**; the failure is a stack
 * trace naming a value that is not a function rather than an assertion diff. The
 * real `usePipelines` / `useBedrockModels` / target-resource hooks therefore run,
 * against a `get` stub that answers `{}` — every one of those fetchers normalises a
 * missing collection to `[]` (`use-resources.ts:62`, `:84`, `use-pipelines.ts:113-115`).
 *
 * `utils/schema-display.ts` is likewise not mocked: `DISPLAY_LABEL_REQUEST_KEY` is
 * part of what is under test.
 *
 * ## Coverage note
 *
 * `UploadSchemaView` (`WizardStepSchema.tsx`) had **1 caller and no covering tests**
 * before this spec (CodeGraph, confirmed by search across the four
 * `WizardStepSchema.*.test.tsx` files). Its expectations come from Requirement 2, not
 * from observed output — before task 5.3 there was no label field to observe.
 *
 * Feature: p36-schema-naming, Property 7
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';
import { INITIAL_WIZARD_STATE, type WizardState } from '../wizard/types';
// R2.8 / R4.7 — the expected key is imported from its single definition.
import { DISPLAY_LABEL_REQUEST_KEY } from '../../utils/schema-display';

const { postMock, getMock } = vi.hoisted(() => ({ postMock: vi.fn(), getMock: vi.fn() }));
vi.mock('../../services/api-client', () => ({
  apiClient: { post: postMock, get: getMock, put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import CreatePipelinePage from '../CreatePipelinePage';

/* ------------------------------------------------------------------ */
/*  Generators                                                         */
/* ------------------------------------------------------------------ */

/**
 * Whitespace both `String.prototype.trim` and Python's `str.strip` remove, so a
 * run of these is "no label" on both sides of the wire.
 */
const WHITESPACE_CHARS = [' ', '\t', '\n', '\r', '\v', '\f', '\u00a0', '\u2003', '\u3000'];

const whitespaceRun = (minLength: number, maxLength: number) =>
  fc.array(fc.constantFrom(...WHITESPACE_CHARS), { minLength, maxLength }).map((cs) => cs.join(''));

/** A label that is nothing but whitespace — Requirement 2.4's second clause. */
const whitespaceOnlyLabel = whitespaceRun(1, 5);

/** Something that survives `trim()`, including shapes a person would really type. */
const visibleCore = fc
  .oneof(
    fc.string({ minLength: 1, maxLength: 10 }),
    fc.constantFrom(
      'Customer master',
      'orders extract',
      'Ünïcödé label',
      '日本語のラベル',
      'a',
      '7',
      '✓',
      'two  spaces',
    ),
  )
  .filter((s) => s.trim() !== '');

/** A non-empty label wearing padding the producer must keep (Requirement 2.4). */
const paddedLabel = fc
  .tuple(whitespaceRun(0, 3), visibleCore, whitespaceRun(0, 3))
  .map(([left, core, right]) => `${left}${core}${right}`);

const typedLabel = fc.oneof(
  { weight: 3, arbitrary: paddedLabel },
  { weight: 3, arbitrary: whitespaceOnlyLabel },
  // Typing nothing at all: the boundary between the two branches.
  { weight: 1, arbitrary: fc.constant('') },
);

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

/**
 * A wizard state that reaches the **inline upload** branch of `handleSubmit`: no
 * existing schema id, non-empty `schemaFields`, an S3 Tables target (no step
 * guards) and a model chosen. `schemaSourceExplicitlySet` keeps the schema step on
 * the Upload tile — the pre-selection effect would otherwise flip a non-JDBC
 * target back to `'detected'` (`WizardStepSchema.tsx`, P19 task 10.2) and the
 * Display_Label input would never render.
 */
const SEEDED_STATE: WizardState = {
  ...INITIAL_WIZARD_STATE,
  pipelineName: 'display-label-property',
  description: 'p36 Property 7',
  targetType: 's3_tables',
  targetConfig: { bucket_name: 'b', table_name: 't', namespace: 'default' },
  schemaSource: 'upload',
  schemaSourceExplicitlySet: true,
  schemaFields: [
    { name: 'id', type: 'string', nullable: false, description: '' },
    { name: 'amount', type: 'double', nullable: true, description: '' },
  ],
  existingSchemaId: null,
  schemaDisplayLabel: '',
  aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/new']}>
          <CreatePipelinePage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
}

function labelInput(): HTMLInputElement {
  // `data-testid` lands on Cloudscape's wrapper, not the control — the same
  // `.querySelector('input')` the Schemas page's tests use for its sibling field.
  return screen.getByTestId('wizard-display-label').querySelector('input')!;
}

/**
 * Types `typed` into the wizard's Display_Label input, submits, and returns both
 * the value the control ended up holding and the body posted to `/schemas/upload`.
 *
 * The state is seeded through the draft-restore path rather than by hand-driving
 * the upload control, so the file-basename default (Requirement 2.9, its own
 * property in task 5.7) never fires and the typed value is the label's only source.
 */
async function submitWithTypedLabel(typed: string) {
  cleanup();
  sessionStorage.clear();
  postMock.mockClear();
  sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(SEEDED_STATE));

  renderPage();
  fireEvent.click(screen.getByTestId('wizard-draft-restore-button'));
  clickNext(); // Name → Source
  clickNext(); // Source → Target
  clickNext(); // Target → Schema

  fireEvent.change(labelInput(), { target: { value: typed } });
  // Read the value back rather than assuming it: an `<input type="text">` applies
  // the HTML value-sanitisation algorithm, which strips CR and LF, and Cloudscape
  // reports `event.target.value`. So this is the string the user actually got into
  // the control, and the property below pins it to what was typed.
  const held = labelInput().value;

  clickNext(); // Schema → Model
  clickNext(); // Model → Review

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
  });

  const uploadCall = postMock.mock.calls.find(([url]) => url === '/schemas/upload');
  expect(uploadCall).toBeDefined();
  return { held, body: uploadCall![1] as Record<string, unknown> };
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  postMock.mockReset();
  getMock.mockReset();
  // Every list fetcher normalises a missing collection to `[]`, so one answer
  // serves all of them.
  getMock.mockResolvedValue({});
  postMock.mockImplementation((url: string) =>
    url === '/schemas/upload' ? Promise.resolve({}) : Promise.resolve({ success: true }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// Feature: p36-schema-naming, Property 7: The gate trims and the value does not
describe('Property 7 — the gate trims and the value does not', () => {
  it(
    'omits the Display_Label key exactly when the typed value trims to empty, and otherwise sends it verbatim',
    async () => {
      // Both halves of the partition have to be reached, or a one-sided run would
      // report a pass for a property it never exercised.
      const branchesSeen = { omitted: 0, forwarded: 0 };

      await fc.assert(
        fc.asyncProperty(typedLabel, async (typed) => {
          const { held, body } = await submitWithTypedLabel(typed);

          // Non-vacuity: the control holds what was typed, modulo the DOM's own
          // newline stripping. Without this a control that silently dropped the
          // value would make every branch below trivially true.
          expect([typed, typed.replace(/[\r\n]/g, '')]).toContain(held);

          if (held.trim() === '') {
            // R2.3 / R2.4 — no label, so no key. Not `""`: an empty label is the
            // absence of one at the producer too, and sending it would make the
            // request say something the user did not.
            expect(Object.keys(body)).not.toContain(DISPLAY_LABEL_REQUEST_KEY);
            branchesSeen.omitted += 1;
          } else {
            // R2.2 / R2.4 — verbatim, padding intact. `toBe` on the untrimmed
            // string is the whole assertion: a `.trim()` slipped into the
            // forwarding would pass a trimmed-equality check.
            expect(body[DISPLAY_LABEL_REQUEST_KEY]).toBe(held);
            branchesSeen.forwarded += 1;
          }
        }),
        { numRuns: 100 },
      );

      expect(branchesSeen.omitted).toBeGreaterThan(0);
      expect(branchesSeen.forwarded).toBeGreaterThan(0);
    },
    240_000,
  );
});
