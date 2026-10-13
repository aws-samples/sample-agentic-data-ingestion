// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';
import { FoundationStack } from './foundation-stack';
import { BEDROCK_MODEL_ID } from './model-config';

/**
 * Ignore mode for every DockerImageAsset in this stack.
 *
 * DockerImageAsset already defaults to IgnoreMode.DOCKER (the
 * `@aws-cdk/aws-ecr-assets:dockerIgnoreSupport` feature flag is on by default
 * in CDK v2), but state it explicitly: the whole scoping strategy here relies
 * on the `.dockerignore` in each build-context directory being interpreted
 * with Docker semantics, and that must not silently change to GLOB if the flag
 * is ever pinned off in cdk.json.
 *
 * Context scoping lives in the `.dockerignore` beside each context rather than
 * in inline `exclude` arrays, because nine of these assets share one context
 * (the project root) and the same file is what `docker build` honours at
 * deploy time. See agentic_data_ingestion/.dockerignore and mcp/.dockerignore.
 */
const DOCKER_ASSET_IGNORE_MODE = cdk.IgnoreMode.DOCKER;

export interface AgentCoreStackProps extends cdk.StackProps {
  environment: string;
  foundation: FoundationStack;
  networkMode?: string;
}

export class AgentCoreStack extends cdk.Stack {
  // Legacy monolithic runtime (kept for backward compatibility)
  public readonly dataProcessingRuntimeId: string;
  public readonly s3TablesRuntimeId: string;
  public readonly dataProcessingInvocationUrl: string;
  public readonly s3TablesInvocationUrl: string;

  // Focused server profile endpoints
  public readonly codegenInvocationUrl: string;
  public readonly infraInvocationUrl: string;
  public readonly analyticsInvocationUrl: string;

  // AgentCore Memory
  public readonly mappingMemoryId: string;

  // Agent Runtime endpoints (Optimization 3)
  public readonly createCodeAgentEndpoint: string;
  public readonly testCodeAgentEndpoint: string;
  public readonly createGluePipelineAgentEndpoint: string;
  public readonly createInfraAgentEndpoint: string;

  // P13 Agentic Capabilities — Agent Runtime endpoints
  public readonly selfHealingAgentEndpoint: string;
  public readonly complexMappingAgentEndpoint: string;
  public readonly dataQualityAgentEndpoint: string;
  public readonly costOptimizationAgentEndpoint: string;
  public readonly nlPipelineBuilderAgentEndpoint: string;

  constructor(scope: Construct, id: string, props: AgentCoreStackProps) {
    super(scope, id, props);

    const { environment, foundation } = props;
    const dpRuntimeName = 'agentic_data_ingestion_aws_dataprocessing_mcp';
    const s3tRuntimeName = 'agentic_data_ingestion_s3tables_mcp';
    const codegenRuntimeName = 'agentic_data_ingestion_mcp_codegen';
    const infraRuntimeName = 'agentic_data_ingestion_mcp_infra';
    const analyticsRuntimeName = 'agentic_data_ingestion_mcp_analytics';
    const networkMode = props.networkMode ?? 'PUBLIC';
    const region = cdk.Stack.of(this).region;
    const account = cdk.Stack.of(this).account;

    // ── OTEL environment variables for AgentCore Observability ──────
    const otelEnv = (serviceName: string) => ({
      AWS_REGION: region,
      AWS_DEFAULT_REGION: region,
      FASTMCP_LOG_LEVEL: 'INFO',
      AGENT_OBSERVABILITY_ENABLED: 'true',
      OTEL_PYTHON_DISTRO: 'aws_distro',
      OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
      OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
      OTEL_RESOURCE_ATTRIBUTES: `service.name=${serviceName}`,
    });

    // MCP server env — write/sensitive access flags (P28 R16.1).
    //
    // Every MCP server reads MCP_ALLOW_WRITE / MCP_ALLOW_SENSITIVE_DATA_ACCESS
    // at startup and defaults both to False; this is the single place where a
    // server's flags are flipped, so the decision stays reviewable in CDK.
    // All five runtimes are pinned to 'false' because no deployed consumer
    // performs AWS mutations through MCP:
    //   - codegen + analytics: consumed by create_glue_pipeline's
    //     worker-recommendation agent for read-only analysis (list jobs,
    //     tables, Athena/EMR describes); Glue job creation happens in Lambda
    //     code via boto3 under the Lambda's own IAM role.
    //   - infra + s3_tables: not wired to any Lambda (create_infrastructure
    //     and create_glue_pipeline provision via boto3 directly).
    //   - data_processing: legacy monolithic server, no consumers.
    // To enable writes for one server, set MCP_ALLOW_WRITE: 'true' on exactly
    // that runtime and record the consumer + rationale here and in the
    // server's README (mcp/<server>/README.md).
    const mcpServerEnv = (serviceName: string) => ({
      ...otelEnv(serviceName),
      MCP_ALLOW_WRITE: 'false',
      MCP_ALLOW_SENSITIVE_DATA_ACCESS: 'false',
    });

    // Agent container env vars — includes table names that Lambda handlers need at import time
    const agentContainerEnv = (serviceName: string) => ({
      ...otelEnv(serviceName),
      AWS_ACCOUNT_ID: account,
      BEDROCK_MODEL_ID,
      GENERATED_CODE_TABLE: 'agentic_data_ingestion_generated_code',
      STATE_TABLE: 'agentic_data_ingestion_state',
      PIPELINES_TABLE: 'agentic_data_ingestion_pipelines',
      MAPPINGS_TABLE: 'agentic_data_ingestion_schema_mappings',
      APPROVALS_TABLE: 'agentic_data_ingestion_approvals',
      TEST_RESULTS_TABLE: 'agentic_data_ingestion_test_results',
      AGENT_TRACES_TABLE: 'agentic_data_ingestion_agent_traces',
      AUDIT_TABLE: 'agentic_data_ingestion_audit_log',
      // P28 R15.2: per-run model-call budget counter — containers vendor
      // shared/model_config.py and enforce the same cross-Lambda budget
      // when their call sites adopt invoke_model. The runtime role already
      // has UpdateItem on agentic_data_ingestion* tables.
      MODEL_CALL_BUDGET_TABLE: 'agentic_data_ingestion_model_call_budget',
      EVENTBRIDGE_ENABLED: 'true',
      MCP_ENABLED: 'false',
      // P42 R2.5: verification executor selection — one of
      // {pandas, glue, shadow}. Resolution lives in the test_code handler.
      //
      // R2.3's fall-through is now 'glue', not 'pandas': unset, empty and
      // unrecognised all select the Glue runner, and only an exact 'pandas' or
      // 'shadow' selects anything else. So this line no longer changes WHICH
      // executor runs — it states the intent explicitly rather than inheriting
      // it, and an unrecognised value here costs a billable Glue session plus a
      // WARNING rather than a quiet downgrade.
      //
      // NOW 'glue': real Spark on a Glue interactive session is the active
      // verification path, and because the code-testing Lambda only
      // dispatches, THIS is the copy that makes that true. A DELIBERATE
      // REVERSAL of the R11.2 hold, not a drift: R11.2 pinned 'pandas' WHILE
      // the concurrent-session ceiling was unmeasured, and it is STILL
      // unmeasured — task 1.3's probe was written and deliberately never run
      // because running it creates billable sessions. An accepted deviation
      // from R11.2, not a satisfaction of it. Hitting the ceiling surfaces as
      // a capacity refusal, which the harness maps to the
      // 'session_capacity_exhausted' non-verification, so a run reports "not
      // verified" rather than failing — an abstention, not an error. Revisit
      // if that abstention rate becomes material, or once 11.1 is measured.
      //
      // STILL SET IN BOTH PLACES, but the reason changed. compute-stack.ts
      // sets USE_AGENTCORE_RUNTIME: 'true' on the code-testing Lambda, so the
      // verification runner executes HERE, in the container — this is the copy
      // the live path actually reads, and its twin on the Lambda covers the
      // inline break-glass path.
      //
      // LGR-87b8538-1 rejected setting only the Lambda because that "produces a
      // flag that changes nothing while every test still passes". With the code
      // fall-through now 'glue', that is no longer true: omitting this copy
      // leaves the container resolving to 'glue' anyway. The pin in
      // test_verification_executor_env.py therefore no longer catches a
      // one-sided set. Both copies and the test are kept regardless — they make
      // the intended executor legible per stack, and a future move back to
      // 'pandas' has to touch both sides deliberately. Do not remove either
      // side, and do not change one without the other. This env applies to all
      // agent runtimes; only test_code reads it.
      VERIFICATION_EXECUTOR: 'glue',
      // P44 R1.7: verification frame-mode selection — one of
      // {synthetic, sample}. Resolution lives in glue_session_runner.py's
      // resolve_frame_source; the test_code handler reads this variable at the
      // point it CONSTRUCTS the runner, not at import, so a warm container
      // reflects a change here on its next run.
      //
      // 'sample' means the Glue verification measures the transform against a
      // bounded 100-row sample of the run's OWN source object instead of 100
      // rows generated from the declared schema. A non-s3:// URI (every JDBC
      // source) or an unrecognised extension falls back to synthetic and records
      // why — that fallback is a fully verified run against a generated frame,
      // NOT a non-verification.
      //
      // THIS IS THE COPY THAT MATTERS, and that asymmetry is worth stating.
      // compute-stack.ts sets USE_AGENTCORE_RUNTIME: 'true' on the code-testing
      // Lambda, so that handler only DISPATCHES and the runner executes HERE, in
      // the container. LGR-87b8538-1 (template / ratified) rejected setting only
      // compute-stack.ts in its own words — it "yields an inert flag on the live
      // path while every test passes" — so the obvious copy is the inert one and
      // this one is the live route. Its twin on the Lambda covers the inline
      // break-glass path. MUST BE SET IN BOTH PLACES: do not remove either side,
      // and do not change one without the other. This env applies to all agent
      // runtimes; only test_code reads it.
      //
      // A near-miss spelling is NOT normalised: 'Sample' or 'sample ' takes
      // resolve_frame_source's unrecognised arm, builds a synthetic frame and
      // logs a WARNING naming the value (LGR-ad78946-1). So a typo here silently
      // reverts the change rather than failing; the allowed pair is exactly
      // {synthetic, sample}.
      //
      // DEPLOY ORDER IS LOAD-BEARING (P44 C11, R7.5). The read this enables
      // needs the VerificationSessionRole S3 grant on the source-files bucket
      // (foundation-stack.ts:877-885), which ships in Foundation. Deploying this
      // stack or Compute BEFORE Foundation means every single-file S3-upload
      // verification selects 'sample', cannot open the object, and abstains —
      // reported as "not verified" naming the source read, so coverage degrades
      // rather than a verdict being corrupted. Every test passes in either
      // order, which is why the ordering is a criterion and not a runbook line.
      VERIFICATION_FRAME_MODE: 'sample',
      // P42 R6.4: the runtime role a Glue interactive session assumes. THIS is
      // the copy the live path reads — the code-testing Lambda dispatches here,
      // so the Glue_Session_Runner resolves the ARN inside this container.
      //
      // Referenced through the Foundation construct, NOT interpolated from
      // account and region like the bucket names below. That asymmetry is
      // deliberate: a bucket name is a naming convention this stack can
      // reproduce, whereas rebuilding a role ARN was considered and rejected —
      // see lambda_function.py:454-473. A reconstruction silently produces a
      // valid-looking ARN for a role that may not exist, so a missing or renamed
      // role surfaces as an opaque AccessDenied at CreateSession instead of a
      // synth-time or deploy-time failure. The per-runtime `RoleArn:
      // foundation.<agent>RuntimeRole.roleArn` entries below are the in-file precedent for crossing Foundation → AgentCore this way.
      //
      // MUST BE SET IN BOTH PLACES, like VERIFICATION_EXECUTOR above. Its twin
      // is on the code-testing Lambda in compute-stack.ts, covering the inline
      // break-glass path. Unset, resolve_effective_executor downgrades 'glue' to
      // 'pandas' and logs at ERROR (lambda_function.py:590-598).
      //
      // Publishing this made 'glue' SELECTABLE; VERIFICATION_EXECUTOR above is
      // what SELECTS it, and as of the R11.2 reversal recorded there it now
      // does. On this side that makes the variable load-bearing on the live
      // path rather than latent: unset, resolve_effective_executor downgrades
      // 'glue' back to 'pandas' and logs at ERROR, silently returning every
      // run to the pandas proxy.
      VERIFICATION_SESSION_ROLE_ARN: foundation.verificationSessionRole.roleArn,
      S3_TABLES_JAR_PATH: `s3://adi-dependencies-${account}-${region}/glue-jars/s3-tables-catalog-for-iceberg-runtime-0.1.5.jar`,
      // The only bucket nl_pipeline_builder's discover_source may read (T-05).
      // Unset, it refuses every read. self_healing_agent reads it too, as its
      // re-trigger fallback, and derived the same name before it was set.
      SOURCE_BUCKET: `adi-source-files-${account}-${region}`,
      PROCESSED_BUCKET: `adi-processed-files-${account}-${region}`,
      TEMPLATES_BUCKET: `adi-templates-${account}-${region}`,
      GENERATED_CODE_BUCKET: `adi-generated-code-${account}-${region}`,
      CHECKPOINT_BUCKET: `adi-checkpoints-${account}-${region}`,
      // Threat model T-03: the boundary create_glue_pipeline must attach to every
      // per-pipeline Glue role; Foundation refuses iam:CreateRole without it. The
      // live path, so this is the copy that matters; its twin is on the
      // create_glue_pipeline Lambda in compute-stack.ts. Referenced through the
      // construct, like VERIFICATION_SESSION_ROLE_ARN above and for its reason.
      GLUE_JOB_ROLE_BOUNDARY_ARN: foundation.glueJobRoleBoundary.managedPolicyArn,
    });

    // ── CloudWatch Transaction Search enablement (one-time) ────────
    // Creates a resource policy allowing X-Ray to write spans to
    // CloudWatch Logs, and sets the trace segment destination to
    // CloudWatchLogs.  Both calls are idempotent.
    const txSearchRole = new iam.Role(this, 'TxSearchSetupRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    txSearchRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:PutResourcePolicy', 'logs:DeleteResourcePolicy'],
      resources: ['*'],
    }));

    // Create the CloudWatch Logs resource policy allowing X-Ray to write spans.
    // This is the essential piece — it enables span ingestion into CloudWatch.
    // The X-Ray trace segment destination (CloudWatchLogs) is NOT set here
    // because the updateTraceSegmentDestination API cascades into
    // application-signals:StartDiscovery and iam:CreateServiceLinkedRole
    // which require broad IAM permissions.  Instead, ADOT exports traces
    // directly via OTLP to CloudWatch, bypassing the X-Ray destination setting.
    // If you want Transaction Search in the CloudWatch console, run once manually:
    //   aws xray update-trace-segment-destination --destination CloudWatchLogs
    new cr.AwsCustomResource(this, 'CWLogsSpansPolicy', {
      onCreate: {
        service: 'CloudWatchLogs',
        action: 'putResourcePolicy',
        parameters: {
          policyName: 'AgentCoreTransactionSearchXRayAccess',
          policyDocument: JSON.stringify({
            Version: '2012-10-17',
            Statement: [{
              Sid: 'TransactionSearchXRayAccess',
              Effect: 'Allow',
              Principal: { Service: 'xray.amazonaws.com' },
              Action: 'logs:PutLogEvents',
              Resource: [
                `arn:aws:logs:${region}:${account}:log-group:aws/spans:*`,
                `arn:aws:logs:${region}:${account}:log-group:/aws/application-signals/data:*`,
              ],
              Condition: {
                ArnLike: { 'aws:SourceArn': `arn:aws:xray:${region}:${account}:*` },
                StringEquals: { 'aws:SourceAccount': account },
              },
            }],
          }),
        },
        physicalResourceId: cr.PhysicalResourceId.of('agentcore-cw-spans-policy'),
      },
      onDelete: {
        service: 'CloudWatchLogs',
        action: 'deleteResourcePolicy',
        parameters: { policyName: 'AgentCoreTransactionSearchXRayAccess' },
      },
      role: txSearchRole,
    });

    // Build Docker images automatically via CDK assets.
    // The four dataprocessing-based servers build with mcp/ as the context
    // (file: <server>/Dockerfile) so their Dockerfiles can COPY the single
    // vendored awslabs fork from mcp/vendor/ (P28 R19.7 — one copy, with
    // PROVENANCE.md). s3_tables has no vendored dependency and keeps its
    // narrow per-server context.
    const mcpRoot = path.join(__dirname, '..', '..', 'mcp');

    const dataProcessingImage = new ecr_assets.DockerImageAsset(this, 'DataProcessingImage', {
      directory: mcpRoot,
      file: 'data_processing/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const s3TablesImage = new ecr_assets.DockerImageAsset(this, 'S3TablesImage', {
      directory: path.join(mcpRoot, 's3_tables'),
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    // Focused server Docker images
    const codegenImage = new ecr_assets.DockerImageAsset(this, 'CodegenImage', {
      directory: mcpRoot,
      file: 'codegen_server/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const infraImage = new ecr_assets.DockerImageAsset(this, 'InfraImage', {
      directory: mcpRoot,
      file: 'infra_server/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const analyticsImage = new ecr_assets.DockerImageAsset(this, 'AnalyticsImage', {
      directory: mcpRoot,
      file: 'analytics_server/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    // Data Processing MCP Runtime (32 tools) — legacy monolithic server
    const dataProcessingRuntime = new cdk.CfnResource(this, 'DataProcessingMCPRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: dpRuntimeName,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: dataProcessingImage.imageUri,
          },
        },
        RoleArn: foundation.mcpServerRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'MCP',
        EnvironmentVariables: mcpServerEnv('agentic-data-ingestion-data-processing-mcp'),
        Description: 'AWS Data Processing MCP Server with 32 tools for AgenticDataIngestion (legacy)',
      },
    });

    // S3 Tables MCP Runtime (17 tools)
    const s3TablesRuntime = new cdk.CfnResource(this, 'S3TablesMCPRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: s3tRuntimeName,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: s3TablesImage.imageUri,
          },
        },
        RoleArn: foundation.mcpServerRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'MCP',
        EnvironmentVariables: mcpServerEnv('agentic-data-ingestion-s3tables-mcp'),
        Description: 'AWS S3 Tables MCP Server for AgenticDataIngestion',
      },
    });

    // ── Focused Server Profile Runtimes ────────────────────────────

    // Codegen MCP Runtime (~10 tools: Glue ETL, data catalog read, interactive sessions)
    const codegenRuntime = new cdk.CfnResource(this, 'CodegenMCPRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: codegenRuntimeName,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: codegenImage.imageUri,
          },
        },
        RoleArn: foundation.mcpServerRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'MCP',
        EnvironmentVariables: mcpServerEnv('agentic-data-ingestion-codegen-mcp'),
        Description: 'AgenticDataIngestion Codegen MCP Server (~10 tools: Glue ETL, catalog, sessions)',
      },
    });

    // Infrastructure MCP Runtime (~12 tools: crawlers, commons, workflows, IAM/S3)
    const infraRuntime = new cdk.CfnResource(this, 'InfraMCPRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: infraRuntimeName,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: infraImage.imageUri,
          },
        },
        RoleArn: foundation.mcpServerRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'MCP',
        EnvironmentVariables: mcpServerEnv('agentic-data-ingestion-infra-mcp'),
        Description: 'AgenticDataIngestion Infrastructure MCP Server (~12 tools: crawlers, workflows, IAM)',
      },
    });

    // Analytics MCP Runtime (~8 tools: Athena, EMR)
    const analyticsRuntime = new cdk.CfnResource(this, 'AnalyticsMCPRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: analyticsRuntimeName,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: analyticsImage.imageUri,
          },
        },
        RoleArn: foundation.mcpServerRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'MCP',
        EnvironmentVariables: mcpServerEnv('agentic-data-ingestion-analytics-mcp'),
        Description: 'AgenticDataIngestion Analytics MCP Server (~8 tools: Athena, EMR)',
      },
    });

    // ── AgentCore Memory for Schema Mapping ───────────────────────
    const mappingMemory = new cdk.CfnResource(this, 'MappingMemory', {
      type: 'AWS::BedrockAgentCore::Memory',
      properties: {
        Name: `agentic_data_ingestion_mapping_memory_${environment}`,
        Description: 'AgentCore Memory for schema mapping historical patterns and approval feedback',
        EventExpiryDuration: 90,
        MemoryStrategies: [
          {
            SemanticMemoryStrategy: {
              Name: `mapping_semantic_${environment}`,
              Description: 'Semantic search over historical field mapping decisions',
            },
          },
        ],
      },
    });

    // Memory ID — assigned immediately after the resource is defined so it is
    // available to downstream agent-runtime container definitions (e.g. the
    // Complex Mapping Agent's MAPPING_MEMORY_ID env var) that reference it
    // before the legacy-outputs section below. Previously this was only set
    // near the end of the constructor, so consumers earlier in the file got
    // ``undefined`` at synth (TS2565).
    this.mappingMemoryId = mappingMemory.getAtt('MemoryId').toString();

    // ── Agent Runtime Containers (Optimization 3) ──────────────────
    // These are agent containers (ProtocolConfiguration: HTTP), not MCP servers.
    // They wrap the existing Lambda handler logic in long-running containers
    // with persistent MCP connections and no cold start.

    // Build context for all nine agent images. Scoped by
    // agentic_data_ingestion/.dockerignore, which is derived from the COPY
    // lines in agentcore/*/Dockerfile — read that file before adding a COPY.
    const projectRoot = path.join(__dirname, '..', '..');

    // NOTE: these nine assets deliberately carry NO buildArgs.
    //
    // They used to pass `BUILD_TS: new Date().toISOString()` with the comment
    // "forces CDK to rebuild images when code changes". It did not do that —
    // content hashing already does — and no agent Dockerfile ever declared
    // `ARG BUILD_TS`, so Docker discarded it. What it did do is land in
    // DockerImageAsset's extraHash (aws-cdk-lib puts buildArgs there unless
    // `invalidation.buildArgs` is false), which gave all nine images a brand
    // new asset hash on EVERY synth. Combined with the unscoped context that
    // is what made the asset cache unable to ever hit: each synth restaged
    // nine multi-GiB contexts and orphaned the previous nine.
    //
    // If a genuine cache-bust is ever needed, bump the `# Cache bust:` comment
    // in the Dockerfile (as agentcore/create_code/Dockerfile:2 already does) —
    // that changes the context content, so the hash moves once, deliberately.

    const createCodeAgentImage = new ecr_assets.DockerImageAsset(this, 'CreateCodeAgentImage', {
      directory: projectRoot,
      file: 'agentcore/create_code/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const testCodeAgentImage = new ecr_assets.DockerImageAsset(this, 'TestCodeAgentImage', {
      directory: projectRoot,
      file: 'agentcore/test_code/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const createGluePipelineAgentImage = new ecr_assets.DockerImageAsset(this, 'CreateGluePipelineAgentImage', {
      directory: projectRoot,
      file: 'agentcore/create_glue_pipeline/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const createInfraAgentImage = new ecr_assets.DockerImageAsset(this, 'CreateInfraAgentImage', {
      directory: projectRoot,
      file: 'agentcore/create_infrastructure/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    // Code Generation Agent Runtime
    const createCodeAgentRuntime = new cdk.CfnResource(this, 'CreateCodeAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_create_code`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: createCodeAgentImage.imageUri,
          },
        },
        RoleArn: foundation.createCodeRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-create-code'),
        Description: 'AgenticDataIngestion Code Generation Agent Container',
      },
    });

    // Code Testing Agent Runtime
    const testCodeAgentRuntime = new cdk.CfnResource(this, 'TestCodeAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_test_code`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: testCodeAgentImage.imageUri,
          },
        },
        RoleArn: foundation.testCodeRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-test-code'),
        Description: 'AgenticDataIngestion Code Testing Agent Container',
      },
    });

    // Glue Pipeline Creation Agent Runtime
    const createGluePipelineAgentRuntime = new cdk.CfnResource(this, 'CreateGluePipelineAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_create_pipeline`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: createGluePipelineAgentImage.imageUri,
          },
        },
        RoleArn: foundation.createGluePipelineRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-create-pipeline'),
        Description: 'AgenticDataIngestion Glue Pipeline Creation Agent Container',
      },
    });

    // Infrastructure Creation Agent Runtime
    const createInfraAgentRuntime = new cdk.CfnResource(this, 'CreateInfraAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_create_infra`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: createInfraAgentImage.imageUri,
          },
        },
        RoleArn: foundation.createInfrastructureRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-create-infra'),
        Description: 'AgenticDataIngestion Infrastructure Creation Agent Container',
      },
    });

    // ── P13 Agentic Capabilities — Agent Runtime Containers ───────

    // Self-Healing Agent
    const selfHealingAgentImage = new ecr_assets.DockerImageAsset(this, 'SelfHealingAgentImage', {
      directory: projectRoot,
      file: 'agentcore/self_healing_agent/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const selfHealingAgentRuntime = new cdk.CfnResource(this, 'SelfHealingAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_self_healing`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: selfHealingAgentImage.imageUri,
          },
        },
        RoleArn: foundation.selfHealingRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-self-healing'),
        Description: 'AgenticDataIngestion Self-Healing Agent Container',
      },
    });

    // Complex Mapping Agent
    const complexMappingAgentImage = new ecr_assets.DockerImageAsset(this, 'ComplexMappingAgentImage', {
      directory: projectRoot,
      file: 'agentcore/complex_mapping_agent/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const complexMappingAgentRuntime = new cdk.CfnResource(this, 'ComplexMappingAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_complex_mapping`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: complexMappingAgentImage.imageUri,
          },
        },
        RoleArn: foundation.complexMappingRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: {
          ...agentContainerEnv('agentic-data-ingestion-agent-complex-mapping'),
          MAPPING_MEMORY_ID: this.mappingMemoryId,
        },
        Description: 'AgenticDataIngestion Complex Mapping Agent Container',
      },
    });

    // Data Quality Agent
    const dataQualityAgentImage = new ecr_assets.DockerImageAsset(this, 'DataQualityAgentImage', {
      directory: projectRoot,
      file: 'agentcore/data_quality_agent/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const dataQualityAgentRuntime = new cdk.CfnResource(this, 'DataQualityAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_data_quality`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: dataQualityAgentImage.imageUri,
          },
        },
        RoleArn: foundation.dataQualityRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-data-quality'),
        Description: 'AgenticDataIngestion Data Quality Agent Container',
      },
    });

    // Cost Optimization Agent
    const costOptimizationAgentImage = new ecr_assets.DockerImageAsset(this, 'CostOptimizationAgentImage', {
      directory: projectRoot,
      file: 'agentcore/cost_optimization_agent/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const costOptimizationAgentRuntime = new cdk.CfnResource(this, 'CostOptimizationAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_cost_optimization`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: costOptimizationAgentImage.imageUri,
          },
        },
        RoleArn: foundation.costOptimizationRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-cost-optimization'),
        Description: 'AgenticDataIngestion Cost Optimization Agent Container',
      },
    });

    // Natural Language Pipeline Builder Agent
    const nlPipelineBuilderAgentImage = new ecr_assets.DockerImageAsset(this, 'NLPipelineBuilderAgentImage', {
      directory: projectRoot,
      file: 'agentcore/nl_pipeline_builder/Dockerfile',
      platform: ecr_assets.Platform.LINUX_ARM64,
      ignoreMode: DOCKER_ASSET_IGNORE_MODE,
    });

    const nlPipelineBuilderAgentRuntime = new cdk.CfnResource(this, 'NLPipelineBuilderAgentRuntime', {
      type: 'AWS::BedrockAgentCore::Runtime',
      properties: {
        AgentRuntimeName: `agentic_data_ingestion_agent_nl_pipeline_builder`,
        AgentRuntimeArtifact: {
          ContainerConfiguration: {
            ContainerUri: nlPipelineBuilderAgentImage.imageUri,
          },
        },
        RoleArn: foundation.nlPipelineBuilderRuntimeRole.roleArn,
        NetworkConfiguration: { NetworkMode: networkMode },
        ProtocolConfiguration: 'HTTP',
        EnvironmentVariables: agentContainerEnv('agentic-data-ingestion-agent-nl-pipeline-builder'),
        Description: 'AgenticDataIngestion Natural Language Pipeline Builder Agent Container',
      },
    });

    // ── Legacy Outputs ─────────────────────────────────────────────
    this.dataProcessingRuntimeId = dataProcessingRuntime.getAtt('AgentRuntimeId').toString();
    this.s3TablesRuntimeId = s3TablesRuntime.getAtt('AgentRuntimeId').toString();

    this.dataProcessingInvocationUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${this.dataProcessingRuntimeId}/invocations?qualifier=DEFAULT`;
    this.s3TablesInvocationUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${this.s3TablesRuntimeId}/invocations?qualifier=DEFAULT`;

    // ── Focused Server Endpoints ───────────────────────────────────
    const codegenRuntimeId = codegenRuntime.getAtt('AgentRuntimeId').toString();
    const infraRuntimeId = infraRuntime.getAtt('AgentRuntimeId').toString();
    const analyticsRuntimeId = analyticsRuntime.getAtt('AgentRuntimeId').toString();

    this.codegenInvocationUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${codegenRuntimeId}/invocations?qualifier=DEFAULT`;
    this.infraInvocationUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${infraRuntimeId}/invocations?qualifier=DEFAULT`;
    this.analyticsInvocationUrl = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${analyticsRuntimeId}/invocations?qualifier=DEFAULT`;

    // Agent Runtime endpoints
    const createCodeAgentRuntimeId = createCodeAgentRuntime.getAtt('AgentRuntimeId').toString();
    const testCodeAgentRuntimeId = testCodeAgentRuntime.getAtt('AgentRuntimeId').toString();
    const createGluePipelineAgentRuntimeId = createGluePipelineAgentRuntime.getAtt('AgentRuntimeId').toString();
    const createInfraAgentRuntimeId = createInfraAgentRuntime.getAtt('AgentRuntimeId').toString();

    this.createCodeAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${createCodeAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.testCodeAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${testCodeAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.createGluePipelineAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${createGluePipelineAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.createInfraAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${createInfraAgentRuntimeId}/invocations?qualifier=DEFAULT`;

    // P13 Agent Runtime endpoints
    const selfHealingAgentRuntimeId = selfHealingAgentRuntime.getAtt('AgentRuntimeId').toString();
    const complexMappingAgentRuntimeId = complexMappingAgentRuntime.getAtt('AgentRuntimeId').toString();
    const dataQualityAgentRuntimeId = dataQualityAgentRuntime.getAtt('AgentRuntimeId').toString();
    const costOptimizationAgentRuntimeId = costOptimizationAgentRuntime.getAtt('AgentRuntimeId').toString();
    const nlPipelineBuilderAgentRuntimeId = nlPipelineBuilderAgentRuntime.getAtt('AgentRuntimeId').toString();

    this.selfHealingAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${selfHealingAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.complexMappingAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${complexMappingAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.dataQualityAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${dataQualityAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.costOptimizationAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${costOptimizationAgentRuntimeId}/invocations?qualifier=DEFAULT`;
    this.nlPipelineBuilderAgentEndpoint = `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/${nlPipelineBuilderAgentRuntimeId}/invocations?qualifier=DEFAULT`;

    // ── CfnOutputs ─────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'DataProcessingRuntimeId', {
      value: this.dataProcessingRuntimeId,
      exportName: `${this.stackName}-RuntimeId`,
    });
    new cdk.CfnOutput(this, 'DataProcessingRuntimeArn', {
      value: dataProcessingRuntime.getAtt('AgentRuntimeArn').toString(),
      exportName: `${this.stackName}-RuntimeArn`,
    });
    new cdk.CfnOutput(this, 'DataProcessingInvocationURL', {
      value: this.dataProcessingInvocationUrl,
      exportName: `${this.stackName}-InvocationURL`,
    });
    new cdk.CfnOutput(this, 'S3TablesRuntimeIdOutput', {
      value: this.s3TablesRuntimeId,
      exportName: `${this.stackName}-S3TablesRuntimeId`,
    });
    new cdk.CfnOutput(this, 'S3TablesRuntimeArn', {
      value: s3TablesRuntime.getAtt('AgentRuntimeArn').toString(),
      exportName: `${this.stackName}-S3TablesRuntimeArn`,
    });
    new cdk.CfnOutput(this, 'S3TablesInvocationURL', {
      value: this.s3TablesInvocationUrl,
      exportName: `${this.stackName}-S3TablesInvocationURL`,
    });

    // Focused server outputs
    new cdk.CfnOutput(this, 'CodegenRuntimeId', {
      value: codegenRuntimeId,
      exportName: `${this.stackName}-CodegenRuntimeId`,
    });
    new cdk.CfnOutput(this, 'CodegenInvocationURL', {
      value: this.codegenInvocationUrl,
      exportName: `${this.stackName}-CodegenInvocationURL`,
    });
    new cdk.CfnOutput(this, 'InfraRuntimeId', {
      value: infraRuntimeId,
      exportName: `${this.stackName}-InfraRuntimeId`,
    });
    new cdk.CfnOutput(this, 'InfraInvocationURL', {
      value: this.infraInvocationUrl,
      exportName: `${this.stackName}-InfraInvocationURL`,
    });
    new cdk.CfnOutput(this, 'AnalyticsRuntimeId', {
      value: analyticsRuntimeId,
      exportName: `${this.stackName}-AnalyticsRuntimeId`,
    });
    new cdk.CfnOutput(this, 'AnalyticsInvocationURL', {
      value: this.analyticsInvocationUrl,
      exportName: `${this.stackName}-AnalyticsInvocationURL`,
    });

    // Memory output
    new cdk.CfnOutput(this, 'MappingMemoryId', {
      value: this.mappingMemoryId,
      exportName: `${this.stackName}-MappingMemoryId`,
    });

    // Agent Runtime outputs
    new cdk.CfnOutput(this, 'CreateCodeAgentRuntimeId', {
      value: createCodeAgentRuntimeId,
      exportName: `${this.stackName}-CreateCodeAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'CreateCodeAgentEndpoint', {
      value: this.createCodeAgentEndpoint,
      exportName: `${this.stackName}-CreateCodeAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'TestCodeAgentRuntimeId', {
      value: testCodeAgentRuntimeId,
      exportName: `${this.stackName}-TestCodeAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'TestCodeAgentEndpoint', {
      value: this.testCodeAgentEndpoint,
      exportName: `${this.stackName}-TestCodeAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'CreateGluePipelineAgentRuntimeId', {
      value: createGluePipelineAgentRuntimeId,
      exportName: `${this.stackName}-CreateGluePipelineAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'CreateGluePipelineAgentEndpoint', {
      value: this.createGluePipelineAgentEndpoint,
      exportName: `${this.stackName}-CreateGluePipelineAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'CreateInfraAgentRuntimeId', {
      value: createInfraAgentRuntimeId,
      exportName: `${this.stackName}-CreateInfraAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'CreateInfraAgentEndpoint', {
      value: this.createInfraAgentEndpoint,
      exportName: `${this.stackName}-CreateInfraAgentEndpoint`,
    });

    // P13 Agent Runtime outputs
    new cdk.CfnOutput(this, 'SelfHealingAgentRuntimeId', {
      value: selfHealingAgentRuntimeId,
      exportName: `${this.stackName}-SelfHealingAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'SelfHealingAgentEndpoint', {
      value: this.selfHealingAgentEndpoint,
      exportName: `${this.stackName}-SelfHealingAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'ComplexMappingAgentRuntimeId', {
      value: complexMappingAgentRuntimeId,
      exportName: `${this.stackName}-ComplexMappingAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'ComplexMappingAgentEndpoint', {
      value: this.complexMappingAgentEndpoint,
      exportName: `${this.stackName}-ComplexMappingAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'DataQualityAgentRuntimeId', {
      value: dataQualityAgentRuntimeId,
      exportName: `${this.stackName}-DataQualityAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'DataQualityAgentEndpoint', {
      value: this.dataQualityAgentEndpoint,
      exportName: `${this.stackName}-DataQualityAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'CostOptimizationAgentRuntimeId', {
      value: costOptimizationAgentRuntimeId,
      exportName: `${this.stackName}-CostOptimizationAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'CostOptimizationAgentEndpoint', {
      value: this.costOptimizationAgentEndpoint,
      exportName: `${this.stackName}-CostOptimizationAgentEndpoint`,
    });
    new cdk.CfnOutput(this, 'NLPipelineBuilderAgentRuntimeId', {
      value: nlPipelineBuilderAgentRuntimeId,
      exportName: `${this.stackName}-NLPipelineBuilderAgentRuntimeId`,
    });
    new cdk.CfnOutput(this, 'NLPipelineBuilderAgentEndpoint', {
      value: this.nlPipelineBuilderAgentEndpoint,
      exportName: `${this.stackName}-NLPipelineBuilderAgentEndpoint`,
    });
  }
}
