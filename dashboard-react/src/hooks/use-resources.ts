// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useQuery } from '@tanstack/react-query';
import { useMutation } from '@tanstack/react-query';
import { apiClient } from '../services/api-client';
import { API_ENDPOINTS } from '../services/api-endpoints';
import { resourceKeys } from './query-keys';
import type {
  RdsInstance,
  RdsCluster,
  GlueJdbcConnection,
  RdsSecret,
  Vpc,
  Subnet,
  SecurityGroup,
  Secret,
  RawBedrockModel,
  BedrockModelsResponse,
  RawS3TableBucket,
  S3TableBucketsResponse,
  RawTableInfo,
  S3TablesResponse,
  RawGlueDatabase,
  GlueDatabasesResponse,
  GlueTablesResponse,
  RdsInstancesResponse,
  RdsClustersResponse,
  RdsGlueConnectionsResponse,
  RdsSecretsResponse,
  VpcListResponse,
  SubnetsResponse,
  SecurityGroupsResponse,
  JdbcCandidateSecretsResponse,
} from '../types';

interface BedrockModel {
  model_id: string;
  model_name: string;
}

interface S3TableBucket {
  bucket_name: string;
  arn?: string;
}

interface TableInfo {
  table_name: string;
  namespace?: string;
  database?: string;
}

/** Normalized Glue database option (canonical `database_name`). */
interface GlueDatabaseOption {
  database_name: string;
}

async function fetchBedrockModels(): Promise<BedrockModel[]> {
  // NOTE (P29 R11.6): no hardcoded FALLBACK_BEDROCK_MODELS. The model list comes
  // solely from the API. Like every other R1.2 fetcher, errors are NOT swallowed —
  // any transport or API error propagates so React Query surfaces an honest error
  // state (with retry) instead of silently showing a stale hardcoded model list.
  // API returns { models: [{ modelId, modelName, ... }] }; legacy paths a bare array.
  const body = await apiClient.get<BedrockModelsResponse | RawBedrockModel[]>('/bedrock-models');
  const rawModels: RawBedrockModel[] = Array.isArray(body) ? body : (body.models ?? []);
  return rawModels.map((m) => ({
    model_id: m.modelId ?? m.model_id ?? '',
    model_name: m.modelName ?? m.model_name ?? '',
  }));
}

/** Normalize a raw table item (S3 Tables or Glue) to the canonical shape. */
function mapTable(t: RawTableInfo): TableInfo {
  return {
    table_name: t.table_name ?? t.name ?? '',
    namespace: t.namespace ?? '',
    database: t.database ?? '',
  };
}

async function fetchS3TablesBuckets(): Promise<S3TableBucket[]> {
  // NOTE (P29 R1.2): fetchers no longer swallow errors into `[]`. Any transport
  // or API error propagates so React Query can surface an honest error state.
  const body = await apiClient.get<S3TableBucketsResponse | RawS3TableBucket[]>(
    '/resources/s3-tables/buckets',
  );
  const raw: RawS3TableBucket[] = Array.isArray(body) ? body : (body.buckets ?? []);
  return raw.map((b) => ({
    bucket_name: b.bucket_name ?? b.name ?? '',
    arn: b.arn ?? undefined,
  }));
}

async function fetchTablesInBucket(bucket: string): Promise<TableInfo[]> {
  const body = await apiClient.get<S3TablesResponse | RawTableInfo[]>(
    `/resources/s3-tables/buckets/${encodeURIComponent(bucket)}/tables`,
  );
  const raw: RawTableInfo[] = Array.isArray(body) ? body : (body.tables ?? []);
  return raw.map(mapTable);
}

async function fetchGlueDatabases(): Promise<GlueDatabaseOption[]> {
  const body = await apiClient.get<GlueDatabasesResponse | RawGlueDatabase[]>(
    '/resources/glue/databases',
  );
  const raw: RawGlueDatabase[] = Array.isArray(body) ? body : (body.databases ?? []);
  return raw.map((d) => ({ database_name: d.database_name || d.name || '' }));
}

async function fetchTablesInDatabase(database: string): Promise<TableInfo[]> {
  // The Glue "tables" handler returns items keyed `name` (not `table_name`),
  // exactly like the S3 Tables sibling, so normalize through the same mapper to
  // honor the declared `TableInfo` shape instead of casting raw items.
  const body = await apiClient.get<GlueTablesResponse | RawTableInfo[]>(
    `/resources/glue/databases/${encodeURIComponent(database)}/tables`,
  );
  const raw: RawTableInfo[] = Array.isArray(body) ? body : (body.tables ?? []);
  return raw.map(mapTable);
}

export function useBedrockModels() {
  return useQuery({
    queryKey: resourceKeys.bedrockModels(),
    queryFn: fetchBedrockModels,
    staleTime: 300_000, // 5 minutes — models don't change often
    retry: 2, // P29 R11.6 — sane retry default so a transient failure self-heals;
    // the model Select surfaces the error + a Retry link if all attempts fail.
  });
}

export function useS3TablesBuckets() {
  return useQuery({
    queryKey: resourceKeys.s3Buckets(),
    queryFn: fetchS3TablesBuckets,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useTablesInBucket(bucket: string | undefined) {
  return useQuery({
    queryKey: resourceKeys.s3BucketTables(bucket!),
    queryFn: () => fetchTablesInBucket(bucket!),
    enabled: !!bucket,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useGlueDatabases() {
  return useQuery({
    queryKey: resourceKeys.glueDatabases(),
    queryFn: fetchGlueDatabases,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useTablesInDatabase(database: string | undefined) {
  return useQuery({
    queryKey: resourceKeys.glueDatabaseTables(database!),
    queryFn: () => fetchTablesInDatabase(database!),
    enabled: !!database,
    staleTime: 60_000,
    retry: 2,
  });
}

// ── RDS Discovery fetch functions (P14) ──────────────────────────

async function fetchRdsInstances(): Promise<RdsInstance[]> {
  const body = await apiClient.get<RdsInstancesResponse | RdsInstance[]>('/resources/rds/instances');
  return Array.isArray(body) ? body : (body.instances ?? []);
}

async function fetchRdsClusters(): Promise<RdsCluster[]> {
  const body = await apiClient.get<RdsClustersResponse | RdsCluster[]>('/resources/rds/clusters');
  return Array.isArray(body) ? body : (body.clusters ?? []);
}

async function fetchRdsGlueConnections(): Promise<GlueJdbcConnection[]> {
  const body = await apiClient.get<RdsGlueConnectionsResponse | GlueJdbcConnection[]>(
    '/resources/rds/glue-connections',
  );
  return Array.isArray(body) ? body : (body.connections ?? []);
}

async function fetchRdsSecrets(): Promise<RdsSecret[]> {
  const body = await apiClient.get<RdsSecretsResponse | RdsSecret[]>('/resources/rds/secrets');
  return Array.isArray(body) ? body : (body.secrets ?? []);
}

// ── RDS Discovery hooks (P14) ────────────────────────────────────

export function useRdsInstances() {
  return useQuery({
    queryKey: resourceKeys.rdsInstances(),
    queryFn: fetchRdsInstances,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useRdsClusters() {
  return useQuery({
    queryKey: resourceKeys.rdsClusters(),
    queryFn: fetchRdsClusters,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useRdsGlueConnections() {
  return useQuery({
    queryKey: resourceKeys.rdsGlueConnections(),
    queryFn: fetchRdsGlueConnections,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useRdsSecrets() {
  return useQuery({
    queryKey: resourceKeys.rdsSecrets(),
    queryFn: fetchRdsSecrets,
    staleTime: 60_000,
    retry: 2,
  });
}

// ── JDBC Schema Detection (P17) ──────────────────────────────────

export type JdbcDetectSchemaMode = 'select_rds' | 'glue_connection';

export interface JdbcDetectSchemaStartRequest {
  source_config: Record<string, string>;
  mode: JdbcDetectSchemaMode;
}

export interface JdbcDetectSchemaStartResponse {
  crawler_name: string;
  glue_database: string;
  table_prefix: string;
  status: 'RUNNING' | 'READY' | 'FAILED';
}

export interface JdbcSchemaField {
  name: string;
  type: string;
  nullable?: boolean;
  description?: string;
}

export interface JdbcDetectSchemaStatusResponse {
  status: 'RUNNING' | 'READY' | 'FAILED';
  schema?: JdbcSchemaField[];
  glue_database: string;
  glue_table?: string;
  table_prefix?: string;
  crawler_name?: string;
  error_message?: string;
}

/**
 * POST mutation that starts a Glue Crawler for JDBC schema detection.
 * Returns the crawler name + Glue database + deterministic table prefix.
 * The caller then polls `useJdbcDetectSchemaStatus(crawler_name)` until the
 * status flips to READY or FAILED.
 *
 * Validates Requirement 17.6.
 */
export function useJdbcDetectSchemaStart() {
  return useMutation({
    mutationFn: async (
      payload: JdbcDetectSchemaStartRequest,
    ): Promise<JdbcDetectSchemaStartResponse> => {
      return apiClient.post<JdbcDetectSchemaStartResponse>(
        API_ENDPOINTS.resources.jdbcDetectSchema,
        payload,
      );
    },
  });
}

/**
 * Polling query for JDBC schema detection. Polls every 3 seconds while the
 * crawler is RUNNING, and stops on the terminal states READY and FAILED.
 *
 * Implementation notes:
 *   - `enabled` is false when `crawlerName` is null/undefined so the query
 *     stays idle until detection has started.
 *   - `refetchInterval` is a callback that returns `false` on terminal status
 *     (READY/FAILED) and `3000` while RUNNING — this is what stops the poll.
 *   - `refetchIntervalInBackground` is TRUE. It was false, to avoid hammering
 *     the API when the tab loses focus, and that is what broke it.
 *
 * WHY BACKGROUND POLLING IS REQUIRED, NOT A PREFERENCE. The caller's deadline
 * (`JdbcSourceDetectionPanel.tsx`, `DETECTION_TIMEOUT_MS`) is a plain
 * `setTimeout` on WALL CLOCK. It keeps counting whether or not anyone is
 * polling. With background refetching off, the two disagree the moment the tab
 * loses focus: the clock runs, the observation stops, and the deadline fires
 * having learned nothing. The user is then told the crawler did not finish —
 * a claim the code never checked.
 *
 * Observed, not theorised. On a real detection the poll ran for exactly 195s
 * (60 requests, every one RUNNING) and then stopped dead, with nothing in this
 * file capable of stopping it there. The crawl SUCCEEDED ~60s later; polling
 * the same crawler afterwards returned READY with the full 5-column schema. The
 * wizard had already reported a 12-minute timeout.
 *
 * Two facts make the focus-gated window far too tight to rely on. A JDBC
 * discovery crawl does ~2-3 min of work (median runtime 165s), and Glue holds
 * `State: RUNNING` well past that through provisioning and teardown — which is
 * why the caller's cap is 12 minutes and not 5. So the interval has to survive
 * a glance away from the tab, which is ordinary behaviour during a 5-minute
 * wait, not an edge case.
 *
 * The cost this traded away is small and bounded: one GET every 3s against a
 * poll that already stops itself on READY/FAILED, for at most the caller's
 * 12-minute cap. Rejected alternatives: pausing the deadline on blur as well
 * (keeps the two in step but makes a 12-minute cap mean an unbounded amount of
 * real time, and the crawler's own cost keeps accruing while it is paused); and
 * polling from a service worker or a visibility listener, which is a lot of
 * machinery for a wizard step. If the request rate ever matters, lengthen the
 * interval — do NOT set this back to false.
 *
 * Validates Requirement 17.6.
 */
export function useJdbcDetectSchemaStatus(crawlerName: string | null | undefined) {
  return useQuery({
    queryKey: resourceKeys.jdbcDetectSchema(crawlerName ?? ''),
    queryFn: async (): Promise<JdbcDetectSchemaStatusResponse> => {
      return apiClient.get<JdbcDetectSchemaStatusResponse>(
        API_ENDPOINTS.resources.jdbcDetectSchemaStatus(crawlerName as string),
      );
    },
    enabled: !!crawlerName,
    retry: false,
    refetchInterval: (query) => {
      const data = query.state.data as JdbcDetectSchemaStatusResponse | undefined;
      if (!data) return 3_000;
      if (data.status === 'READY' || data.status === 'FAILED') return false;
      return 3_000;
    },
    // MUST stay true: the caller's deadline is a wall-clock `setTimeout`, and
    // pausing the poll while that timer keeps counting is what made a SUCCEEDED
    // crawl report a timeout. See the block comment above.
    refetchIntervalInBackground: true,
  });
}

// ── JDBC Target Schema Detection (P19) ───────────────────────────

export interface JdbcDetectTargetSchemaStartRequest {
  target_config: Record<string, string>;
}

/**
 * POST mutation that starts a Glue Crawler for JDBC TARGET-side schema
 * detection. Mirrors the source-side `useJdbcDetectSchemaStart` (P17)
 * line-for-line but POSTs `{ target_config }` to the new target endpoint.
 * The wire-shape of the response is identical to the source-side hook, so
 * `JdbcDetectSchemaStartResponse` is reused per design Decision.
 *
 * The caller then polls `useJdbcDetectTargetSchemaStatus(crawler_name)`
 * until the status flips to READY or FAILED.
 *
 * Validates Requirements 4.1, 4.7.
 */
export function useJdbcDetectTargetSchemaStart() {
  return useMutation({
    mutationFn: async (
      payload: JdbcDetectTargetSchemaStartRequest,
    ): Promise<JdbcDetectSchemaStartResponse> => {
      return apiClient.post<JdbcDetectSchemaStartResponse>(
        API_ENDPOINTS.resources.jdbcDetectTargetSchema,
        payload,
      );
    },
  });
}

/**
 * Polling query for JDBC TARGET-side schema detection. Mirrors the
 * source-side `useJdbcDetectSchemaStatus` (P17) line-for-line. Polls every
 * 3 seconds while the crawler is RUNNING and stops on the terminal states
 * READY and FAILED. Forwards `tablePrefix` as a `?table_prefix=...` query
 * parameter when provided so the backend can short-circuit the derivation
 * from the crawler-name pattern.
 *
 * Implementation notes (mirror of source-side hook):
 *   - `enabled` is false when `crawlerName` is null so the query stays idle
 *     until detection has started.
 *   - `refetchInterval` is a callback that returns `false` on terminal status
 *     (READY/FAILED) and `3000` while RUNNING — this is what stops the poll.
 *   - `refetchIntervalInBackground` is TRUE, for the reason the source-side
 *     hook's comment gives at length: the caller's deadline is a wall-clock
 *     timer, so a poll that pauses on blur turns a SUCCEEDED crawl into a
 *     reported timeout. It was false here too.
 *
 * Validates Requirements 4.8, 4.9.
 */
export function useJdbcDetectTargetSchemaStatus(
  crawlerName: string | null,
  tablePrefix?: string | null,
) {
  return useQuery({
    queryKey: resourceKeys.jdbcDetectTargetSchema(crawlerName ?? ''),
    queryFn: async (): Promise<JdbcDetectSchemaStatusResponse> => {
      const base = API_ENDPOINTS.resources.jdbcDetectTargetSchemaStatus(crawlerName as string);
      const url = tablePrefix
        ? `${base}?table_prefix=${encodeURIComponent(tablePrefix)}`
        : base;
      return apiClient.get<JdbcDetectSchemaStatusResponse>(url);
    },
    enabled: !!crawlerName,
    retry: false,
    refetchInterval: (query) => {
      const data = query.state.data as JdbcDetectSchemaStatusResponse | undefined;
      if (!data) return 3_000;
      if (data.status === 'READY' || data.status === 'FAILED') return false;
      return 3_000;
    },
    // MUST stay true — same reason as the source-side hook.
    refetchIntervalInBackground: true,
  });
}


// ── VPC / Subnet / Security Group / JDBC-Candidate Secrets (P20) ─

/**
 * P20 — Manual JDBC connection mode helper hooks.
 *
 * These hooks back the wizard's `<ManualConnectionForm>` VPC / Subnet /
 * Security Group / Secret dropdowns. The Subnet and Security Group
 * hooks intentionally short-circuit when `vpcId` is falsy via React
 * Query's `enabled` option — without that guard the hooks would issue
 * `GET /resources/vpc/undefined/subnets` on every wizard render.
 *
 * Validates: P20 Requirements 10.1, 10.2, 10.3, 10.4.
 */

async function fetchVpcs(): Promise<Vpc[]> {
  const body = await apiClient.get<VpcListResponse | Vpc[]>(
    API_ENDPOINTS.resources.vpcList,
  );
  return Array.isArray(body) ? body : (body.vpcs ?? []);
}

async function fetchSubnets(vpcId: string): Promise<Subnet[]> {
  const body = await apiClient.get<SubnetsResponse | Subnet[]>(
    API_ENDPOINTS.resources.vpcSubnets(vpcId),
  );
  return Array.isArray(body) ? body : (body.subnets ?? []);
}

async function fetchSecurityGroups(vpcId: string): Promise<SecurityGroup[]> {
  const body = await apiClient.get<SecurityGroupsResponse | SecurityGroup[]>(
    API_ENDPOINTS.resources.vpcSecurityGroups(vpcId),
  );
  return Array.isArray(body) ? body : (body.security_groups ?? []);
}

async function fetchJdbcCandidateSecrets(): Promise<Secret[]> {
  const body = await apiClient.get<JdbcCandidateSecretsResponse | Secret[]>(
    API_ENDPOINTS.resources.jdbcCandidateSecrets,
  );
  return Array.isArray(body) ? body : (body.secrets ?? []);
}

export function useVpcs() {
  return useQuery({
    queryKey: resourceKeys.vpcList(),
    queryFn: fetchVpcs,
    staleTime: 60_000,
    retry: 2,
  });
}

export function useSubnets(vpcId: string | undefined | null) {
  return useQuery({
    queryKey: resourceKeys.vpcSubnets(vpcId ?? ''),
    queryFn: () => fetchSubnets(vpcId!),
    // CRITICAL: skip the fetch when vpcId is falsy. Without this guard
    // the hook would issue GET /resources/vpc//subnets which the API
    // Gateway / router rejects with HTTP 400 on every wizard render.
    enabled: !!vpcId,
    staleTime: 60_000,
    // P29 R1.2 — sane retry default so a transient failure self-heals; the
    // consumer dropdown surfaces the error + a Retry link if all attempts fail.
    retry: 2,
  });
}

export function useSecurityGroups(vpcId: string | undefined | null) {
  return useQuery({
    queryKey: resourceKeys.vpcSecurityGroups(vpcId ?? ''),
    queryFn: () => fetchSecurityGroups(vpcId!),
    enabled: !!vpcId,
    staleTime: 60_000,
    retry: 2, // P29 R1.2
  });
}

export function useJdbcCandidateSecrets() {
  return useQuery({
    queryKey: resourceKeys.jdbcCandidateSecrets(),
    queryFn: fetchJdbcCandidateSecrets,
    staleTime: 60_000,
    retry: 2, // P29 R1.2
  });
}
