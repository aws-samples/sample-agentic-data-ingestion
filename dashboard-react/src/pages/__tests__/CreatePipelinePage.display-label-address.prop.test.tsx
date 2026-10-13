// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 5.6 — **Property 8: no Display_Label value can reach an
 * address.**
 *
 * **Validates: Requirements 2.5, 2.6**
 *
 * > THE Display_Label supplied in the Create Pipeline wizard SHALL NOT change the
 * > uploaded schema's id, the key of its backing object, or the value the wizard
 * > sends as `target_schema` to `/pipelines/provision`.
 *
 * > THE Wizard_Inline_Upload SHALL compute the uploaded schema's name exactly
 * > once and SHALL send that same computed name as `target_schema`.
 *
 * Feature: p36-schema-naming, Property 8: No Display_Label value can reach an
 * address — for **any** string a user types into the wizard's Display_Label
 * input, the `/schemas/upload` request's `name` is still
 * `uploaded_schema_{epoch_ms}` with digits that came from the clock, and the
 * `target_schema` sent to `/pipelines/provision` is *identical* to that `name`.
 *
 * ## D1's guarantee, made mechanical — and why it needs to be
 *
 * D1 (ratified at requirement 2.5) keeps the wizard's chosen name a
 * **presentation** value: the id stays `uploaded_schema_${Date.now()}` and the
 * label rides alongside it. The failure a leak would cause is **silent and lands
 * six steps from its cause**. A label reaching the id flows into `target_schema`
 * → `target_schema_name` (`pipelines_crud.py:837`) → `schema_mapping`'s by-name
 * fallback, which interpolates that attribute into **six** candidate object keys
 * (`schema_mapping/lambda_function.py:249-250` → `:296` →
 * `shared/target_schema_resolution.py:407`) and raises `Could not find uploaded
 * schema` at `:317-319` when all six miss — with no other symptom. Nothing
 * between the wizard and that exception mentions a display label.
 *
 * A user-chosen string is also the input most likely to sanitise to something
 * other than itself: `_sanitised_schema_name` (`handlers/schemas.py:1777-1793`)
 * collapses every run outside `[A-Za-z0-9_-]` to a single `_`, strips leading and
 * trailing `_`/`-`, and truncates at 200 — so the id a user *believed* they chose
 * and the id the object is addressed by would routinely differ.
 *
 * `CreatePipelinePage.schema-upload.test.tsx:125-127` pins exactly this pair as
 * **two examples** (one unlabelled submission). This file generalises it to any
 * label, which is the half an example set cannot cover: the hazard is
 * input-shaped.
 *
 * ## The adversarial classes, and what each one would catch
 *
 * | Class | Would catch |
 * |---|---|
 * | sanitises to something else (spaces, `/`, `.`, `%`, `#`, quotes, unicode) | an id derived from the label — the id and the label diverge silently |
 * | **no ASCII alphanumeric at all** (`----`, `...`, `日本語`, `€€`) | a leak into the *name*, which `upload_schema` answers **400** for (`LGR-44d601e-2`, template / ratified — it refuses an unnameable name rather than inventing one). A label must never be able to produce that 400 |
 * | path-like (`../../etc/passwd`, `schemas/uploaded/x-metadata.json`) | a label composing a key outside `shared/{id}.json` (`LGR-cad8032-1`, template / ratified) |
 * | very long (up to 600 chars) | truncation at `_UPLOAD_NAME_MAX_LENGTH` making the returned id a prefix of the submitted name |
 * | **looks like a schema id** (`uploaded_schema_1785844048926`) | the sneakiest leak: a label that *satisfies* `^uploaded_schema_\d+$` on its own. The regex alone would pass, which is why the digits are also checked against the injected clock window |
 *
 * ## Assertions are on the actual request bodies
 *
 * `apiClient.post` is mocked and both calls are captured, so the assertions read
 * the JSON that would have gone on the wire rather than a component's internal
 * state. `/schemas/upload` answers `{}` — **no** schema id — which is what makes
 * `target_schema` resolve through the fallback to the computed name; that is the
 * branch where a second `Date.now()` diverged pre-P29, and it is the branch the
 * pinned example test uses for the same reason.
 *
 * The label is asserted to arrive **verbatim** under `DISPLAY_LABEL_REQUEST_KEY`
 * whenever it survives the `trim()` gate. Without that half the property could
 * pass vacuously on a label that never reached the request at all — "no label in
 * the address" is only meaningful alongside "the label did travel".
 *
 * ## Mocking
 *
 * `apiClient` only — never `../../hooks`. The real `usePipelines`,
 * `useBedrockModels`, `useSchemas` and the resource hooks run against the mocked
 * client (every one of their fetchers tolerates `{}`), so the state → request
 * path under test is the production one. It also keeps clear of held-assertion
 * inventory row 5: a `vi.mock` factory **replaces** the module it targets, so any
 * export it omits reads back as `undefined` and the component calls
 * `undefined()` **during render, inside the test that renders it** — a stack
 * trace naming a value that is not a function, not an assertion diff. Amend the
 * factory before adding a hook to a component the factory's tests render.
 *
 * `DISPLAY_LABEL_REQUEST_KEY` is imported from `utils/schema-display.ts` rather
 * than transcribed (R2.8, R4.6), and that module is deliberately never mocked.
 *
 * ## One deliberate exclusion from the generators
 *
 * CR and LF are excluded. A single-line `<input>` applies the HTML value
 * sanitization algorithm, which **strips newlines** — so a label containing one
 * is not something this control can hold, and generating it would produce
 * counterexamples about the DOM rather than about the wizard. Tabs and unicode
 * spaces are *not* excluded; the DOM keeps those, and they are exactly where the
 * `trim()` gate earns its keep. Property 7 (task 5.5) owns the whitespace rule
 * itself.
 *
 * _Requirements: 2.5, 2.6_
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, act, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as fc from 'fast-check';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';
import { INITIAL_WIZARD_STATE, type WizardState } from '../wizard/types';
import { DISPLAY_LABEL_REQUEST_KEY } from '../../utils/schema-display';

const { postMock, getMock } = vi.hoisted(() => ({ postMock: vi.fn(), getMock: vi.fn() }));
vi.mock('../../services/api-client', () => ({
  apiClient: { post: postMock, get: getMock, put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import CreatePipelinePage from '../CreatePipelinePage';

/* ------------------------------------------------------------------ */
/*  The injected clock                                                 */
/* ------------------------------------------------------------------ */

/**
 * A strictly-increasing clock, reset per iteration, with a base no generated
 * label uses.
 *
 * Strictly increasing is what makes "computed exactly once" (R2.6) observable: a
 * second `Date.now()` would yield a different suffix and the two payloads would
 * diverge. The narrow window is what makes the id-lookalike class bite — a label
 * of `uploaded_schema_1785844048926` matches `^uploaded_schema_\d+$` all by
 * itself, so the regex alone cannot tell a leak from a legitimate id. The digits
 * must also be *this run's* clock.
 */
const CLOCK_BASE = 900_000_000_000;
const CLOCK_WINDOW = 1_000_000;
let tick = CLOCK_BASE;

function resetClock() {
  tick = CLOCK_BASE;
}

/* ------------------------------------------------------------------ */
/*  Generators — the five adversarial classes                          */
/* ------------------------------------------------------------------ */

/** Characters that `_UPLOAD_NAME_UNSAFE` (`[^A-Za-z0-9_-]+`) collapses to `_`. */
const UNSAFE_CHARS = [
  ' ', '\t', '.', '/', '\\', ':', '%', '#', '?', '&', '=', '+', '*', '"', "'", '`',
  '(', ')', '[', ']', '{', '}', '<', '>', '|', '!', '@', '$', '^', '~', ',', ';',
  '\u00a0', '\u2007', '\u3000', 'é', 'ü', 'ñ', 'Ø', 'ω', 'Ω', '日', '本', '語', '데', '이', '터', '€', '—',
];

const SAFE_CHARS = [...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-'];

const joined = (arb: fc.Arbitrary<string[]>) => arb.map((cs) => cs.join(''));

/**
 * Class 1 — sanitises to something other than itself: at least one unsafe
 * character mixed in with safe ones.
 */
const sanitisesDifferentlyArb: fc.Arbitrary<string> = fc
  .tuple(
    joined(fc.array(fc.constantFrom(...SAFE_CHARS), { minLength: 1, maxLength: 10 })),
    fc.constantFrom(...UNSAFE_CHARS),
    joined(fc.array(fc.constantFrom(...[...SAFE_CHARS, ...UNSAFE_CHARS]), { minLength: 0, maxLength: 12 })),
  )
  .map(([head, unsafe, tail]) => `${head}${unsafe}${tail}`);

/**
 * Class 2 — **no ASCII alphanumeric at all.** `_sanitised_schema_name` leaves
 * nothing usable, and `upload_schema` answers **400** when such a value is used
 * as a *name* (`LGR-44d601e-2`). As a *label* it must be entirely ordinary.
 */
const noAsciiAlnumArb: fc.Arbitrary<string> = joined(
  fc.array(
    fc.constantFrom(...UNSAFE_CHARS.filter((c) => !/[A-Za-z0-9]/.test(c)), '-', '_'),
    { minLength: 1, maxLength: 14 },
  ),
);

/** Class 3 — path-like, including the sidecar's own key shape. */
const pathLikeArb: fc.Arbitrary<string> = fc.oneof(
  fc.constant('../../etc/passwd'),
  fc.constant('../shared/uploaded_schema_1785844048926.json'),
  fc.constant('schemas/uploaded/uploaded_schema_1785844048926-metadata.json'),
  fc.constant('shared/uploaded_schema_1785844048926.json'),
  fc.constant('/absolute/path/customers.json'),
  fc.constant('s3://target-schemas/shared/x.json'),
  joined(
    fc.array(fc.constantFrom('..', 'shared', 'schemas', 'uploaded', 'orders', 'x.json', '/'), {
      minLength: 2,
      maxLength: 8,
    }),
  ),
);

/** Class 4 — long enough to cross `_UPLOAD_NAME_MAX_LENGTH` (200). */
const veryLongArb: fc.Arbitrary<string> = fc
  .tuple(
    fc.integer({ min: 201, max: 600 }),
    fc.constantFrom(...SAFE_CHARS, ...UNSAFE_CHARS),
  )
  .map(([n, ch]) => ch.repeat(n).slice(0, n));

/**
 * Class 5 — a label that **looks like a schema id**, i.e. one that satisfies
 * `^uploaded_schema_\d+$` on its own. Suffixes are 13-digit epoch stamps, well
 * outside the injected clock window, so a leak is still distinguishable.
 */
const looksLikeAnIdArb: fc.Arbitrary<string> = fc.oneof(
  fc
    .integer({ min: 1_700_000_000_000, max: 1_899_999_999_999 })
    .map((n) => `uploaded_schema_${n}`),
  fc
    .integer({ min: 1_700_000_000_000, max: 1_899_999_999_999 })
    .map((n) => `uploaded_schema_${n}-metadata`),
  fc.constant('uploaded_schema_'),
);

const labelArb: fc.Arbitrary<string> = fc.oneof(
  { arbitrary: sanitisesDifferentlyArb, weight: 4 },
  { arbitrary: noAsciiAlnumArb, weight: 3 },
  { arbitrary: pathLikeArb, weight: 2 },
  { arbitrary: veryLongArb, weight: 1 },
  { arbitrary: looksLikeAnIdArb, weight: 3 },
);

/* ------------------------------------------------------------------ */
/*  The rule the backend would apply to a name — mirrored as a NEGATIVE */
/* ------------------------------------------------------------------ */

/**
 * `_sanitised_schema_name`'s rule (`handlers/schemas.py:1777-1793`), mirrored
 * here **only** to assert that its image never appears as an address.
 *
 * This is not a second definition of the producer's rule — nothing in this file
 * expects it to equal any value the code produces. It exists so the property can
 * be stated in the label's own terms: neither the label nor the id the backend
 * *would* have derived from it may be the name or the `target_schema`.
 */
function sanitisedAsAName(label: string): string {
  const collapsed = label.replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^[_-]+|[_-]+$/g, '');
  return collapsed.slice(0, 200).replace(/^[_-]+|[_-]+$/g, '');
}

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

/**
 * A complete wizard state on the inline-upload branch: no existing schema id,
 * non-empty `schemaFields`, an S3 Tables target (no step-2 guard) and a model
 * chosen — so Review is five clicks away and none of the navigation guards
 * fires. `schemaDisplayLabel` is deliberately **left at its initial `''`**: the
 * label under test arrives through the rendered input, not through the draft.
 */
const seededState: WizardState = {
  ...INITIAL_WIZARD_STATE,
  pipelineName: 'inline-schema-label-pipeline',
  description: 'property 8',
  targetType: 's3_tables',
  targetConfig: { bucket_name: 'b', table_name: 't', namespace: 'default' },
  schemaSource: 'upload',
  // Required, not incidental: `WizardStepSchema`'s pre-selection effect
  // (`WizardStepSchema.tsx:411-421`) rewrites `schemaSource` to `'detected'` for
  // a non-JDBC target while this flag is false, so without it the step renders
  // `DetectedSchemaView` and the label input never mounts.
  schemaSourceExplicitlySet: true,
  schemaFields: [
    { name: 'id', type: 'string', nullable: false, description: '' },
    { name: 'amount', type: 'double', nullable: true, description: '' },
  ],
  existingSchemaId: null,
  aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
};

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
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

interface Submitted {
  /** The value the DOM actually held — see the CR/LF note in the header. */
  typed: string;
  upload: Record<string, unknown>;
  provision: Record<string, unknown>;
}

/**
 * Drive the wizard once with `label` typed into the Display_Label input, and
 * return both captured request bodies.
 */
async function submitWithLabel(label: string): Promise<Submitted> {
  resetClock();
  sessionStorage.clear();
  localStorage.clear();
  postMock.mockClear();
  sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(seededState));

  renderPage();
  fireEvent.click(screen.getByTestId('wizard-draft-restore-button'));
  clickNext(); // Name → Source
  clickNext(); // Source → Target
  clickNext(); // Target → Schema

  const input = screen.getByPlaceholderText('Display label (optional)') as HTMLInputElement;
  fireEvent.change(input, { target: { value: label } });
  const typed = input.value;

  clickNext(); // Schema → Model
  clickNext(); // Model → Review

  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
  });

  const uploadCall = postMock.mock.calls.find(([url]) => url === '/schemas/upload');
  const provisionCall = postMock.mock.calls.find(([url]) => url === '/pipelines/provision');
  expect(uploadCall).toBeDefined();
  expect(provisionCall).toBeDefined();

  return {
    typed,
    upload: uploadCall![1] as Record<string, unknown>,
    provision: provisionCall![1] as Record<string, unknown>,
  };
}

beforeEach(() => {
  getMock.mockReset();
  // Every resource fetcher in `src/hooks` unwraps `Array.isArray(body) ? body :
  // (body.x ?? [])`, so one empty envelope serves all of them.
  getMock.mockImplementation(async () => ({}));
  postMock.mockReset();
  // `/schemas/upload` returns NO schema id, so `target_schema` resolves through
  // the fallback to the name the wizard computed — the branch R2.6 is about.
  postMock.mockImplementation(async (url: string) => (url === '/schemas/upload' ? {} : { success: true }));
  resetClock();
  vi.spyOn(Date, 'now').mockImplementation(() => (tick += 1));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  sessionStorage.clear();
  localStorage.clear();
});

/* ================================================================== */
/*  Property 8                                                         */
/* ================================================================== */

// Feature: p36-schema-naming, Property 8: No Display_Label value can reach an
// address
describe('Property 8 — no Display_Label value can reach an address (R2.5, R2.6)', () => {
  it('keeps the upload name a clock-derived id and target_schema identical to it, for any label', async () => {
    await fc.assert(
      fc.asyncProperty(labelArb, async (label) => {
        cleanup();
        const { typed, upload, provision } = await submitWithLabel(label);

        const name = upload.name as string;
        const targetSchema = provision.target_schema as string;

        // R2.5 — the address shape is untouched by the label.
        expect(name).toMatch(/^uploaded_schema_\d+$/);

        // ...and its digits came from *this run's* clock, not from a label that
        // merely looks like an id. Without this, a leaked
        // `uploaded_schema_1785844048926` label would satisfy the regex above.
        const suffix = Number(name.slice('uploaded_schema_'.length));
        expect(suffix).toBeGreaterThan(CLOCK_BASE);
        expect(suffix).toBeLessThanOrEqual(CLOCK_BASE + CLOCK_WINDOW);

        // R2.6 — identical, not merely well-shaped. With a strictly-increasing
        // clock, a second computation cannot produce the same string.
        expect(targetSchema).toBe(name);

        // The adversarial half, in the label's own terms: neither the label nor
        // the id the backend *would* have derived from it is an address.
        const wouldBe = sanitisedAsAName(typed);
        expect(name).not.toBe(typed);
        expect(targetSchema).not.toBe(typed);
        if (wouldBe) {
          expect(name).not.toBe(wouldBe);
          expect(targetSchema).not.toBe(wouldBe);
        }

        // Non-vacuity: the label really did travel — verbatim, under the shared
        // key — whenever it survived the `trim()` gate. A label that never
        // reached the request would make everything above true for free.
        if (typed.trim()) {
          expect(upload[DISPLAY_LABEL_REQUEST_KEY]).toBe(typed);
        } else {
          expect(DISPLAY_LABEL_REQUEST_KEY in upload).toBe(false);
        }
      }),
      {
        numRuns: 100,
        examples: [
          // One from each adversarial class, kept as named regressions.
          ['Q3 orders / EMEA'],
          ['   '],
          ['-----'],
          ['日本語'],
          ['../../etc/passwd'],
          ['schemas/uploaded/uploaded_schema_1785844048926-metadata.json'],
          ['uploaded_schema_1785844048926'],
          ['uploaded_schema_'],
          ['x'.repeat(240)],
          ['  padded label  '],
        ],
      },
    );
  }, 240_000);
});
