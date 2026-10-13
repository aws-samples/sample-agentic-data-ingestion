// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p36-schema-naming task 5.8 — the wizard's Display_Label control, its one
 * success-that-is-also-a-complaint, and the shared request key.
 *
 * Three examples rather than properties: each is one interaction at one state
 * with no input space to quantify over. The generated halves live next door —
 * tasks 5.5–5.7 own the trim/verbatim partition, the addressing invariance and
 * the basename default.
 *
 * | # | Criterion | What it pins |
 * |---|---|---|
 * | 1 | R2.1 | the label input exists and is **optional** — the step still completes with it empty (R2.3) |
 * | 2 | R2.7 | a `200` carrying `displayLabelError` warns **and still provisions** |
 * | 3 | R2.8 / R4.6 | both upload surfaces put the label under `DISPLAY_LABEL_REQUEST_KEY` |
 *
 * ## Test 2 is the one that matters
 *
 * `upload_schema` answers **200** with `displayLabelError` when the schema body
 * was written and only the Context_Sidecar write failed
 * (`handlers/schemas.py`, R2.7). The wizard must warn and **continue**: throwing
 * there would convert an unsaved cosmetic label into a **lost pipeline**, and the
 * retry would upload a second object for a schema that is already stored and
 * addressable. So both halves are asserted on the same interaction — the warning
 * and the `POST /pipelines/provision` — because either one alone is satisfied by
 * the failure mode the criterion exists to forbid.
 *
 * ## Test 3 reads the key rather than transcribing it
 *
 * The two upload paths are **different HTTP clients**: the Schemas page goes
 * `useUploadSchema` → `uploadSchema`, which unpacks a `FormData` and posts JSON
 * (`use-schemas.ts:129-133`); the wizard posts JSON directly from
 * `handleSubmit`. Two string literals that happen to match satisfy "the same
 * key" only *today*, which is why the expectation comes from
 * `DISPLAY_LABEL_REQUEST_KEY` (`utils/schema-display.ts`) — R4.7,
 * `LGR-28b4d75-5` (**template / ratified**): a test written from the producer's
 * output cannot detect the producer drifting.
 *
 * ## Mocking
 *
 * `apiClient` only — never `../../hooks`. A `vi.mock` factory **replaces** the
 * module, so any export it omits reads back as `undefined` and the component
 * calls `undefined()` **during render, inside the test that renders it**: the
 * observable output is a stack trace naming a value that is not a function
 * rather than an assertion diff. (Held-assertion inventory row 5, whose wording
 * item 3 of this same spec is correcting. Amend the factory before adding a hook
 * to a component the factory's tests render.) Mocking the boundary instead keeps
 * the real `useUploadSchema`, `uploadSchema` and its `FormData` → JSON
 * conversion in the code under test, which is the whole subject of test 3.
 *
 * `utils/schema-display.ts` is deliberately **not** mocked: the key constant is
 * the oracle, and a mocked module would hand back `undefined`.
 *
 * Requirements: 2.1, 2.7, 2.8, 4.6
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, fireEvent, act, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';
import { INITIAL_WIZARD_STATE, type WizardState } from '../wizard/types';
// The oracle for test 3. Imported from a pure util no factory replaces.
import { DISPLAY_LABEL_REQUEST_KEY } from '../../utils/schema-display';

const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock, post: postMock, put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import CreatePipelinePage from '../CreatePipelinePage';
import SchemaManagementPage from '../SchemaManagementPage';

// jsdom's File doesn't implement .text(), which `uploadSchema` calls — the same
// polyfill `SchemaManagementPage.upload-filename.test.tsx` installs.
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

/* ------------------------------------------------------------------ */
/*  Harness                                                            */
/* ------------------------------------------------------------------ */

function Providers({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <MemoryRouter initialEntries={['/pipelines/new']}>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

/**
 * A complete wizard state on the inline-upload branch: no existing schema id,
 * non-empty `schemaFields`, an S3 Tables target (no step-2 guard) and a model
 * chosen, so the only thing under test is the label.
 *
 * `schemaSourceExplicitlySet: true` keeps the step-3 pre-selection effect from
 * flipping `schemaSource` back to `'detected'` and unmounting the upload view.
 */
function draftState(overrides: Partial<WizardState> = {}): WizardState {
  return {
    ...INITIAL_WIZARD_STATE,
    pipelineName: 'display-label-pipeline',
    description: 'test',
    targetType: 's3_tables',
    targetConfig: { bucket_name: 'b', table_name: 't', namespace: 'default' },
    schemaSource: 'upload',
    schemaSourceExplicitlySet: true,
    schemaFields: [
      { name: 'id', type: 'string', nullable: false, description: '' },
      { name: 'amount', type: 'double', nullable: true, description: '' },
    ],
    existingSchemaId: null,
    aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
    ...overrides,
  };
}

/**
 * Seeds a draft and restores it, which rehydrates a mid-wizard state without
 * hand-driving four steps of UI — the same shortcut
 * `CreatePipelinePage.schema-upload.test.tsx` takes.
 */
function renderWizardFrom(state: WizardState) {
  sessionStorage.setItem(WIZARD_DRAFT_KEY, JSON.stringify(state));
  render(
    <Providers>
      <CreatePipelinePage />
    </Providers>,
  );
  fireEvent.click(screen.getByTestId('wizard-draft-restore-button'));
}

function clickNext() {
  fireEvent.click(screen.getByRole('button', { name: /next/i }));
}

/** True while the Target Schema step is the active step. */
function onSchemaStep(): boolean {
  return screen.queryByTestId('schema-source-tiles') !== null;
}

/** Name → Source → Target → Schema. */
function walkToSchemaStep() {
  clickNext();
  clickNext();
  clickNext();
}

/** The `<input>` inside the wizard's Display_Label control. */
function labelInput(): HTMLInputElement {
  return screen.getByLabelText(
    'Display label for the uploaded schema (optional)',
  ) as HTMLInputElement;
}

function callsTo(url: string) {
  return postMock.mock.calls.filter(([called]) => called === url);
}

/** The body of the one `POST /schemas/upload`, once it has been made. */
async function uploadedBody(): Promise<Record<string, unknown>> {
  await waitFor(() => expect(callsTo('/schemas/upload').length).toBe(1));
  return callsTo('/schemas/upload')[0][1] as Record<string, unknown>;
}

/** Drives the wizard from Review through submit. */
async function submitWizard() {
  clickNext(); // Schema → Model
  clickNext(); // Model → Review
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
  });
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  getMock.mockReset();
  postMock.mockReset();
  // Every listing the wizard's other steps ask for resolves empty; each sibling
  // fetcher normalizes `{}` to an empty list.
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return { schemas: [], sourcesFailed: [] };
    if (path.startsWith('/pipelines')) return { pipelines: [] };
    return {};
  });
  postMock.mockResolvedValue({ success: true });
});

/* ================================================================== */
/*  R2.1 — the control exists, and it is optional                      */
/* ================================================================== */

describe('Wizard_Upload_View — R2.1, an optional Display_Label input', () => {
  /**
   * Both halves of "optional" on one interaction. The control's presence alone
   * would be satisfied by a `required` field, and R2.3 makes a schema with no
   * Display_Label **fully usable** — so the step has to stay completable with
   * the input empty. That advance is the observable form of R2.3 on this
   * surface.
   */
  it('renders the label input and still lets the schema step complete while it is empty', () => {
    renderWizardFrom(draftState({ schemaDisplayLabel: '' }));
    walkToSchemaStep();

    expect(onSchemaStep()).toBe(true);
    // The control the design names, present on the upload view.
    expect(screen.getByTestId('wizard-display-label')).toBeInTheDocument();

    const input = labelInput();
    expect(input.value).toBe('');
    // Optional means optional at the control, not just in the copy.
    expect(input.hasAttribute('required')).toBe(false);
    expect(input.getAttribute('aria-required')).not.toBe('true');

    clickNext();

    expect(
      onSchemaStep(),
      'R2.1 makes the Display_Label optional and R2.3 makes a schema with no ' +
        'Display_Label fully usable, so an empty label must not block the schema step',
    ).toBe(false);
  });
});

/* ================================================================== */
/*  R2.7 — a success that is also a complaint                          */
/* ================================================================== */

describe('Wizard_Inline_Upload — R2.7, a 200 carrying displayLabelError', () => {
  /**
   * The label did not store; the schema did. Warn, and **continue**.
   *
   * Failing the pipeline creation here would convert an unsaved cosmetic label
   * into a lost pipeline, and the user's retry would produce a second object for
   * a schema that is already stored and addressable. The label itself is
   * recoverable through Rename on the Schemas page, which writes the same key.
   */
  it('warns that the label was not saved and still provisions the pipeline', async () => {
    // `POST /pipelines/provision` is left **in flight** rather than resolved, for
    // one reason worth stating: `ToastProvider` passes `stackItems` to `Flashbar`
    // as soon as a second toast exists (`ToastContext.tsx:57`), and a stacked
    // Flashbar renders only the newest item — so letting the success toast land
    // would collapse the warning out of the DOM and make this assertion about
    // Cloudscape's stacking rather than about R2.7. Holding the provision open
    // also asserts the ordering the criterion actually specifies: the warning is
    // shown, and the pipeline creation is already under way.
    let releaseProvision: (() => void) | undefined;
    postMock.mockImplementation((url: string) => {
      if (url === '/schemas/upload') {
        return Promise.resolve({
          message: 'Schema uploaded successfully',
          schemaId: 'uploaded_schema_1785844049000',
          location: 's3://bucket/shared/uploaded_schema_1785844049000.json',
          // Arrives on a **200** — the body was written, the sidecar was not.
          displayLabelError: 'AccessDenied on the sidecar',
        });
      }
      return new Promise((resolve) => {
        releaseProvision = () => resolve({ success: true });
      });
    });

    renderWizardFrom(draftState({ schemaDisplayLabel: 'Customer master' }));
    walkToSchemaStep();
    await submitWizard();

    // Half one: the user is told the label did not store.
    await waitFor(() =>
      expect(screen.getByText(/display label was not saved/)).toBeInTheDocument(),
    );
    // ...and not as an upload or pipeline failure.
    expect(screen.queryByText(/failed to create pipeline/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be uploaded/i)).not.toBeInTheDocument();

    // Half two — the one that turns a cosmetic shortfall into a lost pipeline if
    // it regresses.
    const provision = callsTo('/pipelines/provision');
    expect(
      provision.length,
      'R2.7 — a displayLabelError arrives on a 200; the wizard must continue ' +
        'creating the pipeline rather than throwing',
    ).toBe(1);
    // And it provisions against the schema that *was* stored, so the label
    // failure has not touched addressing (D1, R2.5).
    expect((provision[0][1] as { target_schema: string }).target_schema).toBe(
      'uploaded_schema_1785844049000',
    );

    // Let the pipeline creation finish inside `act`, so the success toast and
    // the post-submit state updates do not land after the test.
    await act(async () => {
      releaseProvision?.();
    });
  });
});

/* ================================================================== */
/*  R2.8 / R4.6 — one key, two clients                                 */
/* ================================================================== */

describe('Schema_Upload — R2.8, both upload surfaces use the same request key', () => {
  const LABEL = 'Customer master';

  /**
   * Which key **carries the label**, read off the recorded request body rather
   * than assumed. If either client renamed its key, its set stops matching
   * `DISPLAY_LABEL_REQUEST_KEY` and the two sets diverge from each other.
   */
  function keysCarrying(body: Record<string, unknown>, label: string): string[] {
    return Object.keys(body).filter((k) => body[k] === label);
  }

  /** The wizard path: JSON posted directly from `handleSubmit`. */
  async function wizardUploadBody(): Promise<Record<string, unknown>> {
    renderWizardFrom(draftState({ schemaDisplayLabel: LABEL }));
    walkToSchemaStep();
    await submitWizard();
    return uploadedBody();
  }

  /**
   * The Schemas page path: `useUploadSchema` → `uploadSchema`, which unpacks a
   * `FormData` and posts JSON. Driven through the page's plain "Upload schema"
   * input — the hidden one in the table header's actions — so the real hook,
   * fetcher and conversion all run.
   */
  async function schemasPageUploadBody(): Promise<Record<string, unknown>> {
    render(
      <Providers>
        <SchemaManagementPage />
      </Providers>,
    );
    await screen.findByText('Schema Management');

    fireEvent.change(screen.getByTestId('upload-display-label').querySelector('input')!, {
      target: { value: LABEL },
    });

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['{"properties":{"id":{"type":"string"}}}'], 'orders.json', {
      type: 'application/json',
    });
    Object.defineProperty(fileInput, 'files', { value: [file], configurable: true });
    await act(async () => {
      fireEvent.change(fileInput);
    });

    return uploadedBody();
  }

  it('posts the label under DISPLAY_LABEL_REQUEST_KEY from the wizard and from the Schemas page', async () => {
    const fromWizard = await wizardUploadBody();
    // Unmount the wizard before the second surface renders, so the page's own
    // hidden file input is the only one in the document.
    cleanup();
    sessionStorage.clear();
    postMock.mockReset();
    postMock.mockResolvedValue({
      message: 'Schema uploaded successfully',
      schemaId: 'orders_1785844049000',
      location: 's3://bucket/shared/orders_1785844049000.json',
    });
    const fromSchemasPage = await schemasPageUploadBody();

    const wizardKeys = keysCarrying(fromWizard, LABEL);
    const pageKeys = keysCarrying(fromSchemasPage, LABEL);

    expect(
      wizardKeys,
      `R2.8 — the wizard must send the Display_Label under ` +
        `"${DISPLAY_LABEL_REQUEST_KEY}"; body keys: ${JSON.stringify(Object.keys(fromWizard))}`,
    ).toEqual([DISPLAY_LABEL_REQUEST_KEY]);
    expect(
      pageKeys,
      `R2.8 — the Schemas page must send it under the same key; body keys: ` +
        JSON.stringify(Object.keys(fromSchemasPage)),
    ).toEqual([DISPLAY_LABEL_REQUEST_KEY]);
    // Stated as a relation as well, because "the same key in both cases" is the
    // criterion and two independent literal checks satisfy it only today.
    expect(wizardKeys).toEqual(pageKeys);
  });
});
