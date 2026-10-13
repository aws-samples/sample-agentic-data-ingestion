// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as apigatewayv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { WebSocketLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { WebSocketLambdaAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';
import { FoundationStack } from './foundation-stack';
import { DataStack } from './data-stack';
import { BEDROCK_MODEL_ID } from './model-config';
import { pinnedBuildImage, SAM_PYTHON312_ARM64_BUILD_IMAGE } from './build-images';
import { LOG_RETENTION, retainLambdaLogs, retainLogGroup } from './log-retention';

/**
 * Locally generated artefacts that must never enter a Lambda code asset.
 *
 * Interpreted with IgnoreMode.GIT, so these match at any depth. Every entry is
 * something a tool writes into the source tree; none of it is imported at
 * runtime, so excluding it cannot break a handler. Exported because
 * orchestration-stack.ts defines two Lambdas from the same lambda/ tree and
 * must scope them the same way.
 */
export const LAMBDA_CODE_ASSET_EXCLUDE = [
  '__pycache__',
  '*.pyc',
  '*.pyo',
  '.pytest_cache',
  '.mypy_cache',
  '.ruff_cache',
  '.hypothesis',
  '.coverage',
  '.coverage.*',
  '.DS_Store',
  '*.swp',
];

/**
 * Source-tree entries excluded from the FINGERPRINT of a layer asset rooted at
 * `agentic_data_ingestion/`.
 *
 * Extracted from `StrandsAgentsLayer` so `SharedBaseLayer` cannot drift from it.
 * The two layers read the same two directories out of the same asset root, so
 * scoping them differently could only ever be a mistake. Values are unchanged
 * from the inline list this replaces, which matters: the fingerprint walk is the
 * whole synth cost of these assets, and a changed exclude list would restage
 * both layers.
 *
 * Bundling bind-mounts the UNFILTERED source dir at /asset-input, so this list
 * never affects what the pip/cp commands can see — it only decides what CDK
 * fingerprints. That makes it both safe (it cannot break a bundle) and
 * load-bearing (a fingerprint that moves every synth means a rebuild every
 * synth).
 *
 * IgnoreMode.GIT, not the GLOB default, for two reasons:
 *   1. `cdk.out*` then matches the parked `deploy-cdk/cdk.out.stale-*`
 *      directories. Under GLOB, bare `cdk.out` matched `cdk.out` but NOT
 *      `cdk.out.stale-1785775343`, so tens of GiB of previous synth output were
 *      still being walked and hashed.
 *   2. A leading `/` anchors to the context root, so `/lib` cannot accidentally
 *      swallow a nested `shared/lib`. GLOB matches on basename, which makes every
 *      bare entry recursive whether or not that was intended.
 */
export const SHARED_LAYER_SOURCE_ASSET_EXCLUDE = [
  // Sibling trees. Bundling only reads shared/ and pyspark_templates/.
  '/deploy-cdk', '/dashboard-react', '/agentcore', '/lambda', '/mcp',
  '/tests', '/docs', '/diagram', '/scripts', '/lib', '/.kiro',
  // Generated / vendored, at any depth.
  'cdk.out', 'cdk.out.*', 'node_modules', '.venv', 'venv', 'dist',
  '*.tsbuildinfo', '*.egg-info',
  // Caches, at any depth.
  '__pycache__', '*.pyc', '*.pyo', '.mypy_cache', '.ruff_cache',
  '.pytest_cache', '.hypothesis', '.coverage', '.coverage.*', 'htmlcov',
  // VCS / editor / local env.
  '.git', '.DS_Store', '*.swp', '.env', '.env.*',
];

/**
 * The packages in `shared/runtime_requirements.txt` that `SharedBaseLayer` does
 * NOT install — the agent stack, and the whole reason the split is worth making.
 *
 * These four plus their transitive closure are **124,583,653 bytes / 118.81
 * MiB** of the strands layer (measured — see the header of
 * `deploy-cdk/test/p55-schema-extract-layer-budget.test.ts` for the arithmetic
 * and the method). Nothing else in the manifest is anywhere near that scale.
 *
 * This list is the only home for the exclusion ON THE BUILD PATH:
 * `SharedBaseLayer`'s bundling command below builds its `grep -vE` pattern from
 * it, so nothing else decides what the base layer omits — `LGR-87b8538-1`'s rule
 * that "a literal in two files is how the next move manufactures a misleading
 * failure".
 *
 * `tests/cdk/stacks/test_shared_base_layer_manifest.py` **parses this
 * declaration** (a Python suite cannot import a TypeScript const), and three of
 * its four exclusion assertions run off the parsed list. The fourth,
 * `test_declares_the_four_agent_packages`, re-spells all four names as an exact
 * set ON PURPOSE — that is a tripwire, not drift, and its own docstring gives
 * the reason: adding a name here silently removes a package from every function
 * on the base layer, which is an `ImportError` at cold start that no test below
 * CDK can observe. So editing this list fails that test by design, and the
 * failure is the review prompt. Do not "fix" it by making the test read the
 * parsed list.
 *
 * ADDING A NAME HERE IS A DECISION, not a size optimisation. Every function that
 * carries only the base layer loses that package at cold start, and the failure
 * is an `ImportError` in a deployed Lambda that no test below CDK can observe
 * (`LGR-bb8073d-1`: unit tests import `shared/` from the repo, where it is a real
 * sibling directory). Enumerate the importers transitively first.
 *
 * The anchoring in the generated pattern is load-bearing: `mcp` must not exclude
 * a future `mcp-something`, and `strands-agents` must not swallow
 * `strands-agents-tools` (it does not — the alternation requires a version
 * operator or end-of-line immediately after the name, and `-tools` is neither,
 * which is why both spellings are listed).
 */
export const AGENT_ONLY_LAYER_PACKAGES = [
  'strands-agents',
  'strands-agents-tools',
  'bedrock-agentcore',
  'mcp',
];

export interface ComputeStackProps extends cdk.StackProps {
  environment: string;
  projectName: string;
  foundation: FoundationStack;
  data: DataStack;
  agentCoreEndpoint?: string;
  agentCoreS3TablesEndpoint?: string;
  // Focused server profile endpoints (Phase 3 #16)
  agentCoreCodegenEndpoint?: string;
  agentCoreInfraEndpoint?: string;
  agentCoreAnalyticsEndpoint?: string;
  // AgentCore Memory
  agentCoreMappingMemoryId?: string;
  // Agent Runtime endpoints (Optimization 3)
  agentCoreCreateCodeEndpoint?: string;
  agentCoreTestCodeEndpoint?: string;
  agentCoreCreatePipelineEndpoint?: string;
  agentCoreCreateInfraEndpoint?: string;
  // P13 Agent Runtime endpoints
  agentCoreSelfHealingEndpoint?: string;
  agentCoreComplexMappingEndpoint?: string;
  agentCoreDataQualityEndpoint?: string;
  agentCoreCostOptimizationEndpoint?: string;
  agentCoreNlPipelineBuilderEndpoint?: string;
  // Cognito auth (Dashboard Hosting + Cognito spec)
  userPoolArn?: string;
  /**
   * SSM parameters holding the app client IDs the WebSocket authorizer accepts
   * (R-05): the dashboard client's, written by Hosting, and the e2e client's,
   * written by Auth when that client is deployed. Read at run time, because
   * Hosting deploys after this stack.
   */
  wsClientIdParameterNames?: string[];
}

export class ComputeStack extends cdk.Stack {
  // Lambda Functions
  public readonly fileValidationLambda: lambda.Function;
  public readonly schemaExtractionLambda: lambda.Function;
  public readonly schemaComparisonLambda: lambda.Function;
  public readonly schemaMappingLambda: lambda.Function;
  public readonly codeGenerationLambda: lambda.Function;
  public readonly codeTestingLambda: lambda.Function;
  public readonly createGluePipelineLambda: lambda.Function;
  public readonly createInfrastructureLambda: lambda.Function;
  public readonly createQualityChecksLambda: lambda.Function;
  public readonly runPipelineLambda: lambda.Function;
  public readonly selfHealingDispatchLambda: lambda.Function;
  public readonly agentDispatchLambda: lambda.Function;
  public readonly jobCompletionLambda: lambda.Function;
  public readonly approvalConfigLambda: lambda.Function;
  public readonly dashboardApiLambda: lambda.Function;
  public readonly approvalHandlerLambda: lambda.Function;
  public readonly approvalTimeoutHandlerLambda: lambda.Function;
  public readonly executionUpdaterLambda: lambda.Function;
  public readonly wsConnectLambda: lambda.Function;
  public readonly wsDisconnectLambda: lambda.Function;
  public readonly wsBroadcastLambda: lambda.Function;
  public readonly wsAuthorizerLambda: lambda.Function;

  // Layers (exposed for orchestration-stack to use when creating the trigger Lambda)
  public readonly strandsLayer: lambda.LayerVersion;
  public readonly adotLayer: lambda.ILayerVersion;

  // API Gateway
  public readonly api: apigateway.RestApi;
  public readonly apiEndpoint: string;
  public readonly wsApiEndpoint: string;

  // Origin-verify secret (for CloudFront-only enforcement)
  public readonly originVerifySecret?: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const { environment, projectName, foundation, data } = props;
    const account = cdk.Stack.of(this).account;
    const region = cdk.Stack.of(this).region;

    const lambdaCodePath = (fnName: string) =>
      path.join(__dirname, '..', '..', 'lambda', fnName);

    /**
     * Code asset for a Lambda in lambda/<fnName>/, with locally generated files
     * excluded.
     *
     * These contexts are narrow already, but they are NOT filtered by default,
     * so a local `pytest` run leaves __pycache__/*.pyc inside them — 24
     * __pycache__ directories and 157 .pyc files across lambda/ at the time
     * this was written. Those bytes ship to Lambda, and worse, they change the
     * asset hash whenever the interpreter rewrites them, which restages the
     * function code for a source change that never happened.
     *
     * IgnoreMode.GIT so the entries match at any depth (handlers/__pycache__ as
     * well as ./__pycache__) with gitignore semantics rather than minimatch's
     * basename matching. Every entry here is a generated artefact — nothing a
     * handler imports at runtime.
     */
    const lambdaCode = (fnName: string) =>
      lambda.Code.fromAsset(lambdaCodePath(fnName), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: LAMBDA_CODE_ASSET_EXCLUDE,
      });

    // Build Strands Lambda Layer automatically via CDK bundling
    this.strandsLayer = new lambda.LayerVersion(this, 'StrandsAgentsLayer', {
      layerVersionName: `strands-agents-layer-${environment}`,
      description: `Strands Agents with MCP support for ${environment}`,
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..'), {
        // See SHARED_LAYER_SOURCE_ASSET_EXCLUDE for why these entries and why
        // IgnoreMode.GIT. The list moved to module scope so SharedBaseLayer,
        // which reads the same two directories out of the same asset root,
        // cannot be scoped differently. Values are byte-identical to the inline
        // list this replaces, so neither fingerprint moves.
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: SHARED_LAYER_SOURCE_ASSET_EXCLUDE,
        bundling: {
          image: pinnedBuildImage(SAM_PYTHON312_ARM64_BUILD_IMAGE),
          command: ['bash', '-c', [
            // Dependency versions come from the single pinned manifest shared
            // with the AgentCore containers (P28 Req 19.6) — see
            // shared/runtime_requirements.txt and
            // tests/unit/agentcore/test_runtime_requirements_pins.py.
            'pip install -r /asset-input/shared/runtime_requirements.txt',
            '--python-version 3.12',
            '--platform manylinux2014_aarch64',
            '--target /asset-output/python',
            '--only-binary=:all:',
            '--no-cache-dir --upgrade',
            '&& cp -f /asset-input/shared/*.py /asset-output/python/ 2>/dev/null || true',
            // P28 R19.8 contract: prompt templates ship inside the layer at
            // /opt/python/prompt_templates (prompt_template_loader's first
            // search path). The `shared/*.py` copy above only takes modules,
            // so the template directory must be copied explicitly — without
            // it any Lambda-side load_prompt_template call dies with
            // TemplateNotFound (caught live in task 17.2 verification).
            '&& cp -rf /asset-input/shared/prompt_templates /asset-output/python/prompt_templates 2>/dev/null || true',
            '&& cp -rf /asset-input/pyspark_templates /asset-output/python/pyspark_templates 2>/dev/null || true',
            '&& find /asset-output/python -type d -name tests -exec rm -rf {} + 2>/dev/null || true',
            '&& find /asset-output/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true',
            '&& find /asset-output/python -name "*.pyc" -delete 2>/dev/null || true',
          ].join(' ')],
        },
      }),
    });

    /**
     * `shared-base-layer` — everything `StrandsAgentsLayer` delivers EXCEPT the
     * agent stack. Same manifest, same `shared/*.py` copy, same templates, minus
     * `AGENT_ONLY_LAYER_PACKAGES` and their transitive closure.
     *
     * WHY THIS EXISTS
     * ---------------
     * `StrandsAgentsLayer` is misnamed: it is the only runtime delivery
     * mechanism for all 35 `shared/*.py` modules, `shared/prompt_templates/`,
     * `pyspark_templates/` and pinned `aws-lambda-powertools`
     * (`LGR-bb8073d-1`). That is why p47 R10 could not detach it from the 18
     * functions that never import `strands`: detaching is an `ImportError` at
     * cold start, because the `<fn>/../shared` entry each handler appends to
     * `sys.path` does not exist in a deployed Lambda.
     *
     * `LGR-bb8073d-1` names the split as "the only path that reaches R10's
     * actual goal", declines it as out of p47's scope, and states its
     * `revisit_when` as **"the layer split is taken up"** — with the shape it
     * must take: the base layer on all 22, the strands layer on the four that
     * genuinely need it. This is the first step of that, taken for one function
     * because that function had run out of room.
     *
     * `schema_extract` carries BOTH `StrandsAgentsLayer` and `PyArrowLayer` and
     * sat at 260,821,097 of the 262,144,000-byte unzipped ceiling — 1,322,903
     * bytes, 0.5%, of headroom, after p55's `openpyxl` took 41% of what p46 had
     * left free. Any version bump to strands, pyarrow, mcp, jinja2 or boto3
     * failed the deploy with `Unzipped size must be smaller than 262144000
     * bytes`. Swapping this layer in for the strands layer on that one function
     * returns ~118.81 MiB. Arithmetic and method:
     * `deploy-cdk/test/p55-schema-extract-layer-budget.test.ts`.
     *
     * THE DEPENDENCY SET WAS ENUMERATED, NOT SAMPLED
     * ----------------------------------------------
     * A full AST closure over `lambda/agentic_data_ingestion_schema_extract/`
     * (one file, `lambda_function.py`) following every `shared/*.py` module it
     * reaches — 12 files in total — yields 11 shared modules and exactly six
     * third-party names: `aws_lambda_powertools`, `boto3`, `botocore`,
     * `openpyxl`, `pyarrow`, `s3fs`. `boto3`/`botocore` come from the Lambda
     * runtime, `pyarrow` from `PyArrowLayer`, `s3fs` is a guarded optional
     * import deliberately absent (`:218`). No `strands`, no `bedrock_agentcore`,
     * no `mcp`, at any depth. So nothing this function imports is in the set
     * being dropped.
     *
     * WHAT IS DELIBERATELY *KEPT*
     * ---------------------------
     * `jinja2` and `typing-extensions` stay, even though `schema_extract`
     * imports neither. This layer copies `shared/prompt_templates/` like its
     * sibling does, and `shared/prompt_template_loader.py` needs `jinja2` to
     * read them — so a layer shipping the templates without the engine would be
     * a trap for the second function to adopt it. Keeping them costs 0.51 MiB
     * against 118.81 MiB returned, and it is what makes this a genuine drop-in
     * rather than a `schema_extract`-shaped special case.
     *
     * ONE MANIFEST, FILTERED — NOT A SECOND MANIFEST
     * ----------------------------------------------
     * `LGR-bb8073d-1` requires that a split "MUST install from
     * `shared/runtime_requirements.txt` rather than repeat
     * `PowertoolsTriggerLayer`'s unpinned `aws-lambda-powertools`". So the
     * bundling filters that file at build time instead of introducing a second
     * pinned list. *Rejected: a new `shared/base_requirements.txt`.* It puts
     * every shared version in two files that must agree, which is the drift
     * `LGR-87b8538-1` refuses ("a literal in two files is how the next move
     * manufactures a misleading failure") and would need its own consistency
     * test to be safe. Filtering has one authority for every version and one
     * authority for the exclusion (`AGENT_ONLY_LAYER_PACKAGES`).
     *
     * THE COSTS, STATED
     * -----------------
     * A fourth Docker-bundled layer is a fourth `pip install` in a SAM build
     * container per cold synth (`LGR-d5bcee9-1`) — small here, since the four
     * remaining wheels are ~5 MB against strands' ~124 MB, but it is not free.
     * And the fleet is now heterogeneous: 21 functions on the strands layer, 1
     * on this one. That is pinned by
     * `deploy-cdk/test/p47-strands-layer-attachment.test.ts`, whose universal
     * property was split rather than weakened, so the migration state is
     * asserted rather than implicit.
     */
    const sharedBaseLayer = new lambda.LayerVersion(this, 'SharedBaseLayer', {
      layerVersionName: `shared-base-layer-${environment}`,
      description: `shared/*.py, templates and Powertools without the agent stack (${environment})`,
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      code: lambda.Code.fromAsset(path.join(__dirname, '..', '..'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: SHARED_LAYER_SOURCE_ASSET_EXCLUDE,
        bundling: {
          image: pinnedBuildImage(SAM_PYTHON312_ARM64_BUILD_IMAGE),
          command: ['bash', '-c', [
            // Filter the ONE pinned manifest rather than maintain a second one.
            // The pattern is generated from AGENT_ONLY_LAYER_PACKAGES so the
            // exclusion has a single home; the `([=<>!~ ]|$)` tail anchors each
            // name so `mcp` cannot match a future `mcp-something` and
            // `strands-agents` cannot swallow `strands-agents-tools`.
            //
            // Comment lines in the manifest survive the filter and pip ignores
            // them, which is deliberate: the Layer_Budget notes written beside
            // each pin stay visible in the build log.
            `grep -vE '^(${AGENT_ONLY_LAYER_PACKAGES.join('|')})([=<>!~ ]|$)'`,
            '/asset-input/shared/runtime_requirements.txt',
            // /tmp, not /asset-output: a stray requirements file inside the
            // layer would ship to /opt/python and change the layer's bytes.
            '> /tmp/base_requirements.txt',
            // Fail the bundle if the filter removed nothing. Without this a
            // rename in the manifest silently produces a base layer that is a
            // byte-for-byte copy of the strands layer — 124 MB back on
            // schema_extract, and the only symptom a deploy-time size refusal.
            `&& grep -qE '^(${AGENT_ONLY_LAYER_PACKAGES.join('|')})([=<>!~ ]|$)' /asset-input/shared/runtime_requirements.txt`,
            '&& pip install -r /tmp/base_requirements.txt',
            '--python-version 3.12',
            '--platform manylinux2014_aarch64',
            '--target /asset-output/python',
            '--only-binary=:all:',
            '--no-cache-dir --upgrade',
            // The three copies StrandsAgentsLayer makes, unchanged: this layer
            // is a drop-in for it minus the agent wheels, so the shared modules,
            // the prompt templates (P28 R19.8 contract — /opt/python/
            // prompt_templates is prompt_template_loader's first search path)
            // and the pyspark templates all still land here.
            '&& cp -f /asset-input/shared/*.py /asset-output/python/ 2>/dev/null || true',
            '&& cp -rf /asset-input/shared/prompt_templates /asset-output/python/prompt_templates 2>/dev/null || true',
            '&& cp -rf /asset-input/pyspark_templates /asset-output/python/pyspark_templates 2>/dev/null || true',
            '&& find /asset-output/python -type d -name tests -exec rm -rf {} + 2>/dev/null || true',
            '&& find /asset-output/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true',
            '&& find /asset-output/python -name "*.pyc" -delete 2>/dev/null || true',
          ].join(' ')],
        },
      }),
    });

    // ADOT Lambda Layer — auto-instruments boto3, HTTP, and Lambda invocations as OTEL spans
    this.adotLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'AdotLayer',
      `arn:aws:lambda:${region}:901920570463:layer:aws-otel-python-arm64-ver-1-32-0:2`
    );

    // Local aliases for convenience within this stack
    const strandsLayer = this.strandsLayer;
    const adotLayer = this.adotLayer;

    // PyArrow Lambda Layer — dedicated layer for Parquet metadata-only schema extraction (Phase 9)
    // Separate from Strands layer to stay within the 250MB combined uncompressed limit.
    // Attached only to the schema_extract Lambda (the only Lambda that needs Parquet metadata reading).
    // NOTE: s3fs is excluded to save ~60MB — Parquet and ORC footer reads use
    // pyarrow.fs.S3FileSystem instead, a seekable handle with no object download.
    const pyarrowLayer = new lambda.LayerVersion(this, 'PyArrowLayer', {
      layerVersionName: `pyarrow-layer-${environment}`,
      description: `PyArrow for Parquet schema extraction (${environment})`,
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      code: lambda.Code.fromAsset(lambdaCodePath('agentic_data_ingestion_schema_extract'), {
        // The bundling command builds pyarrow from PyPI and reads nothing from
        // /asset-input, so this asset's hash is pure incidental coupling to the
        // schema_extract source directory. Excluding the generated files there
        // stops a local pytest run (which rewrites __pycache__) from rebuilding
        // a 100 MB+ pyarrow layer that has not changed.
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: LAMBDA_CODE_ASSET_EXCLUDE,
        bundling: {
          image: pinnedBuildImage(SAM_PYTHON312_ARM64_BUILD_IMAGE),
          command: ['bash', '-c',
            // P28 R19.10: the version spec MUST be quoted — unquoted, bash
            // treats `>=14.0.0` as a redirect and writes pip's stdout to a
            // stray file named `=14.0.0` in the asset dir (while pip installs
            // an unconstrained pyarrow). The stray artifacts were deleted.
            'pip install "pyarrow>=14.0.0"' +
            ' --python-version 3.12' +
            ' --platform manylinux2014_aarch64' +
            ' --target /asset-output/python' +
            ' --only-binary=:all:' +
            ' --no-cache-dir --upgrade' +
            ' && find /asset-output/python -type d -name tests -exec rm -rf {} + 2>/dev/null || true' +
            ' && find /asset-output/python -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true' +
            ' && find /asset-output/python -name "*.pyc" -delete 2>/dev/null || true' +
            ' && find /asset-output/python -name "*.pyi" -delete 2>/dev/null || true' +
            ' && rm -rf /asset-output/python/pyarrow/tests 2>/dev/null || true' +
            ' && rm -rf /asset-output/python/numpy/tests 2>/dev/null || true' +
            ' && rm -rf /asset-output/python/numpy/f2py 2>/dev/null || true' +
            ' && rm -rf /asset-output/python/numpy/typing 2>/dev/null || true',
          ],
        },
      }),
    });

    const hasMcp = props.agentCoreEndpoint ? 'true' : 'false';
    const hasS3TablesMcp = props.agentCoreS3TablesEndpoint ? 'true' : 'false';

    // Focused server profile endpoints — fall back to monolithic endpoint
    const codegenEndpoint = props.agentCoreCodegenEndpoint ?? props.agentCoreEndpoint ?? '';
    const analyticsEndpoint = props.agentCoreAnalyticsEndpoint ?? props.agentCoreEndpoint ?? '';

    // Agent Runtime endpoints (Optimization 3) — default off
    const createCodeAgentEndpoint = props.agentCoreCreateCodeEndpoint ?? '';
    const testCodeAgentEndpoint = props.agentCoreTestCodeEndpoint ?? '';
    const createPipelineAgentEndpoint = props.agentCoreCreatePipelineEndpoint ?? '';
    const createInfraAgentEndpoint = props.agentCoreCreateInfraEndpoint ?? '';

    // ── Lambda Functions ───────────────────────────────────────────
    this.fileValidationLambda = new lambda.Function(this, 'FileValidation', {
      functionName: 'agentic_data_ingestion_file_validation',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_file_validation'),
      role: foundation.fileValidationRole,
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        // file_validation requires PIPELINES_TABLE at import time
        // (require_env in lambda_function.py). The JDBC source-validation
        // branch (handle_jdbc_source_validation) reads source_config from
        // the pipelines table; without this the Lambda fails to import and
        // the first Step Functions state errors with
        // "Required environment variable 'PIPELINES_TABLE' is not set".
        PIPELINES_TABLE: data.pipelinesTable.tableName,
      },
    });

    this.schemaExtractionLambda = new lambda.Function(this, 'SchemaExtraction', {
      functionName: 'agentic_data_ingestion_schema_extract',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_schema_extract'),
      role: foundation.schemaExtractRole,
      // 15 min (Lambda max). The JDBC source path runs a Glue Crawler and
      // polls it to completion in-process. A crawler's wall-clock time
      // (cold start + provisioning + ~3 min crawl + teardown) routinely
      // exceeds the old 5-min Lambda timeout, so the poll loop could never
      // reach the crawler's READY state before the Lambda was killed. The
      // S3/Parquet path is fast and unaffected by the larger ceiling.
      timeout: cdk.Duration.seconds(900),
      memorySize: 512,
      // `sharedBaseLayer`, NOT `strandsLayer` — this is the one function in
      // Compute on the base layer, and the reason is the 250 MiB unzipped
      // ceiling rather than tidiness. With strands attached this function sat at
      // 260,821,097 of 262,144,000 bytes: 1,322,903 bytes / 0.5% free, so a
      // version bump to any of strands, pyarrow, mcp, jinja2 or boto3 was a
      // failed deploy. The swap returns ~118.81 MiB. See the SharedBaseLayer
      // declaration for the enumerated dependency set that makes it safe —
      // `schema_extract` imports no `strands`, `bedrock_agentcore` or `mcp` at
      // any depth — and `LGR-bb8073d-1` for why the split is the only route to
      // this and what its `revisit_when` asked for.
      //
      // ADOT is still excluded, but the REASON has changed and that is worth
      // being honest about: it used to be arithmetic (ADOT+Strands+PyArrow
      // exceeds the limit), and with 120 MiB free it would now fit. It stays off
      // as a deliberate hold, not a constraint — adding it is a cold-start and
      // tracing decision for whoever wants it, and
      // `p46-schema-extract-layers.test.ts` is where that decision has to be
      // made on purpose. Structured logging comes from Powertools Logger, which
      // the base layer carries.
      layers: [sharedBaseLayer, pyarrowLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        // p55 R8.3: the Excel branch reads the pipeline's Selected_Worksheet
        // from the pipelines table (schema_extract's get_pipeline_metadata).
        // Resolved lazily, in-function, so a deploy that lands the Lambda code
        // before this variable degrades only the Excel path rather than every
        // invocation — but the variable is what makes that path work at all.
        // No IAM change is needed: schemaExtractRole already carries
        // dynamodb:GetItem on table/agentic_data_ingestion* through the shared
        // DynamoDBPolicy attached by foundation-stack's lambdaRole helper.
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        // The Excel branch converts the workbook to CSV and writes the result to
        // the PROCESSED bucket — never back to the source bucket, whose
        // S3-upload EventBridge rule would then re-trigger the pipeline on its
        // own output.
        //
        // p55 task 4.3 could not set this (outside that task's file scope) and
        // handled the absence in Python instead:
        //   os.environ.get("PROCESSED_BUCKET")
        //     or bucket.replace("adi-source-files-", "adi-processed-files-", 1)
        // (`schema_extract/lambda_function.py:1766`). That fallback fails closed
        // — if the replace does not match, `target_bucket == bucket` and it
        // raises rather than writing into the watched bucket — but it makes the
        // Excel path depend on a bucket-name-derivation coincidence. Setting the
        // variable retires the coincidence; the fallback stays as the belt to
        // this braces and simply stops being exercised.
        //
        // This is a BEHAVIOUR CHANGE at deploy: the derivation branch goes cold.
        // The three other functions that already set this are `create_code`,
        // `test_code` and `dashboard_api`.
        //
        // No IAM change is needed, verified rather than assumed:
        // `schemaExtractRole` (foundation-stack.ts:262-264) grants
        // s3:GetObject / s3:PutObject / s3:ListBucket / s3:SelectObjectContent
        // on [sourceBucket, sourceBucket/*, processedBucket, processedBucket/*]
        // in one statement, so the PutObject this variable points at is already
        // permitted.
        PROCESSED_BUCKET: foundation.processedBucket.bucketName,
        // The JDBC path resolves DISCOVERY_DB = agentic_data_ingestion_jdbc_discovery_<AGENTIC_DATA_INGESTION_ENV>
        // at import time (lambda_function.py). Without this it defaults to
        // "_dev" and, on non-dev stages, queries the wrong discovery DB than
        // the one the dashboard API's crawler wrote to → "produced no table".
        AGENTIC_DATA_INGESTION_ENV: environment,
      },
    });

    this.schemaComparisonLambda = new lambda.Function(this, 'SchemaComparison', {
      functionName: 'agentic_data_ingestion_schema_compare',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_schema_compare'),
      role: foundation.schemaCompareRole,
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
      },
    });

    this.schemaMappingLambda = new lambda.Function(this, 'SchemaMapping', {
      functionName: 'agentic_data_ingestion_schema_mapping',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_schema_mapping'),
      role: foundation.schemaMappingRole,
      // 900 s, Lambda's maximum (was 600). The mapping call is non-streaming and
      // asks for up to 16,000 output tokens (model_config.DEFAULT_MAX_TOKENS), so
      // its client read timeout is 380 s and two attempts take up to ~782 s; the
      // arithmetic is at _BEDROCK_READ_TIMEOUT_SECONDS in
      // lambda/agentic_data_ingestion_schema_mapping/lambda_function.py, and
      // tests/unit/lambdas/schema_mapping/test_bedrock_client_config.py pins it
      // against this number (LGR-b397ee2-4).
      timeout: cdk.Duration.seconds(900),
      memorySize: 1024,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        APPROVALS_TABLE: data.approvalsTable.tableName,
        TARGET_SCHEMA_BUCKET: foundation.targetSchemasBucket.bucketName,
        BEDROCK_MODEL_ID,
        EVENTBRIDGE_ENABLED: 'true',
        MAPPING_MEMORY_ID: props.agentCoreMappingMemoryId ?? '',
        MEMORY_ENABLED: 'true',
        AGENT_TRACES_TABLE: data.agentTracesTable.tableName,
        AUDIT_TABLE: data.auditLogTable.tableName,
        COMPLEX_MAPPING_AGENT_ENDPOINT: props.agentCoreComplexMappingEndpoint ?? '',
        // P28 R15.2: cross-Lambda per-run model-call budget counter
        MODEL_CALL_BUDGET_TABLE: data.modelCallBudgetTable.tableName,
      },
    });

    // P28 R19.11 (idle-proxy billing): the four container-backed step Lambdas
    // (CreateCode, TestCode, CreateGluePipeline, CreateInfrastructure) run
    // with USE_AGENTCORE_RUNTIME=true, so the deployed handler only DISPATCHES
    // to the AgentCore container — under the waitForTaskToken shape the
    // container acks immediately and the proxy returns within seconds, so
    // memorySize is sized for module imports + one boto3 call (512 MB), not
    // for the inline agent workload. If USE_AGENTCORE_RUNTIME is ever flipped
    // off (break-glass inline execution), restore the pre-token sizes
    // (create_code/test_code 2048, create_glue_pipeline 1024). The 900s
    // timeout deliberately STAYS: during a deploy's version-skew window an
    // old container still runs the work synchronously (the Lambda-side
    // step_task_token relay finishes the token) — billing stops at return
    // either way, so duration, not memory ceiling, is the cost lever.
    this.codeGenerationLambda = new lambda.Function(this, 'CodeGeneration', {
      functionName: 'agentic_data_ingestion_create_code',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_create_code'),
      role: foundation.codeGenerationRole,
      timeout: cdk.Duration.seconds(900),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        GENERATED_CODE_TABLE: data.generatedCodeTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
        PROCESSED_BUCKET: foundation.processedBucket.bucketName,
        AGENTCORE_ENDPOINT: codegenEndpoint,
        MCP_ENABLED: hasMcp,
        MCP_SIGV4_ENABLED: 'true',
        EVENTBRIDGE_ENABLED: 'true',
        TEMPLATES_BUCKET: foundation.templatesBucket.bucketName,
        USE_AGENTCORE_RUNTIME: 'true',
        AWS_ACCOUNT_ID: account,
        AGENTCORE_CREATE_CODE_ENDPOINT: createCodeAgentEndpoint,
        AGENT_TRACES_TABLE: data.agentTracesTable.tableName,
        AUDIT_TABLE: data.auditLogTable.tableName,
      },
    });

    this.codeTestingLambda = new lambda.Function(this, 'CodeTesting', {
      functionName: 'agentic_data_ingestion_test_code',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_test_code'),
      role: foundation.codeTestingRole,
      timeout: cdk.Duration.seconds(900),
      // P28 R19.11: dispatch-only under waitForTaskToken (see CodeGeneration note)
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        GENERATED_CODE_TABLE: data.generatedCodeTable.tableName,
        TEST_RESULTS_TABLE: data.testResultsTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
        PROCESSED_BUCKET: foundation.processedBucket.bucketName,
        AGENTCORE_ENDPOINT: codegenEndpoint,
        MCP_ENABLED: hasMcp,
        MCP_SIGV4_ENABLED: 'true',
        EVENTBRIDGE_ENABLED: 'true',
        OTEL_PYTHON_CONTEXT: 'contextvars_context',
        TEMPLATES_BUCKET: foundation.templatesBucket.bucketName,
        USE_AGENTCORE_RUNTIME: 'true',
        AWS_ACCOUNT_ID: account,
        AGENTCORE_TEST_CODE_ENDPOINT: testCodeAgentEndpoint,
        AGENT_TRACES_TABLE: data.agentTracesTable.tableName,
        AUDIT_TABLE: data.auditLogTable.tableName,
        // P42 R2.5: verification executor selection — one of
        // {pandas, glue, shadow}. Resolution lives in lambda_function.py.
        //
        // R2.3's fall-through is now 'glue', not 'pandas': unset, empty and
        // unrecognised all select the Glue runner, and only an exact 'pandas'
        // or 'shadow' selects anything else. So this line no longer changes
        // WHICH executor runs — it states the intent explicitly rather than
        // inheriting it, and an unrecognised value here costs a billable Glue
        // session plus a WARNING rather than a quiet downgrade. Setting it
        // wrong is now more expensive than not setting it at all.
        //
        // NOW 'glue': real Spark on a Glue interactive session is the active
        // verification path. This is a DELIBERATE REVERSAL of the R11.2 hold,
        // not a drift. R11.2 pinned 'pandas' WHILE the concurrent-session
        // ceiling was unmeasured, and it is STILL unmeasured — task 1.3's
        // probe was written and deliberately never run because running it
        // creates billable sessions. So this is an accepted deviation from
        // R11.2 rather than a satisfaction of it. What was weighed: hitting
        // the ceiling surfaces as a capacity refusal, which the harness maps
        // to the 'session_capacity_exhausted' non-verification
        // (lambda_function.py:307, :1245) — a run then reports "not verified"
        // instead of failing, so the blast radius of an unmeasured ceiling is
        // an abstention, not a broken pipeline. Revisit if that abstention
        // rate becomes material, or once 11.1's measurement is taken.
        //
        // STILL SET IN BOTH PLACES, but the reason changed — read this before
        // trusting LGR-87b8538-1's rejected alternative. That record rejected
        // setting only compute-stack.ts because it "yields an inert flag on
        // the live path while every test passes": USE_AGENTCORE_RUNTIME:
        // 'true' above means this handler only DISPATCHES to the AgentCore
        // container, so the runner executes there, and the container would
        // have read an unset variable and resolved to 'pandas'.
        //
        // With the code fall-through now 'glue', omitting this variable from a
        // stack no longer downgrades the run, so test_verification_executor_env.py
        // is no longer what catches a one-sided set. Both copies are kept
        // anyway, and the test with them: they make the intended executor
        // legible in each stack, and a future move back to 'pandas' has to
        // touch both sides deliberately. Do not remove either side, and do not
        // change one without the other.
        VERIFICATION_EXECUTOR: 'glue',
        // P44 R1.7: verification frame-mode selection — one of
        // {synthetic, sample}. Resolution lives in glue_session_runner.py's
        // resolve_frame_source; the handler reads this variable at the point it
        // CONSTRUCTS the runner, not at import, so a warm container reflects a
        // change here on its next run.
        //
        // 'sample' means the Glue verification measures the transform against a
        // bounded 100-row sample of the run's OWN source object instead of 100
        // rows generated from the declared schema. A non-s3:// URI (every JDBC
        // source) or an unrecognised extension falls back to synthetic and
        // records why — that fallback is a fully verified run against a
        // generated frame, NOT a non-verification.
        //
        // MUST BE SET IN BOTH PLACES, and the obvious copy is the inert one.
        // LGR-87b8538-1 (template / ratified) rejected the one-stack form in its
        // own words: setting only compute-stack.ts "yields an inert flag on the
        // live path while every test passes". USE_AGENTCORE_RUNTIME: 'true'
        // above means this handler only DISPATCHES, so the runner executes in
        // the AgentCore container and reads the twin in agentcore-stack.ts's
        // agentContainerEnv — THAT is the live path, and THIS copy covers the
        // inline break-glass path. Do not remove either side, and do not change
        // one without the other.
        //
        // A near-miss spelling is NOT normalised: 'Sample' or 'sample ' takes
        // resolve_frame_source's unrecognised arm, builds a synthetic frame and
        // logs a WARNING naming the value (LGR-ad78946-1). So a typo here
        // silently reverts the change rather than failing; the allowed pair is
        // exactly {synthetic, sample}.
        //
        // DEPLOY ORDER IS LOAD-BEARING (P44 C11, R7.5). The read this enables
        // needs the VerificationSessionRole S3 grant on the source-files bucket
        // (foundation-stack.ts:877-885), which ships in Foundation. Deploying
        // this stack or AgentCore BEFORE Foundation means every single-file
        // S3-upload verification selects 'sample', cannot open the object, and
        // abstains — reported as "not verified" naming the source read, so
        // coverage degrades rather than a verdict being corrupted. Every test
        // passes in either order, which is why the ordering is a criterion and
        // not a runbook line.
        VERIFICATION_FRAME_MODE: 'sample',
        // P42 R6.4: the runtime role a Glue interactive session assumes. The
        // role is created by foundation-stack.ts (`VerificationSessionRole`,
        // physical name AgenticDataIngestion-VerificationSession-Role) and is
        // referenced HERE THROUGH THE CONSTRUCT, so CDK renders the cross-stack
        // export/import — the same shape as GLUE_PIPELINE_ROLE_ARN below
        // (`foundation.gluePipelineRole.roleArn`) and as the PassRole grant at
        // foundation-stack.ts:977, which names the role object for this reason.
        //
        // DO NOT rebuild this ARN from account and region. That was considered
        // and rejected; the reasoning is recorded at lambda_function.py:454-473.
        // A reconstruction encodes a CDK physical name in Python and silently
        // yields a valid-LOOKING ARN for a role that may not exist, turning a
        // missing resource into an opaque AccessDenied at CreateSession — the
        // slowest feedback loop this feature has.
        //
        // MUST BE SET IN BOTH PLACES, for exactly the reason VERIFICATION_EXECUTOR
        // above records: USE_AGENTCORE_RUNTIME: 'true' means this handler only
        // dispatches, so the runner reads this in the container. Its twin lives
        // in agentcore-stack.ts's agentContainerEnv. Unset, the handler's
        // resolve_effective_executor downgrades 'glue' to 'pandas' and logs at
        // ERROR (lambda_function.py:590-598), so neither 'glue' nor 'shadow' is
        // reachable however an operator configures VERIFICATION_EXECUTOR.
        //
        // Publishing this made 'glue' SELECTABLE; VERIFICATION_EXECUTOR above
        // is what SELECTS it, and as of the R11.2 reversal recorded there it
        // now does. This variable is therefore load-bearing on the live path
        // rather than latent: unset, resolve_effective_executor downgrades
        // 'glue' back to 'pandas' and logs at ERROR, silently returning every
        // run to the pandas proxy.
        VERIFICATION_SESSION_ROLE_ARN: foundation.verificationSessionRole.roleArn,
      },
    });

    this.createGluePipelineLambda = new lambda.Function(this, 'CreateGluePipeline', {
      functionName: 'agentic_data_ingestion_create_glue_pipeline',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_create_glue_pipeline'),
      role: foundation.gluePipelineRole,
      timeout: cdk.Duration.seconds(900),
      // P28 R19.11: dispatch-only under waitForTaskToken (see CodeGeneration note)
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        GENERATED_CODE_TABLE: data.generatedCodeTable.tableName,
        AGENTCORE_ENDPOINT: codegenEndpoint,
        AGENTCORE_CODEGEN_ENDPOINT: codegenEndpoint,
        AGENTCORE_ANALYTICS_ENDPOINT: analyticsEndpoint,
        MCP_ENABLED: hasMcp,
        MCP_SIGV4_ENABLED: 'true',
        EVENTBRIDGE_ENABLED: 'true',
        S3_TABLES_JAR_PATH: `s3://adi-dependencies-${account}-${region}/glue-jars/s3-tables-catalog-for-iceberg-runtime-0.1.5.jar`,
        TEMPLATES_BUCKET: foundation.templatesBucket.bucketName,
        GENERATED_CODE_BUCKET: foundation.generatedCodeBucket.bucketName,
        CHECKPOINT_BUCKET: foundation.checkpointsBucket.bucketName,
        // Threat model T-03: iam_roles.py creates every per-pipeline Glue role
        // with this boundary; Foundation refuses iam:CreateRole without it.
        // Twin in agentcore-stack.ts agentContainerEnv (the live path).
        GLUE_JOB_ROLE_BOUNDARY_ARN: foundation.glueJobRoleBoundary.managedPolicyArn,
        USE_AGENTCORE_RUNTIME: 'true',
        AWS_ACCOUNT_ID: account,
        AGENTCORE_CREATE_PIPELINE_ENDPOINT: createPipelineAgentEndpoint,
        AGENT_TRACES_TABLE: data.agentTracesTable.tableName,
        AUDIT_TABLE: data.auditLogTable.tableName,
      },
    });

    this.createInfrastructureLambda = new lambda.Function(this, 'CreateInfrastructure', {
      functionName: 'agentic_data_ingestion_create_infrastructure',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_create_infrastructure'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(600),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        USE_AGENTCORE_RUNTIME: 'true',
        AWS_ACCOUNT_ID: account,
        AGENTCORE_CREATE_INFRA_ENDPOINT: createInfraAgentEndpoint,
      },
    });

    this.createQualityChecksLambda = new lambda.Function(this, 'CreateQualityChecks', {
      functionName: 'agentic_data_ingestion_create_quality_checks',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_create_quality_checks'),
      role: foundation.qualityChecksRole,
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
      },
    });

    this.runPipelineLambda = new lambda.Function(this, 'RunPipeline', {
      functionName: 'agentic_data_ingestion_run_pipeline',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_run_pipeline'),
      role: foundation.runPipelineRole,
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        // T-22 residual: the Glue-callback task token is stored here, under
        // `glue_run#<run_id>`, not on the state row the AgentCore runtimes can
        // read. job_completion reads it back. Same table as the approval
        // tokens; every runtime role carries the DenyTaskTokens statement.
        TASK_TOKENS_TABLE: data.taskTokensTable.tableName,
      },
    });

    // P28 R9.3: self-healing dispatch — job_completion fire-and-forgets the
    // failure payload here (async Event invoke) and returns immediately;
    // this function owns the long SYNCHRONOUS AgentCore call (~10 min)
    // inside its own 15-minute timeout. The self-healing agent has no
    // Lambda surface (AgentCore Runtime container only), so this thin
    // dispatcher is the SELF_HEALING_FN target of the async invoke. Memory
    // is minimal — the function just waits on network I/O.
    this.selfHealingDispatchLambda = new lambda.Function(this, 'SelfHealingDispatch', {
      functionName: 'agentic_data_ingestion_self_healing_dispatch',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_self_healing_dispatch'),
      role: foundation.selfHealingDispatchRole,
      timeout: cdk.Duration.seconds(900),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        SELF_HEALING_AGENT_ENDPOINT: props.agentCoreSelfHealingEndpoint ?? '',
        AWS_ACCOUNT_ID: account,
      },
    });

    // p30a R1.2: generic agent dispatch — the generalisation of
    // SelfHealingDispatch above, not a new pattern. Its only job is the long
    // SYNCHRONOUS AgentCore call, and it exists because neither of its two
    // callers can make that call itself: an EventBridge rule cannot target
    // AgentCore Runtime at all, and a dashboard API handler cannot keep a
    // thread alive past its return (Lambda freezes the environment, which is
    // how the cost/quality "async" endpoints were silently losing their work).
    //
    // The 900s timeout is load-bearing, not a default. The handler pins
    // AGENT_READ_TIMEOUT_SECONDS = 870 and its docstring points HERE for the
    // enclosing budget: drop this below 870s and boto3's read timeout becomes
    // unreachable, so every long agent run surfaces as an opaque Lambda
    // timeout instead of a handled failure.
    //
    // The agent endpoints have to be on THIS function's environment, not just
    // the dashboard API's: callers pass `agent_endpoint_env` — the NAME of an
    // env var — and it is resolved in this process. Both values come from the
    // same props the dashboard API reads, so the two agree by construction.
    // Memory is minimal; the function only waits on network I/O.
    this.agentDispatchLambda = new lambda.Function(this, 'AgentDispatch', {
      functionName: 'agentic_data_ingestion_agent_dispatch',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_agent_dispatch'),
      role: foundation.agentDispatchRole,
      timeout: cdk.Duration.seconds(900),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        COST_OPTIMIZATION_AGENT_ENDPOINT: props.agentCoreCostOptimizationEndpoint ?? '',
        DATA_QUALITY_AGENT_ENDPOINT: props.agentCoreDataQualityEndpoint ?? '',
        AWS_ACCOUNT_ID: account,
      },
    });

    this.jobCompletionLambda = new lambda.Function(this, 'JobCompletion', {
      functionName: 'agentic_data_ingestion_job_completion',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_job_completion'),
      role: foundation.jobCompletionRole,
      timeout: cdk.Duration.seconds(300),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        // p30 R9.1: job_completion is the Processed_File_Ledger's only writer —
        // "processed" is only true once Glue reports SUCCEEDED.
        PROCESSED_FILES_TABLE: data.processedFilesTable.tableName,
        // Where run_pipeline stored this run's Glue-callback token (T-22).
        TASK_TOKENS_TABLE: data.taskTokensTable.tableName,
        // P28 R9.3: async dispatch target (replaces the direct
        // SELF_HEALING_AGENT_ENDPOINT + daemon-thread invoke).
        SELF_HEALING_FN: this.selfHealingDispatchLambda.functionName,
        // The Environment dimension of the success datapoint on the pipeline
        // error-rate series and of the run-cost metric. Unset, the handler
        // defaults to 'dev', which splits the series in any other environment
        // from record_failure's, which sets it.
        ENVIRONMENT: environment,
      },
    });

    this.approvalConfigLambda = new lambda.Function(this, 'ApprovalConfig', {
      functionName: 'agentic_data_ingestion_approval_config',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_approval_config'),
      role: foundation.approvalConfigRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        APPROVALS_TABLE: data.approvalsTable.tableName,
      },
    });

    // p47 R11 (task D14), measured 2026-08-23 — 512 MB is CONFIRMED, not assumed.
    //
    // MEMORY. 1889 real REPORT lines from
    // /aws/lambda/agentic_data_ingestion_dashboard_api (444455556666, us-west-2,
    // 2026-07-21..08-23, read-only `logs filter-log-events`): MaxMemoryUsed
    // min 176 / p50 202 / p90 224 / p99 240 / **max 258 MB** of 512 — a floor set
    // by the ADOT collector + boto3 + the 12 handler modules router.ts imports
    // eagerly, and ~50% headroom above the worst case ever observed. Zero
    // "Task timed out", "Runtime exited", "OutOfMemory" or "signal: killed" events
    // in the group's entire history. Locally, the largest marginal per-request
    // Python heap on the post-R7/R8 code (tracemalloc, moto-backed, 200 pipelines
    // x 10 runs x 7 steps) is 20.0 MB (/api/pipelines at page_size=100); /api/stats
    // is 13.3 MB cold and 0.08 MB on a cache hit.
    //
    // 256 MB is ruled out by measurement, not by caution: it is below the 258 MB
    // already observed. 384 MB nets only ~0.80x cost — the 25% rate cut is partly
    // given back because Lambda vCPU scales with memory (512 MB = 0.29 vCPU) and
    // init, which is 20.4% of billed duration here, is CPU-bound and would grow
    // 1.33x — in exchange for halving the headroom on a function whose measured
    // ceiling comes from a low-traffic dev account.
    //
    // Raising it is worse, and the reason is structural rather than empirical: for
    // work whose duration scales linearly with CPU, memory increases are exactly
    // cost-NEUTRAL (2x the rate, half the ms) — you buy latency, never cost. This
    // function's expensive tail is a ThreadPoolExecutor fan-out and DynamoDB
    // pagination, i.e. network-bound, so more CPU does not shorten it at all;
    // 1024 MB would need a 50% duration cut to break even and nothing measured
    // suggests one. Cold-start init (p50 2364 ms) is the one part that would
    // shrink, on 5.3% of invocations, and even a perfect 3.46x speedup of it at
    // 1769 MB still lands at 2.84x the cost.
    //
    // NOT MEASURED, on purpose: no duration-vs-memory curve exists here, because
    // producing one means deploying at several sizes. The numbers above therefore
    // bound the decision rather than optimise it.
    //
    // The 25 s timeout is LOAD-BEARING and must not be raised (R11.3). Three
    // in-code budgets are derived from it as "the ceiling minus 5 s in hand" —
    // `stats.py:_STATS_TIME_BUDGET_SECONDS`, `schemas.py:_LISTING_TIME_BUDGET_SECONDS`
    // and `pipelines_crud.py:_LIST_TIME_BUDGET_SECONDS`, all 20.0 — and
    // `LGR-14d6dd3-2` bounds the label fan-out "by a wall-clock deadline derived at
    // call time from the request's remaining budget" against this figure. Raising
    // the timeout would not move any of those, so it buys nothing while
    // invalidating what they were derived from; it also crosses API Gateway's 29 s
    // integration ceiling. Both values are pinned in
    // `test/p47-dashboard-api-sizing.test.ts`.
    this.dashboardApiLambda = new lambda.Function(this, 'DashboardApi', {
      functionName: 'agentic_data_ingestion_dashboard_api',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_dashboard_api'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(25),
      memorySize: 512,
      layers: [adotLayer, strandsLayer],
      environment: {
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
        GENERATED_CODE_TABLE: data.generatedCodeTable.tableName,
        TEST_RESULTS_TABLE: data.testResultsTable.tableName,
        TARGET_SCHEMAS_BUCKET: foundation.targetSchemasBucket.bucketName,
        APPROVALS_TABLE: data.approvalsTable.tableName,
        NOTIFICATIONS_TABLE: data.notificationsTable.tableName,
        EXECUTIONS_TABLE: data.executionsTable.tableName,
        TASK_TOKENS_TABLE: data.taskTokensTable.tableName,
        SOURCE_BUCKET: foundation.sourceBucket.bucketName,
        PROCESSED_BUCKET: foundation.processedBucket.bucketName,
        UPLOAD_BUCKET: foundation.sourceBucket.bucketName,
        STATE_MACHINE_NAME: 'agentic-data-ingestion-pipeline',
        STATE_MACHINE_ARN: `arn:aws:states:${region}:${account}:stateMachine:agentic-data-ingestion-pipeline`,
        LOCKS_TABLE: data.pipelineLocksTable.tableName,
        EVENTBRIDGE_ENABLED: 'true',
        ERRORS_TABLE: data.errorsTable.tableName,
        AUDIT_TABLE: data.auditLogTable.tableName,
        PIPELINE_TRIGGER_ARN: `arn:aws:lambda:${region}:${account}:function:agentic_data_ingestion_pipeline_trigger`,
        MAPPING_MEMORY_ID: props.agentCoreMappingMemoryId ?? '',
        MEMORY_ENABLED: 'true',
        AGENT_TRACES_TABLE: data.agentTracesTable.tableName,
        DATA_QUALITY_AGENT_ENDPOINT: props.agentCoreDataQualityEndpoint ?? '',
        COST_OPTIMIZATION_AGENT_ENDPOINT: props.agentCoreCostOptimizationEndpoint ?? '',
        NL_PIPELINE_BUILDER_AGENT_ENDPOINT: props.agentCoreNlPipelineBuilderEndpoint ?? '',
        SELF_HEALING_AGENT_ENDPOINT: props.agentCoreSelfHealingEndpoint ?? '',
        // p30a R2.2: async dispatch target for handlers/agents.py, the same
        // shape as SELF_HEALING_FN on JobCompletion above. handlers/agents.py
        // reads this name and fire-and-forgets an InvocationType="Event"
        // invoke; the lambda:InvokeFunction grant on infrastructureRole
        // (foundation-stack.ts) is inert without it, and both the quality-check
        // and cost-apply endpoints then report themselves unconfigured rather
        // than silently doing nothing. The two *_AGENT_ENDPOINT values above
        // are the same props AgentDispatch receives, which is what makes
        // passing `agent_endpoint_env` (a NAME, resolved in the dispatch
        // Lambda's process) safe.
        AGENT_DISPATCH_FN: this.agentDispatchLambda.functionName,
        // JDBC schema discovery (P17): handlers/jdbc_resources.py + pipelines_jdbc.py
        // create Glue Crawlers that execute as gluePipelineRole. Without these env
        // vars the wizard-time "Detect Schema" handler raises a RuntimeError before
        // any AWS call and returns HTTP 500. AGENTIC_DATA_INGESTION_ENV must match the stage so
        // DISCOVERY_DB (agentic_data_ingestion_jdbc_discovery_<env>) resolves to the Glue database
        // provisioned by the Foundation stack.
        GLUE_PIPELINE_ROLE_ARN: foundation.gluePipelineRole.roleArn,
        AGENTIC_DATA_INGESTION_ENV: environment,
      },
    });

    // Grant dashboard API Lambda read access to agent traces table
    data.agentTracesTable.grantReadData(this.dashboardApiLambda);

    // Grant schema mapping Lambda write access to audit table
    data.auditLogTable.grant(this.schemaMappingLambda, 'dynamodb:PutItem');

    // P28 R15.2: schema mapping Lambda increments the per-run model-call
    // budget counter (atomic ADD via UpdateItem in shared/model_config.py)
    data.modelCallBudgetTable.grant(this.schemaMappingLambda, 'dynamodb:UpdateItem');

    // Grant dashboard API Lambda append-only access to the audit table
    // (PutItem for writing entries, Query for reading them)
    data.auditLogTable.grant(this.dashboardApiLambda, 'dynamodb:PutItem', 'dynamodb:Query');

    // Grant Dashboard API Lambda permissions for pipeline deletion cleanup
    this.dashboardApiLambda.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['glue:DeleteJob'],
        resources: [`arn:aws:glue:${region}:${account}:job/agentic-data-ingestion-job-*`],
      }),
    );
    this.dashboardApiLambda.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['events:DeleteRule', 'events:RemoveTargets', 'events:ListTargetsByRule'],
        resources: [`arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-*`],
      }),
    );
    // Schedule rule management permissions (create, update, enable, disable)
    this.dashboardApiLambda.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['events:PutRule', 'events:PutTargets', 'events:EnableRule', 'events:DisableRule'],
        resources: [`arn:aws:events:${region}:${account}:rule/agentic-data-ingestion-schedule-*`],
      }),
    );
    // CloudWatch Logs read permissions for Glue job log viewer
    this.dashboardApiLambda.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['logs:DescribeLogStreams', 'logs:FilterLogEvents', 'logs:GetLogEvents'],
        resources: [
          `arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*`,
          `arn:aws:logs:${region}:${account}:log-group:/aws-glue/jobs/*:*`,
        ],
      }),
    );

    this.approvalHandlerLambda = new lambda.Function(this, 'ApprovalHandler', {
      functionName: 'agentic_data_ingestion_approval_handler',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_approval_handler'),
      role: foundation.approvalConfigRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        APPROVALS_TABLE: data.approvalsTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        EXECUTIONS_TABLE: data.executionsTable.tableName,
        NOTIFICATIONS_TABLE: data.notificationsTable.tableName,
        TASK_TOKENS_TABLE: data.taskTokensTable.tableName,
        // p54 R1.1 — REQUIRED, not optional. `get_approval_data`'s reuse arm
        // resolves a Reuse_Run's mapping by reading this table, and it builds
        // the handle at module scope through `require_env`, which RAISES rather
        // than defaulting outside local mode (`shared/env_utils.py:13-27`).
        // Omitting this key is a cold-start crash on every approval, not a
        // degraded reuse read. No IAM change accompanies it: `approvalConfigRole`
        // already grants `dynamodb:GetItem` on `table/agentic_data_ingestion*`
        // (`foundation-stack.ts`), which covers this table.
        MAPPINGS_TABLE: data.schemaMappingsTable.tableName,
        EVENTBRIDGE_BUS_NAME: `agentic-data-ingestion-pipeline-events-${environment}`,
        EVENTBRIDGE_ENABLED: 'true',
        MAPPING_MEMORY_ID: props.agentCoreMappingMemoryId ?? '',
        MEMORY_ENABLED: 'true',
      },
    });

    this.approvalTimeoutHandlerLambda = new lambda.Function(this, 'ApprovalTimeoutHandler', {
      functionName: 'agentic_data_ingestion_approval_timeout_handler',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_approval_timeout_handler'),
      role: foundation.approvalConfigRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        APPROVALS_TABLE: data.approvalsTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        // P28b R3.2: the timeout handler reads/deletes task tokens from the
        // dedicated table (send-before-status resequencing).
        TASK_TOKENS_TABLE: data.taskTokensTable.tableName,
      },
    });

    // ── Execution Updater (DynamoDB Stream → Lambda) ─────────────
    this.executionUpdaterLambda = new lambda.Function(this, 'ExecutionUpdater', {
      functionName: 'agentic_data_ingestion_execution_updater',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_execution_updater'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        EXECUTIONS_TABLE: data.executionsTable.tableName,
        STATE_TABLE: data.stateTable.tableName,
        PIPELINES_TABLE: data.pipelinesTable.tableName,
        ERRORS_TABLE: data.errorsTable.tableName,
      },
    });

    this.executionUpdaterLambda.addEventSource(
      new lambdaEventSources.DynamoEventSource(data.stateTable, {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 10,
        retryAttempts: 3,
      })
    );

    // ── WebSocket Lambdas ──────────────────────────────────────────
    this.wsConnectLambda = new lambda.Function(this, 'WsConnect', {
      functionName: 'agentic_data_ingestion_ws_connect',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_ws_connect'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      layers: [adotLayer, strandsLayer],
      environment: {
        WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
      },
    });

    this.wsDisconnectLambda = new lambda.Function(this, 'WsDisconnect', {
      functionName: 'agentic_data_ingestion_ws_disconnect',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_ws_disconnect'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      layers: [adotLayer, strandsLayer],
      environment: {
        WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
      },
    });

    this.wsBroadcastLambda = new lambda.Function(this, 'WsBroadcast', {
      functionName: 'agentic_data_ingestion_ws_broadcast',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_ws_broadcast'),
      role: foundation.infrastructureRole,
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      layers: [adotLayer, strandsLayer],
      environment: {
        WS_CONNECTIONS_TABLE: data.wsConnectionsTable.tableName,
        WS_API_ENDPOINT: 'placeholder-set-by-websocket-stack',
      },
    });

    // ── WebSocket Authorizer Lambda ─────────────────────────────────
    // Its own role, holding only what it calls (security review S2). This is the
    // one function that parses unauthenticated internet input (the `$connect`
    // token), with a hand-written verifier, on the one edge that has neither a WAF
    // nor CloudFront in front of it. It used to run as foundation.infrastructureRole,
    // which holds states:SendTaskSuccess/SendTaskFailure on '*' (completing an
    // approval gate without a human, the capability T-22 removed from every agent
    // runtime), s3tables:*, lambda:InvokeFunction and more. The function needs
    // CloudWatch Logs and the two ssm:GetParameters reads granted below; the JWKS
    // fetch is plain HTTPS and needs no IAM. No ADOT layer or active tracing is
    // configured on it, so no X-Ray grant either.
    const wsAuthorizerRole = new cdk.aws_iam.Role(this, 'WsAuthorizerRole', {
      roleName: `${projectName}-${environment}-ws-authorizer-role`,
      description: 'WebSocket $connect authorizer: CloudWatch Logs and the app-client-ID SSM parameters only',
      assumedBy: new cdk.aws_iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        cdk.aws_iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    this.wsAuthorizerLambda = new lambda.Function(this, 'WsAuthorizer', {
      functionName: 'agentic_data_ingestion_ws_authorizer',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'lambda_function.lambda_handler',
      code: lambdaCode('agentic_data_ingestion_ws_authorizer'),
      role: wsAuthorizerRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      layers: [strandsLayer],
      environment: {
        USER_POOL_ID: props.userPoolArn
          ? cdk.Fn.select(1, cdk.Fn.split('userpool/', props.userPoolArn))
          : '',
        // R-05: where to find the app clients whose tokens may open a
        // connection. The dashboard client lives in Hosting, which deploys after
        // this stack, so its ID cannot be an environment value here; Hosting
        // writes it to an SSM parameter and the authorizer reads it. A parameter
        // that does not exist yet contributes nothing, and with no client ID
        // found at all the authorizer denies every connection.
        USER_POOL_CLIENT_ID_PARAMETERS: (props.wsClientIdParameterNames ?? []).join(','),
      },
    });
    const wsClientIdParameterNames = props.wsClientIdParameterNames ?? [];
    if (wsClientIdParameterNames.length > 0) {
      // The named parameters only, on the authorizer's own role (S2).
      // String ARNs, not references: the parameters
      // belong to stacks that deploy after this one (Hosting) or may not exist
      // (Auth's, without the e2e client).
      this.wsAuthorizerLambda.addToRolePolicy(new cdk.aws_iam.PolicyStatement({
        actions: ['ssm:GetParameters'],
        resources: wsClientIdParameterNames.map((name) => `arn:aws:ssm:${region}:${account}:parameter${name}`),
      }));
    }

    // Grant WebSocket Lambdas read/write access to connections table
    data.wsConnectionsTable.grantReadWriteData(this.wsConnectLambda);
    data.wsConnectionsTable.grantReadWriteData(this.wsDisconnectLambda);
    data.wsConnectionsTable.grantReadWriteData(this.wsBroadcastLambda);

    // Broadcast Lambda triggered by executions table DynamoDB Stream
    this.wsBroadcastLambda.addEventSource(
      new lambdaEventSources.DynamoEventSource(data.executionsTable, {
        startingPosition: lambda.StartingPosition.TRIM_HORIZON,
        batchSize: 10,
        retryAttempts: 3,
      })
    );

    // ── Observability: ADOT + Powertools for ALL Lambdas ───────────
    const addObservability = (fn: lambda.Function, stepName: string) => {
      fn.addEnvironment('AWS_LAMBDA_EXEC_WRAPPER', '/opt/otel-instrument');
      fn.addEnvironment('OTEL_RESOURCE_ATTRIBUTES', `service.name=agentic-data-ingestion-${stepName}`);
      fn.addEnvironment('OTEL_PYTHON_DISTRO', 'aws_distro');
      fn.addEnvironment('OTEL_PYTHON_CONFIGURATOR', 'aws_configurator');
      fn.addEnvironment('OTEL_EXPORTER_OTLP_PROTOCOL', 'http/protobuf');
      fn.addEnvironment('OTEL_SDK_DISABLED', 'false');
      fn.addEnvironment('POWERTOOLS_ENABLED', 'true');
      fn.addEnvironment('POWERTOOLS_SERVICE_NAME', 'agentic_data_ingestion');
      fn.addEnvironment('POWERTOOLS_LOG_LEVEL', 'INFO');
    };

    addObservability(this.fileValidationLambda, 'FileValidation');
    // Schema extract has no ADOT layer (PyArrow takes its slot) — add Powertools only, skip OTEL wrapper
    this.schemaExtractionLambda.addEnvironment('POWERTOOLS_ENABLED', 'true');
    this.schemaExtractionLambda.addEnvironment('POWERTOOLS_SERVICE_NAME', 'agentic_data_ingestion');
    this.schemaExtractionLambda.addEnvironment('POWERTOOLS_LOG_LEVEL', 'INFO');
    addObservability(this.schemaComparisonLambda, 'SchemaCompare');
    addObservability(this.schemaMappingLambda, 'SchemaMapping');
    addObservability(this.codeGenerationLambda, 'CodeGeneration');
    addObservability(this.codeTestingLambda, 'CodeTesting');
    addObservability(this.createGluePipelineLambda, 'CreateGluePipeline');
    addObservability(this.createInfrastructureLambda, 'CreateInfrastructure');
    addObservability(this.createQualityChecksLambda, 'CreateQualityChecks');
    addObservability(this.runPipelineLambda, 'RunPipeline');
    addObservability(this.jobCompletionLambda, 'JobCompletion');
    addObservability(this.approvalConfigLambda, 'ApprovalConfig');
    addObservability(this.approvalHandlerLambda, 'ApprovalHandler');
    addObservability(this.approvalTimeoutHandlerLambda, 'ApprovalTimeout');
    addObservability(this.dashboardApiLambda, 'DashboardAPI');
    addObservability(this.executionUpdaterLambda, 'ExecutionUpdater');
    addObservability(this.wsConnectLambda, 'WSConnect');
    addObservability(this.wsDisconnectLambda, 'WSDisconnect');
    addObservability(this.wsBroadcastLambda, 'WSBroadcast');
    // WS Authorizer has no ADOT layer — add Powertools only
    this.wsAuthorizerLambda.addEnvironment('POWERTOOLS_ENABLED', 'true');
    this.wsAuthorizerLambda.addEnvironment('POWERTOOLS_SERVICE_NAME', 'agentic_data_ingestion');
    this.wsAuthorizerLambda.addEnvironment('POWERTOOLS_LOG_LEVEL', 'INFO');

    // ── WebSocket API Gateway ──────────────────────────────────────
    // Build the WebSocket authorizer when a Cognito User Pool is configured
    const wsAuthorizer = props.userPoolArn
      ? new WebSocketLambdaAuthorizer('WsConnectAuthorizer', this.wsAuthorizerLambda, {
          identitySource: ['route.request.querystring.token'],
        })
      : undefined;

    const webSocketApi = new apigatewayv2.WebSocketApi(this, 'AgenticDataIngestionWsApi', {
      apiName: `agentic-data-ingestion-ws-api-${environment}`,
      connectRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsConnectIntegration', this.wsConnectLambda),
        ...(wsAuthorizer ? { authorizer: wsAuthorizer } : {}),
      },
      disconnectRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsDisconnectIntegration', this.wsDisconnectLambda),
      },
      defaultRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsDefaultIntegration', this.wsConnectLambda),
      },
    });

    // Stage throttle (security review S8). A WebSocket API cannot be associated
    // with WAF, and the `$connect` authorizer is reachable before authentication,
    // so this is the only rate limit on the edge. It applies to every route of
    // the stage, across all connections: a dashboard sends a `$connect` and one
    // subscribe per page view, so 50 per second sustained with a burst of 100
    // leaves a small team far below the limit while bounding a flood at about
    // 3,000 authorizer invocations a minute instead of the account default of
    // 10,000 per second. An operational value: raise it for a larger team.
    const wsStage = new apigatewayv2.WebSocketStage(this, 'WsStage', {
      webSocketApi,
      stageName: 'prod',
      autoDeploy: true,
      throttle: { rateLimit: 50, burstLimit: 100 },
    });

    this.wsApiEndpoint = wsStage.url;

    // Grant broadcast Lambda permission to push messages to WebSocket connections.
    // Use a wildcard ARN to avoid a cyclic cross-stack reference — the Lambda role
    // lives in Foundation, and referencing the WebSocket API ID here would create
    // Foundation → Compute → Foundation cycle.
    this.wsBroadcastLambda.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ['execute-api:ManageConnections'],
        resources: [`arn:aws:execute-api:${region}:${account}:*/@connections/*`],
      }),
    );

    // Update broadcast Lambda's WS_API_ENDPOINT env var with the real URL
    // (overrides the placeholder set above)
    this.wsBroadcastLambda.addEnvironment('WS_API_ENDPOINT', wsStage.url);

    new cdk.CfnOutput(this, 'WsApiEndpoint', {
      value: wsStage.url,
      description: 'WebSocket API endpoint URL',
    });

    // ── REST API Gateway ───────────────────────────────────────────
    // CORS origin narrowing happens in ONE place: the Hosting stack's `/api/*`
    // response-headers policy (`apiCorsPolicy`, originOverride), which replaces
    // Access-Control-Allow-Origin on every response the viewer sees, including
    // this preflight's (LGR-f34fe5c-2). This stack deploys before Hosting, so the
    // CloudFront domain cannot be passed in here; an `allowedOrigin` prop that
    // looked like the knob for this was never wired and has been removed
    // (security review S10). Anyone fronting the API differently must narrow the
    // origin at their own edge, or replace ALL_ORIGINS here.
    const corsOrigins = apigateway.Cors.ALL_ORIGINS;

    // Access log for the REST stage (security review S9), one month like every
    // other log group here (log-retention.ts). `ip` is the CloudFront edge's
    // address, since every request arrives through the distribution; the viewer's
    // own address is in the CloudFront access log (Hosting stack). No field
    // carries a header or the query string, so no token reaches this log: the
    // dashboard sends its token in the Authorization header. `sub` is the
    // Cognito subject the authorizer verified, empty on an unauthorized request.
    const restApiAccessLogs = new cdk.aws_logs.LogGroup(this, 'RestApiAccessLogs', {
      logGroupName: `/aws/apigateway/${projectName}-${environment}-dashboard-api-access`,
      retention: LOG_RETENTION,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.api = new apigateway.RestApi(this, 'DashboardRestApi', {
      restApiName: `${projectName}-${environment}-dashboard-api`,
      description: 'AgenticDataIngestion Dashboard API - Resource-level catch-all pattern',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      binaryMediaTypes: ['multipart/form-data'],
      deployOptions: {
        stageName: 'v1',
        tracingEnabled: true,
        throttlingBurstLimit: 200,
        throttlingRateLimit: 100,
        loggingLevel: apigateway.MethodLoggingLevel.INFO,
        // Threat model T-09: data tracing writes full request and response
        // bodies (data previews, generated code, approval payloads) to the
        // execution log. INFO logging keeps per-request status, latency and
        // errors, which is what AwsSolutions-APIG6 asks for.
        dataTraceEnabled: false,
        metricsEnabled: true,
        accessLogDestination: new apigateway.LogGroupLogDestination(restApiAccessLogs),
        accessLogFormat: apigateway.AccessLogFormat.custom(JSON.stringify({
          requestId: apigateway.AccessLogField.contextRequestId(),
          requestTime: apigateway.AccessLogField.contextRequestTime(),
          ip: apigateway.AccessLogField.contextIdentitySourceIp(),
          userAgent: apigateway.AccessLogField.contextIdentityUserAgent(),
          httpMethod: apigateway.AccessLogField.contextHttpMethod(),
          path: apigateway.AccessLogField.contextPath(),
          status: apigateway.AccessLogField.contextStatus(),
          responseLength: apigateway.AccessLogField.contextResponseLength(),
          wafResponseCode: apigateway.AccessLogField.contextWafResponseCode(),
          sub: apigateway.AccessLogField.contextAuthorizerClaims('sub'),
        })),
      },
      defaultCorsPreflightOptions: {
        allowOrigins: corsOrigins,
        allowMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
        allowHeaders: [...apigateway.Cors.DEFAULT_HEADERS, 'Authorization'],
      },
    });

    // Use a single {proxy+} under /api to avoid Lambda permission policy size limit
    // Each individual resource+method creates a separate Lambda::Permission (20KB limit)
    const dashboardIntegration = new apigateway.LambdaIntegration(this.dashboardApiLambda);

    // Cognito User Pool authorizer — validates JWT tokens from the Cognito Hosted UI.
    // When userPoolArn is not provided, fall back to IAM auth for backward compatibility.
    let methodAuth: apigateway.MethodOptions;
    if (props.userPoolArn) {
      const userPool = cdk.aws_cognito.UserPool.fromUserPoolArn(this, 'ImportedUserPool', props.userPoolArn);
      const authorizer = new apigateway.CognitoUserPoolsAuthorizer(this, 'CognitoAuthorizer', {
        cognitoUserPools: [userPool],
        authorizerName: `agentic-data-ingestion-cognito-authorizer-${environment}`,
      });
      methodAuth = {
        authorizer,
        authorizationType: apigateway.AuthorizationType.COGNITO,
      };
    } else {
      methodAuth = { authorizationType: apigateway.AuthorizationType.IAM };
    }

    const apiRoot = this.api.root.addResource('api');
    const apiProxy = apiRoot.addResource('{proxy+}');
    apiProxy.addMethod('ANY', dashboardIntegration, methodAuth);
    apiRoot.addMethod('ANY', dashboardIntegration, methodAuth);

    this.apiEndpoint = this.api.url;

    // ── Origin-Verify Secret + REGIONAL WAF (CloudFront-only enforcement) ──
    // Only create when auth is enabled (userPoolArn provided) — matches the overall
    // authentication enablement pattern. Both this stack (WAF rule) and HostingStack
    // (CloudFront custom origin header) read the secret value from Secrets Manager.
    if (props.userPoolArn) {
      this.originVerifySecret = new secretsmanager.Secret(this, 'OriginVerifySecret', {
        secretName: `agentic-data-ingestion-${environment}-origin-verify`,
        description: 'Secret header value for CloudFront-to-API origin verification',
        generateSecretString: {
          excludePunctuation: true,
          passwordLength: 64,
        },
      });

      // The WAF rule needs the plaintext value at deploy time (embedded in the template).
      // It is never sent to end users, but anyone in the account who can read
      // configuration can read it without secretsmanager:GetSecretValue: in this
      // template (cloudformation:GetTemplate), in the WAF rule's SearchString
      // (wafv2:GetWebACL) and in the CloudFront custom origin header
      // (cloudfront:GetDistributionConfig). It is not rotated (SMG4). So it makes
      // direct execute-api invocation unlikely, not impossible; it is defence in
      // depth, and the Cognito authorizer applies either way (threat model T-12).
      const secretValue = this.originVerifySecret.secretValue.unsafeUnwrap();

      const apiWafAcl = new wafv2.CfnWebACL(this, 'ApiRegionalWaf', {
        name: `agentic-data-ingestion-${environment}-api-regional-waf`,
        scope: 'REGIONAL',
        defaultAction: { block: {} },
        visibilityConfig: {
          cloudWatchMetricsEnabled: true,
          metricName: `agentic-data-ingestion-${environment}-api-regional-waf`,
          sampledRequestsEnabled: true,
        },
        rules: [
          {
            name: 'AllowCloudFrontOriginVerify',
            priority: 0,
            action: { allow: {} },
            visibilityConfig: {
              cloudWatchMetricsEnabled: true,
              metricName: `agentic-data-ingestion-${environment}-allow-origin-verify`,
              sampledRequestsEnabled: true,
            },
            statement: {
              byteMatchStatement: {
                fieldToMatch: {
                  // `Name`, not `name`. CfnWebACL types `singleHeader` as `any`,
                  // so CDK writes its keys to the template verbatim, and the
                  // AWS::WAFv2::WebACL schema requires `Name` (CloudFormation
                  // Validate F3002/F3003 at synth). The lowercase key deployed
                  // only because CloudFormation's handler normalised it: the live
                  // ACL in both dev accounts already reads
                  // `SingleHeader: {Name: 'x-origin-verify'}`, so this changes the
                  // template, not the rule.
                  singleHeader: { Name: 'x-origin-verify' },
                },
                positionalConstraint: 'EXACTLY',
                searchString: secretValue,
                textTransformations: [{ priority: 0, type: 'NONE' }],
              },
            },
          },
        ],
      });

      // Associate the regional WAF with the REST API stage.
      // IMPORTANT: this association must be created AFTER the stage exists. Building
      // resourceArn from `restApiId` (a token) only makes CloudFormation infer a
      // dependency on the RestApi — NOT the 'v1' stage. Without an explicit dependency
      // on the deployment stage, a fresh `cdk deploy` can create the association before
      // the stage exists and fail at the AWS API with WAFv2 NotFound ("AWS WAF couldn't
      // perform the operation because your resource doesn't exist"). `cdk synth` does
      // not catch this race. Use the stage's ARN token and an explicit dependency.
      const apiWafAssociation = new wafv2.CfnWebACLAssociation(this, 'ApiWafAssociation', {
        webAclArn: apiWafAcl.attrArn,
        resourceArn: this.api.deploymentStage.stageArn,
      });
      apiWafAssociation.node.addDependency(this.api.deploymentStage);

      // Export the secret ARN so HostingStack can read it for the CloudFront origin header
      new cdk.CfnOutput(this, 'OriginVerifySecretArn', {
        value: this.originVerifySecret.secretArn,
        exportName: `${this.stackName}-OriginVerifySecretArn`,
        description: 'ARN of the X-Origin-Verify secret for CloudFront origin header',
      });
    }

    // Upload PySpark templates to S3.
    // P28 R19.8: the former PromptTemplates deployment (prompt_templates/
    // prefix) is deleted — prompts have ONE versioned source
    // (shared/prompt_templates, bundled into the Lambda layer and vendored
    // shared/ in containers) and the loader's S3 fallback is removed, so
    // nothing reads that prefix anymore. The TEMPLATES_BUCKET env var stays
    // on the Lambdas for the pyspark templates below.
    // Deliberately a MINIMAL exclude list, unlike the Docker contexts above.
    // Everything in pyspark_templates/ is shipped and read by the codegen path
    // (11 files: 5 job templates, 5 _partials, README), so an over-broad
    // exclusion here would not fail anything loudly — it would silently upload
    // fewer templates and surface as a Jinja TemplateNotFound at pipeline-run
    // time. Only locally generated artefacts are excluded.
    const GENERATED_ONLY_EXCLUDE = ['__pycache__', '*.pyc', '.DS_Store'];

    new s3deploy.BucketDeployment(this, 'PySparkTemplates', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', '..', 'pyspark_templates'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: GENERATED_ONLY_EXCLUDE,
      })],
      destinationBucket: foundation.templatesBucket,
      destinationKeyPrefix: 'templates/',
    });

    // Upload S3 Tables Iceberg JAR to dependencies bucket
    // Same reasoning: the only content is the Iceberg runtime JAR that Glue
    // jobs load by name. Excluding only .DS_Store, which macOS writes into the
    // directory and which would otherwise move this 27 MB asset's hash.
    new s3deploy.BucketDeployment(this, 'GlueJars', {
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', 'glue-jars'), {
        ignoreMode: cdk.IgnoreMode.GIT,
        exclude: ['.DS_Store'],
      })],
      destinationBucket: foundation.dependenciesBucket,
      destinationKeyPrefix: 'glue-jars/',
    });

    // ── Log retention (threat model T-09, R-06) ─────────────────────
    // Last, so every function in this stack exists. The REST stage's execution
    // log group is created by API Gateway, not by CloudFormation, so it gets
    // the same standalone retention resource.
    retainLambdaLogs(this);
    retainLogGroup(this, 'RestApiExecutionLogRetention',
      `API-Gateway-Execution-Logs_${this.api.restApiId}/${this.api.deploymentStage.stageName}`);
  }
}
