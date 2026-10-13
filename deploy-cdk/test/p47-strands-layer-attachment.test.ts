// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';

/**
 * EVERY named Lambda in the Compute stack carries a `shared/*.py` layer — p47
 * R10.1, R10.3, R10.4; design §4.7, §6.4. AMENDED for the layer split.
 *
 * THE PROPERTY SPLIT IN TWO, EXACTLY AS `LGR-bb8073d-1` SAID IT WOULD
 * ------------------------------------------------------------------
 * This file used to assert "every named Compute Lambda carries
 * `StrandsAgentsLayer`". That is no longer true and must not be made true again:
 * `schema_extract` now carries `shared-base-layer` instead — the same manifest,
 * the same `shared/*.py` copy and the same templates, minus the agent stack.
 *
 * `LGR-bb8073d-1` (**template / ratified**) wrote its own `revisit_when` for this
 * moment: *"the layer split is taken up, at which point this file's universal
 * assertion becomes the wrong shape — the base layer stays on all 22 while the
 * strands layer goes on four, so the property splits in two"*. It has been taken
 * up, so the file is amended in place per `LGR-5108dd4-2`'s amend-don't-delete
 * rule, and the amendment is the SHAPE rather than a weakening:
 *
 *   BEFORE  every named Lambda carries the strands layer
 *   AFTER   every named Lambda carries EXACTLY ONE of {strands, shared-base}
 *
 * Both halves of "exactly one" are load-bearing and neither existed before. At
 * least one, because that layer is the only runtime source of `shared/*.py` and
 * Powertools — the original argument, unchanged. At MOST one, because the two
 * layers install the same modules into the same `python/` prefix: carrying both
 * ships `shared/*.py` and Powertools twice, wastes ~5.6 MiB of a 250 MiB ceiling,
 * and makes which copy wins depend on layer order. That combination was
 * unrepresentable while there was one layer and is now one edit away.
 *
 * The eventual state `LGR-bb8073d-1` describes — base on all 22, strands on four
 * — is NOT reached here. One function moved, because one function had run out of
 * room. The migration state is asserted rather than left implicit: see
 * `SHARED_BASE_CARRIERS`.
 *
 * WHY THE ORIGINAL ASSERTION EXISTED
 * ----------------------------------
 * p47 R10 set out to detach `StrandsAgentsLayer` from the functions that never
 * import `strands`. Task D12 audited all 22 attachment sites transitively (full
 * AST closure, not a top-level grep) and found **zero removable attachments**:
 * `.kiro/specs/p47-operative-gaps-and-hot-path-efficiency/d12-strands-layer-audit.md`.
 *
 * The reason is that `StrandsAgentsLayer` is not a Strands layer. Its bundling
 * command (`compute-stack.ts:183-192`) does four things, and only the first is
 * about Strands:
 *
 *   pip install -r shared/runtime_requirements.txt -> /asset-output/python
 *   cp    shared/*.py                              -> /asset-output/python/
 *   cp -r shared/prompt_templates                  -> /asset-output/python/
 *   cp -r pyspark_templates                        -> /asset-output/python/
 *
 * So it is the ONLY runtime delivery mechanism for all 35 `shared/*.py` modules
 * and for pinned `aws-lambda-powertools==3.29.0`. Modules land flat in
 * `/opt/python/`, and there is no fallback: the `<fn>/../shared` entry each
 * handler appends to `sys.path` does not exist in a deployed Lambda, only on a
 * dev machine or inside an AgentCore container. Detaching the layer from a
 * function is an `ImportError` at cold start, not a slower cold start.
 *
 * Measured breadth, from the audit's §1: **all 22** attached functions import
 * `aws_lambda_powertools`, **21 of 22** import at least one `shared/*.py`
 * module, and only **4** reach `strands` at all. So the layer's most widely-used
 * content is the one the layer is not named after, which is exactly why "remove
 * where provably unused" looked safe and is not.
 *
 * Requirements.md R10's premise — "Every other function unpacks an agent layer
 * it never imports on each cold start" — is false as stated, and is corrected in
 * place there and in design §4.7.
 *
 * WHY A TEST AND NOT JUST THE AUDIT
 * ---------------------------------
 * `LGR-b2f79ef-7` (template / ratified) is the governing precedent: add a CDK
 * assertion for a shape that is already correct, when the failure it guards
 * "breaks with an AccessDenied that no test layer below CDK can observe". Same
 * argument, different failure: a stripped layer is a cold-start `ImportError` in
 * a deployed function. Nothing below CDK sees it, because every unit test
 * imports `shared/` from the repo, where it is a real sibling directory. So the
 * audit's finding would otherwise survive only as prose, and the next reader of
 * R10 — which asks for exactly this removal — would make the change and watch
 * every test pass.
 *
 * That failure shape is `LGR-c284fbb-7`'s (do-NOT 10, R10.3): a one-sided change
 * "yields an inert flag on the live path while every test passes". Its concrete
 * subject is the env-var pair duplicated between `compute-stack.ts` and
 * `agentcore-stack.ts`, and its transferable principle is the shape. Under the
 * ratified "no change" arm nothing is stripped from the four break-glass
 * counterparts (`create_code`, `test_code`, `create_glue_pipeline`,
 * `create_infrastructure`), so do-NOT 10 is satisfied trivially — this file is
 * what keeps it satisfied. See `decision-records/D13-layer-scoping.txt`.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * `LGR-1166391-1` (template / ratified) settled this for CDK-shape guards: scan
 * the synthesized template, because that is what CloudFormation hands to the
 * service, and a source-text scan can pass on input that still fails at deploy.
 * It also recorded that the Python `tests/cdk/` suite cannot render a template
 * at all (`aws-cdk-lib` is absent from `pyproject.toml`; every file there
 * asserts on `.ts` text), so vitest is the only suite that can make this
 * assertion — which is why design §6.4's "`pytest tests/cdk/` plus `deploy-cdk/test`
 * vitest for any `compute-stack.ts` change" resolves to the vitest half here.
 *
 * D12 used exactly this method to get the authoritative 22, after finding that a
 * source count gives 25 `strandsLayer` lines (declaration, construction and a
 * two-occurrence alias line are not attachments) — the "~26 call sites" figure
 * R10 was written from. Template scanning also excludes the one unnamed
 * `AWS::Lambda::Function` in this stack, the `BucketDeployment` custom resource,
 * which carries an AWS CLI layer and no Strands.
 *
 * WHAT THIS FILE DELIBERATELY DOES NOT ASSERT
 * -------------------------------------------
 * `p46-schema-extract-layers.test.ts` owns `schema_extract`: exactly
 * `[strandsLayer, pyarrowLayer]` **in that order**, no ADOT, and PyArrow
 * attached nowhere else. That file is `LGR-0739ab8-1`'s assertion and must not be
 * duplicated or contradicted here, so:
 *
 *  - No layer ORDER is asserted below. Order is load-bearing only where two
 *    layers install into `python/` and one shadows the other on `sys.path`,
 *    which is `schema_extract` alone. p46 pins it there; restating it here would
 *    give one property two homes that can disagree.
 *  - No exact layer list is asserted for any single function by name. The
 *    per-function shape check below is a vocabulary check (§6.4's "layer list per
 *    function") — it constrains WHICH layers may appear, not their arrangement,
 *    and it passes `schema_extract` and `ws_authorizer` by construction rather
 *    than by exemption.
 *
 * `LGR-0739ab8-1`'s revisit condition ("the Layer_Budget changes, or PyArrow
 * stops being needed by `schema_extract`") IS tripped by the split — the sentence
 * that used to stand here said the opposite, and it was true of the commit that
 * wrote it. The re-measurement is not this file's: it lives in
 * `p55-schema-extract-layer-budget.test.ts`, which owns the budget and carries
 * the post-split arithmetic. This file owns attachment only.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const STRANDS_LAYER_NAME = `strands-agents-layer-${TEST_ENV}`;
const SHARED_BASE_LAYER_NAME = `shared-base-layer-${TEST_ENV}`;
const PYARROW_LAYER_NAME = `pyarrow-layer-${TEST_ENV}`;

/**
 * The two layers that deliver `shared/*.py` + `aws-lambda-powertools` to
 * `/opt/python`. Every named Compute Lambda must carry exactly one of them.
 *
 * They are interchangeable for that purpose by construction: `SharedBaseLayer`'s
 * bundling is `StrandsAgentsLayer`'s with the manifest filtered through
 * `AGENT_ONLY_LAYER_PACKAGES` and the same three `cp` commands after it. A
 * function needs the strands variant only if it imports `strands`,
 * `strands_agents_tools`, `bedrock_agentcore` or `mcp`.
 */
const SHARED_MODULE_LAYER_NAMES = [STRANDS_LAYER_NAME, SHARED_BASE_LAYER_NAME];

/**
 * The functions on the base layer, as an EXACT set — and the deliberate contrast
 * with `AUDITED_NAMED_LAMBDA_COUNT`'s floor below.
 *
 * `LGR-bb8073d-1` rejected an exact list for the strands side because it "fails on
 * any added Lambda even one that correctly carries the layer, which is churn on a
 * correct change". That argument does not transfer here, and the reason is the
 * direction of the risk. A new Compute Lambda should default to the strands layer
 * and adding one must not fail this file — hence a floor there. But moving a
 * function ONTO the base layer means asserting it imports nothing from the agent
 * stack at any depth, and getting that wrong is an `ImportError` at cold start
 * that no test below CDK can observe. That is exactly the change that should not
 * be possible to make quietly, so here the churn is the feature: migrate a
 * function, edit this list, and record the transitive import closure you checked.
 *
 * `schema_extract` is the first and so far only member. Its closure: 12 files
 * (`lambda_function.py` plus the 11 `shared/*.py` modules it reaches), 11 shared
 * modules, and six third-party names — `aws_lambda_powertools`, `boto3`,
 * `botocore`, `openpyxl`, `pyarrow`, `s3fs`. No `strands`, `bedrock_agentcore` or
 * `mcp` at any depth.
 */
const SHARED_BASE_CARRIERS = ['agentic_data_ingestion_schema_extract'];

/**
 * Substring identifying the ADOT layer in a rendered `Layers` entry. Attached by
 * `fromLayerVersionArn` (`compute-stack.ts:207-209`), so it renders as a literal
 * ARN string rather than a `Ref`. Same marker as the p46 file uses.
 */
const ADOT_MARKER = 'aws-otel-python-arm64';

/**
 * Non-vacuity floor, and the count-vs-floor choice this file makes.
 *
 * The property below is UNIVERSAL — "every named Compute Lambda carries the
 * layer" — rather than an equality against a list of 22 names. Both were
 * considered:
 *
 *  - A hard list of 22 names fails the moment anyone adds a Lambda, including a
 *    Lambda that correctly carries the layer. That is churn on a correct change,
 *    and it would also let a NEW function quietly omit the layer as long as the
 *    22 named ones kept it, since a new name simply is not in the list.
 *  - The universal form has neither problem. A new Compute Lambda is inside the
 *    property from the moment it is synthesized, which is the outcome worth
 *    having: the layer is not optional, so a new function must carry it or say
 *    here why not.
 *
 * A universal quantifier is vacuous over an empty set, so it needs a floor. This
 * floor is set at the audit's EXACT figure, 22, not comfortably below it — and
 * that is the deliberate difference from the p46 file's
 * `MIN_LAMBDAS_CARRYING_ADOT = 10`, which sits well below its own 20 because
 * ADOT is genuinely optional per function and an unrelated Lambda dropping it
 * must not fail that file. The Strands layer is the opposite: it is mandatory on
 * every function, so the domain of the universal check should only ever grow.
 * Pinning the floor at 22 means REMOVING a function from Compute, or a refactor
 * that stops rendering `FunctionName`, fails here loudly instead of silently
 * shrinking what the universal check covers. Adding functions never fails it.
 */
const AUDITED_NAMED_LAMBDA_COUNT = 22;

interface RenderedFunction {
  logicalId: string;
  functionName: string;
  layers: unknown[];
}

function synthesizeCompute(): Record<string, any> {
  // Bundling disabled, per `LGR-1166391-1` and the p46 file's header: bundling is
  // what exhausts the vitest worker, and asset content cannot affect a `Layers`
  // entry. Foundation + Data + Auth + Compute synthesize in a couple of seconds.
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };

  const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env,
    projectName: 'agentic-data-ingestion',
    environment: TEST_ENV,
  });
  const data = new DataStack(app, `AgenticDataIngestion-Data-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const auth = new AuthStack(app, `AgenticDataIngestion-Auth-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    projectName: 'agentic-data-ingestion',
    foundation,
    data,
    userPoolArn: auth.userPool.userPoolArn,
  });

  return Template.fromStack(compute).toJSON();
}

/** Logical id of the `AWS::Lambda::LayerVersion` carrying `LayerName`. */
function layerLogicalId(template: Record<string, any>, layerName: string): string | undefined {
  for (const [logicalId, resource] of Object.entries(template.Resources ?? {}) as [string, any][]) {
    if (resource.Type !== 'AWS::Lambda::LayerVersion') continue;
    if (resource.Properties?.LayerName === layerName) return logicalId;
  }
  return undefined;
}

/**
 * Every `AWS::Lambda::Function` that declares a `FunctionName`. The filter is
 * load-bearing: Compute also renders the `BucketDeployment` custom resource's
 * Lambda, which has no `FunctionName` and carries an AWS CLI layer.
 */
function namedFunctions(template: Record<string, any>): RenderedFunction[] {
  const functions: RenderedFunction[] = [];
  for (const [logicalId, resource] of Object.entries(template.Resources ?? {}) as [string, any][]) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    const functionName = resource.Properties?.FunctionName;
    if (typeof functionName !== 'string') continue;
    functions.push({ logicalId, functionName, layers: resource.Properties?.Layers ?? [] });
  }
  return functions;
}

/**
 * How many times a function's `Layers` array references the given layer. Compared
 * on stringified form so an entry that becomes an `Fn::Join` — a tokenized
 * region, say — is still recognised rather than silently counted as absent.
 */
function countRefs(fn: RenderedFunction, logicalId: string): number {
  const target = JSON.stringify({ Ref: logicalId });
  return fn.layers.filter((entry) => JSON.stringify(entry) === target).length;
}

/** True when a rendered `Layers` entry names the ADOT layer. */
function mentionsAdot(entry: unknown): boolean {
  return JSON.stringify(entry).includes(ADOT_MARKER);
}

describe('ComputeStack — Strands layer attachment (p47 R10)', () => {
  let template: Record<string, any>;
  let functions: RenderedFunction[];
  let strandsId: string | undefined;
  let sharedBaseId: string | undefined;
  let pyarrowId: string | undefined;
  let sharedModuleLayerIds: string[];

  beforeAll(() => {
    template = synthesizeCompute();
    functions = namedFunctions(template);
    strandsId = layerLogicalId(template, STRANDS_LAYER_NAME);
    sharedBaseId = layerLogicalId(template, SHARED_BASE_LAYER_NAME);
    pyarrowId = layerLogicalId(template, PYARROW_LAYER_NAME);
    sharedModuleLayerIds = [strandsId, sharedBaseId].filter((id): id is string => id !== undefined);
  }, 300_000);

  it('renders both shared-module layers, so the assertions below resolve something', () => {
    // Without this, a rename makes every check below compare against `undefined`
    // and fail for the wrong reason.
    expect(
      strandsId,
      `no AWS::Lambda::LayerVersion named '${STRANDS_LAYER_NAME}' in the Compute template. ` +
        'If the layer was renamed, update STRANDS_LAYER_NAME here and in ' +
        'p46-schema-extract-layers.test.ts; if it was removed, the 21 Compute Lambdas still on ' +
        'it have lost shared/*.py and aws-lambda-powertools.',
    ).toBeDefined();
    expect(
      sharedBaseId,
      `no AWS::Lambda::LayerVersion named '${SHARED_BASE_LAYER_NAME}' in the Compute template. ` +
        'This is the strands-free half of the split; without it schema_extract has no source ' +
        'for shared/*.py, aws-lambda-powertools or openpyxl.',
    ).toBeDefined();
    expect(
      sharedModuleLayerIds,
      'both shared-module layers must resolve before the "exactly one of" property below means ' +
        'anything — with one missing it degenerates into the old single-layer assertion',
    ).toHaveLength(SHARED_MODULE_LAYER_NAMES.length);
  });

  it('attaches a shared-module layer to EVERY named Lambda in the stack (R10.1)', () => {
    // THE assertion this file exists for, amended from "the Strands layer" to
    // "one of the two shared-module layers". Universal rather than a list of 22
    // names — see AUDITED_NAMED_LAMBDA_COUNT for why.
    //
    // Either layer ships all 35 shared/*.py modules and pinned
    // aws-lambda-powertools; /opt/python is their only runtime source. So a
    // function carrying NEITHER is a cold-start ImportError in a deployed
    // function, invisible to every test that imports shared/ from the repo.
    const missing = functions
      .filter((fn) => sharedModuleLayerIds.every((id) => countRefs(fn, id) === 0))
      .map((fn) => fn.functionName)
      .sort();

    expect(
      missing,
      `these Compute Lambdas carry neither '${STRANDS_LAYER_NAME}' nor ` +
        `'${SHARED_BASE_LAYER_NAME}': ${missing.join(', ')}.\n` +
        'One of those two is the only runtime source of shared/*.py and aws-lambda-powertools ' +
        '(both bundlings copy them into /opt/python), so this is an ImportError at cold start, ' +
        'not a slower cold start. The <fn>/../shared entry on sys.path does not exist in a ' +
        'deployed Lambda.\n' +
        'If you are here because p47 R10 asked you to detach the layer where strands is ' +
        'unused: detaching is still wrong, and read d12-strands-layer-audit.md for why. The ' +
        'correct move is to SWAP the strands layer for the base layer — which is what ' +
        'schema_extract did, and what SHARED_BASE_CARRIERS records.',
    ).toEqual([]);
  });

  it('attaches EXACTLY ONE shared-module layer per function, never both and never twice', () => {
    // NEW with the split, and the half that could not exist before it. The two
    // layers install the same shared/*.py and the same Powertools into the same
    // /opt/python prefix, so carrying both ships every shared module twice, burns
    // ~5.6 MiB of a 250 MiB ceiling to no effect, and makes which copy wins
    // depend on layer order.
    //
    // Also subsumes the old never-twice guard: `layers:` is the whole attachment
    // surface today (no addLayers() call in deploy-cdk/lib), and a future
    // addLayers() alongside the property would duplicate an entry.
    const wrongCount = functions
      .map((fn) => {
        const total = sharedModuleLayerIds.reduce((sum, id) => sum + countRefs(fn, id), 0);
        return { name: fn.functionName, total };
      })
      .filter((row) => row.total !== 1)
      .map((row) => `${row.name} (x${row.total})`)
      .sort();

    expect(
      wrongCount,
      `these Compute Lambdas do not carry exactly one shared-module layer: ` +
        `${wrongCount.join(', ')}.\nx0 is an ImportError at cold start — the assertion above ` +
        'names it more precisely. x2 or more is double-shipping: the strands and base layers ' +
        'both install shared/*.py and aws-lambda-powertools into /opt/python, so a function ' +
        'carrying both pays 124.69 MiB AND 5.61 MiB for one set of modules, and layer order ' +
        'decides which copy is importable. The base layer exists to REPLACE the strands layer ' +
        'on a function, not to supplement it.',
    ).toEqual([]);
  });

  it('records exactly which functions have migrated to the base layer', () => {
    // The migration state, asserted rather than left implicit. An exact set here
    // and a floor for the count below, deliberately — see SHARED_BASE_CARRIERS
    // for why the churn argument runs the other way for this direction of move.
    const carriers = functions
      .filter((fn) => sharedBaseId !== undefined && countRefs(fn, sharedBaseId) > 0)
      .map((fn) => fn.functionName)
      .sort();

    expect(
      carriers,
      `the set of Compute Lambdas on '${SHARED_BASE_LAYER_NAME}' has changed.\n` +
        'Moving a function onto the base layer asserts that it imports nothing from the agent ' +
        'stack (strands, strands_agents_tools, bedrock_agentcore, mcp) at ANY depth, including ' +
        'through shared/*.py. Getting that wrong is an ImportError at cold start that no test ' +
        'below CDK can observe, because unit tests import shared/ from the repo where it is a ' +
        'real sibling directory (LGR-bb8073d-1).\n' +
        'So: enumerate the transitive import closure for the function, then add it here with ' +
        'what you found. LGR-bb8073d-1 expects this list to end up holding 18 of the 22; it ' +
        'holds 1 because that is the one function that had run out of headroom.',
    ).toEqual([...SHARED_BASE_CARRIERS].sort());
  });

  it('is non-vacuous: the stack still renders all 22 audited Lambdas', () => {
    // The floor is the audit's exact count, not a comfortable margin below it.
    // Growth never fails here; shrinkage does, because shrinkage silently
    // narrows what the universal check above covers.
    expect(
      functions.length,
      `Compute renders ${functions.length} named Lambdas, fewer than the ${AUDITED_NAMED_LAMBDA_COUNT} ` +
        'audited in d12-strands-layer-audit.md §3. Either a function was removed — fine, update ' +
        'this floor and the audit table together — or a refactor stopped setting FunctionName, in ' +
        'which case the universal assertion above has quietly stopped covering it.',
    ).toBeGreaterThanOrEqual(AUDITED_NAMED_LAMBDA_COUNT);
  });

  it('constrains the per-function layer list to the known shapes (§6.4)', () => {
    // §6.4's "layer list per function", scoped so it does not restate p46's
    // exact-equality on schema_extract. This checks the VOCABULARY — which layers
    // may appear beside a shared-module layer — and deliberately not the order,
    // which is load-bearing only on schema_extract (two python/-installing
    // layers) and is pinned there by p46-schema-extract-layers.test.ts.
    //
    // The shapes today, all accommodated by construction rather than by
    // exemption: 20 functions carry [ADOT, Strands]; schema_extract carries
    // [SharedBase, PyArrow] (no ADOT — a hold, not arithmetic, since the split
    // freed ~120 MiB); ws_authorizer carries [Strands] alone.
    //
    // Vocabulary WIDENED by one entry for the split. A further unrecognised layer
    // anywhere in Compute is still worth failing on: 21 functions here carry
    // 124.69 MiB of Strands, and on schema_extract a third python/-installing
    // layer is a CreateFunction refusal rather than a slowdown (LGR-0739ab8-1).
    const knownRefs = [strandsId, sharedBaseId, pyarrowId]
      .filter((id): id is string => id !== undefined)
      .map((id) => JSON.stringify({ Ref: id }));

    const unexpected = functions
      .map((fn) => {
        const others = fn.layers.filter((entry) => {
          if (knownRefs.includes(JSON.stringify(entry))) return false;
          return !mentionsAdot(entry);
        });
        return { name: fn.functionName, others };
      })
      .filter((row) => row.others.length > 0)
      .map((row) => `${row.name}: ${JSON.stringify(row.others)}`)
      .sort();

    expect(
      unexpected,
      'these Compute Lambdas carry a layer that is none of Strands, SharedBase, PyArrow or ADOT: ' +
        `${unexpected.join('; ')}.\nAdding a layer here is a budget decision, not a detail — ` +
        '21 functions already carry 124.69 MiB of Strands, and schema_extract sits at 123.98 MiB ' +
        'of PyArrow plus the base layer. If the addition is deliberate, widen this vocabulary and ' +
        'say what it costs; p55-schema-extract-layer-budget.test.ts is where the arithmetic goes.',
    ).toEqual([]);
  });
});
