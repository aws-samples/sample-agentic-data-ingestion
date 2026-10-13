// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { pipelineKeys } from './query-keys';
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../utils/worksheet-selection';

interface CreatePipelineParams {
  pipeline_name: string;
  target_schema: string;
  ai_model: string;
  description?: string;
  target_config?: Record<string, unknown>;
}

interface EditPipelineParams {
  id: string;
  target_schema?: string;
  ai_model?: string;
  description?: string;
  mapping_context?: string;
  target_config?: Record<string, unknown>;
  approval_config?: {
    schema_mapping_enabled?: boolean;
    code_generation_enabled?: boolean;
    always_approve_schema_changes?: boolean;
    schema_mapping_confidence_threshold?: number;
  };
  /**
   * p64 R7.4 — the Quality_Config on an existing pipeline.
   *
   * **Every key here is optional and omitting one leaves it alone.**
   * `edit_pipeline`'s branch merges **per key** into the existing map — an
   * `if_not_exists` seed then one document path per submitted key
   * (`LGR-7bea749-1`) — so a body naming only `dedup_enabled` cannot erase
   * `dedup_columns`, and cannot erase an explicit `data_quality_enabled`.
   *
   * `data_quality_enabled` is the one that has to stay omittable rather than
   * merely optional. Absent means "use the template's own default" — on for
   * `glue_job_s3_tables.py.j2`, off for the other four — so a client that helpfully
   * sends `false` for a pipeline nobody expressed an opinion about writes an
   * explicit opt-OUT, dropping the row-level split and quarantine write an S3
   * Tables pipeline has had since P28 and moving its verdict from `enforced` to
   * `advisory`. Build this object with `buildQualityConfigRequest`, which is the
   * single normalisation authority and is what decides whether the key is sent at
   * all; do not assemble it by hand.
   *
   * Note the asymmetry with the clear-by-`''` verb the Selected_Worksheet has
   * below: there is deliberately **no** way to revert `data_quality_enabled` to
   * absent through this endpoint. `null` is a 400 on both endpoints rather than a
   * second meaning (`LGR-7bea749-2`), and a revert verb waits on the UI offering
   * an explicit "use the template default" control.
   */
  quality_config?: {
    data_quality_enabled?: boolean;
    dedup_enabled?: boolean;
    dedup_columns?: string[];
  };
  /**
   * p55 R8.4 — set, change or clear the Selected_Worksheet on an existing
   * pipeline. This is the half that closes the dead end: before it, a pipeline
   * created without a worksheet could only be fixed by deleting and recreating
   * it, while the R8.4 refusal told the operator to set one.
   *
   * Three intents, and `edit_pipeline` keeps them three:
   *
   * - **omit the key** → leave the worksheet exactly as it is. Every other field
   *   here works that way, so an edit that only changes the description cannot
   *   clear a configured worksheet.
   * - **a non-blank string** → set it, **verbatim**. Do not trim before sending:
   *   a sheet may legitimately be named `" Data "` and trimming would make it
   *   permanently unselectable. `worksheetSelectionIsSet` trims for the
   *   *question* "is one set"; the value itself does not.
   * - **`''`** → clear it, which REMOVES the attribute rather than storing `''`.
   *   A stored `''` is a recorded selection naming no sheet and refuses every
   *   run (R8.5), so the two must not be conflated (`LGR-44d601e-1`).
   *
   * Keyed off the shared constant, never a literal — the server reads this body
   * key through `SELECTED_WORKSHEET_ATTRIBUTE` and a disagreement would write an
   * attribute nothing reads (`LGR-eee8ac8-5`).
   */
  [SELECTED_WORKSHEET_REQUEST_KEY]?: string;
}

async function createPipeline(params: CreatePipelineParams): Promise<unknown> {
  return apiClient.post<unknown>('/pipelines/provision', params);
}

async function editPipeline(params: EditPipelineParams): Promise<unknown> {
  const { id, ...body } = params;
  return apiClient.put<unknown>(`/pipelines/${encodeURIComponent(id)}/edit`, body);
}

export function useCreatePipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createPipeline,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
    },
  });
}

export function useEditPipeline() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: editPipeline,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: pipelineKeys.detail(variables.id) });
      queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
    },
  });
}
