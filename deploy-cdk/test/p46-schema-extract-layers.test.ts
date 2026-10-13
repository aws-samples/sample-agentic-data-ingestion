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
 * `schema_extract` attaches exactly `[sharedBaseLayer, pyarrowLayer]` — p46 R7.4,
 * AMENDED for the layer split.
 *
 * WHAT CHANGED, AND WHY THIS IS AN AMENDMENT RATHER THAN A REWRITE
 * ---------------------------------------------------------------
 * The first layer in the pair used to be `strands-agents-layer`. It is now
 * `shared-base-layer` — the same manifest, the same `shared/*.py` copy and the
 * same templates, minus the agent stack (`strands-agents`,
 * `strands-agents-tools`, `bedrock-agentcore`, `mcp`) and its transitive
 * closure.
 *
 * `LGR-bb8073d-1` (**template / ratified**) named that split as "the only path
 * that reaches R10's actual goal", declined it as out of p47's scope, and stated
 * its `revisit_when` as **"the layer split is taken up"**. It has been, so this
 * file is amended rather than left to fail — and amended in place, per
 * `LGR-5108dd4-2`'s and `LGR-60c4985-6`'s amend-don't-delete rule, so the change
 * reads as a deliberate reversal and not as a regression. What this file OWNS is
 * unchanged: `schema_extract`'s exact layer list and its order
 * (`LGR-bb8073d-1`'s explicit exclusion list gives it "sole ownership" of both).
 * Only the expected value moved.
 *
 * WHY THIS EXISTS
 * ---------------
 * The 250 MB combined-unzipped Lambda layer limit was BINDING on this one
 * function, and until p46 nothing asserted it. `compute-stack.ts` excluded the
 * ADOT layer with the comment "combined ADOT+Strands+PyArrow exceeds 250MB
 * limit", and the PyArrow layer records `s3fs` excluded "to save ~60MB". Both
 * exclusions lived only in those comments.
 *
 * p46 task 1 measured what the comments left as an open question
 * (`.kiro/specs/p46-avro-orc-schema-extraction/layer-measurement.md`), against
 * the real bundling commands in the image CDK uses, cross-checked against the
 * deployed layer versions in the primary dev account/us-west-2:
 *
 *   strands-agents-layer   129,829,570 B   123.82 MiB
 *   pyarrow-layer          130,007,672 B   123.98 MiB
 *   function code               60,452 B     0.06 MiB
 *   ------------------------------------------------
 *   total                  259,897,694 B   247.86 MiB
 *   HEADROOM                 2,246,306 B     2.14 MiB   (under 1% of the limit)
 *
 * p55 then spent 41% of that on `openpyxl`, leaving 1,322,903 B / 1.26 MiB —
 * 0.5% of the limit — at which point a version bump to any of strands, pyarrow,
 * mcp, jinja2 or boto3 was a failed deploy rather than a slower one. Swapping the
 * strands layer for the base layer on this function returns 124,583,653 B /
 * 118.81 MiB. The post-split arithmetic and the measurement method live in
 * `p55-schema-extract-layer-budget.test.ts`, which owns the budget; this file
 * owns identity and order and deliberately does not restate the numbers as a
 * bound.
 *
 * The measurement is why this assertion is worth more than a tidy-up. Even with
 * the split, a third `python/`-installing layer here is not a graceful
 * degradation: `CreateFunction` refuses, so the failure surfaces as a deploy-time
 * service error that no test layer below CDK can observe. And in the other
 * direction, re-attaching the strands layer alongside the base layer would ship
 * `shared/*.py` and Powertools twice and put the function back over the ceiling —
 * which is why "exactly this pair" is still the property and not "at least".
 *
 * `LGR-b2f79ef-7` (template / ratified) is the governing precedent: it added a
 * CDK assertion for a grant that was already correct, on the ground that the
 * failure it guards "breaks with an AccessDenied that no test layer below CDK
 * can observe". Same argument, different service error. Like that record's
 * assertion, this one is a TRIPWIRE — it is true on the tree today (confirmed
 * live in task 1: the deployed function attaches exactly
 * `[strands-agents-layer-dev:315, pyarrow-layer-dev:156]`) and its value is
 * entirely in failing loudly when someone changes it.
 *
 * There is NO ledger record for the Layer_Budget itself. It exists only in the
 * two comments above, which is why the numbers are restated here rather than
 * cited.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * `LGR-1166391-1` (template / ratified) settled this for CDK-shape guards:
 * scan the synthesized template, because that is what CloudFormation hands to
 * the service, and a source-text scan can pass on input that still fails at
 * deploy. It also recorded that the Python `tests/cdk/` layer cannot render a
 * template at all (`aws-cdk-lib` is not in `pyproject.toml`; all files there
 * assert on `.ts` text), so vitest is the only suite that can make this
 * assertion at all. `Layers` is an ordered array of `Ref`s and literal ARNs —
 * exactly the kind of value a text scan reads badly.
 *
 * The convention notes in `hosting-auth.test.ts:271-274`,
 * `foundation-iam.test.ts:200-203` and `p28-idle-proxy.test.ts:22-25` say a
 * full ComputeStack synth exceeds vitest worker memory. As `LGR-1166391-1`
 * found, that cost is ASSET BUNDLING, which `aws:cdk:bundling-stacks: []`
 * removes; those notes name the cause themselves ("20+ Lambdas with
 * Code.fromAsset"). MEASURED here, not assumed: Foundation + Data + Auth +
 * Compute synthesize in ~2.6 s. Bundling changes asset content and asset
 * hashes, never a `Layers` entry.
 *
 * COUPLING TO `layerVersionName` IS DELIBERATE
 * --------------------------------------------
 * Layers are identified by their `LayerName` property rather than by the
 * hash-suffixed logical id, which would be the brittle choice. p46 R7.3 and
 * do-NOT 8 both forbid renaming the `PyArrowLayer` construct id or its
 * `layerVersionName`, so depending on that name is depending on something
 * already pinned, and a rename failing here is the intended behaviour.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const SCHEMA_EXTRACT = 'agentic_data_ingestion_schema_extract';
const STRANDS_LAYER_NAME = `strands-agents-layer-${TEST_ENV}`;
const SHARED_BASE_LAYER_NAME = `shared-base-layer-${TEST_ENV}`;
const PYARROW_LAYER_NAME = `pyarrow-layer-${TEST_ENV}`;

/**
 * Substring that identifies the ADOT layer in a rendered `Layers` entry. It is
 * attached via `fromLayerVersionArn`, so it renders as a literal ARN string
 * rather than a `Ref` (`compute-stack.ts:207-209`).
 */
const ADOT_MARKER = 'aws-otel-python-arm64';

/**
 * Non-vacuity floor for the ADOT check. 19 of the 21 named Lambdas in Compute
 * carry the ADOT layer today; `schema_extract` and `ws_authorizer` are the two
 * that do not. The floor is deliberately well below 19 so that adding or
 * removing ADOT from an unrelated Lambda does not fail this file — it exists
 * only to stop "no ADOT on schema_extract" passing trivially because ADOT was
 * removed stack-wide, at which point the assertion would be asserting nothing.
 */
const MIN_LAMBDAS_CARRYING_ADOT = 10;

interface RenderedFunction {
  logicalId: string;
  functionName: string;
  layers: unknown[];
}

function synthesizeCompute(): Record<string, any> {
  // Bundling disabled: see the header note. Asset content cannot affect a
  // `Layers` entry, and bundling is what exhausts the vitest worker.
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

/** Every `AWS::Lambda::Function` that declares a `FunctionName`. */
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
 * True when a rendered `Layers` entry names the ADOT layer. Stringified rather
 * than compared as a literal so an entry that becomes an `Fn::Join` (a
 * tokenized region, say) is still caught instead of silently passing.
 */
function mentionsAdot(entry: unknown): boolean {
  return JSON.stringify(entry).includes(ADOT_MARKER);
}

describe('ComputeStack — schema_extract layer attachment (p46 R7.4)', () => {
  let template: Record<string, any>;
  let functions: RenderedFunction[];
  let schemaExtract: RenderedFunction | undefined;
  let strandsId: string | undefined;
  let sharedBaseId: string | undefined;
  let pyarrowId: string | undefined;

  beforeAll(() => {
    template = synthesizeCompute();
    functions = namedFunctions(template);
    schemaExtract = functions.find((f) => f.functionName === SCHEMA_EXTRACT);
    strandsId = layerLogicalId(template, STRANDS_LAYER_NAME);
    sharedBaseId = layerLogicalId(template, SHARED_BASE_LAYER_NAME);
    pyarrowId = layerLogicalId(template, PYARROW_LAYER_NAME);
  }, 300_000);

  it('renders all three named layers, so the assertions below resolve something', () => {
    // A rename would otherwise make every check below compare against
    // `undefined` and fail confusingly. p46 R7.3 / do-NOT 8 forbid the rename.
    //
    // The strands layer is still required to RENDER even though this function no
    // longer carries it: the "not attached" assertion below is meaningless if the
    // layer does not exist, and 21 other Compute Lambdas still depend on it.
    expect(
      strandsId,
      `no AWS::Lambda::LayerVersion named '${STRANDS_LAYER_NAME}' in the Compute template. ` +
        'It is no longer attached to schema_extract, but 21 other Compute Lambdas still carry ' +
        'it — p47-strands-layer-attachment.test.ts owns that half.',
    ).toBeDefined();
    expect(
      sharedBaseId,
      `no AWS::Lambda::LayerVersion named '${SHARED_BASE_LAYER_NAME}' in the Compute template. ` +
        'This is the strands-free layer schema_extract now depends on for shared/*.py, ' +
        'aws-lambda-powertools and openpyxl; without it the function cannot import at all.',
    ).toBeDefined();
    expect(
      pyarrowId,
      `no AWS::Lambda::LayerVersion named '${PYARROW_LAYER_NAME}' in the Compute template — ` +
        'p46 R7.3 and do-NOT 8 forbid renaming the PyArrowLayer construct id or layerVersionName',
    ).toBeDefined();
    expect(schemaExtract, `no Lambda named '${SCHEMA_EXTRACT}' in the Compute template`).toBeDefined();
  });

  it('attaches exactly [sharedBaseLayer, pyarrowLayer] — two layers, in that order', () => {
    // AMENDED from [strandsLayer, pyarrowLayer]. See the header: the split is
    // `LGR-bb8073d-1`'s own revisit path and this is the expected value moving,
    // not the property changing.
    //
    // EXACTLY two, not "at least two", and the reason survives the split. A third
    // `python/`-installing layer here still does not degrade — CreateFunction
    // refuses the deploy — and re-adding the strands layer beside the base layer
    // would double-ship shared/*.py and Powertools and put the function straight
    // back over the ceiling. Order is asserted because both attached layers
    // install into `python/` and layer order decides which shadows the other on
    // sys.path.
    expect(
      schemaExtract!.layers,
      `${SCHEMA_EXTRACT} must attach exactly [${SHARED_BASE_LAYER_NAME}, ${PYARROW_LAYER_NAME}]. ` +
        'If this now shows the strands layer, the split has been reverted: that returns 124 MB ' +
        'to this function\'s package and leaves 1.26 MiB of the 250 MiB ceiling free, which is ' +
        'where p55 left it and why the split happened. p55-schema-extract-layer-budget.test.ts ' +
        'carries the arithmetic.',
    ).toEqual([{ Ref: sharedBaseId }, { Ref: pyarrowId }]);
  });

  it('does NOT attach the strands layer — that is the whole point of the split', () => {
    // Named separately from the exact-list assertion above, which would also
    // catch it, because the failure MESSAGE is the value here: a reader who
    // re-attaches strands needs to be told what it costs, not just that a list
    // did not match.
    const strandsEntries = schemaExtract!.layers.filter(
      (entry) => JSON.stringify(entry) === JSON.stringify({ Ref: strandsId }),
    );
    expect(
      strandsEntries,
      `${SCHEMA_EXTRACT} carries '${STRANDS_LAYER_NAME}'. That layer is 124.69 MiB and this ` +
        'function also carries 123.98 MiB of PyArrow, which is how it came to sit 0.5% under ' +
        'the 262,144,000-byte unzipped ceiling. It needs nothing from the agent stack: a full ' +
        'AST closure over lambda/agentic_data_ingestion_schema_extract/ (12 files, following ' +
        'every shared/*.py it reaches) finds no strands, bedrock_agentcore or mcp import at any ' +
        'depth. If the base layer is genuinely missing something, add it to ' +
        'shared/runtime_requirements.txt — do not bring the agent stack back for it.',
    ).toEqual([]);
  });

  it('does NOT attach the ADOT layer — now a deliberate hold, not arithmetic', () => {
    // AMENDED, and the amendment is the honesty rather than the assertion. This
    // check is unchanged; its JUSTIFICATION is not, and leaving the old reason in
    // place would have made it a false claim.
    //
    // BEFORE the split: ADOT (~50 MB) could not fit against 1.26 MiB of headroom,
    // so the exclusion was arithmetic and `compute-stack.ts` said so.
    // AFTER the split: ~120 MiB is free and ADOT WOULD fit. So the exclusion is
    // now a hold — `LGR-5906ec6-2`'s shape, where a deliberate posture lives in
    // an assertion so that reversing it means editing a test on purpose.
    //
    // Restoring ADOT here is a cold-start and tracing decision, not a budget one,
    // and 19 of the other 21 Compute Lambdas do carry it — so this is the single
    // most plausible future edit to this line. If you make it, amend this test in
    // the same change and say why, the way this amendment does.
    const adotEntries = schemaExtract!.layers.filter(mentionsAdot);
    expect(
      adotEntries,
      `${SCHEMA_EXTRACT} carries the ADOT layer. It now FITS (roughly 120 MiB free after the ` +
        'layer split, against ADOT\'s ~50 MB), so this is no longer a budget refusal — it is a ' +
        'hold. Schema extraction gets structured logging via Powertools Logger from the base ' +
        'layer. Adding ADOT is a legitimate decision; make it deliberately and amend this ' +
        'assertion in the same commit.',
    ).toEqual([]);
  });

  it('the ADOT check is non-vacuous: other Lambdas in this stack do carry ADOT', () => {
    // Without this, removing ADOT stack-wide would leave the check above green
    // while it had stopped asserting anything.
    const carriers = functions.filter((f) => f.layers.some(mentionsAdot));
    expect(
      carriers.length,
      `only ${carriers.length} Lambdas in Compute carry the ADOT layer — if ADOT was removed ` +
        'stack-wide, the exclusion assertion above is disabled rather than passing',
    ).toBeGreaterThanOrEqual(MIN_LAMBDAS_CARRYING_ADOT);
  });

  it('the PyArrow layer is attached to schema_extract and nothing else', () => {
    // "Attached only to the schema_extract Lambda". Same budget concern from the
    // other side — every other Lambda here carries ADOT + Strands, so adding
    // PyArrow to one of them exceeds the limit there. Unaffected by the split:
    // those 21 functions still carry the strands layer.
    const carriers = functions
      .filter((f) => f.layers.some((entry) => JSON.stringify(entry) === JSON.stringify({ Ref: pyarrowId })))
      .map((f) => f.functionName)
      .sort();
    expect(
      carriers,
      'the PyArrow layer is attached to a Lambda other than schema_extract. Every other Lambda in ' +
        'Compute already carries ADOT + Strands, so PyArrow on top exceeds the 250 MB limit there.',
    ).toEqual([SCHEMA_EXTRACT]);
  });
});
