// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/** Query key factories for TanStack Query cache management. */

import type { PipelineFilters } from '../types';

export const pipelineKeys = {
  all: ['pipelines'] as const,
  list: (filters?: PipelineFilters) => ['pipelines', 'list', filters] as const,
  detail: (id: string) => ['pipelines', id] as const,
  runs: (id: string) => ['pipelines', id, 'runs'] as const,
};

export const runKeys = {
  all: ['runs'] as const,
  detail: (runId: string) => ['runs', runId] as const,
  retry: (runId: string) => ['runs', runId, 'retry'] as const,
};

export const approvalKeys = {
  all: ['approvals'] as const,
  pending: () => ['approvals', 'pending'] as const,
  detail: (id: string) => ['approvals', id] as const,
};

export const schemaKeys = {
  all: ['schemas'] as const,
  detail: (id: string) => ['schemas', id] as const,
};

export const archiveKeys = {
  all: ['archived-pipelines'] as const,
  list: () => ['archived-pipelines', 'list'] as const,
};

export const statsKeys = {
  all: ['stats'] as const,
};

export const glueJobKeys = {
  all: ['glue-jobs'] as const,
  detail: (jobName: string) => ['glue-jobs', jobName] as const,
  versions: (pipelineId: string) => ['glue-jobs', pipelineId, 'versions'] as const,
};

export const metricsKeys = {
  all: ['metrics'] as const,
  comprehensive: () => ['metrics', 'comprehensive'] as const,
};

export const codeKeys = {
  all: ['code-versions'] as const,
  detail: (id: string) => ['code-versions', id] as const,
  testResults: (id: string) => ['code-versions', id, 'test-results'] as const,
};

export const artifactKeys = {
  all: ['artifacts'] as const,
  run: (pipelineId: string, runId: string) => ['artifacts', pipelineId, runId] as const,
};


export const mappingKeys = {
  all: (pipelineKey: string) => ['mappings', pipelineKey] as const,
  detail: (pipelineKey: string, schemaHash: string, versionIndex: number) =>
    ['mappings', pipelineKey, schemaHash, versionIndex] as const,
};

export const fileKeys = {
  all: ['files'] as const,
  detail: (pipelineKey: string, fileKey: string) => ['files', pipelineKey, fileKey] as const,
};

export const resourceKeys = {
  bedrockModels: () => ['resources', 'bedrock-models'] as const,
  s3Buckets: () => ['resources', 's3-tables', 'buckets'] as const,
  s3BucketTables: (bucket: string) => ['resources', 's3-tables', 'buckets', bucket, 'tables'] as const,
  glueDatabases: () => ['resources', 'glue', 'databases'] as const,
  glueDatabaseTables: (database: string) => ['resources', 'glue', 'databases', database, 'tables'] as const,
  rdsInstances: () => ['resources', 'rds', 'instances'] as const,
  rdsClusters: () => ['resources', 'rds', 'clusters'] as const,
  rdsGlueConnections: () => ['resources', 'rds', 'glue-connections'] as const,
  rdsSecrets: () => ['resources', 'rds', 'secrets'] as const,
  jdbcDetectSchema: (crawlerName: string) =>
    ['resources', 'jdbcDetectSchema', crawlerName] as const,
  jdbcDetectTargetSchema: (crawlerName: string) =>
    ['resources', 'jdbcDetectTargetSchema', crawlerName] as const,
  // P20 — Manual JDBC connection mode
  vpcList: () => ['resources', 'vpc', 'list'] as const,
  vpcSubnets: (vpcId: string) => ['resources', 'vpc', 'subnets', vpcId] as const,
  vpcSecurityGroups: (vpcId: string) =>
    ['resources', 'vpc', 'security-groups', vpcId] as const,
  jdbcCandidateSecrets: () =>
    ['resources', 'secrets', 'jdbc-candidates'] as const,
};

export const executionKeys = {
  all: ['executions'] as const,
  /**
   * One cache entry per *page position* of a pipeline's run history.
   *
   * `pipelineKey` stays in **second** position and `cursor` is appended after
   * `page`, which is a constraint rather than a style choice: `LGR-8b6e6a1-1`
   * (**template / ratified**) records that a WebSocket frame invalidates the
   * page-key **prefix** `['executions', id]` (`WebSocketProvider.tsx:556`), so
   * any new key member has to sit *after* the id or the flush stops matching and
   * a live run stops refreshing.
   *
   * `cursor` is `null` for page 1 — spelled explicitly rather than left
   * `undefined`, because React Query hashes the key and a trailing `undefined`
   * is not stable across the two spellings a caller might use. It is the
   * opaque `pagination.next_cursor` from the previous page (p48 C2 Arm A);
   * `page` rides alongside as the label the wire echoes back, so the key stays
   * legible in devtools rather than being a bare base64 blob.
   *
   * `status` is appended **last** (p48 C5, task 5.5) — the derived-status filter
   * (`null` for unfiltered, spelled explicitly for the same hashing reason as
   * `cursor`). It is part of the key because a filtered read and an unfiltered
   * read of the same page position are different responses; sharing one entry
   * would serve one as the other. It sits after the id for the invalidation
   * reason above, so the flush prefix still reaches it.
   */
  list: (
    pipelineKey: string,
    page: number,
    cursor?: string | null,
    status?: string | null,
  ) => ['executions', pipelineKey, page, cursor ?? null, status ?? null] as const,
  /**
   * The run either side of one run, chronologically (p48 C10, task 5.2).
   *
   * Filed under `executions` rather than `runKeys` **for the invalidation**, not
   * for tidiness. `LGR-8b6e6a1-1` (**template / ratified**) has a WebSocket frame
   * invalidate the prefix `['executions', id]`, so this key is refreshed whenever
   * anything moves on the pipeline — which is exactly when a run's `newer`
   * neighbour can come into existence. Keyed under `runKeys.detail(runId)`'s
   * prefix instead, it would refresh only when *this* run changed, and a brand-new
   * run would leave a stale disabled "Next Run" button behind it.
   *
   * `pipelineKey` therefore stays in **second** position for the same reason
   * `list`'s does. The literal `'neighbours'` sits where `list` puts a numeric
   * `page`, so the two families cannot collide.
   */
  neighbours: (pipelineKey: string, runId: string) =>
    ['executions', pipelineKey, 'neighbours', runId] as const,
  /**
   * Every **pending approval** on one pipeline (p51 C2, task 3.1).
   *
   * Filed under `executions` rather than `approvalKeys` **for the invalidation**,
   * not for tidiness — the same argument `neighbours` above carries. Both refresh
   * paths were **read and measured**, not assumed (p51 R3.2):
   *
   * - A WebSocket frame invalidates exactly `[...executionKeys.all, pipelineId]`
   *   and `runKeys.detail(runId)` (`WebSocketProvider.tsx:556`, `:559`) and
   *   **nothing** under `['approvals']`. That frame is what carries a
   *   newly-*appearing* approval, so a key under `approvalKeys` would not reach the
   *   overview card until a poll or a remount.
   * - `useApprovalAction` invalidates `approvalKeys.all`, `executionKeys.all` and
   *   `pipelineKeys.all` (`useApprovalAction.ts:73-75`).
   *
   * So this placement is reached by **both** paths, and an `approvalKeys` placement
   * by only one — with no new invalidation wiring and no change to
   * `WebSocketProvider`.
   *
   * **Which invalidation does the reaching, measured rather than reasoned.**
   * `invalidateQueries` matches by key **prefix from index 0**, so `['approvals']`
   * does *not* match `['executions', id, 'approvals']` — the trailing literal is not
   * a prefix. The approve/reject path therefore reaches this key through
   * `executionKeys.all` (`useApprovalAction.ts:74`), not through `approvalKeys.all`
   * (`:73`). Both refresh paths arrive via the `executions` prefix, which is exactly
   * why the placement is the mechanism. P6 pins all three outcomes against a real
   * `QueryClient`, including the negative one.
   *
   * `pipelineKey` stays in **second** position — `LGR-8b6e6a1-1` (**template /
   * ratified**) records that the flush matches the prefix `['executions', id]`, so
   * any member placed ahead of the id stops the flush matching and a live run stops
   * refreshing. The literal `'approvals'` sits where `list` puts a numeric `page`,
   * so the two families cannot collide. P5 asserts the literal key array, so the
   * constraint is pinned by a test rather than by this comment.
   */
  pendingApprovals: (pipelineKey: string) =>
    ['executions', pipelineKey, 'approvals'] as const,
};

export const costKeys = {
  all: ['costs'] as const,
  pipeline: (pipelineId: string) => ['costs', pipelineId] as const,
};

export const previewKeys = {
  all: ['preview'] as const,
  pipeline: (pipelineId: string) => ['preview', pipelineId] as const,
  run: (pipelineId: string, runId: string) => ['preview', pipelineId, runId] as const,
};

export const notificationKeys = {
  all: ['notifications'] as const,
  list: (params?: { limit?: number; unreadOnly?: boolean }) =>
    ['notifications', 'list', params] as const,
};

export const updateKeys = {
  all: ['updates'] as const,
  list: (limit?: number) => ['updates', 'list', limit] as const,
};

export const mappingMemoryKeys = {
  all: ['mapping-memory'] as const,
  list: (pipelineId: string) => ['mapping-memory', pipelineId] as const,
};

export const glueJobLogKeys = {
  all: ['glue-job-logs'] as const,
  detail: (pipelineId: string, runId: string, logType: string) =>
    ['glue-job-logs', pipelineId, runId, logType] as const,
};


export const agentTraceKeys = {
  all: ['agent-traces'] as const,
  run: (runId: string) => ['agent-traces', runId] as const,
  step: (runId: string, stepName: string) => ['agent-traces', runId, stepName] as const,
};
