// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { pipelineKeys, statsKeys } from './query-keys';
import type { PaginatedPipelines, Pipeline, PipelineFilters, DashboardStats, SchemaVersion } from '../types';
import { SELECTED_WORKSHEET_REQUEST_KEY } from '../utils/worksheet-selection';

/**
 * Normalize a raw pipeline object from the API into our Pipeline type.
 * The API uses slightly different field names and shapes.
 */
function normalizePipeline(raw: Record<string, unknown>): Pipeline {
  // Normalize schema_history: API returns { hash, timestamp, extracted_schema, file_name, version }
  // Our type expects { version, created, has_mapping, mapping_count, ai_model_used, schema_hash }
  const rawHistory = (raw.schema_history as Array<Record<string, unknown>>) ?? [];
  const schemaHistory: SchemaVersion[] = rawHistory.map((h) => ({
    version: (h.version as number) ?? 0,
    created: (h.created as string) ?? (h.timestamp as string) ?? '',
    has_mapping: (h.has_mapping as boolean) ?? false,
    mapping_count: (h.mapping_count as number) ?? 0,
    ai_model_used: (h.ai_model_used as string) ?? (raw.ai_model as string) ?? undefined,
    schema_hash: (h.hash as string) ?? (h.schema_hash as string) ?? undefined,
    // Preserve embedded mapping data from the API
    mapping_details: h.mapping_details as Record<string, unknown> | undefined,
    mapping_confidence: h.mapping_confidence as number | undefined,
    unmapped_source_count: h.unmapped_source_count as number | undefined,
    unmapped_target_count: h.unmapped_target_count as number | undefined,
    extracted_schema: h.extracted_schema as string | undefined,
    file_name: h.file_name as string | undefined,
    run_id: h.run_id as string | undefined,
    // Normalize runs array from API (newest first)
    runs: Array.isArray(h.runs)
      ? (h.runs as Array<Record<string, unknown>>).map((r) => ({
          run_id: (r.run_id as string) ?? '',
          timestamp: (r.timestamp as string) ?? '',
          file_name: (r.file_name as string) ?? undefined,
          code_version_id: (r.code_version_id as string) ?? undefined,
          test_status: (r.test_status as string) ?? undefined,
          ai_model_used: (r.ai_model_used as string) ?? undefined,
        }))
      : h.run_id
        ? [{ run_id: h.run_id as string, timestamp: (h.created as string) ?? '' }]
        : [],
  }));

  // Derive source schema from the latest extracted_schema in history
  let sourceSchema = (raw.source_schema as string) ?? '';
  if (!sourceSchema && rawHistory.length > 0) {
    const latest = rawHistory[rawHistory.length - 1];
    const extracted = latest.extracted_schema as string | undefined;
    if (extracted) {
      try {
        const parsed = JSON.parse(extracted);
        const props = parsed?.items?.properties ?? parsed?.properties;
        if (props) {
          sourceSchema = Object.keys(props).join(', ');
        }
      } catch {
        sourceSchema = latest.file_name as string ?? '';
      }
    }
  }

  // updated_at fallback: use latest schema_history timestamp or created_at
  let updatedAt = (raw.updated_at as string) ?? '';
  if (!updatedAt && rawHistory.length > 0) {
    updatedAt = (rawHistory[rawHistory.length - 1].timestamp as string) ?? '';
  }
  if (!updatedAt) {
    updatedAt = (raw.created_at as string) ?? '';
  }

  return {
    pipeline_key: (raw.pipeline_key as string) ?? (raw.pipeline_id as string) ?? '',
    pipeline_name: (raw.pipeline_name as string) ?? '',
    status: (raw.status as string) ?? '',
    source_schema: sourceSchema,
    target_schema: (raw.target_schema as string) ?? (raw.target_schema_name as string) ?? '',
    created_at: (raw.created_at as string) ?? '',
    updated_at: updatedAt,
    model_id: (raw.ai_model as string) ?? undefined,
    ai_model: (raw.ai_model as string) ?? undefined,
    file_count: (raw.file_count as number) ?? 0,
    all_files: (raw.all_files as Pipeline['all_files']) ?? [],
    schema_version: (raw.schema_version as number) ?? schemaHistory.length,
    schema_history: schemaHistory,
    pipeline_description: (raw.pipeline_description as string) ?? undefined,
    target_config: (raw.target_config as Pipeline['target_config']) ?? undefined,
    target_type: (raw.target_type as string) ?? undefined,
    // P17 — source abstraction. Pass through source_type/source_config so the
    // Overview "Source Configuration" card renders the real source (JDBC vs
    // S3). Left undefined for pre-P17 records so SourceConfigDisplay applies
    // its own S3_UPLOAD default rather than this normalizer forcing it.
    source_type: (raw.source_type as Pipeline['source_type']) ?? undefined,
    source_config: (raw.source_config as Pipeline['source_config']) ?? undefined,
    schedule: (raw.schedule as string) ?? undefined,
    versions: (raw.versions as Pipeline['versions']) ?? undefined,
    mapping_context: (raw.mapping_context as string) ?? undefined,
    mapping_id: (raw.mapping_id as string) ?? undefined,
    mapping_count: (raw.mapping_count as number) ?? undefined,
    // p64 R7.4 — the two governance sub-objects, passed through **verbatim and
    // whole**. `get_pipeline_details` projects both on both response arms
    // (`pipelines_crud.py:3296-3303` and `:3565-3568`, each an `in` membership
    // test), and this normalizer is a whitelist — it builds an explicit object
    // and does not spread `...raw` — so until these two lines existed both
    // fields arrived on the wire and were dropped one layer above it.
    //
    // `?? undefined` and NOT `?? {}`, and no per-key handling of either object,
    // because there are **three** distinguishable states here and a default
    // would destroy two of them:
    //
    //   - key absent          → `undefined`. No sub-object on the record.
    //   - key present as `{}` → `{}`. A recorded "no overrides", which is NOT
    //                           the same fact as absent. `{}` is not nullish,
    //                           so `??` leaves it intact; `||` or a `?? {}`
    //                           default would fuse it with the absent case.
    //   - key present, keys set → passed through untouched.
    //
    // The same argument the Selected_Worksheet makes below, one level down: for
    // `quality_config` it applies **inside** the object too. An absent
    // `data_quality_enabled` within a present `quality_config` means "use the
    // template's own default" — on for `glue_job_s3_tables.py.j2`, off for the
    // other four — and `utils/quality-config.ts`'s `readDataQualityEnabled` is a
    // three-state reader that distinguishes absent from `false` precisely so a
    // re-submit cannot turn a template default into an explicit opt-OUT and move
    // the pipeline's verdict from `enforced` to `advisory`. So nothing here reads,
    // seeds or rewrites a member: the object crosses this boundary as it arrived.
    approval_config: (raw.approval_config as Pipeline['approval_config']) ?? undefined,
    quality_config: (raw.quality_config as Pipeline['quality_config']) ?? undefined,
    // p55 R8.4 — the Selected_Worksheet. `??`, deliberately, and NOT `||`:
    // nullish coalescing leaves `''` intact where `||` would rewrite it to
    // `undefined`, and on this one field that rewrite would erase the wire's
    // absent-versus-empty distinction (see `Pipeline.selected_worksheet`). The
    // producer omits the key rather than sending `''` for "not configured", so
    // `undefined` here means absent and `''` means recorded-and-empty.
    [SELECTED_WORKSHEET_REQUEST_KEY]:
      (raw[SELECTED_WORKSHEET_REQUEST_KEY] as string) ?? undefined,
  };
}

async function fetchPipelines(filters: PipelineFilters = {}): Promise<PaginatedPipelines> {
  const params = new URLSearchParams();
  if (filters.page) params.set('page', String(filters.page));
  if (filters.pageSize) params.set('page_size', String(filters.pageSize));
  if (filters.search) params.set('search', filters.search);
  if (filters.status) params.set('status', filters.status);

  const qs = params.toString();
  const body = await apiClient.get<Record<string, unknown>>(`/pipelines${qs ? `?${qs}` : ''}`);

  // Normalize: API may return { pipelines: [...] } or plain array
  const rawPipelines: unknown[] = Array.isArray(body)
    ? body
    : (body?.pipelines && Array.isArray(body.pipelines) ? body.pipelines : []);

  const pipelines = rawPipelines.map((p) => normalizePipeline(p as Record<string, unknown>));

  return {
    pipelines,
    total_count: (body?.total_count as number) ?? pipelines.length,
    page: (body?.page as number) ?? 1,
    page_size: (body?.page_size as number) ?? pipelines.length,
    total_pages: (body?.total_pages as number) ?? 1,
    has_more: (body?.has_more as boolean) ?? false,
  };
}

async function fetchPipelineDetail(id: string): Promise<Pipeline> {
  const body = await apiClient.get<Record<string, unknown>>(`/pipeline/${encodeURIComponent(id)}`);
  return normalizePipeline(body as Record<string, unknown>);
}

async function fetchStats(): Promise<DashboardStats> {
  const body = await apiClient.get<Record<string, unknown>>('/stats');

  // Normalize: API field names differ from our DashboardStats type
  const raw = body as Record<string, unknown>;
  return {
    total_pipelines: (raw.active_pipelines as number) ?? (raw.total_pipelines as number) ?? 0,
    total_files: (raw.total_files as number) ?? (raw.total_files_processed as number) ?? 0,
    schema_versions: (raw.schema_versions as number) ?? (raw.total_mappings as number) ?? 0,
    last_activity: (raw.last_activity as string) ?? (raw.last_updated as string) ?? '',
  };
}

export function usePipelines(filters: PipelineFilters = {}) {
  return useQuery({
    queryKey: pipelineKeys.list(filters),
    queryFn: () => fetchPipelines(filters),
    staleTime: 30_000,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
}

export function usePipelineDetail(id: string | undefined) {
  return useQuery({
    queryKey: pipelineKeys.detail(id!),
    queryFn: () => fetchPipelineDetail(id!),
    enabled: !!id,
    staleTime: 5_000,
    refetchInterval: 30_000, // Poll every 30 seconds (metadata rarely changes mid-run)
    refetchIntervalInBackground: false,
  });
}

export function useStats() {
  return useQuery({
    queryKey: statsKeys.all,
    queryFn: fetchStats,
    staleTime: 60_000,
    refetchInterval: 120_000,
    refetchIntervalInBackground: false,
  });
}
