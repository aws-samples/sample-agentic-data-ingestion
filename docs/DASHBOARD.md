# Dashboard Documentation

The Agentic Data Ingestion Dashboard is a React + TypeScript single-page application built with [Cloudscape Design System](https://cloudscape.design/) (AWS UI components). It provides pipeline management, execution monitoring, approval workflows, and schema management for the Agentic Data Ingestion ETL automation platform.

## Architecture

```
Browser (React SPA)
  │
  │  Unauthenticated → redirect to Cognito Hosted UI
  │  Authenticated → /api/* calls include Authorization: <Cognito ID token>
  ▼
CloudFront Distribution
  │
  ├── /* (default) → S3 bucket (OAC, SPA fallback)
  │
  └── /api/* → REST API Gateway (Cognito User Pools authorizer)
                  │
                  ▼
              Lambda (agentic_data_ingestion_dashboard_api)
                  │
                  ▼
              DynamoDB tables

WebSocket: wss://...?token=<JWT> → Lambda authorizer on $connect
```

**Production:** The dashboard is hosted on private S3 behind CloudFront (OAC). Auth uses Cognito (Hosted UI OIDC code flow + PKCE via `react-oidc-context`). The REST API uses a Cognito User Pools authorizer; the dashboard sends the Cognito **ID token** as the raw `Authorization` header value (no `Bearer ` prefix) — the authorizer is configured without scopes, so it validates the ID token rather than the access token. The WebSocket API uses a Lambda authorizer on `$connect` that validates Cognito JWTs passed via `?token=`. A CLOUDFRONT-scope WAF (deployed in the us-east-1 EdgeWaf stack, attached cross-region) protects the distribution; a REGIONAL-scope WAF on the API stage enforces CloudFront-only access by blocking direct `execute-api` requests.

**Local dev:** The Vite dev server proxies `/api/*` to the CloudFront URL (not raw API Gateway — WAF blocks direct access). Auth works via real Cognito Hosted UI login. See `docs/LOCAL_DEVELOPMENT.md` for setup.

### Tech Stack

| Layer | Technology |
|-------|-----------|
| UI framework | React 18 + TypeScript |
| Component library | Cloudscape Design System |
| Routing | React Router v6 |
| Data fetching | TanStack React Query v5 |
| Auth | `react-oidc-context` + `oidc-client-ts` (Cognito OIDC PKCE) |
| Build / dev server | Vite 6 |
| Testing | Vitest + React Testing Library + fast-check |
| Hosting | S3 + CloudFront (OAC) + WAF |

## Pages

All pages live in `src/pages/`.

| Page | Route | Description |
|------|-------|-------------|
| PipelineListPage | `/` | Grid of pipeline cards with sort/filter controls and stats bar. Create pipeline, upload files, trigger runs. |
| PipelineLayout | `/pipelines/:id` | Shared layout that fetches pipeline data once via `usePipelineDetail`, provides `PipelineContext`, renders Cloudscape `Tabs` (5 items) with URL-driven navigation and `<Outlet />` for nested sub-pages. |
| PipelineOverviewPage | `/pipelines/:id` (index) | Pipeline metadata, target config, stats summary, "Recent Runs" table (last 5 executions), action buttons (upload, edit, archive, Glue job, refresh). Owns `showEditModal` and `showUploadModal` state. |
| SchemaHistoryPage | `/pipelines/:id/schema` | Schema evolution timeline, mapping viewer/editor, version navigation. Owns `selectedVersionIndex`, `versionPage`, `mappingVersionIndex`, `editMappingMode`, `selectedRunIndex`. |
| CodePage | `/pipelines/:id/code` | Code viewer, test results viewer, run selector badges, schema version selector dropdown. Derives active version from latest schema history. Owns `selectedRunIndex`. |
| ExecutionsPage | `/pipelines/:id/executions` | Execution table with status indicators, step icons, and pagination. Owns `execPage`. |
| FilesPage | `/pipelines/:id/files` | File list with pagination and `FileDetailModal`. Filters non-JSON files. Owns `filePage`, `selectedFileKey`. |
| RunDetailPage | `/pipelines/:id/runs/:runId` | Run execution details with tabbed layout (Step Flow, AI Traces, Approvals, Schema & Mappings, Code, Data Quality), prev/next run navigation, inline approval buttons, and WebSocket live updates. (Sibling route, not nested under PipelineLayout.) |
| GlueJobDetailPage | `/pipelines/:id/glue-job/:jobName` | Glue job configuration, parameters, and run history. (Sibling route, not nested under PipelineLayout.) |
| ApprovalsPage | `/approvals` | Pending approvals table with quick approve/reject actions and full review modal. |
| SchemaManagementPage | `/schemas` | Upload and manage target schemas with source selection and source type filter dropdown. Glue Data Catalog rows leave out the app's own tables: everything in the JDBC discovery database (crawler copies of source tables) and any Glue table on S3 Tables managed storage (`--table-s3`), which is a mirror of an S3 Tables table (`get_schemas` in `handlers/schemas.py`). |
| ArchivedPipelinesPage | `/archived` | Archived pipelines list with restore functionality. |
| CreatePipelinePage | `/pipelines/new` | Cloudscape Wizard for pipeline creation (name, source, target, schema, AI model, review — see [Create Pipeline Wizard](#create-pipeline-wizard)). |
| MetricsDashboardPage | `/metrics` | Pipeline metrics, execution stats, and success rates with real CloudWatch data. |
| ChatPage | `/chat` | Natural language pipeline builder chat interface: request/response (not streamed), with a read-only proposal card. **The route is registered but nothing in the UI links to it.** `NAV_ITEMS` in `App.tsx` has no Chat entry, so `/chat` is reachable only by typing the URL. See [Chat Page](#chat-page-chat). |
| MappingMemoryPage | `/pipelines/:id/mapping-memory` | View and manage stored mapping patterns from AgentCore Memory. |
| ErrorsPage | `/errors` | Centralized error tracking with date range and error type filters. |

## Key Components

Components live in `src/components/`.

- **StepFlowVisualization** — Vertical step timeline with status colors, MCP operations, and error details for run visualization.
- **MappingViewer** — Displays field mappings with confidence badges, unmapped fields, mapping context, and rerun mapping button.
- **DragDropMappingEditor** — Interactive drag-and-drop mapping editor for manual field mapping.
- **CodeViewer** — Syntax-highlighted PySpark code display with metadata.
- **TestResultsViewer** — Test results with pass/fail details and coverage info.
- **PipelineCard** — Pipeline summary card for the list view grid.
- **ApprovalWorkflowSection** — Approve/reject buttons and notes input embedded in run detail.
- **TargetConfigDisplay / TargetConfigForm** — Read-only and editable target configuration views.
- **SchemaSourceSelector** — Schema source selection for pipeline configuration.
- **FileUploadModal** — File upload with presigned S3 POST, XHR progress tracking (ProgressBar with percentage, bytes transferred, ETA), cancel button, and pre-upload file validation.
- **PipelineFormModal** — Create/edit pipeline form with source and target configuration.
- **SchemaUploadWizard** — Drag-drop schema upload with client-side JSON/CSV parsing, inline preview Table with editable type and description columns, structure validation, and "Use This Schema" button. Reused in both SchemaManagementPage and CreatePipelinePage wizard step 3.
- **AgentTraceTimeline** — Vertical timeline of AI actions within a pipeline step: model invocations (with token counts, duration, prompt/response previews) and MCP tool calls (with parameters, results, status). Uses Cloudscape `ExpandableSection`, `StatusIndicator`, and `Badge`. Shown as an expandable section per AI step on the run detail page.
- **TokenUsageSummary** — Compact token/cost summary bar displayed at the top of the run detail page. Shows total tokens (input/output), estimated cost, model name, and cache hit rate.
- **PromptResponseModal** — Cloudscape Modal showing the full prompt sent to Bedrock and the response received for a model invocation. Triggered by clicking "Show Prompt" or "Show Response" in the AgentTraceTimeline.
- **AgentOutputDiff** — Side-by-side diff component for comparing two mapping versions or two code versions. Shows line-level changes with color-coded status (added/removed/modified). Used in SchemaHistoryPage compare modal and CodePage diff view.
- **QualityReportCard** — Displays inline Glue Data Quality results with pass/fail per rule, percentage formatting, and expandable drill-down. Shown on RunDetailPage Data Quality tab.
- **PipelineProposalCard** — **Read-only** rendering of a proposal from the NL pipeline builder: a two-column source/target summary plus a Schema Fields `Table` and a Mappings `Table`. It holds no state and renders no `Input`, so nothing on it can be edited; the only interactive element is the "Create Pipeline" button in its `Header` actions, and that button is a prop callback (`onCreatePipeline`), so the card itself decides nothing about what creation means. Used in ChatPage. See [Chat Page](#chat-page-chat) for what the button actually does.
- **HelpModal** — Keyboard shortcuts and help reference.
- **StatsBar** — Dashboard-level statistics summary.

> **The legacy P14 RDS Database target form is gone, and this inventory listed it as live long after it
> was deleted.** P17 task 20.4 replaced it and the legacy `JdbcForm` with the single
> `pages/wizard/JdbcTargetForm.tsx`, whose header comment at `:47` calls that path "now-deleted". The
> only surviving mentions anywhere in `dashboard-react/src/` are comments describing the removal — a
> component name in a comment is not a component. Extend `JdbcTargetForm` instead.

### The rest of the tree

**Counting rule:** one `.tsx` file under `dashboard-react/src/components/`, recursively, excluding
`__tests__/`. That rule yields **55** components. The bullet list above names **21** of them, so **34
are undocumented**, not the 18 this spec's requirement R13.3 estimated. The 34 are named here rather
than summarised, because a count a reader cannot check is a figure to re-litigate rather than use:

```bash
find dashboard-react/src/components -name '*.tsx' -not -path '*__tests__*' | wc -l   # 55
```

Every figure in this section is the output of a command stated beside it. **Re-run the command rather
than trusting the number.** Each of these three was stale by 2, 2 and 1 respectively before the
commands were added, and a literal nobody can re-derive is the mechanism that let them drift.
The four figures close arithmetically, which is the cheap check that none has
moved on its own: 42 top-level + 1 `approvals/` + 4 `common/` + 8 `run-detail/` = 55.

**Top level (21 of the 42 top-level components)**, the 42 being the files directly in
`components/` with no intervening directory:

```bash
find dashboard-react/src/components -maxdepth 1 -name '*.tsx' | wc -l   # 42
```

| Component | Purpose |
|-----------|---------|
| `ActivityFeed` | Recent-activity list on the pipeline list page |
| `ApprovalGatesFields` | The pipeline's **three** boolean approval gates, as one set of controls rendered by *both* editors of the pipeline object: the wizard's Model step (`WizardStepModel`) and `PipelineFormModal`'s `isEdit` branch. Shared rather than duplicated because when each editor grew its own section the modal rendered only two of the three, so an operator editing there could not see or change `always_approve_schema_changes` and nothing indicated it existed. Putting the controls in only one editor was rejected as the other option, because it leaves the second editor's hydration as dead code. It deliberately renders **no** confidence-threshold Slider: that key has no reader, so a control for it would change nothing and, on the edit path, would write a governance number nobody chose. Read its header comment before extending it. |
| `BulkActionBar` | Selection-driven bulk action toolbar (archive, delete) |
| `BulkConfirmModal` | Confirmation modal for a bulk archive or delete |
| `DataPreviewTable` | Tabular preview of sampled source or target rows |
| `FileDetailModal` | Per-file metadata modal opened from `FilesPage` |
| `GlueJobLogViewer` | CloudWatch log stream viewer for a Glue job run |
| `LoadOutcomeIndicator` | `StatusIndicator` form of a run's load outcome |
| `LoadOutcomeLabel` | Text form of a run's load outcome (what actually reached the target) |
| `MappingEditorSection` | Container wrapping the mapping viewer and drag-drop editor |
| `MappingVersionNav` | Previous/next navigation across mapping versions |
| `NotificationBell` | Header notification bell with unread count |
| `PipelineCostPanel` | Per-pipeline cost breakdown panel |
| `PipelineSortControls` | Sort and filter controls above the pipeline card grid |
| `QualitySettingsFields` | The per-pipeline `quality_config` controls, shared by the same two editors as `ApprovalGatesFields` for the same reason. The data-quality gate is a three-option `Select` (`Template default` / `On` / `Off`), **not a Toggle**, because the setting has three states and the third is not a synonym for off. It is on for an S3 Tables target and off for the other four, so offering only two positions would make an editor write an explicit opt-out onto a pipeline that had DQ by template default. Also exports `hydrateQualityConfig`, the record→state reader both editors call, which keeps `dataQualityEnabled` as `boolean \| undefined` for that same reason. |
| `QualityVerdictLabel` | Pass/warn/fail label for a data quality verdict |
| `RunNowButton` | Trigger-a-run button with in-flight and disabled states |
| `SchemaDiffViewer` | Side-by-side diff of two schema versions |
| `SourceConfigDisplay` | Read-only source configuration view (sibling of `TargetConfigDisplay`) |
| `ViewToggle` | Card/table view switch for list surfaces |
| `WorksheetSelectionEditor` | Worksheet picker for multi-sheet spreadsheet sources |

**`components/approvals/` (1):**

| Component | Purpose |
|-----------|---------|
| `ApprovalArtifact` | Renders one approval's payload — mapping table, unsourced target fields, confidence, proposed renames. Reads `approval.details` (see `GET /api/approvals/{approval_id}` below); every access is spelled `approval.details ?? {}`. |

**`components/common/` (4)** — cross-cutting primitives, none of them previously documented:

| Component | Purpose |
|-----------|---------|
| `AsyncContent` | Shared loading/error/empty wrapper for a React Query result |
| `ClickableArea` | Accessible clickable wrapper (keyboard + `role`) for card and row targets |
| `CursorPagination` | Next/previous pager over opaque cursors — **not** a numbered pager, because cursor paging cannot address an arbitrary page |
| `ListPagination` | Page-numbered pager for the listings that still count |

**`components/run-detail/` (8)** — the tab bodies and panels of `RunDetailPage`:

| Component | Purpose |
|-----------|---------|
| `RunDetailHeader` | Run title, status, and older/newer run navigation |
| `RunMetadataPanel` | Run metadata block (ids, timings, source, target) |
| `StepFlowTab` | Step Flow tab — hosts `StepFlowVisualization` |
| `AiTracesTab` | AI Traces tab — hosts `AgentTraceTimeline` |
| `ApprovalsTab` | Approvals tab — hosts `ApprovalWorkflowSection` |
| `SchemaTab` | Schema & Mappings tab |
| `CodeTab` | Code tab — hosts `CodeViewer` and `TestResultsViewer` |
| `DataQualityTab` | Data Quality tab — hosts `QualityReportCard` |

## Hooks (Data Layer)

All hooks live in `src/hooks/` and use TanStack React Query for caching, deduplication, and background refetching.

| Hook | Purpose |
|------|---------|
| `usePipelines` / `usePipelineDetail` | Pipeline list and single pipeline data |
| `usePipelineExecutions` | Execution history with 10s polling via `refetchInterval` |
| `useRunDetail` | Run detail with adaptive polling (5s while running, 30s when completed) |
| `useApprovals` / `useApprovalAction` | Approval list and approve/reject mutations |
| `useSchemas` / `useSchemaDetail` | Schema CRUD operations |
| `useMappings` / `useSaveMappingHints` / `useRegenerateMappings` | Mapping data and mutations |
| `useCodeVersion` / `useTestResults` | Code viewer and test results data |
| `useGlueJob` / `useRunGlueJob` | Glue job details and trigger runs |
| `useMetrics` | Dashboard metrics and stats |
| `useArchivedPipelines` / `useArchivePipeline` / `useRestorePipeline` | Archive/restore operations |
| `useCreatePipeline` / `useEditPipeline` | Pipeline mutation operations |
| `useFileDetail` | File metadata |
| `useBedrockModels` / `useS3TablesBuckets` / `useGlueDatabases` | AWS resource lookups for forms |
| `useRdsInstances` / `useRdsClusters` / `useRdsGlueConnections` / `useRdsSecrets` | RDS instance, cluster, Glue-connection and secret discovery, consumed by the **JDBC** source and target forms. Introduced for the P14 RDS Database form, which no longer exists — the hooks survived the form. |
| `useRerunMapping` | Triggers async schema mapping rerun (P15) |
| `useUpdateMappings` | Direct mapping update on pipeline record (P15) |
| `useRunTraces` / `useAgentTrace` | Agent trace data for run detail page (P13a) |
| `useTokenUsageMetrics` | Aggregate token usage and cost trends for metrics page (P13a) |
| `useSelfHealingActivity` | Self-healing activity data for run detail page (P13) |
| `useQualityReport` | Data quality report (P13) |
| `useCostRecommendations` / `useApplyRecommendation` | Cost optimization recommendations (P13) |
| `useChatAgent` | Chat agent message sending and response handling (P13) |
| `useWebSocket` | WebSocket connection for real-time execution updates, with reconnection and polling fallback |
| `useKeyboardShortcuts` | Keyboard navigation (`Cmd+K` for search, `?` for help) |

→ `useRerunMapping` is asynchronous because `dashboard_api`'s timeout is 25 s and a mapping run takes ~30 s — see [LAMBDA-FUNCTIONS.md § dashboard_api](LAMBDA-FUNCTIONS.md#agentic_data_ingestion_dashboard_api).

> **`useRerunMapping` is a name this document invented; no such symbol exists.** The hook that actually
> issues `POST /api/rerun-mapping` is **`useRegenerateMappings`** in `hooks/use-mappings.ts` (the request
> is built by its private `regenerateMappings`). The row and the pointer above are kept under the old
> name because the 25 s consequence they describe is real and is the thing worth knowing; read
> `useRegenerateMappings` wherever they say `useRerunMapping`. Poll progress with
> `useMappingRegenPolling`.

Query keys are centralized in `src/hooks/query-keys.ts` for consistent cache invalidation.

### Hooks the table above omits

**Counting rule:** one exported `use*` symbol in a `.ts` file under `dashboard-react/src/hooks/`,
excluding `index.ts` and `query-keys.ts` (barrel and key factory, no hooks of their own). That rule
yields **77** exported hooks. The table above names 42 hooks of which **41 exist**. The forty-second
is `useRerunMapping`, which does not, per the note above it, so **36 are undocumented**, not the 9
this spec's requirement R13.4 estimated. Re-derive rather than trust the figure:

```bash
grep -hoE 'export (function|const) use[A-Za-z0-9_]+' dashboard-react/src/hooks/use*.ts \
  | grep -oE 'use[A-Za-z0-9_]+' | sort -u | wc -l    # 77
```

41 + 36 = 77, so between them the two tables name every hook that exists and the arithmetic is the
check that they still do. Read that closure as load-bearing: an earlier revision of this section said
78 total and "42 that exist", two errors in opposite directions that cancelled to the right 36 and so
hid each other. If the totals stop closing, one of the two tables has fallen behind the tree.

| Hook | File | Purpose |
|------|------|---------|
| `useStats` | `use-pipelines.ts` | Dashboard-wide statistics (`GET /api/stats`) |
| `useApprovalDetail` | `use-approvals.ts` | One approval with its parsed payload — `GET /api/approvals/{approval_id}`, see below |
| `usePipelinePendingApprovals` | `use-approvals.ts` | Pending approvals narrowed to one pipeline via `?pipeline_id=` |
| `useDeletePipeline` | `use-archive.ts` | Permanent pipeline delete (distinct from archive) |
| `useBulkArchive` / `useBulkDelete` | `use-bulk-operations.ts` | Multi-pipeline archive and delete |
| `useCancelRun` | `use-cancel-run.ts` | Cancels an in-flight run; resolves with `CANCELLED` (stopped) or `CANCELLING` (its Glue job is stopping) |
| `useRetryRun` | `use-run-detail.ts` | Retries a run from a chosen step, **under the original run's id** |
| `useRunNeighbours` | `use-run-neighbours.ts` | Older/newer run for the run-detail prev/next buttons — endpoint-backed, so it works past the edge of page 1 |
| `useRunArtifacts` | `use-code.ts` | Artifacts produced by a run |
| `useDataPreview` | `use-data-preview.ts` | Sampled source/target rows for `DataPreviewTable` |
| `useErrors` | `use-errors.ts` | Centralised error list behind `ErrorsPage` |
| `useUpdates` | `use-updates.ts` | Update/notification feed backing `ActivityFeed` |
| `useNotifications` / `useMarkNotificationRead` | `use-notifications.ts` | Notification list and read-marking for `NotificationBell` |
| `useGlueJobLogs` | `use-glue-job-logs.ts` | CloudWatch log stream for a Glue job run |
| `useJobVersionHistory` / `useRollbackGlueJob` | `use-glue-job.ts` | Glue job version history and rollback |
| `usePipelineCosts` | `use-pipeline-costs.ts` | Per-pipeline cost series for `PipelineCostPanel` |
| `useMappingMemory` / `useDeleteMappingMemoryEntry` | `use-mapping-memory.ts` | AgentCore Memory mapping entries behind `MappingMemoryPage` |
| `useMappingRegenPolling` | `use-mapping-regen-polling.ts` | Polls for completion of an async mapping regeneration |
| `useUploadSchema` / `useDeleteSchema` | `use-schemas.ts` | Schema upload and delete. Delete answers **409** naming the referencing pipelines rather than orphaning them. |
| `useSetDisplayLabel` / `useUpdateFieldContext` | `use-schemas.ts` | Schema display label and per-field context. Submitting an empty value **clears** it by removing the key, never by storing `""`. |
| `useJdbcDetectSchemaStart` / `useJdbcDetectSchemaStatus` | `use-resources.ts` | Start and poll JDBC **source** schema detection |
| `useJdbcDetectTargetSchemaStart` / `useJdbcDetectTargetSchemaStatus` | `use-resources.ts` | Start and poll JDBC **target** schema detection |
| `useJdbcCandidateSecrets` | `use-resources.ts` | Secrets Manager candidates for a JDBC connection. Credentials are referenced **by ARN only** — no plaintext user/password ever leaves this path. |
| `useVpcs` / `useSubnets` / `useSecurityGroups` | `use-resources.ts` | VPC, subnet and security-group lookups for JDBC manual connection mode |
| `useTablesInBucket` / `useTablesInDatabase` | `use-resources.ts` | Table listings for S3 Tables buckets and Glue databases |

## `GET /api/approvals/{approval_id}`

The review modal on `ApprovalsPage` reads one approval's stored payload from a dedicated endpoint. It is
documented here rather than in `LAMBDA-FUNCTIONS.md` because its only consumer is this UI, and because
what a frontend engineer needs from it is the three shapes it can return.

**Why it is its own endpoint and not a wider listing.** Neither listing handler
produced a `details` key, so every reader on the modal correctly reported "not established" for the
mapping count, the unsourced target fields, the confidence and the proposed renames. Parsing the payload
in the listing instead was rejected: `get_approvals` unfiltered enumerates all four statuses through an
uncapped paginator, so parsing there would ship **every** approval's full `proposed_mappings` on every
page load in order to serve a modal opened for one row. The repo had already settled that shape when it
moved per-row schema field resolution off the listing onto `GET /api/schemas/{id}`, because a paginated
listing that also resolves per-row payloads makes cost a function of an unmeasured per-row
read. The cost belongs on the request that needs the payload.

**Three published payload states.** The response always carries a `details_status`
discriminator, and `details` is **omitted** — not `null`, not `{}` — on the two non-parsed states:

| `details_status` | `details` | Means |
|---|---|---|
| `parsed` | present | The attribute was there and decoded to an object. It may be `{}`, and it may be structurally complete with every value `null`; both are facts the **producer** established, and the field readers report them. An all-`null` live payload is deliberately `parsed`. |
| `absent` | omitted | No attribute, or an empty one. The producer stored nothing. |
| `unreadable` | omitted | An attribute was stored and we could not interpret it. Anything decoding to a non-object — a list, a number, a string, a literal `null` — is `unreadable` under one rule with no special cases. |

**Do not collapse `absent` and `unreadable`.** "The producer stored nothing" and "we could not read what
it stored" are different facts, and an approver acting on the second while shown the first signs off
believing they saw the truth about a mapping. `details: null` was rejected for a concrete reason: all
four existing read sites in `components/approvals/ApprovalArtifact.tsx` spell their access
`approval.details ?? {}`, and `??` catches `null` and `undefined` alike — so a nullable `details` would
be indistinguishable from an absent one at every reader, which is exactly the collapse the field exists
to prevent. Keeping `details` shaped as those readers already expect is also what let every one of them
be reused rather than re-implemented.

Other response notes: a missing approval is a **404** naming the id, matching what the approve and
reject endpoints already answer for the same input. The raw `approval_data` attribute is **not** echoed
back — `details` supersedes it and a `schema_mapping` payload runs to ~6 KB. `decided_by`,
`decided_by_email`, `decided_at` and `comment` are deliberately **not** synthesised here; that collapse
lives once, on `get_run_details`' read path, which is what feeds `ApprovalWorkflowSection`.

Hook: `useApprovalDetail` (`hooks/use-approvals.ts`).

## Metrics tile labels

Two tiles on `MetricsDashboardPage` report **rows loaded into targets**.

| Tile label | Wire field |
|---|---|
| **Rows Loaded** | `performance.lines_ingested_24h` |
| **Rows Loaded per Hour** | `performance.processing_rate` |

**What is counted.** After its target write, every generated Glue job writes a `load_metrics` state row
with `rows_read`, `rows_written` and `write_confirmed`. The tile sums `rows_written` over the rows in the
selected period where `write_confirmed` is true, for every source type, JDBC included. A run whose
source was empty writes a row with 0 and counts as a measured 0.

**What is not counted.** Runs that stopped before Glue (a rejected mapping, a failed code test) wrote no
row and add nothing. Neither do runs from before the jobs recorded counts. With no `load_metrics` row in
the period the tile reads **Not established** rather than 0.

**Rows Loaded per Hour** is the same count divided by the **wall-clock hours in the selected period**,
not by pipeline running time, so it is an average over the period rather than the speed of a run.

**History.** Until 2026-10-10 these tiles were *Records Accepted (estimated)*: file size divided by a
fixed per-format guess, summed over `file_validation` events. That counted re-runs again, credited runs
that never reached Glue, and could say nothing about JDBC sources, which have no file. The wire fields
keep their historical names and the `testId`s are unchanged, so neither is evidence about what the
number means; the `measurement` block in the response says what it is.

## Running Locally

The local dev server runs the React app with Vite HMR and proxies `/api/*` to the deployed **CloudFront** URL. There is **no local signing proxy** — after the move to Cognito auth, the old Flask/SigV4 `localhost:8080` proxy was removed. Auth works against the real Cognito Hosted UI, and CloudFront (not raw API Gateway) must be the proxy target because the regional WAF blocks direct `execute-api` access.

### Prerequisites

- Node.js 18+
- A deployed Agentic Data Ingestion environment (you proxy to its CloudFront URL)
- `http://localhost:5173` registered in the Cognito app client's allowed callback URLs

### Setup

```bash
cd dashboard-react
npm install
```

Create `dashboard-react/.env` with the Cognito and proxy settings (see `docs/LOCAL_DEVELOPMENT.md` for the full list):

```bash
VITE_USER_POOL_ID=us-west-2_xxxxxxxxx
VITE_USER_POOL_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
VITE_HOSTED_UI_DOMAIN=agentic-data-ingestion-<account-id>-dev.auth.us-west-2.amazoncognito.com
# Proxy target MUST be the CloudFront URL, not raw execute-api (WAF blocks direct access)
VITE_API_PROXY_TARGET=https://d1234abcdef8.cloudfront.net
```

### Start the Dev Server

```bash
npm run dev
# Vite dev server on http://localhost:5173, proxies /api/* to VITE_API_PROXY_TARGET
```

Sign in through the Cognito Hosted UI when prompted. For the complete local-dev guide (callback URLs, WebSocket setup, and troubleshooting), see `docs/LOCAL_DEVELOPMENT.md`.

## Building for Production

```bash
npm run build
```

This runs `tsc -b` (type checking) then `vite build`. Output goes to `dist/`. Note that `cdk deploy` builds and deploys the dashboard automatically via asset bundling — a manual build is only needed for local inspection or `npm run preview`.

To preview the production build locally:

```bash
npm run preview
```

## Testing

```bash
# Run all tests once
npm test

# Watch mode (for development)
npm run test:watch
```

Tests use Vitest with jsdom environment, React Testing Library for component tests, and fast-check for property-based tests. Test setup is in `src/test-setup.ts`.

Test files are co-located with their source:
- `src/__tests__/` — API client and integration tests
- `src/components/__tests__/` — Component property tests
- `src/hooks/__tests__/` — Hook tests
- `src/pages/__tests__/` — Page-level tests
- `src/utils/__tests__/` — Utility function tests

## Key Patterns

### Data Fetching with React Query

All API calls go through TanStack React Query hooks. The `QueryClient` is configured with a 30-second `staleTime` and `refetchOnWindowFocus` disabled. Each hook defines its own query key (from `query-keys.ts`) and fetch function using the shared `ApiClient` (`src/services/api-client.ts`).

#### ApiClient Typed Methods

The `ApiClient` provides typed methods that handle Lambda proxy response unwrapping centrally:

| Method | Signature | Description |
|--------|-----------|-------------|
| `get<T>` | `get<T>(url: string): Promise<T>` | GET request, returns parsed JSON directly |
| `post<T>` | `post<T>(url: string, body: unknown): Promise<T>` | POST with JSON body |
| `put<T>` | `put<T>(url: string, body: unknown): Promise<T>` | PUT with JSON body |
| `delete<T>` | `delete<T>(url: string): Promise<T>` | DELETE request |
| `requestRaw` | `requestRaw(url: string, options?: RequestInit): Promise<Response>` | Low-level fetch returning raw `Response` — use for file uploads or streaming |

The typed methods handle three response shapes automatically:
1. `{ statusCode: 200, body: "{...}" }` — Lambda proxy with string body (parsed)
2. `{ statusCode: 200, body: {...} }` — Lambda proxy with object body (returned as-is)
3. Plain JSON — non-proxy response (returned as-is)

On `statusCode >= 400`, methods throw an `ApiError` with `status`, `body`, and `message` properties. A global `QueryCache.onError` handler shows toast notifications for `ApiError` instances via the `global-toast.ts` bridge module.

In-flight request deduplication is preserved — concurrent identical requests share a single fetch.

#### PipelineContext

The `PipelineContext` (`src/contexts/PipelineContext.tsx`) provides pipeline data to all sub-pages under `PipelineLayout`. It exposes `pipeline`, `isLoading`, `isError`, and `refetch`. Sub-pages access it via the `usePipelineContext()` hook — no need to re-fetch pipeline data in each sub-page.

### Real-Time Updates (WebSocket + Polling Fallback)

The dashboard receives execution status updates in real-time via an API Gateway WebSocket API. When a pipeline execution step completes, fails, or triggers an approval, the update arrives in the browser within ~1 second — no polling delay.

#### `useWebSocket` Hook

The `useWebSocket` hook in `src/hooks/useWebSocket.ts` manages the WebSocket lifecycle:

```typescript
const { isConnected, isFallbackPolling } = useWebSocket({
  pipelineIds: ['pipe-abc', 'pipe-xyz'],  // subscribe to specific pipelines
  enabled: true,
});
```

On mount, the hook:
1. Connects to the WebSocket URL (from runtime `config.json` via `getConfig().wsUrl` in the deployed app; `VITE_WS_URL` as a local-dev fallback)
2. Sends a `subscribe` message with the pipeline IDs to watch
3. Receives `execution_update` messages and updates React Query cache via `queryClient.setQueryData()`
4. Also calls `queryClient.invalidateQueries()` for a background refetch of full data

#### Automatic Reconnection

If the WebSocket disconnects, the hook reconnects with exponential backoff:
- Retry 1: 1 second delay
- Retry 2: 2 seconds
- Retry 3: 4 seconds
- Retry 4+: 8 seconds (capped at 30 seconds)

After 3 consecutive failed retries, the hook falls back to polling mode (`isFallbackPolling: true`).

#### Polling Fallback

Polling serves as a safety net, not the primary update mechanism:
- **WebSocket connected**: `refetchInterval` increases to 60 seconds (background safety net)
- **WebSocket disconnected / fallback**: `refetchInterval` drops to 10 seconds (active polling)

This applies to `usePipelineExecutions`. Other hooks (`useRunDetail`, `useApprovals`) retain their original polling intervals.

#### Configuration

In the deployed dashboard, the WebSocket URL is read from the runtime `config.json` (`getConfig().wsUrl`) that CDK writes to S3 at deploy time — it is **not** baked in at build time. For local development, set `VITE_WS_URL` in `dashboard-react/.env` as a fallback:

```bash
VITE_WS_URL=wss://xxxxxxxxxx.execute-api.us-west-2.amazonaws.com/prod
```

The deployed value originates from the CDK WebSocket endpoint output (`WsApiEndpoint`). If neither the runtime config nor `VITE_WS_URL` provides a URL, the hook immediately falls back to polling.

### Live Polling (Legacy)

Execution-related hooks also use React Query's `refetchInterval` as a fallback:
- **Pipeline executions** (`usePipelineExecutions`): 60s when WebSocket connected, 10s otherwise
- **Run detail** (`useRunDetail`): adaptive polling — 5s while the run is active, 30s once completed
- **Approvals** (`useApprovals`): 30s fixed

Polling is automatic and stops when the component unmounts.

### Routing

React Router v6 with nested routes. The `AppLayout` (Cloudscape) provides the side navigation shell, and `<Routes>` renders the active page in the content area.

Pipeline sub-pages use nested routes under `PipelineLayout`:

```
/pipelines/:id              → PipelineLayout > PipelineOverviewPage (index)
/pipelines/:id/schema       → PipelineLayout > SchemaHistoryPage
/pipelines/:id/code         → PipelineLayout > CodePage
/pipelines/:id/executions   → PipelineLayout > ExecutionsPage
/pipelines/:id/files        → PipelineLayout > FilesPage
```

`PipelineLayout` renders Cloudscape `Tabs` with the active tab derived from the URL. Tab clicks navigate via React Router (URL-driven, not local state), so browser back/forward works correctly.

The `/pipelines/:id/runs/:runId` and `/pipelines/:id/glue-job/:jobName` routes are siblings (not nested under PipelineLayout) because they have their own full-page layouts.

### Dark Mode

Theme preference is persisted in `localStorage` and applied on load via Cloudscape's `applyMode()`. A toggle in the side navigation switches between light and dark modes.

### Toast Notifications

A `ToastContext` provider wraps the app and exposes a toast API for success/error/info notifications across all pages.

### Path Aliases

`@/*` maps to `src/*` (configured in both `tsconfig.json` and `vite.config.ts`).


## Phase 5 Features (2026-02-23)

### Create Pipeline Wizard

Route: `/pipelines/new`

A full-page Cloudscape `Wizard` component replaces the previous modal-based pipeline creation flow. The table below is **one row per entry in the `steps={[…]}` array in `CreatePipelinePage.tsx`**, in order, so it is the enumeration rather than a summary of one; the step count itself is held by [ARCHITECTURE.md § System Inventory](ARCHITECTURE.md#system-inventory):

| Step | Component | Description |
|------|-----------|-------------|
| 1. Name & Description | `WizardStepName` | Pipeline name (required, validated) and optional description |
| 2. Source | `WizardStepSource` | Source type selector (`S3_UPLOAD` / `JDBC`), JDBC connection and watermark configuration, source schema detection |
| 3. Target Destination | `WizardStepTarget` | Target type selector (S3 Table Bucket / JDBC / S3 Parquet), dynamic connection form per type, "Detect Schema" button for S3 Tables (and a detect panel for JDBC) |
| 4. Target Schema | `WizardStepSchema` | Auto-populated from step 3 if detected, JSON/CSV upload via `SchemaUploadWizard`, or select a previously uploaded schema. The Existing list leaves out Glue Data Catalog and S3 Tables tables, which `/api/schemas` also returns: a pipeline created from one got no target-schema object, so code generation and testing ran without a target schema |
| 5. AI Model & Options | `WizardStepModel` | AI model selector with cost/quality display, then the shared `ApprovalGatesFields` (three approval gates: schema mapping, code generation, schema changes), a confidence-threshold Slider the step keeps for itself, and the shared `QualitySettingsFields` (data-quality posture and row dedup) |
| 6. Review & Create | `WizardStepReview` | Full summary of all inputs, estimated cost per run, create button |

**There are four target types, not five.** `RDS_DATABASE` is a **legacy alias**, not a fifth type: P17
task 20.4 consolidated it into `jdbc`, `'rds_database'` was dropped from the `targetType` union in
`pages/wizard/types.ts`, and the `TargetForm` dispatcher in `pages/wizard/TargetForms.tsx` has exactly
four `case` arms (`s3_tables`, `glue_catalog`, `jdbc`, `s3_parquet`). Persisted pipelines whose stored
`target_config.type` is `"RDS_DATABASE"` are **normalised onto `jdbc` on read** for the edit flow. Do
not re-add it to this list.

**The selector lists three of the four.** `glue_catalog` is no longer offered for a new pipeline:
`offeredTargetTypeOptions` in `WizardStepTarget.tsx` drops it unless it is already the selected value,
and `POST /pipelines` refuses `GLUE_CATALOG` with a 400. Its `case` arm, `GlueCatalogForm` and the
detect-schema support stay, so a wizard state hydrated from an existing Glue Catalog pipeline still
renders.

Wizard state is managed via `WizardState` interface in `CreatePipelinePage.tsx`. Each step validates inputs before allowing navigation to the next step. On submit, the wizard POSTs to `/api/pipelines` with `approval_config` included.

Schema auto-detection calls `GET /api/resources/detect-schema` with the target config. For Glue Catalog targets, it reads the table schema via `glue:GetTable`. For S3 Tables targets, it reads the Iceberg schema via `s3tables:GetTable`.

Wizard step components live in `src/pages/wizard/`.

### File Upload with Progress Tracking

The `FileUploadModal` component uses a presigned S3 POST URL flow:

1. Frontend calls `POST /api/upload/presign` with `pipeline_id` and `file_name`
2. Backend generates a presigned POST URL (1-hour expiry, 500MB size limit)
3. Frontend uploads via `XMLHttpRequest` with `upload.onprogress` for real-time tracking
4. Cloudscape `ProgressBar` shows percentage, bytes transferred (e.g., "2.4 MB / 5.1 MB"), and estimated time remaining
5. "Cancel" button calls `xhr.abort()` to stop the upload and reset modal state
6. Pre-upload validation displays file format, size, and record count estimate

### Schema Upload with Preview

The `SchemaUploadWizard` component provides drag-drop schema upload with inline preview:

1. Cloudscape `FileUpload` accepts `.json` and `.csv` files
2. Client-side parsing via `schema-parser.ts` utilities (`parseJsonSchema`, `parseCsvSchema`, `inferTypeFromSamples`)
3. Preview `Table` displays field names, types, nullable flags
4. Inline editing — users can change field types and add descriptions before upload
5. Structure validation — rejects files without `properties` or array-of-objects structure
6. "Use This Schema" button passes the finalized schema to the parent component

Supported formats:
- JSON Schema with `properties` object
- Array of objects (infers schema from first item)
- CSV (column names from header, types inferred from first 5 data rows)


## Phase 13 Features — Agentic Capabilities

Phase 13 adds agent-driven UI sections across multiple pages and a new Chat page for natural language pipeline creation.

### Chat Page (`/chat`)

Route: `/chat`

A Cloudscape-styled chat interface for the Natural Language Pipeline Builder agent. Users describe a pipeline in plain English and the agent proposes a configuration.

**This page is a partial build, and the gap is on the client side.** Three things this section used to
claim as working are not: there is no streaming and there is no conversation continuity. What *does* work is a single
request/response round trip that renders a read-only proposal. The paragraphs below say which is which
per element, because a reader extending this page needs to know what exists before deciding what to
add. For the agent's own contract (its payload shapes and its `proposal` response), see
[AGENTS.md § 5 Natural Language Pipeline Builder](AGENTS.md#5-natural-language-pipeline-builder) rather
than a second copy here.

**Navigation: there is none.** `NAV_ITEMS` in `App.tsx` contains Pipelines, Approvals, Schemas,
Archived, Metrics, Errors and Help, and no Chat entry. The `/chat` `<Route>` is registered in the same
file, so the page renders when the URL is entered by hand, but nothing in the shell links to it. This
is the whole truth of it: the link was never hidden behind a flag or a permission check, so do not
look for the condition that hides it.

**Components:**
- `ChatPage.tsx` — Full-page chat layout: message list, text `Input`, and a `Spinner` labelled "Thinking…" while a request is in flight. There is no streaming display; the spinner is replaced by the finished message when the POST resolves.
- `PipelineProposalCard.tsx` — Read-only proposal rendering (see [Key Components](#key-components)). Nothing on it is editable.

**Hooks:**
- `useChatAgent()` (`src/hooks/use-chat-agent.ts`) — Holds the message array and issues one `apiClient.post('/chat', { message })` per turn. It opens no WebSocket, and it sends no `conversation_id`. That field does not appear anywhere in `dashboard-react/src/`, so the handler forwards an empty string and each turn reaches the agent with no memory of the previous one. It reads `reasoning_steps` and `proposal` off the top level of the response, falling back to `response` for the bubble text.

**Behavior, as built:**
1. User types a natural language description (e.g., "Load customer data from s3://bucket/customers/ into an Iceberg table partitioned by region").
2. One POST goes out and the page shows a spinner until it returns. The joined `reasoning_steps` then appear all at once as the assistant's message, which reads like a transcript of streamed steps but is not one.
3. If the response carried a `proposal`, a `PipelineProposalCard` renders below that message. The endpoint does forward it: `AGENT_RESULT_PASSTHROUGH` in `handlers/agents.py` includes `proposal`, and `handle_chat_message` copies each of its fields through only when the agent supplied it.
4. There is no edit step. The card displays the proposal and offers the button.
5. "Create Pipeline" calls `ChatPage`'s `handleCreatePipeline`, which POSTs the proposal's five fields to **`/api/pipelines`**, the ordinary create endpoint the wizard uses. This is the only creation path; chat never creates.

> **`/chat` deliberately does not carry `confirm` or `proposal` (threat model T-05).** The handler
> forwards only `message` and `conversation_id`, and the agent has no pipeline write path, so a
> proposal becomes a pipeline only through `POST /pipelines` and its input validation.
> [AGENTS.md § 5](AGENTS.md#5-natural-language-pipeline-builder) states the same thing from the
> agent's side.

### Self-Healing Activity (RunDetailPage)

A new "Self-Healing" container section on the RunDetailPage, shown below the step flow visualization when a self-healing event exists for the run.

**Hook:** `useSelfHealingActivity(runId)` (`src/hooks/use-self-healing.ts`) — Fetches `GET /api/pipelines/{id}/runs/{runId}/self-healing`

**Display:**
- Error classification badge (e.g., `schema_mismatch`, `oom_error`, `type_cast_error`)
- Remediation description text
- Timeline: `Error detected → Diagnosed (3.2s) → Fix applied → Re-run started → Re-run succeeded`
- StatusIndicator: green (fixed), yellow (re-running), red (fix failed)
- Link to the re-run detail page (`rerun_id`)

**Conditional rendering:** The section only appears when the self-healing API returns data for the run.

### Data Quality Report (RunDetailPage)

A new "Data Quality" container section on the RunDetailPage, shown after the step flow when a quality report exists.

**Hook:** `useQualityReport(runId)` (`src/hooks/use-quality-report.ts`)
- GET: `GET /api/pipelines/{id}/runs/{runId}/quality-report`

**Component:** `QualityReportCard.tsx` (`src/components/QualityReportCard.tsx`) — Scorecard component with expandable check details.

**Display:**
- Overall status badge (pass/warning/fail)
- Per-check scorecard with color-coded StatusIndicators:
  - `row_count` — expected vs actual count
  - `null_rate_{column}` — rate vs threshold, with sample null rows on expand
  - `duplicate_keys` — count with sample duplicated values
  - `freshness` — latest record timestamp vs max age
- Root cause classification badge (data_issue / transform_issue / infrastructure_issue)
- Recommendations list
- No "Run Quality Check" button. The on-demand agent check was removed on 2026-10-11 because it could only write a failing report over the inline one ([AGENTS.md § 3](AGENTS.md))

### Cost Recommendations (MetricsDashboardPage)

A new "Cost Recommendations" section on the MetricsDashboardPage showing right-sizing recommendations from the weekly cost optimization agent.

**Hooks:**
- `useCostRecommendations()` (`src/hooks/use-cost-recommendations.ts`) — Fetches `GET /api/cost-recommendations`
- `useApplyRecommendation()` — Calls `POST /api/cost-recommendations/{id}/apply`

**Display:**
- Per-pipeline recommendation cards:
  - Current config → Recommended config (worker type, worker count)
  - Projected monthly savings
  - Risk badge (low/medium)
  - DPU utilization percentage
- "Apply" button for low-risk changes (with confirmation modal)
- Idle pipeline cards: last run date, idle days, "Archive" suggestion
- Total projected monthly savings summary at the top

### Evidence Cards and Questions (ApprovalWorkflowSection)

Enhanced approval view in `ApprovalWorkflowSection.tsx` when the complex mapping agent has provided evidence-based mappings.

**Evidence Cards (per field):**
- Sample values from source data (e.g., `["123 Main St", "456 Oak Ave"]`)
- Agent reasoning text explaining the mapping decision
- Confidence before/after comparison (e.g., 0.62 → 0.94)
- Memory indicator: "Influenced by N previous approved mappings"

**Questions Section:**
- Specific questions for low-confidence fields with sample data excerpts
- Suggested options as selectable buttons (e.g., `product_category` | `region_code` | `unmapped`)
- Current confidence score per question

### New Hooks Summary (Phase 13)

| Hook | File | Purpose |
|------|------|---------|
| `useSelfHealingActivity` | `src/hooks/use-self-healing.ts` | Self-healing data for a run |
| `useQualityReport` | `src/hooks/use-quality-report.ts` | Quality report data for a run |
| `useCostRecommendations` | `src/hooks/use-cost-recommendations.ts` | Cost optimization recommendations |
| `useApplyRecommendation` | `src/hooks/use-cost-recommendations.ts` | Apply a cost recommendation |
| `useChatAgent` | `src/hooks/use-chat-agent.ts` | One `POST /api/chat` per turn. No streaming, no `conversation_id` |

### New Components Summary (Phase 13)

| Component | File | Purpose |
|-----------|------|---------|
| `ChatPage` | `src/pages/ChatPage.tsx` | NL pipeline builder chat interface. No nav link reaches it |
| `PipelineProposalCard` | `src/components/PipelineProposalCard.tsx` | Read-only pipeline proposal; its create button posts to `/api/pipelines`, not to the agent |
| `QualityReportCard` | `src/components/QualityReportCard.tsx` | Data quality scorecard with expandable checks |
