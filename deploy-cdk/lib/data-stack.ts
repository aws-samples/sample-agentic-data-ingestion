// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { Construct } from 'constructs';

export interface DataStackProps extends cdk.StackProps {
  environment: string;
}

export class DataStack extends cdk.Stack {
  public readonly stateTable: dynamodb.Table;
  public readonly pipelinesTable: dynamodb.Table;
  public readonly schemaMappingsTable: dynamodb.Table;
  public readonly generatedCodeTable: dynamodb.Table;
  public readonly testResultsTable: dynamodb.Table;
  public readonly approvalsTable: dynamodb.Table;
  public readonly notificationsTable: dynamodb.Table;
  public readonly executionsTable: dynamodb.Table;
  public readonly taskTokensTable: dynamodb.Table;
  public readonly wsConnectionsTable: dynamodb.Table;
  public readonly errorsTable: dynamodb.Table;
  public readonly pipelineLocksTable: dynamodb.Table;
  public readonly processedFilesTable: dynamodb.Table;
  public readonly auditLogTable: dynamodb.Table;
  public readonly agentTracesTable: dynamodb.Table;
  public readonly watermarksTable: dynamodb.Table;
  public readonly modelCallBudgetTable: dynamodb.Table;
  public readonly eventBus: events.EventBus;
  public readonly deadLetterQueue: sqs.Queue;

  constructor(scope: Construct, id: string, props: DataStackProps) {
    super(scope, id, { ...props, terminationProtection: true });

    const { environment } = props;
    const tableDefaults: Partial<dynamodb.TableProps> = {
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    };

    // ── State Table (3 GSIs) ──────────────────────────────────────
    this.stateTable = new dynamodb.Table(this, 'StateTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_state',
      partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'step_name', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_IMAGE,
    });
    this.stateTable.addGlobalSecondaryIndex({
      indexName: 'PipelineIndex',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    this.stateTable.addGlobalSecondaryIndex({
      indexName: 'GlueJobRunIndex',
      partitionKey: { name: 'glue_job_run_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // P28 R17.1/17.2: dashboard read paths (metrics stage counts / lines
    // ingested, glue_jobs schema-version lookups) query step rows by
    // step_name within a time window instead of scanning the table. The
    // step-name value space is the small closed set of pipeline steps, so
    // one Query per step is bounded. Sparse by design: rows without a
    // `timestamp` attribute (e.g. the metrics_cache row) never enter the
    // index — matching the previous scan's client-side "unparseable
    // timestamp → skip" behavior.
    // Deploy note: DynamoDB allows only ONE GSI creation per table per
    // stack update — do not add another state-table GSI in the same deploy.
    this.stateTable.addGlobalSecondaryIndex({
      indexName: 'step_name-timestamp-index',
      partitionKey: { name: 'step_name', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── Pipelines Table (2 GSIs) ───────────────────────────────────
    this.pipelinesTable = new dynamodb.Table(this, 'PipelinesTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_pipelines',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    });
    this.pipelinesTable.addGlobalSecondaryIndex({ indexName: 'status-created_at-index', partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    this.pipelinesTable.addGlobalSecondaryIndex({ indexName: 'target_schema_name-index', partitionKey: { name: 'target_schema_name', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Schema Mappings Table (3 GSIs) ─────────────────────────────
    this.schemaMappingsTable = new dynamodb.Table(this, 'SchemaMappingsTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_schema_mappings',
      partitionKey: { name: 'mapping_id', type: dynamodb.AttributeType.STRING },
    });
    this.schemaMappingsTable.addGlobalSecondaryIndex({ indexName: 'source-schema-index', partitionKey: { name: 'source_schema_hash', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_timestamp', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    this.schemaMappingsTable.addGlobalSecondaryIndex({ indexName: 'target-schema-index', partitionKey: { name: 'target_schema_hash', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_timestamp', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    this.schemaMappingsTable.addGlobalSecondaryIndex({ indexName: 'schema-pair-index', partitionKey: { name: 'source_schema_hash', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'target_schema_hash', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Generated Code Table (2 GSIs + Streams) ────────────────────
    this.generatedCodeTable = new dynamodb.Table(this, 'GeneratedCodeTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_generated_code',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.generatedCodeTable.addGlobalSecondaryIndex({ indexName: 'pipeline_id-index', partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'generation_timestamp', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    this.generatedCodeTable.addGlobalSecondaryIndex({ indexName: 'run_id-index', partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'generation_timestamp', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Test Results Table (1 GSI + Streams) ───────────────────────
    this.testResultsTable = new dynamodb.Table(this, 'TestResultsTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_test_results',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
    this.testResultsTable.addGlobalSecondaryIndex({ indexName: 'test_status-index', partitionKey: { name: 'test_status', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'test_timestamp', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Approvals Table (4 GSIs + TTL) ─────────────────────────────
    this.approvalsTable = new dynamodb.Table(this, 'ApprovalsTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_approvals',
      partitionKey: { name: 'approval_id', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });
    this.approvalsTable.addGlobalSecondaryIndex({ indexName: 'pipeline-status-index', partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'status', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    // status-created-index is the R17 "approvals status GSI": the design named it
    // status-created_timestamp-index, but approval rows carry `created_at` (written
    // by the approval_handler), and this index (status, created_at) already serves
    // that access pattern — dashboard status-filtered listings query it instead of
    // scanning (P28 R17.1).
    this.approvalsTable.addGlobalSecondaryIndex({ indexName: 'status-created-index', partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    this.approvalsTable.addGlobalSecondaryIndex({ indexName: 'approval-type-created-index', partitionKey: { name: 'approval_type', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });
    // P28 R17.2: per-run approval lookups (dashboard run details / pending-approval
    // checks) query by run_id instead of scanning inside a per-run loop (the old
    // N+1 scan in pipelines_runs.py). PK-only on purpose: an approval row that
    // lacks a created_at must still be visible to the run-details listing, and a
    // run only ever has a handful of approvals, so no sort key is needed.
    // Deploy note: DynamoDB allows only ONE GSI creation per table per stack
    // update — do not add another approvals-table GSI in the same deploy.
    this.approvalsTable.addGlobalSecondaryIndex({ indexName: 'run_id-index', partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Notifications Table (1 GSI + TTL) ─────────────────────────
    this.notificationsTable = new dynamodb.Table(this, 'NotificationsTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_notifications',
      partitionKey: { name: 'notification_id', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });
    this.notificationsTable.addGlobalSecondaryIndex({ indexName: 'PriorityIndex', partitionKey: { name: 'priority', type: dynamodb.AttributeType.STRING }, sortKey: { name: 'created_at', type: dynamodb.AttributeType.STRING }, projectionType: dynamodb.ProjectionType.ALL });

    // ── Executions Summary Table (3 GSIs + Streams + TTL) ───────────
    this.executionsTable = new dynamodb.Table(this, 'ExecutionsTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_executions',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_IMAGE,
      timeToLiveAttribute: 'ttl',
    });
    this.executionsTable.addGlobalSecondaryIndex({
      indexName: 'StatusIndex',
      partitionKey: { name: 'status', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'started_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // P28 R17.2: run_id is this table's SORT key, so a run-only lookup
    // (cancel_run's recovery path when the state table carries no
    // pipeline_id for the run) has no key-bounded access path without this
    // index — it was the last remaining dashboard scan. PK-only: the lookup
    // needs just the table keys back, and a run_id maps to a single
    // execution row.
    this.executionsTable.addGlobalSecondaryIndex({
      indexName: 'run_id-index',
      partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
    // p48 R5.2: run history had no chronological sort key. The table's sort
    // key is `run_id`, and three id prefixes share it (`queued-` < `run-` <
    // `skipped-`), so a descending scan groups BY KIND and is chronological
    // only within the `run-` group — every duplicate skip pins above every
    // run permanently, and the queued placeholder sorts last. This index is
    // what lets `pipelines_runs.get_pipeline_executions` delegate ordering to
    // DynamoDB instead of ordering by id and calling it "newest first".
    //
    // This REVERSES the position recorded in LGR-9a35b2d-3, whose scope says
    // verbatim: "Explicitly NOT deploy-cdk/lib/data-stack.ts: no GSI added,
    // none altered." That decline (and LGR-9a35b2d-5's, for the pipelines
    // listing) was about preferring a bound where a bound would do. Here a
    // bound will not do: an in-memory sort can only order what it has read,
    // so paging past its own ceiling is impossible by construction.
    //
    // ProjectionType.ALL is REQUIRED, not preferred: the read consumes
    // status, file_name, source_key, run_id, schema_hash, pending_approval_id,
    // pending_approval_type and current_step. KEYS_ONLY or INCLUDE would force
    // a second read per row, reintroducing the N+1 the enrichment avoids.
    //
    // SPARSE, and that is the accepted cost (p48 R9): a GSI indexes only items
    // carrying BOTH key attributes, so a row written without `started_at` is
    // absent from this index and invisible in run history with no error. Do
    // NOT lean on that as a filter (p48 do-NOT 2) — the phantom-row filter
    // stays where it is, and a missing `started_at` is a defect to detect.
    //
    // Deploy note: DynamoDB allows only ONE GSI creation per table per stack
    // update. This table now carries THREE — StatusIndex, run_id-index and
    // RunsByTimeIndex — so a fourth must be deployed on its own, and no two
    // of these may be created in the same stack update.
    this.executionsTable.addGlobalSecondaryIndex({
      indexName: 'RunsByTimeIndex',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'started_at', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── Task Tokens Table (TTL-enabled) ──────────────────────────
    this.taskTokensTable = new dynamodb.Table(this, 'TaskTokensTable', {
      tableName: 'agentic_data_ingestion_task_tokens',
      partitionKey: { name: 'approval_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── WebSocket Connections Table (1 GSI + TTL) ──────────────────
    this.wsConnectionsTable = new dynamodb.Table(this, 'WsConnectionsTable', {
      tableName: 'agentic_data_ingestion_ws_connections',
      partitionKey: { name: 'connection_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.wsConnectionsTable.addGlobalSecondaryIndex({
      indexName: 'PipelineIndex',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── Errors Table (TTL-enabled) ─────────────────────────────────
    this.errorsTable = new dynamodb.Table(this, 'ErrorsTable', {
      tableName: 'agentic_data_ingestion_errors',
      partitionKey: { name: 'error_date', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    new cdk.CfnOutput(this, 'ErrorsTableName', {
      value: this.errorsTable.tableName,
      description: 'Errors tracking DynamoDB table name',
    });

    // ── Pipeline Locks Table (TTL-enabled) ─────────────────────────
    this.pipelineLocksTable = new dynamodb.Table(this, 'PipelineLocksTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_pipeline_locks',
      partitionKey: { name: 'pipeline_prefix', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });

    // ── Processed Files Table — Processed_File_Ledger (TTL-enabled) ─
    // p30 R9.1. Keyed by pipeline + content fingerprint (size + normalised S3
    // ETag): the same content submitted to two pipelines is two legitimate
    // ingestions, so the pipeline is part of the KEY, not a filter. Written by
    // job_completion on the Glue SUCCEEDED callback, read by pipeline_trigger
    // before it starts a run. See shared/processed_file_ledger.py.
    //
    // WHY A TABLE RATHER THAN A RUN_ID PREFIX ON THE EXECUTIONS TABLE (design
    // C8 states the opposite preference; this is the deliberate deviation):
    // the executions table is not a plain key-value store. It carries a
    // DynamoDB Stream that drives the WebSocket broadcast Lambda
    // (compute-stack.ts, `wsBroadcastLambda.addEventSource`), a StatusIndex GSI
    // partitioned on `status`, and a run-history read path
    // (`pipelines_runs.get_pipeline_executions`) that treats every row whose
    // status is not QUEUED as a run and pages it with a server-side Limit. A
    // ledger row there would broadcast a dashboard event per successful run,
    // enter or perturb the status GSI, and render as a phantom run unless three
    // separate readers learned to ignore it. C8's actual hazard — RETAIN plus
    // hardcoded physical names, so a renamed construct ID makes CloudFormation
    // create a duplicate table — is respected instead by fixing this construct
    // ID now: `ProcessedFilesTable` MUST NEVER BE RENAMED. Precedent for adding
    // a Data-stack table this way: PipelineWatermarksTable (LGR-dd80c6d-1).
    //
    // No stream and no GSI, both on purpose: the only access pattern is a keyed
    // get by (pipeline_id, fingerprint), and nothing should react to a row
    // appearing. TTL is `ttl`, set per row from
    // config.PROCESSED_FILE_LEDGER_TTL_SECONDS (30 days) — that constant's
    // comment records why both a shorter and a longer window are wrong.
    this.processedFilesTable = new dynamodb.Table(this, 'ProcessedFilesTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_processed_files',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'fingerprint', type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: 'ttl',
    });

    // ── Audit Log Table (1 GSI + TTL) ─────────────────────────────
    this.auditLogTable = new dynamodb.Table(this, 'AuditLogTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_audit_log',
      partitionKey: { name: 'resource_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });
    this.auditLogTable.addGlobalSecondaryIndex({
      indexName: 'ActionIndex',
      partitionKey: { name: 'action', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── Agent Traces Table (TTL-enabled) ─────────────────────────
    this.agentTracesTable = new dynamodb.Table(this, 'AgentTracesTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_agent_traces',
      partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'step_name', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── Pipeline Watermarks Table (PK-only, JDBC incremental source) ─
    // Read/written per run by the incremental JDBC PySpark template
    // (pyspark_templates/glue_job_jdbc_source.py.j2) via get_item/put_item
    // keyed by pipeline_id. RETAIN + created empty: a missing watermark
    // item is the template's documented first-run case. See docs/JDBC_SOURCE.md.
    //
    // NAMING FENCE (p31 C2, LGR-dd80c6d-1) — construct ID MUST stay
    // 'PipelineWatermarksTable': the deployed Data stack already manages this
    // table under logical ID PipelineWatermarksTable952CB202 (DeletionPolicy:
    // Retain). Renaming the construct would generate a new logical ID and make
    // CloudFormation try to CREATE a second table with the same physical name
    // in the same stack ("already exists in stack"). Keep this ID stable.
    //
    // The ID predates the agentglue -> agentic-data-ingestion rename: LGR-dd80c6d-1
    // added this table as `agentglue_pipeline_watermarks` and deliberately reused
    // the then-existing logical ID for exactly this reason. The construct ID itself
    // carries no Legacy_Name, so a pattern-driven Legacy_Name sweep will not see it —
    // it is a rename-consistency hazard, not a rename leftover. This ID can only
    // change once no environment retains a table under the old logical ID, which
    // RETAIN + terminationProtection make a manual, per-account operation.
    this.watermarksTable = new dynamodb.Table(this, 'PipelineWatermarksTable', {
      ...tableDefaults,
      tableName: 'agentic_data_ingestion_pipeline_watermarks',
      partitionKey: { name: 'pipeline_id', type: dynamodb.AttributeType.STRING },
    });

    // ── Model Call Budget Table (TTL-enabled) ──────────────────────
    // P28 R15.2: cross-Lambda per-run model-call counter backing
    // shared/model_config.py CallBudget (wired via MODEL_CALL_BUDGET_TABLE).
    // Each invoke_model() call does an atomic `ADD call_count 1` on the
    // run's item; `expires_at` TTL self-cleans items after the longest
    // legitimate run window (8 days). Ephemeral counters — DESTROY like
    // the task-tokens/ws-connections/errors tables.
    this.modelCallBudgetTable = new dynamodb.Table(this, 'ModelCallBudgetTable', {
      tableName: 'agentic_data_ingestion_model_call_budget',
      partitionKey: { name: 'run_id', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expires_at',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── EventBridge Custom Bus ─────────────────────────────────────
    this.eventBus = new events.EventBus(this, 'EventBus', {
      eventBusName: `agentic-data-ingestion-pipeline-events-${environment}`,
    });

    // ── Dead Letter Queue ──────────────────────────────────────────
    this.deadLetterQueue = new sqs.Queue(this, 'EventBridgeDLQ', {
 enforceSSL: true, // deny non-TLS access (AwsSolutions-SQS4)
      queueName: `agentic-data-ingestion-eventbridge-dlq-${environment}`,
      retentionPeriod: cdk.Duration.days(14),
      visibilityTimeout: cdk.Duration.seconds(60),
    });
  }
}
