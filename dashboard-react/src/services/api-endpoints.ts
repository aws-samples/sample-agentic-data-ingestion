// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * API Endpoint Registry — single source of truth for all API paths.
 *
 * Every hook MUST use these constants instead of hardcoding paths.
 * The companion test file verifies that:
 *   1. Every endpoint listed here is actually used in a hook
 *   2. Every apiClient call in hooks uses a path from this registry
 *   3. The paths match the vanilla JS dashboard's API calls
 */

export const API_ENDPOINTS = {
  // ── Pipelines ──────────────────────────────────────────────────
  pipelines: {
    list:       '/pipelines',                                         // GET  — list all pipelines (with ?search, ?status, ?page, ?page_size)
    detail:     (id: string) => `/pipeline/${enc(id)}`,               // GET  — single pipeline detail (NOTE: singular /pipeline/)
    provision:  '/pipelines/provision',                                // POST — create new pipeline
    edit:       (id: string) => `/pipelines/${enc(id)}/edit`,         // PUT  — edit pipeline
    archive:    (id: string) => `/pipelines/${enc(id)}/archive`,      // POST — archive pipeline
    restore:    (id: string) => `/pipelines/${enc(id)}/restore`,      // POST — restore archived pipeline
    delete:     (id: string) => `/pipelines/${enc(id)}/delete`,       // POST — permanently delete (NOT HTTP DELETE)
    archived:   '/pipelines/archived',                                // GET  — list archived pipelines
  },

  // ── Pipeline sub-resources ─────────────────────────────────────
  executions: {
    list: (id: string, page: number) =>
      `/pipeline/${enc(id)}/executions?page=${page}&page_size=10`,    // GET
  },

  mappingHints: {
    save: (key: string) => `/pipelines/${enc(key)}/mapping-hints`,    // POST
  },

  mappings: {
    update: (key: string) => `/pipelines/${enc(key)}/mappings`,       // PUT — directly update mapping_result
  },

  rerunMapping: '/rerun-mapping',                                     // POST — regenerate mappings

  // ── Runs ───────────────────────────────────────────────────────
  runs: {
    detail: (runId: string) => `/runs/${enc(runId)}/details`,         // GET
  },

  // ── Code ───────────────────────────────────────────────────────
  code: {
    detail:      (id: string) => `/code/${enc(id)}`,                  // GET
    testResults: (id: string) => `/tests/${enc(id)}`,     // GET
  },

  // ── Files ──────────────────────────────────────────────────────
  files: {
    detail: (pipelineKey: string, fileKey: string) =>
      `/files/${enc(pipelineKey)}/${enc(fileKey)}/details`,            // GET
  },

  // ── Schemas ────────────────────────────────────────────────────
  schemas: {
    list:     '/schemas',                                             // GET
    detail:   (id: string) => `/schemas/${enc(id)}`,                  // GET
    delete:   (id: string) => `/schemas/${enc(id)}`,                  // DELETE
    upload:   '/schemas/upload',                                      // POST (multipart)
    fieldCtx: (schemaId: string, fieldName: string) =>
      `/schemas/${enc(schemaId)}/fields/${enc(fieldName)}/context`,   // PUT
  },

  // ── Approvals ──────────────────────────────────────────────────
  approvals: {
    list:   '/approvals?status=pending&limit=50',                     // GET
    action: (id: string, action: 'approve' | 'reject') =>
      `/approvals/${enc(id)}/${action}`,                              // POST
  },

  // ── Glue Jobs ──────────────────────────────────────────────────
  glueJob: {
    detail: (pipelineId: string) =>
      `/pipelines/${enc(pipelineId)}/glue-job`,                       // GET
    run: (pipelineId: string) =>
      `/pipelines/${enc(pipelineId)}/glue-job/run`,                   // POST
  },

  // ── Metrics ────────────────────────────────────────────────────
  metrics: {
    comprehensive: '/metrics/comprehensive',                          // GET (404/error → honest error state, no mock fallback)
  },

  // ── Errors ─────────────────────────────────────────────────────
  errors: {
    list: '/errors',                                                  // GET — list errors (with ?start_date, ?end_date, ?pipeline_id, ?error_type)
  },

  // ── Stats ──────────────────────────────────────────────────────
  stats: '/stats',                                                    // GET

  // ── Resources (form dropdowns) ─────────────────────────────────
  resources: {
    bedrockModels: '/bedrock-models',                                 // GET
    s3Buckets:     '/resources/s3-tables/buckets',                    // GET
    s3BucketTables: (bucket: string) =>
      `/resources/s3-tables/buckets/${enc(bucket)}/tables`,           // GET
    glueDatabases: '/resources/glue/databases',                       // GET
    glueDatabaseTables: (db: string) =>
      `/resources/glue/databases/${enc(db)}/tables`,                  // GET
    rdsInstances:       '/resources/rds/instances',                   // GET
    rdsClusters:        '/resources/rds/clusters',                    // GET
    rdsGlueConnections: '/resources/rds/glue-connections',            // GET
    rdsSecrets:         '/resources/rds/secrets',                     // GET
    jdbcDetectSchema:   '/resources/jdbc/detect-schema',              // POST — start crawler-based JDBC schema detection (P17)
    jdbcDetectSchemaStatus: (crawlerName: string) =>
      `/resources/jdbc/detect-schema/${enc(crawlerName)}`,            // GET  — poll crawler status (P17)
    jdbcDetectTargetSchema: '/resources/jdbc/detect-target-schema',   // POST — start crawler-based JDBC target schema detection (P19)
    jdbcDetectTargetSchemaStatus: (crawlerName: string) =>
      `/resources/jdbc/detect-target-schema/${enc(crawlerName)}`,     // GET  — poll target crawler status (P19)

    // P20 — Manual JDBC connection mode (on-prem / cross-account / SaaS DBs)
    vpcList:               '/resources/vpc/list',                        // GET  — list VPCs (P20)
    vpcSubnets:            (vpcId: string) =>
      `/resources/vpc/${enc(vpcId)}/subnets`,                            // GET  — list subnets in VPC (P20)
    vpcSecurityGroups:     (vpcId: string) =>
      `/resources/vpc/${enc(vpcId)}/security-groups`,                    // GET  — list security groups in VPC (P20)
    jdbcCandidateSecrets:  '/resources/secrets/jdbc-candidates',         // GET  — heuristic-filtered Secrets Manager list (P20)
  },

  // ── Preview ─────────────────────────────────────────────────────
  preview: {
    latestRun: (pipelineId: string) =>
      `/pipelines/${enc(pipelineId)}/preview`,                        // GET — latest run preview
    specificRun: (pipelineId: string, runId: string) =>
      `/pipelines/${enc(pipelineId)}/run/${enc(runId)}/preview`,      // GET — specific run preview
  },

  // ── Costs ──────────────────────────────────────────────────────
  costs: {
    pipeline: (pipelineId: string) =>
      `/pipelines/${enc(pipelineId)}/costs`,                          // GET — aggregated Glue cost for one pipeline (P30 R3.1)
  },

  // ── Notifications ──────────────────────────────────────────────
  notifications: {
    list: '/notifications',                                           // GET — list notifications
    markRead: (notificationId: string) =>
      `/notifications/${enc(notificationId)}/read`,                   // PUT — mark notification read
  },

  // ── Updates ────────────────────────────────────────────────────
  updates: {
    list: '/updates',                                                 // GET — list recent updates
  },

  // ── Upload ─────────────────────────────────────────────────────
  //
  // DELIBERATELY ABSENT. There is no `upload` entry, and re-adding one alone is
  // a second declaration of a route this registry has no consumer for.
  //
  // p64 task 1d.2 deleted `upload.presignedUrl: '/upload-file'`, which was wrong
  // twice over. The live route is `POST /api/upload/presign`, dispatched by
  // `router.upload_router` to `presign_upload` (`handlers/upload.py`), and the
  // `// POST → returns { upload_url, s3_key }` comment described no handler that
  // ever existed — the handler it named, `generate_upload_url`, returned
  // `upload_url` / `s3_uri` / `bucket` / `key` / `expires_in`, never `s3_key`.
  //
  // Past tense on purpose: p64 task 1d.3 deleted `generate_upload_url` outright,
  // along with the `/upload-url` and `/upload-file` routes that reached it, so
  // `/api/upload/presign` is now the only presign route AND `presign_upload` the
  // only presign handler. Symbol names rather than line numbers below, because a
  // second copy of a line number is drift waiting to happen (`LGR-1e135d2-12`) —
  // this comment carried three that the collapse invalidated.
  //
  // It was deleted rather than corrected because it had zero readers outside its
  // own test. The presign call is issued by a component — `FileUploadModal.tsx`
  // — which passes the path inline. This registry is hook-scoped by its own
  // header above ("Every hook MUST use these constants"), so an entry here for a
  // component's inline call fails the first property that header claims: an
  // endpoint listed and used by no hook. Correcting the string would have
  // restored a self-confirming pair — the entry declared a route and its test
  // pinned the entry to itself, so neither could notice the route drifting from
  // the backend. That pair is how `/upload-file` stayed wrong.
  //
  // If a hook ever needs this path, add the entry AND move `FileUploadModal.tsx`
  // onto it in the same change, so the route stays declared once.
} as const;

function enc(s: string): string {
  return encodeURIComponent(s);
}
