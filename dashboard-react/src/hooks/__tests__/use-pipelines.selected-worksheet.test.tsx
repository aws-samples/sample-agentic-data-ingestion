// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * p55 R8.4 — the Selected_Worksheet's two hop points on the client: the read
 * normalizer in `use-pipelines.ts` and the edit body in `use-pipeline-mutations.ts`.
 *
 * ## Why these two need their own tests
 *
 * The attribute's meaning is carried by **membership**, not value. Downstream,
 * `schema_extract._configured_worksheet` tests
 * `SELECTED_WORKSHEET_ATTRIBUTE in record`, so absent and empty have opposite
 * run-time consequences: absent lets a single-sheet workbook proceed (R8.1),
 * while `''` is a recorded selection naming no sheet and refuses every run (R8.5).
 * `get_pipeline_details` mirrors that by omitting the key rather than sending
 * `''`.
 *
 * Both hops can destroy the distinction with a one-character slip:
 *
 * - the normalizer maps every other field with `?? undefined`, and `||` would
 *   look identical while rewriting `''` to `undefined` — silently turning
 *   "records an empty selection, refuses every run" into "not configured";
 * - the mutation body carries the clear intent **as** `''`, so a hook that
 *   dropped falsy fields would make clearing impossible and leave the operator
 *   back in the dead end this work exists to close.
 *
 * ## The key is imported, never transcribed
 *
 * `SELECTED_WORKSHEET_REQUEST_KEY` is the TypeScript end of a one-hop agreement
 * with `SELECTED_WORKSHEET_ATTRIBUTE` in `shared/pipeline_record_read.py`
 * (`LGR-eee8ac8-5`). A test written from a literal could not detect either side
 * drifting, and the drift would not fail — it would write an attribute nothing
 * reads (`LGR-28b4d75-5` on why the oracle must not be the producer's own
 * literal). The constant's own value is pinned in
 * `utils/__tests__/worksheet-selection.test.ts`; this file relies on that pin
 * rather than repeating it.
 *
 * ## Why the Quality_Config lives in this file too
 *
 * p64 R7.4 added `approval_config` / `quality_config` to the same normalizer, and
 * they fail the same way for the same reason — `normalizePipeline` is a whitelist
 * that builds an explicit object rather than spreading `...raw`, so a field with
 * no line is dropped silently on a response that carried it correctly. Their
 * absent-versus-recorded-empty concern is the Selected_Worksheet's argument one
 * level down: `{}` is a recorded "no overrides" and is not the same fact as an
 * absent sub-object, and inside a present `quality_config` an absent
 * `data_quality_enabled` is not `false`. Same function, same trap, so the tests
 * share a module rather than opening a second one.
 *
 * **Validates: p55 R8.3, R8.4; p64 R7.4**
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import * as React from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../../utils/worksheet-selection';
// Imported, never transcribed — `LGR-eee8ac8-5`'s reason applies identically to
// these four: they must agree with `shared/quality_verdict.py`'s constants, and a
// disagreement would not fail, it would silently read nothing.
import {
  QUALITY_CONFIG_REQUEST_KEY,
  DATA_QUALITY_ENABLED_REQUEST_KEY,
  DEDUP_ENABLED_REQUEST_KEY,
  DEDUP_COLUMNS_REQUEST_KEY,
  readDataQualityEnabled,
} from '../../utils/quality-config';

const mockGet = vi.fn();
const mockPut = vi.fn();

vi.mock('../../services/api-client', () => ({
  apiClient: {
    get: (...args: unknown[]) => mockGet(...args),
    put: (...args: unknown[]) => mockPut(...args),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));

const { usePipelineDetail, usePipelines } = await import('../use-pipelines');
const { useEditPipeline } = await import('../use-pipeline-mutations');

const PIPELINE_ID = 'worksheet-hooks-pipeline';

/** `get_pipeline_details`' success shape, trimmed to what the normalizer reads. */
function detailBody(extra: Record<string, unknown> = {}) {
  return {
    pipeline_key: PIPELINE_ID,
    pipeline_id: PIPELINE_ID,
    pipeline_name: 'Worksheet Pipeline',
    status: 'active',
    target_schema_name: 'test_schema',
    ai_model: 'anthropic.claude-3-haiku-20240307-v1:0',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
    schema_history: [],
    ...extra,
  };
}

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return React.createElement(QueryClientProvider, { client: queryClient }, children);
}

async function readDetail(body: Record<string, unknown>) {
  mockGet.mockResolvedValueOnce(body);
  const { result } = renderHook(() => usePipelineDetail(PIPELINE_ID), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  return result.current.data!;
}

beforeEach(() => {
  mockGet.mockReset();
  mockPut.mockReset();
  mockPut.mockResolvedValue({ success: true });
});

describe('normalizePipeline — the read path preserves absent vs empty (R8.1 / R8.5)', () => {
  it('carries a configured worksheet name through', async () => {
    const pipeline = await readDetail(detailBody({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' }));

    expect(pipeline[SELECTED_WORKSHEET_REQUEST_KEY]).toBe('Q3 Actuals');
  });

  it('reports an omitted key as undefined', async () => {
    const pipeline = await readDetail(detailBody());

    expect(pipeline[SELECTED_WORKSHEET_REQUEST_KEY]).toBeUndefined();
  });

  it('keeps a recorded-empty selection as the empty string, NOT as undefined', async () => {
    // The `||`-versus-`??` trap. `''` means the record carries an empty
    // selection, which refuses every run; `undefined` means no selection at all,
    // which is fine for a single-sheet workbook. Collapsing them here would hide
    // a pipeline that cannot run behind a UI that says nothing is configured.
    const pipeline = await readDetail(detailBody({ [SELECTED_WORKSHEET_REQUEST_KEY]: '' }));

    expect(pipeline[SELECTED_WORKSHEET_REQUEST_KEY]).toBe('');
    expect(pipeline[SELECTED_WORKSHEET_REQUEST_KEY]).not.toBeUndefined();
  });

  it('the two states are distinguishable after normalization', async () => {
    const absent = await readDetail(detailBody());
    const empty = await readDetail(detailBody({ [SELECTED_WORKSHEET_REQUEST_KEY]: '' }));

    expect(absent[SELECTED_WORKSHEET_REQUEST_KEY]).not.toBe(empty[SELECTED_WORKSHEET_REQUEST_KEY]);
  });

  it('does not trim padding out of a configured name', async () => {
    // A sheet may legitimately be named `" Data "`; trimming here would report a
    // correctly-configured pipeline as configured for a different sheet.
    const pipeline = await readDetail(detailBody({ [SELECTED_WORKSHEET_REQUEST_KEY]: ' Data ' }));

    expect(pipeline[SELECTED_WORKSHEET_REQUEST_KEY]).toBe(' Data ');
  });
});

describe('normalizePipeline — the governance sub-objects reach Pipeline (p64 R7.4)', () => {
  it('carries a populated quality_config through verbatim', async () => {
    const pipeline = await readDetail(
      detailBody({
        [QUALITY_CONFIG_REQUEST_KEY]: {
          [DATA_QUALITY_ENABLED_REQUEST_KEY]: false,
          [DEDUP_ENABLED_REQUEST_KEY]: true,
          [DEDUP_COLUMNS_REQUEST_KEY]: ['order_id', 'line_no'],
        },
      }),
    );

    expect(pipeline.quality_config).toEqual({
      [DATA_QUALITY_ENABLED_REQUEST_KEY]: false,
      [DEDUP_ENABLED_REQUEST_KEY]: true,
      [DEDUP_COLUMNS_REQUEST_KEY]: ['order_id', 'line_no'],
    });
  });

  it('carries approval_config through — the declaration is no longer dead', async () => {
    // `Pipeline.approval_config` was typed against a response that never sent the
    // field. `get_pipeline_details` projects it on both arms now, so this asserts
    // the declaration describes something real rather than an intention.
    const pipeline = await readDetail(
      detailBody({
        approval_config: { schema_mapping_enabled: false, code_generation_enabled: true },
      }),
    );

    expect(pipeline.approval_config).toEqual({
      schema_mapping_enabled: false,
      code_generation_enabled: true,
    });
  });

  it('reports an omitted quality_config as undefined', async () => {
    const pipeline = await readDetail(detailBody());

    expect(pipeline.quality_config).toBeUndefined();
  });

  it('keeps a recorded-empty quality_config as {}, NOT as undefined', async () => {
    // The `?? {}` trap, and the reason no default is applied here. `{}` on the
    // wire is a record that HAS the sub-object and records no overrides;
    // `undefined` is a record with no sub-object at all. The producer mirrors the
    // record's membership (`QUALITY_CONFIG_KEY in pipeline_item`, not a truth
    // test) precisely to keep them apart, so collapsing them here would throw
    // away a distinction the wire went to trouble to carry.
    const pipeline = await readDetail(detailBody({ [QUALITY_CONFIG_REQUEST_KEY]: {} }));

    expect(pipeline.quality_config).toEqual({});
    expect(pipeline.quality_config).not.toBeUndefined();
  });

  it('the absent and recorded-empty states are distinguishable after normalization', async () => {
    const absent = await readDetail(detailBody());
    const empty = await readDetail(detailBody({ [QUALITY_CONFIG_REQUEST_KEY]: {} }));

    expect(absent.quality_config).toBeUndefined();
    expect(empty.quality_config).toBeDefined();
  });

  it('does NOT seed data_quality_enabled when the sub-object omits it', async () => {
    // The load-bearing case. A record written by `_default_quality_config` carries
    // dedup keys and deliberately NO `data_quality_enabled`, meaning "use the
    // template's own default" — on for `glue_job_s3_tables.py.j2`. If this
    // normalizer invented `false` here, an edit round trip would write an explicit
    // opt-OUT: the row-level split and quarantine write that target type has had
    // since P28 would stop being generated and the pipeline's verdict would move
    // from `enforced` to `advisory`, with no error and nobody having asked.
    const pipeline = await readDetail(
      detailBody({
        [QUALITY_CONFIG_REQUEST_KEY]: {
          [DEDUP_ENABLED_REQUEST_KEY]: false,
          [DEDUP_COLUMNS_REQUEST_KEY]: [],
        },
      }),
    );

    const quality = pipeline.quality_config!;
    expect(Object.keys(quality)).not.toContain(DATA_QUALITY_ENABLED_REQUEST_KEY);
    // And the three-state reader agrees it is absent rather than off.
    expect(readDataQualityEnabled(quality[DATA_QUALITY_ENABLED_REQUEST_KEY])).toBeUndefined();
  });

  it('keeps an explicit false distinguishable from an absent flag', async () => {
    // `false` is an operator's recorded opt-out and must survive as one; the case
    // above must not. Asserted together because the defect is the two agreeing.
    const explicit = await readDetail(
      detailBody({
        [QUALITY_CONFIG_REQUEST_KEY]: { [DATA_QUALITY_ENABLED_REQUEST_KEY]: false },
      }),
    );
    const omitted = await readDetail(detailBody({ [QUALITY_CONFIG_REQUEST_KEY]: {} }));

    expect(readDataQualityEnabled(explicit.quality_config![DATA_QUALITY_ENABLED_REQUEST_KEY])).toBe(
      false,
    );
    expect(
      readDataQualityEnabled(omitted.quality_config![DATA_QUALITY_ENABLED_REQUEST_KEY]),
    ).toBeUndefined();
  });

  it('does not rewrite a dedup column list', async () => {
    // Dedup columns are interpolated into `dropDuplicates([...])` in generated
    // code. Order, case and duplicates are all part of the recorded value, so this
    // boundary must not sort, fold or de-duplicate them.
    const columns = ['Order_ID', 'order_id', 'b', 'a'];
    const pipeline = await readDetail(
      detailBody({ [QUALITY_CONFIG_REQUEST_KEY]: { [DEDUP_COLUMNS_REQUEST_KEY]: columns } }),
    );

    expect(pipeline.quality_config![DEDUP_COLUMNS_REQUEST_KEY]).toEqual(columns);
  });

  it('reaches the list arm too, not only the detail arm', async () => {
    // `fetchPipelines` and `fetchPipelineDetail` share this normalizer, so the
    // passthrough is not detail-only. Guards against a future fix applied to one
    // call site instead of the shared function.
    mockGet.mockResolvedValueOnce({
      pipelines: [detailBody({ [QUALITY_CONFIG_REQUEST_KEY]: { [DEDUP_ENABLED_REQUEST_KEY]: true } })],
      total_count: 1,
    });
    const { result } = renderHook(() => usePipelines(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data!.pipelines[0].quality_config).toEqual({
      [DEDUP_ENABLED_REQUEST_KEY]: true,
    });
  });
});

describe('useEditPipeline — the edit body expresses all three intents (R8.3)', () => {
  async function edit(params: Record<string, unknown>) {
    const { result } = renderHook(() => useEditPipeline(), { wrapper });
    await result.current.mutateAsync({ id: PIPELINE_ID, ...params } as never);
    expect(mockPut).toHaveBeenCalledTimes(1);
    return mockPut.mock.calls[0];
  }

  it('sends a name under the shared wire key', async () => {
    const [path, body] = await edit({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Q3 Actuals' });

    expect(path).toBe(`/pipelines/${PIPELINE_ID}/edit`);
    expect((body as Record<string, unknown>)[SELECTED_WORKSHEET_REQUEST_KEY]).toBe('Q3 Actuals');
  });

  it('sends the name verbatim, padding included', async () => {
    const [, body] = await edit({ [SELECTED_WORKSHEET_REQUEST_KEY]: ' Data ' });

    expect((body as Record<string, unknown>)[SELECTED_WORKSHEET_REQUEST_KEY]).toBe(' Data ');
  });

  it('sends an empty string to clear, rather than dropping the field', async () => {
    // The clear intent IS the empty string on the wire; the server REMOVEs the
    // attribute. A hook that stripped falsy fields would make clearing
    // unreachable — the same dead end, one layer up.
    const [, body] = await edit({ [SELECTED_WORKSHEET_REQUEST_KEY]: '' });

    const sent = body as Record<string, unknown>;
    expect(Object.keys(sent)).toContain(SELECTED_WORKSHEET_REQUEST_KEY);
    expect(sent[SELECTED_WORKSHEET_REQUEST_KEY]).toBe('');
  });

  it('omits the key entirely when the edit is about something else', async () => {
    // "Leave unchanged" is expressed by absence, and the server reads it that
    // way, so editing a description must not mention the worksheet at all.
    const [, body] = await edit({ description: 'a new description' });

    expect(Object.keys(body as Record<string, unknown>)).not.toContain(SELECTED_WORKSHEET_REQUEST_KEY);
  });

  it('never sends the pipeline id inside the body', async () => {
    // `id` selects the route; it is not an editable field. Guards the rest-spread.
    const [, body] = await edit({ [SELECTED_WORKSHEET_REQUEST_KEY]: 'Sheet1' });

    expect(Object.keys(body as Record<string, unknown>)).not.toContain('id');
  });
});
