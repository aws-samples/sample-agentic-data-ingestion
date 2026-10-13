// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as glue from 'aws-cdk-lib/aws-glue';
import { Construct } from 'constructs';

export interface FoundationStackProps extends cdk.StackProps {
  projectName: string;
  environment: string;
}

export class FoundationStack extends cdk.Stack {
  // S3 Buckets
  public readonly sourceBucket: s3.Bucket;
  public readonly processedBucket: s3.Bucket;
  public readonly targetSchemasBucket: s3.Bucket;
  public readonly generatedCodeBucket: s3.Bucket;
  public readonly testOutputsBucket: s3.Bucket;
  public readonly templatesBucket: s3.Bucket;
  public readonly lambdaCodeBucket: s3.Bucket;
  public readonly dependenciesBucket: s3.Bucket;
  public readonly checkpointsBucket: s3.Bucket;

  // ECR
  public readonly ecrRepository: ecr.Repository;

  // Glue
  public readonly jdbcDiscoveryDatabase: glue.CfnDatabase;

  // IAM Roles
  public readonly fileValidationRole: iam.Role;
  public readonly schemaExtractRole: iam.Role;
  public readonly schemaCompareRole: iam.Role;
  public readonly schemaMappingRole: iam.Role;
  public readonly codeGenerationRole: iam.Role;
  public readonly codeTestingRole: iam.Role;
  public readonly gluePipelineRole: iam.Role;
  public readonly infrastructureRole: iam.Role;
  public readonly qualityChecksRole: iam.Role;
  public readonly runPipelineRole: iam.Role;
  public readonly approvalConfigRole: iam.Role;
  public readonly jobCompletionRole: iam.Role;
  public readonly selfHealingDispatchRole: iam.Role;
  public readonly agentDispatchRole: iam.Role;
  public readonly pipelineTriggerRole: iam.Role;
  /**
   * One execution role per AgentCore agent runtime, plus one shared read-only
   * role for the MCP server runtimes (threat model R-02). Each agent role holds
   * only the tables, buckets and actions its own code calls; see the role
   * block below for the per-runtime inventory.
   */
  public readonly createCodeRuntimeRole: iam.Role;
  public readonly testCodeRuntimeRole: iam.Role;
  public readonly createGluePipelineRuntimeRole: iam.Role;
  public readonly createInfrastructureRuntimeRole: iam.Role;
  public readonly selfHealingRuntimeRole: iam.Role;
  public readonly complexMappingRuntimeRole: iam.Role;
  public readonly dataQualityRuntimeRole: iam.Role;
  public readonly costOptimizationRuntimeRole: iam.Role;
  public readonly nlPipelineBuilderRuntimeRole: iam.Role;
  public readonly mcpServerRuntimeRole: iam.Role;
  public readonly eventBridgeRole: iam.Role;
  /**
   * Read-only runtime role assumed by Glue interactive sessions that verify
   * AI-generated PySpark (real-Spark verification §4.6). Exposed so a later
   * change can grant `iam:PassRole` on it and so the runner can name its ARN.
   */
  public readonly verificationSessionRole: iam.Role;

  /**
   * Permissions boundary every runtime-created per-pipeline Glue job role must
   * carry (threat model T-03). Its ARN is published to create_glue_pipeline as
   * GLUE_JOB_ROLE_BOUNDARY_ARN.
   */
  public readonly glueJobRoleBoundary: iam.ManagedPolicy;

  constructor(scope: Construct, id: string, props: FoundationStackProps) {
    super(scope, id, props);

    const { projectName, environment } = props;
    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    // ── S3 Buckets ─────────────────────────────────────────────────
    const bucketDefaults: Partial<s3.BucketProps> = {
      versioned: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true, // deny non-TLS access (AwsSolutions-S10)
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    };

    // ── Retention of customer data (threat model T-11, R-08) ───────────
    //
    // Uploaded source files expire SOURCE_RETENTION_DAYS after upload. Nothing
    // reads a source object after its run finishes except a retry of that run,
    // and the longest a run waits is the 72 h approval hold, so 90 days leaves
    // a wide margin. Expiration emits no "Object Created" event, so it cannot
    // trigger S3UploadTriggerRule (orchestration-stack.ts).
    //
    // The processed bucket gets NO current-object expiration, deliberately. It
    // is the bucket operators point Glue Catalog target tables at
    // (dashboard_api/handlers/resources.py `_WRITABLE_BUCKET_STEMS`, and what the
    // e2e suite uses), so it can hold a customer's ingested target data. Expiring
    // it would delete the output of the pipeline. Only its transient
    // `dq-reports/` prefix expires.
    //
    // Every bucket that holds customer data drops noncurrent versions after
    // NONCURRENT_VERSION_DAYS (the recovery window for an overwritten or deleted
    // object) and aborts incomplete multipart uploads. Expired delete markers
    // are left alone: ExpiredObjectDeleteMarker cannot share a rule with a Days
    // expiration, and they cost nothing material. Pinned by
    // test/bucket-lifecycle.test.ts. Change the two constants to change the
    // retention; the README states the defaults.
    const SOURCE_RETENTION_DAYS = 90;
    const NONCURRENT_VERSION_DAYS = 30;
    const housekeepingRule: s3.LifecycleRule = {
      id: 'housekeeping',
      noncurrentVersionExpiration: cdk.Duration.days(NONCURRENT_VERSION_DAYS),
      abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
    };

    this.sourceBucket = new s3.Bucket(this, 'SourceFiles', {
      ...bucketDefaults,
      bucketName: `adi-source-files-${account}-${region}`,
      lifecycleRules: [{ ...housekeepingRule, expiration: cdk.Duration.days(SOURCE_RETENTION_DAYS) }],
      eventBridgeEnabled: true,
      cors: [{
        allowedOrigins: ['*'],
        allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.GET, s3.HttpMethods.HEAD],
        allowedHeaders: ['*'],
        exposedHeaders: ['ETag'],
        maxAge: 3600,
      }],
    });

    this.processedBucket = new s3.Bucket(this, 'ProcessedFiles', {
      ...bucketDefaults,
      bucketName: `adi-processed-files-${account}-${region}`,
      lifecycleRules: [
        housekeepingRule,
        // Data-quality reports and their sample files (data_quality_agent
        // `dq-reports/<run_id>/`). Never widen this to the whole bucket: see the
        // retention note above.
        { id: 'dq-reports', prefix: 'dq-reports/', expiration: cdk.Duration.days(SOURCE_RETENTION_DAYS) },
      ],
    });

    this.targetSchemasBucket = new s3.Bucket(this, 'TargetSchemas', {
      ...bucketDefaults,
      bucketName: `adi-target-schemas-${account}-${region}`,
    });

    this.generatedCodeBucket = new s3.Bucket(this, 'GeneratedCode', {
      ...bucketDefaults,
      bucketName: `adi-generated-code-${account}-${region}`,
      lifecycleRules: [
        { noncurrentVersionExpiration: cdk.Duration.days(90) },
        // Rows a data-quality check rejected, written by the generated job to
        // `quarantine/<run_id>/` (_partials/_data_quality.py.j2). This is
        // customer data. Nothing else here may expire: job scripts
        // (ScriptLocation) and `shared/` are read by every future run.
        { id: 'quarantine', prefix: 'quarantine/', expiration: cdk.Duration.days(SOURCE_RETENTION_DAYS) },
      ],
    });

    this.testOutputsBucket = new s3.Bucket(this, 'TestOutputs', {
      ...bucketDefaults,
      bucketName: `adi-test-outputs-${account}-${region}`,
      lifecycleRules: [{ ...housekeepingRule, expiration: cdk.Duration.days(30) }],
    });

    this.templatesBucket = new s3.Bucket(this, 'Templates', {
      ...bucketDefaults,
      bucketName: `adi-templates-${account}-${region}`,
    });

    this.lambdaCodeBucket = new s3.Bucket(this, 'LambdaCode', {
      ...bucketDefaults,
      bucketName: `adi-lambda-code-${account}-${region}`,
    });

    this.dependenciesBucket = new s3.Bucket(this, 'Dependencies', {
      ...bucketDefaults,
      bucketName: `adi-dependencies-${account}-${region}`,
    });

    this.checkpointsBucket = new s3.Bucket(this, 'Checkpoints', {
      ...bucketDefaults,
      bucketName: `adi-checkpoints-${account}-${region}`,
      lifecycleRules: [{ ...housekeepingRule, expiration: cdk.Duration.days(7) }],
    });

    // ── ECR Repository ─────────────────────────────────────────────
    this.ecrRepository = new ecr.Repository(this, 'AgentCoreMCPRepo', {
      repositoryName: `${projectName}-${environment}-agentcore-mcp-server`,
      imageScanOnPush: true,
      imageTagMutability: ecr.TagMutability.MUTABLE,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      emptyOnDelete: true,
      lifecycleRules: [
        { tagPrefixList: ['v', 'prod', 'release'], maxImageCount: 10 },
        { tagPrefixList: ['dev', 'test', 'latest'], maxImageCount: 5 },
        { tagStatus: ecr.TagStatus.UNTAGGED, maxImageAge: cdk.Duration.days(1) },
      ],
    });

    // ── Glue Discovery Database ────────────────────────────────────
    // JDBC source schema-discovery crawlers (schema_extract +
    // handlers/jdbc_resources.py) target this database by name. The name is
    // derived from the same `environment` value the Lambdas use for
    // `AGENTIC_DATA_INGESTION_ENV` (default `dev`), so CDK and runtime agree for every
    // stage: DISCOVERY_DB = f"agentic_data_ingestion_jdbc_discovery_{AGENTIC_DATA_INGESTION_ENV}".
    // CDK is the sole owner — the runtime code never calls create_database
    // for this DB (the two runtime create_database paths build the S3-Tables
    // catalog DB and a per-pipeline `agentic_data_ingestion_{pipeline_id}` DB).
    this.jdbcDiscoveryDatabase = new glue.CfnDatabase(this, 'JdbcDiscoveryDatabase', {
      catalogId: account,
      databaseInput: {
        name: `agentic_data_ingestion_jdbc_discovery_${environment}`,
        description: 'JDBC source schema discovery database — target for AgenticDataIngestion JDBC discovery crawlers.',
      },
    });

    // ── Shared IAM Policies ────────────────────────────────────────
    const allBucketArns = [
      this.sourceBucket, this.processedBucket, this.targetSchemasBucket,
      this.generatedCodeBucket, this.testOutputsBucket, this.templatesBucket,
    ].flatMap(b => [b.bucketArn, `${b.bucketArn}/*`]);

    const dynamoDbPolicy = new iam.ManagedPolicy(this, 'DynamoDBPolicy', {
      managedPolicyName: `${projectName}-${environment}-dynamodb-policy`,
      statements: [new iam.PolicyStatement({
        actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan', 'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem'],
        resources: [`arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*`, `arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*/index/*`],
      })],
    });

    const bedrockPolicy = new iam.ManagedPolicy(this, 'BedrockPolicy', {
      managedPolicyName: `${projectName}-${environment}-bedrock-policy`,
      statements: [new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:GetFoundationModel', 'bedrock:ListFoundationModels'],
        resources: ['*'],
      })],
    });

    const agentCorePolicy = new iam.ManagedPolicy(this, 'AgentCorePolicy', {
      managedPolicyName: `${projectName}-${environment}-agentcore-policy`,
      statements: [
        new iam.PolicyStatement({
          // Only these two are IAM actions. 'bedrock-runtime:InvokeModel' and
          // 'bedrock-agent:InvokeAgent' used to sit here too; neither service
          // prefix exists (IAM Access Analyzer INVALID_SERVICE_IN_ACTION,
          // 2026-10-12), so they granted nothing. InvokeModel is already the
          // bedrock: action on this line; nothing in lambda/ calls InvokeAgent
          // (the client in shared/mcp_integration.py is never constructed), so
          // it is dropped rather than renamed. Keep quotes in these comments
          // balanced: tests/cdk/stacks/test_code_roles_target_schema_iam.py reads
          // this file as text and treats an apostrophe as an opening quote.
          actions: ['bedrock-agentcore:InvokeAgentRuntime', 'bedrock:InvokeModel'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          actions: ['glue:Get*', 'glue:List*', 'glue:StartJobRun'],
          // Databases/tables are snake_case (Glue forbids hyphens there); jobs and
          // crawlers are created kebab-case (e.g. agentic-data-ingestion-job-<id>),
          // so both prefix forms are required.
          // NAMING FENCE (p31 C3) — do NOT delete either prefix form to "make the
          // naming consistent". Both forms were added by commit 2b45b72912 to fix a
          // verified glue:GetJob AccessDenied from the AgentCore runtime role
          // (validated in 777788889999 us-west-2 via the IAM policy simulator) and
          // are pinned by test/foundation-iam.test.ts (Req 9.8). Kebab is required
          // because job_builders.py:442 hardcodes agentic-data-ingestion-job-<id>;
          // snake is required because Glue databases are agentic_data_ingestion*
          // (line 149). Both stay until one of those two producers changes.
          resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`, `arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`, `arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`],
        }),
        new iam.PolicyStatement({
          actions: ['s3tables:*'],
          resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`],
        }),
        new iam.PolicyStatement({
          // 'emr:Describe*' / 'emr:List*' were here until 2026-10-12. The EMR
          // service prefix is 'elasticmapreduce', so they granted nothing, and
          // no Lambda calls EMR (the EMR tools of the analytics MCP server run in
          // AgentCore under McpServerRuntimeRole). Dropped, not renamed.
          actions: ['athena:*', 'cloudwatch:GetMetricStatistics', 'cloudwatch:ListMetrics'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          actions: ['s3:GetObject', 's3:ListBucket', 's3:GetBucketLocation'],
          resources: ['arn:aws:s3:::aws-athena-query-results-*', 'arn:aws:s3:::aws-athena-query-results-*/*', ...allBucketArns],
        }),
      ],
    });

    // Helper to create Lambda roles
    const lambdaRole = (name: string, extraPolicies: iam.ManagedPolicy[], inlineStatements: iam.PolicyStatement[] = []): iam.Role => {
      const role = new iam.Role(this, name, {
        roleName: `${projectName}-${environment}-${name.toLowerCase().replace(/role$/, '')}-role`,
        assumedBy: new iam.CompositePrincipal(
          new iam.ServicePrincipal('lambda.amazonaws.com'),
        ),
        managedPolicies: [
          iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
          dynamoDbPolicy,
          ...extraPolicies,
        ],
      });
      // X-Ray permissions for ADOT/OTEL distributed tracing
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
        resources: ['*'],
      }));
      inlineStatements.forEach(s => role.addToPolicy(s));
      return role;
    };

    const s3ReadWrite = new iam.PolicyStatement({
      // HeadObject/HeadBucket authorize as GetObject/ListBucket; the 's3:Head*'
      // names are not IAM actions (see the runtime-role comment near the end of
      // this file) and were removed 2026-10-12.
      actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:CreateBucket'],
      resources: [`arn:aws:s3:::adi-*-${account}-${region}`, `arn:aws:s3:::adi-*-${account}-${region}/*`],
    });

    const glueAdmin = new iam.PolicyStatement({
      actions: ['glue:*'],
      // Snake_case covers databases/tables (Glue forbids hyphens there); kebab-case
      // covers jobs/crawlers/connections created at runtime (agentic-data-ingestion-*).
      // NAMING FENCE (p31 C3) — do NOT delete either form; see the AgentCorePolicy
      // glue statement above for the verified AccessDenied this prevents.
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`, `arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`, `arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`, `arn:aws:glue:${region}:${account}:connection/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:connection/agentic-data-ingestion-*`],
    });

    // ── Lambda Roles ───────────────────────────────────────────────
    this.fileValidationRole = lambdaRole('FileValidation', [bedrockPolicy], [
      new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'], resources: [this.sourceBucket.bucketArn, `${this.sourceBucket.bucketArn}/*`, this.processedBucket.bucketArn, `${this.processedBucket.bucketArn}/*`] }),
    ]);
    this.fileValidationRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));

    this.schemaExtractRole = lambdaRole('SchemaExtract', [bedrockPolicy], [
      // S3 Select authorizes as s3:GetObject; 's3:SelectObjectContent' is not an
      // IAM action and was removed 2026-10-12.
      new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:PutObject', 's3:ListBucket'], resources: [this.sourceBucket.bucketArn, `${this.sourceBucket.bucketArn}/*`, this.processedBucket.bucketArn, `${this.processedBucket.bucketArn}/*`] }),
    ]);
    this.schemaExtractRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    // JDBC schema-extract: run + poll the per-pipeline runtime crawler
    // `agentic-data-ingestion-{pipeline_id}-source-discover` to discover the source table
    // schema at execution time (schema_extract JDBC branch — start_crawler +
    // get_crawler polling loop). Scoped to agentic-data-ingestion-* crawlers.
    this.schemaExtractRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:StartCrawler', 'glue:GetCrawler'],
      resources: [`arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`],
    }));
    // JDBC schema-extract: after the crawler completes, read the discovered
    // table from the JDBC discovery DB (get_tables with the pipeline prefix,
    // then get_table for column metadata). Scoped to the discovery DB ARNs
    // only — no broader Glue read is given to this Lambda role.
    this.schemaExtractRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetTables', 'glue:GetTable'],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion_jdbc_discovery_*`,
        `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion_jdbc_discovery_*/*`,
      ],
    }));

    this.schemaCompareRole = lambdaRole('SchemaCompare', [], [
      new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:ListBucket'], resources: [this.processedBucket.bucketArn, `${this.processedBucket.bucketArn}/*`, `${this.generatedCodeBucket.bucketArn}/shared/*`] }),
    ]);
    // schema_compare's only S3 read is the target schema object, whose location
    // is the pipeline record's target_schema_bucket + target_schema_s3_key pair
    // — the target-schemas bucket, same as schemaMappingRole below. The grant
    // was previously absent because the read was aimed at the wrong bucket
    // (generated_code_bucket, an attribute no writer sets) AND unreachable: the
    // target-schema check returned early whenever no baseline version existed,
    // which was every pipeline. LGR-eee8ac8-4 removed the early return so a
    // first successful run can establish the baseline, which made the read live.
    this.schemaCompareRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [this.targetSchemasBucket.bucketArn, `${this.targetSchemasBucket.bucketArn}/*`],
    }));
    this.schemaCompareRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));

    this.schemaMappingRole = lambdaRole('SchemaMapping', [], [
      new iam.PolicyStatement({ actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'], resources: ['*'] }),
      new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:ListBucket'], resources: [this.processedBucket.bucketArn, `${this.processedBucket.bucketArn}/*`, this.targetSchemasBucket.bucketArn, `${this.targetSchemasBucket.bucketArn}/*`, this.generatedCodeBucket.bucketArn, `${this.generatedCodeBucket.bucketArn}/*`] }),
    ]);
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    // Source sample reads for the mapping prompt (p49 C2). `read_source_samples`
    // opens the URI schema_extract stored: `sample_s3_uri` when the Glue job has
    // written one (JDBC sources, processedBucket `samples/`, already covered
    // above), else `json_s3_uri`. On the S3-upload path `json_s3_uri` is the
    // uploaded object itself, so it names sourceBucket, which this role could
    // not read. Every S3-upload run therefore logged `AccessDenied` and sent the
    // model no sample values, while the README's data-sent table says it sends
    // up to five per field. The read is inert by design (`{}` on any failure),
    // so the gap cost mapping quality silently rather than failing a run. This
    // is the same consumer-lacks-the-producer's-bucket defect the
    // verificationSessionRole comment below records for that role.
    //
    // ListBucket rides along, matching this role's bucket-read statement above,
    // so a source object that has since expired under the source bucket's
    // lifecycle rule logs `NoSuchKey` rather than an `AccessDenied` that looks
    // like this defect returning. No PutObject: this role never writes the
    // watched bucket.
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [this.sourceBucket.bucketArn, `${this.sourceBucket.bucketArn}/*`],
    }));
    // Glue catalog access for S3 Tables schema retrieval
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetTable', 'glue:GetDatabase', 'glue:GetDatabases'],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/*`,
        `arn:aws:glue:${region}:${account}:table/*/*`,
      ],
    }));
    // S3 Tables API access for reading table schemas and Iceberg metadata
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3tables:*'],
      resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`],
    }));
    // S3 Tables internal bucket access for reading Iceberg metadata JSON files
    // These buckets have UUID-prefixed names ending in --table-s3 and are managed by S3 Tables
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject'],
      resources: ['arn:aws:s3:::*--table-s3/*'],
    }));
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:BatchCreateMemoryRecords',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:ListMemoryRecords',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:memory/*`],
    }));
    // Complex mapping agent invocation: allow the schema-mapping Lambda to
    // invoke the complex mapping AgentCore Runtime. Name-scoped to avoid a
    // cross-stack dependency cycle (the runtime lives in the AgentCore stack).
    this.schemaMappingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_complex_mapping-*`],
    }));

    // P28 R19.11: the four container-backed step Lambdas (create_code,
    // test_code, create_glue_pipeline, create_infrastructure) run under
    // waitForTaskToken task states. The Lambda side closes the task-token
    // loop itself in two paths (shared/sfn_task_token.py step_task_token):
    // direct in-Lambda execution when USE_AGENTCORE_RUNTIME is off, and the
    // version-skew relay when an old container returns a synchronous result
    // instead of the dispatch ack. SendTask* callback actions do NOT support
    // resource-level permissions and require Resource: '*' (same convention
    // as the runPipeline/jobCompletion/approval grants below).
    const sfnTaskCallbackStatement = () => new iam.PolicyStatement({
      actions: ['states:SendTaskSuccess', 'states:SendTaskFailure'],
      resources: ['*'],
    });

    this.codeGenerationRole = lambdaRole('CodeGeneration', [bedrockPolicy, agentCorePolicy], [s3ReadWrite]);
    this.codeGenerationRole.assumeRolePolicy?.addStatements(new iam.PolicyStatement({ actions: ['sts:AssumeRole'], principals: [new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com')] }));
    this.codeGenerationRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    this.codeGenerationRole.addToPolicy(sfnTaskCallbackStatement());

    this.codeTestingRole = lambdaRole('CodeTesting', [bedrockPolicy, agentCorePolicy], [s3ReadWrite]);
    this.codeTestingRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    this.codeTestingRole.addToPolicy(sfnTaskCallbackStatement());
    this.codeTestingRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:CreateCodeInterpreter',
        'bedrock-agentcore:StartCodeInterpreterSession',
        'bedrock-agentcore:InvokeCodeInterpreter',
        'bedrock-agentcore:StopCodeInterpreterSession',
        'bedrock-agentcore:GetCodeInterpreterSession',
        'bedrock-agentcore:ListCodeInterpreters',
        'bedrock-agentcore:GetCodeInterpreter',
        'bedrock-agentcore:ListCodeInterpreterSessions',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:code-interpreter/*`],
    }));

    // ── Per-pipeline Glue job role: permissions boundary (threat model T-03) ──
    //
    // create_glue_pipeline (iam_roles.py) creates one Glue execution role per
    // pipeline at run time, as `AgenticDataIngestion-<pipeline>-ExecutionRole`,
    // under two callers: gluePipelineRole (the Lambda) and
    // createGluePipelineRuntimeRole (the container). Both used to hold iam:CreateRole / iam:PutRolePolicy /
    // iam:AttachRolePolicy on that pattern with no condition, so code running as
    // either could create a role, attach AdministratorAccess to it and pass it to
    // Glue — account administrator in two calls.
    //
    // This boundary is the ceiling for every such role: what any Glue job built
    // by iam_roles.create_pipeline_specific_role may ever be granted, on any
    // branch. CreateRole and PutRolePolicy are conditioned on it below, so a role
    // without it cannot be created and an existing role without it cannot be
    // given an inline policy. AttachRolePolicy is no longer granted at all:
    // since T-02 the runtime code attaches no managed policy.
    //
    // It caps; it does not grant. The per-pipeline inline policy is still what
    // scopes each role to its own buckets, secret, connection and tables. The
    // boundary is deliberately broad on S3 and Secrets Manager because a
    // pipeline target may be any operator-chosen bucket and a JDBC secret may be
    // any secret its Glue connection names (job_arguments.resolve_jdbc_target_secret).
    // What it excludes is the point: no iam:*, no sts:AssumeRole, no Lambda, no
    // Step Functions, and no Glue job, crawler, connection or database WRITE.
    //
    // KEEP IN STEP with iam_roles.py: an action the inline policy grants that is
    // missing here is an AccessDenied at Glue job run time, not at deploy.
    // tests/contracts/test_glue_job_role_boundary_covers_inline_policy.py builds
    // the role for every target branch and fails if this boundary misses one.
    //
    // A boundary grants nothing, so it must never be ATTACHED to a principal:
    // attached, its S3 `*` and secret `*` would become real grants.
    // tests/cdk/stacks/test_p53_credential_path_iam.py asserts it is referenced
    // only by ARN (the conditions and env vars below).
    const glueJobRoleArnPattern = `arn:aws:iam::${account}:role/AgenticDataIngestion-*-ExecutionRole`;
    this.glueJobRoleBoundary = new iam.ManagedPolicy(this, 'GlueJobRoleBoundary', {
      managedPolicyName: `AgenticDataIngestion-GlueJobRoleBoundary-${environment}`,
      description: 'Permissions boundary for runtime-created per-pipeline Glue job roles (threat model T-03)',
      statements: [
        new iam.PolicyStatement({
          sid: 'GlueJobLogs',
          actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
          resources: [`arn:aws:logs:${region}:${account}:log-group:/aws-glue/*`],
        }),
        new iam.PolicyStatement({
          sid: 'GlueJobMetrics',
          actions: ['cloudwatch:PutMetricData'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'PipelineData',
          actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject', 's3:ListBucket', 's3:GetBucketLocation'],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'S3TablesTargets',
          actions: [
            's3tables:GetTableBucket', 's3tables:GetNamespace', 's3tables:CreateNamespace', 's3tables:ListNamespaces',
            's3tables:GetTable', 's3tables:GetTableMetadataLocation', 's3tables:GetTableData', 's3tables:ListTables',
            's3tables:CreateTable', 's3tables:PutTableData', 's3tables:UpdateTableMetadataLocation',
          ],
          resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`],
        }),
        new iam.PolicyStatement({
          sid: 'GlueCatalogTables',
          actions: [
            'glue:GetDatabase', 'glue:GetTable', 'glue:GetTables', 'glue:UpdateTable', 'glue:CreateTable',
            'glue:GetPartition', 'glue:GetPartitions', 'glue:BatchGetPartition', 'glue:CreatePartition',
            'glue:UpdatePartition', 'glue:BatchCreatePartition',
          ],
          resources: [
            `arn:aws:glue:${region}:${account}:catalog`,
            `arn:aws:glue:${region}:${account}:database/*`,
            `arn:aws:glue:${region}:${account}:table/*/*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: 'GlueConnectionsRead',
          actions: ['glue:GetConnection', 'glue:GetConnections'],
          resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:connection/*`],
        }),
        new iam.PolicyStatement({
          sid: 'GlueDataQualityAndOwnJob',
          actions: [
            'glue:PublishDataQuality', 'glue:GetDataQualityResult', 'glue:PutDataQualityStatisticAnnotation',
            'glue:PutDataQualityProfileAnnotation', 'glue:GetJob', 'glue:GetJobBookmark',
          ],
          // The kebab job form (p31 C3 naming fence). Jobs are named
          // agentic-data-ingestion-job-<pipeline>[-v<n>], a subset of it.
          resources: [
            `arn:aws:glue:${region}:${account}:dataQualityRuleset/*`,
            `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`,
          ],
        }),
        new iam.PolicyStatement({
          sid: 'JdbcCredentials',
          actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
          resources: [`arn:aws:secretsmanager:${region}:${account}:secret:*`],
        }),
        new iam.PolicyStatement({
          sid: 'RunStateAndWatermarks',
          actions: ['dynamodb:GetItem', 'dynamodb:PutItem'],
          resources: [`arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_*`],
        }),
        new iam.PolicyStatement({
          sid: 'JdbcVpcNetworking',
          actions: [
            'ec2:CreateNetworkInterface', 'ec2:DescribeNetworkInterfaces', 'ec2:DeleteNetworkInterface',
            'ec2:DescribeVpcEndpoints', 'ec2:DescribeRouteTables', 'ec2:DescribeSecurityGroups',
            'ec2:DescribeSubnets', 'ec2:DescribeVpcAttribute',
          ],
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          sid: 'JdbcEniTags',
          actions: ['ec2:CreateTags', 'ec2:DeleteTags'],
          resources: ['arn:aws:ec2:*:*:network-interface/*'],
          conditions: { 'ForAllValues:StringEquals': { 'aws:TagKeys': ['aws-glue-service-resource'] } },
        }),
      ],
    });
    /** CreateRole + PutRolePolicy on per-pipeline Glue roles, only with the boundary attached (T-03). */
    const boundedGlueRoleWrite = () => new iam.PolicyStatement({
      actions: ['iam:CreateRole', 'iam:PutRolePolicy'],
      resources: [glueJobRoleArnPattern],
      conditions: { ArnEquals: { 'iam:PermissionsBoundary': this.glueJobRoleBoundary.managedPolicyArn } },
    });
    /** Read and tag-on-create for the same roles. TagRole is required by CreateRole's Tags. */
    const glueRoleReadAndTag = () => new iam.PolicyStatement({
      actions: ['iam:GetRole', 'iam:TagRole', 'iam:ListAttachedRolePolicies', 'iam:ListRolePolicies'],
      resources: [glueJobRoleArnPattern],
    });

    this.gluePipelineRole = lambdaRole('GluePipeline', [bedrockPolicy, agentCorePolicy], [
      s3ReadWrite, glueAdmin,
      boundedGlueRoleWrite(),
      glueRoleReadAndTag(),
      new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [glueJobRoleArnPattern], conditions: { StringEquals: { 'iam:PassedToService': 'glue.amazonaws.com' } } }),
      new iam.PolicyStatement({ actions: ['secretsmanager:CreateSecret', 'secretsmanager:GetSecretValue', 'secretsmanager:PutSecretValue', 'secretsmanager:TagResource'], resources: [`arn:aws:secretsmanager:${region}:${account}:secret:agentic-data-ingestion/*`] }),
      // Legacy test-fixtures secrets (P18) were created before the agentglue -> agentic-data-ingestion
      // rename and are still named `agentglue/test-fixtures/*`. The JDBC discovery/runtime Glue
      // crawlers execute as this role and must read those secrets, so grant read-only access to the
      // legacy prefix. Remove this once the P18 test-fixtures secrets are renamed to the new prefix.
      // Provenance: LGR-a04dd56-1 (backfilled) — without this grant the crawler run FAILED on
      // secretsmanager:GetSecretValue AccessDenied and surfaced as "API error 500" in the dashboard.
      // Pinned by test/foundation-iam.test.ts and test/p17-jdbc-source.test.ts.
      new iam.PolicyStatement({ actions: ['secretsmanager:GetSecretValue'], resources: [`arn:aws:secretsmanager:${region}:${account}:secret:agentglue/test-fixtures/*`] }),
      new iam.PolicyStatement({ actions: ['cloudwatch:PutMetricData'], resources: ['*'] }),
      new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }),
      // S3 Tables API access for resolving warehouse_location on existing S3 Table targets
      new iam.PolicyStatement({ actions: ['s3tables:GetTable', 's3tables:ListTableBuckets'], resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`] }),
    ]);
    // Glue Crawler execution (P17): the JDBC discovery crawlers created by the
    // dashboard API pass this role to Glue (Role=gluePipelineRole). A Glue
    // Crawler assumes its execution role via the glue.amazonaws.com service
    // principal, so the role must trust Glue in addition to Lambda. Without
    // this trust the crawler run fails (poll returns FAILED) even though
    // CreateCrawler succeeds.
    this.gluePipelineRole.assumeRolePolicy?.addStatements(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      principals: [new iam.ServicePrincipal('glue.amazonaws.com')],
    }));
    // Glue Crawler VPC networking (P17): a Glue Crawler that reaches a
    // JDBC source through a VPC-bound Glue Connection runs inside the
    // customer VPC via an elastic network interface, exactly like a Glue
    // ETL job. Glue therefore needs ENI lifecycle + VPC describe perms on
    // the crawler's execution role. Without these the crawler run fails
    // with "Service Principal: glue.amazonaws.com is not authorized to
    // perform: ec2:DescribeSubnets". This mirrors the inline VPC block that
    // the per-pipeline ETL execution roles already attach
    // (lambda/agentic_data_ingestion_create_glue_pipeline/iam_roles.py). EC2
    // Describe + CreateNetworkInterface do not support resource-level
    // permissions, so Resource:'*' is required.
    this.gluePipelineRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ec2:CreateNetworkInterface',
        'ec2:DescribeNetworkInterfaces',
        'ec2:DeleteNetworkInterface',
        'ec2:DescribeVpcEndpoints',
        'ec2:DescribeRouteTables',
        'ec2:DescribeSecurityGroups',
        'ec2:DescribeSubnets',
        'ec2:DescribeVpcAttribute',
      ],
      resources: ['*'],
    }));
    // ENI tagging: Glue tags the ENIs it creates with the
    // `aws-glue-service-resource` key. Scoped to network-interface ARNs and
    // gated on that tag key, matching the ETL execution-role grant.
    this.gluePipelineRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:CreateTags', 'ec2:DeleteTags'],
      resources: [`arn:aws:ec2:${region}:${account}:network-interface/*`],
      conditions: { 'ForAllValues:StringEquals': { 'aws:TagKeys': ['aws-glue-service-resource'] } },
    }));
    // Glue Connection READ for the crawler's own JdbcTargets validation.
    //
    // Glue validates a crawler's `JdbcTargets[].ConnectionName` by retrieving
    // the connection AS THE PASSED ROLE, not as the caller. Both crawler
    // creators pass this role (Role=gluePipelineRole) while running under
    // infrastructureRole: wizard-time discovery
    // (dashboard_api/handlers/jdbc_resources.py `_create_or_update_crawler`)
    // and the per-pipeline runtime crawler
    // (dashboard_api/handlers/pipelines_jdbc.py). So infrastructureRole's own
    // connection/* read (:541-552) does not satisfy the validation — this role
    // needs its own.
    //
    // `glue_connection` mode exists to reference PRE-EXISTING connections with
    // arbitrary, customer-chosen names, so the names are not knowable at deploy
    // time. The glue:* statement (glueAdmin) scopes connections to
    // agentic_data_ingestion* / agentic-data-ingestion-*, which covers the
    // connections this app creates and nothing else — so every non-prefixed
    // connection failed CreateCrawler with:
    //   InvalidInputException: Unable to retrieve jdbc connection.
    // Verified in account 444455556666 / us-west-2 (2026-09-17): three
    // CreateCrawler calls identical but for the connection name — the two
    // agentic-data-ingestion-* names succeeded, `agentglue-test-fixtures-mysql-source`
    // raised the InvalidInputException; and `iam simulate-principal-policy` for
    // glue:GetConnection on this role returned implicitDeny for the
    // agentglue-test-fixtures-* connections and allowed for
    // agentic-data-ingestion-discover-*. Path format, role trust and the
    // discovery database were all held constant, so prefix scoping was the only
    // variable.
    //
    // This mirrors the identical grant createGluePipelineRuntimeRole carries
    // (the job-builder runtime role, below) for the same reason, and the read-wide / write-narrow split
    // infrastructureRole already uses (read on connection/*, write scoped to
    // connection/agentic-data-ingestion-* at :558-561).
    //
    // Do NOT collapse this into glueAdmin, and do NOT add connection/* to it:
    // glueAdmin is glue:*, so widening its resources would hand
    // glue:CreateConnection / glue:UpdateConnection / glue:DeleteConnection on
    // every connection in the account to every one of its consumers —
    // gluePipelineRole (:384), qualityChecksRole (:653) and runPipelineRole
    // (:656). The last two have no connection business of any kind.
    // Read-wide and write-narrow is the point. Covered by the generated
    // AwsSolutions-IAM5[Resource::...:connection/*] acknowledgment. Pinned by
    // test/foundation-iam.test.ts.
    this.gluePipelineRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetConnection', 'glue:GetConnections', 'glue:GetTags'],
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:connection/*`],
    }));
    this.gluePipelineRole.addToPolicy(sfnTaskCallbackStatement());

    this.infrastructureRole = lambdaRole('Infrastructure', [bedrockPolicy, agentCorePolicy], [
      s3ReadWrite,
      // The IAM actions are s3:GetBucketNotification / s3:PutBucketNotification;
      // the '...Configuration' names used until 2026-10-12 are the API names and
      // granted nothing, so pipelines_crud._ensure_s3_eventbridge_enabled had
      // been getting AccessDenied and logging its warning on every create.
      new iam.PolicyStatement({ actions: ['s3:GetBucketNotification', 's3:PutBucketNotification'], resources: [`arn:aws:s3:::adi-*-${account}-${region}`] }),
      new iam.PolicyStatement({ actions: ['s3tables:*'], resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`] }),
      new iam.PolicyStatement({ actions: ['states:StartExecution', 'states:DescribeExecution', 'states:StopExecution', 'states:ListExecutions'], resources: [`arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*`] }),
      // The dashboard cancel (cancel_run in handlers/pipelines_runs.py) describes
      // and stops the EXECUTION of a run. Step Functions evaluates DescribeExecution and
      // StopExecution against the execution ARN, which the stateMachine-ARN
      // statement above does not match, so without this the stop is denied
      // (inferred from the policy text, not simulated).
      new iam.PolicyStatement({ actions: ['states:DescribeExecution', 'states:StopExecution'], resources: [`arn:aws:states:${region}:${account}:execution:agentic-data-ingestion-*:*`] }),
      // cancel_run stops the Glue job of a run when the job is still running.
      // glue:GetJobRun, which it reads first, comes from the glue:Get* of AgentCorePolicy.
      // NAMING FENCE (p31 C3): both job/ prefix forms; see that statement.
      new iam.PolicyStatement({ actions: ['glue:BatchStopJobRun'], resources: [`arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`] }),
      // SendTask* callback actions do NOT support resource-level permissions and require Resource: '*'.
      new iam.PolicyStatement({ actions: ['states:SendTaskSuccess', 'states:SendTaskFailure'], resources: ['*'] }),
      new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [`arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_*`] }),
      new iam.PolicyStatement({ actions: ['events:PutRule', 'events:DeleteRule', 'events:DescribeRule', 'events:EnableRule', 'events:DisableRule', 'events:PutTargets', 'events:RemoveTargets', 'events:ListTargetsByRule'], resources: [`arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-*`] }),
      // Detect-schema: allow reading any Glue table and S3 Tables table
      new iam.PolicyStatement({ actions: ['glue:GetTable', 'glue:GetDatabase'], resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/*`, `arn:aws:glue:${region}:${account}:table/*/*`] }),
      new iam.PolicyStatement({ actions: ['s3tables:GetTable'], resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`] }),
      // CloudWatch metrics: allow reading metrics for the dashboard metrics endpoint
      new iam.PolicyStatement({ actions: ['cloudwatch:GetMetricData', 'cloudwatch:ListMetrics', 'cloudwatch:GetMetricStatistics'], resources: ['*'] }),
      // CloudWatch Logs: allow querying logs for dashboard step log retrieval
      new iam.PolicyStatement({ actions: ['logs:StartQuery', 'logs:GetQueryResults', 'logs:DescribeLogGroups', 'logs:DescribeLogStreams', 'logs:FilterLogEvents', 'logs:GetLogEvents'], resources: ['*'] }),
      // SQS: allow peeking at pipeline trigger queue for queued files display
      new iam.PolicyStatement({ actions: ['sqs:ReceiveMessage', 'sqs:GetQueueAttributes'], resources: [`arn:aws:sqs:${region}:${account}:agentic-data-ingestion-pipeline-triggers-*`] }),
    ]);
    this.infrastructureRole.assumeRolePolicy?.addStatements(new iam.PolicyStatement({ actions: ['sts:AssumeRole'], principals: [new iam.ServicePrincipal('events.amazonaws.com')] }));
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    // AgentCore Memory: allow dashboard API to read/write mapping memory
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:BatchCreateMemoryRecords',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:ListMemoryRecords',
        'bedrock-agentcore:DeleteMemoryRecord',
        'bedrock-agentcore:BatchDeleteMemoryRecords',
        'bedrock-agentcore:BatchUpdateMemoryRecords',
        // 'bedrock-agentcore:CreateMemorySession' was listed here until
        // 2026-10-12; it is an SDK helper (MemorySessionManager), not an IAM
        // action, and the calls it makes are the CreateEvent/ListEvents above.
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:memory/*`],
    }));
    // RDS discovery: allow listing RDS instances and Aurora clusters
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['rds:DescribeDBInstances', 'rds:DescribeDBClusters'],
      resources: ['*'],
    }));
    // Glue connection discovery + creation: allow reading and creating Glue JDBC connections.
    // CreateConnection with a `Tags` argument additionally requires
    // glue:TagResource — Glue evaluates the tagging permission against the
    // catalog resource ARN (not the not-yet-created connection ARN), so the
    // catalog must be in scope for TagResource or CreateConnection fails with
    // "not authorized to perform: glue:TagResource on resource: ...:catalog".
    // GetTags / UntagResource round out the connection tag lifecycle used by
    // the pipeline-delete cleanup path.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'glue:GetConnections',
        'glue:GetConnection',
        'glue:CreateConnection',
        'glue:TagResource',
        'glue:UntagResource',
        'glue:GetTags',
      ],
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:connection/*`],
    }));
    // Glue connection delete/update: pipeline-creation rollback and
    // pipeline-delete cleanup paths call DeleteConnection (the P22
    // delete_connection fix) and UpdateConnection. Scoped to agentic-data-ingestion-*
    // connections — narrower than the broad CreateConnection grant above,
    // not a broadening of it.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:DeleteConnection', 'glue:UpdateConnection'],
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:connection/agentic-data-ingestion-*`],
    }));
    // Glue crawler lifecycle: wizard-time discovery crawlers, per-pipeline
    // runtime crawlers, the daily discovery-cleanup janitor (GetCrawlers
    // paginated list-and-tag loop), and pipeline-delete cleanup all run in
    // the dashboard API Lambda (infrastructureRole). The role has no glue:*
    // wildcard, so these are granted as a dedicated least-privilege
    // statement scoped to agentic-data-ingestion-* crawlers.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'glue:CreateCrawler',
        'glue:UpdateCrawler',
        'glue:GetCrawler',
        'glue:StartCrawler',
        'glue:DeleteCrawler',
        'glue:TagResource',
        'glue:GetTags',
        'glue:UntagResource',
      ],
      resources: [`arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`],
    }));
    // glue:GetCrawlers is a list-all API that does NOT support resource-level
    // scoping (it enumerates every crawler in the account), so it must be
    // granted on Resource:'*'. The daily discovery-cleanup janitor
    // (cleanup_discovery_resources) paginates GetCrawlers to find and reap
    // stale agentic-data-ingestion-discover-* crawlers; scoping it to a crawler
    // ARN returns AccessDenied and silently disables the janitor. Covered by
    // the existing AwsSolutions-IAM5[Resource::*] acknowledgment for this stack.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetCrawlers'],
      resources: ['*'],
    }));
    // Glue discovery-DB tables: prefix-based table lookup after a wizard-time
    // crawler completes, plus the janitor / per-pipeline delete cleanup
    // (get_tables + delete_table). The role's existing glue:GetTable grant is
    // on table/*/* but lacks GetTables (plural) and DeleteTable; this scoped
    // statement adds them on the discovery DB ARNs only.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetTables', 'glue:GetTable', 'glue:DeleteTable'],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion_jdbc_discovery_*`,
        `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion_jdbc_discovery_*/*`,
      ],
    }));
    // Glue crawler PassRole (P17): the wizard-time + runtime JDBC discovery
    // crawlers created by the dashboard API (handlers/jdbc_resources.py +
    // pipelines_jdbc.py) execute as gluePipelineRole. glue:CreateCrawler
    // therefore requires iam:PassRole on that role, scoped to Glue via the
    // iam:PassedToService condition. Without this grant CreateCrawler returns
    // AccessDenied and the "Detect Schema" endpoint surfaces an HTTP 500.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [this.gluePipelineRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'glue.amazonaws.com' } },
    }));
    // EC2 Describe: the VPC S3-Gateway-Endpoint pre-flight check in
    // detect_schema_start (jdbc_resources + vpc_resources) validates the
    // customer RDS VPC has an S3 Gateway Endpoint before creating a crawler.
    // ec2:DescribeVpcs additionally backs the manual JDBC connection mode's
    // VPC dropdown (P20 Req 5.7) — handlers/vpc_resources.py calls
    // ec2:DescribeVpcs to populate the list; without this grant that endpoint
    // returns AccessDenied. EC2 Describe APIs do not support resource-level
    // permissions, so Resource:'*' is required.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeVpcs', 'ec2:DescribeSubnets', 'ec2:DescribeSecurityGroups', 'ec2:DescribeRouteTables'],
      resources: ['*'],
    }));
    // Secrets Manager: allow listing all secrets (ListSecrets requires * resource)
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:ListSecrets'],
      resources: ['*'],
    }));
    // Secrets Manager: allow reading agentic-data-ingestion/* secret values
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [`arn:aws:secretsmanager:${region}:${account}:secret:agentic-data-ingestion/*`],
    }));
    // p35 R3.1: the dashboard API deletes an uploaded schema's backing object.
    // s3ReadWrite (:235-238) enumerates Get/Put/List/Head/CreateBucket and NOT
    // DeleteObject, verified implicitDeny. Scoped to the ONE bucket a schema
    // delete may touch rather than widening s3ReadWrite for every role that
    // shares it — R3.3 makes catalog-derived schemas undeletable, so no other
    // bucket is ever a delete target on this path. Widening s3ReadWrite instead
    // was rejected: it is shared by codeGenerationRole (:355), codeTestingRole
    // (:360), gluePipelineRole (:378) and this role (:440), so it would hand a
    // delete on every adi-* bucket to three roles with no delete to perform.
    // The 7c9378d lesson (LGR-7c9378d-1) is about the cost of a missing grant,
    // not a licence to over-grant.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:DeleteObject'],
      resources: [`${this.targetSchemasBucket.bucketArn}/*`],
    }));

    this.qualityChecksRole = lambdaRole('QualityChecks', [], [glueAdmin]);
    this.qualityChecksRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));

    this.runPipelineRole = lambdaRole('RunPipeline', [], [glueAdmin]);
    this.runPipelineRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    // SendTask* callback actions do NOT support resource-level permissions and require Resource: '*'.
    // run_pipeline reports Glue job completion/failure back to the waitForTaskToken
    // RunPipeline state via SendTaskSuccess/SendTaskFailure; without these the
    // callback path raises AccessDeniedException and the execution hangs until timeout.
    this.runPipelineRole.addToPolicy(new iam.PolicyStatement({ actions: ['states:SendTaskSuccess', 'states:SendTaskFailure', 'states:SendTaskHeartbeat'], resources: ['*'] }));

    this.approvalConfigRole = lambdaRole('ApprovalConfig', [], [
      // dynamodb:DeleteItem (P28b R3): the approval/timeout handlers delete
      // the task-token record AFTER a confirmed SendTask* callback
      // (send-before-status ordering) — the delete needs its own grant.
      new iam.PolicyStatement({ actions: ['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan'], resources: [`arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*`, `arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion*/index/*`] }),
      new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }),
      new iam.PolicyStatement({ actions: ['states:SendTaskSuccess', 'states:SendTaskFailure', 'states:SendTaskHeartbeat'], resources: ['*'] }),
      // P28b R2: the notification service's CloudWatch channel
      // (shared/notification_service.py CloudWatchChannel) publishes
      // critical-alert metrics and maintains matching alarms; without these
      // grants every critical notification under this role dies with
      // AccessDenied. PutMetricData defines no resource types, so it is
      // bounded by the cloudwatch:namespace condition to exactly the
      // namespace the channel emits to. The per-approval-id
      // create_timeout_alarm path (namespace AgenticDataIngestion/Approvals,
      // prefix AgenticDataIngestion-Approval-Timeout-) intentionally stays
      // OUTSIDE this grant (recorded p28b R6.1 decision).
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricData'],
        resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'AgenticDataIngestion/Alerts' } },
      }),
      // PutMetricAlarm supports the alarm resource type (AWS service
      // authorization reference), so it is scoped to the channel's
      // critical-alert alarm-name prefix.
      new iam.PolicyStatement({
        actions: ['cloudwatch:PutMetricAlarm'],
        resources: [`arn:aws:cloudwatch:${region}:${account}:alarm:AgenticDataIngestion-Critical-*`],
      }),
      // DescribeAlarms: the R2.3 documented fallback (exercised during the
      // p28b task 6.1 live check). The channel describes by AlarmNamePrefix,
      // and IAM authorizes such requests against alarm:* — a prefix-scoped
      // resource denies in practice (verified live: AccessDenied on
      // DescribeAlarms with the AgenticDataIngestion-Critical-* scope), so
      // this single read-only action gets its own Resource * statement.
      new iam.PolicyStatement({
        actions: ['cloudwatch:DescribeAlarms'],
        resources: ['*'],
      }),
    ]);
    this.approvalConfigRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock-agentcore:BatchCreateMemoryRecords',
        'bedrock-agentcore:RetrieveMemoryRecords',
        'bedrock-agentcore:CreateEvent',
        'bedrock-agentcore:ListEvents',
        'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:ListMemoryRecords',
      ],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:memory/*`],
    }));

    this.jobCompletionRole = lambdaRole('JobCompletion', [], [
      // NAMING FENCE (p31 C3) — both job/ prefix forms are required; do NOT delete
      // either. Snake covers Glue databases/tables (agentic_data_ingestion*), kebab
      // covers the job names job_builders.py:442 actually creates. See the
      // AgentCorePolicy glue statement (~line 188) for the verified AccessDenied.
      new iam.PolicyStatement({ actions: ['glue:GetJobRun', 'glue:GetJob'], resources: [`arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`] }),
      // SendTask* callback actions do NOT support resource-level permissions and require Resource: '*'.
      new iam.PolicyStatement({ actions: ['states:SendTaskSuccess', 'states:SendTaskFailure'], resources: ['*'] }),
      // When the dashboard cancelled a run whose Glue job was running, the
      // execution is left waiting until Glue reports the job stopped; then
      // job_completion (_finish_cancelled_run) describes and stops it. Execution
      // ARN, not stateMachine ARN, for the same reason as the infrastructure role grant.
      new iam.PolicyStatement({ actions: ['states:DescribeExecution', 'states:StopExecution'], resources: [`arn:aws:states:${region}:${account}:execution:agentic-data-ingestion-*:*`] }),
    ]);
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({ actions: ['events:PutEvents'], resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`] }));
    // p39 R2.1: job_completion publishes one GlueJob.RunCostEstimate datapoint per
    // terminal Glue run (`_publish_run_cost` -> `metrics_facade.publish_glue_run_cost`),
    // and that metric is the ONLY source of the dashboard's Est. Monthly Cost tile
    // and its cost trend. The role never had the grant: the IAM policy simulator
    // answers implicitDeny for PutMetricData on this role, and on 2026-10-01 all 8
    // job_completion invocations in the primary dev account logged
    // "Failed to flush direct metrics batch: ... AccessDenied ... PutMetricData".
    // The facade's flush swallows the error by design (best-effort, after the
    // callback), so the cost tile read Not established with nothing failing.
    //
    // PutMetricData defines no resource types, so it is Resource '*' bounded by
    // the cloudwatch:namespace condition: the same shape approvalConfigRole uses
    // for AgenticDataIngestion/Alerts above. Do NOT widen the namespace; this
    // role publishes into AgenticDataIngestion/Pipeline only.
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
      conditions: { StringEquals: { 'cloudwatch:namespace': 'AgenticDataIngestion/Pipeline' } },
    }));
    // P28 R9.3: job_completion fire-and-forgets the self-healing payload to
    // the dispatch Lambda (InvocationType: Event) — scoped to that single
    // function.
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({ actions: ['lambda:InvokeFunction'], resources: [`arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_self_healing_dispatch`] }));
    // job_completion's asynchronous on-failure destination is the Glue job
    // monitor DLQ (orchestration-stack.ts, JobCompletionAsyncInvokeConfig).
    // Lambda delivers the failure record with the function's own execution
    // role, so the role needs SendMessage on that one queue. Named rather than
    // referenced: the queue lives in Orchestration, which deploys after this
    // stack, and Foundation owns every Lambda role's grants (LGR-b46a5bb-3).
    // The name must match GlueJobMonitorDLQ's queueName.
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sqs:SendMessage'],
      resources: [`arn:aws:sqs:${region}:${account}:agentic-data-ingestion-glue-job-monitor-dlq-${environment}`],
    }));
    // p35 R7.3: job_completion resolves the target table's metadataLocation after a
    // SUCCEEDED Glue run and writes it into the Glue mirror. Three actions, all
    // three verified implicitDeny on this role before this change (IAM policy
    // simulator, 777788889999 us-west-2, against real resource ARNs).
    //
    // glue:GetTable is NOT redundant. Glue UpdateTable REPLACES the stored table
    // with the TableInput it is given — create_infrastructure:313-333 only gets
    // away without a read because it still holds warehouse_location from its own
    // s3tables:GetTable. job_completion holds none of that, so its write is
    // read-modify-write or it destroys the StorageDescriptor and the Columns
    // R7.7 maintains.
    //
    // Do NOT "simplify" this by attaching agentCorePolicy: it grants
    // bedrock-agentcore:InvokeAgentRuntime on Resource '*' and would turn this
    // narrow role into a generic invoke-any-agent capability (LGR-b46a5bb-2,
    // template/ratified, which rejected exactly that move for agentDispatchRole).
    // It is also incomplete for R7.3 — its Glue statement (~line 184) is read-only,
    // so it would not authorize glue:UpdateTable at all.
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3tables:GetTable'],
      resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`],
    }));
    this.jobCompletionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetTable', 'glue:UpdateTable'],
      // Snake-case only: the mirror database is agentic_data_ingestion_{bucket}
      // (create_infrastructure:272) and Glue forbids hyphens in database names.
      // This is NOT the p31 C3 naming fence — that fence is about JOB and CRAWLER
      // names, which this role already covers at :669 and which this statement
      // does not touch. Do not "make it consistent" by adding a kebab form here.
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`,
        `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`,
      ],
    }));

    // P28 R9.3: the self-healing dispatch Lambda owns the long synchronous
    // AgentCore call. InvokeAgentRuntime is scoped to the self-healing
    // runtime prefix (same shape as schemaMappingRole's complex-mapping
    // grant — CloudFormation appends a unique suffix to the runtime id, so
    // a name-prefix wildcard is required and avoids a cross-stack cycle).
    this.selfHealingDispatchRole = lambdaRole('SelfHealingDispatch', [], []);
    this.selfHealingDispatchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_self_healing-*`],
    }));

    // p30a R1.2: the GENERIC agent dispatch Lambda is the self-healing
    // dispatcher's generalisation — it owns the long synchronous AgentCore
    // call for callers that cannot make it themselves. Two such callers:
    // the weekly cost-optimization EventBridge schedule (AgentCore Runtime is
    // not an EventBridge target at all) and the dashboard API's on-demand
    // agent endpoints (a Lambda handler cannot keep a thread alive past its
    // return — the environment freezes).
    //
    // InvokeAgentRuntime is ENUMERATED per agent rather than wildcarded over
    // `runtime/*`. Each entry keeps the same name-prefix shape as the
    // self-healing grant above (CloudFormation appends a unique suffix to the
    // runtime id, and name-scoping avoids a cross-stack cycle with the
    // AgentCore stack). The cost of enumeration is that routing a new agent
    // through the dispatcher requires a deliberate edit here — which is the
    // point.
    this.agentDispatchRole = lambdaRole('AgentDispatch', [], []);
    this.agentDispatchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:InvokeAgentRuntime'],
      resources: [
        // R1.1 — weekly cost-optimization schedule.
        `arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_cost_optimization-*`,
        // R2.5 — data quality. Its on-demand route (trigger_quality_check)
        // was removed on 2026-10-11 and its EventBridge rule remains on SNS
        // (R1.5), so nothing dispatches it today. The grant is kept so the
        // agent can be finished without re-plumbing (docs/AGENTS.md § 3).
        `arn:aws:bedrock-agentcore:${region}:${account}:runtime/agentic_data_ingestion_agent_data_quality-*`,
      ],
    }));
    // p30a R1.2: the dashboard API fire-and-forgets both on-demand agent
    // requests to the dispatcher (InvocationType: Event). The role already
    // holds a project-wide `function:agentic_data_ingestion_*` invoke grant
    // (see infrastructureRole above), so this statement is narrower than what
    // is already in force rather than additive. It is stated explicitly
    // because the dependency is load-bearing: if that wildcard is ever
    // tightened, the agent endpoints must not silently start returning 202
    // for work that never dispatched.
    this.infrastructureRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_agent_dispatch`],
    }));

    this.pipelineTriggerRole = lambdaRole('PipelineTrigger', [], [
      new iam.PolicyStatement({
        actions: ['sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:GetQueueAttributes'],
        resources: [`arn:aws:sqs:${region}:${account}:agentic-data-ingestion-pipeline-triggers-*`],
      }),
      new iam.PolicyStatement({
        actions: ['states:StartExecution', 'states:ListExecutions'],
        resources: [`arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*`],
      }),
    ]);

    this.eventBridgeRole = new iam.Role(this, 'EventBridgeSfnRole', {
      roleName: `${projectName}-${environment}-eventbridge-sfn-role`,
      assumedBy: new iam.ServicePrincipal('events.amazonaws.com'),
    });
    this.eventBridgeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['states:StartExecution'],
      resources: [`arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*`],
    }));

    // ── Verification session role (real-Spark verification, §4.6) ──
    // The runtime role a Glue interactive session assumes to verify
    // AI-generated PySpark. READ-ONLY BY DESIGN: three statements, no write
    // action of any kind — no s3:PutObject, no s3tables:*, no DynamoDB, no
    // Secrets Manager, no glue:CreateTable.
    //
    // WHY A NEW ROLE. The zero-work option was to reuse the per-pipeline
    // AgenticDataIngestion-<pipeline>-ExecutionRole: it already trusts
    // glue.amazonaws.com and already sits inside the iam:PassRole grant on
    // `role/AgenticDataIngestion-*-ExecutionRole` above. It was REJECTED
    // because that role holds write access to the pipeline's target, and the
    // code a verification session executes is AI-generated and — at the point
    // the test step runs — unreviewed by a human: the code-approval gate is
    // DOWNSTREAM of TestCode (orchestration-stack.ts, CheckCodeApproval after
    // TestResultChoice). Reusing it would let unreviewed generated PySpark
    // write to the data it is supposed to be verifying against. The widening
    // is also not incremental: today's Code Interpreter session holds no AWS
    // credentials for pipeline data at all (code_interpreter_runner.py:382-391
    // passes an identifier, a name and a timeout — no role), so reuse would
    // introduce target-write capability into a step that has never had any.
    //
    // The role name deliberately does NOT end in `-ExecutionRole`, so the
    // gluePipelineRole grants above (iam:CreateRole / iam:PassRole on
    // `AgenticDataIngestion-*-ExecutionRole`) cannot reach it. Enumerating
    // rather than wildcarding follows LGR-b46a5bb-2's posture: widening this
    // role must require a deliberate policy edit.
    //
    // TWO roles pass this role, and both sets of caller-side grants (Glue
    // session reach + iam:PassRole conditioned on iam:PassedToService:
    // glue.amazonaws.com) are stated below, after this role exists:
    // testCodeRuntimeRole, which is the intended route because the live path
    // runs in the container, and codeTestingRole, which is the inline
    // break-glass route taken when USE_AGENTCORE_RUNTIME is off. Nothing about
    // THIS role's own permissions differs between the two — it stays read-only
    // per R6.1-R6.3 regardless of who passes it.
    this.verificationSessionRole = new iam.Role(this, 'VerificationSessionRole', {
      roleName: 'AgenticDataIngestion-VerificationSession-Role',
      assumedBy: new iam.ServicePrincipal('glue.amazonaws.com'),
      description:
        'Read-only runtime role for Glue interactive sessions that verify AI-generated ' +
        'PySpark. Grants source reads, catalog reads and log writes only; never a write ' +
        'to a pipeline target, a catalog object, a table bucket, a DynamoDB item or a secret.',
    });
    // Source sample reads, on BOTH the source-files and processed-files
    // buckets. An earlier version of this statement named processedBucket
    // alone and asserted it was "where the sampled source objects live, and
    // the only bucket a verification session has any reason to open". That was
    // wrong for the S3-upload path, and the URI trace is the evidence:
    // file_validation writes `original_s3_uri = f"s3://{bucket}/{key}"` from
    // the upload event's own bucket (file_validation/lambda_function.py:632),
    // and that bucket is sourceBucket — `adi-source-files-*`, the
    // eventBridgeEnabled one above, the one S3UploadTriggerRule watches
    // (orchestration-stack.ts:1330-1348) and the one both SOURCE_BUCKET and
    // UPLOAD_BUCKET name (compute-stack.ts:702, :704). schema_extract carries
    // that value through unchanged (:904 → :960 → the `json_s3_uri` it returns
    // at :1021), which Step Functions maps onto the TestCode payload as
    // `source_data_uri` (orchestration-stack.ts:594, via `source_sample_uri`, same value here). So on every S3-upload
    // run the URI a sample is read from names sourceBucket, and a
    // processed-only grant denies it — an AccessDenied raised at the
    // frame-build statement, inside the session, where it reads as a failed
    // transform rather than as a failed harness.
    //
    // processedBucket stays: the legacy converted-JSON path
    // (schema_extract:961-970) makes json_s3_uri the canonical URI, and that
    // object lives there.
    //
    // The two roles that WRITE that URI already hold exactly this four-resource
    // pair in a single statement — fileValidationRole (:257) and
    // schemaExtractRole (:262). The consumer of the URI holding half of what
    // its producers hold was the defect; this makes the read side agree with
    // the write side, minus the s3:PutObject those two also carry.
    //
    // ONE statement listing four resources, not two statements. The action set
    // is identical for both buckets and there is no condition distinguishing
    // them, so a second statement would duplicate the actions array to say
    // nothing new; :257 and :262 are the in-file precedent for the shape. Three
    // statements is also this role's whole documented shape, pinned by
    // test_verification_session_role_iam.py's `len(statements) == 3`, and
    // LGR-fb2310b-1 rejected exactly this "add another statement enumerating
    // bucket ARNs" move in this file.
    //
    // Both ARN forms per bucket are required because s3:ListBucket authorizes
    // against the bucket ARN while s3:GetObject authorizes against the object
    // ARN. Still no PutObject — a verification session that can write is not a
    // verification session — and still enumerated rather than `adi-*`, which
    // would also reach the target schemas and the generated code.
    this.verificationSessionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [
        this.sourceBucket.bucketArn,
        `${this.sourceBucket.bucketArn}/*`,
        this.processedBucket.bucketArn,
        `${this.processedBucket.bucketArn}/*`,
      ],
    }));
    // Glue Data Catalog READS only, so a transform that resolves a catalog
    // table can be executed. GetDatabase/GetTable/GetPartitions and nothing
    // else — no CreateTable, no UpdateTable, no DeleteTable.
    //
    // Only the snake_case prefix appears here, and that is not a missing
    // naming fence: the dual snake+kebab forms exist for jobs, crawlers and
    // connections (created kebab at runtime), and this statement enumerates
    // no job, crawler or connection resource. Glue forbids hyphens in database
    // and table names, so agentic_data_ingestion* is the only form these two
    // resource types can take.
    this.verificationSessionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetDatabase', 'glue:GetTable', 'glue:GetPartitions'],
      resources: [
        `arn:aws:glue:${region}:${account}:catalog`,
        `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`,
        `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`,
      ],
    }));
    // Session logs. Glue writes interactive-session output to service-owned
    // /aws-glue/* log groups whose names carry a runtime session id, so the
    // group cannot be enumerated at deploy time. CreateLogStream and
    // PutLogEvents only — no CreateLogGroup, no Describe, no read.
    this.verificationSessionRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${region}:${account}:log-group:/aws-glue/*`,
        `arn:aws:logs:${region}:${account}:log-group:/aws-glue/*:*`,
      ],
    }));

    // ── AgentCore runtime roles (threat model R-02) ──────────────────────
    //
    // Every AgentCore runtime used to share ONE role holding the union of what
    // nine agents and five MCP servers might need: DynamoDB on every
    // `agentic_data_ingestion*` table, S3 read/write on every `adi-*` bucket,
    // glue:* on project resources, Bedrock, Step Functions callbacks, IAM role
    // creation and PassRole. So code running in the agent that reads chat text
    // (nl_pipeline_builder) or sample values (complex_mapping) held the job
    // builder's IAM power and the pipeline steps' SendTaskSuccess.
    //
    // Now each agent runtime has its own role, and the MCP servers share one
    // read-only role. A role carries only what its own code calls, inventoried
    // from the container entrypoint plus the Lambda module it wraps
    // (agentcore/<agent>/main.py, lambda/agentic_data_ingestion_<step>/) and the
    // shared modules those import. Grants the shared role carried that NO
    // runtime calls are gone: the `*-s3tablemetadata-*` buckets,
    // sts:GetCallerIdentity (needs no permission), s3:HeadBucket/s3:HeadObject
    // (not IAM actions; HEAD authorizes as s3:GetObject / s3:ListBucket),
    // states:SendTaskHeartbeat, states:DescribeExecution,
    // bedrock-agentcore:InvokeAgentRuntime (containers run with MCP_ENABLED
    // 'false', so they open no MCP session), GetCodeInterpreterSession,
    // bedrock:GetFoundationModel/ListFoundationModels, and
    // secretsmanager:GetSecretValue (since T-19 no runtime reads a JDBC secret;
    // the Glue job reads it under its own role).
    //
    // An action a runtime's code calls but its role lacks is an AccessDenied at
    // run time, not at deploy. tests/cdk/stacks/test_agentcore_runtime_roles.py
    // pins which role each runtime uses and the capabilities each must NOT
    // have; widen a role here, deliberately, when its code gains a call.
    //
    // Two conventions hold on every role. The task-tokens Deny (T-22,
    // LGR-8f59603-3) stays even though no role now grants that table, so a
    // later widening to a table wildcard cannot reopen it. And every bucket
    // grant names `adi-*-${account}-${region}` or a specific bucket, never `*`.
    // Index ARNs only where the statement can Query: a GSI is reachable only
    // through Query/Scan, so a put-only or get-only grant has no use for one.
    const runtimeTables = (withIndexes: boolean, ...names: string[]): string[] => names.flatMap((name) => [
      `arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_${name}`,
      ...(withIndexes ? [`arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_${name}/index/*`] : []),
    ]);
    const DDB_READ = ['dynamodb:GetItem', 'dynamodb:Query', 'dynamodb:BatchGetItem'];
    const DDB_READ_WRITE = [...DDB_READ, 'dynamodb:PutItem', 'dynamodb:UpdateItem'];
    const ddb = (actions: string[], ...tables: string[]) =>
      new iam.PolicyStatement({
        actions,
        resources: runtimeTables(actions.some((a) => a === 'dynamodb:Query' || a === 'dynamodb:Scan'), ...tables),
      });
    const adiBucketsRead = () => new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [`arn:aws:s3:::adi-*-${account}-${region}`, `arn:aws:s3:::adi-*-${account}-${region}/*`],
    });
    const putObjects = (...objectArns: string[]) =>
      new iam.PolicyStatement({ actions: ['s3:PutObject'], resources: objectArns });
    const bedrockInvoke = () => new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: ['*'],
    });
    const pipelineEventsPut = () => new iam.PolicyStatement({
      actions: ['events:PutEvents'],
      resources: [`arn:aws:events:${region}:${account}:event-bus/agentic-data-ingestion-*`],
    });
    // P28 R19.11: the four container-backed pipeline steps run under
    // waitForTaskToken task states, and the container closes the loop itself
    // with the token the proxy Lambda forwarded (shared/sfn_task_token.py).
    // SendTask* support no resource-level permissions, hence Resource '*'. Only
    // those four roles get it; no autonomous agent completes a task state.
    const sfnTaskCallback = () => new iam.PolicyStatement({
      actions: ['states:SendTaskSuccess', 'states:SendTaskFailure'],
      resources: ['*'],
    });
    // Project jobs, both naming forms. NAMING FENCE (p31 C3): do NOT delete
    // either form; see the AgentCorePolicy glue statement above for the
    // verified glue:GetJob AccessDenied this prevents.
    const projectGlueJobs = [
      `arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`,
      `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`,
    ];
    const glueJobRolePass = () => new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [glueJobRoleArnPattern],
      conditions: { StringEquals: { 'iam:PassedToService': 'glue.amazonaws.com' } },
    });

    // The retired shared role, kept for ONE release with no permissions at all.
    //
    // Existing deployments' AgentCore stack imports this role's ARN through a
    // CDK auto-export. Foundation deploys before AgentCore, so deleting the role
    // here would fail the Foundation update with "export ... is in use" until
    // AgentCore stopped importing it, and AgentCore cannot stop until Foundation
    // has deployed the new roles. Keeping the role (same construct id, so same
    // physical role) and pinning its export breaks that cycle: the runtimes move
    // to their new roles in the AgentCore deploy that follows, and between the
    // two deploys anything still on this role simply has no permissions.
    //
    // REMOVE this block and the exportValue in the next release, once every
    // deployment has applied this one. Pinned by agentcore-runtime-roles.test.ts,
    // which also checks no runtime uses it and it grants nothing.
    const retiredSharedRuntimeRole = new iam.Role(this, 'AgentCoreRuntimeRole', {
      roleName: `${projectName}-${environment}-agentcore-runtime-role`,
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Retired by R-02 (per-runtime roles). No permissions. Kept one release for the cross-stack export; remove next release.',
    });
    this.exportValue(retiredSharedRuntimeRole.roleArn);

    /** A role AgentCore can assume, with what every runtime needs to start and report. */
    const runtimeRole = (id: string, roleSuffix: string, description: string): iam.Role => {
      const role = new iam.Role(this, id, {
        roleName: `${projectName}-${environment}-agentcore-${roleSuffix}-role`,
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
        description,
      });
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer', 'ecr:BatchCheckLayerAvailability'],
        resources: ['*'],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams', 'logs:DescribeLogGroups'],
        resources: [`arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*`, `arn:aws:logs:${region}:${account}:log-group:/aws/spans/*`],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
        resources: ['*'],
      }));
      role.addToPolicy(new iam.PolicyStatement({ actions: ['cloudwatch:PutMetricData'], resources: ['*'] }));
      // Threat model T-22: no AgentCore runtime may touch the task-tokens
      // table, which holds approval tokens and (since R-02's companion change)
      // the RunPipeline Glue-callback token. Only the approval handler, the
      // approval timeout handler, run_pipeline, job_completion and the
      // dashboard API use it, and none of them runs in AgentCore.
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'DenyTaskTokens',
        effect: iam.Effect.DENY,
        actions: ['dynamodb:*'],
        resources: [
          `arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_task_tokens`,
          `arn:aws:dynamodb:${region}:${account}:table/agentic_data_ingestion_task_tokens/*`,
        ],
      }));
      return role;
    };
    /** An agent runtime: the base plus the audit and trace writes every agent makes. */
    const agentRuntimeRole = (id: string, roleSuffix: string, description: string): iam.Role => {
      const role = runtimeRole(id, roleSuffix, description);
      // shared/audit_utils.py and shared/agent_trace_plugin.py: put_item only.
      role.addToPolicy(ddb(['dynamodb:PutItem'], 'audit_log', 'agent_traces'));
      return role;
    };

    // ── Pipeline-step runtimes ─────────────────────────────────────────

    // create_code: reads the mapping, the pipeline and source samples, writes
    // the generated script to the generated-code bucket and its record, runs a
    // Strands agent on Bedrock, publishes pipeline events.
    this.createCodeRuntimeRole = agentRuntimeRole('CreateCodeRuntimeRole', 'create-code',
      'AgentCore runtime role for the code-generation pipeline step');
    this.createCodeRuntimeRole.addToPolicy(ddb(DDB_READ_WRITE, 'state', 'generated_code'));
    this.createCodeRuntimeRole.addToPolicy(ddb(DDB_READ, 'pipelines', 'schema_mappings'));
    this.createCodeRuntimeRole.addToPolicy(adiBucketsRead());
    this.createCodeRuntimeRole.addToPolicy(putObjects(`${this.generatedCodeBucket.bucketArn}/*`));
    this.createCodeRuntimeRole.addToPolicy(bedrockInvoke());
    this.createCodeRuntimeRole.addToPolicy(pipelineEventsPut());
    this.createCodeRuntimeRole.addToPolicy(sfnTaskCallback());

    // test_code: verifies the generated script in a Glue interactive session
    // (or, on the non-default pandas executor, a Code Interpreter session),
    // records the result, writes test outputs, runs a Strands agent on Bedrock.
    this.testCodeRuntimeRole = agentRuntimeRole('TestCodeRuntimeRole', 'test-code',
      'AgentCore runtime role for the code-testing pipeline step');
    this.testCodeRuntimeRole.addToPolicy(ddb(DDB_READ_WRITE, 'state', 'test_results'));
    this.testCodeRuntimeRole.addToPolicy(ddb(DDB_READ, 'pipelines', 'generated_code'));
    this.testCodeRuntimeRole.addToPolicy(adiBucketsRead());
    this.testCodeRuntimeRole.addToPolicy(putObjects(`${this.testOutputsBucket.bucketArn}/*`));
    this.testCodeRuntimeRole.addToPolicy(bedrockInvoke());
    this.testCodeRuntimeRole.addToPolicy(pipelineEventsPut());
    this.testCodeRuntimeRole.addToPolicy(sfnTaskCallback());
    // The six session APIs glue_session_runner.py calls, plus glue:TagResource,
    // the same enumerated set codeTestingRole holds below (LGR-b46a5bb-2:
    // enumerate, so widening takes a deliberate edit). CreateSession passes
    // Tags, and Glue authorizes those as glue:TagResource on the new session:
    // without it CreateSession fails with "not authorized to perform:
    // glue:TagResource on resource: ...:session/adi-verify-...", verified on a
    // deployment on 2026-10-08 after the R-02 split dropped the shared role's
    // glue:*. Session ids are minted per run, hence session/*; the session
    // itself runs under the read-only VerificationSessionRole, not under this
    // role. Not part of the naming fence: sessions carry no project prefix.
    this.testCodeRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'glue:CreateSession',
        'glue:TagResource',
        'glue:GetSession',
        'glue:RunStatement',
        'glue:GetStatement',
        'glue:StopSession',
        'glue:DeleteSession',
      ],
      resources: [`arn:aws:glue:${region}:${account}:session/*`],
    }));
    // Pass the read-only verification session role to Glue (spec R6.7),
    // conditioned on iam:PassedToService like every other PassRole here. This
    // is now the ONLY route by which this runtime can pass that role: the old
    // unconditioned PassRole on `role/AgenticDataIngestion-*` is gone (T-03),
    // and this role holds no glueJobRolePass.
    this.testCodeRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [this.verificationSessionRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'glue.amazonaws.com' } },
    }));
    // Code Interpreter, used only when VERIFICATION_EXECUTOR is exactly
    // 'pandas' (code_interpreter_runner.py). The AWS-managed interpreter lives
    // in the `aws` account, so its ARN cannot be scoped to this account.
    this.testCodeRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:StartCodeInterpreterSession', 'bedrock-agentcore:InvokeCodeInterpreter', 'bedrock-agentcore:StopCodeInterpreterSession'],
      resources: ['*'],
    }));

    // create_glue_pipeline: the job builder. Creates and updates the Glue job,
    // its database and JDBC connection, creates the per-pipeline execution role
    // (bounded, T-03) and passes it to Glue, uploads jdbc_helpers.py to the
    // dependencies bucket, reads S3 Tables metadata for the target. No Bedrock
    // and no events: its Strands/MCP path needs MCP, which is off in
    // containers, and it publishes nothing.
    this.createGluePipelineRuntimeRole = agentRuntimeRole('CreateGluePipelineRuntimeRole', 'create-pipeline',
      'AgentCore runtime role for the Glue job-builder pipeline step');
    this.createGluePipelineRuntimeRole.addToPolicy(ddb(DDB_READ_WRITE, 'state', 'pipelines'));
    this.createGluePipelineRuntimeRole.addToPolicy(ddb(DDB_READ, 'generated_code'));
    this.createGluePipelineRuntimeRole.addToPolicy(adiBucketsRead());
    this.createGluePipelineRuntimeRole.addToPolicy(putObjects(`${this.dependenciesBucket.bucketArn}/*`));
    this.createGluePipelineRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3tables:GetTableBucket', 's3tables:ListTables', 's3tables:GetTable'],
      resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`],
    }));
    this.createGluePipelineRuntimeRole.addToPolicy(sfnTaskCallback());
    this.createGluePipelineRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:*'],
      // Snake_case covers databases/tables (Glue forbids hyphens there);
      // kebab-case covers jobs/crawlers/connections created at runtime.
      // NAMING FENCE (p31 C3) — do NOT delete either form. This is the step the
      // verified glue:GetJob AccessDenied came from; see the AgentCorePolicy
      // glue statement above.
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`, `arn:aws:glue:${region}:${account}:job/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-*`, `arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`, `arn:aws:glue:${region}:${account}:connection/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:connection/agentic-data-ingestion-*`],
    }));
    // glue_connection mode + test fixtures: a pipeline can reference a
    // PRE-EXISTING Glue Connection whose name does NOT follow the app's
    // agentic-data-ingestion-* convention (a customer-managed connection, or
    // the TestFixtures stack's agentglue-test-fixtures-* connections). The job
    // builder reads that connection at job-creation time, so these READ-ONLY
    // actions are granted account-wide: connection names are not known at
    // deploy time. Writes stay scoped to the glue:* statement above. The
    // gluePipelineRole carries the mirror of this grant (LGR-e8ed63a-1). Covered
    // by the generated AwsSolutions-IAM5[Resource::...:connection/*]
    // acknowledgment.
    this.createGluePipelineRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetConnection', 'glue:GetConnections', 'glue:GetTags'],
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:connection/*`],
    }));
    // Per-pipeline Glue job roles (iam_roles.create_pipeline_specific_role).
    // Threat model T-03: CreateRole / PutRolePolicy only with
    // GlueJobRoleBoundary attached; read and tag-on-create unconditioned;
    // PassRole split out and conditioned on glue.amazonaws.com, because
    // iam:PassedToService is present only on PassRole requests and would deny
    // CreateRole if it shared the statement. No AttachRolePolicy: the code
    // attaches no managed policy. This is the only runtime role that can create
    // a role at all.
    this.createGluePipelineRuntimeRole.addToPolicy(boundedGlueRoleWrite());
    this.createGluePipelineRuntimeRole.addToPolicy(glueRoleReadAndTag());
    this.createGluePipelineRuntimeRole.addToPolicy(glueJobRolePass());

    // create_infrastructure: creates the S3 Tables bucket, namespace and table
    // for an S3 Tables target, and the Glue mirror database and table
    // (shared/glue_mirror_identifiers.py, prefix agentic_data_ingestion_). No
    // Bedrock, no events, no audit or trace writes of its own (the base grants
    // those anyway; they are put-only).
    this.createInfrastructureRuntimeRole = agentRuntimeRole('CreateInfrastructureRuntimeRole', 'create-infra',
      'AgentCore runtime role for the infrastructure-creation pipeline step');
    this.createInfrastructureRuntimeRole.addToPolicy(ddb(DDB_READ_WRITE, 'state'));
    this.createInfrastructureRuntimeRole.addToPolicy(ddb(DDB_READ, 'pipelines'));
    this.createInfrastructureRuntimeRole.addToPolicy(adiBucketsRead());
    this.createInfrastructureRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3tables:*'],
      resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`, `arn:aws:s3tables:${region}:${account}:table/*`],
    }));
    this.createInfrastructureRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetDatabase', 'glue:CreateDatabase', 'glue:GetTable', 'glue:CreateTable', 'glue:UpdateTable'],
      // Databases and tables only, so only the snake_case form exists.
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`],
    }));
    this.createInfrastructureRuntimeRole.addToPolicy(sfnTaskCallback());

    // ── Autonomous-agent runtimes ──────────────────────────────────────

    // self_healing_agent: reads the failed run and pipeline, edits the Glue
    // job's configuration (UpdateJob, which passes the job's execution role),
    // re-triggers the pipeline, records a dashboard notification. No Bedrock.
    this.selfHealingRuntimeRole = agentRuntimeRole('SelfHealingRuntimeRole', 'self-healing',
      'AgentCore runtime role for the self-healing agent');
    this.selfHealingRuntimeRole.addToPolicy(ddb(DDB_READ_WRITE, 'state'));
    this.selfHealingRuntimeRole.addToPolicy(ddb(DDB_READ, 'pipelines'));
    this.selfHealingRuntimeRole.addToPolicy(ddb(['dynamodb:PutItem'], 'notifications'));
    this.selfHealingRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetJob', 'glue:UpdateJob'],
      resources: projectGlueJobs,
    }));
    this.selfHealingRuntimeRole.addToPolicy(glueJobRolePass());
    this.selfHealingRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['states:StartExecution'],
      resources: [`arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-*`],
    }));

    // complex_mapping_agent: reads source samples, retrieves past mappings
    // from AgentCore Memory, asks Bedrock, records its result. Read-only
    // everywhere except its own state row.
    this.complexMappingRuntimeRole = agentRuntimeRole('ComplexMappingRuntimeRole', 'complex-mapping',
      'AgentCore runtime role for the complex-mapping agent');
    this.complexMappingRuntimeRole.addToPolicy(ddb(['dynamodb:PutItem'], 'state'));
    this.complexMappingRuntimeRole.addToPolicy(adiBucketsRead());
    this.complexMappingRuntimeRole.addToPolicy(bedrockInvoke());
    this.complexMappingRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock-agentcore:RetrieveMemoryRecords', 'bedrock-agentcore:ListMemoryRecords'],
      resources: [`arn:aws:bedrock-agentcore:${region}:${account}:memory/*`],
    }));

    // data_quality_agent: writes its report under `dq-reports/` in the
    // processed bucket and records its result. Its Athena path holds no grant
    // today and still holds none: that is a known gap, not something this
    // split changes.
    this.dataQualityRuntimeRole = agentRuntimeRole('DataQualityRuntimeRole', 'data-quality',
      'AgentCore runtime role for the data-quality agent');
    this.dataQualityRuntimeRole.addToPolicy(ddb(['dynamodb:PutItem'], 'state'));
    this.dataQualityRuntimeRole.addToPolicy(putObjects(`${this.processedBucket.bucketArn}/dq-reports/*`));

    // cost_optimization_agent: reads job definitions, run history and DPU
    // metrics, scans past recommendations, records new ones. glue:GetJobs and
    // cloudwatch:GetMetricData support no resource scoping. glue:UpdateJob and
    // PassRole are NOT granted: they are reached only when AUTO_APPLY_ENABLED is
    // 'true', which no stack sets. Grant both here, deliberately, if that ever
    // changes.
    this.costOptimizationRuntimeRole = agentRuntimeRole('CostOptimizationRuntimeRole', 'cost-opt',
      'AgentCore runtime role for the cost-optimization agent');
    this.costOptimizationRuntimeRole.addToPolicy(ddb([...DDB_READ, 'dynamodb:Scan', 'dynamodb:PutItem'], 'state'));
    this.costOptimizationRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetJob', 'glue:GetJobRuns'],
      resources: projectGlueJobs,
    }));
    this.costOptimizationRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:GetJobs', 'cloudwatch:GetMetricData'],
      resources: ['*'],
    }));

    // nl_pipeline_builder: lists and samples files in the SOURCE bucket only
    // (discover_source also refuses any other bucket in code, T-05), asks
    // Bedrock, records its proposal. It writes no pipeline: creation goes
    // through POST /pipelines.
    this.nlPipelineBuilderRuntimeRole = agentRuntimeRole('NLPipelineBuilderRuntimeRole', 'nl-builder',
      'AgentCore runtime role for the natural-language pipeline builder');
    this.nlPipelineBuilderRuntimeRole.addToPolicy(ddb(['dynamodb:PutItem'], 'state'));
    this.nlPipelineBuilderRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:ListBucket'],
      resources: [this.sourceBucket.bucketArn, `${this.sourceBucket.bucketArn}/*`],
    }));
    this.nlPipelineBuilderRuntimeRole.addToPolicy(bedrockInvoke());

    // ── MCP server runtimes ────────────────────────────────────────────
    //
    // The five MCP servers run with MCP_ALLOW_WRITE and
    // MCP_ALLOW_SENSITIVE_DATA_ACCESS 'false' (agentcore-stack.ts mcpServerEnv),
    // and no container opens an MCP session (MCP_ENABLED 'false'). So their role
    // is read-only: Glue catalog and job reads on project resources, S3 and S3
    // Tables reads. No DynamoDB, no Bedrock, no write of any kind.
    this.mcpServerRuntimeRole = runtimeRole('McpServerRuntimeRole', 'mcp-server',
      'Read-only AgentCore runtime role shared by the MCP server runtimes');
    this.mcpServerRuntimeRole.addToPolicy(adiBucketsRead());
    this.mcpServerRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['glue:Get*', 'glue:List*', 'glue:BatchGet*'],
      // NAMING FENCE (p31 C3): both forms, as on every project Glue grant.
      resources: [`arn:aws:glue:${region}:${account}:catalog`, `arn:aws:glue:${region}:${account}:database/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:table/agentic_data_ingestion*/*`, ...projectGlueJobs, `arn:aws:glue:${region}:${account}:crawler/agentic_data_ingestion*`, `arn:aws:glue:${region}:${account}:crawler/agentic-data-ingestion-*`],
    }));
    this.mcpServerRuntimeRole.addToPolicy(new iam.PolicyStatement({
      actions: ['s3tables:Get*', 's3tables:List*'],
      resources: [`arn:aws:s3tables:${region}:${account}:bucket/*`],
    }));

    // ── The same two grants on the code-testing LAMBDA role ──────────
    //
    // WHY BOTH ROLES. The Glue verification executor is selected by the
    // VERIFICATION_EXECUTOR variable, which compute-stack.ts:477 and
    // agentcore-stack.ts:164 both set, and whose ARN dependency
    // (VERIFICATION_SESSION_ROLE_ARN) is likewise published to both. The live
    // path runs in the AgentCore container (USE_AGENTCORE_RUNTIME: 'true',
    // compute-stack.ts:432) under testCodeRuntimeRole, so the grants above are
    // what the intended route uses. But USE_AGENTCORE_RUNTIME is a flag, and
    // with it off the identical runner executes in this Lambda under
    // codeTestingRole — which held no Glue session action and no iam:PassRole at
    // all. The result was worse than an absent capability: resolve_effective_
    // executor sees a populated ARN and selects `glue`, so the configuration
    // reads as correct right up to an AccessDenied at CreateSession.
    //
    // This is a DELIBERATE WIDENING BEYOND R6.5/R6.7, which name the AgentCore
    // runtime role specifically. It makes the inline path a working break-glass
    // route; it does not make it the intended one. USE_AGENTCORE_RUNTIME stays
    // 'true'.
    //
    // VERIFICATION_EXECUTOR no longer 'stays pandas' — that half of this note
    // is reversed twice over. Both stacks publish 'glue' (LGR-87b8538-1, the
    // accepted deviation from R11.2), and the handler's code fall-through is
    // now 'glue' too, so an unset or misspelled variable also selects the Glue
    // runner. That makes these grants load-bearing on both paths rather than
    // latent on one: without them the configuration still reads as correct
    // right up to an AccessDenied at CreateSession, and there is now no
    // spelling of the variable short of an exact 'pandas' that avoids needing
    // them.
    //
    // WHY ENUMERATED. Before R-02 the shared AgentCore role reached sessions by
    // adding `session/*` to a statement whose action was already `glue:*`, so
    // the session capability arrived with every other Glue action attached.
    // Copying that shape here would have handed a Lambda role glue:* over
    // catalogs, databases, tables, jobs, crawlers and connections to buy six
    // session calls. testCodeRuntimeRole now holds the same enumerated six.
    // LGR-b46a5bb-2's posture (enumerate,
    // so that widening later requires a deliberate policy edit) governs, so
    // this states the six APIs glue_session_runner.py actually calls and no
    // more: create_session (:969), get_session (:1004, the readiness poll),
    // run_statement (:1045), get_statement (:1090, the completion poll),
    // stop_session (:1147) and delete_session (:1160). The runner calls no
    // CancelStatement and no ListSessions, so neither is granted. It does pass
    // `Tags` on CreateSession, and Glue authorizes those as glue:TagResource on
    // the new session, so that is granted too. This comment used to say the
    // opposite; a deployment proved it wrong on 2026-10-08 with "not authorized
    // to perform: glue:TagResource on resource: ...:session/adi-verify-...".
    //
    // WHY HERE AND NOT BESIDE THE ROLE AT :366. The PassRole resource is
    // this.verificationSessionRole.roleArn, and that role is constructed at
    // :821 — after codeTestingRole. Stating these grants at :366 would read the
    // property before it is assigned.
    this.codeTestingRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'glue:CreateSession',
        'glue:TagResource',
        'glue:GetSession',
        'glue:RunStatement',
        'glue:GetStatement',
        'glue:StopSession',
        'glue:DeleteSession',
      ],
      // Session ids are minted per verification run
      // (glue_session_runner.py `_SESSION_ID_PREFIX` + a uuid4 fragment) and so
      // cannot be enumerated at deploy time. Not part of the NAMING FENCE
      // above: interactive sessions carry no project prefix, so there is no
      // snake/kebab pair to preserve. Covered by the existing
      // AwsSolutions-IAM5[Resource::...:session/*] acknowledgment.
      resources: [`arn:aws:glue:${region}:${account}:session/*`],
    }));
    // The condition is not decoration. codeTestingRole can now create a Glue
    // session, so an unconditioned iam:PassRole would let it hand any role it
    // can name to a session it controls — the escalation shape the conditioned
    // grants at :386 and :565-569 exist to avoid. Same shape as those two and as
    // the testCodeRuntimeRole grant above; pinned on all four by
    // test_verification_session_role_iam.py and test/foundation-iam.test.ts, so
    // silently losing the condition fails a test rather than deploying.
    //
    // No broad PassRole wildcard reaches this role (the old unconditioned
    // iam:PassRole on role/AgenticDataIngestion-* is gone, T-03), so this
    // statement is the sole route by which the Lambda can pass the
    // verification role, as testCodeRuntimeRole's is for the container.
    this.codeTestingRole.addToPolicy(new iam.PolicyStatement({
      actions: ['iam:PassRole'],
      resources: [this.verificationSessionRole.roleArn],
      conditions: { StringEquals: { 'iam:PassedToService': 'glue.amazonaws.com' } },
    }));

  }
}
