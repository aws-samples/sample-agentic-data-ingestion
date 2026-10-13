// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — what the Create Pipeline wizard puts on the wire for the
 * Selected_Worksheet.
 *
 * ## Why the assertions are on the request body and not on wizard state
 *
 * A state field that never reaches `POST /pipelines/provision` would satisfy R8.4
 * in name only. The control's own behaviour is covered by
 * `wizard/__tests__/WorksheetSelectionField.test.tsx`; this file asserts the one
 * thing that decides whether a run behaves correctly.
 *
 * ## The omit-versus-empty split is the whole point
 *
 * `schema_extract._configured_worksheet` reads the pipeline record with a
 * **membership test**, so the two shapes mean opposite things:
 *
 * - key **absent** → no human has chosen. A single-sheet workbook proceeds (R8.1);
 *   a multi-sheet one refuses and names every Worksheet present (R8.4 residual).
 * - key **present**, *including* `''` → a selection naming no sheet, which refuses
 *   under R8.5 rather than falling back to the workbook's active sheet.
 *
 * So sending `''` for "the user chose nothing" would break **every single-sheet
 * Excel pipeline** created through this wizard. That failure is invisible from the
 * frontend, which is why it is pinned here.
 *
 * ## The key is read, not transcribed
 *
 * The expected key comes from `SELECTED_WORKSHEET_REQUEST_KEY`
 * (`utils/worksheet-selection.ts`), never from the literal `'selected_worksheet'`
 * — R2.8's rule from p36 and `LGR-28b4d75-5` (**template / ratified**): a test
 * written from the producer's own literal cannot detect the producer drifting.
 * That util is deliberately never mocked; it is part of what is under test.
 *
 * Harness (draft-seed-and-restore, `Providers`, the `api-client` mock) is lifted
 * from `CreatePipelinePage.display-label.test.tsx`.
 */
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';
import { WIZARD_DRAFT_KEY } from '../wizard/useWizardDraft';
import { INITIAL_WIZARD_STATE, type WizardState } from '../wizard/types';
// The oracle. Imported from a pure util no factory replaces.
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../../utils/worksheet-selection';

const getMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
vi.mock('../../services/api-client', () => ({
  apiClient: { get: getMock, post: postMock, put: vi.fn(), delete: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import CreatePipelinePage from '../CreatePipelinePage';

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
 * A complete S3-upload wizard state with an EXISTING schema selected, so
 * `handleSubmit` makes exactly one POST (`/pipelines/provision`) and no inline
 * schema upload. No step guard fires: the target is S3 Tables and a model is
 * chosen.
 */
function draftState(overrides: Partial<WizardState> = {}): WizardState {
  return {
    ...INITIAL_WIZARD_STATE,
    pipelineName: 'worksheet-pipeline',
    description: 'test',
    sourceType: 's3_upload',
    targetType: 's3_tables',
    targetConfig: { bucket_name: 'b', table_name: 't', namespace: 'default' },
    schemaSource: 'existing',
    schemaSourceExplicitlySet: true,
    existingSchemaId: 'glue_db_table',
    schemaFields: [{ name: 'id', type: 'string', nullable: false, description: '' }],
    aiModel: 'anthropic.claude-3-haiku-20240307-v1:0',
    ...overrides,
  };
}

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

function callsTo(url: string) {
  return postMock.mock.calls.filter(([called]) => called === url);
}

/** Drive Name → Source → Target → Schema → Model → Review, then submit. */
async function submitFrom(state: WizardState): Promise<Record<string, unknown>> {
  renderWizardFrom(state);
  clickNext(); // Name → Source
  clickNext(); // Source → Target
  clickNext(); // Target → Schema
  clickNext(); // Schema → Model
  clickNext(); // Model → Review
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /create pipeline/i }));
  });
  await waitFor(() => expect(callsTo('/pipelines/provision').length).toBe(1));
  return callsTo('/pipelines/provision')[0][1] as Record<string, unknown>;
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  getMock.mockReset();
  postMock.mockReset();
  getMock.mockImplementation(async (path: string) => {
    if (path === '/schemas') return { schemas: [], sourcesFailed: [] };
    if (path.startsWith('/pipelines')) return { pipelines: [] };
    return {};
  });
  postMock.mockResolvedValue({ success: true });
});

describe('POST /pipelines/provision — the Selected_Worksheet key', () => {
  it('omits the key entirely when no worksheet was chosen (R8.1)', async () => {
    const body = await submitFrom(draftState({ selectedWorksheet: '' }));
    // `not.toContain` on the key list, not a falsy-value check: `{key: ''}` and
    // no key at all are the two shapes that must not be confused, and a truthiness
    // assertion would pass for both.
    expect(Object.keys(body)).not.toContain(SELECTED_WORKSHEET_REQUEST_KEY);
  });

  it('omits the key for a whitespace-only entry, which is a user who typed nothing', async () => {
    const body = await submitFrom(draftState({ selectedWorksheet: '   ' }));
    expect(Object.keys(body)).not.toContain(SELECTED_WORKSHEET_REQUEST_KEY);
  });

  it('sends the chosen worksheet under the shared key (R8.4)', async () => {
    const body = await submitFrom(draftState({ selectedWorksheet: 'Q3 Actuals' }));
    expect(body[SELECTED_WORKSHEET_REQUEST_KEY]).toBe('Q3 Actuals');
  });

  it('sends a padded name verbatim — the gate trims, the value does not', async () => {
    // A `.trim()` slipped into the forwarding would pass a trimmed-equality
    // check, so the assertion is on the exact string. An Excel worksheet may
    // legitimately be named with padding, and trimming it here would make that
    // sheet permanently unselectable while looking correct.
    const body = await submitFrom(draftState({ selectedWorksheet: ' Data ' }));
    expect(body[SELECTED_WORKSHEET_REQUEST_KEY]).toBe(' Data ');
  });

  // A draft saved before this field existed restores `selectedWorksheet` as
  // `undefined`, because `readDraft` is a bare `JSON.parse(raw) as T` and
  // `handleRestoreDraft` replaces the state rather than merging into it. Submit
  // must not throw, and must not put `undefined` on the wire.
  it('survives a draft saved before the field existed', async () => {
    const legacy = draftState();
    delete (legacy as Partial<WizardState>).selectedWorksheet;
    const body = await submitFrom(legacy as WizardState);
    expect(Object.keys(body)).not.toContain(SELECTED_WORKSHEET_REQUEST_KEY);
  });
});

/**
 * The Source-step guard. Its purpose is that a name **no workbook could match**
 * is never persisted: a recorded selection Excel cannot even name would refuse
 * every run of the pipeline forever, which is a key nothing honours
 * (`LGR-eee8ac8-6`).
 *
 * It is a wizard guard and deliberately NOT a new 400 in `create_pipeline` —
 * that handler's permissive posture is governed by `LGR-cad8032-2`, whose blast
 * radius is nine test modules asserting 201.
 */
describe('the Source step blocks a name Excel could not accept', () => {
  it('refuses to advance past the Source step, and advances once corrected', () => {
    renderWizardFrom(draftState({ selectedWorksheet: 'Q3/Q4' }));
    clickNext(); // Name → Source
    // The control is visible, which is how we know we are on the Source step.
    const input = screen.getByLabelText(
      'Name of the Excel worksheet to ingest (optional)',
    ) as HTMLInputElement;
    expect(input).toBeInTheDocument();

    clickNext(); // blocked — still on the Source step
    expect(
      screen.getByLabelText('Name of the Excel worksheet to ingest (optional)'),
    ).toBeInTheDocument();

    fireEvent.change(input, { target: { value: 'Q3' } });
    clickNext(); // now allowed
    expect(
      screen.queryByLabelText('Name of the Excel worksheet to ingest (optional)'),
    ).not.toBeInTheDocument();
  });

  it('does not block a JDBC source, whose step renders no worksheet control', () => {
    // A restored draft can pair a stale worksheet name with a JDBC source. The
    // guard must not strand the user on a step with no field to correct.
    renderWizardFrom(
      draftState({
        sourceType: 'jdbc',
        selectedWorksheet: 'Q3/Q4',
        schedule: 'rate(1 day)',
      }),
    );
    clickNext(); // Name → Source
    expect(
      screen.queryByLabelText('Name of the Excel worksheet to ingest (optional)'),
    ).not.toBeInTheDocument();
    clickNext(); // Source → Target, not blocked
    // The Target step's own controls are present, so navigation succeeded.
    expect(screen.queryByTestId('jdbc-source-tiles')).not.toBeInTheDocument();
  });
});
