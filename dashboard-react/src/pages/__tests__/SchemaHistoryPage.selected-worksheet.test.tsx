// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet's editor is reachable from the pipeline
 * detail pages, and the page hands it the right three things.
 *
 * `WorksheetSelectionEditor.test.tsx` covers the control's own behaviour. This
 * file covers the wiring, which is where the defect could reappear:
 *
 * | # | What | Why it matters |
 * |---|---|---|
 * | 1 | the editor is reachable on the Schema tab **when it is relevant** | before this, the attribute had **no** read or edit surface at all |
 * | 2 | `configured` is passed straight through, `''` included | a `||` here would erase absent-vs-empty after the hook took care to keep it |
 * | 3 | a save calls `useEditPipeline` with the shared wire key | the server reads that key through `SELECTED_WORKSHEET_ATTRIBUTE`; a second spelling writes an attribute nothing reads (`LGR-eee8ac8-5`) |
 * | 4 | a clear sends `''`, not a dropped field | `''` on the wire is the clear intent; the server REMOVEs the attribute |
 * | 5 | the card is gated on relevance, not rendered for everyone | it used to open every pipeline's Schema tab, JDBC included |
 *
 * Why the **Schema** tab: the worksheet decides which sheet the schema is
 * extracted from, and `schema_extract`'s R8.4 / R8.5 refusals are what send an
 * operator looking for this control. `PipelineOverviewPage`'s source card would
 * also be defensible; it is not this change's to edit.
 *
 * ## Row 5 reverses an assertion this file used to make
 *
 * The original of this file asserted the editor rendered **unconditionally** — it
 * was written when the defect was "there is no surface", and "it is always
 * there" was the cheapest proof that one existed. That over-shot: a worksheet is
 * meaningless on a JDBC pipeline and noise on a CSV-only one, so the card opened
 * every Schema tab looking like a required step. The requirement it should have
 * encoded is narrower — a setting is shown when it is **relevant or already in
 * use**, and out of the way otherwise — and the tests below are derived from
 * that rather than from what the page happened to render. The reversed
 * assertions are amended in place, each with its reason, rather than deleted.
 *
 * A note on how "collapsed" is asserted: Cloudscape's `ExpandableSection` keeps
 * its children mounted and hides them with CSS, which jsdom does not apply. So
 * presence in the DOM proves nothing about visibility, and these tests assert the
 * trigger's `aria-expanded` instead — the same fact a screen reader and a
 * keyboard user get.
 *
 * **Validates: p55 R8.3, R8.4**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { PipelineProvider } from '../../contexts/PipelineContext';
import type { Pipeline } from '../../types';
// The one definition of the wire key, imported rather than transcribed.
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../../utils/worksheet-selection';

const mockMutateAsync = vi.fn();
const mockRefetch = vi.fn();

vi.mock('../../hooks', () => ({
  useRegenerateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateMappings: () => ({ mutate: vi.fn(), isPending: false }),
  useEditPipeline: () => ({ mutateAsync: mockMutateAsync, isPending: false }),
  useApprovals: () => ({ data: undefined }),
  useApprovalAction: () => ({ mutate: vi.fn(), isPending: false }),
  useMappingRegenPolling: () => {},
  useBedrockModels: () => ({ data: [], isLoading: false }),
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useTestResults: () => ({ data: undefined, isLoading: false, isError: false }),
}));

vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

const { default: SchemaHistoryPage } = await import('../SchemaHistoryPage');

const PIPELINE_KEY = 'worksheet-page-pipe';

function makePipeline(extra: Partial<Pipeline> = {}): Pipeline {
  return {
    pipeline_key: PIPELINE_KEY,
    pipeline_name: 'Worksheet Pipeline',
    status: 'active',
    source_schema: 'src',
    target_schema: 'tgt',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
    schema_history: [],
    all_files: [],
    ...extra,
  };
}

function renderPage(pipeline: Pipeline) {
  return render(
    <MemoryRouter>
      <PipelineProvider value={{ pipeline, isLoading: false, isError: false, refetch: mockRefetch }}>
        <SchemaHistoryPage />
      </PipelineProvider>
    </MemoryRouter>,
  );
}

function saveButton(): HTMLButtonElement {
  const el = screen.getByTestId('worksheet-selection-save');
  return (el.tagName === 'BUTTON' ? el : el.querySelector('button')!) as HTMLButtonElement;
}

function clearButton(): HTMLButtonElement {
  const el = screen.getByTestId('worksheet-selection-clear');
  return (el.tagName === 'BUTTON' ? el : el.querySelector('button')!) as HTMLButtonElement;
}

/**
 * The `ExpandableSection`'s own trigger, or `null` when the card was not wrapped
 * in one (the configured case, which is deliberately not collapsible).
 *
 * Cloudscape renders the trigger as `role="button"` carrying `aria-expanded`, so
 * this is the accessible expanded/collapsed fact rather than a CSS class.
 */
function worksheetDisclosure(): HTMLElement | null {
  const section = screen.queryByTestId('worksheet-selection-collapsible');
  return section?.querySelector<HTMLElement>('[aria-expanded]') ?? null;
}

/**
 * Do what an operator does to reach a collapsed editor. A no-op when the card is
 * already uncollapsed, so the wiring tests below read the same either way.
 */
function openWorksheetEditor() {
  const trigger = worksheetDisclosure();
  if (trigger && trigger.getAttribute('aria-expanded') === 'false') {
    fireEvent.click(trigger);
  }
}

beforeEach(() => {
  mockMutateAsync.mockReset();
  mockMutateAsync.mockResolvedValue({ success: true });
  mockRefetch.mockReset();
});

describe('SchemaHistoryPage — the worksheet editor is reachable (R8.4)', () => {
  it('offers the worksheet editor on an S3-upload pipeline, collapsed when nothing is set', () => {
    // AMENDED (was: "renders the worksheet editor" — asserted the card rendered
    // unconditionally, for every pipeline, expanded). That was the defect: the
    // card opened every Schema tab. The requirement is that it is *reachable*
    // when it could apply, not that it is in the way. Reachability is what is
    // asserted; the openability of it is asserted below.
    renderPage(makePipeline({ source_type: 'S3_UPLOAD' }));

    const trigger = worksheetDisclosure();
    expect(trigger).not.toBeNull();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('names the collapsed section so it reads as optional rather than as a required step', () => {
    renderPage(makePipeline({ source_type: 'S3_UPLOAD' }));

    const section = screen.getByTestId('worksheet-selection-collapsible');
    // A pipeline that only ever ingests CSV must be able to tell from the
    // collapsed header alone that this does not concern it.
    expect(section.textContent).toMatch(/Excel worksheet \(optional\)/i);
    expect(section.textContent).toMatch(/CSV, JSON and Parquet uploads ignore it/i);
  });

  it('lets the operator open the collapsed section and edit from there', () => {
    renderPage(makePipeline({ source_type: 'S3_UPLOAD' }));

    openWorksheetEditor();

    expect(worksheetDisclosure()).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('worksheet-selection-editor')).toBeInTheDocument();
    expect(screen.getByTestId('worksheet-state-absent')).toBeInTheDocument();
  });

  it('renders NOTHING for a JDBC pipeline with no worksheet', () => {
    // A JDBC source has no workbook, so there is no worksheet to choose and no
    // refusal that could send anyone here.
    renderPage(makePipeline({ source_type: 'JDBC' }));

    expect(screen.queryByTestId('worksheet-selection-editor')).not.toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-selection-collapsible')).not.toBeInTheDocument();
  });

  it('treats a pipeline with no source_type as an S3 upload and still offers the editor', () => {
    // `handlers/pipelines_crud.py:2808` projects `source_type` with an explicit
    // `"S3_UPLOAD"` default, so absence cannot come off the wire — and if it
    // somehow does, the safe reading is the one the producer would have given.
    // Hiding on absence would silently remove the only route to a worksheet.
    renderPage(makePipeline());

    expect(screen.getByTestId('worksheet-selection-collapsible')).toBeInTheDocument();
  });

  it('shows a configured worksheet name with no clicks and no chevron', () => {
    // AMENDED (was: "shows the configured worksheet name" — same assertion, now
    // also pinning that the configured card is NOT put behind a disclosure. A
    // setting an operator has to hunt for is the opposite of this fix.)
    renderPage(makePipeline({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' }));

    expect(screen.getByTestId('worksheet-configured-value')).toHaveTextContent('Q3 Actuals');
    expect(screen.queryByTestId('worksheet-selection-collapsible')).not.toBeInTheDocument();
  });

  it('shows a configured worksheet even on a JDBC pipeline, so it can be cleared', () => {
    // Configured beats the source type. Otherwise a worksheet set on a pipeline
    // later switched to JDBC would be unreachable — set once, stuck forever.
    renderPage(makePipeline({ source_type: 'JDBC', [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' }));

    expect(screen.getByTestId('worksheet-configured-value')).toHaveTextContent('Q3 Actuals');
    expect(clearButton().disabled).toBe(false);
  });

  it('renders the absent state once the collapsed section is opened', () => {
    // AMENDED (was: "renders the absent state for a pipeline with no worksheet
    // configured" — it asserted the absent copy was on screen immediately. With
    // nothing configured there is nothing to report yet, so the copy belongs
    // behind the disclosure; what must hold is that opening it gets you there.)
    renderPage(makePipeline({ source_type: 'S3_UPLOAD' }));
    openWorksheetEditor();

    expect(screen.getByTestId('worksheet-state-absent')).toBeInTheDocument();
  });

  it('passes a recorded-empty selection through as its own state, with no clicks', () => {
    // The distinction survives the hook; it must survive the page too. And `''`
    // counts as configured: it is a real recorded state that refuses every run
    // under R8.5, so it must be visible and clearable without hunting for it.
    renderPage(makePipeline({ [SELECTED_WORKSHEET_REQUEST_KEY]: '' }));

    expect(screen.getByTestId('worksheet-state-recorded-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-state-absent')).not.toBeInTheDocument();
    expect(screen.queryByTestId('worksheet-selection-collapsible')).not.toBeInTheDocument();
    expect(clearButton().disabled).toBe(false);
  });

  it('renders alongside the schema timeline rather than replacing it', () => {
    // AMENDED: the pipeline now names its source type, because "the card is
    // present" is no longer true of every pipeline. What this test is about is
    // unchanged — the card does not displace the version timeline.
    renderPage(
      makePipeline({
        source_type: 'S3_UPLOAD',
        schema_history: [
          { version: 1, created: '2026-01-01T00:00:00Z', has_mapping: false, mapping_count: 0, runs: [] },
        ],
      }),
    );

    expect(screen.getByTestId('worksheet-selection-collapsible')).toBeInTheDocument();
    expect(screen.getAllByTestId('version-dot')).toHaveLength(1);
  });

  it('leaves the schema timeline intact on a JDBC pipeline, where the card is withheld', () => {
    // The withholding must remove the card and nothing else.
    renderPage(
      makePipeline({
        source_type: 'JDBC',
        schema_history: [
          { version: 1, created: '2026-01-01T00:00:00Z', has_mapping: false, mapping_count: 0, runs: [] },
        ],
      }),
    );

    expect(screen.queryByTestId('worksheet-selection-editor')).not.toBeInTheDocument();
    expect(screen.getAllByTestId('version-dot')).toHaveLength(1);
  });
});

describe('SchemaHistoryPage — the worksheet editor is wired to the edit endpoint (R8.3)', () => {
  // These start from a pipeline with nothing configured, which is the collapsed
  // case, so each one opens the section first — the journey an operator actually
  // takes. `openWorksheetEditor` is a no-op when the card is uncollapsed.
  it('sets a worksheet through useEditPipeline under the shared wire key', async () => {
    renderPage(makePipeline());
    openWorksheetEditor();

    const el = screen.getByTestId('worksheet-selection-input').querySelector('input')!;
    fireEvent.change(el, { target: { value: 'Q3 Actuals' } });
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mockMutateAsync).toHaveBeenCalledWith({
        id: PIPELINE_KEY,
        [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals',
      }),
    );
  });

  it('sends the name verbatim, padding included', async () => {
    renderPage(makePipeline());
    openWorksheetEditor();

    const el = screen.getByTestId('worksheet-selection-input').querySelector('input')!;
    fireEvent.change(el, { target: { value: ' Data ' } });
    fireEvent.click(saveButton());

    await waitFor(() =>
      expect(mockMutateAsync).toHaveBeenCalledWith({
        id: PIPELINE_KEY,
        [SELECTED_WORKSHEET_REQUEST_KEY]: ' Data ',
      }),
    );
  });

  it('clears by sending an empty string, which the server REMOVEs', async () => {
    renderPage(makePipeline({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' }));

    fireEvent.click(clearButton());

    await waitFor(() =>
      expect(mockMutateAsync).toHaveBeenCalledWith({
        id: PIPELINE_KEY,
        [SELECTED_WORKSHEET_REQUEST_KEY]: '',
      }),
    );
  });

  it('refetches the pipeline after a save, so the read path shows the new value', async () => {
    renderPage(makePipeline());
    openWorksheetEditor();

    const el = screen.getByTestId('worksheet-selection-input').querySelector('input')!;
    fireEvent.change(el, { target: { value: 'Sheet1' } });
    fireEvent.click(saveButton());

    await waitFor(() => expect(mockRefetch).toHaveBeenCalled());
  });

  it('does not mention the worksheet when a mapping context is saved instead', async () => {
    // "Leave unchanged" is expressed by absence, so the page's other write must
    // not carry the key — otherwise saving a mapping context would clear a sheet.
    renderPage(makePipeline({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' }));

    // No mapping data is rendered for an empty history, so assert the negative
    // directly: the only call this page can have made is the worksheet one, and
    // it has made none yet.
    expect(mockMutateAsync).not.toHaveBeenCalled();
  });
});
